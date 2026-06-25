#!/usr/bin/env python3
"""GitHub PR status classification and Discord status rendering helpers."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json

ACTION_WORDS = (
    "fix",
    "change",
    "block",
    "broken",
    "bug",
    "fail",
    "failing",
    "rebase",
    "conflict",
    "requested",
    "must",
    "need",
    "needs",
    "please",
)
NON_ACTIONABLE_REVIEW_STATES = {"APPROVED", "DISMISSED"}
ISSUE_SEVERITY = {
    "changes_requested": 10,
    "rebase_required": 9,
    "check_failed": 8,
    "review_comment": 5,
    "issue_comment": 4,
    "awaiting_re_review": 2,
    "stacked_base_blocked": 1,
}
NON_PING_ISSUE_KINDS = {"awaiting_re_review", "stacked_base_blocked"}
ACTIVE_ALERT_STATES = {"ACTION_PENDING", "ACTION_IN_PROGRESS", "FIX_PUSHED_WAITING_CI_OR_REVIEW"}


def run_gh_json(args: list[str]) -> Any:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout or "null")


def gh_login() -> str:
    result = subprocess.run(["gh", "api", "user", "--jq", ".login"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def search_open_prs(author: str, query_extra: str = "") -> list[dict[str, Any]]:
    query = f"is:pr is:open author:{author} archived:false {query_extra}".strip()
    result = run_gh_json(["api", "-X", "GET", "/search/issues", "-f", f"q={query}", "-f", "per_page=100"])
    items = result.get("items", []) if isinstance(result, dict) else []
    prs = []
    for item in items:
        repo_url = item.get("repository_url", "")
        owner_repo = repo_url.rsplit("/repos/", 1)[-1]
        if "/" not in owner_repo:
            continue
        owner, repo = owner_repo.split("/", 1)
        prs.append({"owner": owner, "repo": repo, "number": item.get("number")})
    return prs


def fetch_pr_details(owner: str, repo: str, number: int) -> dict[str, Any]:
    pr = run_gh_json(["api", "-X", "GET", f"repos/{owner}/{repo}/pulls/{number}"])
    reviews = run_gh_json(["api", "-X", "GET", f"repos/{owner}/{repo}/pulls/{number}/reviews", "-f", "per_page=100"])
    issue_comments = run_gh_json(["api", "-X", "GET", f"repos/{owner}/{repo}/issues/{number}/comments", "-f", "per_page=100"])
    review_comments = run_gh_json(["api", "-X", "GET", f"repos/{owner}/{repo}/pulls/{number}/comments", "-f", "per_page=100"])
    head_sha = (pr.get("head") or {}).get("sha")
    check_runs: list[dict[str, Any]] = []
    if head_sha:
        checks = run_gh_json(["api", "-X", "GET", f"repos/{owner}/{repo}/commits/{head_sha}/check-runs", "-f", "per_page=100"])
        check_runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "title": pr.get("title"),
        "html_url": pr.get("html_url"),
        "author": ((pr.get("user") or {}).get("login")),
        "draft": pr.get("draft", False),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "head_sha": head_sha,
        "head_ref": (pr.get("head") or {}).get("ref"),
        "head_repo_full_name": ((pr.get("head") or {}).get("repo") or {}).get("full_name"),
        "base_ref": (pr.get("base") or {}).get("ref"),
        "base_repo_full_name": ((pr.get("base") or {}).get("repo") or {}).get("full_name"),
        "reviews": reviews if isinstance(reviews, list) else [],
        "issue_comments": issue_comments if isinstance(issue_comments, list) else [],
        "review_comments": review_comments if isinstance(review_comments, list) else [],
        "check_runs": check_runs,
    }


def _actor_login(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("login")
    if value:
        return str(value)
    return None


def _latest_review_by_user(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        user = _actor_login(review.get("user"))
        if not user:
            continue
        latest[str(user)] = review
    return latest


def _comment_is_actionable(comment: dict[str, Any], operator_login: str) -> bool:
    user = _actor_login(comment.get("user"))
    if str(user) == operator_login:
        return False
    body = str(comment.get("body") or "").lower()
    return any(word in body for word in ACTION_WORDS)


def classify_pr(pr: dict[str, Any], *, operator_login: str) -> dict[str, Any]:
    """Classify one PR for the operator status channel."""
    issues: list[dict[str, Any]] = []
    mergeable_state = str(pr.get("mergeable_state") or "").lower()
    if mergeable_state in {"dirty", "behind"}:
        issues.append({
            "kind": "rebase_required",
            "summary": f"branch merge state is `{mergeable_state}`; rebase/update before review can clear",
            "autocure": "rebase_supported_with_operator_authorization",
        })

    for reviewer, review in _latest_review_by_user(pr.get("reviews", [])).items():
        state = str(review.get("state") or "").upper()
        if state in NON_ACTIONABLE_REVIEW_STATES:
            continue
        if state == "CHANGES_REQUESTED":
            review_sha = review.get("commit_id") or review.get("commit", {}).get("oid")
            if review_sha and pr.get("head_sha") and str(review_sha) != str(pr.get("head_sha")):
                issues.append({
                    "kind": "awaiting_re_review",
                    "summary": f"{reviewer} requested changes on an older SHA; fix appears pushed, awaiting re-review",
                    "url": review.get("html_url"),
                    "autocure": "none_wait_for_re_review",
                    "review_sha": review_sha,
                    "head_sha": pr.get("head_sha"),
                })
            else:
                issues.append({
                    "kind": "changes_requested",
                    "summary": f"{reviewer} requested changes",
                    "url": review.get("html_url"),
                    "autocure": "manual_or_targeted_fix",
                })

    for run in pr.get("check_runs", []):
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if status == "completed" and conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
            issues.append({
                "kind": "check_failed",
                "summary": f"check `{run.get('name')}` concluded `{conclusion}`",
                "url": run.get("html_url"),
                "autocure": "ci_log_diagnosis_supported",
            })

    for comment in pr.get("review_comments", []):
        if _comment_is_actionable(comment, operator_login):
            issues.append({
                "kind": "review_comment",
                "summary": f"review comment by {_actor_login(comment.get('user'))}: {str(comment.get('body') or '')[:120]}",
                "url": comment.get("html_url"),
                "autocure": "manual_or_targeted_fix",
            })

    for comment in pr.get("issue_comments", []):
        if _comment_is_actionable(comment, operator_login):
            issues.append({
                "kind": "issue_comment",
                "summary": f"PR comment by {_actor_login(comment.get('user'))}: {str(comment.get('body') or '')[:120]}",
                "url": comment.get("html_url"),
                "autocure": "manual_triage",
            })

    issues.sort(key=lambda issue: (-ISSUE_SEVERITY.get(issue["kind"], 0), issue.get("summary", "")))
    pr_id = f"{pr.get('owner')}/{pr.get('repo')}#{pr.get('number')}"
    state = "DRAFT" if pr.get("draft") else "OK"
    if issues:
        state = "WAITING" if all(issue.get("kind") in NON_PING_ISSUE_KINDS for issue in issues) else "ISSUES"
    return {
        "id": pr_id,
        "owner": pr.get("owner"),
        "repo": pr.get("repo"),
        "number": pr.get("number"),
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "author": pr.get("author"),
        "draft": bool(pr.get("draft")),
        "head_sha": pr.get("head_sha"),
        "head_ref": pr.get("head_ref"),
        "base_ref": pr.get("base_ref"),
        "state": state,
        "issues": issues,
        "updated_at": utc_now(),
    }



def apply_stacked_pr_blocks(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark child PRs as waiting when their base branch is another open PR with issues.

    This prevents a stacked PR from looking merge-ready while its parent PR is
    still blocked. It is intentionally non-pinging: the action belongs on the
    parent PR thread.
    """
    by_head = {status.get("head_ref"): status for status in statuses if status.get("head_ref")}
    for status in statuses:
        base_ref = status.get("base_ref")
        parent = by_head.get(base_ref)
        if not parent or parent is status:
            continue
        if parent.get("state") not in {"ISSUES", "WAITING", "DRAFT"}:
            continue
        issues = status.setdefault("issues", [])
        parent_id = parent.get("id")
        if any(issue.get("kind") == "stacked_base_blocked" and issue.get("parent") == parent_id for issue in issues):
            continue
        issues.append({
            "kind": "stacked_base_blocked",
            "summary": f"stacked on {parent_id}, which is {str(parent.get('state')).lower()}; work the parent before this child",
            "url": parent.get("url"),
            "parent": parent_id,
            "autocure": "work_parent_pr_first",
        })
        issues.sort(key=lambda issue: (-ISSUE_SEVERITY.get(issue["kind"], 0), issue.get("summary", "")))
        if status.get("state") == "OK":
            status["state"] = "WAITING"
    return statuses

