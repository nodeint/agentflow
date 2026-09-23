from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import unittest

from agentflow_adapters import provider_commands
from agentflow_adapters.codex_adapter import CodexAdapter


class CodexAdapterTests(unittest.TestCase):
    def test_reads_listed_models_from_the_codex_catalog(self) -> None:
        catalog = CodexAdapter().parse_model_catalog(
            '{"models":['
            '{"slug":"gpt-5","visibility":"list"},'
            '{"slug":"internal","visibility":"hide"}'
            "]}"
        )
        self.assertEqual(catalog.ids, ("gpt-5",))
        self.assertIsNone(catalog.default_id)
        self.assertEqual(
            CodexAdapter().model_catalog_command(),
            ["codex", "debug", "models"],
        )

    def test_rejects_a_thinking_value_codex_does_not_accept(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "thinking must be one of: minimal, low, medium, high, xhigh",
        ):
            CodexAdapter().validate_options({"thinking": "max"})
        CodexAdapter().validate_options({"thinking": "medium", "temperature": "0.2"})

    def test_maps_options_onto_codex_config_overrides(self) -> None:
        spec = CodexAdapter().build_command(
            model="gpt-5",
            workspace="/work",
            prompt="prompt",
            agent_id=None,
            new_agent_id=None,
            prompt_file=Path("/tmp/prompt.txt"),
            last_message_file=Path("/tmp/last.txt"),
            options={"thinking": "medium", "temperature": "0.2"},
        )
        self.assertEqual(spec.argv[0], CodexAdapter.command)
        self.assertEqual(provider_commands()["codex"], CodexAdapter.command)
        self.assertIn("-c", spec.argv)
        overrides = [
            spec.argv[index + 1]
            for index, arg in enumerate(spec.argv)
            if arg == "-c"
        ]
        self.assertEqual(
            overrides,
            ['model_reasoning_effort="medium"', "temperature=0.2"],
        )
        self.assertEqual(spec.stdin, "prompt")


if __name__ == "__main__":
    unittest.main()
