from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import io
import json
import tempfile
import unittest
from unittest.mock import patch

from agentflow_cli.run import main
from tests.support import repo_workflow_text


def _which(command: str) -> str | None:
    if command in {"codex", "grok"}:
        return f"/bin/{command}"
    return None


class DoctorCommandTests(unittest.TestCase):
    def test_doctor_passes_from_a_project_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_project(workspace)
            child = workspace / "src"
            child.mkdir()
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "agentflow_cli.commands.doctor.shutil.which", _which
            ):
                code = main(["doctor"], cwd=child)
        text = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("ok", text)
        self.assertIn("developer", text)
        self.assertIn("plan-review", text)
        self.assertIn("/bin/grok", text)

    def test_doctor_json_fails_when_a_provider_command_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "agentflow_cli.commands.doctor.shutil.which",
                lambda command: "/bin/codex" if command == "codex" else None,
            ):
                code = main(["doctor", "--json"], cwd=workspace)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        providers = next(check for check in payload["checks"] if check["name"] == "providers")
        self.assertEqual(providers["problems"], ["grok is not on PATH"])
        self.assertIn("codex  /bin/codex", providers["items"])

    def test_doctor_exits_2_when_the_workspace_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                code = main(["doctor"], cwd=Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn(".agentflow/config.yaml", stderr.getvalue())


def _write_project(workspace: Path) -> None:
    workflows = workspace / ".agentflow" / "workflows"
    workflows.mkdir(parents=True)
    config = (
        "models:\n"
        "  planner-model:\n"
        "    provider: codex\n"
        "    model: gpt-5\n"
        "  reviewer-model:\n"
        "    provider: grok\n"
        "    model: grok-4\n"
        "roles:\n"
        "  planner:\n"
        "    default_model: planner-model\n"
        "  reviewer:\n"
        "    default_model: reviewer-model\n"
        "  developer:\n"
        "    default_model: planner-model\n"
    )
    (workspace / ".agentflow" / "config.yaml").write_text(config, encoding="utf-8")
    (workflows / "plan-review.yaml").write_text(
        repo_workflow_text("plan-review"), encoding="utf-8"
    )


if __name__ == "__main__":
    unittest.main()
