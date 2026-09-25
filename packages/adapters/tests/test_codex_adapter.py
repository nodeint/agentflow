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
    def test_reads_listed_models_and_their_effort_from_the_codex_catalog(self) -> None:
        catalog = CodexAdapter().parse_model_catalog(
            '{"models":['
            '{"slug":"gpt-5","visibility":"list","supported_reasoning_levels":'
            '[{"effort":"low"},{"effort":"high"}]},'
            '{"slug":"gpt-5-pro","visibility":"list","supported_reasoning_levels":'
            '[{"effort":"max"}]},'
            '{"slug":"internal","visibility":"hide","supported_reasoning_levels":'
            '[{"effort":"low"}]}'
            "]}"
        )
        self.assertEqual(catalog.ids, ("gpt-5", "gpt-5-pro"))
        self.assertEqual(catalog.values_for("gpt-5", "model_reasoning_effort"), ("low", "high"))
        self.assertEqual(catalog.values_for("gpt-5-pro", "model_reasoning_effort"), ("max",))
        self.assertIsNone(catalog.values_for("internal", "model_reasoning_effort"))
        self.assertIsNone(catalog.default_id)
        self.assertEqual(
            CodexAdapter().model_catalog_command(),
            ["codex", "debug", "models"],
        )
        self.assertEqual(
            CodexAdapter().parse_option_values(
                "model_reasoning_effort",
                '{"models":[{"slug":"gpt-5","visibility":"list",'
                '"supported_reasoning_levels":[{"effort":"low"},{"effort":"high"}]}]}',
                model="gpt-5",
            ),
            ("low", "high"),
        )

    def test_accepts_any_non_empty_effort_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "1bad is not a codex option"):
            CodexAdapter().validate_options({"1bad": "x"})
        with self.assertRaisesRegex(ValueError, "model_reasoning_effort is empty"):
            CodexAdapter().validate_options({"model_reasoning_effort": " "})
        CodexAdapter().validate_options({"model_reasoning_effort": "max", "temperature": "0.2"})

    def test_maps_options_onto_codex_config_overrides(self) -> None:
        spec = CodexAdapter().build_command(
            model="gpt-5",
            workspace="/work",
            prompt="prompt",
            agent_id=None,
            new_agent_id=None,
            prompt_file=Path("/tmp/prompt.txt"),
            last_message_file=Path("/tmp/last.txt"),
            options={"model_reasoning_effort": "medium", "temperature": "0.2"},
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
