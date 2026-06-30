#!/usr/bin/env python3
"""Portable auto-dispatch starter.

This script intentionally does not run model roles. It only inspects durable task
state and emits a decision about whether dispatch would be allowed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_STATES = {"SHAPE", "CONTRACT-CHECKPOINT", "DRAFT-LOOP", "EXECUTE", "VERIFY-LOOP", "EVIDENCE", "RETRO", "quota-paused", "escalated"}


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def main():
    repo_root = Path.cwd()
    tasks_root = repo_root / "tasks"
    automation_root = repo_root / ".automation"
    ledger_path = automation_root / "dispatch-ledger.json"

    active = []
    if tasks_root.exists():
        for meta_path in sorted(tasks_root.glob("*/meta.json")):
            meta = load_json(meta_path, {})
            if meta.get("state") in ACTIVE_STATES and not meta.get("dispatch", {}).get("pr_created", False):
                active.append({
                    "task_id": meta.get("task_id", meta_path.parent.name),
                    "state": meta.get("state"),
                    "awaiting_operator": meta.get("awaiting_operator", False),
                })

    if len(active) > 1:
        decision = "BLOCKED"
        reason = "more than one active task present"
    elif len(active) == 1:
        decision = "HOLD"
        reason = f"active task present: {active[0]['task_id']}"
    else:
        decision = "READY"
        reason = "no active task present"

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "reason": reason,
        "active_tasks": active,
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
