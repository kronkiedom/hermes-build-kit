#!/usr/bin/env python3
"""Detect stale automation tasks and worker status ledgers.

Portable starter worker for the automation architecture's stall detector role. It
is intentionally read-only with respect to tasks: it writes one status artifact
under `.automation/status/` and leaves remediation decisions to the operator or a
separate dispatcher.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

TERMINAL_TASK_STATES = {"DONE", "CANCELLED", "parked"}
STATUS_TIME_FIELDS = ["checked_at", "timestamp", "generated_at", "updated_at", "last_run_at"]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_utc(now: str | None = None) -> datetime:
    parsed = parse_time(now) if now else None
    return parsed or datetime.now(timezone.utc)


def item_age_hours(reference: datetime, value: str | None) -> float | None:
    parsed = parse_time(value)
    if not parsed:
        return None
    return (reference - parsed).total_seconds() / 3600


def task_timestamp(meta: dict[str, Any]) -> str | None:
    for key in ["updated_at", "created", "created_at"]:
        if meta.get(key):
            return str(meta[key])
    return None


def detect_task_stalls(repo_root: Path, *, reference: datetime, stale_after: timedelta) -> list[dict[str, Any]]:
    tasks_root = repo_root / "tasks"
    if not tasks_root.exists():
        return []
    stalls: list[dict[str, Any]] = []
    for meta_path in sorted(tasks_root.glob("*/meta.json")):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        state = str(meta.get("state") or "UNKNOWN")
        if state in TERMINAL_TASK_STATES:
            continue
        if meta.get("awaiting_operator"):
            continue
        timestamp = task_timestamp(meta)
        parsed = parse_time(timestamp)
        if not parsed:
            stalls.append({
                "kind": "stale_task",
                "severity": "P2",
                "task_id": meta.get("task_id") or meta_path.parent.name,
                "state": state,
                "reason": "active task has no parseable updated_at/created timestamp",
                "meta_path": str(meta_path),
            })
            continue
        age = reference - parsed
        if age > stale_after:
            stalls.append({
                "kind": "stale_task",
                "severity": "P2",
                "task_id": meta.get("task_id") or meta_path.parent.name,
                "state": state,
                "age_hours": round(age.total_seconds() / 3600, 2),
                "threshold_hours": round(stale_after.total_seconds() / 3600, 2),
                "updated_at": timestamp,
                "reason": meta.get("state_reason") or "active task has not changed within threshold",
                "meta_path": str(meta_path),
            })
    return stalls


def status_timestamp(payload: dict[str, Any]) -> str | None:
    for key in STATUS_TIME_FIELDS:
        if payload.get(key):
            return str(payload[key])
    return None


def detect_worker_status_stalls(repo_root: Path, *, reference: datetime, stale_after: timedelta) -> list[dict[str, Any]]:
    status_root = repo_root / ".automation" / "status"
    if not status_root.exists():
        return []
    stalls: list[dict[str, Any]] = []
    for status_path in sorted(status_root.glob("*-last.json")):
        if status_path.name == "stall-detector-last.json":
            continue
        payload = read_json(status_path, {})
        if not isinstance(payload, dict):
            continue
        timestamp = status_timestamp(payload)
        parsed = parse_time(timestamp)
        if not parsed:
            stalls.append({
                "kind": "stale_worker_status",
                "severity": "P2",
                "worker": status_path.stem,
                "reason": "worker status has no parseable timestamp",
                "status_path": str(status_path),
            })
            continue
        age = reference - parsed
        if age > stale_after:
            stalls.append({
                "kind": "stale_worker_status",
                "severity": "P2",
                "worker": status_path.stem,
                "age_hours": round(age.total_seconds() / 3600, 2),
                "threshold_hours": round(stale_after.total_seconds() / 3600, 2),
                "updated_at": timestamp,
                "reason": "worker status ledger has not refreshed within threshold",
                "status_path": str(status_path),
            })
    return stalls


def detect_stalls(repo_root: Path, *, now: str | None = None, stale_hours: float = 24, write_status: bool = True) -> dict[str, Any]:
    reference = now_utc(now)
    stale_after = timedelta(hours=stale_hours)
    task_stalls = detect_task_stalls(repo_root, reference=reference, stale_after=stale_after)
    worker_stalls = detect_worker_status_stalls(repo_root, reference=reference, stale_after=stale_after)
    stalls = [*task_stalls, *worker_stalls]
    result = {
        "kind": "STALL-DETECTOR",
        "checked_at": reference.isoformat(),
        "decision": "STALLS_FOUND" if stalls else "CLEAR",
        "stall_count": len(stalls),
        "stalls": stalls,
        "threshold_hours": stale_hours,
    }
    if write_status:
        write_json(repo_root / ".automation" / "status" / "stall-detector-last.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stale-hours", type=float, default=24)
    parser.add_argument("--now", default=None, help="Override current time for deterministic tests, ISO-8601.")
    parser.add_argument("--dry-run", action="store_true", help="Compute stalls without writing status/stall-detector-last.json")
    args = parser.parse_args()
    payload = detect_stalls(Path.cwd(), now=args.now, stale_hours=args.stale_hours, write_status=not args.dry_run)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
