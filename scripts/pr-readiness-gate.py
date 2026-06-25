#!/usr/bin/env python3
"""Queue or resolve a PR-readiness gate job for one commit SHA."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from pr_readiness_lib import create_readiness_job, load_readiness_job, mark_readiness_result, readiness_blocks


def current_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def current_branch() -> str:
    result = subprocess.run(["git", "branch", "--show-current"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip() or "DETACHED"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    queue = sub.add_parser("queue", help="create a readiness job for the current or supplied SHA")
    queue.add_argument("--task-id", required=True)
    queue.add_argument("--branch", default=None)
    queue.add_argument("--sha", default=None)
    queue.add_argument("--pr-url", default=None)

    result = sub.add_parser("result", help="record gate result")
    result.add_argument("--job-id", required=True)
    result.add_argument("--passed", action="store_true")
    result.add_argument("--issues-json", default="[]")
    result.add_argument("--evidence-json", default=None, help="Optional structured evidence, including review_cleanup findings/critics for re-review claims")

    check = sub.add_parser("check", help="check whether ready-for-review is blocked")
    check.add_argument("--job-id", required=True)
    check.add_argument("--current-sha", default=None)

    args = parser.parse_args()
    repo_root = Path.cwd()
    if args.command == "queue":
        payload = create_readiness_job(
            repo_root,
            task_id=args.task_id,
            branch=args.branch or current_branch(),
            sha=args.sha or current_sha(),
            pr_url=args.pr_url,
        )
    elif args.command == "result":
        payload = mark_readiness_result(
            repo_root,
            args.job_id,
            passed=args.passed,
            issues=json.loads(args.issues_json),
            evidence=json.loads(args.evidence_json) if args.evidence_json else None,
        )
    else:
        job = load_readiness_job(repo_root, args.job_id)
        payload = readiness_blocks(job, current_sha=args.current_sha or current_sha(), explain=True)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
