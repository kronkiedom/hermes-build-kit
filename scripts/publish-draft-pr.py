#!/usr/bin/env python3
"""Publish a readiness-passed task branch as a draft GitHub PR.

This publisher is fail-closed: it refuses to push or create a draft PR unless the
recorded readiness job passes for the worktree's current HEAD SHA.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json, write_text
from pr_readiness_lib import load_readiness_job, readiness_blocks


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = False) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    payload = {"cmd": cmd, "cwd": str(cwd) if cwd else None, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(payload, indent=2))
    return payload


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def load_task(repo_root: Path, task_id: str | None) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        if task_id and meta.get("task_id") != task_id and meta_path.parent.name != task_id:
            continue
        dispatch = dict_or_empty(meta.get("dispatch"))
        build = dict_or_empty(meta.get("build"))
        if dispatch.get("worktree") and build.get("readiness_job_id"):
            candidates.append((meta_path.parent, meta))
    if task_id:
        return candidates[0] if candidates else None
    return candidates[0] if candidates else None


def current_sha(worktree: Path) -> str:
    return str(run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True)["stdout"]).strip()


def current_branch(worktree: Path) -> str:
    branch = str(run(["git", "branch", "--show-current"], cwd=worktree, check=True)["stdout"]).strip()
    return branch or "DETACHED"


def default_pr_body(task_id: str, meta: dict[str, Any], readiness_job_id: str) -> str:
    build = dict_or_empty(meta.get("build"))
    changed_files = "\n".join(f"- `{path}`" for path in build.get("changed_files", [])) or "- none recorded"
    return (
        f"## Summary\n"
        f"Draft PR for build-control task `{task_id}`.\n\n"
        f"## Build evidence\n"
        f"- Commit: `{build.get('commit_sha')}`\n"
        f"- Readiness job: `{readiness_job_id}`\n"
        f"- Evidence: `{build.get('evidence_path')}`\n\n"
        f"## Changed files\n"
        f"{changed_files}\n\n"
        f"## Test plan\n"
        f"- Readiness gate passed for the current head SHA before publishing.\n"
    )


def append_publish_ledger(repo_root: Path, event: dict[str, Any]) -> None:
    path = repo_root / ".automation" / "publish-ledger.json"
    ledger = read_json(path, {"events": []})
    ledger.setdefault("events", []).append(event)
    ledger["updated_at"] = utc_now()
    write_json(path, ledger)


def update_task_state(task_dir: Path, meta: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    updated = {**meta, **updates, "updated_at": utc_now()}
    write_json(task_dir / "meta.json", updated)
    return updated


def publish_draft_pr(
    repo_root: Path,
    *,
    task_id: str | None = None,
    execute: bool = False,
    repo: str | None = None,
    title: str | None = None,
    push_remote: str = "origin",
    head: str | None = None,
) -> dict[str, Any]:
    selected = load_task(repo_root, task_id)
    if not selected:
        return {"kind": "DRAFT-PR-PUBLISH", "decision": "IDLE", "reason": "no built task with readiness evidence is eligible for publishing"}
    task_dir, meta = selected
    selected_task_id = str(meta.get("task_id") or task_dir.name)
    dispatch = dict_or_empty(meta.get("dispatch"))
    build = dict_or_empty(meta.get("build"))
    packet = dict_or_empty(meta.get("pr_packet"))
    worktree = Path(str(dispatch.get("worktree") or "")).expanduser()
    if not worktree.exists():
        return {"kind": "DRAFT-PR-PUBLISH", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "dispatch worktree does not exist", "worktree": str(worktree)}

    readiness_job_id = str(build.get("readiness_job_id") or "")
    try:
        readiness_job = load_readiness_job(repo_root, readiness_job_id)
    except FileNotFoundError as exc:
        return {"kind": "DRAFT-PR-PUBLISH", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "missing readiness job", "error": str(exc)}

    sha = current_sha(worktree)
    readiness = readiness_blocks(readiness_job, current_sha=sha, explain=True)
    if not isinstance(readiness, dict):
        return {
            "kind": "DRAFT-PR-PUBLISH",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "reason": "readiness gate returned an invalid explanation",
            "readiness_job_id": readiness_job_id,
        }
    if readiness["blocked"]:
        return {
            "kind": "DRAFT-PR-PUBLISH",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "reason": "readiness gate blocks draft PR publishing",
            "readiness": readiness,
            "readiness_job_id": readiness_job_id,
        }

    branch = str(dispatch.get("branch") or current_branch(worktree))
    base_branch = str(dispatch.get("base_branch") or "main")
    head_ref = head or branch
    pr_title = title or f"draft: {packet.get('title') or selected_task_id}"
    body = default_pr_body(selected_task_id, meta, readiness_job_id)
    body_path = task_dir / "draft-pr-body.md"
    write_text(body_path, body)

    if not execute:
        return {
            "kind": "DRAFT-PR-PUBLISH",
            "decision": "WOULD_PUBLISH",
            "task_id": selected_task_id,
            "branch": branch,
            "base_branch": base_branch,
            "push_remote": push_remote,
            "head": head_ref,
            "current_sha": sha,
            "readiness_job_id": readiness_job_id,
            "body_path": str(body_path),
        }

    push = run(["git", "push", "-u", push_remote, branch], cwd=worktree, check=True)
    gh_cmd = ["gh", "pr", "create", "--draft", "--base", base_branch, "--head", head_ref, "--title", pr_title, "--body-file", str(body_path)]
    if repo:
        gh_cmd.extend(["--repo", repo])
    created = run(gh_cmd, cwd=worktree, check=True)
    pr_url = str(created["stdout"]).strip().splitlines()[-1] if str(created["stdout"]).strip() else ""
    updated = update_task_state(task_dir, meta, {
        "state": "PR_DRAFT",
        "state_reason": "draft PR published after readiness gate passed",
        "github": {**dict_or_empty(meta.get("github")), "draft_pr_url": pr_url, "published_at": utc_now()},
    })
    event = {"kind": "DRAFT-PR-PUBLISH", "decision": "PUBLISHED", "task_id": selected_task_id, "branch": branch, "head": head_ref, "push_remote": push_remote, "pr_url": pr_url, "timestamp": utc_now()}
    append_publish_ledger(repo_root, event)
    return {**event, "meta": {"state": updated.get("state"), "state_reason": updated.get("state_reason")}, "push": push}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--execute", action="store_true", help="Push and create the draft PR. Without this, dry-run only.")
    parser.add_argument("--repo", default=None, help="Optional owner/repo for gh pr create.")
    parser.add_argument("--title", default=None)
    parser.add_argument("--push-remote", default="origin", help="Git remote to push the branch to. Use a fork remote when upstream origin is read-only.")
    parser.add_argument("--head", default=None, help="Head ref for gh pr create, e.g. owner:branch for fork PRs. Defaults to the branch name.")
    args = parser.parse_args()
    payload = publish_draft_pr(Path.cwd(), task_id=args.task_id, execute=args.execute, repo=args.repo, title=args.title, push_remote=args.push_remote, head=args.head)
    write_json(Path.cwd() / ".automation" / "status" / "publish-draft-pr-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
