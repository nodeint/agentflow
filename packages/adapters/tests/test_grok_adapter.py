from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import json
import unittest

from agentflow_adapters import provider_commands
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

    def test_reads_models_from_grok_models_output(self) -> None:
        catalog = GrokAdapter().parse_model_catalog(
            "You are logged in with grok.com.\n"
            "\n"
            "Default model: grok-4.7\n"
            "\n"
            "Available models:\n"
            "  * grok-4.7 (default)\n"
            "  - grok-4.6\n"
        )
        self.assertEqual(catalog.ids, ("grok-4.7", "grok-4.6"))
        self.assertIsNone(catalog.thinking_for("grok-4.7"))
        self.assertEqual(catalog.default_id, "grok-4.7")
        self.assertEqual(GrokAdapter().model_catalog_command(), ["grok", "models"])

    def test_reads_thinking_values_for_the_selected_model(self) -> None:
        adapter = GrokAdapter()
        text = (
            "--effort/--reasoning-effort: unknown effort level 'agentflow-probe'; "
            "use one of: high, medium, low\n"
        )
        self.assertEqual(
            adapter.parse_thinking_values(text, model="grok-4.5"),
            ("high", "medium", "low"),
        )
        self.assertEqual(
            adapter.thinking_command("grok-4.5"),
            [
                "grok",
                "-m",
                "grok-4.5",
                "--reasoning-effort",
                "agentflow-probe",
                "-p",
                "x",
                "--output-format",
                "plain",
            ],
        )
        with self.assertRaisesRegex(ValueError, "not logged in"):
            adapter.parse_thinking_values("not logged in", model="grok-4.5")

    def test_rejects_an_unknown_option(self) -> None:
        adapter = GrokAdapter()
        with self.assertRaisesRegex(ValueError, "temperature is not a grok option"):
            adapter.validate_options({"temperature": "0.2"})
        adapter.validate_options({"thinking": "ultra"})
        with self.assertRaisesRegex(ValueError, "max_turns must be an integer"):
            adapter.validate_options({"max_turns": "0"})

    def test_maps_options_onto_grok_flags(self) -> None:
        spec = GrokAdapter().build_command(
            model="grok-4",
            workspace="/work",
            prompt="prompt",
            agent_id=None,
            new_agent_id=None,
            prompt_file=Path("/tmp/prompt.txt"),
            last_message_file=Path("/tmp/last.txt"),
            options={"thinking": "medium", "max_turns": "4"},
        )
        self.assertEqual(provider_commands()["grok"], GrokAdapter.command)
        self.assertEqual(
            spec.argv,
            [
                GrokAdapter.command,
                "--prompt-file",
                "/tmp/prompt.txt",
                "-m",
                "grok-4",
                "--cwd",
                "/work",
                "--output-format",
                "streaming-json",
                "--always-approve",
                "--verbatim",
                "--reasoning-effort",
                "medium",
                "--max-turns",
                "4",
            ],
        )


if __name__ == "__main__":
    unittest.main()
