import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pr_status_lib
import plan_status_lib
import plan_automation_lib
from pr_readiness_lib import create_readiness_job, mark_readiness_result, readiness_blocks, validate_review_cleanup_evidence
from pr_status_lib import apply_stacked_pr_blocks, classify_pr, format_status_message, status_fingerprint, sync_discord_status_channel
from plan_status_lib import sync_open_plan_threads
import importlib.util


def load_script_module(name: str, file_name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / file_name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


open_plan_router = load_script_module("open_plan_router_script", "open-plan-router.py")
plan_thread_poller = load_script_module("plan_thread_poller_script", "discord-plan-thread-poller.py")
dispatch_pr_worker = load_script_module("dispatch_pr_worker_script", "dispatch-pr-worker.py")
run_builder_worker = load_script_module("run_builder_worker_script", "run-builder-worker.py")
publish_draft_pr = load_script_module("publish_draft_pr_script", "publish-draft-pr.py")
stall_detector = load_script_module("stall_detector_script", "stall-detector.py")
render_dashboard = load_script_module("render_dashboard_script", "render-dashboard.py")
ensure_build_threads = load_script_module("ensure_build_threads_script", "ensure-build-threads.py")
reconcile_merged_prs = load_script_module("reconcile_merged_prs_script", "reconcile-merged-prs.py")
reconcile_plan_progress = load_script_module("reconcile_plan_progress_script", "reconcile-plan-progress.py")
auto_builder_runner = load_script_module("auto_builder_runner_script", "auto-builder-runner.py")
auto_publish_runner = load_script_module("auto_publish_runner_script", "auto-publish-runner.py")
pre_pr_rebase_autocure = load_script_module("pre_pr_rebase_autocure_script", "pre-pr-rebase-autocure.py")
readiness_runner = load_script_module("readiness_runner_script", "readiness-runner.py")
build_control_autopilot = load_script_module("build_control_autopilot_script", "build-control-autopilot.py")


def run_script(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "discord-plan-intake.py"), *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )


