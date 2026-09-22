from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import json
import unittest

from agentflow_adapters.grok_adapter import GrokAdapter
from agentflow_kernel.stage_outcome import parse_stage_outcome


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


class GrokAdapterTests(unittest.TestCase):
    def test_keeps_text_after_the_last_tool_event(self) -> None:
        text, session_id = GrokAdapter().parse_response(
            _stream(
                {"type": "text", "data": "I'll load the docs.\n"},
                {
                    "type": "tool_call",
                    "toolCallId": "call_1",
                    "toolName": "read_file",
                    "rawInput": {"path": "README.md"},
                },
                {
                    "type": "tool_call_update",
                    "toolCallId": "call_1",
                    "status": "completed",
                },
                {"type": "text", "data": "status: complete\n\n# Plan\n"},
                {"type": "end", "sessionId": "sess-final"},
            ),
            Path("/tmp/unused"),
        )
        self.assertEqual(text, "status: complete\n\n# Plan")
        self.assertEqual(session_id, "sess-final")
        self.assertEqual(parse_stage_outcome(text, required=True).status, "complete")

    def test_keeps_a_status_header_before_a_later_thought_segment(self) -> None:
        text, _ = GrokAdapter().parse_response(
            _stream(
                {"type": "text", "data": "status: complete\n\n# Plan\n"},
                {
                    "type": "tool_call",
                    "toolCallId": "call_1",
                    "toolName": "read_file",
                    "rawInput": {"path": "README.md"},
                },
                {
                    "type": "tool_call_update",
                    "toolCallId": "call_1",
                    "status": "completed",
                },
                {"type": "text", "data": "checking the tree once more"},
                {"type": "end", "sessionId": "sess-thought"},
            ),
            Path("/tmp/unused"),
        )
        self.assertEqual(text, "status: complete\n\n# Plan")
        self.assertEqual(parse_stage_outcome(text, required=True).status, "complete")

    def test_trims_preamble_before_a_status_header(self) -> None:
        text, _ = GrokAdapter().parse_response(
            _stream(
                {
                    "type": "text",
                    "data": "I'll write the plan next.\nstatus: complete\n\n# Plan\n",
                },
                {"type": "end", "sessionId": "sess-trim"},
            ),
            Path("/tmp/unused"),
        )
        self.assertEqual(text, "status: complete\n\n# Plan")

    def test_trims_a_status_header_glued_to_preamble(self) -> None:
        text, _ = GrokAdapter().parse_response(
            _stream(
                {
                    "type": "text",
                    "data": "Next I'll write the plan.status: complete\n\n# Plan\n",
                },
                {"type": "end", "sessionId": "sess-glue"},
            ),
            Path("/tmp/unused"),
        )
        self.assertEqual(text, "status: complete\n\n# Plan")
        self.assertEqual(parse_stage_outcome(text, required=True).status, "complete")


if __name__ == "__main__":
    unittest.main()
