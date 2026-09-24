from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from agentflow_cli.commands.skill import install_skill, update_skill
from agentflow_cli.run import main


def _workspace(root: str) -> Path:
    workspace = Path(root)
    config = workspace / ".agentflow" / "config.yaml"
    config.parent.mkdir()
    config.write_text("models: {}\nroles: {}\n", encoding="utf-8")
    return workspace


class SkillCommandTests(unittest.TestCase):
    def test_install_writes_a_managed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)

            self.assertEqual(install_skill(workspace), 0)

            skill_directory = workspace / ".agents" / "skills" / "agentflow"
            content = (skill_directory / "SKILL.md").read_text(encoding="utf-8")
            state = json.loads(
                (skill_directory / ".agentflow-template.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("name: agentflow", content)
            self.assertEqual(state["template_version"], 1)
            self.assertEqual(len(state["content_sha256"]), 64)

    def test_install_refuses_to_replace_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            skill_directory = workspace / ".agents" / "skills" / "agentflow"
            skill_directory.mkdir(parents=True)
            custom = skill_directory / "SKILL.md"
            custom.write_text("custom\n", encoding="utf-8")

            self.assertEqual(install_skill(workspace), 1)
            self.assertEqual(custom.read_text(encoding="utf-8"), "custom\n")

    def test_update_refuses_local_changes_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            self.assertEqual(install_skill(workspace), 0)
            skill = workspace / ".agents" / "skills" / "agentflow" / "SKILL.md"
            skill.write_text("custom\n", encoding="utf-8")

            self.assertEqual(update_skill(workspace), 1)
            self.assertEqual(skill.read_text(encoding="utf-8"), "custom\n")
            self.assertEqual(update_skill(workspace, force=True), 0)
            self.assertIn("name: agentflow", skill.read_text(encoding="utf-8"))

    def test_update_replaces_an_older_managed_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            self.assertEqual(install_skill(workspace), 0)
            skill_directory = workspace / ".agents" / "skills" / "agentflow"
            skill = skill_directory / "SKILL.md"
            old_content = "---\nname: agentflow\ndescription: Old template.\n---\n"
            skill.write_text(old_content, encoding="utf-8")
            (skill_directory / ".agentflow-template.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "template_version": 0,
                        "content_sha256": hashlib.sha256(
                            old_content.encode("utf-8")
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(update_skill(workspace), 0)
            self.assertIn("name: agentflow", skill.read_text(encoding="utf-8"))
            self.assertNotIn("Old template", skill.read_text(encoding="utf-8"))

    def test_update_refuses_an_unmanaged_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            skill = workspace / ".agents" / "skills" / "agentflow" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("custom\n", encoding="utf-8")

            self.assertEqual(update_skill(workspace, force=True), 1)
            self.assertEqual(skill.read_text(encoding="utf-8"), "custom\n")

    def test_cli_exposes_install_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = _workspace(tmp)
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                self.assertEqual(main(["skill", "install"], cwd=workspace), 0)
                self.assertEqual(main(["skill", "update"], cwd=workspace), 0)

            self.assertIn("Installed", stdout.getvalue())
            self.assertIn("Already up to date", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
