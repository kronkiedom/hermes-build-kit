#!/usr/bin/env python3
"""Reconcile child task progress back into parent build-control plans.

This worker closes the gap between task-level progress and plan-level status:
- answered decision packets become dispatch-ready;
- parent cards/state reasons point at the active child task;
- parents become DONE once every child task is terminal.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json

TERMINAL_TASK_STATES = {"DONE", "CANCELLED", "parked"}
TERMINAL_PLAN_STATES = {"DONE", "CANCELLED"}


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def plan_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "plans").glob("*/meta.json"))


def task_packet(meta: dict[str, Any]) -> dict[str, Any]:
    packet = meta.get("pr_packet")
    return packet if isinstance(packet, dict) else {}


def task_github(meta: dict[str, Any]) -> dict[str, Any]:
    github = meta.get("github")
    return github if isinstance(github, dict) else {}


def is_terminal_task(meta: dict[str, Any]) -> bool:
    return str(meta.get("state") or "") in TERMINAL_TASK_STATES


def is_handed_to_pr_status(meta: dict[str, Any]) -> bool:
    github = task_github(meta)
    return bool(github.get("pr_url") or github.get("draft_pr_url"))


def load_tasks_by_plan(repo_root: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    by_plan: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        plan_id = str(meta.get("source_plan_id") or "")
        if not plan_id:
            continue
        by_plan.setdefault(plan_id, []).append((meta_path.parent, meta))
    return by_plan


def render_answered_decision_task(meta: dict[str, Any], packet: dict[str, Any]) -> str:
    task_id = str(meta.get("task_id") or packet.get("task_id") or "")
    title = str(packet.get("title") or task_id)
    plan_id = str(meta.get("source_plan_id") or "")
    branch = str(packet.get("branch") or "")
    decision_raw = meta.get("operator_decision")
    decision: dict[str, Any] = decision_raw if isinstance(decision_raw, dict) else {}
    decision_text = str(decision.get("content") or "").strip()
    options_raw = meta.get("decision_options")
    options: dict[str, Any] = options_raw if isinstance(options_raw, dict) else {}
    option_raw = options.get("pr_b4_sandbox")
    option_a: dict[str, Any] = option_raw if isinstance(option_raw, dict) else {}
    question = str(option_a.get("question") or "Which implementation option was selected?")
    return f"""# Task: {title}

- Source plan: `{plan_id}`
- Branch: `{branch}`
- State: decision answered; ready for build shaping/dispatch

## Captured decision

Question: {question}

Decision: {decision_text or '(decision content recorded in meta.json)'}

## Build intent

Shape and implement the next PR-sized work packet implied by the captured decision. For PR-B4/H7, this means beginning the empirical-verify sandbox work using the selected Option A model:

- public digest-pinned verify image;
- source copy outside `/root`;
- rootless Podman as `swarmrunner`;
- network-layer default-deny with IMDS/link-local/RFC1918 blocked;
- Node/npm first with `--ignore-scripts` default.

## Done means

