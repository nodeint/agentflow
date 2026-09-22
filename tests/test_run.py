from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from agentflow_kernel.config import ConfigurationError
from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.query import list_run_rows
from agentflow_cli.run import execute_workflow, list_runs, parse_args, watch_run
from tests.support import (
    ScriptedAdapter,
    repo_workflow_text,
    write_fake_workflow_workspace,
)


def _write_watch_run(
    workspace: Path,
    *,
    events: list[dict],
    execution: dict,
    run_status: dict | None = None,
) -> None:
    execution_id = "0001-implement--attempt-01"
    run_directory = workspace / ".agentflow" / "runs" / "run-1"
    execution_directory = run_directory / "executions" / execution_id
    execution_directory.mkdir(parents=True)
    (run_directory / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    payload = {
        "execution_id": execution_id,
        "stage_id": "implement",
        "status": "running",
        **execution,
    }
    (execution_directory / "execution.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    status = {
        "run_id": "run-1",
        "status": "active",
        "latest_execution_id": execution_id,
        "latest_execution_status": payload.get("status", "running"),
        "updated_at": "2026-09-19T10:20:30Z",
    }
    if run_status is not None:
        status.update(run_status)
    (run_directory / "status.json").write_text(json.dumps(status), encoding="utf-8")


class WatchCommandTests(unittest.TestCase):
    def test_watch_once_prints_a_snapshot_without_replaying_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_watch_run(
                workspace,
                events=[
                    {
                        "event": "provider.heartbeat",
                        "stage_id": "implement",
                        "timestamp": "2026-09-19T10:20:30Z",
                        "summary": "running for 30s",
                    }
                ],
                execution={"status": "running"},
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(watch_run(workspace, "run-1", once=True), 0)
        output = stdout.getvalue()
        self.assertIn("run_id: run-1", output)
        self.assertIn("status: active", output)
        self.assertIn("events_cursor:", output)
        self.assertNotIn("heartbeat", output)

    def test_until_terminal_reads_execution_json_not_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_watch_run(
                workspace,
                events=[
                    {
                        "event": "execution.failed",
                        "execution_id": "0001-implement--attempt-01",
                        "stage_id": "implement",
                        "timestamp": "2026-09-19T10:20:30Z",
                    }
                ],
                execution={"status": "failed", "error": "missing status header"},
                run_status={"status": "active"},
            )
            self.assertEqual(
                watch_run(
                    workspace,
                    "run-1",
                    execution_id="0001-implement--attempt-01",
                    until_terminal=True,
                ),
                1,
            )


def _run_execute(
    workspace: Path, arguments: list[str], adapter: ScriptedAdapter
) -> tuple[int, list[dict]]:
    args = parse_args(["execute", *arguments])
    stdout = io.StringIO()
    with redirect_stderr(io.StringIO()), patch("sys.stdout", stdout):
        code = execute_workflow(
            workspace,
            args,
            runner=AgentToolRunner(adapters={"fake": adapter}, timeout_sec=10),
        )
    objects = [
        json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()
    ]
    return code, objects


class ListRunsTests(unittest.TestCase):
    def test_lists_sorted_rows_and_skips_invalid_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runs = workspace / ".agentflow" / "runs"
            for name, payload in (
                (
                    "older",
                    {
                        "updated_at": "2026-09-19T00:00:00+00:00",
                        "workflow_id": "plan-review",
                        "status": "completed",
                        "latest_execution_id": "0001",
                        "latest_decision": "approved",
                        "outputs": {"plan": {}},
                        "task": "old task",
                    },
                ),
                (
                    "newer",
                    {
                        "updated_at": "2026-09-20T00:00:00+00:00",
                        "workflow_id": "plan-implement",
                        "status": "active",
                        "task": "new task",
                    },
                ),
            ):
                directory = runs / name
                directory.mkdir(parents=True)
                (directory / "status.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
            broken = runs / "broken"
            broken.mkdir()
            (broken / "status.json").write_text("{", encoding="utf-8")
            self.assertEqual(
                [row[1] for row in list_run_rows(workspace)],
                ["newer", "older"],
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(list_runs(workspace), 0)
            text = stdout.getvalue()
            self.assertLess(text.index("newer"), text.index("older"))
            self.assertIn(
                "newer · plan-implement · active · not started · -- · none · new task",
                text,
            )
            self.assertIn(
                "older · plan-review · completed · 0001 · approved · plan · old task",
                text,
            )
            self.assertNotIn("broken", text)


class ExecuteWorkflowTests(unittest.TestCase):
    def test_execute_completes_plan_review_and_publishes_plan(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned",
                "status: complete\ndecision: approved\n\nok",
            ],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            ["--workflow-id", "plan-review", "--task", "Write a plan"],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[-1]["run_status"], "completed")
        self.assertEqual(objects[-1]["stop_reason"], "completed")
        self.assertIn("plan", objects[-1]["outputs"])
        self.assertEqual(objects[0]["outcome_status"], "complete")
        self.assertEqual(objects[1]["outcome_decision"], "approved")
        published = (
            workspace
            / ".agentflow"
            / "runs"
            / objects[-1]["run_id"]
            / "outputs"
            / "plan"
        )
        self.assertTrue(published.is_file())
        self.assertTrue(published.read_text(encoding="utf-8").strip())

    def test_execute_reruns_plan_then_review_after_revise(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned v1",
                "status: complete\ndecision: revise\n\nneeds work",
                "status: complete\n\nplanned v2",
                "status: complete\ndecision: approved\n\nok",
            ],
            artifacts=["# Plan v1\n", None, "# Plan v2\n", None],
        )
        code, objects = _run_execute(
            workspace,
            ["--workflow-id", "plan-review", "--task", "Write a plan"],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [item.get("outcome_decision") for item in objects[:-1]],
            ["", "revise", "", "approved"],
        )
        self.assertEqual(objects[-1]["stop_reason"], "completed")

    def test_execute_requires_prior_run_then_completes_plan_implement(self) -> None:
        requiring = (
            "requires:\n  workflow: plan-review\n  decision: approved\n"
            + repo_workflow_text("plan-implement")
        )
        workspace = write_fake_workflow_workspace(
            {
                "plan-review": repo_workflow_text("plan-review"),
                "plan-implement": requiring,
            }
        )
        with self.assertRaisesRegex(ConfigurationError, "pass --prior-run-id"):
            _run_execute(
                workspace,
                ["--workflow-id", "plan-implement", "--task", "Do work"],
                ScriptedAdapter(["status: complete\n\nx"], artifacts=["# x\n"]),
            )
        with self.assertRaisesRegex(ConfigurationError, "pass --prior-run-id"):
            _run_execute(
                workspace,
                [
                    "--workflow-id",
                    "plan-implement",
                    "--task",
                    "Do work",
                    "--stage-id",
                    "write-tests",
                ],
                ScriptedAdapter(["status: complete\n\nx"], artifacts=["# x\n"]),
            )
        self.assertFalse((workspace / ".agentflow" / "runs").exists())
        prior_code, prior_objects = _run_execute(
            workspace,
            ["--workflow-id", "plan-review", "--task", "Write a plan"],
            ScriptedAdapter(
                [
                    "status: complete\n\nplanned",
                    "status: complete\ndecision: approved\n\nok",
                ],
                artifacts=["# Plan\n", None],
            ),
        )
        self.assertEqual(prior_code, 0)
        prior_id = prior_objects[-1]["run_id"]
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nimplemented",
                "status: complete\ndecision: approved\n\nok",
                "status: complete\n\ntests written",
                "status: complete\ndecision: approved\n\nok",
            ],
            artifacts=["# impl\n", None, "# tests\n", None],
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "plan-implement",
                "--task",
                "Do work",
                "--prior-run-id",
                prior_id,
            ],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            set(objects[-1]["outputs"]),
            {"implementation-result", "test-result"},
        )
        self.assertEqual(objects[0]["runner_session_id"], objects[2]["runner_session_id"])

    def test_execute_run_id_on_terminal_runs_does_not_dispatch(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        blocked_adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned",
                "status: blocked\nblocker: missing design\n\nnope",
            ],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            ["--workflow-id", "plan-review", "--task", "Write a plan"],
            blocked_adapter,
        )
        self.assertEqual(code, 1)
        self.assertEqual(objects[-1]["stop_reason"], "blocked")
        run_id = objects[-1]["run_id"]
        executions = list(
            (workspace / ".agentflow" / "runs" / run_id / "executions").iterdir()
        )
        idle = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        code2, objects2 = _run_execute(
            workspace, ["--run-id", run_id], idle
        )
        self.assertEqual(code2, 1)
        self.assertEqual(len(objects2), 1)
        self.assertEqual(idle.calls, [])
        self.assertEqual(
            list((workspace / ".agentflow" / "runs" / run_id / "executions").iterdir()),
            executions,
        )
        blocked_again = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        stage_code, stage_objects = _run_execute(
            workspace,
            ["--run-id", run_id, "--stage-id", "review-plan"],
            blocked_again,
        )
        self.assertEqual(stage_code, 1)
        self.assertEqual(stage_objects[-1]["stop_reason"], "blocked")
        self.assertEqual(blocked_again.calls, [])
        self.assertEqual(
            list((workspace / ".agentflow" / "runs" / run_id / "executions").iterdir()),
            executions,
        )

        completed_workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        done_code, done_objects = _run_execute(
            completed_workspace,
            ["--workflow-id", "plan-review", "--task", "Write a plan"],
            ScriptedAdapter(
                [
                    "status: complete\n\nplanned",
                    "status: complete\ndecision: approved\n\nok",
                ],
                artifacts=["# Plan\n", None],
            ),
        )
        self.assertEqual(done_code, 0)
        completed_id = done_objects[-1]["run_id"]
        unused = ScriptedAdapter(["status: complete\n\nnope"], artifacts=["# x\n"])
        code3, objects3 = _run_execute(
            completed_workspace, ["--run-id", completed_id], unused
        )
        self.assertEqual(code3, 0)
        self.assertEqual(objects3[-1]["stop_reason"], "completed")
        self.assertEqual(unused.calls, [])
        unused_stage = ScriptedAdapter(["status: complete\n\nnope"], artifacts=["# x\n"])
        code_stage, objects_stage = _run_execute(
            completed_workspace,
            ["--run-id", completed_id, "--stage-id", "plan"],
            unused_stage,
        )
        self.assertEqual(code_stage, 0)
        self.assertEqual(objects_stage[-1]["stop_reason"], "completed")
        self.assertEqual(unused_stage.calls, [])

        cancelled_id = done_objects[-1]["run_id"]
        status_path = (
            completed_workspace
            / ".agentflow"
            / "runs"
            / cancelled_id
            / "status.json"
        )
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["status"] = "cancelled"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        unused2 = ScriptedAdapter(["status: complete\n\nnope"], artifacts=["# x\n"])
        code4, objects4 = _run_execute(
            completed_workspace, ["--run-id", cancelled_id], unused2
        )
        self.assertEqual(code4, 130)
        self.assertEqual(objects4[-1]["stop_reason"], "cancelled")
        self.assertEqual(unused2.calls, [])

    def test_retry_budget_keeps_run_active_and_stage_can_continue(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        adapter = ScriptedAdapter(
            ["status: complete\n\nplanned", "not a header"],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "plan-review",
                "--task",
                "Write a plan",
                "--max-attempts",
                "1",
            ],
            adapter,
        )
        self.assertEqual(code, 3)
        self.assertEqual(objects[-1]["stop_reason"], "retry_exhausted")
        self.assertEqual(objects[-1]["run_status"], "active")
        run_id = objects[-1]["run_id"]
        refused = ScriptedAdapter(
            ["status: complete\n\nplanned again"], artifacts=["# Plan\n"]
        )
        with self.assertRaisesRegex(
            ConfigurationError, "not the eligible stage review-plan"
        ):
            _run_execute(
                workspace,
                ["--run-id", run_id, "--stage-id", "plan"],
                refused,
            )
        self.assertEqual(refused.calls, [])
        stage_code, stage_objects = _run_execute(
            workspace,
            ["--run-id", run_id, "--stage-id", "review-plan"],
            ScriptedAdapter(
                ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
            ),
        )
        self.assertEqual(stage_code, 0)
        self.assertEqual(stage_objects[0]["outcome_decision"], "approved")
        self.assertEqual(stage_objects[-1]["run_status"], "completed")
        self.assertEqual(stage_objects[-1]["stop_reason"], "completed")

    def test_retry_budget_is_retained_across_execute_invocations(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        first = ScriptedAdapter(
            ["status: complete\n\nplanned", "not a header"],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "plan-review",
                "--task",
                "Write a plan",
                "--max-attempts",
                "1",
            ],
            first,
        )
        self.assertEqual(code, 3)
        run_id = objects[-1]["run_id"]
        second = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        code2, objects2 = _run_execute(
            workspace,
            ["--run-id", run_id, "--max-attempts", "1"],
            second,
        )
        self.assertEqual(code2, 3)
        self.assertEqual(objects2[-1]["stop_reason"], "retry_exhausted")
        self.assertEqual(objects2[-1]["run_status"], "active")
        self.assertEqual(second.calls, [])

    def test_rejects_a_non_positive_max_attempts(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        args = parse_args(
            [
                "execute",
                "--workflow-id",
                "plan-review",
                "--task",
                "Write a plan",
                "--max-attempts",
                "0",
            ]
        )
        with self.assertRaisesRegex(ConfigurationError, "--max-attempts"):
            execute_workflow(workspace, args)

    def test_stage_id_runs_the_first_stage_and_stops(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned",
                "status: complete\ndecision: approved\n\nshould not run",
            ],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "plan-review",
                "--task",
                "Write a plan",
                "--stage-id",
                "plan",
            ],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[0]["outcome_status"], "complete")
        self.assertEqual(objects[-1]["stop_reason"], "stage")
        self.assertEqual(objects[-1]["run_status"], "active")
        self.assertEqual(len(adapter.calls), 1)

    def test_stage_id_of_a_later_stage_creates_no_run(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        adapter = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        with self.assertRaisesRegex(
            ConfigurationError, "not the eligible stage plan"
        ):
            _run_execute(
                workspace,
                [
                    "--workflow-id",
                    "plan-review",
                    "--task",
                    "Write a plan",
                    "--stage-id",
                    "review-plan",
                ],
                adapter,
            )
        self.assertFalse((workspace / ".agentflow" / "runs").exists())
        self.assertEqual(adapter.calls, [])

    def test_stage_id_follows_eligibility_after_the_first_stage(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        first = ScriptedAdapter(
            ["status: complete\n\nplanned"], artifacts=["# Plan\n"]
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "plan-review",
                "--task",
                "Write a plan",
                "--stage-id",
                "plan",
            ],
            first,
        )
        self.assertEqual(code, 0)
        run_id = objects[-1]["run_id"]
        executions = list(
            (workspace / ".agentflow" / "runs" / run_id / "executions").iterdir()
        )
        repeated = ScriptedAdapter(
            ["status: complete\n\nplanned again"], artifacts=["# Plan\n"]
        )
        with self.assertRaisesRegex(
            ConfigurationError, "not the eligible stage review-plan"
        ):
            _run_execute(
                workspace,
                ["--run-id", run_id, "--stage-id", "plan"],
                repeated,
            )
        self.assertEqual(repeated.calls, [])
        self.assertEqual(
            list((workspace / ".agentflow" / "runs" / run_id / "executions").iterdir()),
            executions,
        )
        review = ScriptedAdapter(
            [
                "status: complete\ndecision: revise\n\nneeds work",
                "status: complete\n\nshould not run",
            ],
            artifacts=[None, "# Plan\n"],
        )
        code2, objects2 = _run_execute(
            workspace,
            ["--run-id", run_id, "--stage-id", "review-plan"],
            review,
        )
        self.assertEqual(code2, 0)
        self.assertEqual(objects2[0]["outcome_decision"], "revise")
        self.assertEqual(objects2[-1]["stop_reason"], "stage")
        self.assertEqual(objects2[-1]["run_status"], "active")
        self.assertEqual(len(review.calls), 1)

    def test_stage_id_ignores_a_non_positive_max_attempts(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan-review": repo_workflow_text("plan-review")}
        )
        adapter = ScriptedAdapter(
            ["status: complete\n\nplanned"], artifacts=["# Plan\n"]
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "plan-review",
                "--task",
                "Write a plan",
                "--stage-id",
                "plan",
                "--max-attempts",
                "0",
            ],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[-1]["stop_reason"], "stage")
        self.assertEqual(len(adapter.calls), 1)

    def test_stage_id_resumes_the_declared_source_session(self) -> None:
        workflow = """\
id: resume-sample
stages:
  - id: draft
    role: developer
    depends_on: []
    produces:
      artifact: draft.md
  - id: revise-draft
    role: developer
    session:
      resume_from: draft
    depends_on:
      - draft
    produces:
      artifact: revision.md
"""
        workspace = write_fake_workflow_workspace({"resume-sample": workflow})
        first = ScriptedAdapter(
            ["status: complete\n\ndraft"], artifacts=["# draft\n"]
        )
        code, objects = _run_execute(
            workspace,
            [
                "--workflow-id",
                "resume-sample",
                "--task",
                "Draft",
                "--stage-id",
                "draft",
            ],
            first,
        )
        self.assertEqual(code, 0)
        run_id = objects[-1]["run_id"]
        second = ScriptedAdapter(
            ["status: complete\n\nrevised"], artifacts=["# revision\n"]
        )
        code2, objects2 = _run_execute(
            workspace,
            ["--run-id", run_id, "--stage-id", "revise-draft"],
            second,
        )
        self.assertEqual(code2, 0)
        self.assertEqual(
            objects2[0]["runner_session_id"], objects[0]["runner_session_id"]
        )
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(second.calls[0]["provider_session_id"], "native-session-1")
        self.assertIsNone(second.calls[0]["new_provider_session_id"])


if __name__ == "__main__":
    unittest.main()
