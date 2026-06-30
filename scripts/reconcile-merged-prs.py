#!/usr/bin/env python3
"""Reconcile merged GitHub PRs back into build-control task state.

PR-status monitors open PRs. Once a PR merges, it disappears from that monitor;
this worker closes the loop by scanning durable task packets that reference PR
numbers, querying those PRs by number, marking merged PR tasks DONE, and flagging
now-unblocked dependent decision/build packets.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from plan_automation_lib import read_json, utc_now, write_json, write_text

TERMINAL_STATES = {"DONE", "CANCELLED"}


def run_gh_json(args: list[str]) -> Any:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout or "null")


def fetch_pr(number: int, repo: str | None = None) -> dict[str, Any]:
    args = ["pr", "view", str(number), "--json", "number,state,url,mergedAt,headRefOid,baseRefName,title"]
    if repo:
        args.extend(["--repo", repo])
    return run_gh_json(args)


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def load_task_meta(meta_path: Path) -> dict[str, Any]:
    meta = read_json(meta_path, {})
    return meta if isinstance(meta, dict) else {}


def task_packet(meta: dict[str, Any]) -> dict[str, Any]:
    packet = meta.get("pr_packet")
    return packet if isinstance(packet, dict) else {}


def task_github(meta: dict[str, Any]) -> dict[str, Any]:
    github = meta.get("github")
    return github if isinstance(github, dict) else {}


def packet_id(meta: dict[str, Any]) -> str:
    packet = task_packet(meta)
    return str(packet.get("packet_id") or meta.get("task_id") or "")


def packet_pr_number(meta: dict[str, Any]) -> int | None:
    packet = task_packet(meta)
    github = task_github(meta)
    raw = packet.get("pr_number") or github.get("pr_number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    pr_url = str(github.get("pr_url") or github.get("draft_pr_url") or "")
    match = re.search(r"/pull/(\d+)(?:\b|$)", pr_url)
    if match:
        # Root cause: PR-status handoff tasks may only store github.pr_url, so
        # merged-PR reconciliation must recover the PR number from that URL.
        return int(match.group(1))
    return None


def packet_depends_on(meta: dict[str, Any]) -> list[str]:
    packet = task_packet(meta)
    depends = packet.get("depends_on")
    if isinstance(depends, list):
        return [str(item) for item in depends]
    return []


def github_repo_for_task(meta: dict[str, Any], default_repo: str | None) -> str | None:
    packet = task_packet(meta)
    github = task_github(meta)
    return str(packet.get("repo") or github.get("repo") or default_repo or "") or None


def append_checkpoint(task_dir: Path, line: str) -> None:
    path = task_dir / "checkpoints.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# checkpoints\n\n"
    write_text(path, existing.rstrip() + "\n" + line.rstrip() + "\n")


def reconcile_merged_prs(
    repo_root: Path,
    *,
    default_repo: str | None = None,
    fetch_pr: Callable[[int, str | None], dict[str, Any]] = fetch_pr,
    dry_run: bool = False,
) -> dict[str, Any]:
    now = utc_now()
    tasks: list[tuple[Path, dict[str, Any]]] = [(p.parent, load_task_meta(p)) for p in task_meta_paths(repo_root)]
    by_packet = {packet_id(meta): (task_dir, meta) for task_dir, meta in tasks if packet_id(meta)}
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    merged_packet_ids: set[str] = set()

    for task_dir, meta in tasks:
        if meta.get("state") in TERMINAL_STATES:
            if meta.get("state") == "DONE":
                merged_packet_ids.add(packet_id(meta))
            continue
        number = packet_pr_number(meta)
        if number is None:
            continue
        repo = github_repo_for_task(meta, default_repo)
        try:
            pr = fetch_pr(number, repo)
        except Exception as exc:
            errors.append({"task_id": meta.get("task_id") or task_dir.name, "pr_number": number, "error": str(exc)})
            continue
        state = str(pr.get("state") or "").upper()
        if state != "MERGED":
            actions.append({"action": "pr_not_merged", "task_id": meta.get("task_id") or task_dir.name, "pr_number": number, "state": state})
            continue
        action = {
            "action": "would_mark_done" if dry_run else "marked_done",
            "task_id": meta.get("task_id") or task_dir.name,
            "packet_id": packet_id(meta),
            "pr_number": number,
            "merged_at": pr.get("mergedAt"),
            "url": pr.get("url"),
            "head_sha": pr.get("headRefOid"),
        }
        actions.append(action)
        merged_packet_ids.add(packet_id(meta))
        if not dry_run:
            github = meta.get("github") if isinstance(meta.get("github"), dict) else {}
            github.update({
                "pr_number": number,
                "pr_url": pr.get("url"),
                "merged_at": pr.get("mergedAt"),
                "head_sha": pr.get("headRefOid"),
                "base_ref": pr.get("baseRefName"),
            })
            meta["github"] = github
            meta["state"] = "DONE"
            meta["awaiting_operator"] = False
            meta["state_reason"] = f"PR #{number} merged; build-control task reconciled from GitHub"
            meta["completed_at"] = now
            meta["updated_at"] = now
            write_json(task_dir / "meta.json", meta)
            append_checkpoint(task_dir, f"- {now} — PR #{number} merged ({pr.get('url')}); task marked DONE")

    # Re-read after writes so dependency checks use latest state.
    if not dry_run:
        tasks = [(p.parent, load_task_meta(p)) for p in task_meta_paths(repo_root)]
        by_packet = {packet_id(meta): (task_dir, meta) for task_dir, meta in tasks if packet_id(meta)}
    done_packet_ids = {pid for pid, (_, meta) in by_packet.items() if meta.get("state") == "DONE"}

    unblocked: list[dict[str, Any]] = []
    for task_dir, meta in tasks:
        if meta.get("state") in TERMINAL_STATES:
            continue
        deps = packet_depends_on(meta)
        if not deps or not all(dep in done_packet_ids for dep in deps):
            continue
        if meta.get("dependencies_cleared") is True:
            continue
        action = {
            "action": "would_mark_dependencies_cleared" if dry_run else "marked_dependencies_cleared",
            "task_id": meta.get("task_id") or task_dir.name,
            "packet_id": packet_id(meta),
            "depends_on": deps,
            "state": meta.get("state"),
        }
        actions.append(action)
        unblocked.append(action)
        if not dry_run:
            meta["dependencies_cleared"] = True
            meta["dependencies_cleared_at"] = now
            if meta.get("state") == "QUESTION" or meta.get("awaiting_operator"):
                meta["state"] = "QUESTION"
                meta["awaiting_operator"] = True
                meta["state_reason"] = "dependencies cleared; operator decision is now unblocked"
            else:
                meta["state_reason"] = "dependencies cleared; ready for next build-control action"
            meta["updated_at"] = now
            write_json(task_dir / "meta.json", meta)
            append_checkpoint(task_dir, f"- {now} — dependencies cleared: {', '.join(deps)}")

    payload = {
        "kind": "MERGED-PR-RECONCILER",
        "checked_at": now,
        "dry_run": dry_run,
        "merged_count": sum(1 for action in actions if action["action"] in {"marked_done", "would_mark_done"}),
        "unblocked_count": len(unblocked),
        "actions": actions,
        "errors": errors,
    }
    if not dry_run:
        write_json(repo_root / ".automation" / "status" / "merged-pr-reconciler-last.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="Default GitHub owner/repo for task PR numbers, e.g. armor/armor-swarm")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = reconcile_merged_prs(Path.cwd(), default_repo=args.repo, dry_run=args.dry_run)
    if args.dry_run:
        write_json(Path.cwd() / ".automation" / "status" / "merged-pr-reconciler-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
