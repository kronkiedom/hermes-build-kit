#!/usr/bin/env python3
"""Run a configured builder command for one dispatched PR packet.

This worker intentionally does not invent changes. It consumes the durable
`builder-prompt.md` dispatch packet, runs an operator-provided command inside the
isolated worktree, records output/evidence, commits produced changes, and queues
the SHA-scoped readiness gate for the new commit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json, write_task_readiness_artifacts, write_text
from pr_readiness_lib import create_readiness_job

BUILDABLE_STATES = {"EXECUTE", "DISPATCHED", "READY_FOR_BUILDER"}


def dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def is_builder_eligible(meta: dict[str, Any]) -> bool:
    """Return true only for states this worker can actually execute."""
    state = str(meta.get("state") or "")
    dispatch = dict_or_empty(meta.get("dispatch"))
    if meta.get("awaiting_operator"):
        return False
    if not dispatch.get("worktree"):
        return False
    return state in BUILDABLE_STATES


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = False) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    payload = {"cmd": cmd, "cwd": str(cwd) if cwd else None, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    if check and result.returncode != 0:
        raise RuntimeError(json.dumps(payload, indent=2))
    return payload


def run_shell(command: str, *, cwd: Path, env: dict[str, str], timeout_seconds: int) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        env=env,
    )
    return {"cmd": command, "cwd": str(cwd), "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def task_meta_paths(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "tasks").glob("*/meta.json"))


def load_task(repo_root: Path, task_id: str | None) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for meta_path in task_meta_paths(repo_root):
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict):
            continue
        if task_id and meta.get("task_id") != task_id and meta_path.parent.name != task_id:
            continue
        if is_builder_eligible(meta):
            candidates.append((meta_path.parent, meta))
    if task_id:
        return candidates[0] if candidates else None
    return candidates[0] if candidates else None


def git_changed_files(worktree: Path) -> list[str]:
    result = run(["git", "status", "--porcelain"], cwd=worktree, check=True)
    files: list[str] = []
    for line in str(result["stdout"]).splitlines():
        if not line.strip():
            continue
        files.append(line[3:].strip())
    return files


def commit_message_for(meta: dict[str, Any], task_id: str) -> str:
    packet = dict_or_empty(meta.get("pr_packet"))
    title = str(packet.get("title") or task_id).strip()
    return f"feat: {title}\n\nBuilds PR packet {task_id}."


def append_build_ledger(repo_root: Path, event: dict[str, Any]) -> None:
    path = repo_root / ".automation" / "build-ledger.json"
    ledger = read_json(path, {"events": []})
    ledger.setdefault("events", []).append(event)
    ledger["updated_at"] = utc_now()
    write_json(path, ledger)


def update_task_state(task_dir: Path, meta: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    updated = {**meta, **updates, "updated_at": utc_now()}
    write_json(task_dir / "meta.json", updated)
    return updated


def without_build_blocker(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in meta.items() if key != "build_blocker"}


def validate_builder_model_metadata(task_dir: Path) -> tuple[bool, dict[str, Any] | str]:
    metadata_path = task_dir / "builder-run-metadata.json"
    if not metadata_path.exists():
        return False, f"missing builder model provenance: {metadata_path}"
    try:
        metadata = read_json(metadata_path, {})
    except Exception as exc:
        return False, f"invalid builder model provenance: {exc}"
    if not isinstance(metadata, dict):
        return False, f"invalid builder model provenance payload: {metadata_path}"
    provider = str(metadata.get("provider") or "")
    model = str(metadata.get("model") or "")
    tier = str(metadata.get("required_model_tier") or "")
    role = str(metadata.get("workflow_role") or "")
    allowed = {("openai", "gpt-5.4"), ("openai-api", "gpt-5.4"), ("anthropic", "claude-sonnet-4")}
    if tier != "coding_working" or role != "builder" or (provider, model) not in allowed:
        return False, f"builder model provenance mismatch: tier={tier or 'missing'} role={role or 'missing'} provider={provider or 'missing'} model={model or 'missing'}"
    return True, metadata


def run_builder(
    repo_root: Path,
    *,
    task_id: str | None = None,
    builder_command: str | None = None,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    command = builder_command or os.environ.get("HERMES_BUILDER_COMMAND")
    if not command:
        return {
            "kind": "BUILDER-WORKER",
            "decision": "BLOCKED",
            "reason": "builder command is required",
            "required": "Pass --builder-command or set HERMES_BUILDER_COMMAND to the command that consumes builder-prompt.md.",
        }

    selected = load_task(repo_root, task_id)
    if not selected:
        return {"kind": "BUILDER-WORKER", "decision": "IDLE", "reason": "no dispatched task with a worktree is ready for builder execution"}
    task_dir, meta = selected
    selected_task_id = str(meta.get("task_id") or task_dir.name)
    dispatch = dict_or_empty(meta.get("dispatch"))
    worktree = Path(str(dispatch.get("worktree") or "")).expanduser()
    if not worktree.exists():
        return {"kind": "BUILDER-WORKER", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "dispatch worktree does not exist", "worktree": str(worktree)}

    prompt_path = task_dir / "builder-prompt.md"
    if not prompt_path.exists():
        return {"kind": "BUILDER-WORKER", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "missing builder-prompt.md", "required": "Run dispatch-pr-worker.py --execute before builder execution."}

    before_sha = str(run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True)["stdout"]).strip()
    before_status = run(["git", "status", "--porcelain"], cwd=worktree, check=True)
    env = os.environ.copy()
    env.update({
        "BUILD_TASK_ID": selected_task_id,
        "BUILD_TASK_DIR": str(task_dir),
        "BUILDER_PROMPT_PATH": str(prompt_path),
        "BUILDER_SUMMARY_PATH": str(task_dir / "builder-summary.md"),
        "BUILDER_EVIDENCE_PATH": str(task_dir / "builder-command-output.json"),
    })

    try:
        command_result = run_shell(command, cwd=worktree, env=env, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        payload = {
            "kind": "BUILDER-WORKER",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "reason": "builder command timed out",
            "timeout_seconds": timeout_seconds,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
        write_json(task_dir / "builder-command-output.json", payload)
        update_task_state(task_dir, meta, {"state": "ESCALATED", "awaiting_operator": True, "state_reason": payload["reason"], "build_blocker": payload})
        append_build_ledger(repo_root, {**payload, "timestamp": utc_now()})
        return payload

    write_json(task_dir / "builder-command-output.json", command_result)
    if command_result["returncode"] != 0:
        payload = {
            "kind": "BUILDER-WORKER",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "reason": "builder command failed",
            "returncode": command_result["returncode"],
        }
        write_text(task_dir / "builder-summary.md", f"# Builder blocked\n\n- Task: `{selected_task_id}`\n- Reason: builder command failed\n- Return code: `{command_result['returncode']}`\n")
        update_task_state(task_dir, meta, {"state": "ESCALATED", "awaiting_operator": True, "state_reason": payload["reason"], "build_blocker": payload})
        append_build_ledger(repo_root, {**payload, "timestamp": utc_now()})
        return payload

    model_ok, model_metadata = validate_builder_model_metadata(task_dir)
    if not model_ok:
        payload = {
            "kind": "BUILDER-WORKER",
            "decision": "BLOCKED",
            "task_id": selected_task_id,
            "reason": str(model_metadata),
        }
        write_text(task_dir / "builder-summary.md", f"# Builder blocked\n\n- Task: `{selected_task_id}`\n- Reason: {model_metadata}\n")
        update_task_state(task_dir, meta, {"state": "ESCALATED", "awaiting_operator": True, "state_reason": payload["reason"], "build_blocker": payload})
        append_build_ledger(repo_root, {**payload, "timestamp": utc_now()})
        return payload

    changed_files = git_changed_files(worktree)
    if not changed_files:
        previous_build = dict_or_empty(meta.get("build"))
        # Root cause: after a failed readiness pass, the next builder may have no
        # code changes left to make; that should re-run/readiness-check the
        # existing built SHA, not lock the parent plan waiting on the operator.
        if previous_build.get("commit_sha"):
            readiness = create_readiness_job(repo_root, task_id=selected_task_id, branch=str(dispatch.get("branch") or ""), sha=before_sha)
            base_branch = str(dispatch.get("base_branch") or "main")
            diff_stat = run(["git", "diff", "--stat", f"origin/{base_branch}...HEAD"], cwd=worktree, check=False)
            changed_since_base = run(["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"], cwd=worktree, check=False)
            refreshed_files = [line.strip() for line in str(changed_since_base.get("stdout") or "").splitlines() if line.strip()]
            build = {
                **previous_build,
                "commit_sha": before_sha,
                "readiness_job_id": readiness["job_id"],
                "requeued_without_changes_at": utc_now(),
                "changed_files": refreshed_files or previous_build.get("changed_files", []),
            }
            payload = {
                "kind": "BUILDER-WORKER",
                "decision": "NO_CHANGES_READINESS_REQUEUED",
                "task_id": selected_task_id,
                "reason": "builder command produced no git changes; readiness requeued for existing built SHA",
                "commit_sha": before_sha,
                "readiness_job_id": readiness["job_id"],
            }
            evidence_path = Path(str(build.get("evidence_path") or task_dir / "build-evidence.json"))
            summary_path = Path(str(build.get("summary_path") or task_dir / "builder-summary.md"))
            write_json(evidence_path, {
                "kind": "BUILDER-EVIDENCE",
                "task_id": selected_task_id,
                "before_sha": previous_build.get("commit_sha"),
                "after_sha": before_sha,
                "changed_files": build["changed_files"],
                "diff_stat": diff_stat.get("stdout") or "",
                "readiness_job_id": readiness["job_id"],
                "timestamp": utc_now(),
                "note": "evidence refreshed after no-change builder retry",
                "model_policy": model_metadata,
            })
            write_text(summary_path, f"# Builder summary\n\n- Task: `{selected_task_id}`\n- Worktree: `{worktree}`\n- Commit: `{before_sha}`\n- Changed files: `{len(build['changed_files'])}`\n- Readiness job: `{readiness['job_id']}`\n- Note: builder command produced no new git changes; evidence was refreshed for the existing built SHA.\n\n## Diff stat\n\n```text\n{diff_stat.get('stdout') or ''}```\n")
            build["evidence_path"] = str(evidence_path)
            build["summary_path"] = str(summary_path)
            state_reason = "builder produced no new code changes; readiness requeued for existing built SHA"
            write_text(task_dir / "builder-noop.md", f"# Builder no-op\n\n- Task: `{selected_task_id}`\n- Reason: builder command produced no git changes; readiness requeued for existing built SHA.\n- Commit: `{before_sha}`\n- Readiness job: `{readiness['job_id']}`\n")
            write_task_readiness_artifacts(
                task_dir,
                task_id=selected_task_id,
                worktree=worktree,
                commit_sha=before_sha,
                readiness_job_id=readiness["job_id"],
                changed_files=build["changed_files"],
                state_reason=state_reason,
                note="builder command produced no new git changes; evidence was refreshed for the existing built SHA",
            )
            update_task_state(task_dir, without_build_blocker(meta), {
                "state": "VERIFYING",
                "phase_status": {**dict_or_empty(meta.get("phase_status")), "EXECUTE": "BUILT", "VERIFY": "QUEUED"},
                "awaiting_operator": False,
                "state_reason": state_reason,
                "commit": before_sha,
                "dispatch": {**dispatch, "readiness_job_id": readiness["job_id"]},
                "build": build,
            })
            append_build_ledger(repo_root, {**payload, "timestamp": utc_now()})
            return payload
        payload = {"kind": "BUILDER-WORKER", "decision": "BLOCKED", "task_id": selected_task_id, "reason": "builder command produced no git changes", "before_sha": before_sha}
        write_text(task_dir / "builder-summary.md", f"# Builder blocked\n\n- Task: `{selected_task_id}`\n- Reason: builder command produced no git changes\n")
        update_task_state(task_dir, meta, {"state": "ESCALATED", "awaiting_operator": True, "state_reason": payload["reason"], "build_blocker": payload})
        append_build_ledger(repo_root, {**payload, "timestamp": utc_now()})
        return payload

    run(["git", "add", "--", *changed_files], cwd=worktree, check=True)
    message = commit_message_for(meta, selected_task_id)
    commit = run(["git", "commit", "-m", message], cwd=worktree, check=True)
    after_sha = str(run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True)["stdout"]).strip()
    diff_stat = run(["git", "diff", "--stat", f"{before_sha}..{after_sha}"], cwd=worktree, check=True)
    readiness = create_readiness_job(repo_root, task_id=selected_task_id, branch=str(dispatch.get("branch") or ""), sha=after_sha)

    evidence = {
        "kind": "BUILDER-EVIDENCE",
        "task_id": selected_task_id,
        "builder_command": command,
        "before_sha": before_sha,
        "after_sha": after_sha,
        "changed_files": changed_files,
        "git_status_before": before_status["stdout"],
        "commit_stdout": commit["stdout"],
        "commit_stderr": commit["stderr"],
        "diff_stat": diff_stat["stdout"],
        "command_output_path": str(task_dir / "builder-command-output.json"),
        "readiness_job_id": readiness["job_id"],
        "timestamp": utc_now(),
        "model_policy": model_metadata,
    }
    write_json(task_dir / "build-evidence.json", evidence)
    write_text(
        task_dir / "builder-summary.md",
        f"# Builder summary\n\n- Task: `{selected_task_id}`\n- Worktree: `{worktree}`\n- Commit: `{after_sha}`\n- Changed files: `{len(changed_files)}`\n- Readiness job: `{readiness['job_id']}`\n\n## Diff stat\n\n```text\n{diff_stat['stdout']}```\n",
    )
    state_reason = "builder command produced a commit; readiness audit queued"
    write_task_readiness_artifacts(
        task_dir,
        task_id=selected_task_id,
        worktree=worktree,
        commit_sha=after_sha,
        readiness_job_id=readiness["job_id"],
        changed_files=changed_files,
        state_reason=state_reason,
        note="builder command produced a commit and queued SHA-scoped readiness",
    )
    updated = update_task_state(task_dir, without_build_blocker(meta), {
        "state": "VERIFYING",
        "phase_status": {**(meta.get("phase_status") or {}), "EXECUTE": "BUILT", "VERIFY": "QUEUED"},
        "awaiting_operator": False,
        "state_reason": state_reason,
        "commit": after_sha,
        "dispatch": {**dispatch, "readiness_job_id": readiness["job_id"]},
        "build": {
            "builder_command": command,
            "commit_sha": after_sha,
            "changed_files": changed_files,
            "readiness_job_id": readiness["job_id"],
            "summary_path": str(task_dir / "builder-summary.md"),
            "evidence_path": str(task_dir / "build-evidence.json"),
            "model_policy": model_metadata,
        },
    })
    event = {"kind": "BUILDER-WORKER", "decision": "BUILT", "task_id": selected_task_id, "commit_sha": after_sha, "readiness_job_id": readiness["job_id"], "timestamp": utc_now()}
    append_build_ledger(repo_root, event)
    return {**event, "meta": {"state": updated.get("state"), "state_reason": updated.get("state_reason")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--builder-command", default=None, help="Command to run in the dispatched worktree. Receives BUILDER_PROMPT_PATH and BUILD_TASK_DIR env vars.")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    payload = run_builder(Path.cwd(), task_id=args.task_id, builder_command=args.builder_command, timeout_seconds=args.timeout_seconds)
    write_json(Path.cwd() / ".automation" / "status" / "run-builder-worker-last.json", {**payload, "checked_at": utc_now()})
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
