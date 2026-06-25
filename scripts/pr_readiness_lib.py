#!/usr/bin/env python3
"""File-backed PR-readiness gate helpers.

The gate certifies one immutable commit SHA at a time. It blocks only
ready-for-review / merge-ready claims; it does not block continued building.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from plan_automation_lib import read_json, utc_now, write_json


BLOCKED_ACTIONS = ["ready_for_review", "merge_ready", "pr_ready_claim", "ready_for_rereview"]
ALLOWED_ACTIONS = ["continue_building", "draft_pr", "other_packets", "fix_issues"]
REQUIRED_CLEANUP_CRITICS = {"grounding", "security", "regression", "edge_case_matrix", "fresh_review_delta"}
SSRF_REQUIRED_CASES = {"ipv6_loopback", "ipv4_mapped_metadata", "private_ipv4"}
RACE_REQUIRED_CASES = {"stale_actor_window", "prior_state_guard", "identity_pin", "reset_stale_binding"}


def readiness_root(repo_root: Path) -> Path:
    return repo_root / ".automation" / "pr-readiness"


def make_job_id(task_id: str, branch: str, sha: str) -> str:
    digest = hashlib.sha1(f"{task_id}\n{branch}\n{sha}".encode("utf-8")).hexdigest()[:12]
    return f"readiness-{digest}"


def create_readiness_job(
    repo_root: Path,
    *,
    task_id: str,
    branch: str,
    sha: str,
    pr_url: str | None = None,
    audit_contract: str = "5x5-two-loop",
) -> dict[str, Any]:
    """Create or replace a readiness job for a specific commit SHA."""
    now = utc_now()
    job_id = make_job_id(task_id, branch, sha)
    job = {
        "job_id": job_id,
        "task_id": task_id,
        "branch": branch,
        "sha": sha,
        "pr_url": pr_url,
        "audit_contract": audit_contract,
        "state": "READINESS_QUEUED",
        "passed": False,
        "issues": [],
        "blocks": BLOCKED_ACTIONS,
        "does_not_block": ALLOWED_ACTIONS,
        "created_at": now,
        "updated_at": now,
        "state_reason": "readiness audit queued for immutable commit SHA",
    }
    write_json(readiness_root(repo_root) / f"{job_id}.json", job)
    return job


def load_readiness_job(repo_root: Path, job_id: str) -> dict[str, Any]:
    path = readiness_root(repo_root) / f"{job_id}.json"
    job = read_json(path, None)
    if not job:
        raise FileNotFoundError(f"missing PR-readiness job: {path}")
    return job


def validate_review_cleanup_evidence(evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return gate-blocking issues for a review-cleanup/readiness claim.

    This is intentionally conservative. A PR can keep building without this, but a
    ready-for-re-review claim is INVALID-WITHOUT fresh source-grounded closure for
    each reviewer finding and an adversarial matrix for the risk class that failed.
    """
    if not evidence:
        return []
    cleanup = evidence.get("review_cleanup") if isinstance(evidence, dict) else None
    if not isinstance(cleanup, dict):
        return []
    issues: list[dict[str, Any]] = []
    findings = cleanup.get("findings")
    critics = set(str(x) for x in cleanup.get("critics", []))
    missing_critics = sorted(REQUIRED_CLEANUP_CRITICS - critics)
    if missing_critics:
        issues.append({
            "kind": "missing_cleanup_critics",
            "severity": "P1",
            "missing": missing_critics,
            "message": "ready-for-re-review requires grounding, security, regression, edge-case, and fresh-review-delta critics",
        })
    if not isinstance(findings, list) or not findings:
        issues.append({"kind": "missing_review_findings", "severity": "P1", "message": "no reviewer findings were closed in evidence"})
        return issues
    for idx, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            issues.append({"kind": "invalid_finding", "severity": "P1", "index": idx})
            continue
        missing = [field for field in ["id", "status", "fix_commit", "evidence", "tests"] if not finding.get(field)]
        if missing:
            issues.append({"kind": "incomplete_finding_closure", "severity": "P1", "index": idx, "missing": missing})
        if str(finding.get("status") or "").lower() not in {"resolved", "fixed", "closed"}:
            issues.append({"kind": "finding_not_resolved", "severity": "P1", "index": idx, "status": finding.get("status")})
        tags = {str(tag).lower() for tag in finding.get("tags", [])}
        cases = {str(case).lower() for case in finding.get("edge_cases", [])}
        if "ssrf" in tags:
            missing_cases = sorted(SSRF_REQUIRED_CASES - cases)
            if missing_cases and not finding.get("dns_private_resolution_deferred_reason"):
                issues.append({
                    "kind": "ssrf_edge_cases_missing",
                    "severity": "P1",
                    "index": idx,
                    "missing": missing_cases,
                    "message": "SSRF cleanup must cover IPv6 literals / IPv4-mapped metadata / private IPv4 or explicitly defer DNS resolution with reason",
                })
        if tags & {"race", "state-write", "state_write", "toctou"}:
            missing_cases = sorted(RACE_REQUIRED_CASES - cases)
            if missing_cases:
                issues.append({
                    "kind": "race_edge_cases_missing",
                    "severity": "P1",
                    "index": idx,
                    "missing": missing_cases,
                    "message": "state/race cleanup must cover stale actor window, prior-state guard, identity pin, and stale-binding reset",
                })
    return issues


def mark_readiness_result(
    repo_root: Path,
    job_id: str,
    *,
    passed: bool,
    issues: list[dict[str, Any]],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job = load_readiness_job(repo_root, job_id)
    evidence_issues = validate_review_cleanup_evidence(evidence)
    all_issues = [*issues, *evidence_issues]
    job["passed"] = bool(passed) and not all_issues
    job["issues"] = all_issues
    if evidence is not None:
        job["evidence"] = evidence
    job["state"] = "PR_READY" if job["passed"] else "READINESS_FAILED"
    job["state_reason"] = (
        "5x5 two-loop audit passed for this SHA"
        if job["passed"]
        else "readiness audit found blocking issues"
    )
    job["updated_at"] = utc_now()
    write_json(readiness_root(repo_root) / f"{job_id}.json", job)
    return job


def readiness_blocks(job: dict[str, Any], *, current_sha: str, explain: bool = False) -> bool | dict[str, Any]:
    """Return whether ready-for-review is blocked for the current branch HEAD."""
    if job.get("sha") != current_sha:
        result = {
            "blocked": True,
            "reason": "stale_sha",
            "audited_sha": job.get("sha"),
            "current_sha": current_sha,
        }
    elif not job.get("passed") or job.get("state") != "PR_READY":
        result = {"blocked": True, "reason": "audit_not_passed", "state": job.get("state")}
    else:
        result = {"blocked": False, "reason": "passed", "state": job.get("state")}
    return result if explain else bool(result["blocked"])
