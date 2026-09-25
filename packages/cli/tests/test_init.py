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

from agentflow_cli.commands.init import run_init
from agentflow_cli.run import main


def _run_command(argv: list[str]) -> str:
    if argv[:2] == ["codex", "debug"]:
        return json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5",
                        "visibility": "list",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "medium"},
                            {"effort": "high"},
                            {"effort": "xhigh"},
                        ],
                    },
                    {"slug": "hidden", "visibility": "hide"},
                ]
            }
        )
    if argv[:2] == ["grok", "models"]:
        return (
            "Default model: grok-4.7\n"
            "Available models:\n"
            "  * grok-4.7 (default)\n"
            "  - grok-4.6\n"
        )
    if argv[:1] == ["grok"] and "--reasoning-effort" in argv:
        return "use one of: xhigh, high, medium, low"
    raise AssertionError(argv)


class _ScriptedPrompt:
    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)

    def select_provider(self, rows, message: str) -> str:
        del message
        raw = next(self._answers)
        return rows[0][0] if raw == "" else raw

    def select_model(self, provider: str, catalog, message: str) -> str:
        del provider, message
        raw = next(self._answers)
        if raw == "":
            return catalog.default_id or catalog.ids[0]
        if raw not in catalog.ids:
            raise ValueError(f"Unknown model: {raw}.")
        return raw

    def select_thinking(self, values: tuple[str, ...], message: str) -> str:
        del message
        raw = next(self._answers)
        if raw == "":
            return ""
        if raw not in values:
            raise ValueError(f"Unknown thinking value: {raw}.")
        return raw

    def confirm(self, message: str) -> bool:
        del message
        return next(self._answers) not in {"n", "no"}


def _which(found: set[str]):
    def which(command: str) -> str | None:
        if command in found:
            return f"/bin/{command}"
        return None

    return which


class InitCommandTests(unittest.TestCase):
    def test_writes_one_provider_and_the_implement_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = run_init(
                    workspace,
                    provider="codex",
                    model="gpt-5",
                    run_command=_run_command,
                    which=_which({"codex", "grok"}),
                )
            config = (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8")
            workflow = (
                workspace / ".agentflow" / "workflows" / "implement.yaml"
            ).read_text(encoding="utf-8")
            gitignore = (workspace / ".agentflow" / ".gitignore").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("provider: codex", config)
            self.assertIn("model: gpt-5", config)
            self.assertNotIn("thinking", config)
            self.assertNotIn("options", config)
            self.assertIn("default_model: default", config)
            self.assertNotIn("grok", config)
            self.assertNotIn("planner", config)
            self.assertIn("id: implement", workflow)
            self.assertIn("role: developer", workflow)
            self.assertEqual(gitignore, "sessions/\n")
            text = stdout.getvalue()
            self.assertIn(".agentflow/workflows/implement.yaml", text)
            self.assertIn('agentflow start implement --task "..."', text)
            self.assertIn("ok", text)

    def test_refuses_a_partial_project_and_leaves_it_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            root = workspace / ".agentflow"
            root.mkdir()
            config = root / "config.yaml"
            config.write_text("partial: true\n", encoding="utf-8")
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                code = run_init(
                    workspace,
                    provider="codex",
                    model="gpt-5",
                    run_command=_run_command,
                    which=_which({"codex"}),
                )
            self.assertEqual(code, 1)
            self.assertEqual(config.read_text(encoding="utf-8"), "partial: true\n")
            self.assertFalse((root / "workflows").exists())
            self.assertIn(".agentflow/config.yaml", stderr.getvalue())

    def test_reports_an_existing_valid_project_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.assertEqual(
                run_init(
                    workspace,
                    provider="codex",
                    model="gpt-5",
                    run_command=_run_command,
                    which=_which({"codex"}),
                ),
                0,
            )
            config = workspace / ".agentflow" / "config.yaml"
            original = config.read_text(encoding="utf-8")
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = run_init(
                    workspace,
                    provider="grok",
                    model="grok-4",
                    run_command=_run_command,
                    which=_which(set()),
                )
            self.assertEqual(code, 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertIn("Already initialized.", stdout.getvalue())

    def test_plan_review_shares_the_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = run_init(
                workspace,
                provider="codex",
                model="gpt-5",
                preset="plan",
                run_command=_run_command,
                which=_which({"codex"}),
            )
            config = (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8")
            workflow = (
                workspace / ".agentflow" / "workflows" / "plan.yaml"
            ).read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("planner:", config)
            self.assertIn("reviewer:", config)
            self.assertNotIn("\n  review:", config)
            self.assertIn("id: plan", workflow)
            self.assertIn("revise: plan", workflow)

    def test_interactive_plan_review_can_choose_a_second_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = run_init(
                workspace,
                preset="plan",
                interactive=True,
                run_command=_run_command,
                which=_which({"codex", "grok"}),
                prompter=_ScriptedPrompt(["", "gpt-5", "", "n", "grok", "", "high"]),
            )
            config = (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("provider: codex", config)
            self.assertIn("model: gpt-5", config)
            self.assertNotIn("thinking: medium", config)
            self.assertIn("provider: grok", config)
            self.assertIn("model: grok-4.7", config)
            self.assertIn("thinking: high", config)
            self.assertIn("default_model: review", config)

    def test_interactive_prefers_an_installed_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = run_init(
                workspace,
                interactive=True,
                run_command=_run_command,
                which=_which({"grok"}),
                prompter=_ScriptedPrompt(["", "", ""]),
            )
            config = (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("provider: grok", config)
            self.assertIn("model: grok-4.7", config)
            self.assertNotIn("codex", config)

    def test_writes_thinking_only_when_it_is_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            code = run_init(
                workspace,
                provider="codex",
                model="gpt-5",
                thinking="high",
                run_command=_run_command,
                which=_which({"codex"}),
            )
            config = (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("thinking: high", config)

    def test_rejects_a_model_the_provider_cli_did_not_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                code = run_init(
                    workspace,
                    provider="codex",
                    model="hidden",
                    run_command=_run_command,
                    which=_which({"codex"}),
                )
            self.assertEqual(code, 2)
            self.assertFalse((workspace / ".agentflow").exists())
            self.assertIn("Unknown model: hidden", stderr.getvalue())

    def test_rejects_a_thinking_value_the_adapter_does_not_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                code = run_init(
                    workspace,
                    provider="codex",
                    model="gpt-5",
                    thinking="max",
                    run_command=_run_command,
                    which=_which({"codex"}),
                )
            self.assertEqual(code, 2)
            self.assertFalse((workspace / ".agentflow").exists())
            self.assertIn("thinking must be one of", stderr.getvalue())

    def test_requires_provider_and_model_when_not_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stderr = io.StringIO()
            with (
                patch("sys.stderr", stderr),
                patch("sys.stdin.isatty", return_value=False),
            ):
                code = main(["init"], cwd=workspace)
            self.assertEqual(code, 2)
            self.assertFalse((workspace / ".agentflow").exists())
            self.assertIn("--provider", stderr.getvalue())
            stderr = io.StringIO()
            with (
                patch("sys.stderr", stderr),
                patch("sys.stdin.isatty", return_value=False),
            ):
                code = main(["init", "--provider", "codex"], cwd=workspace)
            self.assertEqual(code, 2)
            self.assertIn("--model", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
