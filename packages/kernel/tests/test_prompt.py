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
            review = Path(tmp) / "review-work"
            failed.mkdir()
            ok.mkdir()
            review.mkdir()
            (review / "response.md").write_text(
                "status: complete\ndecision: approved\n", encoding="utf-8"
            )
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
                    _snapshot("review-work", 3, review / "artifacts", decision="approved"),
                ],
                workspace=Path(tmp),
            )
        self.assertIn(str((ok / "implementation-result.md").resolve()), prompt)
        self.assertNotIn(str((failed / "implementation-result.md").resolve()), prompt)
        self.assertIn("Write the file artifact as `test-result.md`", prompt)
        self.assertIn(str((review / "response.md").resolve()), prompt)

    def test_revision_references_latest_review_and_previous_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            plan = workspace / "plan-1" / "artifacts"
            review = workspace / "review-1"
            plan.mkdir(parents=True)
            review.mkdir()
            (plan / "plan.md").write_text("initial plan", encoding="utf-8")
            (review / "response.md").write_text(
                "status: complete\ndecision: revise\n\nFix the plan.",
                encoding="utf-8",
            )
            prompt = build_stage_prompt(
                workflow=_workflow("plan"),
                stage_id="plan",
                task="Write a plan",
                snapshots=[
                    _snapshot("plan", 1, plan),
                    _snapshot("review-plan", 2, review / "artifacts", decision="revise"),
                ],
                workspace=workspace,
            )
        self.assertIn(str((review / "response.md").resolve()), prompt)
        self.assertIn(str((plan / "plan.md").resolve()), prompt)
        self.assertIn("Read this response and use its decision and feedback", prompt)

    def test_revision_retry_keeps_the_review_that_requested_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            old_review = workspace / "review-1"
            new_review = workspace / "review-2"
            old_review.mkdir()
            new_review.mkdir()
            (old_review / "response.md").write_text("old", encoding="utf-8")
            (new_review / "response.md").write_text("new", encoding="utf-8")
            prompt = build_stage_prompt(
                workflow=_workflow("plan"),
                stage_id="plan",
                task="Write a plan",
                snapshots=[
                    _snapshot("plan", 1, workspace / "plan-1"),
                    _snapshot("review-plan", 2, old_review / "artifacts", decision="revise"),
                    _snapshot("plan", 3, workspace / "plan-2"),
                    _snapshot("review-plan", 4, new_review / "artifacts", decision="revise"),
                    _snapshot(
                        "plan", 5, workspace / "failed-plan", status="failed", outcome_status=None
                    ),
                ],
                workspace=workspace,
            )
        self.assertIn(str((new_review / "response.md").resolve()), prompt)
        self.assertNotIn(str((old_review / "response.md").resolve()), prompt)

    def test_routed_stage_requires_its_decision_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(ValueError, "Routed stage response is missing"):
                build_stage_prompt(
                    workflow=_workflow("plan"),
                    stage_id="plan",
                    task="Write a plan",
                    snapshots=[
                        _snapshot("plan", 1, workspace / "plan-1"),
                        _snapshot(
                            "review-plan", 2, workspace / "review" / "artifacts", decision="revise"
                        ),
                    ],
                    workspace=workspace,
                )

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
