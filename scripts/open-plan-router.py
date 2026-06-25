#!/usr/bin/env python3
"""Classify open build-control plans into deterministic next-action packets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, write_json, utc_now


DISPATCH_WORKER = "dispatch-pr-worker.py"
BUILDER_WORKER = "run-builder-worker.py"


def load_plan(repo_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    plan_dir = Path(str(entry.get("plan_dir") or repo_root / "plans" / str(entry.get("plan_id"))))
    meta = read_json(plan_dir / "meta.json", {})
    if not isinstance(meta, dict):
        meta = {}
    return {**entry, **meta, "plan_dir": str(plan_dir), "plan_id": meta.get("plan_id") or entry.get("plan_id") or plan_dir.name}


def classify_next_action(plan: dict[str, Any]) -> dict[str, Any]:
    state = str(plan.get("state") or "UNKNOWN")
    awaiting_operator = bool(plan.get("awaiting_operator"))
    plan_id = plan.get("plan_id")
    base = {
        "plan_id": plan_id,
        "state": state,
        "awaiting_operator": awaiting_operator,
        "reason": plan.get("state_reason") or "",
    }
    if state == "CONTRACT":
        return {**base, "recommended_action": "shape_contract", "handler": "agent_required", "blocking": False}
    if state == "QUESTION" or awaiting_operator:
        return {**base, "recommended_action": "wait_for_operator_reply", "handler": "discord_thread_poller", "blocking": True}
    if state == "CONTRACT_REVIEW":
        return {**base, "recommended_action": "wait_for_contract_approval", "handler": "discord_thread_poller", "blocking": True}
    if state == "DECOMPOSE":
        return {**base, "recommended_action": "decompose_plan", "handler": "script", "blocking": False}
    if state == "EXECUTING":
        worker_exists = (Path(__file__).resolve().parent / DISPATCH_WORKER).exists()
        if worker_exists:
            return {**base, "recommended_action": "dispatch_pr_worker", "handler": "script", "blocking": False, "worker": DISPATCH_WORKER}
        return {**base, "recommended_action": "dispatch_pr_worker", "handler": "missing_worker", "blocking": True, "missing_worker": DISPATCH_WORKER}
    if state in {"VERIFYING", "VERIFY-LOOP"}:
        return {**base, "recommended_action": "verify_plan_outputs", "handler": "agent_required", "blocking": False}
    if state in {"DONE", "CANCELLED"}:
        return {**base, "recommended_action": "close_thread", "handler": "open_plan_status_monitor", "blocking": False}
    return {**base, "recommended_action": "inspect_plan", "handler": "operator_or_agent", "blocking": True}


def route_open_plans(repo_root: Path) -> dict[str, Any]:
    index = read_json(repo_root / ".automation" / "plans-index.json", {"plans": {}})
    plans = index.get("plans", {}) if isinstance(index, dict) else {}
    actions = []
    for entry in plans.values():
        if not isinstance(entry, dict):
            continue
        plan = load_plan(repo_root, entry)
        actions.append(classify_next_action(plan))
    script_dir = Path(__file__).resolve().parent
    missing_workers = [worker for worker in [DISPATCH_WORKER, BUILDER_WORKER] if not (script_dir / worker).exists()]
    return {
        "kind": "OPEN-PLAN-ROUTER",
        "checked_at": utc_now(),
        "action_count": len(actions),
        "actions": actions,
        "execution_capability": "intake_contract_decompose_dispatch_worktree_builder_command",
        "missing_workers": missing_workers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Compute routes without writing status file")
    args = parser.parse_args()
    repo_root = Path.cwd()
    payload = route_open_plans(repo_root)
    if not args.dry_run:
        write_json(repo_root / ".automation" / "status" / "open-plan-router-last.json", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
