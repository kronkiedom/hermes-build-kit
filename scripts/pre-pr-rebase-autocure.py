#!/usr/bin/env python3
"""Autocure pre-PR build-control task branches that are behind their base.

This worker runs before draft PR publishing. It rebases a clean, built task
worktree onto its configured base branch and queues a fresh SHA-scoped readiness
job. It never pushes. Conflicts or dirty worktrees fail closed and require an
operator/manual fix.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json
from pr_readiness_lib import create_readiness_job

ELIGIBLE_STATES = {"VERIFYING", "PR_READY"}
TERMINAL_STATES = {"DONE", "CANCELLED", "SUPERSEDED", "PR_DRAFT"}


def run(cmd: list[str], *, cwd: Path, check: bool = False) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    payload: dict[str, Any] = {
        "cmd": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(payload, indent=2))
    return payload


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def current_sha(worktree: Path) -> str:
    return str(run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True)["stdout"]).strip()


def changed_file_set(worktree: Path, revspec: str) -> set[str]:
    result = run(["git", "diff", "--name-only", revspec], cwd=worktree, check=True)
    return {line.strip() for line in str(result["stdout"]).splitlines() if line.strip()}


def eligible_task(repo_root: Path) -> tuple[Path, dict[str, Any]] | None:
    for meta_path in sorted((repo_root / "tasks").glob("*/meta.json")):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        state = str(meta.get("state") or "")
        if state in TERMINAL_STATES or state not in ELIGIBLE_STATES:
            continue
        if meta.get("awaiting_operator"):
            continue
        if dict_or_empty(meta.get("github")).get("draft_pr_url") or dict_or_empty(meta.get("github")).get("pr_url"):
            continue
        dispatch = dict_or_empty(meta.get("dispatch"))
        build = dict_or_empty(meta.get("build"))
        if dispatch.get("worktree") and build.get("readiness_job_id"):
            return meta_path.parent, meta
    return None


def update_task(task_dir: Path, meta: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    updated = {**meta, **updates, "updated_at": utc_now()}
    write_json(task_dir / "meta.json", updated)
    return updated


def autocure_pre_pr_rebase(repo_root: Path, *, execute: bool = False, task_id: str | None = None) -> dict[str, Any]:
    selected: tuple[Path, dict[str, Any]] | None = None
    if task_id:
        task_dir = repo_root / "tasks" / task_id
        meta = read_json(task_dir / "meta.json", {})
        if isinstance(meta, dict) and meta:
            selected = (task_dir, meta)
    else:
        selected = eligible_task(repo_root)
    if not selected:
        return {"kind": "PRE-PR-REBASE-AUTOCURE", "decision": "IDLE", "reason": "no built pre-PR task branch is eligible for rebase autocure"}

    task_dir, meta = selected
    selected_task_id = str(meta.get("task_id") or task_dir.name)
    dispatch = dict_or_empty(meta.get("dispatch"))
    build = dict_or_empty(meta.get("build"))
    worktree = Path(str(dispatch.get("worktree") or "")).expanduser()
    base_branch = str(dispatch.get("base_branch") or "main")
    branch = str(dispatch.get("branch") or "")
    if not worktree.exists():
        return {"kind": "PRE-PR-REBASE-AUTOCURE", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "dispatch worktree does not exist", "worktree": str(worktree)}

    status = run(["git", "status", "--porcelain"], cwd=worktree, check=True)
    if str(status["stdout"]).strip():
        reason = "pre-PR rebase autocure requires a clean worktree"
        if execute:
            update_task(task_dir, meta, {"awaiting_operator": True, "state_reason": reason})
        return {"kind": "PRE-PR-REBASE-AUTOCURE", "decision": "BLOCKED", "task_id": selected_task_id, "reason": reason, "status": status["stdout"]}

    fetch = run(["git", "fetch", "origin", base_branch, "--prune"], cwd=worktree, check=False)
    if fetch["returncode"] != 0:
        return {"kind": "PRE-PR-REBASE-AUTOCURE", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "failed to fetch base branch", "fetch": fetch}

    before_sha = current_sha(worktree)
    behind_count = int(str(run(["git", "rev-list", "--count", f"HEAD..origin/{base_branch}"], cwd=worktree, check=True)["stdout"]).strip() or "0")
    ahead_count = int(str(run(["git", "rev-list", "--count", f"origin/{base_branch}..HEAD"], cwd=worktree, check=True)["stdout"]).strip() or "0")
    if behind_count == 0:
        return {
            "kind": "PRE-PR-REBASE-AUTOCURE",
            "decision": "CURRENT",
            "task_id": selected_task_id,
            "branch": branch,
            "base_branch": base_branch,
            "current_sha": before_sha,
            "ahead": ahead_count,
            "behind": 0,
        }

    branch_files = changed_file_set(worktree, f"origin/{base_branch}...HEAD")
    base_files = changed_file_set(worktree, f"HEAD...origin/{base_branch}")
    overlap = sorted(branch_files & base_files)
    if not execute:
        return {
            "kind": "PRE-PR-REBASE-AUTOCURE",
            "decision": "WOULD_REBASE",
            "task_id": selected_task_id,
            "branch": branch,
            "base_branch": base_branch,
            "current_sha": before_sha,
            "ahead": ahead_count,
            "behind": behind_count,
            "overlap_files": overlap,
        }

    rebase = run(["git", "rebase", f"origin/{base_branch}"], cwd=worktree, check=False)
    if rebase["returncode"] != 0:
        reason = "pre-PR rebase autocure hit conflicts; manual fix required"
        update_task(task_dir, meta, {"awaiting_operator": True, "state": "VERIFYING", "state_reason": reason})
        event = {
            "kind": "PRE-PR-REBASE-AUTOCURE",
            "decision": "CONFLICT",
            "task_id": selected_task_id,
            "branch": branch,
            "base_branch": base_branch,
            "before_sha": before_sha,
            "ahead": ahead_count,
            "behind": behind_count,
            "overlap_files": overlap,
            "rebase": rebase,
            "reason": reason,
        }
        write_json(repo_root / ".automation" / "status" / "pre-pr-rebase-autocure-last.json", {**event, "checked_at": utc_now()})
        return event

    after_sha = current_sha(worktree)
    # Root cause: base can advance after a build, making the old readiness job certify
    # the wrong SHA; rebase must queue a fresh readiness job before publishing.
    new_job = create_readiness_job(repo_root, task_id=selected_task_id, branch=branch, sha=after_sha)
    build = {**build, "commit_sha": after_sha, "readiness_job_id": new_job["job_id"], "rebased_from_sha": before_sha, "rebased_at": utc_now()}
    update_task(task_dir, meta, {
        "state": "VERIFYING",
        "awaiting_operator": False,
        "state_reason": "pre-PR branch rebased onto current base; readiness audit queued for rebased SHA",
        "build": build,
    })
    event = {
        "kind": "PRE-PR-REBASE-AUTOCURE",
        "decision": "REBASED_READINESS_QUEUED",
        "task_id": selected_task_id,
        "branch": branch,
        "base_branch": base_branch,
        "before_sha": before_sha,
        "after_sha": after_sha,
        "ahead_before": ahead_count,
        "behind_before": behind_count,
        "overlap_files": overlap,
        "readiness_job_id": new_job["job_id"],
        "rebase": rebase,
    }
    write_json(repo_root / ".automation" / "status" / "pre-pr-rebase-autocure-last.json", {**event, "checked_at": utc_now()})
    return event


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--task-id", default=None)
    args = parser.parse_args()
    payload = autocure_pre_pr_rebase(Path.cwd(), execute=args.execute, task_id=args.task_id)
    write_json(Path.cwd() / ".automation" / "status" / "pre-pr-rebase-autocure-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
