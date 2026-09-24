from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
from typing import Annotated

import typer

from ..display import print_error
from ..workspace import find_workspace, invocation_cwd

SKILL_RELATIVE_PATH = Path(".agents/skills/agentflow")
STATE_FILENAME = ".agentflow-template.json"
TEMPLATE_VERSION = 1

skill_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Install or update the Agentflow skill in this project.",
)


@skill_app.command("install")
def install_command() -> None:
    """Install the Agentflow skill without replacing an existing directory."""
    raise typer.Exit(install_skill(find_workspace(invocation_cwd())))


@skill_app.command("update")
def update_command(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Replace a locally modified managed skill with the bundled template.",
        ),
    ] = False,
) -> None:
    """Update a managed Agentflow skill to the bundled template."""
    raise typer.Exit(update_skill(find_workspace(invocation_cwd()), force=force))


def install_skill(workspace: Path) -> int:
    skill_directory = workspace / SKILL_RELATIVE_PATH
    if skill_directory.exists():
        print_error(
            f"{_display_path(skill_directory, workspace)} already exists; "
            "use `agentflow skill update`."
        )
        return 1

    content = _template_content()
    skill_directory.mkdir(parents=True)
    _write_managed_skill(skill_directory, content)
    print(f"Installed {_display_path(skill_directory, workspace)}")
    return 0


def update_skill(workspace: Path, *, force: bool = False) -> int:
    skill_directory = workspace / SKILL_RELATIVE_PATH
    skill_path = skill_directory / "SKILL.md"
    state_path = skill_directory / STATE_FILENAME
    if not skill_directory.is_dir() or not skill_path.is_file():
        print_error("Agentflow skill is not installed; run `agentflow skill install`.")
        return 1
    state = _read_state(state_path)
    if state is None:
        print_error(
            "Existing Agentflow skill is not managed by Agentflow; "
            "move it aside and run `agentflow skill install`."
        )
        return 1

    current = skill_path.read_text(encoding="utf-8")
    installed_digest = state.get("content_sha256")
    current_digest = _digest(current)
    if installed_digest != current_digest and not force:
        print_error(
            "Agentflow skill has local changes; rerun with "
            "`agentflow skill update --force` to replace them."
        )
        return 1

    content = _template_content()
    if (
        current == content
        and installed_digest == current_digest
        and state.get("template_version") == TEMPLATE_VERSION
    ):
        print(f"Already up to date: {_display_path(skill_directory, workspace)}")
        return 0

    _write_managed_skill(skill_directory, content)
    print(f"Updated {_display_path(skill_directory, workspace)}")
    return 0


def _template_content() -> str:
    resource = files("agentflow_cli").joinpath("templates", "agentflow", "SKILL.md")
    return resource.read_text(encoding="utf-8")


def _write_managed_skill(skill_directory: Path, content: str) -> None:
    (skill_directory / "SKILL.md").write_text(content, encoding="utf-8")
    state = {
        "schema_version": 1,
        "template_version": TEMPLATE_VERSION,
        "content_sha256": _digest(content),
    }
    (skill_directory / STATE_FILENAME).write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    return payload


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _display_path(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix()