- Build scope is converted into an implementation-ready PR packet.
- The isolated worktree is prepared from verified GitHub.com `main`.
- Builder evidence and readiness artifacts are written before any PR-ready claim.
- No merge occurs; push/PR handoff still requires the configured readiness gates and operator authorization.
"""


def mark_decision_task_ready(task_dir: Path, meta: dict[str, Any], now: str, *, dry_run: bool = False) -> dict[str, Any] | None:
    """If a decision-required task has a concrete decision, make it dispatch-ready.

    Dispatch only selects SHAPE/READY tasks whose phase_status.SHAPE is READY/DONE/None.
    The reply-ingestion path moves a decision task to SHAPE, but it may leave SHAPE
    marked ACTIVE. This reconciler advances that state once the durable decision is present.
    """
    packet = task_packet(meta)
    state = str(meta.get("state") or "")
    if packet.get("kind") != "decision_required":
        return None
    if state != "SHAPE":
        return None
    if meta.get("awaiting_operator"):
        return None
    if not isinstance(meta.get("operator_decision"), dict):
        return None
    phase_raw = meta.get("phase_status")
    phase: dict[str, Any] = phase_raw if isinstance(phase_raw, dict) else {}
    if phase.get("SHAPE") == "READY" and packet.get("status") == "answered":
        return None
    action = {
        "action": "would_mark_decision_ready" if dry_run else "marked_decision_ready",
        "task_id": meta.get("task_id") or task_dir.name,
        "from_state": state,
        "to_state": "SHAPE",
    }
    if not dry_run:
        phase["DECISION"] = "ANSWERED"
        phase["SHAPE"] = "READY"
        packet["awaiting_operator"] = False
        packet["status"] = "answered"
        meta["phase_status"] = phase
        meta["pr_packet"] = packet
        meta["state_reason"] = "decision answered; ready for dispatch"
        meta["updated_at"] = now
        (task_dir / "task.md").write_text(render_answered_decision_task(meta, packet), encoding="utf-8")
        write_json(task_dir / "meta.json", meta)
    return action


def normalize_builder_ready_task(task_dir: Path, meta: dict[str, Any], now: str, *, dry_run: bool = False) -> dict[str, Any] | None:
    """Repair stale ESCALATED task state after operator/readiness reconciliation.

    Root cause: parent progress and auto-builder selection can disagree when a
    child is no longer awaiting the operator and its execute phase is builder-ready
    but the top-level state still says ESCALATED. Normalize the source state so
    the next autopilot tick can execute the task instead of posting a stale
    operator-input alert.
    """
    state = str(meta.get("state") or "")
    phase_raw = meta.get("phase_status")
    phase: dict[str, Any] = phase_raw if isinstance(phase_raw, dict) else {}
    dispatch_raw = meta.get("dispatch")
    dispatch: dict[str, Any] = dispatch_raw if isinstance(dispatch_raw, dict) else {}
    if state != "ESCALATED":
        return None
    if meta.get("awaiting_operator"):
        return None
    if phase.get("EXECUTE") != "READY_FOR_BUILDER":
        return None
    if not dispatch.get("worktree"):
        return None
    action = {
        "action": "would_normalize_builder_ready_task" if dry_run else "normalized_builder_ready_task",
        "task_id": meta.get("task_id") or task_dir.name,
        "from_state": state,
        "to_state": "READY_FOR_BUILDER",
    }
    if not dry_run:
        meta["state"] = "READY_FOR_BUILDER"
        meta["awaiting_operator"] = False
        meta["state_reason"] = "operator/readiness reconciliation complete; ready for builder execution"
        meta["updated_at"] = now
        write_json(task_dir / "meta.json", meta)
    return action


def child_owner(meta: dict[str, Any]) -> str:
    state = str(meta.get("state") or "UNKNOWN")
    if state in {"DONE", "CANCELLED", "parked"}:
        return "completed" if state == "DONE" else "closed/superseded"
    if meta.get("awaiting_operator") or state == "QUESTION":
        return "operator"
    if is_handed_to_pr_status(meta):
        return "pr-status"
    return "build-control"


def child_workflow_row(task_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    packet = task_packet(meta)
    github = task_github(meta)
    state = str(meta.get("state") or "UNKNOWN")
    pr_number = github.get("pr_number") or packet.get("pr_number")
    label = f"PR #{pr_number}" if pr_number else str(packet.get("packet_id") or meta.get("task_id") or task_dir.name)
    title = str(packet.get("title") or meta.get("title") or meta.get("task_id") or task_dir.name)
    if pr_number and title.startswith("Decision needed:"):
        title = title.replace("Decision needed:", "").strip()
    return {
        "task_id": meta.get("task_id") or task_dir.name,
        "label": label,
        "title": title,
        "state": state,
        "owner": child_owner(meta),
        "awaiting_operator": bool(meta.get("awaiting_operator")),
        "branch": packet.get("branch") or meta.get("branch"),
        "depends_on": packet.get("depends_on") if isinstance(packet.get("depends_on"), list) else [],
        "pr_number": pr_number,
        "pr_url": github.get("pr_url") or github.get("draft_pr_url"),
        "merged_at": github.get("merged_at"),
        "thread_id": (meta.get("discord") or {}).get("thread_id") if isinstance(meta.get("discord"), dict) else None,
        "state_reason": meta.get("state_reason") or "",
    }


def should_display_child(row: dict[str, Any]) -> bool:
    if row.get("pr_number") or row.get("pr_url"):
        return True
    if row.get("state") not in {"CANCELLED", "parked"}:
        return True
    return False


def summarize_child_progress(children: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    total = len(children)
    terminal = [(d, m) for d, m in children if is_terminal_task(m)]
    active = [(d, m) for d, m in children if not is_terminal_task(m)]
    waiting = [(d, m) for d, m in active if m.get("awaiting_operator") or str(m.get("state") or "") == "QUESTION"]
    handed = [(d, m) for d, m in active if is_handed_to_pr_status(m)]
    build_owned = [(d, m) for d, m in active if not is_handed_to_pr_status(m)]
    rows = [child_workflow_row(d, m) for d, m in children]
    visible_rows = [row for row in rows if should_display_child(row)]
    hidden_cancelled = len([row for row in rows if row not in visible_rows and row.get("state") in {"CANCELLED", "parked"}])
    return {
        "total": total,
        "terminal_count": len(terminal),
        "active_count": len(active),
        "waiting_count": len(waiting),
        "handoff_count": len(handed),
        "build_owned_count": len(build_owned),
        "active": active,
        "waiting": waiting,
        "handed": handed,
        "terminal": terminal,
        "workflow_map": visible_rows,
        "hidden_cancelled_count": hidden_cancelled,
    }


def parent_reason(progress: dict[str, Any]) -> str:
    if progress["total"] == 0:
        return "no child tasks found; inspect plan decomposition"
    if progress["active_count"] == 0:
        return f"all {progress['total']} child task(s) are terminal"
    if progress["waiting_count"]:
        task_dir, meta = progress["waiting"][0]
        return f"waiting on child task {meta.get('task_id') or task_dir.name} ({meta.get('state')})"
    if progress["handoff_count"] == progress["active_count"]:
        return f"all active child task(s) are handed to PR-status"
    task_dir, meta = progress["active"][0]
    return f"active child task {meta.get('task_id') or task_dir.name} is {meta.get('state')}: {meta.get('state_reason') or 'in progress'}"


def update_plan_index(repo_root: Path, plan_id: str, meta: dict[str, Any], plan_dir: Path, now: str) -> None:
    index_path = repo_root / ".automation" / "plans-index.json"
    index = read_json(index_path, {"plans": {}})
    if not isinstance(index, dict):
        index = {"plans": {}}
    plans_raw = index.setdefault("plans", {})
    plans: dict[str, Any] = plans_raw if isinstance(plans_raw, dict) else {}
    index["plans"] = plans
    entry_raw = plans.get(plan_id)
    entry: dict[str, Any] = entry_raw if isinstance(entry_raw, dict) else {"plan_id": plan_id}
    discord_raw = meta.get("discord")
    discord: dict[str, Any] = discord_raw if isinstance(discord_raw, dict) else {}
    entry.update({
        "plan_id": plan_id,
        "title": meta.get("title"),
        "state": meta.get("state"),
        "repo": meta.get("repo"),
        "base_branch": meta.get("base_branch"),
        "thread_id": discord.get("thread_id"),
        "plan_dir": str(plan_dir),
        "updated_at": meta.get("updated_at") or now,
        "state_reason": meta.get("state_reason"),
        "child_progress": meta.get("child_progress"),
    })
    plans[plan_id] = entry
    index["updated_at"] = now
    write_json(index_path, index)


def reconcile_plan_progress(repo_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    now = utc_now()
    by_plan = load_tasks_by_plan(repo_root)
    actions: list[dict[str, Any]] = []
    plan_summaries: list[dict[str, Any]] = []

    # First reconcile task-level readiness gates.
    for children in by_plan.values():
        for task_dir, meta in children:
            action = mark_decision_task_ready(task_dir, meta, now, dry_run=dry_run)
            if action:
                actions.append(action)
            action = normalize_builder_ready_task(task_dir, meta, now, dry_run=dry_run)
            if action:
                actions.append(action)

    # Re-load tasks after task mutations so parent summaries are current.
    by_plan = load_tasks_by_plan(repo_root)

    for meta_path in plan_meta_paths(repo_root):
        plan_dir = meta_path.parent
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        plan_id = str(meta.get("plan_id") or plan_dir.name)
        if str(meta.get("state") or "") in TERMINAL_PLAN_STATES:
            continue
        children = by_plan.get(plan_id, [])
        if not children:
            continue
        progress = summarize_child_progress(children)
        reason = parent_reason(progress)
        desired_state = "DONE" if progress["total"] and progress["active_count"] == 0 else "EXECUTING"
        latest_child_progress = {
            "total": progress["total"],
            "terminal_count": progress["terminal_count"],
            "active_count": progress["active_count"],
            "waiting_count": progress["waiting_count"],
            "handoff_count": progress["handoff_count"],
            "build_owned_count": progress["build_owned_count"],
            "workflow_map": progress["workflow_map"],
            "hidden_cancelled_count": progress["hidden_cancelled_count"],
        }
        if meta.get("state") != desired_state or meta.get("state_reason") != reason or meta.get("child_progress") != latest_child_progress:
            actions.append({
                "action": "would_update_parent_plan" if dry_run else "updated_parent_plan",
                "plan_id": plan_id,
                "from_state": meta.get("state"),
                "to_state": desired_state,
                "reason": reason,
            })
            if not dry_run:
                meta["state"] = desired_state
                meta["awaiting_operator"] = False
                meta["state_reason"] = reason
                meta["child_progress"] = latest_child_progress
                meta["updated_at"] = now
                write_json(meta_path, meta)
        if not dry_run:
            update_plan_index(repo_root, plan_id, meta, plan_dir, now)
        plan_summaries.append({
            "plan_id": plan_id,
            "state": desired_state,
            "reason": reason,
            "child_progress": {
                "total": progress["total"],
                "terminal_count": progress["terminal_count"],
                "active_count": progress["active_count"],
                "waiting_count": progress["waiting_count"],
                "handoff_count": progress["handoff_count"],
                "build_owned_count": progress["build_owned_count"],
                "workflow_map": progress["workflow_map"],
                "hidden_cancelled_count": progress["hidden_cancelled_count"],
            },
        })

    payload = {
        "kind": "PLAN-PROGRESS-RECONCILER",
        "checked_at": now,
        "dry_run": dry_run,
        "action_count": len(actions),
        "actions": actions,
        "plans": plan_summaries,
    }
    if not dry_run:
        write_json(repo_root / ".automation" / "status" / "plan-progress-reconciler-last.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = reconcile_plan_progress(Path.cwd(), dry_run=args.dry_run)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
