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


def split_pr_packets(source: str, title: str) -> list[dict[str, Any]]:
    bullets = [line.strip().lstrip("-*0123456789. ").strip() for line in source.splitlines() if line.lstrip().startswith(("- ", "* "))]
    if not bullets:
        bullets = [title]
    packets = []
    for idx, item in enumerate(bullets, start=1):
        slug = slugify(item, fallback=f"packet-{idx}")[:36]
        packets.append({
            "packet_id": f"pr{idx:02d}-{slug}",
            "title": item[:100],
            "branch": f"feat/{slug}",
            "status": "planned",
            "depends_on": [],
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
        task_meta = {
            "task_id": task_id,
            "source_plan_id": plan_id,
            "state": "SHAPE",
            "phase_status": {"SHAPE": "READY"},
            "operator_approvals": [],
            "escalations": [],
            "awaiting_operator": False,
            "state_reason": "created from approved plan decomposition",
            "created": now,
            "pr_packet": packet,
        }
        task_md = (
            f"# Task: {packet['title']}\n\n"
            f"- Source plan: `{plan_id}`\n"
            f"- Branch: `{packet['branch']}`\n"
            f"- Target repo: `{meta.get('repo')}`\n"
            f"- Base branch: `{meta.get('base_branch')}`\n\n"
            "## Done means\n"
            "- Implement this packet only.\n"
            "- Open a draft PR or record why PR creation is blocked.\n"
            "- Persist summary, verification, and evidence artifacts.\n"
        )
        write_json(task_dir / "meta.json", task_meta)
        write_text(task_dir / "task.md", task_md)
        write_text(task_dir / "checkpoints.md", f"# checkpoints\n\n- {now} — created from plan decomposition `{plan_id}`\n")
        tasks.append({"task_id": task_id, "task_dir": str(task_dir), "branch": packet["branch"]})

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
