#!/usr/bin/env python3
"""Create or verify the Discord channel used for PR status messages."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plan_automation_lib import read_json, utc_now, write_json
from pr_status_lib import ensure_discord_text_channel, load_discord_token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", default=".automation/discord-routing.json")
    parser.add_argument("--name", default="pr-status")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path.cwd()
    routing_path = repo_root / args.routing
    routing = read_json(routing_path, {})
    guild_id = str(routing.get("guild_id") or "")
    if not guild_id:
        raise RuntimeError("discord routing must define guild_id")
    topic = "Status-only channel for open PRs authored by the operator. Issue threads ping the operator for fixes/rebases."
    if args.dry_run:
        result = {"guild_id": guild_id, "name": args.name, "dry_run": True, "would_update": str(routing_path)}
    else:
        token = load_discord_token()
        channel = ensure_discord_text_channel(token, guild_id, args.name, topic=topic)
        routing["pr_status_channel_id"] = channel["id"]
        routing["pr_status_channel_name"] = channel["name"]
        routing["pr_status_channel_verified_at"] = utc_now()
        write_json(routing_path, routing)
        result = {"guild_id": guild_id, **channel, "routing_path": str(routing_path)}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
