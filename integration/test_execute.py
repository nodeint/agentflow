from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from integration.support import (
    APPROVED,
    BLOCKED,
    CODEX_CONFIG,
    CODEX_THREAD,
    COMPLETE,
    DRAFT_NOTE,
    GROK_CONFIG,
    GROK_SESSION,
    PLAN_REVIEW,
    REVISE,
    SOLO,
    execution_records,
    execution_statuses,
    has_args,
    install_script,
    provider_invocations,
    resumed_notes,
    run_main,
    session_status,
    write_workspace,
)


@unittest.skipUnless(os.name == "posix", "provider processes require POSIX")
class ExecuteTests(unittest.TestCase):
    def test_plan_review_publishes_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan": PLAN_REVIEW})
            code, objects, stderr = run_main(
                workspace,
                ["start", "plan", "--task", "Write a plan"],
            )
            self.assertEqual(code, 0, stderr)
            result = objects[0]
            self.assertEqual(
                [item.get("agent_id") for item in result["stages"]],
                [GROK_SESSION, GROK_SESSION],
            )
            self.assertEqual(result["stop_reason"], "completed")
            self.assertEqual(result["session_status"], "completed")
            published = (
                workspace
                / ".agentflow"
                / "sessions"
                / result["session_id"]
                / "outputs"
                / "plan"
            )
            self.assertTrue(published.is_file())
            self.assertTrue(published.read_text(encoding="utf-8").strip())

    def test_stage_runs_through_the_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, CODEX_CONFIG, {"draft-note": DRAFT_NOTE})
            code, objects, stderr = run_main(
                workspace,
                [
                    "stage",
                    "--workflow",
                    "draft-note",
                    "--task",
                    "Draft a note",
                ],
            )
            self.assertEqual(code, 0, stderr)
            result = objects[0]
            self.assertEqual(result["stages"][0]["agent_id"], CODEX_THREAD)
            self.assertEqual(result["stages"][0]["outcome_status"], "complete")
            self.assertEqual(result["stop_reason"], "stage")
            artifact = Path(result["stages"][0]["artifact_directory"]) / "note.md"
            self.assertTrue(artifact.is_file())
            self.assertTrue(artifact.read_text(encoding="utf-8").strip())

    def test_invalid_stdout_fails_the_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"solo": SOLO})
            code, objects, stderr = run_main(
                workspace,
                [
                    "start",
                    "solo",
                    "--task",
                    "Write",
                    "--attempts",
                    "1",
                ],
                outcome="invalid",
            )
            self.assertEqual(code, 3, stderr)
            self.assertEqual(objects[0]["stop_reason"], "retry_exhausted")
            self.assertEqual(execution_statuses(workspace), ["failed"])

    def test_revise_publishes_the_later_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan": PLAN_REVIEW})
            install_script(
                workspace,
                [
                    {"text": COMPLETE, "artifact": "# v1\n"},
                    {"text": REVISE},
                    {"text": COMPLETE, "artifact": "# v2\n"},
                    {"text": APPROVED},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "plan", "--task", "Write a plan"],
            )
            self.assertEqual(code, 0, stderr)
            result = objects[0]
            self.assertEqual(
                [item.get("outcome_decision") for item in result["stages"]],
                ["", "revise", "", "approved"],
            )
            self.assertEqual(result["stop_reason"], "completed")
            published = (
                workspace
                / ".agentflow"
                / "sessions"
                / result["session_id"]
                / "outputs"
                / "plan"
            )
            self.assertEqual(published.read_text(encoding="utf-8"), "# v2\n")
            invocations = len(provider_invocations(workspace))
            code, again, stderr = run_main(workspace, ["continue", result["session_id"]])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(again[0]["stop_reason"], "completed")
            self.assertEqual(again[0]["stages"], [])
            self.assertEqual(len(provider_invocations(workspace)), invocations)

    def test_blocked_review_is_not_dispatched_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan": PLAN_REVIEW})
            install_script(
                workspace,
                [
                    {"text": COMPLETE, "artifact": "# plan\n"},
                    {"text": BLOCKED},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "plan", "--task", "Write a plan"],
            )
            self.assertEqual(code, 1, stderr)
            result = objects[0]
            self.assertEqual(result["stop_reason"], "blocked")
            self.assertEqual(result["session_status"], "blocked")
            self.assertEqual(
                session_status(workspace, result["session_id"])["status"],
                "blocked",
            )
            self.assertEqual(len(provider_invocations(workspace)), 2)
            for argv in (
                ["continue", result["session_id"]],
                ["stage", result["session_id"]],
            ):
                code, stopped, stderr = run_main(workspace, argv)
                self.assertEqual(code, 1, stderr)
                self.assertEqual(stopped[0]["stop_reason"], "blocked")
                self.assertEqual(stopped[0]["stages"], [])
            self.assertEqual(len(provider_invocations(workspace)), 2)

    def test_a_bad_response_is_retried_and_the_workflow_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan": PLAN_REVIEW})
            install_script(
                workspace,
                [
                    {"text": "not a status header"},
                    {"text": COMPLETE, "artifact": "# plan\n"},
                    {"text": APPROVED},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "plan", "--task", "Write a plan", "--attempts", "2"],
            )
            self.assertEqual(code, 0, stderr)
            result = objects[0]
            self.assertEqual(result["stop_reason"], "completed")
            records = execution_records(workspace, result["session_id"])
            self.assertEqual(
                [record["status"] for record in records],
                ["failed", "completed", "completed"],
            )
            self.assertEqual(records[0]["stage_id"], "plan")
            self.assertEqual(records[1]["stage_id"], "plan")
            self.assertEqual(len(provider_invocations(workspace)), 3)

    def test_provider_exit_exhausts_the_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"solo": SOLO})
            install_script(workspace, [{"exit": 1}])
            code, objects, stderr = run_main(
                workspace,
                ["start", "solo", "--task", "Write", "--attempts", "1"],
            )
            self.assertEqual(code, 3, stderr)
            self.assertEqual(objects[0]["stop_reason"], "retry_exhausted")
            self.assertEqual(objects[0]["session_status"], "active")
            records = execution_records(workspace, objects[0]["session_id"])
            self.assertEqual(records[0]["status"], "failed")
            self.assertIn("exited with code 1", records[0].get("error") or "")

    def test_provider_error_event_fails_the_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"solo": SOLO})
            install_script(
                workspace,
                [{"events": "error", "message": "quota exceeded"}],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "solo", "--task", "Write", "--attempts", "1"],
            )
            self.assertEqual(code, 3, stderr)
            self.assertEqual(objects[0]["stop_reason"], "retry_exhausted")
            records = execution_records(workspace, objects[0]["session_id"])
            self.assertEqual(records[0]["status"], "failed")
            self.assertEqual(len(provider_invocations(workspace)), 1)

    def test_complete_without_the_artifact_fails_the_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"solo": SOLO})
            install_script(
                workspace,
                [{"text": COMPLETE, "skip_artifact": True}],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "solo", "--task", "Write", "--attempts", "1"],
            )
            self.assertEqual(code, 3, stderr)
            records = execution_records(workspace, objects[0]["session_id"])
            self.assertEqual(records[0]["status"], "failed")
            self.assertIn("note.md", records[0].get("error") or "")

    def test_continue_finishes_the_stage_left_by_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan": PLAN_REVIEW})
            install_script(
                workspace,
                [
                    {"text": COMPLETE, "artifact": "# plan\n"},
                    {"text": APPROVED},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["stage", "--workflow", "plan", "--task", "Write a plan"],
            )
            self.assertEqual(code, 0, stderr)
            started = objects[0]
            self.assertEqual(started["stop_reason"], "stage")
            self.assertEqual(started["session_status"], "active")
            code, finished, stderr = run_main(
                workspace,
                ["continue", started["session_id"]],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(finished[0]["session_id"], started["session_id"])
            self.assertEqual(finished[0]["stop_reason"], "completed")
            published = (
                workspace
                / ".agentflow"
                / "sessions"
                / started["session_id"]
                / "outputs"
                / "plan"
            )
            self.assertEqual(published.read_text(encoding="utf-8"), "# plan\n")

    def test_dispatch_limit_leaves_a_session_continue_can_finish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan": PLAN_REVIEW})
            install_script(
                workspace,
                [
                    {"text": COMPLETE, "artifact": "# plan\n"},
                    {"text": APPROVED},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                [
                    "start",
                    "plan",
                    "--task",
                    "Write a plan",
                    "--max-dispatches",
                    "1",
                ],
            )
            self.assertEqual(code, 5, stderr)
            stopped = objects[0]
            self.assertEqual(stopped["stop_reason"], "dispatch_limit")
            self.assertEqual(stopped["session_status"], "active")
            self.assertEqual(len(provider_invocations(workspace)), 1)
            code, finished, stderr = run_main(
                workspace,
                ["continue", stopped["session_id"]],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(finished[0]["stop_reason"], "completed")
            self.assertEqual(len(provider_invocations(workspace)), 2)

    def test_grok_resume_from_passes_the_agent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"notes": resumed_notes("planner")})
            install_script(
                workspace,
                [
                    {"text": COMPLETE, "artifact": "# draft\n"},
                    {"text": COMPLETE, "artifact": "# follow\n"},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "notes", "--task", "Write notes"],
            )
            self.assertEqual(code, 0, stderr)
            invocations = provider_invocations(workspace)
            self.assertEqual([item["provider"] for item in invocations], ["grok", "grok"])
            first, second = (item["argv"] for item in invocations)
            self.assertTrue(has_args(first, "-m", "grok-test"))
            self.assertTrue(has_args(first, "--reasoning-effort", "low"))
            self.assertNotIn("--resume", first)
            self.assertTrue(has_args(second, "--resume", GROK_SESSION))
            session_id = objects[0]["session_id"]
            records = execution_records(workspace, session_id)
            self.assertEqual(records[1]["resumes_execution_id"], records[0]["execution_id"])
            self.assertEqual(records[1]["agent_id"], GROK_SESSION)
            session_dir = workspace / ".agentflow" / "sessions" / session_id
            self.assertEqual(
                (session_dir / "outputs" / "draft").read_text(encoding="utf-8"),
                "# draft\n",
            )
            follow_artifact = (
                session_dir
                / "executions"
                / records[1]["execution_id"]
                / "artifacts"
                / "follow.md"
            )
            self.assertEqual(follow_artifact.read_text(encoding="utf-8"), "# follow\n")

    def test_codex_resume_from_passes_the_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, CODEX_CONFIG, {"notes": resumed_notes("writer")})
            install_script(
                workspace,
                [
                    {"text": COMPLETE, "artifact": "# draft\n"},
                    {"text": COMPLETE, "artifact": "# follow\n"},
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "notes", "--task", "Write notes"],
            )
            self.assertEqual(code, 0, stderr)
            invocations = provider_invocations(workspace)
            self.assertEqual([item["provider"] for item in invocations], ["codex", "codex"])
            first, second = (item["argv"] for item in invocations)
            self.assertTrue(has_args(first, "-m", "codex-test"))
            self.assertTrue(has_args(first, "-c", 'model_reasoning_effort="low"'))
            self.assertIn("--approve-for-me", first)
            self.assertNotIn("resume", first)
            self.assertTrue(has_args(second, "resume", CODEX_THREAD, "-"))
            self.assertNotIn("--approve-for-me", second)
            records = execution_records(workspace, objects[0]["session_id"])
            self.assertEqual(records[1]["resumes_execution_id"], records[0]["execution_id"])
            self.assertEqual(records[1]["agent_id"], CODEX_THREAD)

    def test_grok_status_after_a_tool_event_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"solo": SOLO})
            install_script(
                workspace,
                [{"events": "tool", "text": COMPLETE, "artifact": "# noted\n"}],
            )
            code, objects, stderr = run_main(
                workspace,
                ["start", "solo", "--task", "Write"],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(objects[0]["stages"][0]["outcome_status"], "complete")
            self.assertEqual(objects[0]["stop_reason"], "completed")

    def test_codex_last_message_overrides_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, CODEX_CONFIG, {"draft-note": DRAFT_NOTE})
            install_script(
                workspace,
                [
                    {
                        "stdout_text": "not a status header",
                        "file_text": COMPLETE,
                        "artifact": "# noted\n",
                    }
                ],
            )
            code, objects, stderr = run_main(
                workspace,
                ["stage", "--workflow", "draft-note", "--task", "Draft a note"],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(objects[0]["stages"][0]["outcome_status"], "complete")
            self.assertEqual(objects[0]["stages"][0]["agent_id"], CODEX_THREAD)
            artifact = Path(objects[0]["stages"][0]["artifact_directory"]) / "note.md"
            self.assertEqual(artifact.read_text(encoding="utf-8"), "# noted\n")


if __name__ == "__main__":
    unittest.main()
