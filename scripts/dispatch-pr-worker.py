#!/usr/bin/env python3
"""Dispatch one decomposed plan task into an isolated git worktree.

This worker is intentionally conservative: it prepares the workspace and durable
execution packet, but it does not invent code changes. Draft PR creation is
optional and only happens after an execution pass has produced commits.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json, write_text
from pr_readiness_lib import create_readiness_job

ACTIVE_EXECUTION_STATES = {"EXECUTE", "VERIFYING", "VERIFY-LOOP", "PR_READY", "DISPATCHED"}
READY_TASK_STATES = {"SHAPE", "READY"}
UNRESOLVED_REPOS = {"", "operator-defined", "todo", "tbd", "unknown"}


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = False) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    payload = {"cmd": cmd, "cwd": str(cwd) if cwd else None, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(payload, indent=2))
    return payload


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def task_dir_for_meta(meta_path: Path) -> Path:
    return meta_path.parent


def load_plan(repo_root: Path, plan_id: str | None) -> dict[str, Any]:
    if not plan_id:
        return {}
    return read_json(repo_root / "plans" / plan_id / "meta.json", {})


def repo_from_task(repo_root: Path, task_meta: dict[str, Any]) -> str:
    if task_meta.get("repo"):
        return str(task_meta["repo"])
    plan = load_plan(repo_root, task_meta.get("source_plan_id"))
    return str(plan.get("repo") or "")


def base_branch_from_task(repo_root: Path, task_meta: dict[str, Any]) -> str:
    if task_meta.get("base_branch"):
        return str(task_meta["base_branch"])
    plan = load_plan(repo_root, task_meta.get("source_plan_id"))
    return str(plan.get("base_branch") or "main")


def resolve_local_repo(repo_root: Path, repo_value: str) -> Path | None:
    normalized = repo_value.strip()
    if normalized.lower() in UNRESOLVED_REPOS:
        return None
    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    if (candidate / ".git").exists() or (candidate / ".git").is_file():
        return candidate
    return None


def active_execution_tasks(repo_root: Path) -> list[dict[str, Any]]:
    active = []
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if meta.get("state") in ACTIVE_EXECUTION_STATES and not meta.get("awaiting_operator") and not meta.get("dispatch", {}).get("pr_created", False):
            active.append({"task_id": meta.get("task_id", meta_path.parent.name), "state": meta.get("state"), "task_dir": str(meta_path.parent)})
    return active


def eligible_tasks(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if meta.get("awaiting_operator"):
            continue
        if meta.get("state") in READY_TASK_STATES and (meta.get("phase_status") or {}).get("SHAPE") in {"READY", "DONE", None}:
            out.append((meta_path.parent, meta))
    return out


def select_task(repo_root: Path, task_id: str | None = None) -> tuple[Path, dict[str, Any]] | None:
    candidates = eligible_tasks(repo_root)
    if task_id:
        for task_dir, meta in candidates:
            if meta.get("task_id") == task_id or task_dir.name == task_id:
                return task_dir, meta
        return None
    return candidates[0] if candidates else None


def render_builder_prompt(task_dir: Path, task_meta: dict[str, Any], *, repo_path: Path, branch: str, base_branch: str) -> str:
    task_md = (task_dir / "task.md").read_text(encoding="utf-8") if (task_dir / "task.md").exists() else ""
    return f"""# Builder dispatch packet

Task: `{task_meta.get('task_id')}`
Branch: `{branch}`
Base branch: `{base_branch}`
Worktree: `{repo_path}`

## Rules
- Implement this packet only.
- Do not merge.
- Create a ready-for-review PR only after code changes and local verification exist.
- Write `summary.md`, `evidence.md`, and verification output before claiming ready.
- Run the SHA-scoped PR readiness gate before ready-for-review.

## Task

