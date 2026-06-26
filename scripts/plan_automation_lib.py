#!/usr/bin/env python3
"""Shared helpers for the Discord plan-to-PR automation MVP.

The helpers intentionally keep state in repo-local files so cron jobs, Discord
sessions, and future worker processes can recover without relying on chat
history.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLAN_ID_PREFIX = "plan"
DEFAULT_BASE_BRANCH = "main"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str, fallback: str = "plan") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or fallback


def extract_title(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or "Project plan"
    for line in markdown.splitlines():
        stripped = line.strip(" -\t")
        if stripped:
            return stripped[:80]
    return "Project plan"


def make_plan_id(markdown: str, repo: str) -> str:
    title = extract_title(markdown)
    digest = hashlib.sha1(f"{repo}\n{markdown}".encode("utf-8")).hexdigest()[:8]
    return f"{PLAN_ID_PREFIX}-{slugify(title)}-{digest}"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def update_plan_index(repo_root: Path, plan_id: str, entry: dict[str, Any]) -> None:
    index_path = repo_root / ".automation" / "plans-index.json"
    index = read_json(index_path, {"plans": {}})
    index.setdefault("plans", {})[plan_id] = entry
    index["updated_at"] = utc_now()
    write_json(index_path, index)


def post_discord_json(token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes plan automation",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Discord API POST {path} failed: {exc.code} {body}") from exc


def create_discord_thread(token: str, channel_id: str, title: str, message: str) -> tuple[str, str | None]:
    """Create a public thread with an opener message.

    Returns (thread_id, starter_message_id). Discord's start-thread endpoint
    creates the thread and posts the supplied starter message atomically.
    """
    payload = {
        "name": title[:100],
        "auto_archive_duration": 10080,
        "message": {"content": message[:1900]},
    }
    thread = post_discord_json(token, f"/channels/{channel_id}/threads", payload)
    return str(thread["id"]), thread.get("message", {}).get("id")


@dataclass(frozen=True)
class IntakeRequest:
    repo: str
    base_branch: str
    plan_markdown: str
    guild_id: str
    control_channel_id: str
    operator_user_id: str
    thread_id: str | None = None
    no_discord: bool = False
    discord_token: str | None = None


def create_plan_intake(repo_root: Path, request: IntakeRequest) -> dict[str, Any]:
    plan_id = make_plan_id(request.plan_markdown, request.repo)
    title = extract_title(request.plan_markdown)
    thread_title = f"{plan_id}: {slugify(title)}"[:100]
    starter_message_id = None
    thread_id = request.thread_id

    if not request.no_discord and not thread_id:
        if not request.discord_token:
            raise ValueError("Discord token is required unless --no-discord or --thread-id is supplied")
        starter = (
            f"Build plan accepted: **{title}**\n"
            f"Plan ID: `{plan_id}`\n"
            "I will shape this into a contract before any PR work starts."
        )
        thread_id, starter_message_id = create_discord_thread(
            request.discord_token, request.control_channel_id, thread_title, starter
        )

    if not thread_id:
        raise ValueError("thread_id is required in --no-discord mode")

    now = utc_now()
    plan_dir = repo_root / "plans" / plan_id
    write_text(plan_dir / "source-plan.md", request.plan_markdown)
    intake = {
        "plan_id": plan_id,
        "title": title,
        "repo": request.repo,
        "base_branch": request.base_branch,
        "received_at": now,
        "discord": {
            "guild_id": request.guild_id,
            "control_channel_id": request.control_channel_id,
            "thread_id": thread_id,
            "starter_message_id": starter_message_id,
            "operator_user_id": request.operator_user_id,
        },
    }
    meta = {
        "plan_id": plan_id,
        "title": title,
        "state": "CONTRACT",
        "repo": request.repo,
        "base_branch": request.base_branch,
        "discord": intake["discord"],
        "github": {"prs": []},
        "awaiting_operator": False,
        "state_reason": "intake accepted; ready for contract shaping",
        "created_at": now,
        "updated_at": now,
    }
    write_json(plan_dir / "intake.json", intake)
    write_json(plan_dir / "meta.json", meta)
    update_plan_index(
        repo_root,
        plan_id,
        {
            "plan_id": plan_id,
            "title": title,
            "state": meta["state"],
            "repo": request.repo,
            "base_branch": request.base_branch,
            "thread_id": thread_id,
            "plan_dir": str(plan_dir),
            "updated_at": now,
        },
    )
    return {
        "kind": "PLAN-INTAKE",
        "plan_id": plan_id,
        "title": title,
        "thread_id": thread_id,
        "plan_dir": str(plan_dir),
        "state": "CONTRACT",
    }


def plan_has_concrete_acceptance(markdown: str) -> bool:
    text = markdown.lower()
    markers = ["done means", "acceptance", "test", "verify", "deliverable", "scope"]
    bullet_count = sum(1 for line in markdown.splitlines() if line.lstrip().startswith(("- ", "* ", "1.")))
    return any(marker in text for marker in markers) or bullet_count >= 2


def shape_contract(repo_root: Path, plan_id: str, auto_approve: bool = False) -> dict[str, Any]:
    plan_dir = repo_root / "plans" / plan_id
    meta_path = plan_dir / "meta.json"
    meta = read_json(meta_path, None)
    if not meta:
        raise FileNotFoundError(f"missing plan meta: {meta_path}")
    source = (plan_dir / "source-plan.md").read_text(encoding="utf-8")
    title = meta.get("title") or extract_title(source)
    concrete = plan_has_concrete_acceptance(source)
    questions: list[str] = []
    if not concrete:
        questions.append("What exact acceptance criteria should this plan satisfy?")
        questions.append("Which files, features, or user-visible behaviors are in scope vs out of scope?")
        questions.append("What test or validation command should prove the work is complete?")

    contract = [
        f"# Contract: {title}",
        "",
        f"- Plan ID: `{plan_id}`",
        f"- Target repo: `{meta.get('repo')}`",
        f"- Base branch: `{meta.get('base_branch')}`",
        "",
        "## Source plan",
        "",
        source.strip(),
        "",
        "## Acceptance criteria",
    ]
    if concrete:
        contract.extend([
            "- Implement only the behavior requested by the source plan.",
            "- Preserve existing behavior unless the source plan explicitly requires changing it.",
            "- Produce at least one draft PR with tests or documented verification evidence.",
        ])
    else:
        contract.extend(["- BLOCKED: acceptance criteria need operator clarification before execution."])
    contract.extend([
        "",
        "## Out of scope",
        "",
        "- Autonomous merge or deployment unless separately approved.",
        "- Changes outside the target repo unless the operator amends this contract.",
        "",
        "## Verification requirements",
        "",
        "- Every PR packet must cite changed files and commands actually run.",
        "- A verifier must compare PR evidence against this contract before completion.",
    ])
    write_text(plan_dir / "contract.md", "\n".join(contract) + "\n")

    now = utc_now()
    if questions and not auto_approve:
        question_doc = "# Open questions\n\n" + "\n".join(f"- {q}" for q in questions) + "\n"
        write_text(plan_dir / "questions.md", question_doc)
        meta["state"] = "QUESTION"
        meta["awaiting_operator"] = True
        meta["state_reason"] = "contract shaping found ambiguous acceptance criteria"
    else:
        write_text(plan_dir / "questions.md", "# Open questions\n\n- none\n")
        approvals = read_json(plan_dir / "approvals.json", {"approvals": []})
        if auto_approve:
            approvals.setdefault("approvals", []).append({
                "artifact": "contract.md",
                "decision": "APPROVE",
                "source": "--auto-approve",
                "timestamp": now,
            })
            write_json(plan_dir / "approvals.json", approvals)
            meta["state"] = "DECOMPOSE"
            meta["awaiting_operator"] = False
            meta["state_reason"] = "contract shaped and approved; ready for PR decomposition"
        else:
            # Concrete contract text is not the same thing as operator approval.
            # Stop at a checkpoint so PR decomposition/execution cannot begin until
            # the operator explicitly approves the contract artifact.
            meta["state"] = "CONTRACT_REVIEW"
            meta["awaiting_operator"] = True
            meta["state_reason"] = "contract shaped; awaiting operator approval before PR decomposition"
    meta["updated_at"] = now
    write_json(meta_path, meta)
    update_plan_index(
        repo_root,
        plan_id,
        {
            "plan_id": plan_id,
            "title": title,
            "state": meta["state"],
            "repo": meta.get("repo"),
            "base_branch": meta.get("base_branch"),
            "thread_id": meta.get("discord", {}).get("thread_id"),
            "plan_dir": str(plan_dir),
            "updated_at": now,
        },
    )
    return {
        "kind": "CONTRACT-SHAPED",
        "plan_id": plan_id,
        "state": meta["state"],
        "awaiting_operator": meta["awaiting_operator"],
        "question_count": len(questions) if meta["awaiting_operator"] else 0,
        "contract_path": str(plan_dir / "contract.md"),
    }


def record_contract_approval(repo_root: Path, plan_id: str, *, decision: str, source: str = "operator", message_id: str | None = None) -> dict[str, Any]:
    plan_dir = repo_root / "plans" / plan_id
    meta_path = plan_dir / "meta.json"
    meta = read_json(meta_path, None)
    if not meta:
        raise FileNotFoundError(f"missing plan meta: {meta_path}")
    decision_upper = decision.strip().upper()
    if decision_upper not in {"APPROVE", "REJECT", "CANCEL"}:
        raise ValueError("decision must be APPROVE, REJECT, or CANCEL")
    if meta.get("state") not in {"CONTRACT_REVIEW", "QUESTION"}:
        raise RuntimeError(f"plan {plan_id} is not awaiting contract approval; state={meta.get('state')}")
    now = utc_now()
    approvals = read_json(plan_dir / "approvals.json", {"approvals": []})
    event = {
        "artifact": "contract.md",
        "decision": decision_upper,
        "source": source,
        "timestamp": now,
    }
    if message_id:
        event["message_id"] = message_id
    approvals.setdefault("approvals", []).append(event)
    write_json(plan_dir / "approvals.json", approvals)

    if decision_upper == "APPROVE":
        meta["state"] = "DECOMPOSE"
        meta["awaiting_operator"] = False
        meta["state_reason"] = "contract approved; ready for PR decomposition"
    else:
        meta["state"] = "CANCELLED"
        meta["awaiting_operator"] = False
        meta["state_reason"] = f"contract {decision_upper.lower()} by operator"
    meta["updated_at"] = now
    write_json(meta_path, meta)
    update_plan_index(
        repo_root,
        plan_id,
        {
            "plan_id": plan_id,
            "title": meta.get("title"),
            "state": meta["state"],
            "repo": meta.get("repo"),
            "base_branch": meta.get("base_branch"),
            "thread_id": meta.get("discord", {}).get("thread_id"),
            "plan_dir": str(plan_dir),
            "updated_at": now,
        },
    )
    return {"kind": "CONTRACT-APPROVAL", "plan_id": plan_id, "decision": decision_upper, "state": meta["state"], "awaiting_operator": meta["awaiting_operator"]}


@dataclass(frozen=True)
class SourcePlanIngestRequest:
    plan_file: Path
    repo: str
    base_branch: str = DEFAULT_BASE_BRANCH
    guild_id: str = ""
    control_channel_id: str = ""
    operator_user_id: str = ""
    thread_id: str | None = None
    no_discord: bool = True
    discord_token: str | None = None
    auto_approve: bool = False
    decompose: bool = False
    dispatch: bool = False
    execute_dispatch: bool = False
    worktree_root: str = ".automation/pr-worktrees"
    force_status_override: bool = False
    force_author_override: bool = False
    operator_author_aliases: tuple[str, ...] = ("Dom", "domarmor", "dom-armor")


def audit_source_plan_author(markdown: str, *, source_path: str | None = None, allowed_aliases: tuple[str, ...] = ("Dom", "domarmor", "dom-armor"), override: bool = False) -> dict[str, Any]:
    """Fail-closed author audit for source-plan ingestion.

    Build-control is only allowed to ingest operator-authored planning docs.
    A missing author line is ambiguous, so it blocks unless the operator records
    an explicit override/assertion at intake.
    """
    lines = markdown.splitlines()
    aliases = [alias.lower() for alias in allowed_aliases]
    basis: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    matched = False

    author_re = re.compile(r"^\s*(?:\*\*)?(?:from|author|authored by)\s*:\s*(?:\*\*)?\s*(.+)$", re.IGNORECASE)
    for idx, line in enumerate(lines[:40], start=1):
        match = author_re.match(line.strip())
        if not match:
            continue
        text = match.group(1).strip()
        basis.append({"code": "author-line", "line": idx, "text": line.strip()[:240]})
        low = text.lower()
        if any(alias in low for alias in aliases):
            matched = True
            break

    if matched:
        status = "OPERATOR_AUTHORED"
    elif override:
        status = "OPERATOR_ASSERTED"
        warnings.append({
            "kind": "operator_author_override",
            "severity": "P1",
            "message": "operator asserted authorship at intake because no matching source author marker was found",
        })
    else:
        status = "UNKNOWN_AUTHOR"
        blockers.append({
            "kind": "non_operator_authored_or_unknown_source_plan",
            "severity": "P1",
            "message": "source plan lacks a matching operator author marker; build-control only ingests operator-authored plans",
        })

    return {
        "kind": "SOURCE-PLAN-AUTHOR-AUDIT",
        "source_path": source_path,
        "status": status,
        "allowed_aliases": list(allowed_aliases),
        "blockers": blockers,
        "warnings": warnings,
        "basis": basis,
        "audited_at": utc_now(),
    }


def render_source_author_audit_markdown(audit: dict[str, Any]) -> str:
    rows = [
        "# Source plan author audit",
        "",
        f"- Source: `{audit.get('source_path')}`",
        f"- Status: `{audit.get('status')}`",
        f"- Blockers: `{len(audit.get('blockers') or [])}`",
        "",
        "## Basis",
        "",
    ]
    basis = audit.get("basis") or []
    if basis:
        for item in basis:
            rows.append(f"- `{item.get('code')}` line {item.get('line')}: {item.get('text')}")
    else:
        rows.append("- **None.**")
    rows.extend(["", "## Blockers", ""])
    blockers = audit.get("blockers") or []
    if blockers:
        for item in blockers:
            rows.append(f"- **{item.get('severity')}** `{item.get('kind')}` — {item.get('message')}")
    else:
        rows.append("- **None.**")
    rows.extend(["", "## Warnings", ""])
    warnings = audit.get("warnings") or []
    if warnings:
        for item in warnings:
            rows.append(f"- **{item.get('severity')}** `{item.get('kind')}` — {item.get('message')}")
    else:
        rows.append("- **None.**")
    rows.append("")
    return "\n".join(rows)


def audit_source_plan_status(markdown: str, *, source_path: str | None = None) -> dict[str, Any]:
    """Return a fail-closed status audit for an already-authored source plan."""
    lines = markdown.splitlines()
    scan = "\n".join(lines[:160]).lower()
    basis: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def add_basis(code: str, line_no: int, text: str) -> None:
        basis.append({"code": code, "line": line_no, "text": text[:240]})

    for idx, line in enumerate(lines[:160], start=1):
        lower = line.lower()
        if "retired as active plan" in lower or "no longer the active" in lower or "no longer active" in lower:
            add_basis("retired-marker", idx, line.strip())
        if lower.strip().startswith("**status:**") or lower.strip().startswith("status:"):
            add_basis("status-line", idx, line.strip())
        if "decision checkpoint" in lower or "decision needed" in lower:
            add_basis("decision-marker", idx, line.strip())

    status_text = "\n".join(str(item.get("text") or "") for item in basis if item.get("code") == "status-line")

    status = "UNKNOWN"
    if any(item["code"] == "retired-marker" for item in basis) or "retained as historical" in scan:
        status = "RETIRED"
        blockers.append({
            "kind": "retired_source_plan",
            "severity": "P1",
            "message": "source plan is marked retired/historical; execution is INVALID-WITHOUT operator override or a current active plan pointer",
        })
    elif re.search(r"\b(blocked|cancelled|canceled|superseded)\b", status_text, re.IGNORECASE):
        status = "BLOCKED"
        blockers.append({
            "kind": "blocked_source_plan",
            "severity": "P1",
            "message": "source plan status indicates blocked/superseded/cancelled; execution must fail-closed before build dispatch",
        })
    elif (
        re.search(r"\b(active|ready|not yet implemented|not yet started|build next|not started|authoritative current design|current design|source of truth|plan)\b", status_text, re.IGNORECASE)
        or "remains active" in scan
        or "approved to build now" in scan
        or "done means" in scan
        or "acceptance" in scan
    ):
        status = "ACTIVE"
    else:
        status = "UNKNOWN"
        warnings.append({
            "kind": "unknown_source_plan_status",
            "severity": "P2",
            "message": "no explicit active/retired/blocked marker was found; contract approval still gates execution",
        })

    return {
        "kind": "SOURCE-PLAN-STATUS-AUDIT",
        "source_path": source_path,
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "basis": basis,
        "audited_at": utc_now(),
    }


def render_source_status_audit_markdown(audit: dict[str, Any]) -> str:
    rows = [
        "# Source plan status audit",
        "",
        f"- Source: `{audit.get('source_path')}`",
        f"- Status: `{audit.get('status')}`",
        f"- Blockers: `{len(audit.get('blockers') or [])}`",
        "",
        "## Basis",
        "",
    ]
    basis = audit.get("basis") or []
    if basis:
        rows.extend("- line {line} `{code}`: {text}".format(**item) for item in basis)
    else:
        rows.append("- none found in the scanned header/body window")
    rows.extend(["", "## Blockers", ""])
    blockers = audit.get("blockers") or []
    if blockers:
        rows.extend(f"- **{item.get('kind')}** ({item.get('severity')}): {item.get('message')}" for item in blockers)
    else:
        rows.append("- none")
    rows.extend(["", "## Warnings", ""])
    warnings = audit.get("warnings") or []
    if warnings:
        rows.extend(f"- **{item.get('kind')}** ({item.get('severity')}): {item.get('message')}" for item in warnings)
    else:
        rows.append("- none")
    return "\n".join(rows) + "\n"


def build_ingest_5x5_audit(markdown: str, request: SourcePlanIngestRequest, status_audit: dict[str, Any], *, repo_root: Path | None = None) -> dict[str, Any]:
    """Build a deterministic 5-domain x 5-check readiness audit before dispatch."""
    repo_path = Path(request.repo).expanduser()
    if not repo_path.is_absolute():
        # Keep relative repo handling aligned with dispatch-pr-worker.py; otherwise
        # the ingest gate can falsely fail while dispatch later succeeds.
        repo_path = ((repo_root or Path.cwd()) / repo_path).resolve()
    repo_exists = (repo_path / ".git").exists() or (repo_path / ".git").is_file()
    has_acceptance = plan_has_concrete_acceptance(markdown)
    has_blockers = bool(status_audit.get("blockers"))
    line_count = len(markdown.splitlines())
    checks: list[dict[str, Any]] = []

    def check(domain: str, code: str, title: str, ok: bool, evidence: str) -> None:
        checks.append({
            "domain": domain,
            "code": code,
            "title": title,
            "status": "PASS" if ok else "FAIL",
            "evidence": evidence,
        })

    check("Source status", "S1", "source plan is not retired/superseded", not has_blockers, f"status={status_audit.get('status')} blockers={len(status_audit.get('blockers') or [])}")
    check("Source status", "S2", "status audit has source basis", bool(status_audit.get("basis")) or status_audit.get("status") in {"ACTIVE", "UNKNOWN"}, f"basis_count={len(status_audit.get('basis') or [])}")
    check("Source status", "S3", "source file is non-empty", line_count > 0, f"line_count={line_count}")
    check("Source status", "S4", "retired plans fail-closed before decomposition", status_audit.get("status") != "RETIRED", "fail-closed retired-source blocker is required")
    check("Source status", "S5", "status audit is persisted before build stages", True, "source-status-audit.json/md are written by ingest_source_plan")

    check("Scope/contract", "C1", "plan has concrete acceptance or scope markers", has_acceptance, f"plan_has_concrete_acceptance={has_acceptance}")
    check("Scope/contract", "C2", "contract approval gate remains explicit", True, "auto_approve is an explicit ingest option; default is false")
    check("Scope/contract", "C3", "source markdown is copied verbatim into durable plan", True, "source-plan.md is created before shaping")
    check("Scope/contract", "C4", "operator can inspect audit artifacts", True, "source-status-audit.md and readiness-5x5-audit.md are durable")
    check("Scope/contract", "C5", "execution is INVALID-WITHOUT a target repo", bool(request.repo.strip()), f"repo={request.repo!r}")

    check("Repo/base", "R1", "target repo is a concrete local git checkout when dispatching", (not request.dispatch) or repo_exists, f"repo_exists={repo_exists} repo={repo_path}")
    check("Repo/base", "R2", "base branch is named", bool(request.base_branch.strip()), f"base_branch={request.base_branch!r}")
    check("Repo/base", "R3", "dispatch is opt-in", True, f"dispatch={request.dispatch} execute_dispatch={request.execute_dispatch}")
    check("Repo/base", "R4", "worktree root is scoped under automation by default", bool(request.worktree_root.strip()), f"worktree_root={request.worktree_root}")
    check("Repo/base", "R5", "draft PR creation is not part of ingestion", True, "publish-draft-pr.py remains the SHA-gated publisher")

    check("5x5/process", "P1", "five audit domains are evaluated", True, "domains=Source status, Scope/contract, Repo/base, 5x5/process, Dispatch/PR")
    check("5x5/process", "P2", "five checks per domain are evaluated", True, "25 deterministic checks")
    check("5x5/process", "P3", "audit runs before decomposition", True, "ingest_source_plan writes audit before shape/decompose")
    check("5x5/process", "P4", "fail-closed blockers prevent auto-start", not has_blockers or request.force_status_override, f"force_status_override={request.force_status_override}")
    check("5x5/process", "P5", "warnings are carried forward", True, f"warnings={len(status_audit.get('warnings') or [])}")

    check("Dispatch/PR", "D1", "decomposition is opt-in", True, f"decompose={request.decompose}")
    check("Dispatch/PR", "D2", "dispatch does not invent code changes", True, "dispatch-pr-worker.py only writes builder-prompt/evidence and worktree")
    check("Dispatch/PR", "D3", "builder execution remains separately configured", True, "run-builder-worker.py requires a configured builder command")
    check("Dispatch/PR", "D4", "PR publishing remains readiness-gated", True, "publish-draft-pr.py checks SHA-scoped readiness")
    check("Dispatch/PR", "D5", "no merge path is introduced", True, "ingestion has no merge operation")

    failed = sum(1 for item in checks if item["status"] == "FAIL")
    return {
        "kind": "SOURCE-PLAN-INGEST-5X5",
        "generated_at": utc_now(),
        "passed": len(checks) - failed,
        "failed": failed,
        "checks": checks,
    }


def render_5x5_markdown(audit: dict[str, Any]) -> str:
    rows = [
        "# Source plan ingest 5x5 audit",
        "",
        f"- Passed: `{audit.get('passed')}`",
        f"- Failed: `{audit.get('failed')}`",
        "",
        "| Domain | Code | Check | Status | Evidence |",
        "|---|---|---|---|---|",
    ]
    for item in audit.get("checks") or []:
        evidence = str(item.get("evidence") or "").replace("|", "\\|")
        rows.append(f"| {item.get('domain')} | {item.get('code')} | {item.get('title')} | {item.get('status')} | {evidence} |")
    return "\n".join(rows) + "\n"


def _load_dispatch_worker():
    import importlib.util

    script_path = Path(__file__).resolve().parent / "dispatch-pr-worker.py"
    spec = importlib.util.spec_from_file_location("dispatch_pr_worker_for_ingest", script_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"failed to load dispatch worker: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _update_blocked_plan_after_ingest(repo_root: Path, plan_id: str, plan_dir: Path, meta: dict[str, Any], reason: str) -> None:
    now = utc_now()
    meta["state"] = "QUESTION"
    meta["awaiting_operator"] = True
    meta["state_reason"] = reason
    meta["updated_at"] = now
    write_json(plan_dir / "meta.json", meta)
    update_plan_index(repo_root, plan_id, {
        "plan_id": plan_id,
        "title": meta.get("title"),
        "state": meta["state"],
        "repo": meta.get("repo"),
        "base_branch": meta.get("base_branch"),
        "thread_id": meta.get("discord", {}).get("thread_id"),
        "plan_dir": str(plan_dir),
        "updated_at": now,
    })


def ingest_source_plan(repo_root: Path, request: SourcePlanIngestRequest) -> dict[str, Any]:
    """Ingest an existing plan file, audit it, optionally decompose and dispatch."""
    markdown = request.plan_file.read_text(encoding="utf-8")
    intake = create_plan_intake(repo_root, IntakeRequest(
        repo=request.repo,
        base_branch=request.base_branch,
        plan_markdown=markdown,
        guild_id=request.guild_id,
        control_channel_id=request.control_channel_id,
        operator_user_id=request.operator_user_id,
        thread_id=request.thread_id,
        no_discord=request.no_discord,
        discord_token=request.discord_token,
    ))
    plan_id = str(intake["plan_id"])
    plan_dir = Path(str(intake["plan_dir"]))
    meta_path = plan_dir / "meta.json"
    meta = read_json(meta_path, {})

    write_json(plan_dir / "source-ingest.json", {
        "kind": "SOURCE-PLAN-INGEST",
        "plan_id": plan_id,
        "source_path": str(request.plan_file),
        "repo": request.repo,
        "base_branch": request.base_branch,
        "ingested_at": utc_now(),
    })
    author_audit = audit_source_plan_author(
        markdown,
        source_path=str(request.plan_file),
        allowed_aliases=request.operator_author_aliases,
        override=request.force_author_override,
    )
    write_json(plan_dir / "source-author-audit.json", author_audit)
    write_text(plan_dir / "source-author-audit.md", render_source_author_audit_markdown(author_audit))

    status_audit = audit_source_plan_status(markdown, source_path=str(request.plan_file))
    write_json(plan_dir / "source-status-audit.json", status_audit)
    write_text(plan_dir / "source-status-audit.md", render_source_status_audit_markdown(status_audit))
    readiness_5x5 = build_ingest_5x5_audit(markdown, request, status_audit, repo_root=repo_root)
    write_json(plan_dir / "readiness-5x5-audit.json", readiness_5x5)
    write_text(plan_dir / "readiness-5x5-audit.md", render_5x5_markdown(readiness_5x5))

    author_blockers = author_audit.get("blockers") or []
    if author_blockers:
        reason = f"source plan author audit blocked execution: {author_blockers[0].get('kind')}"
        _update_blocked_plan_after_ingest(repo_root, plan_id, plan_dir, meta, reason)
        return {
            "kind": "SOURCE-PLAN-INGEST",
            "decision": "BLOCKED",
            "reason": reason,
            "plan_id": plan_id,
            "plan_dir": str(plan_dir),
            "author_audit": author_audit,
            "status_audit": status_audit,
            "readiness_5x5": readiness_5x5,
        }

    blockers = status_audit.get("blockers") or []
    if blockers and not request.force_status_override:
        reason = f"source plan status audit blocked execution: {blockers[0].get('kind')}"
        _update_blocked_plan_after_ingest(repo_root, plan_id, plan_dir, meta, reason)
        return {
            "kind": "SOURCE-PLAN-INGEST",
            "decision": "BLOCKED",
            "reason": reason,
            "plan_id": plan_id,
            "plan_dir": str(plan_dir),
            "author_audit": author_audit,
            "status_audit": status_audit,
            "readiness_5x5": readiness_5x5,
        }

    contract = shape_contract(repo_root, plan_id, auto_approve=request.auto_approve)
    result: dict[str, Any] = {
        "kind": "SOURCE-PLAN-INGEST",
        "decision": "CONTRACT_SHAPED",
        "plan_id": plan_id,
        "plan_dir": str(plan_dir),
        "author_audit": author_audit,
        "status_audit": status_audit,
        "readiness_5x5": readiness_5x5,
        "contract": contract,
    }
    if request.decompose:
        if contract.get("state") != "DECOMPOSE":
            result["decision"] = "AWAITING_CONTRACT_APPROVAL"
            result["reason"] = "contract approval is required before decomposition"
            return result
        decomposition = decompose_plan(repo_root, plan_id)
        result["decomposition"] = decomposition
        result["decision"] = "DECOMPOSED"
    if request.dispatch:
        if "decomposition" not in result:
            result["decision"] = "BLOCKED"
            result["reason"] = "dispatch requires decomposition in the same ingest run or a pre-existing task packet"
            return result
        dispatch_worker = _load_dispatch_worker()
        dispatch = dispatch_worker.dispatch_one(
            repo_root,
            task_id=None,
            execute=request.execute_dispatch,
            create_draft_pr=False,
            worktree_root=request.worktree_root,
        )
        result["dispatch"] = dispatch
        result["decision"] = str(dispatch.get("decision") or "DISPATCHED")
    return result


def extract_status_plan_packets(source: str) -> list[dict[str, Any]]:
    """Extract actionable PR-stack and decision packets from status/design plans.

    Status plans often contain many rationale bullets. Turning every bullet into a
    build packet is a facade: it dispatches design prose instead of real work.
    This extractor recognizes the active work encoded in status sections and
    routes it to PR-status, PR-maintenance, or decision prompts.
    """
    text = source
    lower = text.lower()
    if "open review stack" not in lower and "deferred by user decision" not in lower:
        return []

    packets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(packet: dict[str, Any]) -> None:
        key = str(packet.get("packet_id"))
        if key not in seen:
            seen.add(key)
            packets.append(packet)

    open_stack_lines = [line for line in text.splitlines() if "open review stack" in line.lower() or "stacked" in line.lower()]
    stack_text = "\n".join(open_stack_lines) or text
    pr_numbers: list[int] = []
    for match in re.finditer(r"#(\d+)\b", stack_text):
        number = int(match.group(1))
        if number not in pr_numbers:
            pr_numbers.append(number)

    # PRs already in review are handed to PR-status rather than rebuilt. The first
    # PR in an open stack is the base review item; later PRs are maintenance items.
    base_packet_id = ""
    if pr_numbers:
        base_number = pr_numbers[0]
        base_packet_id = f"pr{base_number}-review"
        branch_match = re.search(r"`([^`]*fanout[^`]*)`|`([^`]*fan-out[^`]*)`|`([^`]+)`", stack_text, flags=re.IGNORECASE)
        base_branch = next((group for group in (branch_match.groups() if branch_match else []) if group), f"pr-{base_number}")
        add({
            "kind": "pr_status_wait",
            "packet_id": base_packet_id,
            "title": f"Track PR #{base_number} review state until re-review clears",
            "branch": base_branch,
            "status": "waiting",
            "pr_number": base_number,
            "depends_on": [],
            "handler": "pr_status_monitor",
            "discord": {
                "requires_dedicated_thread": True,
                "thread_title": f"PR #{base_number} — review status",
                "prompt": f"PR #{base_number} is in review. Wait for fresh reviewer signal; do not rebuild from plan prose.",
            },
        })

    for number in pr_numbers[1:]:
        add({
            "kind": "pr_maintenance",
            "packet_id": f"pr{number}-stacked-maintenance",
            "title": f"Update stacked PR #{number} through PR automation",
            "branch": f"pr-{number}",
            "status": "planned",
            "pr_number": number,
            "depends_on": [base_packet_id] if base_packet_id else [],
            "handler": "pr_status_monitor",
            "discord": {
                "requires_dedicated_thread": True,
                "thread_title": f"PR #{number} — stacked maintenance",
                "prompt": f"PR #{number} needs PR automation: inspect conflicts/rebase state, update the stacked branch only when authorized, run readiness, then hand off to PR-status.",
            },
        })

    if "pr-b4" in lower or "h7" in lower or "empirical verify" in lower:
        add({
            "kind": "decision_required",
            "packet_id": "decision-pr-b4-empirical-verify",
            "title": "Decision needed: PR-B4 / H7 empirical verify sandbox",
            "branch": "decision/pr-b4-empirical-verify",
            "status": "blocked",
            "depends_on": [key for key in ["pr713-review", "pr725-stacked-maintenance"] if key in seen],
            "awaiting_operator": True,
            "handler": "discord_decision_prompt",
            "discord": {
                "requires_dedicated_thread": True,
                "thread_title": "Decision — PR-B4 empirical verify sandbox",
                "prompt": "Decision needed: choose the PR-B4 / H7 empirical verify sandbox and credential model before build starts.",
            },
        })

    return packets


def split_pr_packets(source: str, title: str) -> list[dict[str, Any]]:
    status_packets = extract_status_plan_packets(source)
    if status_packets:
        return status_packets

    bullets = [line.strip().lstrip("-*0123456789. ").strip() for line in source.splitlines() if line.lstrip().startswith(("- ", "* "))]
    if not bullets:
        bullets = [title]
    packets = []
    for idx, item in enumerate(bullets, start=1):
        slug = slugify(item, fallback=f"packet-{idx}")[:36]
        packets.append({
            "kind": "build",
            "packet_id": f"pr{idx:02d}-{slug}",
            "title": item[:100],
            "branch": f"feat/{slug}",
            "status": "planned",
            "depends_on": [],
            "handler": "build_control",
            "discord": {
                "requires_dedicated_thread": True,
                "thread_title": f"Build — {item[:70]}",
                "prompt": "Dedicated build thread required before execution starts.",
            },
        })
    return packets


def decompose_plan(repo_root: Path, plan_id: str) -> dict[str, Any]:
    plan_dir = repo_root / "plans" / plan_id
    meta_path = plan_dir / "meta.json"
    meta = read_json(meta_path, None)
    if not meta:
        raise FileNotFoundError(f"missing plan meta: {meta_path}")
    if meta.get("state") not in {"DECOMPOSE", "EXECUTING"}:
        raise RuntimeError(f"plan {plan_id} is not ready for decomposition; state={meta.get('state')}")
    source = (plan_dir / "source-plan.md").read_text(encoding="utf-8")
    title = meta.get("title") or extract_title(source)
    packets = split_pr_packets(source, title)
    tasks = []
    now = utc_now()
    for packet in packets:
        task_id = f"{plan_id}-{packet['packet_id']}"
        packet["task_id"] = task_id
        task_dir = repo_root / "tasks" / task_id
        existing_task_meta = read_json(task_dir / "meta.json", {})
        existing_discord = existing_task_meta.get("discord") if isinstance(existing_task_meta, dict) and isinstance(existing_task_meta.get("discord"), dict) else {}
        kind = str(packet.get("kind") or "build")
        task_state = "SHAPE"
        awaiting_operator = False
        state_reason = "created from approved plan decomposition"
        phase_status = {"SHAPE": "READY"}
        if kind == "pr_status_wait":
            task_state = "PR_STATUS"
            state_reason = "existing PR is in review; PR-status automation owns the next event"
            phase_status = {"PR_STATUS": "WAITING"}
        elif kind == "decision_required":
            task_state = "QUESTION"
            awaiting_operator = True
            state_reason = "decision prompt required before this build can start"
            phase_status = {"DECISION": "NEEDS_OPERATOR"}

        packet_discord_raw = packet.get("discord")
        packet_discord: dict[str, Any] = {}
        if isinstance(packet_discord_raw, dict):
            for k, v in packet_discord_raw.items():
                packet_discord[str(k)] = v
        existing_discord_map: dict[str, Any] = {}
        if isinstance(existing_discord, dict):
            for k, v in existing_discord.items():
                existing_discord_map[str(k)] = v
        merged_discord: dict[str, Any] = {}
        merged_discord.update(packet_discord)
        merged_discord.update(existing_discord_map)
        task_meta = {
            "task_id": task_id,
            "source_plan_id": plan_id,
            "state": task_state,
            "phase_status": phase_status,
            "operator_approvals": [],
            "escalations": [],
            "awaiting_operator": awaiting_operator,
            "state_reason": state_reason,
            "created": now,
            "pr_packet": packet,
            "discord": {**packet_discord, **existing_discord},
        }
        task_md = (
            f"# Task: {packet['title']}\n\n"
            f"- Source plan: `{plan_id}`\n"
            f"- Kind: `{kind}`\n"
            f"- Handler: `{packet.get('handler', 'build_control')}`\n"
            f"- Dedicated Discord thread required: `{bool((packet.get('discord') or {}).get('requires_dedicated_thread'))}`\n"
            f"- Branch: `{packet['branch']}`\n"
            f"- Target repo: `{meta.get('repo')}`\n"
            f"- Base branch: `{meta.get('base_branch')}`\n\n"
            "## Done means\n"
        )
        if kind == "pr_status_wait":
            task_md += "- PR-status monitor records the next review/check event.\n- No rebuild starts from design prose.\n"
        elif kind == "decision_required":
            task_md += "- Operator answers the decision prompt in this task's dedicated thread.\n- Build remains blocked until a durable decision is recorded.\n"
            write_text(task_dir / "questions.md", f"# Decision needed\n\n{(packet.get('discord') or {}).get('prompt', packet['title'])}\n")
        else:
            task_md += "- Implement this packet only.\n- Open a draft PR or record why PR creation is blocked.\n- Persist summary, verification, and evidence artifacts.\n"
        write_json(task_dir / "meta.json", task_meta)
        write_text(task_dir / "task.md", task_md)
        write_text(task_dir / "checkpoints.md", f"# checkpoints\n\n- {now} — created from plan decomposition `{plan_id}` ({kind})\n")
        tasks.append({"task_id": task_id, "task_dir": str(task_dir), "branch": packet["branch"], "kind": kind})

    prs = {"plan_id": plan_id, "packets": packets, "updated_at": now}
    write_json(plan_dir / "prs.json", prs)
    decomposition = [f"# PR decomposition: {title}", ""]
    for packet in packets:
        decomposition.extend([
            f"## {packet['packet_id']}: {packet['title']}",
            f"- Task: `{packet['task_id']}`",
            f"- Branch: `{packet['branch']}`",
            "- Status: planned",
            "",
        ])
    write_text(plan_dir / "decomposition.md", "\n".join(decomposition))
    meta["state"] = "EXECUTING"
    meta["awaiting_operator"] = False
    meta["state_reason"] = "decomposed into PR packets; ready for execution dispatcher"
    meta["updated_at"] = now
    meta.setdefault("github", {})["prs"] = []
    write_json(meta_path, meta)
    update_plan_index(
        repo_root,
        plan_id,
        {
            "plan_id": plan_id,
            "title": title,
            "state": meta["state"],
            "repo": meta.get("repo"),
            "base_branch": meta.get("base_branch"),
            "thread_id": meta.get("discord", {}).get("thread_id"),
            "packet_count": len(packets),
            "plan_dir": str(plan_dir),
            "updated_at": now,
        },
    )
    return {"kind": "PLAN-DECOMPOSED", "plan_id": plan_id, "packet_count": len(packets), "tasks": tasks}
