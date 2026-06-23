#!/usr/bin/env python3
"""Portable prepared-candidate admission starter.

Consumes a simple ack file and creates a starter task directory.
Adapt schema, safety rules, and approval semantics per target repo.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def main():
    repo_root = Path.cwd()
    prep_root = repo_root / ".auto-prep"
    tasks_root = repo_root / "tasks"
    automation_root = repo_root / ".automation"
    status_root = automation_root / "status"
    ack_path = prep_root / "ack.json"

    ack = load_json(ack_path)
    if not ack:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "NO-WORK",
            "note": "no ack.json present",
        }
        write_json(status_root / "prep-admission-last.json", result)
        print(json.dumps(result, indent=2))
        return

    task_id = ack.get("task_id") or ack.get("candidate_id", "candidate").replace("candidate-", "")
    task_dir = tasks_root / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "task_id": task_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "state": "SHAPE",
        "phase_status": {"SHAPE": "PROCESSING"},
        "operator_approvals": [
            {
                "phase": "PREP-ADMISSION",
                "outcome": "APPROVE",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": str(ack_path),
            }
        ],
        "escalations": [],
        "awaiting_operator": False,
        "state_reason": "created from prepared candidate acknowledgement",
    }
    write_json(task_dir / "meta.json", meta)
    (task_dir / "checkpoints.md").write_text(
        "# checkpoints\n\n"
        f"- {datetime.now(timezone.utc).isoformat()} — task admitted from prepared candidate\n"
    )

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": "ADMITTED",
        "task_id": task_id,
        "task_dir": str(task_dir),
        "ack_path": str(ack_path),
    }
    write_json(status_root / "prep-admission-last.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
