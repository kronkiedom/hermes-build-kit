#!/usr/bin/env python3
"""Automatically run the configured builder for ready build-control tasks.

This is the deterministic bridge from "approved/dispatched" to actual builder
execution. It is fail-closed: without a configured builder command it records a
BLOCKED status and leaves tasks ready rather than pretending to build.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json


def load_run_builder_worker():
    script_path = Path(__file__).resolve().parent / "run-builder-worker.py"
    spec = importlib.util.spec_from_file_location("run_builder_worker_script", script_path)
    if not spec or not spec.loader:
        raise RuntimeError("failed to load run-builder-worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

READY_STATES = {"READY_FOR_BUILDER", "DISPATCHED"}
ACTIVE_STATES = {"EXECUTE", "BUILDING", "VERIFYING", "VERIFY-LOOP"}


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def configured_builder_command(repo_root: Path) -> str:
    if os.environ.get("HERMES_BUILDER_COMMAND"):
        return str(os.environ["HERMES_BUILDER_COMMAND"])
    cfg = read_json(repo_root / ".automation" / "builder-config.json", {})
    if isinstance(cfg, dict) and cfg.get("enabled") and cfg.get("builder_command"):
        return str(cfg["builder_command"])
    return ""


def ready_task(repo_root: Path) -> tuple[Path, dict[str, Any]] | None:
    run_builder_worker = load_run_builder_worker()
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        if run_builder_worker.is_builder_eligible(meta):
            return meta_path.parent, meta
    return None


def active_build_tasks(repo_root: Path) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        if str(meta.get("state") or "") in ACTIVE_STATES and not meta.get("awaiting_operator"):
            # READY_FOR_BUILDER is represented as DISPATCHED + phase flag, not EXECUTE.
            phase = dict_or_empty(meta.get("phase_status"))
            if phase.get("EXECUTE") in {"READY_FOR_BUILDER", "DISPATCHED"}:
                continue
            active.append({"task_id": meta.get("task_id") or meta_path.parent.name, "state": meta.get("state")})
    return active


def mark_ready_for_builder(task_dir: Path, meta: dict[str, Any], reason: str) -> None:
    phase = dict_or_empty(meta.get("phase_status"))
    phase["EXECUTE"] = "READY_FOR_BUILDER"
    meta["phase_status"] = phase
    meta["state"] = "READY_FOR_BUILDER"
    meta["awaiting_operator"] = False
    meta["state_reason"] = reason
    meta["updated_at"] = utc_now()
    write_json(task_dir / "meta.json", meta)


def _acquire_builder_lock(repo_root: Path):
    """Single-builder mutex with stale-pid reclaim. A build held phase EXECUTE=DISPATCHED
    for its whole (long) run, and active_build_tasks() skips DISPATCHED — so a later tick
    saw 'no active build' and started a SECOND concurrent builder for the same task. This
    lock makes builds strictly one-at-a-time across ticks; a dead owner (crash/reboot) is
    reclaimed so it never wedges (the failure mode the autopilot lock hit after an outage)."""
    lock_dir = repo_root / ".automation" / "locks" / "auto-builder-runner.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.mkdir()
    except FileExistsError:
        owner = read_json(lock_dir / "owner.json", {}) or {}
        pid = owner.get("pid")
        alive = isinstance(pid, int)
        if alive:
            try:
                os.kill(pid, 0)
            except OSError:
                alive = False
        if alive:
            return None
        shutil.rmtree(lock_dir, ignore_errors=True)
        try:
            lock_dir.mkdir()
        except FileExistsError:
            return None
    write_json(lock_dir / "owner.json", {"kind": "AUTO-BUILDER-LOCK", "pid": os.getpid(), "acquired_at": utc_now()})
    return lock_dir


def _release_builder_lock(lock_dir) -> None:
    if lock_dir is not None:
        shutil.rmtree(lock_dir, ignore_errors=True)


def auto_run_builder(repo_root: Path, *, execute: bool = False, timeout_seconds: int = 1800) -> dict[str, Any]:
    command = configured_builder_command(repo_root)
    selected = ready_task(repo_root)
    active = active_build_tasks(repo_root)
    if active:
        return {"kind": "AUTO-BUILDER", "decision": "HOLD", "reason": "active builder task already present", "active_tasks": active}
    if not selected:
        return {"kind": "AUTO-BUILDER", "decision": "IDLE", "reason": "no task is ready for builder execution"}
    task_dir, meta = selected
    task_id = str(meta.get("task_id") or task_dir.name)
    if not command:
        reason = "builder command is not configured; set .automation/builder-config.json enabled=true with builder_command or HERMES_BUILDER_COMMAND"
        if execute:
            mark_ready_for_builder(task_dir, meta, reason)
        return {"kind": "AUTO-BUILDER", "decision": "BLOCKED", "task_id": task_id, "reason": reason}
    if not execute:
        return {"kind": "AUTO-BUILDER", "decision": "WOULD_RUN_BUILDER", "task_id": task_id, "builder_command_configured": True}
    lock_dir = _acquire_builder_lock(repo_root)
    if lock_dir is None:
        return {"kind": "AUTO-BUILDER", "decision": "HOLD", "reason": "another builder run holds the single-builder lock", "task_id": task_id}
    try:
        run_builder_worker = load_run_builder_worker()
        return run_builder_worker.run_builder(repo_root, task_id=task_id, builder_command=command, timeout_seconds=timeout_seconds)
    finally:
        _release_builder_lock(lock_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    payload = auto_run_builder(Path.cwd(), execute=args.execute, timeout_seconds=args.timeout_seconds)
    write_json(Path.cwd() / ".automation" / "status" / "auto-builder-runner-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
