from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import json
import unittest

from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.outputs import should_publish_outputs
from agentflow_kernel.stage_outcome import StageOutcome
from agentflow_kernel.workflow import (
    StageSpec,
    WorkflowCompletion,
    WorkflowDocument,
    WorkflowOutput,
)
from tests.support import ScriptedAdapter, write_plan_review_session


def _plan_review() -> WorkflowDocument:
    return WorkflowDocument(
        id="plan-review",
        stages={
            "plan": StageSpec(id="plan", role="planner", artifact="plan.md"),
            "review-plan": StageSpec(
                id="review-plan",
                role="reviewer",
                decision_values=("approved", "revise"),
            ),
        },
        completion=WorkflowCompletion(
            stage="review-plan",
            decision="approved",
            outputs=(WorkflowOutput("plan", "plan", "plan.md"),),
        ),
    )


class OutputPublicationTests(unittest.TestCase):
    def test_publish_gate_requires_the_completion_decision(self) -> None:
        workflow = _plan_review()
        approved = StageOutcome(status="complete", decision="approved")
        self.assertTrue(
            should_publish_outputs(
                workflow_id="plan-review",
                execution_status="completed",
                outcome=approved,
                workflow=workflow,
                stage_id="review-plan",
            )
        )
        self.assertFalse(
            should_publish_outputs(
                workflow_id="plan-review",
                execution_status="completed",
                outcome=StageOutcome(status="complete", decision="revise"),
                workflow=workflow,
                stage_id="review-plan",
            )
        )
        self.assertFalse(
            should_publish_outputs(
                workflow_id="agent",
                execution_status="completed",
                outcome=approved,
                workflow=workflow,
                stage_id="review-plan",
            )
        )

    def test_approve_publishes_the_reviewed_plan_artifact(self) -> None:
        workspace, session_id = write_plan_review_session()
        runner = AgentToolRunner(
            adapters={
                "fake": ScriptedAdapter(
                    [
                        "status: complete\n\nplanned",
                        "status: complete\ndecision: approved\n\nlooks good",
                    ],
                    artifacts=["# Plan v1\n", None],
                )
            },
            timeout_sec=10,
        )
        runner.run("fake", "m1", "plan", str(workspace), session_id=session_id, stage_id="plan")
        review = runner.run(
            "fake", "m1", "review", str(workspace), session_id=session_id, stage_id="review-plan"
        )
        session_directory = workspace / ".agentflow" / "sessions" / session_id
        status = json.loads((session_directory / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (session_directory / "outputs" / "plan").read_text(encoding="utf-8"),
            "# Plan v1\n",
        )
        self.assertEqual(status["status"], "completed")
        self.assertEqual(review["session_status"], "completed")

    def test_missing_or_empty_artifact_fails_the_producing_stage(self) -> None:
        for body in (None, "   \n"):
            with self.subTest(body=body):
                workspace, session_id = write_plan_review_session()
                artifacts = None if body is None else [body]
                result = AgentToolRunner(
                    adapters={
                        "fake": ScriptedAdapter(
                            ["status: complete\n\nplanned"], artifacts=artifacts
                        )
                    },
                    timeout_sec=10,
                ).run(
                    "fake", "m1", "plan", str(workspace), session_id=session_id, stage_id="plan"
                )
                session_directory = workspace / ".agentflow" / "sessions" / session_id
                execution = json.loads(
                    (
                        session_directory
                        / "executions"
                        / result["execution_id"]
                        / "execution.json"
                    ).read_text(encoding="utf-8")
                )
                status = json.loads(
                    (session_directory / "status.json").read_text(encoding="utf-8")
                )
                self.assertEqual(execution["status"], "failed")
                self.assertIn("artifact missing", execution["error"])
                self.assertEqual(status["status"], "active")
                self.assertFalse((session_directory / "outputs").exists())

    def test_prose_approval_without_a_decision_header_fails(self) -> None:
        workspace, session_id = write_plan_review_session()
        runner = AgentToolRunner(
            adapters={
                "fake": ScriptedAdapter(
                    [
                        "status: complete\n\nplanned",
                        "status: complete\n\nApproved.",
                    ],
                    artifacts=["# Plan v1\n", None],
                )
            },
            timeout_sec=10,
        )
        runner.run("fake", "m1", "plan", str(workspace), session_id=session_id, stage_id="plan")
        review = runner.run(
            "fake", "m1", "review", str(workspace), session_id=session_id, stage_id="review-plan"
        )
        session_directory = workspace / ".agentflow" / "sessions" / session_id
        execution = json.loads(
            (
                session_directory
                / "executions"
                / review["execution_id"]
                / "execution.json"
            ).read_text(encoding="utf-8")
        )
        status = json.loads((session_directory / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(execution["status"], "failed")
        self.assertIn("decision:", execution["error"])
        self.assertEqual(status["status"], "active")
        self.assertFalse((session_directory / "outputs").exists())


if __name__ == "__main__":
    unittest.main()