{task_md}
"""


def prepare_worktree(repo_root: Path, source_repo: Path, *, task_id: str, branch: str, base_branch: str, worktree_root: Path) -> tuple[Path, list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / task_id
    actions.append(run(["git", "fetch", "origin", base_branch], cwd=source_repo, check=False))
    if not worktree_path.exists():
        # Worktrees are isolated so parallel builders never edit the same checkout.
        add = run(["git", "worktree", "add", "-B", branch, str(worktree_path), f"origin/{base_branch}"], cwd=source_repo, check=False)
        actions.append(add)
        if add["returncode"] != 0:
            raise RuntimeError(json.dumps(add, indent=2))
    else:
        actions.append(run(["git", "checkout", branch], cwd=worktree_path, check=False))
    return worktree_path, actions


def update_task_state(task_dir: Path, meta: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    meta = {**meta, **updates, "updated_at": utc_now()}
    write_json(task_dir / "meta.json", meta)
    return meta


def append_dispatch_ledger(repo_root: Path, event: dict[str, Any]) -> None:
    path = repo_root / ".automation" / "dispatch-ledger.json"
    ledger = read_json(path, {"events": []})
    ledger.setdefault("events", []).append(event)
    ledger["updated_at"] = utc_now()
    write_json(path, ledger)


def dispatch_one(repo_root: Path, *, task_id: str | None = None, execute: bool = False, create_draft_pr: bool = False, worktree_root: str = ".automation/pr-worktrees") -> dict[str, Any]:
    if create_draft_pr:
        return {
            "kind": "PR-DISPATCH",
            "decision": "BLOCKED",
            "reason": "PR creation is intentionally gated until a builder has produced commits and verification evidence",
            "required": "Run dispatch without --create-draft-pr, complete builder work, then use a separate PR publishing step after readiness evidence exists.",
        }
    active = active_execution_tasks(repo_root)
    if active and not task_id:
        return {"kind": "PR-DISPATCH", "decision": "HOLD", "reason": "active execution task already present", "active_tasks": active}

    selected = select_task(repo_root, task_id)
    if not selected:
        return {"kind": "PR-DISPATCH", "decision": "IDLE", "reason": "no eligible SHAPE/READY task packets"}
    task_dir, meta = selected
    selected_task_id = str(meta.get("task_id") or task_dir.name)
    packet = meta.get("pr_packet") or {}
    branch = str(packet.get("branch") or f"feat/{selected_task_id[:36]}")
    base_branch = base_branch_from_task(repo_root, meta)
    repo_value = repo_from_task(repo_root, meta)
    local_repo = resolve_local_repo(repo_root, repo_value)

    if not local_repo:
        blocker = {
            "kind": "PR-DISPATCH",
            "decision": "BLOCKED",
            "reason": "target repo is not a concrete local git checkout",
            "task_id": selected_task_id,
            "repo": repo_value,
            "required": "Set plan/task repo to an absolute/relative local git checkout before dispatch.",
        }
        if execute:
            write_text(task_dir / "summary.md", f"# Dispatch blocked\n\n- Reason: {blocker['reason']}\n- Repo: `{repo_value}`\n")
            write_text(task_dir / "evidence.md", json.dumps(blocker, indent=2) + "\n")
            update_task_state(task_dir, meta, {"state": "ESCALATED", "awaiting_operator": True, "state_reason": blocker["reason"], "dispatch_blocker": blocker})
            append_dispatch_ledger(repo_root, {**blocker, "timestamp": utc_now()})
        return blocker

    worktree_path = Path(worktree_root)
    if not worktree_path.is_absolute():
        worktree_path = repo_root / worktree_path

    if not execute:
        return {
            "kind": "PR-DISPATCH",
            "decision": "WOULD_DISPATCH",
            "task_id": selected_task_id,
            "branch": branch,
            "base_branch": base_branch,
            "repo_path": str(local_repo),
            "worktree": str(worktree_path / selected_task_id),
        }

    worktree, actions = prepare_worktree(repo_root, local_repo, task_id=selected_task_id, branch=branch, base_branch=base_branch, worktree_root=worktree_path)
    prompt = render_builder_prompt(task_dir, meta, repo_path=worktree, branch=branch, base_branch=base_branch)
    write_text(task_dir / "builder-prompt.md", prompt)
    write_text(task_dir / "summary.md", f"# Dispatch summary\n\n- Task: `{selected_task_id}`\n- Worktree: `{worktree}`\n- Branch: `{branch}`\n- Draft PR: not created yet; builder must make changes first.\n")
    write_text(task_dir / "evidence.md", json.dumps({"actions": actions, "worktree": str(worktree), "branch": branch}, indent=2) + "\n")
    sha_result = run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True)
    sha = str(sha_result["stdout"]).strip()
    readiness = create_readiness_job(repo_root, task_id=selected_task_id, branch=branch, sha=sha)
    updated = update_task_state(task_dir, meta, {
        "state": "EXECUTE",
        "phase_status": {**(meta.get("phase_status") or {}), "EXECUTE": "DISPATCHED"},
        "awaiting_operator": False,
        "state_reason": "dispatched to isolated worktree; awaiting builder changes",
        "dispatch": {
            "worktree": str(worktree),
            "branch": branch,
            "base_branch": base_branch,
            "repo_path": str(local_repo),
            "readiness_job_id": readiness["job_id"],
            "pr_created": False,
        },
    })
    event = {"kind": "PR-DISPATCH", "decision": "DISPATCHED", "task_id": selected_task_id, "worktree": str(worktree), "branch": branch, "readiness_job_id": readiness["job_id"], "timestamp": utc_now()}
    append_dispatch_ledger(repo_root, event)
    return {**event, "meta": {"state": updated.get("state"), "state_reason": updated.get("state_reason")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--execute", action="store_true", help="Mutate task state and prepare the worktree. Without this, dry-runs only.")
    parser.add_argument("--create-draft-pr", action="store_true", help="Deprecated compatibility flag; dispatch does not create empty PRs.")
    parser.add_argument("--worktree-root", default=".automation/pr-worktrees")
    args = parser.parse_args()
    payload = dispatch_one(Path.cwd(), task_id=args.task_id, execute=args.execute, create_draft_pr=args.create_draft_pr, worktree_root=args.worktree_root)
    write_json(Path.cwd() / ".automation" / "status" / "dispatch-pr-worker-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
