#!/usr/bin/env python3
"""Automatically publish readiness-passed built tasks as ready-for-review PRs when configured.

Fail-closed: without .automation/publish-config.json enabled=true, this records a
BLOCKED status rather than pushing/opening a PR. This makes the source of truth
explicit while preserving the no-merge contract.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json


def load_publish_draft_pr():
    script_path = Path(__file__).resolve().parent / "publish-draft-pr.py"
    spec = importlib.util.spec_from_file_location("publish_draft_pr_script", script_path)
    if not spec or not spec.loader:
        raise RuntimeError("failed to load publish-draft-pr.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish_config(repo_root: Path) -> dict[str, Any]:
    cfg = read_json(repo_root / ".automation" / "publish-config.json", {})
    return cfg if isinstance(cfg, dict) else {}


def eligible_built_task(repo_root: Path) -> tuple[Path, dict[str, Any]] | None:
    for meta_path in sorted((repo_root / "tasks").glob("*/meta.json")):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        if meta.get("awaiting_operator"):
            continue
        github_raw = meta.get("github")
        github: dict[str, Any] = github_raw if isinstance(github_raw, dict) else {}
        if github.get("draft_pr_url") or github.get("pr_url"):
            continue
        build_raw = meta.get("build")
        build: dict[str, Any] = build_raw if isinstance(build_raw, dict) else {}
        dispatch_raw = meta.get("dispatch")
        dispatch: dict[str, Any] = dispatch_raw if isinstance(dispatch_raw, dict) else {}
        if dispatch.get("worktree") and build.get("readiness_job_id") and str(meta.get("state") or "") in {"VERIFYING", "PR_READY"}:
            return meta_path.parent, meta
    return None


def auto_publish(repo_root: Path, *, execute: bool = False) -> dict[str, Any]:
    selected = eligible_built_task(repo_root)
    if not selected:
        return {"kind": "AUTO-PUBLISH", "decision": "IDLE", "reason": "no built task with readiness evidence is eligible for PR publishing"}
    task_dir, meta = selected
    task_id = str(meta.get("task_id") or task_dir.name)
    cfg = publish_config(repo_root)
    if not cfg.get("enabled"):
        return {
            "kind": "AUTO-PUBLISH",
            "decision": "BLOCKED",
            "task_id": task_id,
            "reason": "PR publishing is not configured; set .automation/publish-config.json enabled=true with repo/push_remote/head as needed",
        }
    publisher = load_publish_draft_pr()
    return publisher.publish_draft_pr(
        repo_root,
        task_id=task_id,
        execute=execute,
        repo=cfg.get("repo"),
        title=cfg.get("title"),
        push_remote=str(cfg.get("push_remote") or "origin"),
        head=cfg.get("head"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    payload = auto_publish(Path.cwd(), execute=args.execute)
    write_json(Path.cwd() / ".automation" / "status" / "auto-publish-runner-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
