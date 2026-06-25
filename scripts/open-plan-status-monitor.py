#!/usr/bin/env python3
"""Sync open build-control plan states with their Discord threads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plan_automation_lib import read_json, write_json, utc_now
from plan_status_lib import load_discord_token, sync_open_plan_threads


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", default=".automation/discord-routing.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path.cwd()
    routing = read_json(repo_root / args.routing, {})
    operator_user_id = str(routing.get("operator_user_id") or "")
    if not operator_user_id:
        raise RuntimeError("discord routing must define operator_user_id")
    token = "" if args.dry_run else load_discord_token()
    payload = sync_open_plan_threads(
        repo_root,
        operator_user_id=operator_user_id,
        token=token,
        dry_run=args.dry_run,
    )
    status_path = repo_root / ".automation" / "status" / "open-plan-status-last.json"
    write_json(status_path, {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
