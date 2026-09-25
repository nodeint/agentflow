from __future__ import annotations

import json
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
    COMPLETE,
    GROK_CONFIG,
    GROK_SESSION,
    execution_records,
    has_args,
    install_script,
    provider_invocations,
    run_main,
    write_workspace,
)


@unittest.skipUnless(os.name == "posix", "provider processes require POSIX")
class AgentTests(unittest.TestCase):
    def test_agent_writes_a_session_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {})
            code, objects, stderr = run_main(
                workspace,
                [
                    "agent",
                    "start",
                    "--role",
                    "planner",
                    "--task",
                    "Think",
                    "--prompt",
                    "hello",
                    "--json",
                ],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(len(objects), 1)
            result = objects[0]
            self.assertEqual(result["outcome_status"], "complete")
            self.assertEqual(result["agent_id"], GROK_SESSION)
            status_path = (
                workspace / ".agentflow" / "sessions" / result["session_id"] / "status.json"
            )
            self.assertTrue(status_path.is_file())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["workflow_id"], "agent")
            self.assertEqual(status["status"], "completed")

    def test_continue_resumes_the_grok_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {})
            install_script(workspace, [{"text": COMPLETE}, {"text": COMPLETE}])
            code, objects, stderr = run_main(
                workspace,
                [
                    "agent",
                    "start",
                    "--role",
                    "planner",
                    "--task",
                    "Think",
                    "--prompt",
                    "hello",
                    "--json",
                ],
            )
            self.assertEqual(code, 0, stderr)
            session_id = objects[0]["session_id"]
            code, continued, stderr = run_main(
                workspace,
                [
                    "agent",
                    "continue",
                    session_id,
                    "--role",
                    "planner",
                    "--prompt",
                    "again",
                    "--json",
                ],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(continued[0]["session_id"], session_id)
            self.assertEqual(continued[0]["agent_id"], GROK_SESSION)
            self.assertEqual(continued[0]["outcome_status"], "complete")
            first, second = (item["argv"] for item in provider_invocations(workspace))
            self.assertTrue(has_args(first, "-m", "grok-test"))
            self.assertTrue(has_args(first, "--reasoning-effort", "low"))
            self.assertNotIn("--resume", first)
            self.assertTrue(has_args(second, "--resume", GROK_SESSION))
            records = execution_records(workspace, session_id)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]["resumes_execution_id"], records[0]["execution_id"])
            self.assertEqual(records[1]["status"], "completed")

    def test_continue_resumes_the_codex_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, CODEX_CONFIG, {})
            install_script(workspace, [{"text": COMPLETE}, {"text": COMPLETE}])
            code, objects, stderr = run_main(
                workspace,
                [
                    "agent",
                    "start",
                    "--role",
                    "writer",
                    "--prompt",
                    "hello",
                    "--json",
                ],
            )
            self.assertEqual(code, 0, stderr)
            session_id = objects[0]["session_id"]
            code, continued, stderr = run_main(
                workspace,
                [
                    "agent",
                    "continue",
                    session_id,
                    "--role",
                    "writer",
                    "--prompt",
                    "again",
                    "--json",
                ],
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(continued[0]["agent_id"], CODEX_THREAD)
            first, second = (item["argv"] for item in provider_invocations(workspace))
            self.assertIn("--approve-for-me", first)
            self.assertNotIn("resume", first)
            self.assertTrue(has_args(second, "resume", CODEX_THREAD, "-"))
            self.assertNotIn("--approve-for-me", second)

    def test_a_different_option_does_not_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            write_workspace(workspace, GROK_CONFIG, {})
            install_script(workspace, [{"text": COMPLETE}, {"text": COMPLETE}])
            code, objects, stderr = run_main(
                workspace,
                [
                    "agent",
                    "start",
                    "--role",
                    "planner",
                    "--prompt",
                    "hello",
                    "--json",
                ],
            )
            self.assertEqual(code, 0, stderr)
            code, _, stderr = run_main(
                workspace,
                [
                    "agent",
                    "continue",
                    objects[0]["session_id"],
                    "--role",
                    "planner",
                    "--option",
                    "reasoning-effort=high",
                    "--prompt",
                    "again",
                    "--json",
                ],
            )
            self.assertEqual(code, 2, stderr)
            self.assertIn("options", stderr)
            self.assertEqual(len(provider_invocations(workspace)), 1)


if __name__ == "__main__":
    unittest.main()
