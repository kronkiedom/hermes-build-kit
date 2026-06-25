#!/usr/bin/env python3
"""Open-plan status classification and Discord thread lifecycle helpers."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json

TERMINAL_PLAN_STATES = {"DONE", "CANCELLED"}
OPERATOR_WAIT_STATES = {"QUESTION", "CONTRACT-CHECKPOINT"}
ACTIVE_PLAN_ALERT_STATES = {"OPERATOR_PENDING", "ACTION_PENDING", "IN_PROGRESS"}


def load_discord_token() -> str:
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


def discord_request(token: str, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes open plan status monitor",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Discord API {method} {path} failed: {exc.code} {body}") from exc


def post_message(token: str, channel_id: str, content: str) -> str:
    message = discord_request(token, f"/channels/{channel_id}/messages", method="POST", payload={"content": content[:1900]})
    return str(message["id"])


def archive_thread(token: str, thread_id: str) -> None:
    # Discord closes a thread by archiving it. Locking keeps completed/cancelled
    # plan threads from being revived as stale action surfaces.
    discord_request(token, f"/channels/{thread_id}", method="PATCH", payload={"archived": True, "locked": True})


def load_plan_meta(repo_root: Path, plan_entry: dict[str, Any]) -> dict[str, Any]:
    plan_dir = Path(str(plan_entry.get("plan_dir") or repo_root / "plans" / str(plan_entry.get("plan_id"))))
    meta = read_json(plan_dir / "meta.json", {})
    if not isinstance(meta, dict):
        meta = {}
    merged = {**plan_entry, **meta}
    merged["plan_dir"] = str(plan_dir)
    merged.setdefault("plan_id", plan_entry.get("plan_id") or plan_dir.name)
    return merged


def classify_plan(plan: dict[str, Any]) -> dict[str, Any]:
    state = str(plan.get("state") or "UNKNOWN")
    awaiting_operator = bool(plan.get("awaiting_operator")) or state in OPERATOR_WAIT_STATES
    terminal = state in TERMINAL_PLAN_STATES
    thread_id = str((plan.get("discord") or {}).get("thread_id") or plan.get("thread_id") or "")
    status = "TERMINAL" if terminal else ("OPERATOR_WAITING" if awaiting_operator else "IN_PROGRESS")
    return {
        "plan_id": plan.get("plan_id"),
        "title": plan.get("title"),
        "state": state,
        "status": status,
        "awaiting_operator": awaiting_operator,
        "terminal": terminal,
        "thread_id": thread_id,
        "reason": plan.get("state_reason") or "",
        "updated_at": plan.get("updated_at"),
        "repo": plan.get("repo"),
        "base_branch": plan.get("base_branch"),
    }


def plan_fingerprint(status: dict[str, Any]) -> str:
    return "|".join([
        str(status.get("plan_id") or ""),
        str(status.get("state") or ""),
        str(status.get("status") or ""),
        str(status.get("reason") or ""),
        str(status.get("updated_at") or ""),
    ])


def format_operator_alert(status: dict[str, Any], operator_user_id: str) -> str:
    return (
        f"<@{operator_user_id}> Plan `{status.get('plan_id')}` needs operator input.\n"
        f"- title: {status.get('title')}\n"
        f"- state: `{status.get('state')}`\n"
        f"- reason: {status.get('reason') or 'awaiting operator decision'}\n\n"
        "Reply in this thread with the requested decision/input."
    )[:1900]


def format_terminal_message(status: dict[str, Any]) -> str:
    label = "completed" if status.get("state") == "DONE" else "cancelled"
    return (
        f"✅ Plan `{status.get('plan_id')}` is {label}.\n"
        f"- title: {status.get('title')}\n"
        f"- final state: `{status.get('state')}`\n"
        f"- reason: {status.get('reason') or 'terminal state reached'}\n\n"
        "Closing this plan thread so stale plan alerts do not keep resurfacing."
    )[:1900]


def sync_open_plan_threads(
    repo_root: Path,
    *,
    operator_user_id: str,
    token: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    index_path = repo_root / ".automation" / "plans-index.json"
    ledger_path = repo_root / ".automation" / "plan-status-ledger.json"
    plans_index = read_json(index_path, {"plans": {}})
    plans = plans_index.get("plans", {}) if isinstance(plans_index, dict) else {}
    ledger = read_json(ledger_path, {"plans": {}})
    ledger.setdefault("plans", {})
    actions: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    for raw_entry in plans.values():
        if not isinstance(raw_entry, dict):
            continue
        meta = load_plan_meta(repo_root, raw_entry)
        status = classify_plan(meta)
        statuses.append(status)
        plan_id = str(status.get("plan_id") or "")
        if not plan_id:
            continue
        entry = ledger["plans"].setdefault(plan_id, {})
        thread_id = status.get("thread_id") or entry.get("thread_id")
        if thread_id:
            entry["thread_id"] = thread_id
        fingerprint = plan_fingerprint(status)
        active = entry.get("active_alert") if isinstance(entry.get("active_alert"), dict) else None

        if status.get("terminal"):
            if entry.get("last_state") == status.get("state") and entry.get("thread_archived_at"):
                actions.append({"action": "already_closed", "plan_id": plan_id})
            elif dry_run:
                actions.append({"action": "would_close_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
            else:
                if thread_id:
                    post_message(token, thread_id, format_terminal_message(status))
                    archive_thread(token, thread_id)
                    entry["thread_archived_at"] = utc_now()
                    actions.append({"action": "closed_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
                else:
                    actions.append({"action": "terminal_no_thread", "plan_id": plan_id})
            if active:
                active["state"] = "RESOLVED"
                active["resolved_at"] = active.get("resolved_at") or utc_now()
                active["resolved_by"] = str(status.get("state"))
            entry["last_state"] = status.get("state")
            entry["last_fingerprint"] = fingerprint
            entry["updated_at"] = utc_now()
            continue

        if status.get("awaiting_operator"):
            if active and active.get("state") in ACTIVE_PLAN_ALERT_STATES:
                active["last_seen_at"] = utc_now()
                active["current_fingerprint"] = fingerprint
                actions.append({"action": "suppressed_plan_alert_pending", "plan_id": plan_id, "state": active.get("state")})
            elif entry.get("last_fingerprint") != fingerprint:
                if dry_run:
                    actions.append({"action": "would_alert_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
                else:
                    if thread_id:
                        post_message(token, thread_id, format_operator_alert(status, operator_user_id))
                        actions.append({"action": "alerted_plan_thread", "plan_id": plan_id, "thread_id": thread_id})
                    else:
                        actions.append({"action": "operator_wait_no_thread", "plan_id": plan_id})
                entry["active_alert"] = {
                    "state": "OPERATOR_PENDING",
                    "fingerprint": fingerprint,
                    "current_fingerprint": fingerprint,
                    "opened_at": utc_now(),
                    "last_seen_at": utc_now(),
                }
            entry["last_fingerprint"] = fingerprint
        else:
            if active and active.get("state") in ACTIVE_PLAN_ALERT_STATES:
                active["state"] = "RESOLVED"
                active["resolved_at"] = utc_now()
                active["resolved_by"] = "plan_left_operator_wait"
                actions.append({"action": "resolved_plan_alert", "plan_id": plan_id})
            actions.append({"action": "plan_in_progress_no_ping", "plan_id": plan_id, "state": status.get("state")})
            entry["last_fingerprint"] = fingerprint

        entry["last_state"] = status.get("state")
        entry["updated_at"] = utc_now()

    ledger["updated_at"] = utc_now()
    if not dry_run:
        write_json(ledger_path, ledger)
    return {"kind": "OPEN-PLAN-STATUS", "status_count": len(statuses), "statuses": statuses, "actions": actions, "ledger_path": str(ledger_path), "dry_run": dry_run}
