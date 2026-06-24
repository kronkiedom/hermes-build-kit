import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
