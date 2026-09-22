from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import json
import tempfile
import unittest

from agentflow_kernel.config import ConfigurationError
from agentflow_kernel.dispatch import resolve_resume_execution
from agentflow_kernel.selection import (
    Error,
    NextStage,
    Stop,
    next_stage,
    stage_dispatch_error,
)
from agentflow_kernel.session_records import require_agent_session, resolve_prior_session_id
from agentflow_kernel.outputs import ExecutionSnapshot
from agentflow_kernel.workflow import (
    StageSpec,
    WorkflowDocument,
    WorkflowRequires,
    load_workflow_document,
)



def _workflow() -> WorkflowDocument:
    return WorkflowDocument(
        id="plan-implement",
        stages={
            "implement": StageSpec(id="implement", role="developer"),
            "review-work": StageSpec(
                id="review-work",
                role="reviewer",
                depends_on=("implement",),
                routes={"approved": "write-tests", "revise": "implement"},
            ),
            "write-tests": StageSpec(
                id="write-tests",
                role="developer",
                depends_on=("implement", "review-work"),
                resume_from="implement",
            ),
        },
        requires=WorkflowRequires(workflow="plan-review", decision="approved"),
    )


def _snapshot(
    stage_id: str,
    order: int,
    *,
    decision: str | None = None,
    status: str = "completed",
    outcome_status: str | None = "complete",
) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        execution_id=f"{order:04d}-{stage_id}",
        stage_id=stage_id,
        execution_order=order,
        status=status,
        outcome_status=outcome_status,
        artifact_directory=Path("/tmp"),
        outcome_decision=decision,
    )


def _repo_workflow(workflow_id: str) -> WorkflowDocument:
    return load_workflow_document(
        PACKAGE_ROOT / "tests" / "fixtures" / "workflows" / f"{workflow_id}.yaml"
    )


class DispatchGateTests(unittest.TestCase):
    def test_rejects_a_stage_before_its_dependency(self) -> None:
        error = stage_dispatch_error(_workflow(), "review-work", [])
        self.assertIsNotNone(error)
        self.assertIn("depends on implement", error)

    def test_rejects_a_route_target_after_revise(self) -> None:
        error = stage_dispatch_error(
            _workflow(),
            "write-tests",
            [
                _snapshot("implement", 1),
                _snapshot("review-work", 2, decision="revise"),
            ],
        )
        self.assertIsNotNone(error)
        self.assertIn("not routed", error)

    def test_allows_the_stage_selected_by_the_latest_route(self) -> None:
        self.assertIsNone(
            stage_dispatch_error(
                _workflow(),
                "write-tests",
                [
                    _snapshot("implement", 1),
                    _snapshot("review-work", 2, decision="approved"),
                ],
            )
        )


