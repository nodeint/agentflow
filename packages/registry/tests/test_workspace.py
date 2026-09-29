from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.workspace import WorkspaceLocationError, locate_workspace
from tests.support import make_workspace


class WorkspaceTests(unittest.TestCase):
    def test_parent_walk_finds_the_root_from_an_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            nested = workspace / "src" / "pkg"
            nested.mkdir(parents=True)
            found = locate_workspace(str(nested))
            self.assertEqual(found, workspace.resolve())

    def test_directory_without_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaises(WorkspaceLocationError) as caught:
                locate_workspace(str(empty))
            self.assertEqual(caught.exception.reason, "not_workspace")

    def test_file_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "notes.txt"
            target.write_text("x", encoding="utf-8")
            with self.assertRaises(WorkspaceLocationError) as caught:
                locate_workspace(str(target))
            self.assertEqual(caught.exception.reason, "file")

    def test_relative_path_is_rejected_before_the_walk_and_before_cwd_join(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = make_workspace(Path(tmp))
            previous = Path.cwd()
            os.chdir(workspace)
            try:
                with self.assertRaises(WorkspaceLocationError) as caught:
                    locate_workspace("src")
            finally:
                os.chdir(previous)
            self.assertEqual(caught.exception.reason, "relative")


if __name__ == "__main__":
    unittest.main()
