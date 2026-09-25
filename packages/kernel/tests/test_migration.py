from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import unittest

from agentflow_adapters.codex_adapter import CodexAdapter
from agentflow_adapters.grok_adapter import GrokAdapter
from agentflow_kernel.config import ConfigurationError, load_yaml_mapping
from agentflow_kernel.config_edit import patch_config_text
from agentflow_kernel.migration import (
    SCHEMA_VERSION,
    document_version,
    migrate_config,
    migrate_workflow,
    patch_workflow_text,
)
from agentflow_kernel.workflow import load_workflow_document
from agentflow_kernel.yaml_subset import parse_yaml_document
from tests.support import write_config


LEGACY = """\
# project
project: "Local Multi-Model Agent System"
version: "1.0"

models:
  grok:
    provider: grok
    model: grok-4.6
    options:
      thinking: medium
  gpt-terra:
    provider: codex
    model: gpt-5.6-terra
    options:
      thinking: medium
      temperature: "0.2"

roles:
  planner:
    role: "System Architect & Planner"
    description: "Break the task down."
    default_model: grok
    thinking: high
  reviewer:
    role: "Reviewer"
    default_model: gpt-terra
"""

WORKFLOW = """\
# review flow
id: plan
stages:
  - id: plan
    role: planner
    depends_on: []
    instructions:
      - Keep the word thinking in the prose.
    thinking: high
  - id: review
    role: reviewer
    model: gpt-terra
    depends_on:
      - plan
    options:
      thinking: low
completion:
  stage: review
"""


class _StepAdapter:
    def __init__(self) -> None:
        self.models: list[str] = []
        self.roles: list[str] = []
        self.stages: list[str] = []

    def model_migrations(self) -> dict[int, object]:
        return {0: lambda body: self.models.append(body["model"])}

    def role_migrations(self) -> dict[int, object]:
        return {0: lambda body: self.roles.append(body["default_model"])}

    def stage_migrations(self) -> dict[int, object]:
        return {0: lambda body: self.stages.append(body["id"])}


class _ChainAdapter:
    def __init__(self) -> None:
        self.trace: list[tuple[str, int]] = []

    def _mark(self, kind: str, version: int):
        def apply(body: dict, kind: str = kind, version: int = version) -> None:
            del body
            self.trace.append((kind, version))

        return apply

    def model_migrations(self) -> dict[int, object]:
        return {version: self._mark("model", version) for version in (0, 1, 2)}

    def role_migrations(self) -> dict[int, object]:
        return {version: self._mark("role", version) for version in (0, 1, 2)}

    def stage_migrations(self) -> dict[int, object]:
        return {version: self._mark("stage", version) for version in (0, 1, 2)}


