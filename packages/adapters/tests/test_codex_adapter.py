from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import unittest

from agentflow_adapters.codex_adapter import CodexAdapter


class CodexAdapterTests(unittest.TestCase):
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
