#!/usr/bin/env python3
"""Open-plan status classification and Discord thread lifecycle helpers."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json

TERMINAL_PLAN_STATES = {"DONE", "CANCELLED"}
OPERATOR_WAIT_STATES = {"QUESTION", "CONTRACT-CHECKPOINT"}
ACTIVE_PLAN_ALERT_STATES = {"OPERATOR_PENDING", "ACTION_PENDING", "IN_PROGRESS"}


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
            "User-Agent": "Hermes open plan status monitor",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {body}") from exc


def post_message(token: str, channel_id: str, content: str) -> str:
    message = discord_request(token, f"/channels/{channel_id}/messages", method="POST", payload={"content": content[:1900]})
    return str(message["id"])


def update_message(token: str, channel_id: str, message_id: str, content: str) -> str:
    message = discord_request(token, f"/channels/{channel_id}/messages/{message_id}", method="PATCH", payload={"content": content[:1900]})
    return str(message["id"])


def add_thread_member(token: str, thread_id: str, user_id: str) -> None:
    discord_request(token, f"/channels/{thread_id}/thread-members/{user_id}", method="PUT", payload={})


def archive_thread(token: str, thread_id: str) -> None:
    # Discord closes a thread by archiving it. Locking keeps completed/cancelled
    # plan threads from being revived as stale action surfaces.
    discord_request(token, f"/channels/{thread_id}", method="PATCH", payload={"archived": True, "locked": True})


def load_plan_meta(repo_root: Path, plan_entry: dict[str, Any]) -> dict[str, Any]:
    plan_dir = Path(str(plan_entry.get("plan_dir") or repo_root / "plans" / str(plan_entry.get("plan_id"))))
    meta = read_json(plan_dir / "meta.json", {})
    if not isinstance(meta, dict):
        meta = {}
    merged = {**plan_entry, **meta}
    merged["plan_dir"] = str(plan_dir)
    merged.setdefault("plan_id", plan_entry.get("plan_id") or plan_dir.name)
    return merged


def classify_plan(plan: dict[str, Any]) -> dict[str, Any]:
    state = str(plan.get("state") or "UNKNOWN")
    progress_raw = plan.get("child_progress")
    progress: dict[str, Any] = progress_raw if isinstance(progress_raw, dict) else {}
    child_waiting = int(progress.get("waiting_count") or 0) > 0
    awaiting_operator = bool(plan.get("awaiting_operator")) or state in OPERATOR_WAIT_STATES or child_waiting
    terminal = state in TERMINAL_PLAN_STATES
    thread_id = str((plan.get("discord") or {}).get("thread_id") or plan.get("thread_id") or "")
    plan_card_message_id = str((plan.get("discord") or {}).get("plan_card_message_id") or plan.get("plan_card_message_id") or "")
    status = "TERMINAL" if terminal else ("OPERATOR_WAITING" if awaiting_operator else "IN_PROGRESS")
    return {
        "plan_id": plan.get("plan_id"),
        "title": plan.get("title"),
        "state": state,
        "status": status,
        "awaiting_operator": awaiting_operator,
        "terminal": terminal,
        "thread_id": thread_id,
        "plan_card_message_id": plan_card_message_id,
        "reason": plan.get("state_reason") or "",
        "updated_at": plan.get("updated_at"),
        "repo": plan.get("repo"),
        "base_branch": plan.get("base_branch"),
        "child_progress": plan.get("child_progress") if isinstance(plan.get("child_progress"), dict) else {},
    }


def plan_fingerprint(status: dict[str, Any]) -> str:
    return "|".join([
        str(status.get("plan_id") or ""),
        str(status.get("state") or ""),
        str(status.get("status") or ""),
        str(status.get("reason") or ""),
        str(status.get("updated_at") or ""),
    ])


def format_operator_alert(status: dict[str, Any], operator_user_id: str) -> str:
    return (
        f"<@{operator_user_id}> Plan `{status.get('plan_id')}` needs operator input.\n"
        f"- title: {status.get('title')}\n"
        f"- state: `{status.get('state')}`\n"
        f"- reason: {status.get('reason') or 'awaiting operator decision'}\n\n"
        "Reply in this thread with the requested decision/input."
    )[:1900]


def workflow_bucket(row: dict[str, Any]) -> str:
    state = str(row.get("state") or "UNKNOWN")
    owner = str(row.get("owner") or "")
    if state == "DONE" or owner == "completed":
        return "completed"
    if state in {"CANCELLED", "parked"} or owner == "closed/superseded":
        return "closed"
    if owner == "operator":
        return "waiting"
    if owner == "pr-status":
        return "pr-status"
    if state in {"SHAPE", "READY", "READY_FOR_BUILDER", "DISPATCHED", "QUEUED", "PLANNED"}:
        return "planned"
    return "in-progress"


def workflow_icon(row: dict[str, Any]) -> str:
    bucket = workflow_bucket(row)
    if bucket == "completed":
        return "✅"
    if bucket == "waiting":
        return "❓"
    if bucket == "pr-status":
        return "🔁"
    if bucket == "planned":
        return "🧭"
    if bucket == "closed":
        return "🧹"
    return "▶️"


def workflow_progress_counts(rows: list[Any]) -> dict[str, int]:
    counts = {"completed": 0, "in-progress": 0, "waiting": 0, "planned": 0, "pr-status": 0, "closed": 0}
    for row in rows:
        if isinstance(row, dict):
            bucket = workflow_bucket(row)
            counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def workflow_row_sort_key(row: Any) -> tuple[int, str]:
    if not isinstance(row, dict):
        return (99, "")
    order = {"in-progress": 0, "waiting": 1, "planned": 2, "pr-status": 3, "completed": 4, "closed": 5}
    return (order.get(workflow_bucket(row), 99), str(row.get("label") or row.get("task_id") or ""))


def format_workflow_map(status: dict[str, Any]) -> str:
    progress_raw = status.get("child_progress")
    progress: dict[str, Any] = progress_raw if isinstance(progress_raw, dict) else {}
    rows_raw = progress.get("workflow_map")
    rows = rows_raw if isinstance(rows_raw, list) else []
    if not rows:
        state = str(status.get("state") or "UNKNOWN")
        if state in {"CONTRACT", "CONTRACT_REVIEW", "DECOMPOSE", "QUESTION"}:
            return (
                "\n**Contained workflow / PR progress:**\n"
                "Progress: no PR/build packets decomposed yet. Remaining planned PRs will appear here after contract approval/decomposition."
            )
        return ""
    counts = workflow_progress_counts(rows)
    total_visible = sum(counts.values())
    lines = [
        "",
        "**Contained workflow / PR progress:**",
        (
            f"Progress: ✅ {counts['completed']} complete · ▶️ {counts['in-progress']} in progress · "
            f"❓ {counts['waiting']} waiting · 🧭 {counts['planned']} planned/left to build · "
            f"🔁 {counts['pr-status']} with PR-status ({total_visible} visible)"
        ),
    ]
    sorted_rows = sorted(rows, key=workflow_row_sort_key)
    for row in sorted_rows[:10]:
        if not isinstance(row, dict):
            continue
        label = row.get("label") or row.get("task_id") or "task"
        title = str(row.get("title") or "").strip()
        if len(title) > 60:
            title = title[:57] + "..."
        state = row.get("state") or "UNKNOWN"
        owner = row.get("owner") or "unknown"
        bucket = workflow_bucket(row)
        branch = f" — `{row.get('branch')}`" if row.get("branch") else ""
        pr = f" — {row.get('pr_url')}" if row.get("pr_url") else ""
        lines.append(f"{workflow_icon(row)} [{bucket}] {label} — {title} — `{state}` — owner: `{owner}`{branch}{pr}")
    hidden = progress.get("hidden_cancelled_count") or 0
    if hidden:
        lines.append(f"🧹 {hidden} superseded/cancelled generated packet(s) hidden from this card.")
    if len(rows) > 10:
        lines.append(f"… {len(rows) - 10} additional visible item(s) omitted for Discord length.")
    return "\n".join(lines)


def format_plan_card(status: dict[str, Any], operator_user_id: str) -> str:
    title = status.get('title') or status.get('plan_id')
    state = str(status.get('state') or 'UNKNOWN')
    reason = status.get('reason') or 'No current blocker recorded.'
    needs = reason
    if status.get('awaiting_operator'):
        needs = "Operator response in this thread."
    elif state == 'CONTRACT':
        needs = "Contract shaping/re-shaping by build-control."
    elif state == 'CONTRACT_REVIEW':
        needs = "Operator approval (`approve`) or requested changes in this thread."
    elif state == 'DECOMPOSE':
        needs = "Run decomposition into PR/build packets."
    elif state == 'EXECUTING':
        needs = "Continue child build/decision packets until all are terminal."
    workflow_map = format_workflow_map(status)
    question = ""
    if status.get('awaiting_operator') or state in {'QUESTION', 'CONTRACT_REVIEW'}:
        question = f"\n**Decision / reply needed:** <@{operator_user_id}> {needs}"
    return (
        f"**Persistent plan card**\n"
        f"**Plan:** {title}\n"
        f"**Plan ID:** `{status.get('plan_id')}`\n"
        f"**State:** `{state}` (`{status.get('status')}`)\n"
        f"**Where it is:** {reason}\n"
        f"**Needs to complete:** {needs}\n"
        f"**Repo/base:** `{status.get('repo') or 'unknown'}` / `{status.get('base_branch') or 'main'}`"
        f"{workflow_map}"
        f"{question}\n\n"
        "Reply in this thread to progress the workflow. For plan approval, type `approve` with no slash. `/approve` is reserved for Hermes command approvals and may be blocked in build-control. Other replies are recorded and move waiting plans back to contract shaping."
    )[:1900]


def ensure_plan_card(token: str, status: dict[str, Any], entry: dict[str, Any], operator_user_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    thread_id = str(status.get('thread_id') or entry.get('thread_id') or '')
    if not thread_id:
        return {"action": "plan_card_no_thread", "plan_id": status.get('plan_id')}
    content = format_plan_card(status, operator_user_id)
    card_id = str(entry.get('plan_card_message_id') or status.get('plan_card_message_id') or '')
    if dry_run:
        return {"action": "would_update_plan_card" if card_id else "would_create_plan_card", "plan_id": status.get('plan_id'), "thread_id": thread_id, "message_id": card_id or None}
    if card_id:
        try:
            update_message(token, thread_id, card_id, content)
            return {"action": "updated_plan_card", "plan_id": status.get('plan_id'), "thread_id": thread_id, "message_id": card_id}
        except Exception:
            # If the recorded message vanished, recreate the card below.
            card_id = ""
    message_id = post_message(token, thread_id, content)
    entry['plan_card_message_id'] = message_id
    entry['thread_id'] = thread_id
    return {"action": "created_plan_card", "plan_id": status.get('plan_id'), "thread_id": thread_id, "message_id": message_id}


def format_terminal_message(status: dict[str, Any]) -> str:
    label = "completed" if status.get("state") == "DONE" else "cancelled"
    return (
        f"✅ Plan `{status.get('plan_id')}` is {label}.\n"
        f"- title: {status.get('title')}\n"
        f"- final state: `{status.get('state')}`\n"
        f"- reason: {status.get('reason') or 'terminal state reached'}\n\n"
        "Closing this plan thread so stale plan alerts do not keep resurfacing."
    )[:1900]


def sync_open_plan_threads(
    repo_root: Path,
    *,
    operator_user_id: str,
    token: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    index_path = repo_root / ".automation" / "plans-index.json"
    ledger_path = repo_root / ".automation" / "plan-status-ledger.json"
    plans_index = read_json(index_path, {"plans": {}})
    plans = plans_index.get("plans", {}) if isinstance(plans_index, dict) else {}
    ledger = read_json(ledger_path, {"plans": {}})
    ledger.setdefault("plans", {})
    actions: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    for raw_entry in plans.values():
        if not isinstance(raw_entry, dict):
            continue
        meta = load_plan_meta(repo_root, raw_entry)
        status = classify_plan(meta)
        statuses.append(status)
        plan_id = str(status.get("plan_id") or "")
        if not plan_id:
            continue
        entry = ledger["plans"].setdefault(plan_id, {})
        thread_id = status.get("thread_id") or entry.get("thread_id")
        if thread_id:
            entry["thread_id"] = thread_id
        fingerprint = plan_fingerprint(status)
        if thread_id and not status.get("terminal"):
            if not dry_run:
                try:
                    add_thread_member(token, str(thread_id), operator_user_id)
                except Exception:
                    # Visibility is best-effort; the card/update below still links the thread.
                    pass
            card_action = ensure_plan_card(token, status, entry, operator_user_id, dry_run=dry_run)
            actions.append(card_action)
        active = entry.get("active_alert") if isinstance(entry.get("active_alert"), dict) else None

        if status.get("terminal"):
            if entry.get("last_state") == status.get("state") and entry.get("thread_archived_at"):
                actions.append({"action": "already_closed", "plan_id": plan_id})
            elif dry_run:
                actions.append({"action": "would_close_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
            else:
                if thread_id:
                    try:
                        post_message(token, thread_id, format_terminal_message(status))
                        archive_thread(token, thread_id)
                        entry["thread_archived_at"] = utc_now()
                        actions.append({"action": "closed_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
                    except Exception as exc:
                        entry["thread_archive_error"] = str(exc)[:500]
                        entry["thread_archived_at"] = entry.get("thread_archived_at") or utc_now()
                        actions.append({"action": "close_plan_thread_failed", "plan_id": plan_id, "thread_id": thread_id, "error": str(exc)[:200]})
                else:
                    actions.append({"action": "terminal_no_thread", "plan_id": plan_id})
            if active:
                active["state"] = "RESOLVED"
                active["resolved_at"] = active.get("resolved_at") or utc_now()
                active["resolved_by"] = str(status.get("state"))
            entry["last_state"] = status.get("state")
            entry["last_fingerprint"] = fingerprint
            entry["updated_at"] = utc_now()
            continue

        if status.get("awaiting_operator"):
            if active and active.get("state") in ACTIVE_PLAN_ALERT_STATES:
                active["last_seen_at"] = utc_now()
                active["current_fingerprint"] = fingerprint
                actions.append({"action": "suppressed_plan_alert_pending", "plan_id": plan_id, "state": active.get("state")})
            elif entry.get("last_fingerprint") != fingerprint:
                if dry_run:
                    actions.append({"action": "would_alert_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
                else:
                    if thread_id:
                        post_message(token, thread_id, format_operator_alert(status, operator_user_id))
                        actions.append({"action": "alerted_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
                    else:
                        actions.append({"action": "operator_wait_no_thread", "plan_id": plan_id})
                entry["active_alert"] = {
                    "state": "OPERATOR_PENDING",
                    "fingerprint": fingerprint,
                    "current_fingerprint": fingerprint,
                    "opened_at": utc_now(),
                    "last_seen_at": utc_now(),
                }
            entry["last_fingerprint"] = fingerprint
        else:
            if active and active.get("state") in ACTIVE_PLAN_ALERT_STATES:
                active["state"] = "RESOLVED"
                active["resolved_at"] = utc_now()
                active["resolved_by"] = "plan_left_operator_wait"
                actions.append({"action": "resolved_plan_alert", "plan_id": plan_id})
            actions.append({"action": "plan_in_progress_no_ping", "plan_id": plan_id, "state": status.get("state")})
            entry["last_fingerprint"] = fingerprint

        entry["last_state"] = status.get("state")
        entry["updated_at"] = utc_now()

    ledger["updated_at"] = utc_now()
    if not dry_run:
        write_json(ledger_path, ledger)
    return {"kind": "OPEN-PLAN-STATUS", "status_count": len(statuses), "statuses": statuses, "actions": actions, "ledger_path": str(ledger_path), "dry_run": dry_run}
