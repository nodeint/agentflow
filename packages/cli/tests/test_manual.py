from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import io
import unittest
from unittest.mock import patch

from agentflow_kernel.config import ConfigurationError
from agentflow_cli.manual import render_manual
from agentflow_cli.run import main


class ManualTests(unittest.TestCase):
    def test_unknown_topic_names_the_known_topics(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            render_manual("missing")
        message = str(caught.exception)
        self.assertIn("'missing'", message)
        self.assertIn("config", message)
        self.assertNotIn("index", message)

    def test_manual_on_a_pipe_is_the_source_markdown(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main(["man", "execute"]), 0)
        self.assertTrue(stdout.getvalue().startswith("# execute\n"))


if __name__ == "__main__":
    unittest.main()
