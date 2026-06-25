#!/usr/bin/env python3
"""Operator-authorized helper for PR rebase autocure.

This script prepares or performs a rebase for a pull request branch. It is
non-destructive by default: without --authorized-push it checks out the PR in a
local worktree and reports the next commands, but does not push updates.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> dict[str, object]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    payload = {"cmd": cmd, "returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(payload, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", help="PR selector accepted by gh, e.g. owner/repo#123 or 123 in repo cwd")
    parser.add_argument("--worktree-root", default=".automation/rebase-worktrees")
    parser.add_argument("--authorized-push", action="store_true", help="Push the rebased branch; use only after explicit operator authorization")
    args = parser.parse_args()

    repo_root = Path.cwd()
    worktree_root = repo_root / args.worktree_root
    worktree_root.mkdir(parents=True, exist_ok=True)
    pr_json = run(["gh", "pr", "view", args.pr, "--json", "number,headRefName,baseRefName,headRepository,headRepositoryOwner,url"], check=True)
    pr = json.loads(str(pr_json["stdout"]))
    branch = pr["headRefName"]
    base = pr["baseRefName"]
    number = pr["number"]
    worktree = worktree_root / f"pr-{number}"
    actions = [pr_json]
    if not worktree.exists():
        actions.append(run(["git", "worktree", "add", str(worktree), "HEAD"], cwd=repo_root))
    actions.append(run(["gh", "pr", "checkout", str(number)], cwd=worktree))
    actions.append(run(["git", "fetch", "origin", base], cwd=worktree))
    rebase = run(["git", "rebase", f"origin/{base}"], cwd=worktree, check=False)
    actions.append(rebase)
    pushed = False
    if rebase["returncode"] == 0 and args.authorized_push:
        actions.append(run(["git", "push", "--force-with-lease"], cwd=worktree))
        pushed = True
    result = {
        "kind": "PR-REBASE-AUTOCURE",
        "pr": args.pr,
        "url": pr.get("url"),
        "branch": branch,
        "base": base,
        "worktree": str(worktree),
        "rebase_returncode": rebase["returncode"],
        "pushed": pushed,
        "push_policy": "requires --authorized-push after explicit operator authorization",
        "actions": actions,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if rebase["returncode"] != 0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
