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

from agentflow_cli.commands.config import run_config_model, run_config_role
from agentflow_cli.run import main
from agentflow_kernel.config import load_yaml_mapping


CONFIG = """\
models:
  shared:
    provider: grok
    model: grok-4.6
    options:
      thinking: medium
      max_turns: "4"
  plain:
    provider: grok
    model: grok-4.7
roles:
  developer:
    default_model: shared
  reviewer:
    default_model: shared
"""

WORKFLOW = """\
id: plan
stages:
  - id: implement
    role: developer
    depends_on: []
  - id: review
    role: reviewer
    model: plain
    depends_on: []
  - id: plain-review
    role: reviewer
    depends_on:
      - review
"""


def _run_command(argv: list[str]) -> str:
    if argv[:2] == ["grok", "models"]:
        return (
            "Default model: grok-4.7\n"
            "Available models:\n"
            "  * grok-4.7 (default)\n"
            "  - grok-4.6\n"
        )
    if argv[:2] == ["codex", "debug"]:
        return json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "high"}],
                    }
                ]
            }
        )
    if argv[:1] == ["grok"] and "--reasoning-effort" in argv:
        return "use one of: xhigh, high, medium, low"
    raise AssertionError(argv)


def _which(command: str) -> str | None:
    if command in {"grok", "codex"}:
        return f"/bin/{command}"
    return None


class _Answers:
    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.text_defaults: list[str] = []

    def select(self, message: str, choices, default: str | None = None) -> str:
        del message, choices, default
        return next(self._answers)

    def confirm(self, message: str) -> bool:
        del message
        return next(self._answers) == "y"

    def text(self, message: str, default: str = "") -> str:
        del message
        self.text_defaults.append(default)
        return next(self._answers)