class PlanAutomationTests(unittest.TestCase):
    def test_discord_plan_intake_creates_durable_plan_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_file = tmp_path / "plan.md"
            plan_file.write_text("# Build auth\n\nAdd login and logout.\n", encoding="utf-8")

            result = run_script(
                tmp_path,
                "--repo",
                "dom-armor/example",
                "--base-branch",
                "main",
                "--plan-file",
                str(plan_file),
                "--guild-id",
                "guild-1",
                "--control-channel-id",
                "channel-1",
                "--operator-user-id",
                "user-1",
                "--thread-id",
                "thread-1",
                "--no-discord",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            plan_id = payload["plan_id"]
            plan_dir = tmp_path / "plans" / plan_id
            self.assertTrue(plan_dir.is_dir())
            self.assertEqual(
                (plan_dir / "source-plan.md").read_text(encoding="utf-8"),
                plan_file.read_text(encoding="utf-8"),
            )
            meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "CONTRACT")
            self.assertEqual(meta["repo"], "dom-armor/example")
            self.assertEqual(meta["base_branch"], "main")
            self.assertEqual(meta["discord"]["thread_id"], "thread-1")
            index = json.loads((tmp_path / ".automation" / "plans-index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["plans"][plan_id]["thread_id"], "thread-1")

    def test_plan_contract_identifies_questions_and_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_file = tmp_path / "plan.md"
            plan_file.write_text("Build it soon.\n", encoding="utf-8")
            intake = run_script(
                tmp_path,
                "--repo",
                "dom-armor/example",
                "--plan-file",
                str(plan_file),
                "--guild-id",
                "guild-1",
                "--control-channel-id",
                "channel-1",
                "--operator-user-id",
                "user-1",
                "--thread-id",
                "thread-1",
                "--no-discord",
            )
            self.assertEqual(intake.returncode, 0, intake.stderr)
            plan_id = json.loads(intake.stdout)["plan_id"]

            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "shape-plan-contract.py"), "--plan-id", plan_id],
                cwd=tmp_path,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIs(payload["awaiting_operator"], True)
            meta = json.loads((tmp_path / "plans" / plan_id / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "QUESTION")
            self.assertIs(meta["awaiting_operator"], True)
            questions = (tmp_path / "plans" / plan_id / "questions.md").read_text(encoding="utf-8")
            self.assertIn("acceptance criteria", questions.lower())

    def test_decomposer_creates_pr_sized_task_from_approved_contract(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_file = tmp_path / "plan.md"
            plan_file.write_text("# Add docs\n\n- Update README docs\n- Add usage examples\n", encoding="utf-8")
            intake = run_script(
                tmp_path,
                "--repo",
                "dom-armor/example",
                "--plan-file",
                str(plan_file),
                "--guild-id",
                "guild-1",
                "--control-channel-id",
                "channel-1",
                "--operator-user-id",
                "user-1",
                "--thread-id",
                "thread-1",
                "--no-discord",
            )
            self.assertEqual(intake.returncode, 0, intake.stderr)
            plan_id = json.loads(intake.stdout)["plan_id"]
            shape = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "shape-plan-contract.py"), "--plan-id", plan_id, "--auto-approve"],
                cwd=tmp_path,
                text=True,
                capture_output=True,
            )
            self.assertEqual(shape.returncode, 0, shape.stderr)

            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "decompose-plan-to-prs.py"), "--plan-id", plan_id],
                cwd=tmp_path,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertGreaterEqual(payload["packet_count"], 1)
            task_id = payload["tasks"][0]["task_id"]
            task_meta = json.loads((tmp_path / "tasks" / task_id / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(task_meta["state"], "SHAPE")
            self.assertEqual(task_meta["source_plan_id"], plan_id)
            prs = json.loads((tmp_path / "plans" / plan_id / "prs.json").read_text(encoding="utf-8"))
            self.assertEqual(prs["packets"][0]["task_id"], task_id)
    def test_concrete_contract_requires_explicit_approval_before_decompose(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_file = tmp_path / "plan.md"
            plan_file.write_text("# Add docs\n\n- Update README docs\n- Add usage examples\n", encoding="utf-8")
            intake = run_script(
                tmp_path,
                "--repo",
                "dom-armor/example",
                "--plan-file",
                str(plan_file),
                "--guild-id",
                "guild-1",
                "--control-channel-id",
                "channel-1",
                "--operator-user-id",
                "user-1",
                "--thread-id",
                "thread-1",
                "--no-discord",
            )
            self.assertEqual(intake.returncode, 0, intake.stderr)
            plan_id = json.loads(intake.stdout)["plan_id"]
            shape = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "shape-plan-contract.py"), "--plan-id", plan_id],
                cwd=tmp_path,
                text=True,
                capture_output=True,
            )
            self.assertEqual(shape.returncode, 0, shape.stderr)
            meta = json.loads((tmp_path / "plans" / plan_id / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "CONTRACT_REVIEW")
            self.assertIs(meta["awaiting_operator"], True)

            blocked = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "decompose-plan-to-prs.py"), "--plan-id", plan_id],
                cwd=tmp_path,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("not ready for decomposition", blocked.stderr)

            approve = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "approve-plan-contract.py"), "--plan-id", plan_id, "--decision", "APPROVE", "--source", "test"],
                cwd=tmp_path,
                text=True,
                capture_output=True,
            )
            self.assertEqual(approve.returncode, 0, approve.stderr)
            approved_meta = json.loads((tmp_path / "plans" / plan_id / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(approved_meta["state"], "DECOMPOSE")
            approvals = json.loads((tmp_path / "plans" / plan_id / "approvals.json").read_text(encoding="utf-8"))
            self.assertEqual(approvals["approvals"][-1]["decision"], "APPROVE")

    def test_open_plan_router_classifies_contract_and_dispatch_worker(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            auto = tmp_path / ".automation"
            auto.mkdir()
            for plan_id, state in [("plan-contract", "CONTRACT"), ("plan-exec", "EXECUTING")]:
                plan_dir = tmp_path / "plans" / plan_id
                plan_dir.mkdir(parents=True)
                (plan_dir / "meta.json").write_text(json.dumps({
                    "plan_id": plan_id,
                    "title": plan_id,
                    "state": state,
                    "awaiting_operator": False,
                    "state_reason": "test",
                    "discord": {"thread_id": f"thread-{plan_id}"},
                }), encoding="utf-8")
            (auto / "plans-index.json").write_text(json.dumps({
                "plans": {
                    "plan-contract": {"plan_id": "plan-contract", "plan_dir": str(tmp_path / "plans" / "plan-contract"), "state": "CONTRACT"},
                    "plan-exec": {"plan_id": "plan-exec", "plan_dir": str(tmp_path / "plans" / "plan-exec"), "state": "EXECUTING"},
                }
            }), encoding="utf-8")

            routed = open_plan_router.route_open_plans(tmp_path)

            by_id = {action["plan_id"]: action for action in routed["actions"]}
            self.assertEqual(by_id["plan-contract"]["recommended_action"], "shape_contract")
            self.assertEqual(by_id["plan-contract"]["handler"], "agent_required")
            self.assertEqual(by_id["plan-exec"]["recommended_action"], "dispatch_pr_worker")
            self.assertEqual(by_id["plan-exec"]["handler"], "script")
            self.assertEqual(by_id["plan-exec"]["worker"], "dispatch-pr-worker.py")
            self.assertEqual(routed["execution_capability"], "intake_contract_decompose_dispatch_worktree_builder_command")
            self.assertEqual(routed["missing_workers"], [])

    def test_thread_reply_ingestion_moves_question_back_to_contract(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-question"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Question plan",
                "state": "QUESTION",
                "awaiting_operator": True,
                "state_reason": "needs input",
                "discord": {"thread_id": "thread-q"},
            }), encoding="utf-8")
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({
                "plans": {plan_id: {"plan_id": plan_id, "plan_dir": str(plan_dir), "thread_id": "thread-q", "state": "QUESTION"}},
            }), encoding="utf-8")

            result = plan_thread_poller.handle_operator_reply(tmp_path, {"plan_id": plan_id, "plan_dir": str(plan_dir), "state": "QUESTION"}, {
                "id": "msg-1",
                "content": "Acceptance: add tests and docs only",
                "author": {"id": "op"},
                "timestamp": "2026-06-25T00:00:00+00:00",
            })

            self.assertEqual(result["action"], "operator_reply_ingested")
            meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "CONTRACT")
            self.assertIs(meta["awaiting_operator"], False)
            replies = (plan_dir / "operator-replies.jsonl").read_text(encoding="utf-8")
            self.assertIn("Acceptance: add tests", replies)

    def test_thread_reply_approval_moves_contract_review_to_decompose(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-review"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Review plan",
                "state": "CONTRACT_REVIEW",
                "awaiting_operator": True,
                "state_reason": "awaiting approval",
                "discord": {"thread_id": "thread-r"},
            }), encoding="utf-8")
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({
                "plans": {plan_id: {"plan_id": plan_id, "plan_dir": str(plan_dir), "thread_id": "thread-r", "state": "CONTRACT_REVIEW"}},
            }), encoding="utf-8")

            result = plan_thread_poller.handle_operator_reply(tmp_path, {"plan_id": plan_id, "plan_dir": str(plan_dir), "state": "CONTRACT_REVIEW"}, {
                "id": "msg-approve",
                "content": "approve",
                "author": {"id": "op"},
                "timestamp": "2026-06-25T00:00:00+00:00",
            })

            self.assertEqual(result["action"], "contract_decision_recorded")
            self.assertEqual(result["state"], "DECOMPOSE")
            approvals = json.loads((plan_dir / "approvals.json").read_text(encoding="utf-8"))
            self.assertEqual(approvals["approvals"][-1]["message_id"], "msg-approve")

    def test_pr_status_classifies_rebase_reviews_and_failing_checks_as_issues(self):
        pr = {
            "owner": "dom-armor",
            "repo": "armor-swarm",
            "number": 42,
            "title": "fix: repair queue",
            "html_url": "https://github.com/dom-armor/armor-swarm/pull/42",
            "author": "dom-armor",
            "draft": False,
            "mergeable_state": "dirty",
            "head_sha": "abc123",
            "reviews": [{"user": "reviewer", "state": "CHANGES_REQUESTED", "html_url": "https://github.com/x#review"}],
            "check_runs": [{"name": "test", "status": "completed", "conclusion": "failure", "html_url": "https://github.com/x#check"}],
            "issue_comments": [],
            "review_comments": [],
        }

        status = classify_pr(pr, operator_login="dom-armor")

        self.assertEqual(status["state"], "ISSUES")
        self.assertIn("rebase_required", {issue["kind"] for issue in status["issues"]})
        self.assertIn("changes_requested", {issue["kind"] for issue in status["issues"]})
        self.assertIn("check_failed", {issue["kind"] for issue in status["issues"]})
        rendered = format_status_message(status)
        self.assertIn("needs attention", rendered)
        self.assertIn("rebase", rendered.lower())

    def test_pr_status_fingerprint_changes_only_when_actionable_issues_change(self):
        clean = {"id": "dom-armor/armor-swarm#1", "issues": [], "head_sha": "a"}
        comment_only = {"id": "dom-armor/armor-swarm#1", "issues": [{"kind": "comment", "body": "nit"}], "head_sha": "b"}
        requested = {"id": "dom-armor/armor-swarm#1", "issues": [{"kind": "changes_requested", "body": "fix"}], "head_sha": "b"}

        self.assertEqual(status_fingerprint(clean), status_fingerprint({**clean, "head_sha": "b"}))
        self.assertNotEqual(status_fingerprint(comment_only), status_fingerprint(requested))

    def test_pr_status_suppresses_action_comment_after_operator_response(self):
        pr = {
            "owner": "dom-armor",
            "repo": "armor-swarm",
            "number": 44,
            "title": "fix: answered comment",
            "html_url": "https://github.com/dom-armor/armor-swarm/pull/44",
            "author": "dom-armor",
            "draft": False,
            "mergeable_state": "clean",
            "head_sha": "new-sha",
            "reviews": [],
            "check_runs": [],
            "issue_comments": [
                {"user": "reviewer", "body": "please fix the stale case", "created_at": "2026-06-25T14:00:00Z", "html_url": "https://github.com/x#old"},
                {"user": "dom-armor", "body": "Fixed in current head, ready for re-review", "created_at": "2026-06-25T15:00:00Z", "html_url": "https://github.com/x#reply"},
            ],
            "review_comments": [],
        }

        status = classify_pr(pr, operator_login="dom-armor")

        self.assertEqual(status["state"], "OK")
        self.assertEqual(status["issues"], [])

    def test_pr_status_marks_old_review_as_waiting_for_re_review(self):
        pr = {
            "owner": "dom-armor",
            "repo": "armor-swarm",
            "number": 43,
            "title": "fix: review cleanup",
            "html_url": "https://github.com/dom-armor/armor-swarm/pull/43",
            "author": "dom-armor",
            "draft": False,
            "mergeable_state": "clean",
            "head_sha": "new-sha",
            "reviews": [{"user": "reviewer", "state": "CHANGES_REQUESTED", "commit_id": "old-sha", "html_url": "https://github.com/x#review"}],
            "check_runs": [],
            "issue_comments": [],
            "review_comments": [],
        }

        status = classify_pr(pr, operator_login="dom-armor")

        self.assertEqual(status["state"], "WAITING")
        self.assertEqual(status["issues"][0]["kind"], "awaiting_re_review")
        self.assertIn("awaiting re-review", format_status_message(status))

    def test_pr_status_marks_stacked_child_waiting_on_blocked_parent(self):
        parent = {
            "id": "dom-armor/armor-swarm#1",
            "head_ref": "feat/base",
            "base_ref": "main",
            "state": "ISSUES",
            "issues": [{"kind": "changes_requested", "summary": "reviewer requested changes"}],
            "url": "https://github.com/dom-armor/armor-swarm/pull/1",
        }
        child = {
            "id": "dom-armor/armor-swarm#2",
            "head_ref": "feat/child",
            "base_ref": "feat/base",
            "state": "OK",
            "issues": [],
            "url": "https://github.com/dom-armor/armor-swarm/pull/2",
        }

        apply_stacked_pr_blocks([child, parent])

        self.assertEqual(child["state"], "WAITING")
        self.assertEqual(child["issues"][0]["kind"], "stacked_base_blocked")
        self.assertEqual(child["issues"][0]["parent"], "dom-armor/armor-swarm#1")

    def test_pr_status_suppresses_repeated_alert_while_action_pending(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            sent = []
            edited = []
            old_post, old_edit, old_thread = pr_status_lib.post_message, pr_status_lib.edit_message, pr_status_lib.ensure_thread
            try:
                pr_status_lib.post_message = lambda token, channel_id, content: sent.append((channel_id, content)) or f"msg-{len(sent)}"
                pr_status_lib.edit_message = lambda token, channel_id, message_id, content: edited.append((channel_id, message_id, content))
                pr_status_lib.ensure_thread = lambda token, channel_id, message_id, name: "thread-1"
                status = {
                    "id": "dom-armor/armor-swarm#9",
                    "state": "ISSUES",
                    "url": "https://github.com/dom-armor/armor-swarm/pull/9",
                    "title": "fix: issue",
                    "head_ref": "feat/x",
                    "base_ref": "main",
                    "head_sha": "sha-1",
                    "issues": [{"kind": "changes_requested", "summary": "reviewer requested changes", "url": "https://github.com/x#r1"}],
                }
                first = sync_discord_status_channel(tmp_path, [status], channel_id="chan", operator_user_id="op", token="tok")
                second_status = {**status, "head_sha": "sha-2", "issues": [{"kind": "changes_requested", "summary": "reviewer requested changes plus note", "url": "https://github.com/x#r1"}]}
                second = sync_discord_status_channel(tmp_path, [second_status], channel_id="chan", operator_user_id="op", token="tok")
            finally:
                pr_status_lib.post_message, pr_status_lib.edit_message, pr_status_lib.ensure_thread = old_post, old_edit, old_thread

            self.assertIn("alerted_thread", {a["action"] for a in first["actions"]})
            self.assertIn("suppressed_alert_action_pending", {a["action"] for a in second["actions"]})
            # First sync creates one status message and one alert; second sync edits only.
            self.assertEqual(len(sent), 2)
            ledger = json.loads((tmp_path / ".automation" / "pr-status-ledger.json").read_text(encoding="utf-8"))
            active = ledger["prs"]["dom-armor/armor-swarm#9"]["active_alert"]
            self.assertEqual(active["state"], "FIX_PUSHED_WAITING_CI_OR_REVIEW")
            self.assertEqual(active["resolution_sha"], "sha-2")

    def test_pr_status_archives_thread_after_pr_merge(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            ledger_path = tmp_path / ".automation" / "pr-status-ledger.json"
            ledger_path.parent.mkdir(parents=True)
            ledger_path.write_text(json.dumps({
                "channel_id": "chan",
                "prs": {
                    "dom-armor/armor-swarm#10": {
                        "message_id": "msg-10",
                        "thread_id": "thread-10",
                        "last_state": "WAITING",
                        "active_alert": {"state": "FIX_PUSHED_WAITING_CI_OR_REVIEW"},
                    }
                },
            }), encoding="utf-8")
            edited = []
            archived = []
            old_fetch, old_edit, old_archive = pr_status_lib.fetch_pr_merge_state, pr_status_lib.edit_message, pr_status_lib.archive_thread
            try:
                pr_status_lib.fetch_pr_merge_state = lambda pr_key: {
                    "merged": True,
                    "merged_at": "2026-06-25T14:30:00Z",
                    "url": "https://github.com/dom-armor/armor-swarm/pull/10",
                    "title": "fix: merged",
                    "head_ref": "fix/merged",
                    "base_ref": "main",
                    "head_sha": "sha-merged",
                }
                pr_status_lib.edit_message = lambda token, channel_id, message_id, content: edited.append((channel_id, message_id, content))
                pr_status_lib.archive_thread = lambda token, thread_id: archived.append(thread_id)
                result = sync_discord_status_channel(tmp_path, [], channel_id="chan", operator_user_id="op", token="tok")
            finally:
                pr_status_lib.fetch_pr_merge_state, pr_status_lib.edit_message, pr_status_lib.archive_thread = old_fetch, old_edit, old_archive

            self.assertIn("updated_message_merged", {a["action"] for a in result["actions"]})
            self.assertIn("archived_thread_after_merge", {a["action"] for a in result["actions"]})
            self.assertEqual(archived, ["thread-10"])
            self.assertIn("merged", edited[0][2])
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            entry = ledger["prs"]["dom-armor/armor-swarm#10"]
            self.assertEqual(entry["last_state"], "MERGED")
            self.assertEqual(entry["active_alert"]["state"], "RESOLVED")
            self.assertEqual(entry["active_alert"]["resolved_by"], "merged")
            self.assertIn("thread_archived_at", entry)

    def test_dispatch_worker_blocks_unresolved_operator_defined_repo(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-dispatch"
            task_id = "task-unresolved"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / task_id
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (plan_dir / "meta.json").write_text(json.dumps({"plan_id": plan_id, "repo": "operator-defined", "base_branch": "main"}), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "source_plan_id": plan_id,
                "state": "SHAPE",
                "phase_status": {"SHAPE": "READY"},
                "awaiting_operator": False,
                "pr_packet": {"branch": "feat/demo", "title": "Demo"},
            }), encoding="utf-8")
            (task_dir / "task.md").write_text("# Task\n", encoding="utf-8")

            result = dispatch_pr_worker.dispatch_one(tmp_path, execute=False)

            self.assertEqual(result["decision"], "BLOCKED")
            self.assertIn("target repo", result["reason"])
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "SHAPE")

    def test_dispatch_worker_prepares_isolated_worktree_and_readiness_job(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target_repo = tmp_path / "target-repo"
            target_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=target_repo, check=True)
            (target_repo / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=target_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", str(target_repo)], cwd=target_repo, check=True)

            plan_id = "plan-dispatch"
            task_id = "task-local"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / task_id
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (plan_dir / "meta.json").write_text(json.dumps({"plan_id": plan_id, "repo": str(target_repo), "base_branch": "main"}), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "source_plan_id": plan_id,
                "state": "SHAPE",
                "phase_status": {"SHAPE": "READY"},
                "awaiting_operator": False,
                "pr_packet": {"branch": "feat/demo-dispatch", "title": "Demo dispatch"},
            }), encoding="utf-8")
            (task_dir / "task.md").write_text("# Task\n\n- change docs\n", encoding="utf-8")

            result = dispatch_pr_worker.dispatch_one(tmp_path, execute=True)

            self.assertEqual(result["decision"], "DISPATCHED")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "EXECUTE")
            self.assertTrue(Path(meta["dispatch"]["worktree"]).exists())
            self.assertTrue((task_dir / "builder-prompt.md").exists())
            self.assertTrue((task_dir / "summary.md").exists())
            self.assertTrue((task_dir / "evidence.md").exists())
            readiness_job = tmp_path / ".automation" / "pr-readiness" / f"{meta['dispatch']['readiness_job_id']}.json"
            self.assertTrue(readiness_job.exists())

    def test_run_builder_worker_executes_command_commits_changes_and_queues_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target_repo = tmp_path / "target-repo"
            target_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=target_repo, check=True)
            (target_repo / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=target_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", str(target_repo)], cwd=target_repo, check=True)

            plan_id = "plan-build"
            task_id = "task-build"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / task_id
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (plan_dir / "meta.json").write_text(json.dumps({"plan_id": plan_id, "repo": str(target_repo), "base_branch": "main"}), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "source_plan_id": plan_id,
                "state": "SHAPE",
                "phase_status": {"SHAPE": "READY"},
                "awaiting_operator": False,
                "pr_packet": {"branch": "feat/demo-build", "title": "Demo build"},
            }), encoding="utf-8")
            (task_dir / "task.md").write_text("# Task\n\n- add build output docs\n", encoding="utf-8")
            dispatched = dispatch_pr_worker.dispatch_one(tmp_path, task_id=task_id, execute=True)
            self.assertEqual(dispatched["decision"], "DISPATCHED")

            command = (
                f"{sys.executable} -c \"from pathlib import Path; "
                "Path('BUILD_OUTPUT.md').write_text('builder wrote this\\n', encoding='utf-8')\""
            )
            result = run_builder_worker.run_builder(tmp_path, task_id=task_id, builder_command=command)

            self.assertEqual(result["decision"], "BUILT")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "VERIFYING")
            self.assertEqual(meta["phase_status"]["EXECUTE"], "BUILT")
            self.assertTrue(meta["build"]["commit_sha"])
            self.assertTrue((Path(meta["dispatch"]["worktree"]) / "BUILD_OUTPUT.md").exists())
            git_log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=Path(meta["dispatch"]["worktree"]), text=True, capture_output=True, check=True)
            self.assertIn("Demo build", git_log.stdout)
            evidence = json.loads((task_dir / "build-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["builder_command"], command)
            self.assertIn("BUILD_OUTPUT.md", "\n".join(evidence["changed_files"]))
            readiness_job = tmp_path / ".automation" / "pr-readiness" / f"{meta['build']['readiness_job_id']}.json"
            self.assertTrue(readiness_job.exists())

    def test_run_builder_worker_requeues_readiness_when_built_task_has_no_new_changes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target_repo = tmp_path / "target-repo"
            target_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=target_repo, check=True)
            (target_repo / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=target_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", str(target_repo)], cwd=target_repo, check=True)

            task_id = "task-noop-built"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            worktree = tmp_path / "worktree"
            subprocess.run(["git", "clone", str(target_repo), str(worktree)], check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "-b", "feat/noop-built"], cwd=worktree, check=True, text=True, capture_output=True)
            (worktree / "BUILT.md").write_text("already built\n", encoding="utf-8")
            subprocess.run(["git", "add", "BUILT.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "feat: already built"], cwd=worktree, check=True, text=True, capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()
            job = create_readiness_job(tmp_path, task_id=task_id, branch="feat/noop-built", sha=sha)
            (task_dir / "builder-prompt.md").write_text("# no-op retry\n", encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "READY_FOR_BUILDER",
                "awaiting_operator": False,
                "phase_status": {"EXECUTE": "READY_FOR_BUILDER", "VERIFY": "FAILED"},
                "dispatch": {"worktree": str(worktree), "branch": "feat/noop-built", "base_branch": "main"},
                "build": {"commit_sha": sha, "readiness_job_id": job["job_id"]},
            }), encoding="utf-8")
            command = f"{sys.executable} -c \"print('nothing to change')\""

            result = run_builder_worker.run_builder(tmp_path, task_id=task_id, builder_command=command)

            self.assertEqual(result["decision"], "NO_CHANGES_READINESS_REQUEUED")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "VERIFYING")
            self.assertFalse(meta["awaiting_operator"])
            self.assertEqual(meta["phase_status"]["VERIFY"], "QUEUED")
            self.assertEqual(meta["build"]["commit_sha"], sha)
            self.assertEqual(meta["build"]["readiness_job_id"], job["job_id"])
            requeued_job = json.loads((tmp_path / ".automation" / "pr-readiness" / f"{meta['build']['readiness_job_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(requeued_job["state"], "READINESS_QUEUED")
            evidence = json.loads((task_dir / "build-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["after_sha"], sha)
            self.assertEqual(evidence["readiness_job_id"], job["job_id"])
            self.assertIn("evidence refreshed after no-change builder retry", evidence["note"])
            summary = (task_dir / "builder-summary.md").read_text(encoding="utf-8")
            self.assertIn(sha, summary)

    def test_auto_builder_blocks_ready_task_without_configured_command(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_id = "task-ready-builder"
            task_dir = tmp_path / "tasks" / task_id
            worktree = tmp_path / "worktree"
            task_dir.mkdir(parents=True)
            worktree.mkdir()
            (task_dir / "builder-prompt.md").write_text("build this\n", encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "DISPATCHED",
                "awaiting_operator": False,
                "phase_status": {"EXECUTE": "READY_FOR_BUILDER"},
                "dispatch": {"worktree": str(worktree), "branch": "feat/ready"},
            }), encoding="utf-8")

            result = auto_builder_runner.auto_run_builder(tmp_path, execute=True)

            self.assertEqual(result["decision"], "BLOCKED")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "READY_FOR_BUILDER")
            self.assertIn("builder command is not configured", meta["state_reason"])

    def test_auto_builder_uses_run_builder_worker_eligibility(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            stale_dir = tmp_path / "tasks" / "task-aaa-stale-phase"
            ready_dir = tmp_path / "tasks" / "task-ready"
            stale_wt = tmp_path / "wt-stale"
            ready_wt = tmp_path / "wt-ready"
            stale_dir.mkdir(parents=True)
            ready_dir.mkdir(parents=True)
            stale_wt.mkdir()
            ready_wt.mkdir()
            (stale_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-stale-phase",
                "state": "SHAPE",
                "awaiting_operator": False,
                "phase_status": {"EXECUTE": "READY_FOR_BUILDER"},
                "dispatch": {"worktree": str(stale_wt), "branch": "feat/stale"},
            }), encoding="utf-8")
            (ready_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-ready",
                "state": "READY_FOR_BUILDER",
                "awaiting_operator": False,
                "phase_status": {"EXECUTE": "READY_FOR_BUILDER"},
                "dispatch": {"worktree": str(ready_wt), "branch": "feat/ready"},
            }), encoding="utf-8")

            selected = auto_builder_runner.ready_task(tmp_path)

            self.assertIsNotNone(selected)
            self.assertEqual(selected[1]["task_id"], "task-ready")

    def test_plan_progress_normalizes_nonwaiting_escalated_ready_builder_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-normalize"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / "task-escalated-ready"
            (tmp_path / ".automation").mkdir(parents=True)
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({"plans": {plan_id: {
                "plan_id": plan_id,
                "plan_dir": str(plan_dir),
                "state": "EXECUTING",
            }}}), encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Normalize stale child state",
                "state": "EXECUTING",
            }), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-escalated-ready",
                "source_plan_id": plan_id,
                "state": "ESCALATED",
                "awaiting_operator": False,
                "phase_status": {"DECISION": "ANSWERED", "EXECUTE": "READY_FOR_BUILDER", "VERIFY": "FAILED"},
                "dispatch": {"worktree": str(tmp_path / "worktree"), "branch": "feat/normalize"},
                "state_reason": "operator reply ingested from task thread; ready for next build-control action",
            }), encoding="utf-8")

            result = reconcile_plan_progress.reconcile_plan_progress(tmp_path)

            self.assertIn("normalized_builder_ready_task", {a["action"] for a in result["actions"]})
            task_meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(task_meta["state"], "READY_FOR_BUILDER")
            self.assertFalse(task_meta["awaiting_operator"])
            self.assertEqual(task_meta["phase_status"]["EXECUTE"], "READY_FOR_BUILDER")
            plan_meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertIn("task-escalated-ready is READY_FOR_BUILDER", plan_meta["state_reason"])

    def test_auto_builder_runs_configured_command_for_ready_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target_repo = tmp_path / "target-repo"
            target_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=target_repo, check=True)
            (target_repo / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=target_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", str(target_repo)], cwd=target_repo, check=True)
            plan_id = "plan-auto-build"
            task_id = "task-auto-build"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / task_id
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (plan_dir / "meta.json").write_text(json.dumps({"plan_id": plan_id, "repo": str(target_repo), "base_branch": "main"}), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "source_plan_id": plan_id,
                "state": "SHAPE",
                "phase_status": {"SHAPE": "READY"},
                "awaiting_operator": False,
                "pr_packet": {"branch": "feat/auto-build", "title": "Auto build"},
            }), encoding="utf-8")
            (task_dir / "task.md").write_text("# Task\n\n- auto build\n", encoding="utf-8")
            dispatch_pr_worker.dispatch_one(tmp_path, task_id=task_id, execute=True)
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            meta["state"] = "READY_FOR_BUILDER"
            meta["phase_status"] = {**meta.get("phase_status", {}), "EXECUTE": "READY_FOR_BUILDER"}
            (task_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (tmp_path / ".automation").mkdir(exist_ok=True)
            command = f"{sys.executable} -c \"from pathlib import Path; Path('AUTO_BUILD.md').write_text('auto built\\n', encoding='utf-8')\""
            (tmp_path / ".automation" / "builder-config.json").write_text(json.dumps({"enabled": True, "builder_command": command}), encoding="utf-8")

            result = auto_builder_runner.auto_run_builder(tmp_path, execute=True)

            self.assertEqual(result["decision"], "BUILT")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "VERIFYING")
            self.assertTrue((Path(meta["dispatch"]["worktree"]) / "AUTO_BUILD.md").exists())

    def test_pre_pr_rebase_autocure_rebases_clean_built_task_and_requeues_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            origin = tmp_path / "origin-repo"
            worktree = tmp_path / "worktree"
            origin.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=origin, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=origin, check=True)
            (origin / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=origin, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=origin, check=True, text=True, capture_output=True)
            subprocess.run(["git", "clone", str(origin), str(worktree)], check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            subprocess.run(["git", "checkout", "-b", "feat/pre-pr"], cwd=worktree, check=True, text=True, capture_output=True)
            (worktree / "FEATURE.md").write_text("feature\n", encoding="utf-8")
            subprocess.run(["git", "add", "FEATURE.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "feat: pre pr work"], cwd=worktree, check=True, text=True, capture_output=True)
            old_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()

            (origin / "BASE.md").write_text("base moved\n", encoding="utf-8")
            subprocess.run(["git", "add", "BASE.md"], cwd=origin, check=True)
            subprocess.run(["git", "commit", "-m", "docs: move base"], cwd=origin, check=True, text=True, capture_output=True)

            task_id = "task-pre-pr-rebase"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            job = create_readiness_job(tmp_path, task_id=task_id, branch="feat/pre-pr", sha=old_sha)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "awaiting_operator": False,
                "dispatch": {"worktree": str(worktree), "branch": "feat/pre-pr", "base_branch": "main"},
                "build": {"commit_sha": old_sha, "readiness_job_id": job["job_id"]},
            }), encoding="utf-8")

            result = pre_pr_rebase_autocure.autocure_pre_pr_rebase(tmp_path, execute=True)

            self.assertEqual(result["decision"], "REBASED_READINESS_QUEUED")
            self.assertEqual(result["before_sha"], old_sha)
            self.assertNotEqual(result["after_sha"], old_sha)
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "VERIFYING")
            self.assertEqual(meta["build"]["commit_sha"], result["after_sha"])
            self.assertEqual(meta["build"]["readiness_job_id"], result["readiness_job_id"])
            new_job_path = tmp_path / ".automation" / "pr-readiness" / f"{result['readiness_job_id']}.json"
            new_job = json.loads(new_job_path.read_text(encoding="utf-8"))
            self.assertEqual(new_job["state"], "READINESS_QUEUED")
            self.assertEqual(new_job["sha"], result["after_sha"])
            evidence = json.loads((task_dir / "build-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["after_sha"], result["after_sha"])
            self.assertEqual(evidence["readiness_job_id"], result["readiness_job_id"])
            self.assertIn("evidence refreshed after pre-PR rebase autocure", evidence["note"])
            summary = (task_dir / "builder-summary.md").read_text(encoding="utf-8")
            self.assertIn(result["after_sha"], summary)
            self.assertIn(result["readiness_job_id"], summary)

    def test_readiness_runner_blocks_without_configured_verifier(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, text=True, capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()
            task_id = "task-readiness-blocked"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            job = create_readiness_job(tmp_path, task_id=task_id, branch="main", sha=sha)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "awaiting_operator": False,
                "dispatch": {"worktree": str(worktree), "branch": "main", "base_branch": "main"},
                "build": {"commit_sha": sha, "readiness_job_id": job["job_id"]},
            }), encoding="utf-8")

            result = readiness_runner.run_readiness(tmp_path, execute=True)

            self.assertEqual(result["decision"], "BLOCKED")
            self.assertIn("readiness verifier is not configured", result["reason"])

    def test_readiness_runner_marks_pr_ready_from_structured_verifier_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, text=True, capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()
            task_id = "task-readiness-pass"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            job = create_readiness_job(tmp_path, task_id=task_id, branch="main", sha=sha)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "awaiting_operator": False,
                "dispatch": {"worktree": str(worktree), "branch": "main", "base_branch": "main"},
                "build": {"commit_sha": sha, "readiness_job_id": job["job_id"]},
            }), encoding="utf-8")
            verifier = tmp_path / "verifier.py"
            verifier.write_text(
                "import json, os\n"
                "print(json.dumps({'passed': True, 'issues': [], 'evidence': {'job': os.environ['READINESS_JOB_ID'], 'method': 'test-verifier'}}))\n",
                encoding="utf-8",
            )
            (tmp_path / ".automation").mkdir(exist_ok=True)
            (tmp_path / ".automation" / "readiness-config.json").write_text(json.dumps({"enabled": True, "verifier_command": f"{sys.executable} {verifier}"}), encoding="utf-8")

            result = readiness_runner.run_readiness(tmp_path, execute=True)

            self.assertEqual(result["decision"], "PR_READY")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "PR_READY")
            marked = json.loads((tmp_path / ".automation" / "pr-readiness" / f"{job['job_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(marked["state"], "PR_READY")
            self.assertTrue(marked["passed"])
            self.assertEqual(marked["evidence"]["method"], "test-verifier")

    def test_readiness_runner_writes_failed_issues_into_next_builder_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, text=True, capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()
            task_id = "task-readiness-fail"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            (task_dir / "builder-prompt.md").write_text("# Original builder prompt\n", encoding="utf-8")
            job = create_readiness_job(tmp_path, task_id=task_id, branch="main", sha=sha)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "awaiting_operator": False,
                "dispatch": {"worktree": str(worktree), "branch": "main", "base_branch": "main"},
                "build": {"commit_sha": sha, "readiness_job_id": job["job_id"]},
            }), encoding="utf-8")
            verifier = tmp_path / "verifier.py"
            verifier.write_text(
                "import json\n"
                "print(json.dumps({'passed': False, 'issues': [{'kind': 'regression', 'severity': 'P0', 'message': 'fix the runtime path', 'evidence': 'probe failed'}], 'evidence': {'method': 'test-verifier'}}))\n",
                encoding="utf-8",
            )
            (tmp_path / ".automation").mkdir(exist_ok=True)
            (tmp_path / ".automation" / "readiness-config.json").write_text(json.dumps({"enabled": True, "verifier_command": f"{sys.executable} {verifier}"}), encoding="utf-8")

            result = readiness_runner.run_readiness(tmp_path, execute=True)

            self.assertEqual(result["decision"], "READINESS_FAILED")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "READY_FOR_BUILDER")
            prompt = (task_dir / "builder-prompt.md").read_text(encoding="utf-8")
            self.assertIn("Readiness feedback", prompt)
            self.assertIn("fix the runtime path", prompt)
            self.assertIn("probe failed", prompt)
            self.assertTrue((task_dir / "readiness-feedback.md").exists())

    def test_publish_draft_pr_blocks_until_readiness_passes_for_current_sha(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "feat/demo-publish"], cwd=worktree, check=True, text=True, capture_output=True)
            (worktree / "PUBLISH.md").write_text("publish me\n", encoding="utf-8")
            subprocess.run(["git", "add", "PUBLISH.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "feat: publish demo"], cwd=worktree, check=True, text=True, capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()

            task_id = "task-publish"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            failed_job = create_readiness_job(tmp_path, task_id=task_id, branch="feat/demo-publish", sha=sha)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "dispatch": {"worktree": str(worktree), "branch": "feat/demo-publish", "base_branch": "main"},
                "build": {"commit_sha": sha, "readiness_job_id": failed_job["job_id"]},
                "pr_packet": {"title": "Publish demo"},
            }), encoding="utf-8")

            result = publish_draft_pr.publish_draft_pr(tmp_path, task_id=task_id, execute=False)

            self.assertEqual(result["decision"], "BLOCKED")
            self.assertEqual(result["reason"], "readiness gate blocks draft PR publishing")

    def test_publish_draft_pr_dry_run_when_readiness_passes_for_current_sha(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            worktree = tmp_path / "worktree"
            worktree.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=worktree, check=True)
            (worktree / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=worktree, check=True, text=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "feat/demo-publish"], cwd=worktree, check=True, text=True, capture_output=True)
            (worktree / "PUBLISH.md").write_text("publish me\n", encoding="utf-8")
            subprocess.run(["git", "add", "PUBLISH.md"], cwd=worktree, check=True)
            subprocess.run(["git", "commit", "-m", "feat: publish demo"], cwd=worktree, check=True, text=True, capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, check=True, text=True, capture_output=True).stdout.strip()

            task_id = "task-publish"
            task_dir = tmp_path / "tasks" / task_id
            task_dir.mkdir(parents=True)
            job = create_readiness_job(tmp_path, task_id=task_id, branch="feat/demo-publish", sha=sha)
            mark_readiness_result(tmp_path, job["job_id"], passed=True, issues=[], evidence={"audit": "passed"})
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "dispatch": {"worktree": str(worktree), "branch": "feat/demo-publish", "base_branch": "main"},
                "build": {"commit_sha": sha, "readiness_job_id": job["job_id"]},
                "pr_packet": {"title": "Publish demo"},
            }), encoding="utf-8")

            result = publish_draft_pr.publish_draft_pr(tmp_path, task_id=task_id, execute=False, push_remote="fork", head="dom-armor:feat/demo-publish")

            self.assertEqual(result["decision"], "WOULD_PUBLISH")
            self.assertEqual(result["branch"], "feat/demo-publish")
            self.assertEqual(result["base_branch"], "main")
            self.assertEqual(result["push_remote"], "fork")
            self.assertEqual(result["head"], "dom-armor:feat/demo-publish")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "VERIFYING")

    def test_auto_publish_blocks_without_publish_config(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_id = "task-built"
            task_dir = tmp_path / "tasks" / task_id
            worktree = tmp_path / "worktree"
            task_dir.mkdir(parents=True)
            worktree.mkdir()
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": task_id,
                "state": "VERIFYING",
                "awaiting_operator": False,
                "dispatch": {"worktree": str(worktree), "branch": "feat/built"},
                "build": {"readiness_job_id": "readiness-1"},
            }), encoding="utf-8")

            result = auto_publish_runner.auto_publish(tmp_path, execute=True)

            self.assertEqual(result["decision"], "BLOCKED")
            self.assertIn("draft PR publishing is not configured", result["reason"])

    def test_build_control_autopilot_decomposes_dispatches_and_blocks_on_missing_builder(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target_repo = tmp_path / "target-repo"
            target_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=target_repo, check=True)
            (target_repo / "README.md").write_text("# demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=target_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=target_repo, check=True, text=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", str(target_repo)], cwd=target_repo, check=True)
            plan_id = "plan-auto"
            plan_dir = tmp_path / "plans" / plan_id
            (tmp_path / ".automation").mkdir(parents=True)
            plan_dir.mkdir(parents=True)
            (plan_dir / "source-plan.md").write_text("# Auto plan\n\n- add docs\n", encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Auto plan",
                "state": "DECOMPOSE",
                "repo": str(target_repo),
                "base_branch": "main",
                "discord": {"thread_id": "thread-plan"},
            }), encoding="utf-8")
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({"plans": {plan_id: {"plan_id": plan_id, "plan_dir": str(plan_dir), "state": "DECOMPOSE"}}}), encoding="utf-8")

            result = build_control_autopilot.advance_build_control(tmp_path, execute=True)

            action_names = [action["action"] for action in result["actions"]]
            self.assertIn("decomposed_plan", action_names)
            self.assertIn("dispatch", action_names)
            self.assertIn("pre_pr_rebase_autocure", action_names)
            self.assertIn("readiness_runner", action_names)
            tasks = list((tmp_path / "tasks").glob("*/meta.json"))
            self.assertEqual(len(tasks), 1)
            meta = json.loads(tasks[0].read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "READY_FOR_BUILDER")
            self.assertIn("builder command is not configured", meta["state_reason"])

    def test_build_control_autopilot_holds_when_lock_exists(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            lock_dir = tmp_path / ".automation" / "locks" / "build-control-autopilot.lock"
            lock_dir.mkdir(parents=True)

            result = build_control_autopilot.advance_build_control(tmp_path, execute=True)

            self.assertEqual(result["decision"], "HOLD")
            self.assertIn("already active", result["reason"])
            self.assertTrue(lock_dir.exists())

    def test_stall_detector_flags_stale_active_tasks_and_worker_ledgers(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-stale"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-stale",
                "state": "VERIFY-LOOP",
                "awaiting_operator": False,
                "updated_at": "2026-06-20T00:00:00+00:00",
                "state_reason": "old verification run",
            }), encoding="utf-8")
            status_dir = tmp_path / ".automation" / "status"
            status_dir.mkdir(parents=True)
            (status_dir / "old-worker-last.json").write_text(json.dumps({
                "kind": "OLD-WORKER",
                "checked_at": "2026-06-20T00:00:00+00:00",
            }), encoding="utf-8")

            result = stall_detector.detect_stalls(tmp_path, now="2026-06-25T00:00:00+00:00", stale_hours=24, write_status=True)

            self.assertEqual(result["stall_count"], 2)
            kinds = {stall["kind"] for stall in result["stalls"]}
            self.assertIn("stale_task", kinds)
            self.assertIn("stale_worker_status", kinds)
            self.assertTrue((status_dir / "stall-detector-last.json").exists())

    def test_stall_detector_ignores_terminal_and_recent_items(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            for task_id, state, updated_at in [
                ("task-done", "DONE", "2026-06-20T00:00:00+00:00"),
                ("task-recent", "VERIFY-LOOP", "2026-06-24T23:30:00+00:00"),
            ]:
                task_dir = tmp_path / "tasks" / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "meta.json").write_text(json.dumps({
                    "task_id": task_id,
                    "state": state,
                    "awaiting_operator": False,
                    "updated_at": updated_at,
                }), encoding="utf-8")
            status_dir = tmp_path / ".automation" / "status"
            status_dir.mkdir(parents=True)
            (status_dir / "recent-worker-last.json").write_text(json.dumps({
                "kind": "RECENT-WORKER",
                "checked_at": "2026-06-24T23:40:00+00:00",
            }), encoding="utf-8")

            result = stall_detector.detect_stalls(tmp_path, now="2026-06-25T00:00:00+00:00", stale_hours=24, write_status=True)

            self.assertEqual(result["stall_count"], 0)
            self.assertEqual(result["decision"], "CLEAR")

    def test_dashboard_does_not_count_cancelled_tasks_as_active(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            tasks_root = tmp_path / "tasks"
            for task_id, state in [("task-cancelled", "CANCELLED"), ("task-done", "DONE"), ("task-active", "VERIFY-LOOP")]:
                task_dir = tasks_root / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "meta.json").write_text(json.dumps({
                    "task_id": task_id,
                    "state": state,
                    "awaiting_operator": False,
                }), encoding="utf-8")

            active = render_dashboard.collect_active_tasks(tasks_root)

            self.assertEqual([task["task_id"] for task in active], ["task-active"])

    def test_dashboard_build_control_items_stop_at_pr_status_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            tasks_root = tmp_path / "tasks"
            fixtures = {
                "task-pre-handoff": {
                    "task_id": "task-pre-handoff",
                    "state": "VERIFYING",
                    "awaiting_operator": False,
                    "pr_packet": {"title": "Before handoff", "branch": "feat/before-handoff"},
                },
                "task-handed-off": {
                    "task_id": "task-handed-off",
                    "state": "PR_DRAFT",
                    "awaiting_operator": False,
                    "github": {"draft_pr_url": "https://github.example/pr/1"},
                    "pr_packet": {"title": "After handoff", "branch": "feat/after-handoff"},
                },
            }
            for task_id, payload in fixtures.items():
                task_dir = tasks_root / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
            stall_status = {
                "stalls": [
                    {"kind": "stale_task", "task_id": "task-pre-handoff", "reason": "old verify", "severity": "P2"}
                ]
            }

            items = render_dashboard.collect_build_control_items(tasks_root, stall_status)

            self.assertEqual([item["task_id"] for item in items], ["task-pre-handoff"])
            self.assertEqual(items[0]["handoff_state"], "BUILD_CONTROL")
            self.assertTrue(items[0]["stalled"])
            self.assertEqual(items[0]["branch"], "feat/before-handoff")

    def test_dispatch_worker_create_draft_pr_flag_is_gated(self):
        with tempfile.TemporaryDirectory() as td:
            result = dispatch_pr_worker.dispatch_one(Path(td), create_draft_pr=True)
            self.assertEqual(result["decision"], "BLOCKED")
            self.assertIn("verification evidence", result["reason"])

    def test_open_plan_status_alerts_once_then_suppresses_until_operator_responds(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-needs-input"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            meta = {
                "plan_id": plan_id,
                "title": "Needs input",
                "state": "QUESTION",
                "awaiting_operator": True,
                "state_reason": "needs acceptance criteria",
                "updated_at": "2026-06-25T00:00:00+00:00",
                "discord": {"thread_id": "thread-plan"},
            }
            (plan_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({
                "plans": {plan_id: {"plan_id": plan_id, "plan_dir": str(plan_dir), "thread_id": "thread-plan", "state": "QUESTION"}},
            }), encoding="utf-8")
            sent = []
            old_post = plan_status_lib.post_message
            try:
                plan_status_lib.post_message = lambda token, channel_id, content: sent.append((channel_id, content)) or "msg-plan"
                first = sync_open_plan_threads(tmp_path, operator_user_id="op", token="tok")
                second = sync_open_plan_threads(tmp_path, operator_user_id="op", token="tok")
            finally:
                plan_status_lib.post_message = old_post

            self.assertIn("alerted_plan_thread", {a["action"] for a in first["actions"]})
            self.assertIn("suppressed_plan_alert_pending", {a["action"] for a in second["actions"]})
            self.assertEqual(len([item for item in sent if "needs operator input" in item[1]]), 1)
            self.assertTrue(any("Persistent plan card" in item[1] for item in sent))
            self.assertTrue(any("<@op>" in item[1] for item in sent))
            ledger = json.loads((tmp_path / ".automation" / "plan-status-ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["plans"][plan_id]["active_alert"]["state"], "OPERATOR_PENDING")

    def test_open_plan_status_closes_thread_when_plan_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-done"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            meta = {
                "plan_id": plan_id,
                "title": "Done plan",
                "state": "DONE",
                "awaiting_operator": False,
                "state_reason": "all PRs merged",
                "updated_at": "2026-06-25T00:00:00+00:00",
                "discord": {"thread_id": "thread-done"},
            }
            (plan_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({
                "plans": {plan_id: {"plan_id": plan_id, "plan_dir": str(plan_dir), "thread_id": "thread-done", "state": "DONE"}},
            }), encoding="utf-8")
            sent = []
            archived = []
            old_post, old_archive = plan_status_lib.post_message, plan_status_lib.archive_thread
            try:
                plan_status_lib.post_message = lambda token, channel_id, content: sent.append((channel_id, content)) or "msg-done"
                plan_status_lib.archive_thread = lambda token, thread_id: archived.append(thread_id)
                result = sync_open_plan_threads(tmp_path, operator_user_id="op", token="tok")
            finally:
                plan_status_lib.post_message, plan_status_lib.archive_thread = old_post, old_archive

            self.assertIn("closed_plan_thread", {a["action"] for a in result["actions"]})
            self.assertEqual(archived, ["thread-done"])
            self.assertIn("Closing this plan thread", sent[0][1])
            ledger = json.loads((tmp_path / ".automation" / "plan-status-ledger.json").read_text(encoding="utf-8"))
            entry = ledger["plans"][plan_id]
            self.assertEqual(entry["last_state"], "DONE")
            self.assertIn("thread_archived_at", entry)

    def test_pr_readiness_review_cleanup_gate_requires_risk_edge_cases(self):
        ssrf_evidence = {
            "review_cleanup": {
                "critics": ["grounding", "security", "regression", "edge_case_matrix", "fresh_review_delta"],
                "findings": [{
                    "id": "ipv6-ssrf",
                    "status": "resolved",
                    "fix_commit": "abc123",
                    "evidence": "api/settings/git-installations.ts strips IPv6 brackets before isIP",
                    "tests": ["vitest ssrf cases"],
                    "tags": ["ssrf"],
                    "edge_cases": ["private_ipv4"],
                }],
            }
        }
        issues = validate_review_cleanup_evidence(ssrf_evidence)
        self.assertIn("ssrf_edge_cases_missing", {issue["kind"] for issue in issues})

        race_evidence = {
            "review_cleanup": {
                "critics": ["grounding", "security", "regression", "edge_case_matrix", "fresh_review_delta"],
                "findings": [{
                    "id": "stale-writeback",
                    "status": "resolved",
                    "fix_commit": "def456",
                    "evidence": "claim CAS resets verify_deployment_id before re-fanout",
                    "tests": ["pglite stale callback race"],
                    "tags": ["race", "state-write"],
                    "edge_cases": ["stale_actor_window", "prior_state_guard", "identity_pin", "reset_stale_binding"],
                }],
            }
        }
        self.assertEqual(validate_review_cleanup_evidence(race_evidence), [])

    def test_pr_readiness_result_fails_without_required_cleanup_critics(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            job = create_readiness_job(tmp_path, task_id="task-review", branch="feat/review", sha="abc123")
            result = mark_readiness_result(
                tmp_path,
                job["job_id"],
                passed=True,
                issues=[],
                evidence={"review_cleanup": {"critics": ["grounding"], "findings": []}},
            )
            self.assertFalse(result["passed"])
            self.assertEqual(result["state"], "READINESS_FAILED")
            self.assertIn("missing_cleanup_critics", {issue["kind"] for issue in result["issues"]})
            self.assertIn("missing_review_findings", {issue["kind"] for issue in result["issues"]})

    def test_ready_for_rereview_signal_blocks_when_gate_not_passed(self):
        module = load_script_module("pr_ready_for_rereview_script", "pr-ready-for-rereview.py")
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            job = create_readiness_job(tmp_path, task_id="task-review", branch="feat/review", sha="abc123")
            old_head = getattr(module, "pr_head_sha")
            try:
                setattr(module, "pr_head_sha", lambda pr: "abc123")
                result = module.assert_rereview_ready(tmp_path, pr="123", job_id=job["job_id"])
            finally:
                setattr(module, "pr_head_sha", old_head)
            self.assertFalse(result["ready"])
            self.assertEqual(result["gate"]["reason"], "audit_not_passed")

    def test_pr_readiness_gate_blocks_ready_claim_until_same_sha_passes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            job = create_readiness_job(
                tmp_path,
                task_id="task-1",
                branch="feat/example",
                sha="abc123",
                pr_url="https://github.com/dom-armor/armor-swarm/pull/1",
            )

            self.assertTrue(readiness_blocks(job, current_sha="abc123"))
            passed = mark_readiness_result(tmp_path, job["job_id"], passed=True, issues=[])
            self.assertFalse(readiness_blocks(passed, current_sha="abc123"))
            self.assertTrue(readiness_blocks(passed, current_sha="def456"))
            self.assertEqual(readiness_blocks(passed, current_sha="def456", explain=True)["reason"], "stale_sha")

    def test_source_plan_status_treats_authoritative_current_design_as_active(self):
        markdown = "# Upgrade plan\n\n**Status:** Authoritative current design — the single source of truth.\n"
        audit = plan_automation_lib.audit_source_plan_status(markdown, source_path="plan.md")

        self.assertEqual(audit["status"], "ACTIVE")
        self.assertEqual(audit["blockers"], [])
        self.assertEqual(audit["warnings"], [])

    def test_source_plan_ingestion_blocks_retired_plan_before_decomposition(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            source_plan = tmp_path / "retired-plan.md"
            source_plan.write_text(
                "# Plan → Executable Workflow\n\n"
                "**From:** Dom\n\n"
                "## Retired as active plan — 2026-06-25\n\n"
                "> This document is retained as historical sequencing/audit context only.\n\n"
                "## Acceptance\n\n- Build the current active work only.\n",
                encoding="utf-8",
            )

            result = plan_automation_lib.ingest_source_plan(
                tmp_path,
                plan_automation_lib.SourcePlanIngestRequest(
                    plan_file=source_plan,
                    repo="/tmp/target-repo",
                    base_branch="main",
                    guild_id="guild-1",
                    control_channel_id="channel-1",
                    operator_user_id="user-1",
                    thread_id="thread-1",
                    no_discord=True,
                    auto_approve=True,
                    decompose=True,
                ),
            )

            self.assertEqual(result["decision"], "BLOCKED")
            self.assertEqual(result["status_audit"]["status"], "RETIRED")
            self.assertIn("retired", result["reason"].lower())
            plan_dir = Path(result["plan_dir"])
            self.assertTrue((plan_dir / "source-status-audit.json").exists())
            self.assertTrue((plan_dir / "source-status-audit.md").exists())
            self.assertTrue((plan_dir / "readiness-5x5-audit.json").exists())
            self.assertTrue((plan_dir / "readiness-5x5-audit.md").exists())
            meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "QUESTION")
            self.assertIs(meta["awaiting_operator"], True)
            self.assertFalse((tmp_path / "tasks").exists())

    def test_source_plan_ingestion_blocks_missing_or_non_operator_author(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            source_plan = tmp_path / "third-party-plan.md"
            source_plan.write_text(
                "# External plan\n\n"
                "**From:** Drake\n\n"
                "**Status:** Active — ready.\n\n"
                "## Done means\n\n- Update docs.\n",
                encoding="utf-8",
            )

            result = plan_automation_lib.ingest_source_plan(
                tmp_path,
                plan_automation_lib.SourcePlanIngestRequest(
                    plan_file=source_plan,
                    repo="/tmp/target-repo",
                    thread_id="thread-1",
                    no_discord=True,
                    auto_approve=True,
                    decompose=True,
                ),
            )

            self.assertEqual(result["decision"], "BLOCKED")
            self.assertEqual(result["author_audit"]["status"], "UNKNOWN_AUTHOR")
            self.assertIn("author audit", result["reason"])
            self.assertFalse((tmp_path / "tasks").exists())
            plan_dir = Path(result["plan_dir"])
            self.assertTrue((plan_dir / "source-author-audit.json").exists())
            meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "QUESTION")

    def test_source_plan_ingestion_allows_explicit_operator_author_override(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            source_plan = tmp_path / "legacy-dom-plan.md"
            source_plan.write_text(
                "# Legacy Dom-authored plan with no author line\n\n"
                "**Status:** Active — ready.\n\n"
                "## Done means\n\n- Update docs.\n",
                encoding="utf-8",
            )

            result = plan_automation_lib.ingest_source_plan(
                tmp_path,
                plan_automation_lib.SourcePlanIngestRequest(
                    plan_file=source_plan,
                    repo="/tmp/target-repo",
                    thread_id="thread-1",
                    no_discord=True,
                    force_author_override=True,
                ),
            )

            self.assertEqual(result["decision"], "CONTRACT_SHAPED")
            self.assertEqual(result["author_audit"]["status"], "OPERATOR_ASSERTED")
            self.assertEqual(result["author_audit"]["blockers"], [])

    def test_source_plan_ingestion_can_approve_decompose_and_dispatch_active_plan(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            remote = tmp_path / "remote.git"
            target = tmp_path / "target"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "clone", str(remote), str(target)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=target, check=True)
            (target / "README.md").write_text("# target\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=target, check=True, capture_output=True, text=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=target, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=target, check=True, capture_output=True, text=True)

            source_plan = tmp_path / "active-plan.md"
            source_plan.write_text(
                "# Build dashboard polish\n\n"
                "**From:** Dom\n\n"
                "**Status:** Active — ready to build.\n\n"
                "## Done means\n\n"
                "- Update README docs.\n"
                "- Run tests or record verification.\n",
                encoding="utf-8",
            )

            result = plan_automation_lib.ingest_source_plan(
                tmp_path,
                plan_automation_lib.SourcePlanIngestRequest(
                    plan_file=source_plan,
                    repo=str(target),
                    base_branch="main",
                    guild_id="guild-1",
                    control_channel_id="channel-1",
                    operator_user_id="user-1",
                    thread_id="thread-1",
                    no_discord=True,
                    auto_approve=True,
                    decompose=True,
                    dispatch=True,
                    execute_dispatch=True,
                ),
            )

            self.assertEqual(result["decision"], "DISPATCHED")
            self.assertEqual(result["status_audit"]["status"], "ACTIVE")
            self.assertEqual(result["readiness_5x5"]["failed"], 0)
            self.assertEqual(result["decomposition"]["packet_count"], 2)
            dispatch = result["dispatch"]
            self.assertEqual(dispatch["decision"], "DISPATCHED")
            task_dir = tmp_path / "tasks" / dispatch["task_id"]
            self.assertTrue((task_dir / "builder-prompt.md").exists())
            task_meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(task_meta["state"], "EXECUTE")
            self.assertTrue(Path(task_meta["dispatch"]["worktree"]).exists())

    def test_source_plan_ingest_5x5_resolves_relative_repo_against_repo_root(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            target = tmp_path / "target"
            subprocess.run(["git", "init", str(target)], check=True, capture_output=True, text=True)
            markdown = "# Build docs\n\n**Status:** Active — ready.\n\n- Acceptance: update docs.\n"
            status_audit = plan_automation_lib.audit_source_plan_status(markdown, source_path="plan.md")

            audit = plan_automation_lib.build_ingest_5x5_audit(
                markdown,
                plan_automation_lib.SourcePlanIngestRequest(
                    plan_file=tmp_path / "plan.md",
                    repo="target",
                    thread_id="thread-1",
                    no_discord=True,
                    dispatch=True,
                ),
                status_audit,
                repo_root=tmp_path,
            )

            repo_check = next(item for item in audit["checks"] if item["code"] == "R1")
            self.assertEqual(repo_check["status"], "PASS")
            self.assertEqual(audit["failed"], 0)

    def test_source_plan_status_treats_not_yet_implemented_plan_as_active(self):
        markdown = """# Chat-card terminal-status fix + async acceptance-criteria generation

**Status:** PLAN — not yet implemented. No code written. tsc/tests **NOT RUN** (no diff yet).

## Status update — 2026-06-25 plan audit
**Async criteria generation:** approved to build now.
"""

        audit = plan_automation_lib.audit_source_plan_status(markdown, source_path="plan.md")

        self.assertEqual(audit["status"], "ACTIVE")
        self.assertEqual(audit["blockers"], [])

    def test_upgrade_status_plan_decomposes_to_pr_stack_and_decision_actions(self):
        source = """# Upgrade / Migration workflow — plan-fronted hybrid design

## Status update — 2026-06-25 plan audit

**Open review stack:** PR-B3 fan-out/coarse verify/writeback/reaper/template is #713 (`feat/upgrade-fanout-verify`, open and mergeable at audit time). Fan-out SPEND admission is #725, stacked on #713 and inert until the stack lands.

**Deferred by user decision:** PR-B4 / H7 real empirical verify (`apply → install → build → test`) is deferred until after #713 and #725 merge. The sandbox/credential model remains the live design to revisit then.

## 0. The shape in one paragraph
- No approval-signal coordination seam. This is rationale, not a build packet.
- Cleaner RLS posture. This is rationale, not a build packet.
"""

        packets = plan_automation_lib.split_pr_packets(source, "Upgrade / Migration workflow")

        self.assertEqual([p["kind"] for p in packets], ["pr_status_wait", "pr_maintenance", "decision_required"])
        by_pr = {p.get("pr_number"): p for p in packets if p.get("pr_number")}
        self.assertEqual(by_pr[713]["status"], "waiting")
        self.assertEqual(by_pr[725]["status"], "planned")
        self.assertEqual(by_pr[725]["depends_on"], ["pr713-review"] )
        self.assertTrue(by_pr[725]["discord"]["requires_dedicated_thread"])
        self.assertIn("PR #725", by_pr[725]["discord"]["thread_title"])
        decision = packets[2]
        self.assertIn("PR-B4", decision["title"])
        self.assertTrue(decision["awaiting_operator"])

    def test_decompose_plan_marks_decisions_and_pr_handoffs_without_dispatching_design_rationale(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-upgrade"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            source = """# Upgrade / Migration workflow

**Open review stack:** PR-B3 fan-out is #713 (`feat/upgrade-fanout-verify`, open and mergeable at audit time). Fan-out SPEND admission is #725, stacked on #713 and inert until the stack lands.

**Deferred by user decision:** PR-B4 / H7 real empirical verify is deferred until after #713 and #725 merge.

- Design rationale bullet that must not become a PR packet.
"""
            (plan_dir / "source-plan.md").write_text(source, encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Upgrade / Migration workflow",
                "state": "DECOMPOSE",
                "repo": "/tmp/target",
                "base_branch": "main",
                "discord": {"thread_id": "plan-thread"},
                "awaiting_operator": False,
            }), encoding="utf-8")

            result = plan_automation_lib.decompose_plan(tmp_path, plan_id)

            self.assertEqual(result["packet_count"], 3)
            tasks = {item["kind"]: item for item in result["tasks"]}
            wait_meta = json.loads((Path(tasks["pr_status_wait"]["task_dir"]) / "meta.json").read_text(encoding="utf-8"))
            maint_meta = json.loads((Path(tasks["pr_maintenance"]["task_dir"]) / "meta.json").read_text(encoding="utf-8"))
            decision_meta = json.loads((Path(tasks["decision_required"]["task_dir"]) / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(wait_meta["state"], "PR_STATUS")
            self.assertEqual(maint_meta["state"], "SHAPE")
            self.assertTrue(maint_meta["discord"]["requires_dedicated_thread"])
            self.assertEqual(decision_meta["state"], "QUESTION")
            self.assertTrue(decision_meta["awaiting_operator"])
            questions = (Path(tasks["decision_required"]["task_dir"]) / "questions.md").read_text(encoding="utf-8")
            self.assertIn("Decision needed", questions)

    def test_redecompose_preserves_existing_task_discord_thread_ids(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-upgrade"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            source = """# Upgrade / Migration workflow

**Open review stack:** PR-B3 fan-out is #713 (`feat/upgrade-fanout-verify`, open and mergeable at audit time). Fan-out SPEND admission is #725, stacked on #713 and inert until the stack lands.

**Deferred by user decision:** PR-B4 / H7 real empirical verify is deferred until after #713 and #725 merge.
"""
            (plan_dir / "source-plan.md").write_text(source, encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Upgrade / Migration workflow",
                "state": "DECOMPOSE",
                "repo": "/tmp/target",
                "base_branch": "main",
                "discord": {"thread_id": "plan-thread"},
                "awaiting_operator": False,
            }), encoding="utf-8")

            first = plan_automation_lib.decompose_plan(tmp_path, plan_id)
            maint_dir = Path(next(t for t in first["tasks"] if t["kind"] == "pr_maintenance")["task_dir"])
            maint_meta = json.loads((maint_dir / "meta.json").read_text(encoding="utf-8"))
            maint_meta["discord"]["thread_id"] = "thread-pr725"
            (maint_dir / "meta.json").write_text(json.dumps(maint_meta), encoding="utf-8")

            plan_automation_lib.decompose_plan(tmp_path, plan_id)

            redecomposed = json.loads((maint_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(redecomposed["discord"]["thread_id"], "thread-pr725")

    def test_reconcile_merged_prs_marks_pr_tasks_done_and_unblocks_dependents(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-upgrade"
            (tmp_path / "tasks").mkdir()
            def write_task(task_id, state, packet, awaiting=False):
                task_dir = tmp_path / "tasks" / task_id
                task_dir.mkdir()
                (task_dir / "meta.json").write_text(json.dumps({
                    "task_id": task_id,
                    "source_plan_id": plan_id,
                    "state": state,
                    "awaiting_operator": awaiting,
                    "state_reason": "test",
                    "pr_packet": packet,
                }), encoding="utf-8")
                return task_dir
            t713 = write_task("task-713", "PR_STATUS", {"kind": "pr_status_wait", "packet_id": "pr713-review", "pr_number": 713, "depends_on": []})
            t725 = write_task("task-725", "SHAPE", {"kind": "pr_maintenance", "packet_id": "pr725-stacked-maintenance", "pr_number": 725, "depends_on": ["pr713-review"]})
            tb4 = write_task("task-b4", "QUESTION", {"kind": "decision_required", "packet_id": "decision-pr-b4", "depends_on": ["pr713-review", "pr725-stacked-maintenance"]}, awaiting=True)

            pr_data = {
                713: {"number": 713, "state": "MERGED", "url": "https://example/pr/713", "mergedAt": "2026-06-25T20:01:30Z", "headRefOid": "sha713"},
                725: {"number": 725, "state": "MERGED", "url": "https://example/pr/725", "mergedAt": "2026-06-26T02:45:12Z", "headRefOid": "sha725"},
            }

            result = reconcile_merged_prs.reconcile_merged_prs(tmp_path, fetch_pr=lambda n, repo: pr_data[n])

            self.assertEqual(result["merged_count"], 2)
            m713 = json.loads((t713 / "meta.json").read_text(encoding="utf-8"))
            m725 = json.loads((t725 / "meta.json").read_text(encoding="utf-8"))
            mb4 = json.loads((tb4 / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(m713["state"], "DONE")
            self.assertEqual(m725["state"], "DONE")
            self.assertEqual(m713["github"]["merged_at"], "2026-06-25T20:01:30Z")
            self.assertEqual(m725["github"]["head_sha"], "sha725")
            self.assertEqual(mb4["state"], "QUESTION")
            self.assertTrue(mb4["awaiting_operator"])
            self.assertTrue(mb4["dependencies_cleared"])
            self.assertIn("dependencies cleared", mb4["state_reason"])

    def test_reconcile_merged_prs_leaves_open_pr_task_waiting(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-1"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-1",
                "state": "PR_STATUS",
                "awaiting_operator": False,
                "pr_packet": {"kind": "pr_status_wait", "packet_id": "pr1-review", "pr_number": 1},
            }), encoding="utf-8")

            result = reconcile_merged_prs.reconcile_merged_prs(tmp_path, fetch_pr=lambda n, repo: {"number": n, "state": "OPEN"})

            self.assertEqual(result["merged_count"], 0)
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "PR_STATUS")

    def test_ensure_build_threads_dry_run_lists_each_in_flight_task_thread(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-pr725"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-pr725",
                "state": "SHAPE",
                "source_plan_id": "plan-upgrade",
                "pr_packet": {"kind": "pr_maintenance", "title": "Update stacked PR #725"},
                "discord": {
                    "requires_dedicated_thread": True,
                    "thread_title": "PR #725 — stacked maintenance",
                    "prompt": "Inspect conflicts and run PR automation.",
                },
            }), encoding="utf-8")

            result = ensure_build_threads.ensure_threads(tmp_path, channel_id="build-control", dry_run=True)
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            opener = ensure_build_threads.starter_message(meta)

            self.assertEqual(result["action_count"], 1)
            self.assertEqual(result["actions"][0]["action"], "would_create_thread")
            self.assertEqual(result["actions"][0]["title"], "PR #725 — stacked maintenance")
            self.assertIn("Persistent task card", opener)
            self.assertIn("Needs to complete", opener)
            self.assertIn("Reply in this thread to progress", opener)

    def test_open_plan_status_posts_then_updates_persistent_plan_card(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-card"
            plan_dir = tmp_path / "plans" / plan_id
            plan_dir.mkdir(parents=True)
            (tmp_path / ".automation").mkdir()
            meta = {
                "plan_id": plan_id,
                "title": "Card plan",
                "state": "CONTRACT_REVIEW",
                "awaiting_operator": True,
                "state_reason": "contract shaped; awaiting approval",
                "updated_at": "2026-06-25T00:00:00+00:00",
                "repo": "/repo",
                "base_branch": "main",
                "discord": {"thread_id": "thread-card"},
            }
            (plan_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({
                "plans": {plan_id: {"plan_id": plan_id, "plan_dir": str(plan_dir), "thread_id": "thread-card", "state": "CONTRACT_REVIEW"}},
            }), encoding="utf-8")
            sent = []
            updated = []
            old_post, old_update, old_member = plan_status_lib.post_message, plan_status_lib.update_message, plan_status_lib.add_thread_member
            try:
                plan_status_lib.post_message = lambda token, channel_id, content: sent.append((channel_id, content)) or "card-msg"
                plan_status_lib.update_message = lambda token, channel_id, message_id, content: updated.append((channel_id, message_id, content)) or message_id
                plan_status_lib.add_thread_member = lambda token, thread_id, user_id: None
                first = sync_open_plan_threads(tmp_path, operator_user_id="op", token="tok")
                second = sync_open_plan_threads(tmp_path, operator_user_id="op", token="tok")
            finally:
                plan_status_lib.post_message, plan_status_lib.update_message, plan_status_lib.add_thread_member = old_post, old_update, old_member

            self.assertIn("created_plan_card", {a["action"] for a in first["actions"]})
            self.assertIn("updated_plan_card", {a["action"] for a in second["actions"]})
            self.assertTrue(any("Persistent plan card" in item[1] and "Decision / reply needed" in item[1] for item in sent))
            self.assertEqual(updated[0][1], "card-msg")
            ledger = json.loads((tmp_path / ".automation" / "plan-status-ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["plans"][plan_id]["plan_card_message_id"], "card-msg")

    def test_task_thread_reply_unblocks_decision_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-decision"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-decision",
                "state": "QUESTION",
                "awaiting_operator": True,
                "pr_packet": {"kind": "decision_required", "packet_id": "decision-1"},
                "discord": {"thread_id": "thread-decision"},
            }), encoding="utf-8")

            result = plan_thread_poller.handle_operator_task_reply(tmp_path, {"task_id": "task-decision", "task_dir": str(task_dir)}, {
                "id": "msg-1",
                "content": "Use public digest-pinned image and rootless podman.",
                "author": {"id": "op"},
                "timestamp": "2026-06-26T00:00:00+00:00",
            })

            self.assertEqual(result["action"], "operator_task_reply_ingested")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "SHAPE")
            self.assertFalse(meta["awaiting_operator"])
            self.assertIn("operator_decision", meta)
            replies = (task_dir / "operator-replies.jsonl").read_text(encoding="utf-8")
            self.assertIn("rootless podman", replies)
    def test_parent_plan_reply_routes_to_single_child_decision_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-parent"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / "task-decision"
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Parent plan",
                "state": "EXECUTING",
                "discord": {"thread_id": "thread-plan"},
            }), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-decision",
                "source_plan_id": plan_id,
                "state": "QUESTION",
                "awaiting_operator": True,
                "phase_status": {"DECISION": "NEEDS_OPERATOR"},
                "pr_packet": {"kind": "decision_required", "packet_id": "decision-1", "awaiting_operator": True, "status": "blocked"},
            }), encoding="utf-8")

            result = plan_thread_poller.handle_operator_reply(tmp_path, {"plan_id": plan_id, "plan_dir": str(plan_dir), "state": "EXECUTING"}, {
                "id": "msg-1",
                "content": "Use rootless Podman with public digest-pinned image.",
                "author": {"id": "op"},
                "timestamp": "2026-06-26T00:00:00+00:00",
            })

            self.assertEqual(result["action"], "operator_reply_routed_to_child_task")
            self.assertEqual(result["child_action"]["action"], "operator_task_reply_ingested")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "SHAPE")
            self.assertFalse(meta["awaiting_operator"])
            self.assertIn("operator_decision", meta)
            self.assertEqual(meta["phase_status"]["DECISION"], "ANSWERED")
            self.assertFalse(meta["pr_packet"]["awaiting_operator"])
            self.assertEqual(meta["pr_packet"]["status"], "answered")

    def test_bare_approval_does_not_satisfy_concrete_decision_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-decision"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-decision",
                "state": "QUESTION",
                "awaiting_operator": True,
                "pr_packet": {"kind": "decision_required", "packet_id": "decision-1"},
                "discord": {"thread_id": "thread-decision"},
            }), encoding="utf-8")

            result = plan_thread_poller.handle_operator_task_reply(tmp_path, {"task_id": "task-decision", "task_dir": str(task_dir)}, {
                "id": "msg-approve",
                "content": "approve",
                "author": {"id": "op"},
                "timestamp": "2026-06-26T00:00:00+00:00",
            })

            self.assertEqual(result["action"], "operator_task_reply_needs_concrete_decision")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "QUESTION")
            self.assertTrue(meta["awaiting_operator"])
            self.assertNotIn("operator_decision", meta)
            self.assertIn("concrete answer", meta["state_reason"])

    def test_bare_approval_after_concrete_decision_does_not_reblock_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-decision"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-decision",
                "state": "READY_FOR_BUILDER",
                "state_reason": "readiness audit failed; builder should address blocking issues",
                "awaiting_operator": False,
                "operator_decision": {"content": "Choose Option A", "message_id": "decision-msg"},
                "phase_status": {"DECISION": "ANSWERED", "EXECUTE": "READY_FOR_BUILDER"},
                "pr_packet": {"kind": "decision_required", "packet_id": "decision-1", "status": "answered", "awaiting_operator": False},
                "discord": {"thread_id": "thread-decision"},
            }), encoding="utf-8")

            result = plan_thread_poller.handle_operator_task_reply(tmp_path, {"task_id": "task-decision", "task_dir": str(task_dir)}, {
                "id": "msg-approve-late",
                "content": "approve",
                "author": {"id": "op"},
                "timestamp": "2026-06-26T01:00:00+00:00",
            })

            self.assertEqual(result["action"], "operator_task_reply_ignored_decision_already_answered")
            meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["state"], "READY_FOR_BUILDER")
            self.assertFalse(meta["awaiting_operator"])
            self.assertEqual(meta["operator_decision"]["message_id"], "decision-msg")
            self.assertIn("builder should address", meta["state_reason"])

    def test_plan_progress_reconciler_marks_answered_decision_ready_and_updates_parent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-upgrade"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / "task-decision"
            (tmp_path / ".automation").mkdir(parents=True)
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({"plans": {plan_id: {
                "plan_id": plan_id,
                "plan_dir": str(plan_dir),
                "state": "EXECUTING",
                "thread_id": "thread-plan",
            }}}), encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Upgrade plan",
                "state": "EXECUTING",
                "repo": "/repo",
                "base_branch": "main",
                "discord": {"thread_id": "thread-plan"},
                "state_reason": "decomposed",
            }), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-decision",
                "source_plan_id": plan_id,
                "state": "SHAPE",
                "awaiting_operator": False,
                "operator_decision": {"content": "Option A"},
                "phase_status": {"DECISION": "ANSWERED", "SHAPE": "ACTIVE"},
                "pr_packet": {"kind": "decision_required", "packet_id": "decision-1", "awaiting_operator": False, "status": "answered"},
            }), encoding="utf-8")

            result = reconcile_plan_progress.reconcile_plan_progress(tmp_path)

            self.assertIn("marked_decision_ready", {a["action"] for a in result["actions"]})
            self.assertIn("updated_parent_plan", {a["action"] for a in result["actions"]})
            task_meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(task_meta["phase_status"]["SHAPE"], "READY")
            self.assertEqual(task_meta["state_reason"], "decision answered; ready for dispatch")
            task_md = (task_dir / "task.md").read_text(encoding="utf-8")
            self.assertIn("Decision: Option A", task_md)
            self.assertIn("public digest-pinned verify image", task_md)
            plan_meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_meta["state"], "EXECUTING")
            self.assertIn("active child task task-decision is SHAPE", plan_meta["state_reason"])
            index = json.loads((tmp_path / ".automation" / "plans-index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["plans"][plan_id]["state"], "EXECUTING")
            self.assertIn("active child task task-decision", index["plans"][plan_id]["state_reason"])
            workflow_map = index["plans"][plan_id]["child_progress"]["workflow_map"]
            self.assertEqual(workflow_map[0]["label"], "decision-1")
            self.assertEqual(workflow_map[0]["owner"], "build-control")

    def test_plan_card_renders_contained_pr_workflow_map(self):
        status = {
            "plan_id": "plan-upgrade",
            "title": "Upgrade Migration",
            "state": "EXECUTING",
            "status": "IN_PROGRESS",
            "reason": "active child task PR-B4 is DISPATCHED",
            "repo": "/repo",
            "base_branch": "main",
            "child_progress": {
                "workflow_map": [
                    {"label": "PR #713", "title": "review status", "state": "DONE", "owner": "completed", "pr_url": "https://example/pr/713"},
                    {"label": "PR #725", "title": "stacked maintenance", "state": "DONE", "owner": "completed", "pr_url": "https://example/pr/725"},
                    {"label": "decision-pr-b4", "title": "Build PR-B4 / H7 empirical verify sandbox", "state": "VERIFYING", "owner": "build-control", "branch": "decision/pr-b4-empirical-verify"},
                    {"label": "PR-B5", "title": "Remaining planned compatibility PR", "state": "SHAPE", "owner": "build-control", "branch": "feat/pr-b5"},
                ],
                "hidden_cancelled_count": 68,
            },
        }

        card = plan_status_lib.format_plan_card(status, "op")

        self.assertIn("Contained workflow / PR progress", card)
        self.assertIn("✅ 2 complete", card)
        self.assertIn("▶️ 1 in progress", card)
        self.assertIn("🧭 1 planned/left to build", card)
        self.assertIn("PR #713", card)
        self.assertIn("PR #725", card)
        self.assertIn("decision-pr-b4", card)
        self.assertIn("[in-progress] decision-pr-b4", card)
        self.assertIn("[planned] PR-B5", card)
        self.assertIn("owner: `build-control`", card)
        self.assertIn("68 superseded/cancelled", card)

    def test_plan_status_treats_waiting_child_as_operator_waiting(self):
        status = plan_status_lib.classify_plan({
            "plan_id": "plan-child-waiting",
            "title": "Child waiting plan",
            "state": "EXECUTING",
            "state_reason": "waiting on child task task-1 (ESCALATED)",
            "child_progress": {"waiting_count": 1, "workflow_map": []},
        })

        self.assertTrue(status["awaiting_operator"])
        self.assertEqual(status["status"], "OPERATOR_WAITING")

    def test_plan_card_renders_pre_decomposition_progress_placeholder(self):
        status = {
            "plan_id": "plan-contract",
            "title": "Contract plan",
            "state": "CONTRACT_REVIEW",
            "status": "OPERATOR_WAITING",
            "reason": "awaiting approval",
            "repo": "/repo",
            "base_branch": "main",
            "child_progress": {},
        }

        card = plan_status_lib.format_plan_card(status, "op")

        self.assertIn("Contained workflow / PR progress", card)
        self.assertIn("no PR/build packets decomposed yet", card)
        self.assertIn("Remaining planned PRs will appear", card)

    def test_ensure_build_threads_skips_parent_contained_task(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            task_dir = tmp_path / "tasks" / "task-contained"
            task_dir.mkdir(parents=True)
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-contained",
                "state": "DISPATCHED",
                "discord": {"thread_id": "parent-thread", "contained_in_parent_thread": True},
            }), encoding="utf-8")

            payload = ensure_build_threads.ensure_threads(tmp_path, channel_id="channel", dry_run=True)

            self.assertEqual(payload["action_count"], 0)

    def test_plan_progress_reconciler_marks_parent_done_when_children_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            plan_id = "plan-done"
            plan_dir = tmp_path / "plans" / plan_id
            task_dir = tmp_path / "tasks" / "task-done"
            (tmp_path / ".automation").mkdir(parents=True)
            plan_dir.mkdir(parents=True)
            task_dir.mkdir(parents=True)
            (tmp_path / ".automation" / "plans-index.json").write_text(json.dumps({"plans": {plan_id: {
                "plan_id": plan_id,
                "plan_dir": str(plan_dir),
                "state": "EXECUTING",
                "thread_id": "thread-plan",
            }}}), encoding="utf-8")
            (plan_dir / "meta.json").write_text(json.dumps({
                "plan_id": plan_id,
                "title": "Done plan",
                "state": "EXECUTING",
                "discord": {"thread_id": "thread-plan"},
            }), encoding="utf-8")
            (task_dir / "meta.json").write_text(json.dumps({
                "task_id": "task-done",
                "source_plan_id": plan_id,
                "state": "DONE",
                "awaiting_operator": False,
            }), encoding="utf-8")

            result = reconcile_plan_progress.reconcile_plan_progress(tmp_path)

            self.assertIn("updated_parent_plan", {a["action"] for a in result["actions"]})
            plan_meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_meta["state"], "DONE")
            self.assertEqual(plan_meta["child_progress"]["terminal_count"], 1)
            index = json.loads((tmp_path / ".automation" / "plans-index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["plans"][plan_id]["state"], "DONE")


if __name__ == "__main__":
    unittest.main()
