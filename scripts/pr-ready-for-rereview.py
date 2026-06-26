#!/usr/bin/env python3
"""Post a ready-for-re-review signal only after the 5x5 cleanup gate passes."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from pr_readiness_lib import load_readiness_job, readiness_blocks


def run_json(args: list[str]) -> Any:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout or "null")


def pr_head_sha(pr: str) -> str:
    data = run_json(["pr", "view", pr, "--json", "headRefOid"])
    return str(data.get("headRefOid") or "")


def assert_rereview_ready(repo_root: Path, *, pr: str, job_id: str) -> dict[str, Any]:
    job = load_readiness_job(repo_root, job_id)
    head_sha = pr_head_sha(pr)
    gate = readiness_blocks(job, current_sha=head_sha, explain=True)
    assert isinstance(gate, dict)
    if gate["blocked"]:
        return {
            "ready": False,
            "pr": pr,
            "job_id": job_id,
            "head_sha": head_sha,
            "gate": gate,
            "message": "ready-for-re-review is blocked until the 5x5 review-cleanup gate passes for the current head SHA",
        }
    return {"ready": True, "pr": pr, "job_id": job_id, "head_sha": head_sha, "gate": gate}


def post_comment(pr: str, body: str) -> str:
    result = subprocess.run(["gh", "pr", "comment", pr, "--body", body], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def request_reviewer(pr: str, reviewer: str) -> None:
    result = subprocess.run(["gh", "pr", "edit", pr, "--add-reviewer", reviewer], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, help="PR number or selector accepted by gh")
    parser.add_argument("--job-id", required=True, help="PR-readiness job that passed the review-cleanup gate")
    parser.add_argument("--body-file", required=True, help="Ready-for-re-review comment body")
    parser.add_argument("--reviewer", default=None, help="Optional reviewer to request after posting the gated comment")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path.cwd()
    gate = assert_rereview_ready(repo_root, pr=args.pr, job_id=args.job_id)
    if not gate["ready"]:
        print(json.dumps(gate, indent=2, sort_keys=True))
        raise SystemExit(2)

    body = Path(args.body_file).read_text(encoding="utf-8")
    if args.dry_run:
        payload = {**gate, "dry_run": True, "would_comment": body[:500], "would_request_reviewer": args.reviewer}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    comment_url = post_comment(args.pr, body)
    if args.reviewer:
        request_reviewer(args.pr, args.reviewer)
    print(json.dumps({**gate, "comment_url": comment_url, "requested_reviewer": args.reviewer}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
