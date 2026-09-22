from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import unittest

from agentflow_kernel.config import (
    ConfigurationError,
    load_yaml_mapping,
    resolve_coordinator,
    resolve_role_target,
    resolve_stage_target,
)
from tests.support import (
    CONFIG,
    STAGE_CONFIG,
    write_config,
    write_role_workspace,
    write_stage_workspace,
)


class CoordinatorConfigTests(unittest.TestCase):
    def test_rejects_unsupported_coordinator_provider(self) -> None:
        config = load_yaml_mapping(
            write_config(CONFIG.replace("provider: codex", "provider: claude", 1))
        )
        with self.assertRaisesRegex(ConfigurationError, "unsupported provider 'claude'"):
            resolve_coordinator(config)

    def test_rejects_unsupported_coordinator_thinking(self) -> None:
        config = load_yaml_mapping(
            write_config(CONFIG.replace("thinking: high", "thinking: xhigh", 1))
        )
        with self.assertRaisesRegex(ConfigurationError, "not allowed"):
            resolve_coordinator(config)


class StageTargetTests(unittest.TestCase):
    def test_resolves_role_default_stage_override_and_cli_override(self) -> None:
        workspace = write_stage_workspace()
        role_default = resolve_stage_target(workspace, "plan-implement", "implement")
        stage_override = resolve_stage_target(
            workspace, "plan-implement", "review-work"
        )
        cli_override = resolve_stage_target(
            workspace,
            "plan-implement",
            "implement",
            provider="codex",
            model="gpt-5.6-terra",
            thinking="medium",
        )
        self.assertEqual(
            (role_default.provider, role_default.model, role_default.thinking),
            ("grok", "grok-4.6", "high"),
        )
        self.assertEqual(
            (stage_override.provider, stage_override.model, stage_override.thinking),
            ("codex", "gpt-5.6-terra", "high"),
        )
        self.assertEqual(
            (cli_override.provider, cli_override.model, cli_override.thinking),
            ("codex", "gpt-5.6-terra", "medium"),
        )

    def test_rejects_a_partial_provider_model_override(self) -> None:
        workspace = write_stage_workspace()
        with self.assertRaisesRegex(ConfigurationError, "both --provider and --model"):
            resolve_stage_target(
                workspace, "plan-implement", "implement", provider="grok"
            )

    def test_rejects_a_missing_stage(self) -> None:
        workspace = write_stage_workspace()
        with self.assertRaisesRegex(ConfigurationError, "Stage not found: missing"):
            resolve_stage_target(workspace, "plan-implement", "missing")

    def test_cli_override_returns_before_workflow_read(self) -> None:
        workspace = write_role_workspace()
        target = resolve_stage_target(
            workspace,
            "missing-workflow",
            "missing-stage",
            provider="codex",
            model="gpt-5.6-terra",
            thinking="medium",
        )
        self.assertEqual(
            (target.provider, target.model, target.thinking),
            ("codex", "gpt-5.6-terra", "medium"),
        )

    def test_preserves_stage_thinking_error_label(self) -> None:
        workspace = write_stage_workspace()
        workflow = workspace / ".agentflow" / "workflows" / "plan-implement.yaml"
        workflow.write_text(
            workflow.read_text(encoding="utf-8").replace("thinking: high", "thinking: low"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ConfigurationError,
            r"Stage review-work thinking 'low' is not allowed by models\.gpt-terra\.",
        ):
            resolve_stage_target(workspace, "plan-implement", "review-work")


class YamlSubsetConfigTests(unittest.TestCase):
    def test_preserves_stage_config_lists_and_role_thinking(self) -> None:
        parsed = load_yaml_mapping(write_config(STAGE_CONFIG))
        self.assertEqual(
            parsed["models"]["grok"]["thinking"]["allowed"],
            ["low", "medium", "high", "xhigh"],
        )
        self.assertEqual(parsed["roles"]["reviewer"]["thinking"], "medium")

    def test_rejects_a_non_mapping_configuration(self) -> None:
        path = write_config("- a\n- b\n")
        with self.assertRaisesRegex(
            ConfigurationError, "Invalid configuration: expected a mapping."
        ):
            load_yaml_mapping(path)


class RoleTargetTests(unittest.TestCase):
    def test_rejects_a_missing_role_even_with_provider_model_override(self) -> None:
        workspace = write_role_workspace()
        with self.assertRaisesRegex(ConfigurationError, "roles.missing"):
            resolve_role_target(workspace, "missing")
        with self.assertRaisesRegex(ConfigurationError, "roles.missing"):
            resolve_role_target(
                workspace, "missing", provider="grok", model="grok-4.6"
            )

    def test_cli_override_skips_the_role_cascade(self) -> None:
        workspace = write_role_workspace()
        target = resolve_role_target(
            workspace,
            "developer",
            provider="codex",
            model="gpt-5.6-terra",
            thinking="low",
        )
        self.assertEqual(target.provider, "codex")
        self.assertEqual(target.model, "gpt-5.6-terra")
        self.assertEqual(target.thinking, "low")
        with self.assertRaisesRegex(ConfigurationError, "both --provider and --model"):
            resolve_role_target(workspace, "developer", provider="grok")


if __name__ == "__main__":
    unittest.main()
