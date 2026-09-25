from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from agentflow_kernel.config import ConfigurationError
from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.query import list_session_rows
from agentflow_cli.run import execute_workflow, list_sessions, main, watch_session
from tests.support import (
    ScriptedAdapter,
    repo_workflow_text,
    write_fake_workflow_workspace,
)


def _write_watch_session(
    workspace: Path,
    *,
    events: list[dict],
    execution: dict,
    session_status: dict | None = None,
) -> None:
    execution_id = "0001-implement--attempt-01"
    session_directory = workspace / ".agentflow" / "sessions" / "run-1"
    execution_directory = session_directory / "executions" / execution_id
    execution_directory.mkdir(parents=True)
    (session_directory / "events.jsonl").write_text(
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
        "session_id": "run-1",
        "status": "active",
        "latest_execution_id": execution_id,
        "latest_execution_status": payload.get("status", "running"),
        "updated_at": "2026-09-19T10:20:30Z",
    }
    if session_status is not None:
        status.update(session_status)
    (session_directory / "status.json").write_text(json.dumps(status), encoding="utf-8")


class WatchCommandTests(unittest.TestCase):
    def test_watch_once_prints_a_snapshot_without_replaying_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_watch_session(
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
                self.assertEqual(watch_session(workspace, "run-1", once=True), 0)
        output = stdout.getvalue()
        self.assertIn("run-1", output)
        self.assertIn("active", output)
        self.assertIn("events_cursor", output)
        self.assertNotIn("heartbeat", output)
        self.assertNotIn("running for 30s", output)

    def test_until_terminal_reads_execution_json_not_session_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_watch_session(
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
                session_status={"status": "active"},
            )
            self.assertEqual(
                watch_session(
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
    runner = AgentToolRunner(adapters={"fake": adapter}, timeout_sec=10)
    stdout = io.StringIO()
    with (
        redirect_stderr(io.StringIO()),
        patch("sys.stdout", stdout),
        patch("agentflow_cli.commands.execute.AgentToolRunner", return_value=runner),
    ):
        code = main(arguments, cwd=workspace)
    payload = json.loads(stdout.getvalue())
    summary = {
        "session_id": payload["session_id"],
        "session_status": payload["session_status"],
        "stop_reason": payload["stop_reason"],
        "outputs": payload["outputs"],
    }
    return code, [*payload["stages"], summary]


class ListRunsTests(unittest.TestCase):
    def test_lists_sorted_rows_and_skips_invalid_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            runs = workspace / ".agentflow" / "sessions"
            for name, payload in (
                (
                    "older",
                    {
                        "updated_at": "2026-09-19T00:00:00+00:00",
                        "workflow_id": "plan",
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
                [row[1] for row in list_session_rows(workspace)],
                ["newer", "older"],
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(list_sessions(workspace), 0)
            text = stdout.getvalue()
            self.assertLess(text.index("newer"), text.index("older"))
            self.assertIn("plan-implement", text)
            self.assertIn("not started", text)
            self.assertIn("new task", text)
            self.assertIn("plan", text)
            self.assertIn("0001", text)
            self.assertIn("approved", text)
            self.assertIn("old task", text)
            self.assertNotIn("broken", text)
            json_out = io.StringIO()
            with patch("sys.stdout", json_out):
                self.assertEqual(list_sessions(workspace, as_json=True), 0)
            rows = json.loads(json_out.getvalue())
            self.assertEqual([row["session"] for row in rows], ["newer", "older"])
            self.assertEqual(rows[1]["decision"], "approved")


class ExecuteWorkflowTests(unittest.TestCase):
    def test_execute_completes_plan_review_and_publishes_plan(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
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
            ["start", "plan", "--task", "Write a plan"],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[-1]["session_status"], "completed")
        self.assertEqual(objects[-1]["stop_reason"], "completed")
        self.assertIn("plan", objects[-1]["outputs"])
        self.assertEqual(objects[0]["outcome_status"], "complete")
        self.assertEqual(objects[1]["outcome_decision"], "approved")
        published = (
            workspace
            / ".agentflow"
            / "sessions"
            / objects[-1]["session_id"]
            / "outputs"
            / "plan"
        )
        self.assertTrue(published.is_file())
        self.assertTrue(published.read_text(encoding="utf-8").strip())

    def test_execute_reruns_plan_then_review_after_revise(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
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
            ["start", "plan", "--task", "Write a plan"],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [item.get("outcome_decision") for item in objects[:-1]],
            ["", "revise", "", "approved"],
        )
        self.assertEqual(objects[-1]["stop_reason"], "completed")

    def test_execute_requires_prior_session_then_completes_plan_implement(self) -> None:
        requiring = (
            "requires:\n  workflow: plan\n  decision: approved\n"
            + repo_workflow_text("plan-implement")
        )
        workspace = write_fake_workflow_workspace(
            {
                "plan": repo_workflow_text("plan"),
                "plan-implement": requiring,
            }
        )
        for argv in (
            ["start", "plan-implement", "--task", "Do work"],
            [
                "stage",
                "--workflow",
                "plan-implement",
                "--task",
                "Do work",
            ],
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(argv, cwd=workspace)
            self.assertEqual(code, 2)
            self.assertIn("pass --prior", stderr.getvalue())
        self.assertFalse((workspace / ".agentflow" / "sessions").exists())
        prior_code, prior_objects = _run_execute(
            workspace,
            ["start", "plan", "--task", "Write a plan"],
            ScriptedAdapter(
                [
                    "status: complete\n\nplanned",
                    "status: complete\ndecision: approved\n\nok",
                ],
                artifacts=["# Plan\n", None],
            ),
        )
        self.assertEqual(prior_code, 0)
        prior_id = prior_objects[-1]["session_id"]
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
                "start",
                "plan-implement",
                "--task",
                "Do work",
                "--prior",
                prior_id,
            ],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            set(objects[-1]["outputs"]),
            {"implementation-result", "test-result"},
        )
        resumed = json.loads(
            (
                workspace
                / ".agentflow"
                / "sessions"
                / objects[-1]["session_id"]
                / "executions"
                / objects[2]["execution_id"]
                / "execution.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(resumed["resumes_execution_id"], objects[0]["execution_id"])
        self.assertEqual(adapter.calls[2]["agent_id"], "native-session-1")
        self.assertIsNone(adapter.calls[0]["agent_id"])

    def test_execute_session_id_on_terminal_sessions_does_not_dispatch(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
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
            ["start", "plan", "--task", "Write a plan"],
            blocked_adapter,
        )
        self.assertEqual(code, 1)
        self.assertEqual(objects[-1]["stop_reason"], "blocked")
        session_id = objects[-1]["session_id"]
        executions = list(
            (workspace / ".agentflow" / "sessions" / session_id / "executions").iterdir()
        )
        idle = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        code2, objects2 = _run_execute(
            workspace, ["continue", session_id], idle
        )
        self.assertEqual(code2, 1)
        self.assertEqual(len(objects2), 1)
        self.assertEqual(idle.calls, [])
        self.assertEqual(
            list((workspace / ".agentflow" / "sessions" / session_id / "executions").iterdir()),
            executions,
        )
        blocked_again = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        stage_code, stage_objects = _run_execute(
            workspace,
            ["stage", session_id],
            blocked_again,
        )
        self.assertEqual(stage_code, 1)
        self.assertEqual(stage_objects[-1]["stop_reason"], "blocked")
        self.assertEqual(blocked_again.calls, [])
        self.assertEqual(
            list((workspace / ".agentflow" / "sessions" / session_id / "executions").iterdir()),
            executions,
        )

        completed_workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
        )
        done_code, done_objects = _run_execute(
            completed_workspace,
            ["start", "plan", "--task", "Write a plan"],
            ScriptedAdapter(
                [
                    "status: complete\n\nplanned",
                    "status: complete\ndecision: approved\n\nok",
                ],
                artifacts=["# Plan\n", None],
            ),
        )
        self.assertEqual(done_code, 0)
        completed_id = done_objects[-1]["session_id"]
        unused = ScriptedAdapter(["status: complete\n\nnope"], artifacts=["# x\n"])
        code3, objects3 = _run_execute(
            completed_workspace, ["continue", completed_id], unused
        )
        self.assertEqual(code3, 0)
        self.assertEqual(objects3[-1]["stop_reason"], "completed")
        self.assertEqual(unused.calls, [])
        unused_stage = ScriptedAdapter(["status: complete\n\nnope"], artifacts=["# x\n"])
        code_stage, objects_stage = _run_execute(
            completed_workspace,
            ["stage", completed_id],
            unused_stage,
        )
        self.assertEqual(code_stage, 0)
        self.assertEqual(objects_stage[-1]["stop_reason"], "completed")
        self.assertEqual(unused_stage.calls, [])

        cancelled_id = done_objects[-1]["session_id"]
        status_path = (
            completed_workspace
            / ".agentflow"
            / "sessions"
            / cancelled_id
            / "status.json"
        )
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["status"] = "cancelled"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        unused2 = ScriptedAdapter(["status: complete\n\nnope"], artifacts=["# x\n"])
        code4, objects4 = _run_execute(
            completed_workspace, ["continue", cancelled_id], unused2
        )
        self.assertEqual(code4, 130)
        self.assertEqual(objects4[-1]["stop_reason"], "cancelled")
        self.assertEqual(unused2.calls, [])

    def test_retry_budget_keeps_session_active_and_stage_can_continue(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
        )
        adapter = ScriptedAdapter(
            ["status: complete\n\nplanned", "not a header"],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            [
                "start",
                "plan",
                "--task",
                "Write a plan",
                "--attempts",
                "1",
            ],
            adapter,
        )
        self.assertEqual(code, 3)
        self.assertEqual(objects[-1]["stop_reason"], "retry_exhausted")
        self.assertEqual(objects[-1]["session_status"], "active")
        session_id = objects[-1]["session_id"]
        stage_code, stage_objects = _run_execute(
            workspace,
            ["stage", session_id],
            ScriptedAdapter(
                ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
            ),
        )
        self.assertEqual(stage_code, 0)
        self.assertEqual(stage_objects[0]["outcome_decision"], "approved")
        self.assertEqual(stage_objects[-1]["session_status"], "completed")
        self.assertEqual(stage_objects[-1]["stop_reason"], "completed")

    def test_retry_budget_is_retained_across_execute_invocations(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
        )
        first = ScriptedAdapter(
            ["status: complete\n\nplanned", "not a header"],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            [
                "start",
                "plan",
                "--task",
                "Write a plan",
                "--attempts",
                "1",
            ],
            first,
        )
        self.assertEqual(code, 3)
        session_id = objects[-1]["session_id"]
        second = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        code2, objects2 = _run_execute(
            workspace,
            ["continue", session_id, "--attempts", "1"],
            second,
        )
        self.assertEqual(code2, 3)
        self.assertEqual(objects2[-1]["stop_reason"], "retry_exhausted")
        self.assertEqual(objects2[-1]["session_status"], "active")
        self.assertEqual(second.calls, [])

    def test_rejects_a_non_positive_max_attempts(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
        )
        with self.assertRaisesRegex(ConfigurationError, "--attempts"):
            execute_workflow(
                workspace,
                action="start",
                workflow_id="plan",
                task="Write a plan",
                max_attempts=0,
            )

    def test_stage_id_runs_the_first_stage_and_stops(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
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
                "stage",
                "--workflow",
                "plan",
                "--task",
                "Write a plan",
            ],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[0]["outcome_status"], "complete")
        self.assertEqual(objects[-1]["stop_reason"], "stage")
        self.assertEqual(objects[-1]["session_status"], "active")
        self.assertEqual(len(adapter.calls), 1)

    def test_stage_id_of_a_later_stage_creates_no_session(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
        )
        adapter = ScriptedAdapter(
            ["status: complete\ndecision: approved\n\nok"], artifacts=[None]
        )
        code, objects = _run_execute(
            workspace,
            ["stage", "--workflow", "plan", "--task", "Write a plan"],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[-1]["stop_reason"], "stage")
        self.assertEqual(len(adapter.calls), 1)

    def test_stage_id_follows_eligibility_after_the_first_stage(self) -> None:
        workspace = write_fake_workflow_workspace(
            {"plan": repo_workflow_text("plan")}
        )
        first = ScriptedAdapter(
            ["status: complete\n\nplanned"], artifacts=["# Plan\n"]
        )
        code, objects = _run_execute(
            workspace,
            [
                "stage",
                "--workflow",
                "plan",
                "--task",
                "Write a plan",
            ],
            first,
        )
        self.assertEqual(code, 0)
        session_id = objects[-1]["session_id"]
        executions = list(
            (workspace / ".agentflow" / "sessions" / session_id / "executions").iterdir()
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
            ["stage", session_id],
            review,
        )
        self.assertEqual(code2, 0)
        self.assertEqual(objects2[0]["outcome_decision"], "revise")
        self.assertEqual(objects2[-1]["stop_reason"], "stage")
        self.assertEqual(objects2[-1]["session_status"], "active")
        self.assertEqual(len(review.calls), 1)

    def test_stage_id_rejects_max_attempts(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "stage",
                    "--workflow",
                    "plan",
                    "--task",
                    "Write a plan",
                    "--attempts",
                    "1",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("--attempts", stderr.getvalue())

    def test_stage_rejects_a_dispatch_ceiling(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "stage",
                    "--workflow",
                    "plan",
                    "--task",
                    "Write a plan",
                    "--max-dispatches",
                    "2",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("--max-dispatches", stderr.getvalue())

    def test_stage_id_resumes_the_declared_source_session(self) -> None:
        workflow = """\
schema_version: 1
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
                "stage",
                "--workflow",
                "resume-sample",
                "--task",
                "Draft",
            ],
            first,
        )
        self.assertEqual(code, 0)
        session_id = objects[-1]["session_id"]
        second = ScriptedAdapter(
            ["status: complete\n\nrevised"], artifacts=["# revision\n"]
        )
        code2, objects2 = _run_execute(
            workspace,
            ["stage", session_id],
            second,
        )
        self.assertEqual(code2, 0)
        resumed = json.loads(
            (
                workspace
                / ".agentflow"
                / "sessions"
                / session_id
                / "executions"
                / objects2[0]["execution_id"]
                / "execution.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(resumed["resumes_execution_id"], objects[0]["execution_id"])
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(second.calls[0]["agent_id"], "native-session-1")
        self.assertIsNone(second.calls[0]["new_agent_id"])


_REVISION_LOOP = """\
schema_version: 1
id: loop
stages:
  - id: plan
    role: planner
    max_revisions: 0
    produces:
      artifact: plan.md
  - id: review-plan
    role: reviewer
    depends_on:
      - plan
    decision:
      values:
        - approved
        - revise
      routes:
        approved: complete
        revise: plan
"""

_OPEN_LOOP = """\
schema_version: 1
id: loop
stages:
  - id: plan
    role: planner
    produces:
      artifact: plan.md
  - id: review-plan
    role: reviewer
    depends_on:
      - plan
    decision:
      values:
        - approved
        - revise
      routes:
        approved: complete
        revise: plan
"""


class RevisionCeilingTests(unittest.TestCase):
    def test_revise_stops_before_another_plan_and_later_commands_stop_too(self) -> None:
        workspace = write_fake_workflow_workspace({"loop": _REVISION_LOOP})
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned",
                "status: complete\ndecision: revise\n\nagain",
                "status: complete\n\nshould not run",
            ],
            artifacts=["# Plan\n", None, "# Plan\n"],
        )
        code, objects = _run_execute(
            workspace,
            ["start", "loop", "--task", "Write a plan", "--max-dispatches", "10"],
            adapter,
        )
        self.assertEqual(code, 4)
        self.assertEqual(objects[-1]["stop_reason"], "revisions_exhausted")
        self.assertEqual(objects[-1]["session_status"], "active")
        self.assertEqual(
            [item.get("outcome_decision") for item in objects[:-1]],
            ["", "revise"],
        )
        self.assertEqual(len(adapter.calls), 2)
        session_id = objects[-1]["session_id"]
        idle = ScriptedAdapter(
            ["status: complete\n\nnope"], artifacts=["# Plan\n"]
        )
        continue_code, continue_objects = _run_execute(
            workspace,
            ["continue", session_id, "--max-dispatches", "10"],
            idle,
        )
        self.assertEqual(continue_code, 4)
        self.assertEqual(continue_objects[-1]["stop_reason"], "revisions_exhausted")
        self.assertEqual(idle.calls, [])
        stage = ScriptedAdapter(
            ["status: complete\n\nnope"], artifacts=["# Plan\n"]
        )
        stage_code, stage_objects = _run_execute(
            workspace, ["stage", session_id], stage
        )
        self.assertEqual(stage_code, 4)
        self.assertEqual(stage_objects[-1]["stop_reason"], "revisions_exhausted")
        self.assertEqual(stage_objects[-1]["session_status"], "active")
        self.assertEqual(stage.calls, [])

    def test_dispatch_ceiling_stops_before_the_next_stage(self) -> None:
        workspace = write_fake_workflow_workspace({"loop": _OPEN_LOOP})
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned",
                "status: complete\ndecision: revise\n\nagain",
                "status: complete\n\nshould not run",
            ],
            artifacts=["# Plan\n", None, "# Plan\n"],
        )
        code, objects = _run_execute(
            workspace,
            ["start", "loop", "--task", "Write a plan", "--max-dispatches", "2"],
            adapter,
        )
        self.assertEqual(code, 5)
        self.assertEqual(objects[-1]["stop_reason"], "dispatch_limit")
        self.assertEqual(objects[-1]["session_status"], "active")
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(objects[-2]["outcome_decision"], "revise")

    def test_default_dispatch_ceiling_matches_the_constant(self) -> None:
        from agentflow_kernel.execute import DEFAULT_MAX_DISPATCHES

        workspace = write_fake_workflow_workspace({"loop": _OPEN_LOOP})
        responses = []
        artifacts = []
        for index in range(DEFAULT_MAX_DISPATCHES + 1):
            if index % 2 == 0:
                responses.append("status: complete\n\nplanned")
                artifacts.append("# Plan\n")
            else:
                responses.append("status: complete\ndecision: revise\n\nagain")
                artifacts.append(None)
        adapter = ScriptedAdapter(responses, artifacts)
        code, objects = _run_execute(
            workspace,
            ["start", "loop", "--task", "Write a plan"],
            adapter,
        )
        self.assertEqual(code, 5)
        self.assertEqual(objects[-1]["stop_reason"], "dispatch_limit")
        self.assertEqual(len(adapter.calls), DEFAULT_MAX_DISPATCHES)

    def test_unlimited_dispatches_runs_past_the_default_ceiling(self) -> None:
        workspace = write_fake_workflow_workspace({"loop": _OPEN_LOOP})
        adapter = ScriptedAdapter(
            [
                "status: complete\n\nplanned",
                "status: complete\ndecision: approved\n\nok",
            ],
            artifacts=["# Plan\n", None],
        )
        code, objects = _run_execute(
            workspace,
            ["start", "loop", "--task", "Write a plan", "--unlimited-dispatches"],
            adapter,
        )
        self.assertEqual(code, 0)
        self.assertEqual(objects[-1]["stop_reason"], "completed")
        self.assertEqual(len(adapter.calls), 2)

    def test_rejects_a_non_positive_dispatch_ceiling(self) -> None:
        workspace = write_fake_workflow_workspace({"loop": _OPEN_LOOP})
        with self.assertRaisesRegex(ConfigurationError, "--max-dispatches"):
            execute_workflow(
                workspace,
                action="start",
                workflow_id="loop",
                task="Write a plan",
                max_dispatches=0,
            )

    def test_unlimited_dispatches_rejects_an_explicit_ceiling(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "start",
                    "loop",
                    "--task",
                    "Write a plan",
                    "--max-dispatches",
                    "4",
                    "--unlimited-dispatches",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("--unlimited-dispatches", stderr.getvalue())


class ArgvShapeTests(unittest.TestCase):
    def test_continue_rejects_flags_that_start_a_session(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["continue", "run-1", "--task", "again"])
        self.assertEqual(code, 2)
        self.assertIn("--task", stderr.getvalue())

    def test_agent_override_requires_both_provider_and_model(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "agent",
                    "start",
                    "--role",
                    "developer",
                    "--provider",
                    "grok",
                    "--prompt",
                    "x",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("--provider", stderr.getvalue())
        self.assertIn("--model", stderr.getvalue())


class CommandHelpTests(unittest.TestCase):
    def test_help_prints_command_usage_without_a_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            help_out = io.StringIO()
            with patch("sys.stdout", help_out):
                self.assertEqual(main(["start", "--help"], cwd=cwd), 0)
        text = help_out.getvalue()
        self.assertIn("Usage: agentflow start", text)
        self.assertIn("Exit 3", text)
        self.assertIn("Exit 4", text)
        self.assertIn("Exit 5", text)
        self.assertIn("--attempts", text)
        self.assertIn("--max-dispatches", text)
        self.assertIn("--unlimited-dispatches", text)

    def test_help_subcommand_prints_flags_declared_on_that_subcommand(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main(["continue", "--help"]), 0)
        text = stdout.getvalue()
        self.assertIn("Usage: agentflow continue", text)
        self.assertIn("--attempts", text)
        self.assertIn("--max-dispatches", text)

    def test_stage_help_has_no_dispatch_ceiling(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main(["stage", "--help"]), 0)
        text = stdout.getvalue()
        self.assertIn("Exit 4", text)
        self.assertNotIn("--max-dispatches", text)

    def test_help_rejects_an_unknown_subcommand(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["missing"])
        self.assertEqual(code, 2)
        self.assertIn("missing", stderr.getvalue())

    def test_no_command_prints_help(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main([]), 2)
        text = stdout.getvalue()
        self.assertIn("Usage: agentflow", text)
        self.assertIn("start", text)
        self.assertIn("stage", text)
        self.assertNotIn("execute", text)


if __name__ == "__main__":
    unittest.main()
