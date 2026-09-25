from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import unittest

from agentflow_kernel.config import load_yaml_mapping
from agentflow_kernel.config_edit import (
    ConfigEditError,
    assign_roles,
    inspect_config,
    patch_config_text,
    place_role_model,
    set_role_thinking,
    upsert_model,
)
from agentflow_kernel.workflow import StageSpec, WorkflowDocument
from tests.support import write_config


SHARED = """\
# project models
models:
  shared:
    provider: grok
    model: grok-4.6
    options:
      thinking: medium
      temperature: "0.2"  # stable
  other:
    provider: codex
    model: gpt-5
roles:
  developer:
    default_model: shared
  planner:
    default_model: shared
  reviewer:
    default_model: shared
    thinking: low
"""


def _workflow() -> WorkflowDocument:
    return WorkflowDocument(
        id="plan",
        stages={
            "implement": StageSpec(id="implement", role="developer"),
            "review": StageSpec(id="review", role="reviewer", model_name="other"),
            "audit": StageSpec(
                id="audit", role="reviewer", model_name="shared", thinking="high"
            ),
            "plain-review": StageSpec(id="plain-review", role="reviewer"),
        },
    )


def _config(text: str = SHARED) -> dict:
    return load_yaml_mapping(write_config(text))


class ConfigEditTests(unittest.TestCase):
    def test_assign_role_keeps_the_shared_model(self) -> None:
        config = _config()
        change = assign_roles(config, ("reviewer",), "other", workflows=(_workflow(),))
        self.assertEqual(change.roles, ("reviewer",))
        self.assertEqual(change.config["models"]["shared"]["model"], "grok-4.6")
        self.assertEqual(change.config["roles"]["developer"]["default_model"], "shared")
        self.assertEqual(change.config["roles"]["planner"]["default_model"], "shared")
        self.assertEqual(change.config["roles"]["reviewer"]["default_model"], "other")
        self.assertEqual(change.config["roles"]["reviewer"]["thinking"], "low")
        self.assertEqual(change.stages_updated, (("plan", "plain-review"),))
        self.assertEqual(change.stages_unchanged[0].stage, "review")
        self.assertEqual(change.stages_unchanged[0].ignores, ("role-model",))
        self.assertIn("audit", {stage.stage for stage in change.stages_unchanged})

    def test_place_role_model_creates_a_private_entry(self) -> None:
        config = _config()
        change = place_role_model(
            config,
            "reviewer",
            provider="grok",
            model="grok-4.7",
            thinking="high",
            workflows=(_workflow(),),
        )
        self.assertEqual(change.created_model, "reviewer")
        self.assertIsNone(change.reused_model)
        self.assertEqual(change.config["models"]["shared"]["model"], "grok-4.6")
        self.assertEqual(change.config["models"]["reviewer"]["model"], "grok-4.7")
        self.assertNotIn("options", change.config["models"]["reviewer"])
        self.assertEqual(change.config["roles"]["reviewer"]["default_model"], "reviewer")
        self.assertEqual(change.config["roles"]["reviewer"]["thinking"], "high")
        self.assertEqual(change.config["roles"]["developer"]["default_model"], "shared")
        self.assertEqual(change.orphaned_models, ())

    def test_place_role_model_reuses_an_identical_entry(self) -> None:
        text = """\
models:
  plain:
    provider: grok
    model: grok-4.7
  shared:
    provider: grok
    model: grok-4.6
    options:
      thinking: medium
roles:
  developer:
    default_model: shared
  reviewer:
    default_model: shared
"""
        change = place_role_model(
            _config(text), "reviewer", provider="grok", model="grok-4.7"
        )
        self.assertEqual(change.reused_model, "plain")
        self.assertIsNone(change.created_model)
        self.assertEqual(change.config["roles"]["reviewer"]["default_model"], "plain")
        self.assertEqual(set(change.config["models"]), {"plain", "shared"})

    def test_upsert_thinking_preserves_other_options(self) -> None:
        config = _config()
        change = upsert_model(config, "shared", thinking="high", workflows=(_workflow(),))
        options = change.config["models"]["shared"]["options"]
        self.assertEqual(options["thinking"], "high")
        self.assertEqual(options["temperature"], "0.2")
        self.assertEqual(change.updated_model, "shared")
        self.assertEqual(change.roles, ("developer", "planner"))
        self.assertIn(("plan", "implement"), change.stages_updated)
        shielded = {stage.stage: stage.ignores for stage in change.stages_unchanged}
        self.assertEqual(shielded["audit"], ("role-thinking",))

    def test_clear_thinking_removes_only_the_thinking_option(self) -> None:
        change = upsert_model(_config(), "shared", thinking=None)
        self.assertEqual(
            change.config["models"]["shared"]["options"], {"temperature": "0.2"}
        )

    def test_stage_following_a_model_name_tracks_that_model(self) -> None:
        change = upsert_model(
            _config(),
            "shared",
            provider="grok",
            model="grok-4.7",
            workflows=(_workflow(),),
        )
        self.assertIn(("plan", "audit"), change.stages_updated)
        self.assertNotIn(("plan", "review"), change.stages_updated)

    def test_orphan_model_is_reported_and_kept(self) -> None:
        text = """\
models:
  shared:
    provider: grok
    model: grok-4.6
  review:
    provider: codex
    model: gpt-5
roles:
  developer:
    default_model: shared
  reviewer:
    default_model: review
"""
        change = assign_roles(_config(text), ("reviewer",), "shared")
        self.assertEqual(change.orphaned_models, ("review",))
        self.assertIn("review", change.config["models"])

    def test_patch_preserves_comments_and_sibling_options(self) -> None:
        config = _config()
        change = assign_roles(config, ("reviewer",), "other")
        patched = patch_config_text(SHARED, change.config)
        self.assertIn("# project models", patched)
        self.assertIn('temperature: "0.2"  # stable', patched)
        parsed = load_yaml_mapping(write_config(patched))
        self.assertEqual(parsed["roles"]["developer"]["default_model"], "shared")
        self.assertEqual(parsed["roles"]["reviewer"]["default_model"], "other")
        self.assertEqual(parsed["roles"]["reviewer"]["thinking"], "low")
        self.assertEqual(parsed["models"]["shared"]["options"]["temperature"], "0.2")

    def test_patch_adds_a_model_and_a_role(self) -> None:
        config = _config()
        created = upsert_model(config, "fast", provider="codex", model="gpt-5", thinking="low")
        assigned = assign_roles(created.config, ("security",), "fast")
        patched = patch_config_text(SHARED, assigned.config)
        parsed = load_yaml_mapping(write_config(patched))
        self.assertEqual(parsed["models"]["fast"]["provider"], "codex")
        self.assertEqual(parsed["models"]["fast"]["options"]["thinking"], "low")
        self.assertEqual(parsed["roles"]["security"]["default_model"], "fast")
        self.assertIn("# project models", patched)
        self.assertEqual(parsed["roles"]["developer"]["default_model"], "shared")

    def test_rejects_a_partial_provider_update(self) -> None:
        with self.assertRaisesRegex(ConfigEditError, "both --provider and --model"):
            upsert_model(_config(), "shared", provider="grok")

    def test_role_thinking_does_not_change_the_model(self) -> None:
        change = set_role_thinking(_config(), "developer", "high", workflows=(_workflow(),))
        self.assertEqual(change.roles, ("developer",))
        self.assertEqual(change.config["models"]["shared"]["options"]["thinking"], "medium")
        self.assertEqual(change.config["roles"]["developer"]["thinking"], "high")
        self.assertIn(("plan", "implement"), change.stages_updated)

    def test_new_role_points_at_an_existing_model(self) -> None:
        change = assign_roles(_config(), ("security", "planner"), "other")
        self.assertEqual(change.config["roles"]["security"]["default_model"], "other")
        self.assertEqual(change.config["roles"]["planner"]["default_model"], "other")
        self.assertNotIn("security", change.config["models"])

    def test_inspect_shows_the_model_name_and_stage_override(self) -> None:
        view = inspect_config(_config(), (_workflow(),))
        reviewer = next(role for role in view.roles if role.role == "reviewer")
        self.assertEqual(reviewer.model_name, "shared")
        self.assertEqual(reviewer.thinking_from, "role")
        self.assertEqual(reviewer.shared_with, ("developer", "planner"))
        overrides = {
            stage.stage: (stage.model_name, stage.thinking) for stage in reviewer.stages
        }
        self.assertEqual(overrides["review"], ("other", None))
        self.assertEqual(overrides["audit"], ("shared", "high"))
        self.assertEqual(overrides["plain-review"], (None, None))


if __name__ == "__main__":
    unittest.main()
