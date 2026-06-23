#!/usr/bin/env python3
"""Portable discovery governor starter.

Reads autonomy + simple queue state and emits a durable decision record.
This does not pause real jobs by itself; target environments should wire that behavior.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HIGH_WATERMARK = 10
RESUME_WATERMARK = 3
EMPTY_PASS_THRESHOLD = 3


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def main():
    repo_root = Path.cwd()
    automation_root = repo_root / ".automation"
    status_root = automation_root / "status"
    backlog_root = repo_root / ".backlog"
    autonomy = load_json(automation_root / "AUTONOMY.json", {})

    status_root.mkdir(parents=True, exist_ok=True)
    candidate_count = len(list(backlog_root.glob("candidate-*.md"))) if backlog_root.exists() else 0
    open_candidate_count = candidate_count
    enabled = bool(autonomy.get("enabled", False))

    if open_candidate_count >= HIGH_WATERMARK:
        decision = "pause-discovery"
        reason = f"open_candidate_count {open_candidate_count} >= high watermark {HIGH_WATERMARK}"
    elif open_candidate_count <= RESUME_WATERMARK:
        decision = "resume-discovery"
        reason = f"open_candidate_count {open_candidate_count} <= resume watermark {RESUME_WATERMARK}"
    else:
        decision = "no-change"
        reason = "queue between watermarks"

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "autonomy_enabled": enabled,
        "decision": decision,
        "reason": reason,
        "open_candidate_count": open_candidate_count,
        "threshold_high_watermark": HIGH_WATERMARK,
        "threshold_resume_watermark": RESUME_WATERMARK,
        "threshold_empty_passes": EMPTY_PASS_THRESHOLD,
    }
    out = status_root / "backlog-discovery-governor.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
