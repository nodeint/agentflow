from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import tempfile
import unittest

from agentflow_kernel.workflow import load_workflow_document


class WorkflowDocumentTests(unittest.TestCase):
    def test_loads_repo_instructions_and_constraints(self) -> None:
        workflows = PACKAGE_ROOT / "tests" / "fixtures" / "workflows"
        review = load_workflow_document(workflows / "plan-review.yaml")
        implement = load_workflow_document(workflows / "plan-implement.yaml")
        self.assertEqual(
            review.constraints,
            (
                "Do not write implementation code.",
                "Do not infer decisions from review prose.",
            ),
        )
        self.assertEqual(review.stages["plan"].instructions, ())
        self.assertEqual(
            implement.stages["implement"].instructions,
            (
                "Implement production code only.",
                "Do not add or modify test code.",
            ),
        )
        self.assertEqual(implement.constraints, ())

    def test_rejects_non_list_instructions_and_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                "id: broken\nconstraints: nope\nstages:\n  - id: a\n    role: r\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "constraints must be a list"):
                load_workflow_document(path)
            path.write_text(
                "id: broken\nstages:\n  - id: a\n    role: r\n    instructions: nope\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "stages.a.instructions must be a list"
            ):
                load_workflow_document(path)


    def test_loads_routes_requires_and_resume_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                """\
id: plan-implement
requires:
  workflow: plan-review
  decision: approved
stages:
  - id: implement
    role: developer
    depends_on: []
  - id: review-work
    role: reviewer
    depends_on:
      - implement
    decision:
      values:
        - approved
        - revise
      routes:
        approved: write-tests
        revise: implement
  - id: write-tests
    role: developer
    session:
      resume_from: implement
    depends_on:
      - implement
      - review-work
""",
                encoding="utf-8",
            )
            document = load_workflow_document(path)
        self.assertEqual(document.requires.workflow, "plan-review")
        self.assertEqual(document.requires.decision, "approved")
        self.assertEqual(document.stages["review-work"].routes["approved"], "write-tests")
        self.assertEqual(document.stages["write-tests"].resume_from, "implement")

    def test_resume_from_must_be_a_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                """\
id: broken
stages:
  - id: implement
    role: developer
  - id: write-tests
    role: developer
    session:
      resume_from: implement
    depends_on: []
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must be a dependency"):
                load_workflow_document(path)

    def test_loads_max_revisions_on_a_route_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                """\
id: loop
stages:
  - id: plan
    role: planner
    max_revisions: 0
  - id: review
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
""",
                encoding="utf-8",
            )
            document = load_workflow_document(path)
        self.assertEqual(document.stages["plan"].max_revisions, 0)
        self.assertIsNone(document.stages["review"].max_revisions)

    def test_rejects_max_revisions_without_a_route_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                """\
id: loop
stages:
  - id: plan
    role: planner
  - id: review
    role: reviewer
    max_revisions: 3
    depends_on:
      - plan
    decision:
      values:
        - revise
      routes:
        revise: plan
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires a route back to review"):
                load_workflow_document(path)

    def test_rejects_a_negative_max_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                """\
id: loop
stages:
  - id: plan
    role: planner
    max_revisions: -1
  - id: review
    role: reviewer
    depends_on:
      - plan
    decision:
      values: [revise]
      routes:
        revise: plan
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-negative integer"):
                load_workflow_document(path)

    def test_rejects_empty_stage_model_and_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flow.yaml"
            path.write_text(
                'id: broken\nstages:\n  - id: a\n    role: r\n    model: ""\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, r"Missing or invalid stages\.a\.model\."):
                load_workflow_document(path)
            path.write_text(
                'id: broken\nstages:\n  - id: a\n    role: r\n    thinking: ""\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, r"Missing or invalid stages\.a\.thinking\."):
                load_workflow_document(path)


if __name__ == "__main__":
    unittest.main()
