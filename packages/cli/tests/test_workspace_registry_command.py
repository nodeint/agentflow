from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_cli.run import main
from agentflow_registry.service import ServiceEndpoint


class WorkspaceRegistryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self._env = patch.dict(os.environ, {"AGENTFLOW_HOME": str(self.home)})
        self._env.start()
        self.endpoint = ServiceEndpoint("127.0.0.1", 9, "token", "instance", 7)
        self.calls: list[tuple[object, ...]] = []

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_register_from_a_subdirectory_sends_the_absolute_root(self) -> None:
        workspace = _project(self.root / "demo")
        child = workspace / "src"
        child.mkdir()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch(
            "agentflow_cli.commands.workspace_registry.start_detached",
            return_value=self.endpoint,
        ) as started, patch(
            "agentflow_cli.commands.workspace_registry.call_service",
            side_effect=self._capture_call,
        ):
            code = main(["workspace", "register"], cwd=child)
        self.assertEqual(code, 0)
        started.assert_called_once()
        body = self.calls[0][5]
        self.assertEqual(self.calls[0][3], "POST")
        self.assertEqual(body["path"], str(workspace.resolve()))
        self.assertTrue(Path(body["path"]).is_absolute())
        self.assertEqual(body["tool"]["name"], "agentflow")
        self.assertIn(str(workspace.resolve()), stderr.getvalue())
        self.assertIn(str(workspace.resolve()), stdout.getvalue())

    def test_register_relative_path_resolves_against_the_invocation_cwd(self) -> None:
        workspace = _project(self.root / "demo")
        nested = workspace / "src" / "pkg"
        nested.mkdir(parents=True)
        with patch(
            "agentflow_cli.commands.workspace_registry.start_detached",
            return_value=self.endpoint,
        ), patch(
            "agentflow_cli.commands.workspace_registry.call_service",
            side_effect=self._capture_call,
        ):
            code = main(["workspace", "register", "--path", "src/pkg"], cwd=workspace)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0][5]["path"], str(workspace.resolve()))

    def test_register_without_a_workspace_exits_2_and_does_not_start(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), patch(
            "agentflow_cli.commands.workspace_registry.start_detached"
        ) as started:
            code = main(["workspace", "register"], cwd=self.root)
        self.assertEqual(code, 2)
        started.assert_not_called()
        self.assertIn("config.yaml", stderr.getvalue())

    def test_list_json_exits_0_and_remove_of_unknown_id_exits_2(self) -> None:
        registry = self.home / "registry"
        registry.mkdir()
        (registry / "service.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": "instance",
                    "pid": 7,
                    "pgid": 7,
                    "start_key": "start",
                    "boot_id": "boot",
                    "hostname": "host",
                    "host": "127.0.0.1",
                    "port": 9,
                    "token": "token",
                    "started_at": "2026-09-26T08:07:57Z",
                }
            ),
            encoding="utf-8",
        )
        rows = [{"id": "ws_abc", "name": "demo", "path": "/tmp/demo", "available": True}]

        def call_service(*args, **_kwargs):
            self.calls.append(args)
            if args[3] == "GET":
                return 200, {"workspaces": rows}
            return 404, {"error": "workspace_not_found", "message": "unknown id"}

        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch(
            "agentflow_cli.commands.workspace_registry.call_service", side_effect=call_service
        ):
            listed = main(["workspace", "list", "--json"])
        self.assertEqual(listed, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"workspaces": rows})
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), patch(
            "agentflow_cli.commands.workspace_registry.call_service", side_effect=call_service
        ):
            removed = main(["workspace", "remove", "ws_missing"])
        self.assertEqual(removed, 2)
        self.assertIn("unknown id", stderr.getvalue())

    def _capture_call(self, *args, **_kwargs):
        self.calls.append(args)
        body = args[5]
        return 201, {"id": "ws_demo", "path": body["path"], "available": True}


def _project(path: Path) -> Path:
    config = path / ".agentflow"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