class MigrationTests(unittest.TestCase):
    def adapters(self) -> dict[str, object]:
        return {"grok": GrokAdapter(), "codex": CodexAdapter()}

    def test_version_steps_follow_each_models_provider(self) -> None:
        config = load_yaml_mapping(write_config(LEGACY))
        grok = _StepAdapter()
        codex = _StepAdapter()
        notes = migrate_config(config, {"grok": grok, "codex": codex})  # type: ignore[arg-type]
        self.assertEqual(notes[0], "schema_version -> 1")
        self.assertEqual(grok.models, ["grok-4.6"])
        self.assertEqual(codex.models, ["gpt-5.6-terra"])
        self.assertEqual(grok.roles, ["grok"])
        self.assertEqual(codex.roles, ["gpt-terra"])
        self.assertEqual(config["schema_version"], 1)
        workflow = {
            "id": "plan",
            "stages": [
                {"id": "plan", "role": "planner"},
                {"id": "review", "role": "reviewer", "model": "gpt-terra"},
            ],
        }
        migrate_workflow(workflow, config, {"grok": grok, "codex": codex})  # type: ignore[arg-type]
        self.assertEqual(grok.stages, ["plan"])
        self.assertEqual(codex.stages, ["review"])

    def test_current_version_is_left_alone(self) -> None:
        config = {"schema_version": "1", "models": {}, "roles": {}}
        grok = _StepAdapter()
        self.assertEqual(migrate_config(config, {"grok": grok}), ())  # type: ignore[arg-type]
        self.assertEqual(grok.models, [])
        self.assertEqual(document_version(config), SCHEMA_VERSION)

    def test_an_unknown_provider_is_skipped_and_the_version_still_advances(self) -> None:
        config = {
            "models": {"custom": {"provider": "other", "model": "x", "options": {"thinking": "medium"}}},
            "roles": {},
        }
        notes = migrate_config(config, {})
        self.assertEqual(notes, ("schema_version -> 1",))
        self.assertEqual(config["models"]["custom"]["options"]["thinking"], "medium")
        self.assertEqual(config["schema_version"], 1)

    def test_newer_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "newer than this Agentflow"):
            migrate_config({"schema_version": 2}, {})

    def test_thinking_moves_to_the_provider_option_and_keeps_the_file(self) -> None:
        config = load_yaml_mapping(write_config(LEGACY))
        notes = migrate_config(config, self.adapters())  # type: ignore[arg-type]
        self.assertIn("models.grok.options.thinking -> reasoning-effort", notes)
        self.assertIn("models.gpt-terra.options.thinking -> model_reasoning_effort", notes)
        self.assertIn("roles.planner.thinking -> options.reasoning-effort", notes)
        self.assertEqual(config["models"]["grok"]["options"], {"reasoning-effort": "medium"})
        self.assertEqual(
            config["models"]["gpt-terra"]["options"],
            {"model_reasoning_effort": "medium", "temperature": "0.2"},
        )
        self.assertEqual(config["roles"]["planner"]["options"], {"reasoning-effort": "high"})
        self.assertNotIn("thinking", config["roles"]["planner"])
        self.assertEqual(config["roles"]["planner"]["description"], "Break the task down.")
        rewritten = patch_config_text(LEGACY, config)
        parsed = load_yaml_mapping(write_config(rewritten))
        self.assertEqual(parsed["version"], "1.0")
        self.assertEqual(parsed["schema_version"], "1")
        self.assertEqual(parsed["models"]["grok"]["options"]["reasoning-effort"], "medium")
        self.assertNotIn("thinking", rewritten)
        self.assertIn("# project", rewritten)
        self.assertIn('role: "System Architect & Planner"', rewritten)

    def test_workflow_stage_keeps_instructions_and_records_its_version(self) -> None:
        config = load_yaml_mapping(write_config(LEGACY))
        migrate_config(config, self.adapters())  # type: ignore[arg-type]
        parsed = parse_yaml_document(WORKFLOW)
        notes = migrate_workflow(parsed, config, self.adapters())  # type: ignore[arg-type]
        self.assertIn("stages.plan.thinking -> options.reasoning-effort", notes)
        self.assertIn("stages.review.options.thinking -> model_reasoning_effort", notes)
        rewritten = patch_workflow_text(WORKFLOW, parsed)
        self.assertIn("Keep the word thinking in the prose.", rewritten)
        self.assertIn("# review flow", rewritten)
        document = load_workflow_document(write_config(rewritten))
        self.assertEqual(document.stages["plan"].options, (("reasoning-effort", "high"),))
        self.assertEqual(
            document.stages["review"].options, (("model_reasoning_effort", "low"),)
        )
        self.assertIn("schema_version: 1", rewritten)

    def test_an_upgrade_runs_each_version_and_skips_versions_already_passed(self) -> None:
        adapter = _ChainAdapter()
        config = {
            "schema_version": 1,
            "models": {"m": {"provider": "grok", "model": "x"}},
            "roles": {"dev": {"default_model": "m"}},
        }
        migrate_config(config, {"grok": adapter}, current=3)  # type: ignore[arg-type]
        self.assertEqual(
            [item for item in adapter.trace if item[0] == "model"],
            [("model", 1), ("model", 2)],
        )
        self.assertEqual(
            [item for item in adapter.trace if item[0] == "role"],
            [("role", 1), ("role", 2)],
        )
        self.assertEqual(config["schema_version"], 3)
        workflow = {"schema_version": 1, "stages": [{"id": "plan", "role": "dev"}]}
        migrate_workflow(workflow, config, {"grok": adapter}, current=3)  # type: ignore[arg-type]
        self.assertEqual(
            [item for item in adapter.trace if item[0] == "stage"],
            [("stage", 1), ("stage", 2)],
        )
        self.assertEqual(workflow["schema_version"], 3)

    def test_an_existing_option_wins_over_thinking(self) -> None:
        body = {"options": {"thinking": "medium", "reasoning-effort": "low"}, "thinking": "high"}
        GrokAdapter()._migrate_model_0(body)
        self.assertEqual(body, {"options": {"reasoning-effort": "low"}})


if __name__ == "__main__":
    unittest.main()
