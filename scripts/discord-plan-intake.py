#!/usr/bin/env python3
"""Create a durable plan intake record and optionally a Discord thread."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from plan_automation_lib import DEFAULT_BASE_BRANCH, IntakeRequest, create_plan_intake


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Target repo, e.g. owner/repo or an absolute path")
    parser.add_argument("--base-branch", default=DEFAULT_BASE_BRANCH)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan-file")
    source.add_argument("--plan-text")
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--control-channel-id", required=True)
    parser.add_argument("--operator-user-id", required=True)
    parser.add_argument("--thread-id", help="Existing thread ID; required with --no-discord")
    parser.add_argument("--no-discord", action="store_true", help="Do not call Discord API; use supplied --thread-id")
    parser.add_argument("--discord-token", default=os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.plan_file:
        plan_markdown = Path(args.plan_file).read_text(encoding="utf-8")
    else:
        plan_markdown = args.plan_text
    result = create_plan_intake(
        Path.cwd(),
        IntakeRequest(
            repo=args.repo,
            base_branch=args.base_branch,
            plan_markdown=plan_markdown,
            guild_id=args.guild_id,
            control_channel_id=args.control_channel_id,
            operator_user_id=args.operator_user_id,
            thread_id=args.thread_id,
            no_discord=args.no_discord,
            discord_token=args.discord_token,
        ),
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
