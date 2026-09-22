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
    CODEX_CONFIG,
    CODEX_THREAD,
    DRAFT_NOTE,
    GROK_CONFIG,
    GROK_SESSION,
    PLAN_REVIEW,
    SOLO,
    execution_statuses,
    run_main,
    write_workspace,
)


@unittest.skipUnless(os.name == "posix", "provider processes require POSIX")
class ExecuteTests(unittest.TestCase):
    def test_plan_review_publishes_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"plan-review": PLAN_REVIEW})
            code, objects, stderr = run_main(
                workspace,
                ["execute", "--workflow-id", "plan-review", "--task", "Write a plan"],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(
                [item.get("provider_session_id") for item in objects[:-1]],
                [GROK_SESSION, GROK_SESSION],
            )
            self.assertEqual(objects[-1]["stop_reason"], "completed")
            self.assertEqual(objects[-1]["run_status"], "completed")
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

    def test_stage_runs_through_the_codex_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, CODEX_CONFIG, {"draft-note": DRAFT_NOTE})
            code, objects, stderr = run_main(
                workspace,
                [
                    "execute",
                    "--workflow-id",
                    "draft-note",
                    "--task",
                    "Draft a note",
                    "--stage-id",
                    "draft",
                ],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(objects[0]["provider_session_id"], CODEX_THREAD)
            self.assertEqual(objects[0]["outcome_status"], "complete")
            self.assertEqual(objects[-1]["stop_reason"], "stage")
            artifact = Path(objects[0]["artifact_directory"]) / "note.md"
            self.assertTrue(artifact.is_file())
            self.assertTrue(artifact.read_text(encoding="utf-8").strip())

    def test_invalid_stdout_fails_the_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {"solo": SOLO})
            code, objects, stderr = run_main(
                workspace,
                [
                    "execute",
                    "--workflow-id",
                    "solo",
                    "--task",
                    "Write",
                    "--max-attempts",
                    "1",
                ],
                outcome="invalid",
            )
            self.assertEqual(code, 3, stderr)
            self.assertEqual(objects[-1]["stop_reason"], "retry_exhausted")
            self.assertEqual(execution_statuses(workspace), ["failed"])


if __name__ == "__main__":
    unittest.main()