def _project(root: Path) -> None:
    agentflow = root / ".agentflow"
    workflows = agentflow / "workflows"
    workflows.mkdir(parents=True)
    (agentflow / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (workflows / "plan.yaml").write_text(WORKFLOW, encoding="utf-8")


class ConfigCommandTests(unittest.TestCase):
    def test_config_without_a_command_prints_help(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = main(["config"], cwd=Path(tmp))
        text = stdout.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("show", text)
        self.assertIn("model", text)
        self.assertIn("role", text)
        self.assertNotIn("models:", text)

    def test_show_prints_the_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = main(["config", "show"], cwd=workspace)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), CONFIG)

    def test_lists_models_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = main(["config", "model", "--json"], cwd=workspace)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        shared = next(model for model in payload["models"] if model["key"] == "shared")
        self.assertEqual(shared["roles"], ["developer", "reviewer"])
        self.assertEqual(shared["options"]["max_turns"], "4")
        self.assertIn("plan", {stage["workflow"] for stage in shared["stages"]})

    def test_set_role_model_name_updates_only_that_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "agentflow_cli.commands.config.shutil.which", _which
            ):
                code = main(
                    ["config", "role", "reviewer", "--model-name", "plain", "--json"],
                    cwd=workspace,
                )
            payload = json.loads(stdout.getvalue())
            config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
            self.assertEqual(code, 0)
            self.assertTrue(payload["wrote"])
            self.assertEqual(payload["roles"], ["reviewer"])
            self.assertEqual(config["roles"]["developer"]["default_model"], "shared")
            self.assertEqual(config["roles"]["reviewer"]["default_model"], "plain")
            self.assertEqual(config["models"]["shared"]["model"], "grok-4.6")
            self.assertEqual(config["models"]["shared"]["options"]["max_turns"], "4")
            unchanged = {stage["stage"]: stage["ignores"] for stage in payload["stages_unchanged"]}
            self.assertEqual(unchanged["review"], ["role-model"])
            self.assertIn("plain-review", {stage["stage"] for stage in payload["stages_updated"]})
            self.assertTrue(payload["doctor"]["ok"])

    def test_role_provider_model_does_not_rewrite_a_shared_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = run_config_role(
                    workspace,
                    roles=("reviewer",),
                    provider="grok",
                    model="grok-4.6",
                    thinking="high",
                    as_json=True,
                    which=_which,
                    run_command=_run_command,
                )
            payload = json.loads(stdout.getvalue())
            config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
            self.assertEqual(code, 0)
            self.assertEqual(payload["created_model"], "reviewer")
            self.assertEqual(config["models"]["shared"]["model"], "grok-4.6")
            self.assertEqual(config["models"]["shared"]["options"]["thinking"], "medium")
            self.assertEqual(config["roles"]["developer"]["default_model"], "shared")
            self.assertEqual(config["roles"]["reviewer"]["default_model"], "reviewer")
            self.assertEqual(config["roles"]["reviewer"]["thinking"], "high")
            self.assertNotIn("options", config["models"]["reviewer"])

    def test_model_update_reports_every_role_using_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = run_config_model(
                    workspace,
                    key="shared",
                    provider="grok",
                    model="grok-4.7",
                    as_json=True,
                    which=_which,
                    run_command=_run_command,
                )
            payload = json.loads(stdout.getvalue())
            config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
            self.assertEqual(code, 0)
            self.assertEqual(payload["updated_model"], "shared")
            self.assertEqual(payload["roles"], ["developer", "reviewer"])
            self.assertEqual(config["models"]["shared"]["model"], "grok-4.7")
            self.assertEqual(config["models"]["shared"]["options"]["thinking"], "medium")
            self.assertEqual(config["models"]["shared"]["options"]["max_turns"], "4")

    def test_one_model_name_can_be_assigned_to_several_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout), patch(
                "agentflow_cli.commands.config.shutil.which", _which
            ):
                code = main(
                    [
                        "config",
                        "role",
                        "developer",
                        "reviewer",
                        "--model-name",
                        "plain",
                        "--json",
                    ],
                    cwd=workspace,
                )
            payload = json.loads(stdout.getvalue())
            config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
        self.assertEqual(code, 0)
        self.assertEqual(payload["roles"], ["developer", "reviewer"])
        self.assertEqual(config["roles"]["developer"]["default_model"], "plain")
        self.assertEqual(config["roles"]["reviewer"]["default_model"], "plain")
        self.assertEqual(config["models"]["shared"]["model"], "grok-4.6")

    def test_rejects_provider_without_model_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                code = run_config_model(workspace, key="shared", provider="grok")
            self.assertEqual(code, 2)
            self.assertIn("--provider", stderr.getvalue())
            self.assertEqual(
                (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8"),
                CONFIG,
            )

    def test_interactive_new_model_writes_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            answers = _Answers(["__new__", "grok", "grok-4.7", "medium", "fast", "y"])
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = run_config_model(
                    workspace,
                    interactive=True,
                    prompter=answers,
                    which=_which,
                    run_command=_run_command,
                )
            self.assertEqual(answers.text_defaults, [""])
            config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
            self.assertEqual(code, 0)
            self.assertEqual(config["models"]["fast"]["provider"], "grok")
            self.assertEqual(config["models"]["fast"]["model"], "grok-4.7")
            self.assertEqual(config["models"]["fast"]["options"]["thinking"], "medium")
            self.assertEqual(config["roles"]["developer"]["default_model"], "shared")
            self.assertIn("ok", stdout.getvalue())

    def test_interactive_role_new_model_leaves_the_name_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            answers = _Answers(
                ["reviewer", "__new__", "grok", "grok-4.7", "high", "fresh", "y"]
            )
            with patch("sys.stdout", io.StringIO()):
                code = run_config_role(
                    workspace,
                    interactive=True,
                    prompter=answers,
                    which=_which,
                    run_command=_run_command,
                )
            config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
        self.assertEqual(code, 0)
        self.assertEqual(answers.text_defaults, [""])
        self.assertEqual(config["roles"]["reviewer"]["default_model"], "fresh")
        self.assertEqual(config["models"]["fresh"]["model"], "grok-4.7")
        self.assertEqual(config["roles"]["developer"]["default_model"], "shared")

    def test_clear_role_thinking_keeps_the_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _project(workspace)
            config_path = workspace / ".agentflow" / "config.yaml"
            config_path.write_text(
                CONFIG.replace(
                    "  reviewer:\n    default_model: shared\n",
                    "  reviewer:\n    default_model: shared\n    thinking: high\n",
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = run_config_role(
                    workspace,
                    roles=("reviewer",),
                    clear_thinking=True,
                    as_json=True,
                    which=_which,
                )
            config = load_yaml_mapping(config_path)
            self.assertEqual(code, 0)
            self.assertNotIn("thinking", config["roles"]["reviewer"])
            self.assertEqual(config["roles"]["reviewer"]["default_model"], "shared")
            self.assertEqual(config["models"]["shared"]["options"]["thinking"], "medium")


if __name__ == "__main__":
    unittest.main()
