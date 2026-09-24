from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import tempfile
import unittest

from agentflow_kernel.outputs import ExecutionSnapshot
from agentflow_kernel.prompt import build_stage_prompt
from agentflow_kernel.workflow import StageSpec, WorkflowDocument, load_workflow_document


def _workflow(workflow_id: str):
    return load_workflow_document(
        PACKAGE_ROOT / "tests" / "fixtures" / "workflows" / f"{workflow_id}.yaml"
    )


def _snapshot(
    stage_id: str,
    order: int,
    directory: Path,
    *,
    status: str = "completed",
    outcome_status: str | None = "complete",
    decision: str | None = None,
) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        execution_id=f"{order:04d}-{stage_id}",
        stage_id=stage_id,
        execution_order=order,
        status=status,
        outcome_status=outcome_status,
        artifact_directory=directory,
        outcome_decision=decision,
    )


class BuildStagePromptTests(unittest.TestCase):
    def test_populated_prompt_matches_the_template_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            upstream = workspace / "upstream"
            upstream.mkdir()
            workflow = WorkflowDocument(
                id="sample",
                stages={
                    "plan": StageSpec(
                        id="plan", role="planner", artifact="plan.md"
                    ),
                    "implement": StageSpec(
                        id="implement",
                        role="developer",
                        depends_on=("plan",),
                        instructions=(
                            "Implement production code only.",
                            "Do not add or modify test code.",
                        ),
                        artifact="implementation-result.md",
                    ),
                },
                constraints=(
                    "Do not write implementation code.",
                    "Do not infer decisions from review prose.",
                ),
            )
            prompt = build_stage_prompt(
                workflow=workflow,
                stage_id="implement",
                task="Do work",
                snapshots=[_snapshot("plan", 1, upstream)],
                workspace=workspace,
                prior_session_id="prior-1",
                prior_outputs={"plan": {"path": "outputs/plan"}},
            )
            plan_path = (upstream / "plan.md").resolve()
            prior_path = (
                workspace / ".agentflow" / "sessions" / "prior-1" / "outputs" / "plan"
            ).resolve()
        self.assertEqual(
            prompt,
            "\n\n".join(
                [
                    "Task: Do work",
                    "Stage: implement (role: developer)",
                    "Instructions:\n"
                    "- Implement production code only.\n"
                    "- Do not add or modify test code.",
                    "Constraints:\n"
                    "- Do not write implementation code.\n"
                    "- Do not infer decisions from review prose.",
                    f"Upstream artifacts on this session:\n- plan: {plan_path}",
                    "Required prior workflow result:\n"
                    "- session: prior-1\n"
                    f"- outputs: {prior_path}",
                    "Write the file artifact as `implementation-result.md` "
                    "in the execution artifacts directory.",
                ]
            ),
        )

    def test_plan_includes_artifact_sentence_and_constraints(self) -> None:
        prompt = build_stage_prompt(
            workflow=_workflow("plan"),
            stage_id="plan",
            task="Write a plan",
            snapshots=[],
            workspace=Path("/tmp"),
        )
        self.assertIn("Write the file artifact as `plan.md`", prompt)
        self.assertIn("Do not write implementation code.", prompt)
        self.assertNotIn("Upstream artifacts", prompt)

    def test_review_omits_artifact_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan-v1"
            plan_dir.mkdir()
            prompt = build_stage_prompt(
                workflow=_workflow("plan"),
                stage_id="review-plan",
                task="Write a plan",
                snapshots=[_snapshot("plan", 1, plan_dir)],
                workspace=Path(tmp),
            )
        self.assertNotIn("Write the file artifact", prompt)
        self.assertIn("Upstream artifacts on this session:", prompt)
        self.assertIn(str((plan_dir / "plan.md").resolve()), prompt)

    def test_review_uses_the_latest_plan_attempt_after_revise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "plan-v1"
            second = Path(tmp) / "plan-v2"
            first.mkdir()
            second.mkdir()
            prompt = build_stage_prompt(
                workflow=_workflow("plan"),
                stage_id="review-plan",
                task="Write a plan",
                snapshots=[
                    _snapshot("plan", 1, first),
                    _snapshot("review-plan", 2, Path(tmp), decision="revise"),
                    _snapshot("plan", 3, second),
                ],
                workspace=Path(tmp),
            )
        self.assertIn(str((second / "plan.md").resolve()), prompt)
        self.assertNotIn(str((first / "plan.md").resolve()), prompt)

    def test_write_tests_uses_latest_successful_implement_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            failed = Path(tmp) / "implement-failed"
            ok = Path(tmp) / "implement-ok"
            failed.mkdir()
            ok.mkdir()
            prompt = build_stage_prompt(
                workflow=_workflow("plan-implement"),
                stage_id="write-tests",
                task="Do work",
                snapshots=[
                    _snapshot(
                        "implement",
                        1,
                        failed,
                        status="failed",
                        outcome_status=None,
                    ),
                    _snapshot("implement", 2, ok),
                    _snapshot("review-work", 3, Path(tmp), decision="approved"),
                ],
                workspace=Path(tmp),
            )
        self.assertIn(str((ok / "implementation-result.md").resolve()), prompt)
        self.assertNotIn(str((failed / "implementation-result.md").resolve()), prompt)
        self.assertIn("Write the file artifact as `test-result.md`", prompt)
        self.assertNotIn("review-work:", prompt)

    def test_includes_prior_session_published_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prompt = build_stage_prompt(
                workflow=_workflow("plan-implement"),
                stage_id="implement",
                task="Do work",
                snapshots=[],
                workspace=workspace,
                prior_session_id="prior-1",
                prior_outputs={"plan": {"path": "outputs/plan"}},
            )
        self.assertIn("- session: prior-1", prompt)
        expected = (workspace / ".agentflow" / "sessions" / "prior-1" / "outputs" / "plan")
        self.assertIn(str(expected.resolve()), prompt)

    def test_rejects_an_undeclared_stage(self) -> None:
        with self.assertRaisesRegex(ValueError, "Stage is not declared: missing"):
            build_stage_prompt(
                workflow=_workflow("plan"),
                stage_id="missing",
                task="Write a plan",
                snapshots=[],
                workspace=Path("/tmp"),
            )


if __name__ == "__main__":
    unittest.main()
