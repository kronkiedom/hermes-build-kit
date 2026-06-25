#!/usr/bin/env python3
"""Poll active plan threads for operator replies/approvals."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, record_contract_approval, update_plan_index, utc_now, write_json


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


def request_json(token: str, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes plan thread poller",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {body}") from exc


def active_thread_plans(repo_root: Path) -> list[dict[str, Any]]:
    index = read_json(repo_root / ".automation" / "plans-index.json", {"plans": {}})
    plans = index.get("plans", {}) if isinstance(index, dict) else {}
    out = []
    for entry in plans.values():
        if not isinstance(entry, dict):
            continue
        plan_dir = Path(str(entry.get("plan_dir") or repo_root / "plans" / str(entry.get("plan_id"))))
        meta = read_json(plan_dir / "meta.json", {})
        if not isinstance(meta, dict):
            meta = {}
        merged = {**entry, **meta, "plan_dir": str(plan_dir)}
        state = str(merged.get("state") or "")
        if state in {"DONE", "CANCELLED"}:
            continue
        thread_id = str((merged.get("discord") or {}).get("thread_id") or merged.get("thread_id") or "")
        if thread_id:
            merged["thread_id"] = thread_id
            out.append(merged)
    return out


def append_reply(plan_dir: Path, reply: dict[str, Any]) -> None:
    path = plan_dir / "operator-replies.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(reply, sort_keys=True) + "\n")


def classify_reply_action(content: str) -> str:
    normalized = content.strip().lower()
    if normalized in {"approve", "approved", "lgtm", "yes", "ok", "go", "ship it"} or normalized.startswith("approve "):
        return "APPROVE"
    if normalized in {"reject", "rejected", "no"} or normalized.startswith("reject "):
        return "REJECT"
    if normalized in {"cancel", "cancelled", "stop"} or normalized.startswith("cancel "):
        return "CANCEL"
    return "REPLY"


def handle_operator_reply(repo_root: Path, plan: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id"))
    plan_dir = Path(str(plan.get("plan_dir")))
    content = str(message.get("content") or "")
    reply = {
        "message_id": str(message.get("id")),
        "author_id": str((message.get("author") or {}).get("id") or ""),
        "content": content,
        "created_at": message.get("timestamp") or utc_now(),
        "ingested_at": utc_now(),
    }
    append_reply(plan_dir, reply)
    action = classify_reply_action(content)
    if action in {"APPROVE", "REJECT", "CANCEL"} and str(plan.get("state")) == "CONTRACT_REVIEW":
        return {"action": "contract_decision_recorded", "decision": action, **record_contract_approval(repo_root, plan_id, decision=action, source="discord-thread", message_id=reply["message_id"])}

    meta_path = plan_dir / "meta.json"
    meta = read_json(meta_path, {})
    if isinstance(meta, dict) and (meta.get("state") == "QUESTION" or meta.get("awaiting_operator")):
        meta["state"] = "CONTRACT"
        meta["awaiting_operator"] = False
        meta["state_reason"] = "operator reply ingested; ready for contract reshaping"
        meta["updated_at"] = utc_now()
        write_json(meta_path, meta)
        update_plan_index(repo_root, plan_id, {
            "plan_id": plan_id,
            "title": meta.get("title"),
            "state": meta["state"],
            "repo": meta.get("repo"),
            "base_branch": meta.get("base_branch"),
            "thread_id": (meta.get("discord") or {}).get("thread_id"),
            "plan_dir": str(plan_dir),
            "updated_at": meta["updated_at"],
        })
        return {"action": "operator_reply_ingested", "plan_id": plan_id, "next_state": "CONTRACT"}
    return {"action": "operator_reply_recorded", "plan_id": plan_id}


def poll_plan_threads(repo_root: Path, *, operator_user_id: str, token: str, dry_run: bool = False) -> dict[str, Any]:
    state_path = repo_root / ".automation" / "discord-plan-thread-poller-state.json"
    state = read_json(state_path, {"plans": {}})
    state.setdefault("plans", {})
    actions = []
    checked = 0
    for plan in active_thread_plans(repo_root):
        plan_id = str(plan.get("plan_id"))
        thread_id = str(plan.get("thread_id"))
        plan_state = state["plans"].setdefault(plan_id, {})
        query = {"limit": 50}
        if plan_state.get("last_message_id"):
            query["after"] = plan_state["last_message_id"]
        if dry_run:
            actions.append({"action": "would_poll_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
            continue
        messages = request_json(token, f"/channels/{thread_id}/messages?{urllib.parse.urlencode(query)}")
        if not isinstance(messages, list):
            messages = []
        messages = sorted(messages, key=lambda item: int(item.get("id", 0)))
        if not plan_state.get("last_message_id") and not plan_state.get("initialized_at"):
            if messages:
                plan_state["last_message_id"] = str(messages[-1].get("id"))
            plan_state["initialized_at"] = utc_now()
            actions.append({"action": "initialized_plan_thread_cursor", "plan_id": plan_id, "thread_id": thread_id})
            continue
        for message in messages:
            checked += 1
            message_id = str(message.get("id"))
            plan_state["last_message_id"] = message_id
            author_id = str((message.get("author") or {}).get("id") or "")
            if author_id != operator_user_id:
                continue
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            if dry_run:
                actions.append({"action": "would_ingest_operator_reply", "plan_id": plan_id, "message_id": message_id})
            else:
                actions.append(handle_operator_reply(repo_root, plan, message))
    state["updated_at"] = utc_now()
    if not dry_run:
        write_json(state_path, state)
    return {"kind": "PLAN-THREAD-POLLER", "checked": checked, "actions": actions, "dry_run": dry_run}


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
    token = "" if args.dry_run else load_token()
    payload = poll_plan_threads(repo_root, operator_user_id=operator_user_id, token=token, dry_run=args.dry_run)
    write_json(repo_root / ".automation" / "status" / "plan-thread-poller-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
