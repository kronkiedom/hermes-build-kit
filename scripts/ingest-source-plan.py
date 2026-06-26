#!/usr/bin/env python3
"""Ingest an existing plan file into build-control, audit it, and optionally start execution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from plan_automation_lib import SourcePlanIngestRequest, ingest_source_plan, write_json, utc_now


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", required=True, help="Markdown plan file to ingest")
    parser.add_argument("--repo", required=True, help="Target local git checkout for dispatch/build work")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--guild-id", default="")
    parser.add_argument("--control-channel-id", default="")
    parser.add_argument("--operator-user-id", default="")
    parser.add_argument("--thread-id", default=None, help="Existing Discord thread id; required with --no-discord")
    parser.add_argument("--no-discord", action="store_true", help="Do not create a Discord thread; use --thread-id")
    parser.add_argument("--discord-token", default=None, help="Discord bot token; defaults to DISCORD_BOT_TOKEN")
    parser.add_argument("--auto-approve", action="store_true", help="Development/operator override: approve the shaped contract immediately")
    parser.add_argument("--decompose", action="store_true", help="After contract approval, create PR-sized task packets")
    parser.add_argument("--dispatch", action="store_true", help="After decomposition, run dispatch-pr-worker for the first eligible task")
    parser.add_argument("--execute-dispatch", action="store_true", help="Make dispatch mutate task state and create the isolated worktree")
    parser.add_argument("--worktree-root", default=".automation/pr-worktrees")
    parser.add_argument("--force-status-override", action="store_true", help="Override retired/blocked source-plan audit blockers")
    parser.add_argument("--force-author-override", action="store_true", help="Explicit operator assertion that this source plan was authored by the operator despite missing author metadata")
    parser.add_argument("--operator-author-alias", action="append", default=None, help="Allowed source-plan author alias; repeatable. Defaults to Dom/domarmor/dom-armor")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = args.discord_token or os.environ.get("DISCORD_BOT_TOKEN")
    request = SourcePlanIngestRequest(
        plan_file=Path(args.plan_file),
        repo=args.repo,
        base_branch=args.base_branch,
        guild_id=args.guild_id,
        control_channel_id=args.control_channel_id,
        operator_user_id=args.operator_user_id,
        thread_id=args.thread_id,
        no_discord=args.no_discord,
        discord_token=token,
        auto_approve=args.auto_approve,
        decompose=args.decompose,
        dispatch=args.dispatch,
        execute_dispatch=args.execute_dispatch,
        worktree_root=args.worktree_root,
        force_status_override=args.force_status_override,
        force_author_override=args.force_author_override,
        operator_author_aliases=tuple(args.operator_author_alias or ("Dom", "domarmor", "dom-armor")),
    )
    payload = ingest_source_plan(Path.cwd(), request)
    write_json(Path.cwd() / ".automation" / "status" / "ingest-source-plan-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