class PriorRunTests(unittest.TestCase):
    def test_requires_a_completed_matching_prior_session(self) -> None:
        workflow = _workflow()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(ConfigurationError, "pass --prior"):
                resolve_prior_session_id(workspace, workflow, None)
            with self.assertRaisesRegex(ConfigurationError, "Required session not found"):
                resolve_prior_session_id(workspace, workflow, "missing")
            session_directory = workspace / ".agentflow" / "sessions" / "prior"
            session_directory.mkdir(parents=True)
            (session_directory / "status.json").write_text(
                json.dumps(
                    {
                        "workflow_id": "plan-review",
                        "status": "completed",
                        "latest_decision": "revise",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "decision is 'revise'"):
                resolve_prior_session_id(workspace, workflow, "prior")
            (session_directory / "status.json").write_text(
                json.dumps(
                    {
                        "workflow_id": "plan-review",
                        "status": "completed",
                        "latest_decision": "approved",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_prior_session_id(workspace, workflow, "prior"), "prior"
            )


class ResumeSessionTests(unittest.TestCase):
    def test_resume_from_selects_the_source_stage_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            execution = (
                workspace
                / ".agentflow"
                / "sessions"
                / "run-1"
                / "executions"
                / "0001-implement--attempt-01"
            )
            execution.mkdir(parents=True)
            (execution / "execution.json").write_text(
                json.dumps(
                    {
                        "execution_id": "0001-implement--attempt-01",
                        "stage_id": "implement",
                        "execution_order": 1,
                        "status": "completed",
                        "outcome": {"status": "complete"},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_resume_execution(
                    workspace, _workflow(), "run-1", "write-tests"
                ),
                "0001-implement--attempt-01",
            )


class AgentRunGuardTests(unittest.TestCase):
    def test_rejects_a_named_workflow_session_as_an_agent_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session_directory = workspace / ".agentflow" / "sessions" / "wf"
            session_directory.mkdir(parents=True)
            (session_directory / "status.json").write_text(
                json.dumps({"workflow_id": "plan-review"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ConfigurationError, "was not created by agent"):
                require_agent_session(workspace, "wf")


class NextStageTests(unittest.TestCase):
    def test_plan_review_walk(self) -> None:
        workflow = _repo_workflow("plan-review")
        cases = (
            ("empty", [], "active", NextStage("plan")),
            ("after plan", [_snapshot("plan", 1)], "active", NextStage("review-plan")),
            (
                "revise",
                [
                    _snapshot("plan", 1),
                    _snapshot("review-plan", 2, decision="revise"),
                ],
                "active",
                NextStage("plan"),
            ),
            (
                "after plan v2",
                [
                    _snapshot("plan", 1),
                    _snapshot("review-plan", 2, decision="revise"),
                    _snapshot("plan", 3),
                ],
                "active",
                NextStage("review-plan"),
            ),
            (
                "approved while active",
                [
                    _snapshot("plan", 1),
                    _snapshot("review-plan", 2, decision="approved"),
                ],
                "active",
                Stop("completed"),
            ),
            (
                "completed session",
                [
                    _snapshot("plan", 1),
                    _snapshot("review-plan", 2, decision="approved"),
                ],
                "completed",
                Stop("completed"),
            ),
        )
        for name, snapshots, session_status, expected in cases:
            with self.subTest(name):
                self.assertEqual(
                    next_stage(workflow, snapshots, session_status), expected
                )

    def test_plan_implement_walk(self) -> None:
        workflow = _repo_workflow("plan-implement")
        after_implement = [_snapshot("implement", 1)]
        after_revise = [
            _snapshot("implement", 1),
            _snapshot("review-work", 2, decision="revise"),
        ]
        after_approved = [
            _snapshot("implement", 1),
            _snapshot("review-work", 2, decision="approved"),
        ]
        after_tests = after_approved + [_snapshot("write-tests", 3)]
        cases = (
            ("empty", [], "active", NextStage("implement")),
            ("after implement", after_implement, "active", NextStage("review-work")),
            ("revise", after_revise, "active", NextStage("implement")),
            (
                "after implement v2",
                after_revise + [_snapshot("implement", 3)],
                "active",
                NextStage("review-work"),
            ),
            ("approved", after_approved, "active", NextStage("write-tests")),
            ("after write-tests", after_tests, "active", NextStage("review-tests")),
            (
                "review-tests revise",
                after_tests + [_snapshot("review-tests", 4, decision="revise")],
                "active",
                NextStage("write-tests"),
            ),
            (
                "review-tests approved",
                after_tests + [_snapshot("review-tests", 4, decision="approved")],
                "completed",
                Stop("completed"),
            ),
        )
        for name, snapshots, session_status, expected in cases:
            with self.subTest(name):
                self.assertEqual(
                    next_stage(workflow, snapshots, session_status), expected
                )

    def test_failed_latest_retries_that_stage(self) -> None:
        workflow = _repo_workflow("plan-review")
        self.assertEqual(
            next_stage(
                workflow,
                [
                    _snapshot("plan", 1),
                    _snapshot(
                        "review-plan",
                        2,
                        status="failed",
                        outcome_status=None,
                    ),
                ],
                "active",
            ),
            NextStage("review-plan"),
        )

    def test_terminal_session_status_wins_over_failed_snapshot(self) -> None:
        workflow = _repo_workflow("plan-implement")
        failed = [
            _snapshot(
                "implement", 1, status="failed", outcome_status=None
            )
        ]
        self.assertEqual(next_stage(workflow, failed, "blocked"), Stop("blocked"))
        self.assertEqual(
            next_stage(workflow, failed, "cancelled"), Stop("cancelled")
        )

    def test_running_execution_is_an_error(self) -> None:
        result = next_stage(
            _repo_workflow("plan-review"),
            [_snapshot("plan", 1, status="running", outcome_status=None)],
            "active",
        )
        self.assertEqual(result, Error("an execution is still running"))

    def test_two_roots_are_ambiguous(self) -> None:
        workflow = WorkflowDocument(
            id="two",
            stages={
                "a": StageSpec(id="a", role="developer"),
                "b": StageSpec(id="b", role="developer"),
            },
        )
        result = next_stage(workflow, [], "active")
        self.assertIsInstance(result, Error)
        assert isinstance(result, Error)
        self.assertIn("ambiguous next stage", result.message)


if __name__ == "__main__":
    unittest.main()
