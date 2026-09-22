from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import json
import tempfile
import unittest

from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.run_records import create_agent_run, create_workflow_run
from agentflow_kernel.session_store import SessionRecord
from tests.support import (
    BlockedAdapter,
    FakeAdapter,
    MissingOutcomeAdapter,
    repo_workflow_text,
)


class RunnerTests(unittest.TestCase):
    def test_reads_legacy_native_session_id_as_provider_session_id(self) -> None:
        record = SessionRecord.from_json(
            {"native_session_id": "legacy-provider-id"},
            "grok",
            "grok-4.6",
            "/repo",
        )
        self.assertEqual(record.provider_session_id, "legacy-provider-id")
        self.assertNotIn("native_session_id", record.to_json())

    def test_migrates_a_legacy_session_into_an_execution(self) -> None:
        adapter = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".sessions").mkdir()
            (workspace / ".sessions" / "legacy-runner-id.json").write_text(
                json.dumps(
                    {
                        "provider": "fake",
                        "model": "m1",
                        "workspace": str(workspace),
                        "native_session_id": "legacy-provider-id",
                        "status": "completed",
                        "history": [{"role": "user", "content": "first prompt"}],
                    }
                ),
                encoding="utf-8",
            )
            AgentToolRunner(adapters={"fake": adapter}, timeout_sec=10).run(
                "fake",
                "m1",
                "follow up",
                str(workspace),
                runner_session_id="legacy-runner-id",
            )
            self.assertEqual(
                adapter.calls[0]["provider_session_id"], "legacy-provider-id"
            )

    def test_resume_reuses_provider_session_on_a_new_execution(self) -> None:
        adapter = FakeAdapter()
        runner = AgentToolRunner(adapters={"fake": adapter}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            first = runner.run("fake", "m1", "first prompt", tmp)
            second = runner.run(
                "fake",
                "m1",
                "second prompt",
                tmp,
                runner_session_id=first["runner_session_id"],
            )
            execution = json.loads(
                (
                    Path(tmp)
                    / ".agentflow"
                    / "runs"
                    / second["run_id"]
                    / "executions"
                    / second["execution_id"]
                    / "execution.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(second["runner_session_id"], first["runner_session_id"])
            self.assertNotEqual(second["execution_id"], first["execution_id"])
            self.assertEqual(adapter.calls[1]["provider_session_id"], "native-session-1")
            self.assertEqual(execution["resumes_execution_id"], first["execution_id"])

    def test_rejects_a_session_bound_to_another_run(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            first = runner.run("fake", "m1", "one", tmp, run_id="run-a")
            with self.assertRaisesRegex(ValueError, "does not match runner session run"):
                runner.run(
                    "fake",
                    "m1",
                    "two",
                    tmp,
                    run_id="run-b",
                    runner_session_id=first["runner_session_id"],
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
                    runner_session_id=first["runner_session_id"],
                    thinking="medium",
                )

    def test_records_loop_executions_without_overwriting_attempts(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first = runner.run(
                "fake", "m1", "review", str(workspace), run_id="run_loop", stage_id="review"
            )
            second = runner.run(
                "fake",
                "m1",
                "review again",
                str(workspace),
                run_id="run_loop",
                stage_id="review",
                stage_attempt=2,
                execution_order=2,
            )
            executions = workspace / ".agentflow" / "runs" / "run_loop" / "executions"
            self.assertEqual(
                sorted(path.name for path in executions.iterdir()),
                [first["execution_id"], second["execution_id"]],
            )
            self.assertTrue((executions / first["execution_id"] / "response.md").exists())

    def test_blocks_a_run_after_a_blocked_stage_outcome(self) -> None:
        runner = AgentToolRunner(adapters={"fake": BlockedAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = runner.run(
                "fake",
                "m1",
                "implement",
                str(workspace),
                run_id="blocked-run",
                stage_id="implement",
            )
            status = json.loads(
                (
                    workspace / ".agentflow" / "runs" / "blocked-run" / "status.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["outcome_status"], "blocked")
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["blocked_by_execution_id"], result["execution_id"])
            with self.assertRaisesRegex(ValueError, "Run blocked-run is blocked"):
                runner.run(
                    "fake",
                    "m1",
                    "review",
                    str(workspace),
                    run_id="blocked-run",
                    stage_id="review-work",
                )
            self.assertEqual(
                len(
                    list(
                        (
                            workspace / ".agentflow" / "runs" / "blocked-run" / "executions"
                        ).iterdir()
                    )
                ),
                1,
            )

    def test_fails_a_workflow_run_without_a_stage_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = AgentToolRunner(
                adapters={"fake": MissingOutcomeAdapter()}, timeout_sec=10
            ).run(
                "fake",
                "m1",
                "implement",
                str(workspace),
                run_id="missing-status-run",
                stage_id="implement",
            )
            run_directory = workspace / ".agentflow" / "runs" / result["run_id"]
            execution = json.loads(
                (
                    run_directory
                    / "executions"
                    / result["execution_id"]
                    / "execution.json"
                ).read_text(encoding="utf-8")
            )
            status = json.loads((run_directory / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(execution["status"], "failed")
            self.assertIn("must start with", execution["error"])
            self.assertEqual(status["status"], "active")
            self.assertNotIn("terminal_execution_id", status)
            retry = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10).run(
                "fake",
                "m1",
                "implement",
                str(workspace),
                run_id="missing-status-run",
                stage_id="implement",
            )
            self.assertEqual(retry["outcome_status"], "complete")
            self.assertNotEqual(retry["execution_id"], result["execution_id"])
            retry_status = json.loads(
                (run_directory / "status.json").read_text(encoding="utf-8")
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

    def test_allows_another_execution_on_a_completed_agent_run(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_id = create_agent_run(workspace, "reviewer", "review the plan")
            first = runner.run(
                "fake", "m1", "first", str(workspace), run_id=run_id, stage_id="reviewer"
            )
            status_path = workspace / ".agentflow" / "runs" / run_id / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed")
            second = runner.run(
                "fake", "m1", "second", str(workspace), run_id=run_id, stage_id="reviewer"
            )
            self.assertEqual(second["outcome_status"], "complete")
            self.assertNotEqual(second["execution_id"], first["execution_id"])

    def test_refuses_another_execution_on_a_completed_named_workflow_run(self) -> None:
        runner = AgentToolRunner(adapters={"fake": FakeAdapter()}, timeout_sec=10)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            workflows = workspace / ".agentflow" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "plan-review.yaml").write_text(
                repo_workflow_text("plan-review"), encoding="utf-8"
            )
            run_id = create_workflow_run(workspace, "plan-review", "Write a plan")
            status_path = workspace / ".agentflow" / "runs" / run_id / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["status"] = "completed"
            status["terminal_execution_id"] = "exec-1"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                rf"Run {run_id} is completed at exec-1; start a new run to continue\.",
            ):
                runner.run(
                    "fake",
                    "m1",
                    "again",
                    str(workspace),
                    run_id=run_id,
                    stage_id="plan",
                )


if __name__ == "__main__":
    unittest.main()
