from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.home import (
    agentflow_home_path,
    ensure_registry_home,
    registry_home_path,
)


class HomeTests(unittest.TestCase):
    def test_agentflow_home_overrides_the_default_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTFLOW_HOME": tmp}):
                self.assertEqual(agentflow_home_path(), Path(tmp))
                self.assertEqual(registry_home_path(), Path(tmp) / "registry")

    def test_default_home_is_dot_agentflow_under_the_user_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            environ = os.environ.copy()
            environ.pop("AGENTFLOW_HOME", None)
            with patch.dict(os.environ, environ, clear=True), patch(
                "agentflow_registry.home.Path.home", return_value=home
            ):
                self.assertEqual(agentflow_home_path(), home / ".agentflow")
                self.assertEqual(registry_home_path(), home / ".agentflow" / "registry")

    def test_ensure_creates_the_registry_directory_mode_0700(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = ensure_registry_home(Path(tmp) / "registry")
            self.assertTrue(directory.is_dir())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
