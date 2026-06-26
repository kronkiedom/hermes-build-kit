#!/usr/bin/env python3
"""Run configured PR-readiness audits for queued build-control jobs.

The runner is fail-closed. It never invents a readiness pass: a configured verifier
command must return structured JSON with `passed`, `issues`, and optional
`evidence`. Without a configured verifier it records BLOCKED/IDLE and leaves the
job queued.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json
from pr_readiness_lib import load_readiness_job, mark_readiness_result

ELIGIBLE_STATES = {"VERIFYING", "PR_READY"}
TERMINAL_STATES = {"DONE", "CANCELLED", "SUPERSEDED", "PR_DRAFT"}


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, timeout: int = 1800) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=False, timeout=timeout)
    return {"cmd": cmd, "returncode": result.returncode, "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:]}


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def configured_verifier_command(repo_root: Path) -> str:
    if os.environ.get("HERMES_READINESS_COMMAND"):
        return str(os.environ["HERMES_READINESS_COMMAND"])
    cfg = read_json(repo_root / ".automation" / "readiness-config.json", {})
    if isinstance(cfg, dict) and cfg.get("enabled") and cfg.get("verifier_command"):
        return str(cfg["verifier_command"])
    return ""


def current_sha(worktree: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=60)
    if result["returncode"] != 0:
        raise RuntimeError(json.dumps(result, indent=2))
    return str(result["stdout"]).strip()


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def eligible_task(repo_root: Path, task_id: str | None = None) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    for meta_path in task_meta_paths(repo_root):
        task_dir = meta_path.parent
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        selected_task_id = str(meta.get("task_id") or task_dir.name)
        if task_id and selected_task_id != task_id:
            continue
        state = str(meta.get("state") or "")
        if state in TERMINAL_STATES or state not in ELIGIBLE_STATES:
            continue
        if meta.get("awaiting_operator"):
            continue
        if dict_or_empty(meta.get("github")).get("draft_pr_url") or dict_or_empty(meta.get("github")).get("pr_url"):
            continue
        build = dict_or_empty(meta.get("build"))
        readiness_job_id = str(build.get("readiness_job_id") or "")
        if not readiness_job_id:
            continue
        try:
            job = load_readiness_job(repo_root, readiness_job_id)
        except FileNotFoundError:
            continue
        if job.get("state") != "READINESS_QUEUED":
            continue
        return task_dir, meta, job
    return None


def parse_verifier_result(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    candidates = [text, *[line.strip() for line in text.splitlines() if line.strip()]]
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def format_readiness_feedback(marked_job: dict[str, Any]) -> str:
    issues_raw = marked_job.get("issues")
    issues: list[Any] = issues_raw if isinstance(issues_raw, list) else []
    lines = [
        "## Readiness feedback — continue building",
        "",
        f"Readiness job `{marked_job.get('job_id')}` failed for SHA `{marked_job.get('sha')}`.",
        "Address every blocking issue below before the next PR-ready attempt.",
        "",
    ]
    if not issues:
        lines.append("- No structured issues were returned; inspect readiness command output.")
    for idx, issue in enumerate(issues, start=1):
        if not isinstance(issue, dict):
            lines.append(f"{idx}. Invalid issue payload: `{issue}`")
            continue
        lines.extend([
            f"{idx}. **{issue.get('severity') or 'P1'} {issue.get('kind') or 'readiness_issue'}** — {issue.get('message') or 'No message provided.'}",
            f"   - Evidence: {issue.get('evidence') or issue.get('command_output_path') or 'not provided'}",
        ])
    return "\n".join(lines).strip() + "\n"


def write_readiness_feedback(task_dir: Path, marked_job: dict[str, Any]) -> None:
    feedback = format_readiness_feedback(marked_job)
    (task_dir / "readiness-feedback.md").write_text(feedback, encoding="utf-8")
    prompt_path = task_dir / "builder-prompt.md"
    existing = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    marker = "\n## Readiness feedback — continue building\n"
    base = existing.split(marker, 1)[0].rstrip() if marker in existing else existing.rstrip()
    prompt_path.write_text(f"{base}\n\n{feedback}", encoding="utf-8")


def update_task(task_dir: Path, meta: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    updated = {**meta, **updates, "updated_at": utc_now()}
    write_json(task_dir / "meta.json", updated)
    return updated


def run_readiness(repo_root: Path, *, execute: bool = False, task_id: str | None = None, timeout_seconds: int = 1800) -> dict[str, Any]:
    selected = eligible_task(repo_root, task_id)
    if not selected:
        return {"kind": "READINESS-RUNNER", "decision": "IDLE", "reason": "no queued readiness job is eligible"}
    task_dir, meta, job = selected
    selected_task_id = str(meta.get("task_id") or task_dir.name)
    build = dict_or_empty(meta.get("build"))
    dispatch = dict_or_empty(meta.get("dispatch"))
    worktree = Path(str(dispatch.get("worktree") or "")).expanduser()
    if not worktree.exists():
        return {"kind": "READINESS-RUNNER", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "dispatch worktree does not exist", "worktree": str(worktree)}
    head_sha = current_sha(worktree)
    if str(job.get("sha") or "") != head_sha:
        return {
            "kind": "READINESS-RUNNER",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "reason": "readiness job SHA is stale for current worktree HEAD",
            "job_id": job.get("job_id"),
            "job_sha": job.get("sha"),
            "current_sha": head_sha,
        }
    command = configured_verifier_command(repo_root)
    if not command:
        return {
            "kind": "READINESS-RUNNER",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "job_id": job.get("job_id"),
            "reason": "readiness verifier is not configured; set .automation/readiness-config.json enabled=true with verifier_command or HERMES_READINESS_COMMAND",
        }
    if not execute:
        return {"kind": "READINESS-RUNNER", "decision": "WOULD_RUN", "task_id": selected_task_id, "job_id": job.get("job_id"), "verifier_configured": True}

    env = os.environ.copy()
    env.update({
        "READINESS_JOB_ID": str(job.get("job_id") or ""),
        "READINESS_TASK_ID": selected_task_id,
        "READINESS_SHA": head_sha,
        "BUILD_TASK_DIR": str(task_dir),
        "BUILD_WORKTREE": str(worktree),
        "BUILD_EVIDENCE_PATH": str(build.get("evidence_path") or task_dir / "build-evidence.json"),
    })
    command_result = run(["bash", "-lc", command], cwd=worktree, env=env, timeout=timeout_seconds)
    parsed = parse_verifier_result(str(command_result.get("stdout") or ""))
    command_output_path = task_dir / "readiness-command-output.json"
    write_json(command_output_path, {"timestamp": utc_now(), "job_id": job.get("job_id"), "command": command, "result": command_result, "parsed": parsed})

    if parsed is None or not isinstance(parsed.get("passed"), bool):
        reason = "readiness verifier did not return structured JSON with boolean `passed`; manual attention required"
        issue = {"kind": "invalid_readiness_verifier_output", "severity": "P1", "message": reason, "command_output_path": str(command_output_path)}
        marked = mark_readiness_result(repo_root, str(job["job_id"]), passed=False, issues=[issue], evidence={"command_output_path": str(command_output_path)})
        write_readiness_feedback(task_dir, marked)
        update_task(task_dir, meta, {"awaiting_operator": True, "state": "VERIFYING", "state_reason": reason})
        return {"kind": "READINESS-RUNNER", "decision": "BLOCKED", "task_id": selected_task_id, "job_id": job.get("job_id"), "reason": reason, "readiness": {"state": marked.get("state"), "passed": marked.get("passed")}}

    issues_raw = parsed.get("issues", [])
    issues = issues_raw if isinstance(issues_raw, list) else [{"kind": "invalid_issues", "severity": "P1", "message": "verifier returned non-list issues"}]
    evidence_raw = parsed.get("evidence", {})
    evidence = evidence_raw if isinstance(evidence_raw, dict) else {"raw_evidence": evidence_raw}
    evidence = {**evidence, "command_output_path": str(command_output_path), "verifier_command": command}
    if command_result["returncode"] != 0 and parsed.get("passed"):
        issues.append({"kind": "verifier_command_failed", "severity": "P1", "message": "verifier exited nonzero while claiming pass", "returncode": command_result["returncode"]})
    marked = mark_readiness_result(repo_root, str(job["job_id"]), passed=bool(parsed.get("passed")), issues=issues, evidence=evidence)
    passed = bool(marked.get("passed")) and marked.get("state") == "PR_READY"
    if passed:
        update_task(task_dir, meta, {
            "state": "PR_READY",
            "phase_status": {**dict_or_empty(meta.get("phase_status")), "VERIFY": "PASSED"},
            "awaiting_operator": False,
            "state_reason": "readiness audit passed for current SHA; draft PR publishing may proceed",
        })
        decision = "PR_READY"
    else:
        write_readiness_feedback(task_dir, marked)
        update_task(task_dir, meta, {
            "state": "READY_FOR_BUILDER",
            "phase_status": {**dict_or_empty(meta.get("phase_status")), "VERIFY": "FAILED", "EXECUTE": "READY_FOR_BUILDER"},
            "awaiting_operator": False,
            "state_reason": "readiness audit failed; builder should address blocking issues",
        })
        decision = "READINESS_FAILED"
    return {
        "kind": "READINESS-RUNNER",
        "decision": decision,
        "task_id": selected_task_id,
        "job_id": job.get("job_id"),
        "readiness_state": marked.get("state"),
        "passed": marked.get("passed"),
        "issue_count": len(marked.get("issues") or []),
        "command_output_path": str(command_output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    payload = run_readiness(Path.cwd(), execute=args.execute, task_id=args.task_id, timeout_seconds=args.timeout_seconds)
    write_json(Path.cwd() / ".automation" / "status" / "readiness-runner-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
