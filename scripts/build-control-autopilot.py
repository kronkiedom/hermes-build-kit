#!/usr/bin/env python3
"""Advance approved build-control plans through the deterministic lifecycle.

Target workflow:
Plan thread opens -> operator approves once -> build-control keeps moving until
it needs a concrete decision -> operator answers decision -> build resumes ->
readiness-gated draft PR opens when publish config allows -> PR-status owns review.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

from plan_automation_lib import decompose_plan, read_json, utc_now, write_json


def load_script(name: str, filename: str):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"failed to load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plans_index(repo_root: Path) -> dict[str, Any]:
    raw = read_json(repo_root / ".automation" / "plans-index.json", {"plans": {}})
    return raw if isinstance(raw, dict) else {"plans": {}}


def decompose_ready_plans(repo_root: Path, *, execute: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    plans = plans_index(repo_root).get("plans", {})
    if not isinstance(plans, dict):
        return actions
    for plan_id, entry in plans.items():
        if not isinstance(entry, dict):
            continue
        plan_dir = Path(str(entry.get("plan_dir") or repo_root / "plans" / str(plan_id)))
        meta = read_json(plan_dir / "meta.json", {})
        if not isinstance(meta, dict):
            continue
        if meta.get("state") != "DECOMPOSE":
            continue
        if execute:
            result = decompose_plan(repo_root, str(plan_id))
            actions.append({"action": "decomposed_plan", "plan_id": plan_id, "result": result})
        else:
            actions.append({"action": "would_decompose_plan", "plan_id": plan_id})
    return actions


def advance_build_control(repo_root: Path, *, execute: bool = False) -> dict[str, Any]:
    dispatch_worker = load_script("dispatch_pr_worker_script", "dispatch-pr-worker.py")
    reconciler = load_script("reconcile_plan_progress_script", "reconcile-plan-progress.py")
    auto_builder = load_script("auto_builder_runner_script", "auto-builder-runner.py")
    pre_pr_rebase = load_script("pre_pr_rebase_autocure_script", "pre-pr-rebase-autocure.py")
    auto_publish = load_script("auto_publish_runner_script", "auto-publish-runner.py")

    actions: list[dict[str, Any]] = []
    actions.extend(decompose_ready_plans(repo_root, execute=execute))
    actions.append({"action": "plan_progress", "result": reconciler.reconcile_plan_progress(repo_root, dry_run=not execute)})
    # Dispatch at most one eligible task per tick; dispatch worker itself holds if another execution is active.
    dispatch = dispatch_worker.dispatch_one(repo_root, execute=execute)
    actions.append({"action": "dispatch", "result": dispatch})
    actions.append({"action": "plan_progress_after_dispatch", "result": reconciler.reconcile_plan_progress(repo_root, dry_run=not execute)})
    builder = auto_builder.auto_run_builder(repo_root, execute=execute)
    actions.append({"action": "auto_builder", "result": builder})
    actions.append({"action": "plan_progress_after_builder", "result": reconciler.reconcile_plan_progress(repo_root, dry_run=not execute)})
    rebase = pre_pr_rebase.autocure_pre_pr_rebase(repo_root, execute=execute)
    actions.append({"action": "pre_pr_rebase_autocure", "result": rebase})
    actions.append({"action": "plan_progress_after_pre_pr_rebase", "result": reconciler.reconcile_plan_progress(repo_root, dry_run=not execute)})
    publisher = auto_publish.auto_publish(repo_root, execute=execute)
    actions.append({"action": "auto_publish", "result": publisher})
    actions.append({"action": "plan_progress_after_publish", "result": reconciler.reconcile_plan_progress(repo_root, dry_run=not execute)})

    return {
        "kind": "BUILD-CONTROL-AUTOPILOT",
        "checked_at": utc_now(),
        "execute": execute,
        "action_count": len(actions),
        "actions": actions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    payload = advance_build_control(Path.cwd(), execute=args.execute)
    write_json(Path.cwd() / ".automation" / "status" / "build-control-autopilot-last.json", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
