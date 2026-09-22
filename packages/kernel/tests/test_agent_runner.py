from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import json
import tempfile
import unittest

from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.session_records import create_agent_session, create_workflow_session
from agentflow_kernel.session_store import AgentSessionRecord
from tests.support import (
    BlockedAdapter,
    FakeAdapter,
    MissingOutcomeAdapter,
    repo_workflow_text,
)


class RunnerTests(unittest.TestCase):
    def test_reads_legacy_native_session_id_as_agent_id(self) -> None:
        record = AgentSessionRecord.from_json(
            {"native_session_id": "legacy-provider-id"},
            "grok",
            "grok-4.6",
            "/repo",
        )
        self.assertEqual(record.agent_id, "legacy-provider-id")
        self.assertNotIn("native_session_id", record.to_json())

    def test_resume_reuses_agent_id_on_a_new_execution(self) -> None:
        adapter = FakeAdapter()
        runner = AgentToolRunner(adapters={"fake": adapter}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            first = runner.run("fake", "m1", "first prompt", tmp)
            second = runner.run(
                "fake",
                "m1",
                "second prompt",
                tmp,
                session_id=first["session_id"],
                resume_execution_id=first["execution_id"],
            )
            execution = json.loads(
                (
                    Path(tmp)
                    / ".agentflow"
                    / "sessions"
                    / second["session_id"]
                    / "executions"
                    / second["execution_id"]
                    / "execution.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(second["session_id"], first["session_id"])
            self.assertNotEqual(second["execution_id"], first["execution_id"])
            self.assertEqual(adapter.calls[1]["agent_id"], "native-session-1")
            self.assertEqual(execution["resumes_execution_id"], first["execution_id"])

    def test_rejects_a_resume_execution_from_another_session(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            first = runner.run("fake", "m1", "one", tmp, session_id="run-a")
            with self.assertRaisesRegex(ValueError, "was not found in session"):
                runner.run(
                    "fake",
                    "m1",
                    "two",
                    tmp,
                    session_id="run-b",
                    resume_execution_id=first["execution_id"],
                )

    def test_rejects_a_thinking_level_change_when_resuming(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            first = runner.run("fake", "m1", "first", tmp, thinking="high")
            with self.assertRaisesRegex(
                ValueError, "different provider, model, or thinking"
            ):
                runner.run(
                    "fake",
                    "m1",
                    "second",
                    tmp,
                    session_id=first["session_id"],
                    resume_execution_id=first["execution_id"],
                    thinking="medium",
                )

    def test_records_loop_executions_without_overwriting_attempts(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = runner.run(
                "fake", "m1", "review", str(workspace), session_id="run_loop", stage_id="review"
            )
            second = runner.run(
                "fake",
                "m1",
                "review again",
                str(workspace),
                session_id="run_loop",
                stage_id="review",
                stage_attempt=2,
                execution_order=2,
            )
            executions = workspace / ".agentflow" / "sessions" / "run_loop" / "executions"
            self.assertEqual(
                sorted(path.name for path in executions.iterdir()),
                [first["execution_id"], second["execution_id"]],
            )
            self.assertTrue((executions / first["execution_id"] / "response.md").exists())

    def test_blocks_a_session_after_a_blocked_stage_outcome(self) -> None:
        runner = AgentToolRunner(adapters={"fake": BlockedAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = runner.run(
                "fake",
                "m1",
                "implement",
                str(workspace),
                session_id="blocked-run",
                stage_id="implement",
            )
            status = json.loads(
                (
                    workspace / ".agentflow" / "sessions" / "blocked-run" / "status.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["outcome_status"], "blocked")
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["blocked_by_execution_id"], result["execution_id"])
            with self.assertRaisesRegex(ValueError, "Session blocked-run is blocked"):
                runner.run(
                    "fake",
                    "m1",
                    "review",
                    str(workspace),
                    session_id="blocked-run",
                    stage_id="review-work",
                )
            self.assertEqual(
                len(
                    list(
                        (
                            workspace / ".agentflow" / "sessions" / "blocked-run" / "executions"
                        ).iterdir()
                    )
                ),
                1,
            )

    def test_fails_a_workflow_session_without_a_stage_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = AgentToolRunner(
                adapters={"fake": MissingOutcomeAdapter()}, timeout_sec=10
            ).run(
                "fake",
                "m1",
                "implement",
                str(workspace),
                session_id="missing-status-run",
                stage_id="implement",
            )
            session_directory = workspace / ".agentflow" / "sessions" / result["session_id"]
            execution = json.loads(
                (
                    session_directory
                    / "executions"
                    / result["execution_id"]
                    / "execution.json"
                ).read_text(encoding="utf-8")
            )
            status = json.loads((session_directory / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(execution["status"], "failed")
            self.assertIn("must start with", execution["error"])
            self.assertEqual(status["status"], "active")
            self.assertNotIn("terminal_execution_id", status)
            retry = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10).run(
                "fake",
                "m1",
                "implement",
                str(workspace),
                session_id="missing-status-run",
                stage_id="implement",
            )
            self.assertEqual(retry["outcome_status"], "complete")
            self.assertNotEqual(retry["execution_id"], result["execution_id"])
            retry_status = json.loads(
                (session_directory / "status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(retry_status["status"], "active")

    def test_constructs_the_default_adapter_registry(self) -> None:
        from agentflow_adapters import default_adapters

        first = default_adapters()
        second = default_adapters()
        self.assertEqual(set(AgentToolRunner().adapters), {"codex", "grok"})
        self.assertEqual(set(first), {"codex", "grok"})
        self.assertIsNot(first, second)
        self.assertIsNot(first["codex"], second["codex"])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Unsupported CLI provider"):
                AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10).run(
                    "missing", "m1", "prompt", tmp
                )

    def test_allows_another_execution_on_a_completed_agent_session(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_id = create_agent_session(workspace, "reviewer", "review the plan")
            first = runner.run(
                "fake", "m1", "first", str(workspace), session_id=session_id, stage_id="reviewer"
            )
            status_path = workspace / ".agentflow" / "sessions" / session_id / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed")
            second = runner.run(
                "fake", "m1", "second", str(workspace), session_id=session_id, stage_id="reviewer"
            )
            self.assertEqual(second["outcome_status"], "complete")
            self.assertNotEqual(second["execution_id"], first["execution_id"])

    def test_refuses_another_execution_on_a_completed_named_workflow_session(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            workflows = workspace / ".agentflow" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "plan-review.yaml").write_text(
                repo_workflow_text("plan-review"), encoding="utf-8"
            )
            session_id = create_workflow_session(workspace, "plan-review", "Write a plan")
            status_path = workspace / ".agentflow" / "sessions" / session_id / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["status"] = "completed"
            status["terminal_execution_id"] = "exec-1"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                rf"Session {session_id} is completed at exec-1; start a new session to continue\.",
            ):
                runner.run(
                    "fake",
                    "m1",
                    "again",
                    str(workspace),
                    session_id=session_id,
                    stage_id="plan",
                )


if __name__ == "__main__":
    unittest.main()
