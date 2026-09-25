from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import tempfile
import unittest

from agentflow_adapters import default_adapters
from agentflow_kernel.doctor import diagnose
from tests.support import STAGE_CONFIG, STAGE_WORKFLOW


def _which(found: set[str]):
    def which(command: str) -> str | None:
        if command in found:
            return f"/bin/{command}"
        return None

    return which


class DoctorTests(unittest.TestCase):
    def test_reports_roles_workflows_and_provider_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            config, workflows, providers = diagnose(
                workspace, adapters=default_adapters(), which=_which({"codex", "grok"})
            )
        self.assertTrue(config.ok)
        self.assertEqual(config.items, ("developer", "reviewer"))
        self.assertTrue(workflows.ok)
        self.assertEqual(workflows.items, ("plan-implement",))
        self.assertTrue(providers.ok)
        self.assertEqual(
            providers.items,
            ("codex  /bin/codex", "grok  /bin/grok"),
        )

    def test_rejects_an_unsupported_provider_without_checking_its_command(self) -> None:
        calls: list[str] = []

        def which(command: str) -> str | None:
            calls.append(command)
            return f"/bin/{command}"

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            config_path = workspace / ".agentflow" / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "provider: grok", "provider: claude", 1
                ),
                encoding="utf-8",
            )
            config, _workflows, providers = diagnose(
                workspace, adapters=default_adapters(), which=which
            )
        self.assertFalse(config.ok)
        self.assertIn(
            "models.grok.provider is not supported: claude",
            config.problems,
        )
        self.assertEqual(calls, ["codex"])
        self.assertTrue(providers.ok)
        self.assertEqual(providers.items, ("codex  /bin/codex",))

    def test_reports_a_missing_provider_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            _config, _workflows, providers = diagnose(
                workspace, adapters=default_adapters(), which=_which({"codex"})
            )
        self.assertFalse(providers.ok)
        self.assertEqual(providers.problems, ("grok is not on PATH",))
        self.assertEqual(providers.items, ("codex  /bin/codex",))

    def test_reports_a_workflow_role_and_a_requires_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            workflows_dir = workspace / ".agentflow" / "workflows"
            (workflows_dir / "plan.yaml").write_text(
                "id: plan\n"
                "stages:\n"
                "  - id: plan\n"
                "    role: missing-role\n"
                "completion:\n"
                "  stage: plan\n"
                "  decision: revise\n",
                encoding="utf-8",
            )
            (workflows_dir / "follow-on.yaml").write_text(
                "id: follow-on\n"
                "requires:\n"
                "  workflow: plan\n"
                "  decision: approved\n"
                "stages:\n"
                "  - id: implement\n"
                "    role: developer\n",
                encoding="utf-8",
            )
            (workflows_dir / "notes.txt").write_text("ignore", encoding="utf-8")
            config, workflows, _providers = diagnose(
                workspace, adapters=default_adapters(), which=_which({"codex", "grok"})
            )
        self.assertTrue(config.ok)
        self.assertFalse(workflows.ok)
        self.assertIn("plan-implement", workflows.items)
        self.assertNotIn("plan", workflows.items)
        self.assertNotIn("follow-on", workflows.items)
        self.assertIn(
            "plan.plan: Missing or invalid roles.missing-role.",
            workflows.problems,
        )
        self.assertIn(
            "follow-on: requires plan to complete with approved, "
            "but it completes with revise.",
            workflows.problems,
        )

    def test_reports_a_file_whose_id_does_not_match_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            workflows_dir = workspace / ".agentflow" / "workflows"
            (workflows_dir / "plan-implement.yaml").write_text(
                "id: other\nstages:\n  - id: implement\n    role: developer\n",
                encoding="utf-8",
            )
            _config, workflows, _providers = diagnose(
                workspace, adapters=default_adapters(), which=_which({"codex", "grok"})
            )
        self.assertFalse(workflows.ok)
        self.assertEqual(
            workflows.problems,
            ("plan-implement.yaml: id is other.",),
        )
        self.assertEqual(workflows.items, ())

    def test_reports_invalid_config_and_an_unreadable_workflow_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            agentflow = workspace / ".agentflow"
            workflows = agentflow / "workflows"
            workflows.mkdir(parents=True)
            (agentflow / "config.yaml").write_text("- not-a-map\n", encoding="utf-8")
            (workflows / "broken.yaml").write_text("stages: [\n", encoding="utf-8")
            config, workflow_check, providers = diagnose(
                workspace, adapters=default_adapters(), which=_which({"grok"})
            )
        self.assertFalse(config.ok)
        self.assertIn("Invalid configuration: expected a mapping.", config.problems)
        self.assertFalse(workflow_check.ok)
        self.assertTrue(workflow_check.problems)
        self.assertFalse(providers.ok)
        self.assertEqual(
            providers.problems,
            ("No providers could be resolved from config.",),
        )

    def test_reports_an_option_the_adapter_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            config_path = workspace / ".agentflow" / "config.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "      thinking: high\n",
                    '      thinking: high\n      nope: "1"\n',
                    1,
                ),
                encoding="utf-8",
            )
            config, workflows, _providers = diagnose(
                workspace,
                adapters=default_adapters(),
                which=_which({"codex", "grok"}),
            )
        self.assertFalse(config.ok)
        self.assertIn(
            "models.grok.options: nope is not a grok option.",
            config.problems,
        )
        self.assertIn(
            "roles.developer: nope is not a grok option.",
            config.problems,
        )
        self.assertTrue(workflows.ok)

    def test_does_not_reject_thinking_against_a_builtin_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_stage_project(workspace)
            workflow = workspace / ".agentflow" / "workflows" / "plan-implement.yaml"
            workflow.write_text(
                workflow.read_text(encoding="utf-8").replace(
                    "thinking: high", "thinking: banana", 1
                ),
                encoding="utf-8",
            )
            config, workflows, _providers = diagnose(
                workspace,
                adapters=default_adapters(),
                which=_which({"codex", "grok"}),
            )
        self.assertTrue(config.ok)
        self.assertTrue(workflows.ok)

    def test_reports_a_missing_workflows_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            agentflow = workspace / ".agentflow"
            agentflow.mkdir()
            (agentflow / "config.yaml").write_text(STAGE_CONFIG, encoding="utf-8")
            _config, workflows, _providers = diagnose(
                workspace, adapters=default_adapters(), which=_which({"codex", "grok"})
            )
        self.assertFalse(workflows.ok)
        self.assertEqual(
            workflows.problems,
            ("Workflows directory not found: .agentflow/workflows",),
        )


def _write_stage_project(workspace: Path) -> None:
    workflows = workspace / ".agentflow" / "workflows"
    workflows.mkdir(parents=True)
    (workspace / ".agentflow" / "config.yaml").write_text(STAGE_CONFIG, encoding="utf-8")
    (workflows / "plan-implement.yaml").write_text(STAGE_WORKFLOW, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
