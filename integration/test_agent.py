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

from integration.support import GROK_CONFIG, GROK_SESSION, run_main, write_workspace


@unittest.skipUnless(os.name == "posix", "provider processes require POSIX")
class AgentTests(unittest.TestCase):
    def test_agent_writes_a_run_record(self) -> None:
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
            self.assertEqual(result["provider_session_id"], GROK_SESSION)
            status_path = (
                workspace / ".agentflow" / "runs" / result["run_id"] / "status.json"
            )
            self.assertTrue(status_path.is_file())
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["workflow_id"], "agent")
            self.assertEqual(status["status"], "completed")


if __name__ == "__main__":
    unittest.main()
