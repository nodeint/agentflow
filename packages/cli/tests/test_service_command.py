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
from agentflow_registry.service import ServiceEndpoint, ServiceError, StopResult


class ServiceCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"AGENTFLOW_HOME": str(self.home)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_status_json_is_stopped_without_a_service_file(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(["service", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"state": "stopped"})

    def test_status_json_is_stopped_when_identity_does_not_match(self) -> None:
        registry = self.home / "registry"
        registry.mkdir()
        (registry / "service.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "instance_id": "instance-under-test",
                    "pid": 424242,
                    "pgid": 424242,
                    "start_key": "start-key",
                    "boot_id": "boot-1",
                    "hostname": "host.example",
                    "host": "127.0.0.1",
                    "port": 9,
                    "token": "secret-token-value",
                    "started_at": "2026-09-26T08:07:57Z",
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["service", "status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"state": "stopped"})
        self.assertNotIn("secret-token-value", stdout.getvalue())
        self.assertNotIn("secret-token-value", stderr.getvalue())

    def test_start_unverified_identity_exits_1_without_a_token(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fail(*_args, **_kwargs):
            raise ServiceError("Service identity is unverified.")

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch(
            "agentflow_cli.commands.service.start_detached", fail
        ):
            code = main(["service", "start"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Service identity is unverified.", stderr.getvalue())

    def test_stop_still_alive_exits_1_and_leaves_the_file(self) -> None:
        registry = self.home / "registry"
        registry.mkdir()
        path = registry / "service.json"
        path.write_text('{"keep": true}\n', encoding="utf-8")
        original = path.read_bytes()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), patch(
            "agentflow_cli.commands.service.stop",
            lambda *_args, **_kwargs: StopResult("error", "The process is still alive."),
        ):
            code = main(["service", "stop"])
        self.assertEqual(code, 1)
        self.assertIn("The process is still alive.", stderr.getvalue())
        self.assertEqual(path.read_bytes(), original)

    def test_detached_start_prints_the_endpoint_and_not_the_token(self) -> None:
        endpoint = ServiceEndpoint("127.0.0.1", 47321, "secret-token-value", "instance", 3)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch(
            "agentflow_cli.commands.service.start_detached", return_value=endpoint
        ) as started, patch("agentflow_cli.commands.service.serve_foreground") as foreground:
            code = main(["service", "start"])
        self.assertEqual(code, 0)
        started.assert_called_once()
        foreground.assert_not_called()
        self.assertEqual(stdout.getvalue().strip(), "127.0.0.1:47321")
        self.assertNotIn("secret-token-value", stdout.getvalue())

    def test_foreground_start_serves_in_process(self) -> None:
        endpoint = ServiceEndpoint("127.0.0.1", 47321, "secret-token-value", "instance", 3)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), patch(
            "agentflow_cli.commands.service.serve_foreground", return_value=endpoint
        ) as foreground, patch("agentflow_cli.commands.service.start_detached") as started:
            code = main(["service", "start", "--foreground"])
        self.assertEqual(code, 0)
        foreground.assert_called_once()
        started.assert_not_called()
        self.assertNotIn("secret-token-value", stdout.getvalue())

    def test_stop_absent_exits_0(self) -> None:
        with patch(
            "agentflow_cli.commands.service.stop",
            lambda *_args, **_kwargs: StopResult("absent"),
        ):
            code = main(["service", "stop"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
