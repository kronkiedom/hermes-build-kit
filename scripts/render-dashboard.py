#!/usr/bin/env python3
"""Render a simple operator-facing dashboard from durable task/backlog/status files.

Portable starter script: adapt paths and state schema to the target repo.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        return {"error": f"failed to parse {path}: {exc}"}


def dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def collect_active_tasks(tasks_root: Path):
    active = []
    if not tasks_root.exists():
        return active
    for meta_path in sorted(tasks_root.glob("*/meta.json")):
        data = load_json(meta_path, {})
        state = data.get("state")
        if state and state not in {"DONE", "CANCELLED", "parked"}:
            active.append({
                "task_id": data.get("task_id", meta_path.parent.name),
                "state": state,
                "awaiting_operator": data.get("awaiting_operator", False),
            })
    return active


def is_handed_off_to_pr_status(task: dict):
    """Return true once build-control has published a PR for PR-status to own."""
    github = dict_or_empty(task.get("github"))
    return bool(github.get("draft_pr_url") or github.get("pr_url"))


def collect_build_control_items(tasks_root: Path, stall_status: dict):
    """Collect tasks still owned by build-control before PR-status handoff."""
    items = []
    if not tasks_root.exists():
        return items
    stalls_by_task = {
        stall.get("task_id"): stall
        for stall in stall_status.get("stalls", [])
        if isinstance(stall, dict) and stall.get("kind") == "stale_task"
    } if isinstance(stall_status, dict) else {}
    for meta_path in sorted(tasks_root.glob("*/meta.json")):
        data = load_json(meta_path, {})
        state = data.get("state")
        if not state or state in {"DONE", "CANCELLED", "parked"}:
            continue
        if is_handed_off_to_pr_status(data):
            continue
        task_id = data.get("task_id", meta_path.parent.name)
        packet = dict_or_empty(data.get("pr_packet"))
        stall = stalls_by_task.get(task_id)
        items.append({
            "task_id": task_id,
            "title": packet.get("title"),
            "branch": packet.get("branch"),
            "state": state,
            "awaiting_operator": data.get("awaiting_operator", False),
            "handoff_state": "BUILD_CONTROL",
            "stalled": bool(stall),
            "stall_reason": stall.get("reason") if stall else None,
            "stall_severity": stall.get("severity") if stall else None,
        })
    return items


def main():
    repo_root = Path.cwd()
    automation_root = repo_root / ".automation"
    status_root = automation_root / "status"
    tasks_root = repo_root / "tasks"
    backlog_root = repo_root / ".backlog"

    status_root.mkdir(parents=True, exist_ok=True)

    active_tasks = collect_active_tasks(tasks_root)
    backlog_candidates = sorted(p.name for p in backlog_root.glob("candidate-*.md")) if backlog_root.exists() else []
    plans_index_raw = load_json(automation_root / "plans-index.json", {"plans": {}})
    plans = plans_index_raw.get("plans", {}) if isinstance(plans_index_raw, dict) else {}
    active_plans = [
        plan for plan in plans.values()
        if isinstance(plan, dict) and plan.get("state") not in {"DONE", "CANCELLED"}
    ]
    routing = load_json(automation_root / "discord-routing.json", {})
    autonomy = load_json(automation_root / "AUTONOMY.json", {})
    stall_status = load_json(status_root / "stall-detector-last.json", {})
    build_control_items = collect_build_control_items(tasks_root, stall_status)

    dashboard = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(repo_root),
        "active_task_count": len(active_tasks),
        "active_tasks": active_tasks,
        "active_plan_count": len(active_plans),
        "active_plans": active_plans,
        "backlog_candidate_count": len(backlog_candidates),
        "discord_routing": routing,
        "autonomy": autonomy,
        "stall_detector": stall_status,
        "build_control_item_count": len(build_control_items),
        "build_control_items": build_control_items,
    }

    md_lines = [
        "# automation dashboard",
        "",
        f"- generated_at: `{dashboard['generated_at']}`",
        f"- repo_root: `{dashboard['repo_root']}`",
        "",
        "## Active tasks",
    ]
    if active_tasks:
        for task in active_tasks:
            md_lines.append(f"- `{task['task_id']}` — state `{task['state']}`, awaiting_operator `{task['awaiting_operator']}`")
    else:
        md_lines.append("- none")
    md_lines.extend([
        "",
        "## Plans",
    ])
    if active_plans:
        for plan in active_plans:
            md_lines.append(
                f"- `{plan.get('plan_id')}` — state `{plan.get('state')}`, "
                f"thread `{plan.get('thread_id')}`"
            )
    else:
        md_lines.append("- none")
    md_lines.extend([
        "",
        "## Build-control owned items",
    ])
    if build_control_items:
        for item in build_control_items:
            stalled = " stalled" if item.get("stalled") else ""
            branch = f", branch `{item.get('branch')}`" if item.get("branch") else ""
            md_lines.append(
                f"- `{item['task_id']}` — state `{item['state']}`{branch}, "
                f"awaiting_operator `{item['awaiting_operator']}`{stalled}"
            )
    else:
        md_lines.append("- none")
    md_lines.extend([
        "",
        "## Backlog",
        f"- candidate_count: `{len(backlog_candidates)}`",
        "",
        "## Stall detector",
        f"- decision: `{stall_status.get('decision')}`",
        f"- stall_count: `{stall_status.get('stall_count')}`",
        "",
        "## Discord routing",
        f"- build_control_channel_id: `{routing.get('build_control_channel_id')}`",
        f"- general_channel_id: `{routing.get('general_channel_id')}`",
        "",
        "## Autonomy",
        f"- enabled: `{autonomy.get('enabled')}`",
        f"- phase: `{autonomy.get('phase')}`",
    ])

    (status_root / "dashboard.json").write_text(json.dumps(dashboard, indent=2) + "\n")
    (status_root / "dashboard.md").write_text("\n".join(md_lines) + "\n")
    print(json.dumps({
        "dashboard_json": str(status_root / "dashboard.json"),
        "dashboard_md": str(status_root / "dashboard.md"),
        "active_task_count": len(active_tasks),
        "build_control_item_count": len(build_control_items),
        "backlog_candidate_count": len(backlog_candidates),
    }, indent=2))


if __name__ == "__main__":
    main()
