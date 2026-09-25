"""Upgrade a project's config and workflows to the current schema version."""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from agentflow_adapters import default_adapters
from agentflow_kernel.base_adapter import BaseCLIAdapter
from agentflow_kernel.config import ConfigurationError
from agentflow_kernel.config_edit import patch_config_text
from agentflow_kernel.migration import (
    SCHEMA_VERSION,
    document_version,
    migrate_config,
    migrate_workflow,
    patch_workflow_text,
)
from agentflow_kernel.workflow import workflows_directory
from agentflow_kernel.yaml_subset import parse_yaml_document

from .display import print_error, print_notice

_OUTDATED = "Schema is outdated. Run `agentflow migrate` to update."


@dataclass(frozen=True)
class _Rewrite:
    path: Path
    text: str
    notes: tuple[str, ...]


def require_current_schema(workspace: Path) -> None:
    """Fail when a config or workflow is older or newer than this Agentflow."""
    newer: list[str] = []
    outdated = False
    for label, path in _schema_files(workspace):
        version = _stored_version(path)
        if version is None or version == SCHEMA_VERSION:
            continue
        if version > SCHEMA_VERSION:
            newer.append(
                f"{label} schema_version {version} is newer than this Agentflow ({SCHEMA_VERSION})."
            )
            continue
        outdated = True
    if outdated:
        raise ConfigurationError(_OUTDATED)
    if newer:
        raise ConfigurationError(" ".join(newer))


def run_migrate(
    workspace: Path,
    *,
    assume_yes: bool = False,
    confirm: Optional[Callable[[str], bool]] = None,
    adapters: Optional[Mapping[str, BaseCLIAdapter]] = None,
) -> int:
    """Ask, then rewrite outdated config and workflows.

    ``assume_yes`` skips the question. Without it, a non-interactive caller
    must pass a ``confirm`` callback or the command stops before writing.
    """
    chosen = default_adapters() if adapters is None else adapters
    rewrites = _pending_rewrites(workspace, chosen)
    if not rewrites:
        print("Schema is current.")
        return 0
    for rewrite in rewrites:
        for note in rewrite.notes:
            print_notice(note)
    if not assume_yes and not _accepted(confirm):
        return 2 if confirm is None and not sys.stdin.isatty() else 0
    for rewrite in rewrites:
        rewrite.path.write_text(rewrite.text, encoding="utf-8")
    print(f"Upgraded schema to version {SCHEMA_VERSION}.")
    return 0


def _accepted(confirm: Optional[Callable[[str], bool]]) -> bool:
    question = f"Upgrade schema to version {SCHEMA_VERSION}?"
    if confirm is not None:
        if confirm(question):
            return True
        print("Left unchanged.")
        return False
    if not sys.stdin.isatty():
        print_error("Pass --yes to upgrade without confirmation.")
        return False
    from InquirerPy import inquirer

    result = inquirer.confirm(message=question, default=False).execute()
    if result is True:
        return True
    print("Left unchanged.")
    return False


def _pending_rewrites(
    workspace: Path, adapters: Mapping[str, BaseCLIAdapter]
) -> tuple[_Rewrite, ...]:
    config_path = workspace / ".agentflow" / "config.yaml"
    if not config_path.is_file():
        return ()
    config, config_rewrite = _config_rewrite(config_path, adapters)
    if config is None:
        return ()
    rewrites: list[_Rewrite] = []
    if config_rewrite is not None:
        rewrites.append(config_rewrite)
    rewrites.extend(_workflow_rewrites(workspace, config, adapters))
    return tuple(rewrites)


def _schema_files(workspace: Path) -> tuple[tuple[str, Path], ...]:
    files = [(".agentflow/config.yaml", workspace / ".agentflow" / "config.yaml")]
    directory = workflows_directory(workspace)
    if directory.is_dir():
        files.extend(
            (f".agentflow/workflows/{path.name}", path)
            for path in sorted(directory.glob("*.yaml"))
            if path.is_file()
        )
    return tuple(files)


def _stored_version(path: Path) -> Optional[int]:
    if not path.is_file():
        return None
    try:
        parsed = parse_yaml_document(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return document_version(parsed)


def _config_rewrite(
    path: Path, adapters: Mapping[str, BaseCLIAdapter]
) -> tuple[Optional[dict[str, Any]], Optional[_Rewrite]]:
    original = path.read_text(encoding="utf-8")
    try:
        parsed = parse_yaml_document(original)
    except ValueError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    updated = copy.deepcopy(parsed)
    notes = migrate_config(updated, adapters)
    if not notes:
        return updated, None
    rewritten = patch_config_text(original, updated)
    if rewritten == original:
        return updated, None
    labeled = tuple(
        f"Migrated .agentflow/config.yaml {note}" if note.startswith("schema_version") else note
        for note in notes
    )
    return updated, _Rewrite(path, rewritten, labeled)


def _workflow_rewrites(
    workspace: Path,
    config: Mapping[str, Any],
    adapters: Mapping[str, BaseCLIAdapter],
) -> tuple[_Rewrite, ...]:
    directory = workflows_directory(workspace)
    if not directory.is_dir():
        return ()
    rewrites: list[_Rewrite] = []
    for path in sorted(item for item in directory.glob("*.yaml") if item.is_file()):
        original = path.read_text(encoding="utf-8")
        try:
            parsed = parse_yaml_document(original)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        updated = copy.deepcopy(parsed)
        changed = migrate_workflow(updated, config, adapters)
        if not changed:
            continue
        rewritten = patch_workflow_text(original, updated)
        if rewritten == original:
            continue
        relative = f".agentflow/workflows/{path.name}"
        notes = tuple(
            f"Migrated {relative} {note}" if note.startswith("schema_version") else f"{relative}: {note}"
            for note in changed
        )
        rewrites.append(_Rewrite(path, rewritten, notes))
    return tuple(rewrites)
