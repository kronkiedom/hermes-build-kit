#!/usr/bin/env python3
"""Create one dedicated Discord thread for each in-flight build-control task."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from plan_automation_lib import create_discord_thread, read_json, utc_now, write_json
from plan_status_lib import add_thread_member, post_message, update_message

TERMINAL_STATES = {"DONE", "CANCELLED"}


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


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def thread_required(meta: dict[str, Any]) -> bool:
    discord = meta.get("discord") if isinstance(meta.get("discord"), dict) else {}
    return bool(discord.get("requires_dedicated_thread")) and not discord.get("thread_id")


def starter_message(meta: dict[str, Any]) -> str:
    packet_raw = meta.get("pr_packet")
    packet: dict[str, Any] = packet_raw if isinstance(packet_raw, dict) else {}
    discord_raw = meta.get("discord")
    discord: dict[str, Any] = discord_raw if isinstance(discord_raw, dict) else {}
    kind = packet.get("kind") or "build"
    title = packet.get("title") or meta.get("task_id")
    prompt = discord.get("prompt") or "Track this build-control action in this dedicated thread."
    state = meta.get("state") or "UNKNOWN"
    needs = prompt
    if kind == "decision_required" or meta.get("awaiting_operator"):
        needs = "Operator decision/reply in this thread."
    elif state in {"SHAPE", "EXECUTE", "VERIFYING"}:
        needs = "Complete the build-control worker step, record evidence, and pass readiness before PR handoff."
    return (
        f"**Persistent task card**\n"
        f"**Task:** {title}\n"
        f"**Task ID:** `{meta.get('task_id')}`\n"
        f"**Source plan:** `{meta.get('source_plan_id') or 'unknown'}`\n"
        f"**Kind/state:** `{kind}` / `{state}`\n"
        f"**Where it is:** {meta.get('state_reason') or prompt}\n"
        f"**Needs to complete:** {needs}\n"
        f"**Branch:** `{packet.get('branch') or meta.get('branch') or 'not assigned'}`\n"
        f"\n{prompt}\n\n"
        "Reply in this thread to progress the workflow; decision replies are recorded into the task metadata."
    )[:1900]


def ensure_threads(repo_root: Path, *, channel_id: str, token: str | None = None, operator_user_id: str = "", dry_run: bool = False) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        if str(meta.get("state") or "") in TERMINAL_STATES:
            continue
        discord_raw = meta.get("discord")
        discord: dict[str, Any] = discord_raw if isinstance(discord_raw, dict) else {}
        needs_thread = bool(discord.get("requires_dedicated_thread")) and not discord.get("thread_id")
        has_thread = bool(discord.get("thread_id"))
        if not needs_thread and not has_thread:
            continue
        title = str(discord.get("thread_title") or meta.get("task_id") or meta_path.parent.name)[:100]
        action = {
            "action": "would_create_thread" if dry_run and needs_thread else ("would_update_task_card" if dry_run else "updated_task_card"),
            "task_id": meta.get("task_id") or meta_path.parent.name,
            "title": title,
            "channel_id": channel_id,
        }
        if dry_run:
            actions.append(action)
            continue
        if not token:
            raise RuntimeError("Discord token is required unless --dry-run is supplied")
        card = starter_message(meta)
        if needs_thread:
            thread_id, starter_message_id = create_discord_thread(token, channel_id, title, card)
            discord["thread_id"] = thread_id
            discord["starter_message_id"] = starter_message_id
            discord["task_card_message_id"] = starter_message_id
            discord["thread_created_at"] = utc_now()
            action["action"] = "created_thread"
            action["starter_message_id"] = starter_message_id
        else:
            thread_id = str(discord.get("thread_id"))
            card_id = str(discord.get("task_card_message_id") or "")
            if card_id:
                try:
                    update_message(token, thread_id, card_id, card)
                    action["task_card_message_id"] = card_id
                except Exception:
                    card_id = ""
            if not card_id:
                card_id = post_message(token, thread_id, card)
                discord["task_card_message_id"] = card_id
                action["action"] = "created_task_card"
                action["task_card_message_id"] = card_id
        if operator_user_id:
            try:
                add_thread_member(token, str(discord.get("thread_id")), operator_user_id)
            except Exception:
                # Best-effort visibility; the thread is still linked in status output.
                pass
        meta["discord"] = discord
        meta["updated_at"] = utc_now()
        write_json(meta_path, meta)
        actions.append({**action, "thread_id": discord.get("thread_id")})
    return {"kind": "BUILD-THREADS", "checked_at": utc_now(), "dry_run": dry_run, "action_count": len(actions), "actions": actions}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing", default=".automation/discord-routing.json")
    parser.add_argument("--channel-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    repo_root = Path.cwd()
    routing = read_json(repo_root / args.routing, {})
    channel_id = args.channel_id or routing.get("build_control_channel_id")
    if not channel_id:
        raise RuntimeError("build-control channel id is required")
    token = None if args.dry_run else load_token()
    payload = ensure_threads(repo_root, channel_id=str(channel_id), token=token, operator_user_id=str(routing.get("operator_user_id") or ""), dry_run=args.dry_run)
    write_json(repo_root / ".automation" / "status" / "ensure-build-threads-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