def status_fingerprint(status: dict[str, Any]) -> str:
    payload = {
        "id": status.get("id"),
        "issues": [
            {"kind": issue.get("kind"), "summary": issue.get("summary"), "url": issue.get("url")}
            for issue in status.get("issues", [])
        ],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def format_status_message(status: dict[str, Any]) -> str:
    state = str(status.get("state") or "unknown")
    icon = {"OK": "✅", "DRAFT": "📝", "ISSUES": "🚨", "WAITING": "⏳"}.get(state, "•")
    label = "needs attention" if state == "ISSUES" else ("waiting" if state == "WAITING" else state.lower())
    lines = [
        f"{icon} **{status.get('id')}** — {label}",
        f"<{status.get('url')}>",
        f"- title: {status.get('title')}",
        f"- branch: `{status.get('head_ref')}` → `{status.get('base_ref')}`",
        f"- head: `{str(status.get('head_sha') or '')[:12]}`",
    ]
    issues = status.get("issues", [])
    if issues:
        lines.append("- issues:")
        for issue in issues[:8]:
            url = f" ({issue['url']})" if issue.get("url") else ""
            lines.append(f"  - `{issue['kind']}`: {issue.get('summary')}{url}")
    else:
        lines.append("- issues: none detected")
    return "\n".join(lines)[:1900]


def load_discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
    if token:
        return token
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {"DISCORD_BOT_TOKEN", "DISCORD_TOKEN"}:
                return value.strip().strip('"\'')
    raise RuntimeError("DISCORD_BOT_TOKEN not found")


def discord_request(token: str, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes PR status monitor",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {body}") from exc


def ensure_discord_text_channel(token: str, guild_id: str, name: str, *, topic: str) -> dict[str, Any]:
    channels = discord_request(token, f"/guilds/{guild_id}/channels")
    normalized = name.lower().replace(" ", "-")
    for channel in channels:
        if channel.get("type") == 0 and str(channel.get("name", "")).lower() == normalized:
            return {"id": str(channel["id"]), "name": channel["name"], "created": False}
    created = discord_request(token, f"/guilds/{guild_id}/channels", method="POST", payload={
        "name": normalized,
        "type": 0,
        "topic": topic[:1024],
    })
    return {"id": str(created["id"]), "name": created["name"], "created": True}


def post_message(token: str, channel_id: str, content: str) -> str:
    message = discord_request(token, f"/channels/{channel_id}/messages", method="POST", payload={"content": content[:1900]})
    return str(message["id"])


def edit_message(token: str, channel_id: str, message_id: str, content: str) -> None:
    discord_request(token, f"/channels/{channel_id}/messages/{message_id}", method="PATCH", payload={"content": content[:1900]})


def ensure_thread(token: str, channel_id: str, message_id: str, name: str) -> str:
    thread = discord_request(
        token,
        f"/channels/{channel_id}/messages/{message_id}/threads",
        method="POST",
        payload={"name": name[:100], "auto_archive_duration": 10080},
    )
    return str(thread["id"])


def archive_thread(token: str, thread_id: str) -> None:
    # Discord closes threads by archiving them; locking prevents a resolved PR
    # thread from being reopened and re-used for a stale action.
    discord_request(token, f"/channels/{thread_id}", method="PATCH", payload={"archived": True, "locked": True})


def parse_pr_key(pr_key: str) -> tuple[str, str, int] | None:
    try:
        owner_repo, number = pr_key.rsplit("#", 1)
        owner, repo = owner_repo.split("/", 1)
        return owner, repo, int(number)
    except (TypeError, ValueError):
        return None


def fetch_pr_merge_state(pr_key: str) -> dict[str, Any] | None:
    parsed = parse_pr_key(pr_key)
    if not parsed:
        return None
    owner, repo, number = parsed
    pr = run_gh_json(["api", "-X", "GET", f"repos/{owner}/{repo}/pulls/{number}"])
    if not isinstance(pr, dict):
        return None
    return {
        "state": pr.get("state"),
        "merged": bool(pr.get("merged_at")),
        "merged_at": pr.get("merged_at"),
        "url": pr.get("html_url"),
        "title": pr.get("title"),
        "head_ref": (pr.get("head") or {}).get("ref"),
        "base_ref": (pr.get("base") or {}).get("ref"),
        "head_sha": (pr.get("head") or {}).get("sha"),
    }


def format_merged_status_message(pr_key: str, merge_state: dict[str, Any]) -> str:
    lines = [
        f"✅ **{pr_key}** — merged",
        f"<{merge_state.get('url')}>",
        f"- title: {merge_state.get('title')}",
        f"- branch: `{merge_state.get('head_ref')}` → `{merge_state.get('base_ref')}`",
        f"- merged_at: `{merge_state.get('merged_at')}`",
        "- issues: resolved by merge",
    ]
    return "\n".join(lines)[:1900]


def sync_discord_status_channel(
    repo_root: Path,
    statuses: list[dict[str, Any]],
    *,
    channel_id: str,
    operator_user_id: str,
    token: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upsert one status message per PR and open issue threads when needed."""
    ledger_path = repo_root / ".automation" / "pr-status-ledger.json"
    ledger = read_json(ledger_path, {"prs": {}})
    ledger.setdefault("prs", {})
    actions: list[dict[str, Any]] = []
    for status in statuses:
        pr_key = status["id"]
        entry = ledger["prs"].setdefault(pr_key, {})
        content = format_status_message(status)
        fingerprint = status_fingerprint(status)
        if dry_run:
            actions.append({"action": "would_upsert_message", "pr": pr_key, "state": status["state"]})
        elif entry.get("message_id"):
            edit_message(token, channel_id, entry["message_id"], content)
            actions.append({"action": "updated_message", "pr": pr_key})
        else:
            entry["message_id"] = post_message(token, channel_id, content)
            actions.append({"action": "created_message", "pr": pr_key})

        issues = status.get("issues", [])
        pingable = bool(issues) and any(issue.get("kind") not in NON_PING_ISSUE_KINDS for issue in issues)
        head_sha = status.get("head_sha")
        active = entry.get("active_alert") if isinstance(entry.get("active_alert"), dict) else None
        # Back-compat/migration: existing ledgers already have thread_id +
        # issue_fingerprint from alerts that fired before active_alert existed.
        # Treat those as an open pending action so the next monitor tick does not
        # re-ping the same PR just to initialize the new schema.
        if not active and pingable and entry.get("thread_id") and entry.get("issue_fingerprint") and status.get("issues"):
            active = {
                "fingerprint": entry.get("issue_fingerprint"),
                "current_fingerprint": entry.get("issue_fingerprint"),
                "state": "ACTION_PENDING",
                "opened_at": entry.get("updated_at") or utc_now(),
                "last_seen_at": entry.get("updated_at") or utc_now(),
                "head_sha": entry.get("head_sha") or head_sha,
                "migrated_from_legacy_alert": True,
            }
            entry["active_alert"] = active

        if not issues:
            entry["issue_fingerprint"] = fingerprint
            if active and active.get("state") != "RESOLVED":
                active["state"] = "RESOLVED"
                active["resolved_at"] = utc_now()
                actions.append({"action": "resolved_active_alert", "pr": pr_key})
        elif not pingable:
            entry["issue_fingerprint"] = fingerprint
            if active and active.get("migrated_from_legacy_alert"):
                active["state"] = "RESOLVED"
                active["resolved_at"] = utc_now()
                actions.append({"action": "resolved_legacy_non_ping_alert", "pr": pr_key})
            elif active and active.get("state") in ACTIVE_ALERT_STATES and active.get("head_sha") != head_sha:
                active["state"] = "FIX_PUSHED_WAITING_CI_OR_REVIEW"
                active["resolution_sha"] = head_sha
            actions.append({"action": "suppressed_non_ping_waiting", "pr": pr_key, "state": status.get("state")})
        elif active and active.get("state") in ACTIVE_ALERT_STATES:
            if active.get("head_sha") != head_sha:
                active["state"] = "FIX_PUSHED_WAITING_CI_OR_REVIEW"
                active["resolution_sha"] = head_sha
            active["current_fingerprint"] = fingerprint
            active["last_seen_at"] = utc_now()
            entry["issue_fingerprint"] = fingerprint
            actions.append({"action": "suppressed_alert_action_pending", "pr": pr_key, "state": active.get("state")})
        elif entry.get("issue_fingerprint") != fingerprint:
            if dry_run:
                actions.append({"action": "would_alert_thread", "pr": pr_key})
            else:
                if not entry.get("thread_id"):
                    entry["thread_id"] = ensure_thread(token, channel_id, entry["message_id"], f"{pr_key} issues")
                alert = (
                    f"<@{operator_user_id}> PR `{pr_key}` needs attention.\n"
                    "Reply here to start the interactive fix/rebase session.\n\n"
                    f"{format_status_message(status)}"
                )
                post_message(token, entry["thread_id"], alert)
                actions.append({"action": "alerted_thread", "pr": pr_key, "thread_id": entry.get("thread_id")})
            entry["active_alert"] = {
                "fingerprint": fingerprint,
                "current_fingerprint": fingerprint,
                "state": "ACTION_PENDING",
                "opened_at": utc_now(),
                "last_seen_at": utc_now(),
                "head_sha": head_sha,
            }
            entry["issue_fingerprint"] = fingerprint
        entry["last_state"] = status.get("state")
        entry["updated_at"] = utc_now()

    active_pr_keys = {status.get("id") for status in statuses}
    for pr_key, entry in list(ledger.get("prs", {}).items()):
        if pr_key in active_pr_keys or entry.get("last_state") == "MERGED":
            continue
        if not entry.get("message_id") and not entry.get("thread_id"):
            continue
        try:
            merge_state = fetch_pr_merge_state(pr_key)
        except Exception as exc:  # keep one bad historical PR from blocking current sync
            actions.append({"action": "merged_check_failed", "pr": pr_key, "error": str(exc)})
            continue
        if not merge_state or not merge_state.get("merged"):
            continue
        if dry_run:
            actions.append({"action": "would_mark_merged_and_close_thread", "pr": pr_key, "thread_id": entry.get("thread_id")})
        else:
            if entry.get("message_id"):
                edit_message(token, channel_id, entry["message_id"], format_merged_status_message(pr_key, merge_state))
                actions.append({"action": "updated_message_merged", "pr": pr_key})
            if entry.get("thread_id") and not entry.get("thread_archived_at"):
                archive_thread(token, entry["thread_id"])
                entry["thread_archived_at"] = utc_now()
                actions.append({"action": "archived_thread_after_merge", "pr": pr_key, "thread_id": entry.get("thread_id")})
        active = entry.get("active_alert") if isinstance(entry.get("active_alert"), dict) else None
        if active:
            active["state"] = "RESOLVED"
            active["resolved_at"] = active.get("resolved_at") or merge_state.get("merged_at") or utc_now()
            active["resolved_by"] = "merged"
        entry["last_state"] = "MERGED"
        entry["merged_at"] = merge_state.get("merged_at")
        entry["updated_at"] = utc_now()
    ledger["updated_at"] = utc_now()
    ledger["channel_id"] = channel_id
    if not dry_run:
        write_json(ledger_path, ledger)
    return {"actions": actions, "status_count": len(statuses), "ledger_path": str(ledger_path)}
