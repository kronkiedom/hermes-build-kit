#!/usr/bin/env python3
"""Scan operator-authored open GitHub PRs and sync a Discord status channel.

The monitor is safe by default: it reports status, opens/pings issue threads,
and never pushes, merges, or resolves review comments.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from plan_automation_lib import read_json, utc_now, write_json
from pr_status_lib import (
    apply_stacked_pr_blocks,
    classify_pr,
    fetch_pr_details,
    gh_login,
    load_discord_token,
    search_open_prs,
    sync_discord_status_channel,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", default=".automation/discord-routing.json")
    parser.add_argument("--author", default=None, help="GitHub login to scan; defaults to gh api user")
    parser.add_argument("--query-extra", default="", help="Extra GitHub search qualifiers, e.g. 'repo:owner/repo'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path.cwd()
    routing = read_json(repo_root / args.routing, {})
    channel_id = str(routing.get("pr_status_channel_id") or "")
    operator_user_id = str(routing.get("operator_user_id") or "")
    if not channel_id and not args.dry_run:
        raise RuntimeError("discord routing must define pr_status_channel_id; run setup-discord-pr-status-channel.py first")
    if not operator_user_id:
        raise RuntimeError("discord routing must define operator_user_id")

    author = args.author or routing.get("operator_github_login") or gh_login()
    prs = search_open_prs(str(author), args.query_extra or str(routing.get("pr_status_query_extra") or ""))
    statuses = []
    errors = []
    for pr_ref in prs:
        try:
            details = fetch_pr_details(pr_ref["owner"], pr_ref["repo"], int(pr_ref["number"]))
            statuses.append(classify_pr(details, operator_login=str(author)))
        except Exception as exc:  # fail loud in output, but keep other PRs visible
            errors.append({"pr": pr_ref, "error": str(exc)})
    apply_stacked_pr_blocks(statuses)
    token = "" if args.dry_run else load_discord_token()
    sync = sync_discord_status_channel(
        repo_root,
        statuses,
        channel_id=channel_id or "dry-run-channel",
        operator_user_id=operator_user_id,
        token=token,
        dry_run=args.dry_run,
    )
    status_path = repo_root / ".automation" / "status" / "pr-status-monitor-last.json"
    payload = {
        "kind": "PR-STATUS-MONITOR",
        "author": author,
        "checked_at": utc_now(),
        "open_pr_count": len(statuses),
        "issue_pr_count": sum(1 for status in statuses if status.get("issues")),
        "statuses": statuses,
        "errors": errors,
        "sync": sync,
        "dry_run": args.dry_run,
    }
    write_json(status_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
