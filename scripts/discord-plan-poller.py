#!/usr/bin/env python3
"""Poll a Discord build-control channel for `build plan:` messages.

This avoids a core Hermes plugin change for the MVP: a cron job can run this
script, convert new operator messages into durable plan intake artifacts, and
create one Discord thread per accepted plan.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from plan_automation_lib import DEFAULT_BASE_BRANCH, IntakeRequest, create_plan_intake, read_json, write_json, utc_now


def request_json(token: str, path: str, method: str = "GET", payload: dict | None = None) -> dict | list:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes plan poller",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {body}") from exc


def post_message(token: str, channel_id: str, content: str) -> None:
    request_json(token, f"/channels/{channel_id}/messages", method="POST", payload={"content": content[:1900]})


def load_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
    if token:
        return token
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {"DISCORD_BOT_TOKEN", "DISCORD_TOKEN"}:
                return value.strip().strip('"\'')
    raise RuntimeError("DISCORD_BOT_TOKEN not found")


def parse_build_message(content: str) -> str | None:
    lowered = content.lower()
    marker = "build plan:"
    if marker not in lowered:
        return None
    start = lowered.index(marker) + len(marker)
    return content[start:].strip() or None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", default=".automation/discord-routing.json")
    parser.add_argument("--state", default=".automation/discord-plan-poller-state.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path.cwd()
    routing = read_json(repo_root / args.routing, {})
    control_channel_id = routing.get("build_control_channel_id")
    guild_id = routing.get("guild_id")
    operator_user_id = str(routing.get("operator_user_id") or "")
    target_repo = routing.get("default_repo") or "operator-defined"
    base_branch = routing.get("default_base_branch") or DEFAULT_BASE_BRANCH
    if not control_channel_id or not guild_id or not operator_user_id:
        raise RuntimeError("discord routing must define guild_id, build_control_channel_id, operator_user_id")

    state_path = repo_root / args.state
    state = read_json(state_path, {"last_message_id": None, "processed_message_ids": []})
    processed = set(str(x) for x in state.get("processed_message_ids", []))
    token = load_token()
    query = {"limit": 20}
    if state.get("last_message_id"):
        query["after"] = state["last_message_id"]
    messages = request_json(token, f"/channels/{control_channel_id}/messages?{urllib.parse.urlencode(query)}")
    messages = sorted(messages, key=lambda item: int(item["id"]))

    accepted = []
    for message in messages:
        message_id = str(message.get("id"))
        author_id = str((message.get("author") or {}).get("id"))
        if message_id in processed:
            continue
        state["last_message_id"] = message_id
        processed.add(message_id)
        if author_id != operator_user_id:
            continue
        plan_text = parse_build_message(message.get("content") or "")
        if not plan_text:
            continue
        if args.dry_run:
            accepted.append({"message_id": message_id, "dry_run": True})
            continue
        result = create_plan_intake(
            repo_root,
            IntakeRequest(
                repo=target_repo,
                base_branch=base_branch,
                plan_markdown=plan_text,
                guild_id=str(guild_id),
                control_channel_id=str(control_channel_id),
                operator_user_id=operator_user_id,
                no_discord=False,
                discord_token=token,
            ),
        )
        accepted.append({"message_id": message_id, **result})
        post_message(
            token,
            control_channel_id,
            f"Accepted build plan `{result['plan_id']}` → <#{result['thread_id']}>. I will shape a contract before PR work starts.",
        )

    state["processed_message_ids"] = sorted(processed, key=int)[-200:] if processed else []
    state["updated_at"] = utc_now()
    write_json(state_path, state)
    print(json.dumps({"kind": "DISCORD-PLAN-POLLER", "accepted": accepted, "checked": len(messages)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
