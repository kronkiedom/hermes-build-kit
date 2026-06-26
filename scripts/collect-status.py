#!/usr/bin/env python3
"""Collect simple durable status ledgers for a portable automation install.

Portable starter script: adapt directory names and richer policy as needed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def main():
    repo_root = Path.cwd()
    automation_root = repo_root / ".automation"
    status_root = automation_root / "status"
    tasks_root = repo_root / "tasks"
    backlog_root = repo_root / ".backlog"
    prep_root = repo_root / ".auto-prep"

    task_meta = list(tasks_root.glob("*/meta.json")) if tasks_root.exists() else []
    backlog_candidates = list(backlog_root.glob("candidate-*.md")) if backlog_root.exists() else []
    prep_candidates = list(prep_root.glob("*.md")) if prep_root.exists() else []
    plans_index_path = automation_root / "plans-index.json"
    if plans_index_path.exists():
        try:
            plans_index = json.loads(plans_index_path.read_text())
            plan_count = len(plans_index.get("plans", {}))
        except Exception:
            plan_count = -1
    else:
        plan_count = 0

    now = datetime.now(timezone.utc).isoformat()
    write_json(status_root / "backlog-discovery-last.json", {
        "timestamp": now,
        "kind": "READY" if backlog_root.exists() else "MISSING",
        "candidate_count": len(backlog_candidates),
        "note": "starter status collection only",
    })
    write_json(status_root / "soft-prep-last.json", {
        "timestamp": now,
        "kind": "READY" if prep_root.exists() else "MISSING",
        "prepared_count": len(prep_candidates),
        "note": "starter status collection only",
    })
    write_json(status_root / "prep-admission-last.json", {
        "timestamp": now,
        "kind": "READY",
        "task_count": len(task_meta),
        "note": "starter status collection only",
    })
    print(json.dumps({
        "status_root": str(status_root),
        "task_count": len(task_meta),
        "backlog_candidate_count": len(backlog_candidates),
        "prepared_count": len(prep_candidates),
        "plan_count": plan_count,
    }, indent=2))


if __name__ == "__main__":
    main()
