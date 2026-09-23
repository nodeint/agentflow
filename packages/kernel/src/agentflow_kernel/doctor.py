from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .base_adapter import BaseCLIAdapter
from .config import (
    ConfigurationError,
    load_model_target,
    load_yaml_mapping,
    resolve_role_target,
    resolve_stage_spec,
)
from .workflow import WorkflowDocument, load_workflow_catalog


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    items: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def diagnose(
    workspace: Path,
    *,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]] = shutil.which,
) -> tuple[Check, Check, Check]:
    """Check config, workflows, and the provider CLIs named by config.

    Each adapter checks the options of models that name it. ``which`` resolves
    that adapter's executable on PATH. The provider CLI is not executed.
    """
    config_check, executable_by_provider, config_problems, config = _config(
        workspace, adapters
    )
    workflow_check = _workflows(workspace, adapters, config_problems, config)
    provider_check = _providers(executable_by_provider, config_problems, which)
    return config_check, workflow_check, provider_check


def _config(
    workspace: Path,
    adapters: Mapping[str, BaseCLIAdapter],
) -> tuple[Check, dict[str, str], tuple[str, ...], Optional[dict[str, Any]]]:
    try:
        config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
    except ConfigurationError as exc:
        problem = str(exc)
        return _failed("config", (problem,)), {}, (problem,), None

    problems: list[str] = []
    executable_by_provider: dict[str, str] = {}
    models = config.get("models")
    roles = config.get("roles")
    if not isinstance(models, dict):
        problems.append("Missing or invalid models.")
        models = {}
    elif not models:
        problems.append("models is empty.")
    if not isinstance(roles, dict):
        problems.append("Missing or invalid roles.")
        roles = {}
    elif not roles:
        problems.append("roles is empty.")

    for model_key in sorted(models):
        try:
            target = load_model_target(config, model_key)
        except ConfigurationError as exc:
            _add(problems, str(exc))
            continue
        adapter = adapters.get(target.provider)
        if adapter is None:
            _add(
                problems,
                f"models.{model_key}.provider is not supported: {target.provider}",
            )
            continue
        executable_by_provider.setdefault(target.provider, adapter.command)
        _reject_options(problems, f"models.{model_key}.options", adapter, target.options)

    role_names: list[str] = []
    for role_key in sorted(roles):
        try:
            resolved = resolve_role_target(workspace, role_key)
        except ConfigurationError as exc:
            _add(problems, str(exc))
            continue
        role_adapter = adapters.get(resolved.provider)
        if role_adapter is not None:
            _reject_options(problems, f"roles.{role_key}", role_adapter, resolved.options)
        role_names.append(role_key)

    if problems:
        return (
            _failed("config", tuple(problems)),
            executable_by_provider,
            tuple(problems),
            config,
        )
    return (
        Check("config", ok=True, items=tuple(role_names)),
        executable_by_provider,
        (),
        config,
    )


def _workflows(
    workspace: Path,
    adapters: Mapping[str, BaseCLIAdapter],
    config_problems: tuple[str, ...],
    config: Optional[dict[str, Any]],
) -> Check:
    catalog = load_workflow_catalog(workspace)
    problems = list(catalog.problems)
    known_problems = set(config_problems)
    if config is not None:
        for document in catalog.documents.values():
            _check_stage_bindings(
                config, document, adapters, known_problems, problems
            )
    items = tuple(
        workflow_id
        for workflow_id in catalog.documents
        if not _workflow_failed(workflow_id, problems)
    )
    if problems:
        return _failed("workflows", tuple(problems), items)
    return Check("workflows", ok=True, items=items)


def _check_stage_bindings(
    config: dict[str, Any],
    document: WorkflowDocument,
    adapters: Mapping[str, BaseCLIAdapter],
    known_problems: set[str],
    problems: list[str],
) -> None:
    for stage in document.stages.values():
        try:
            target = resolve_stage_spec(config, stage)
        except ConfigurationError as exc:
            message = str(exc)
            if message in known_problems:
                continue
            _add(problems, f"{document.id}.{stage.id}: {message}")
            continue
        adapter = adapters.get(target.provider)
        if adapter is None:
            continue
        try:
            adapter.validate_options(target.options)
        except ValueError as exc:
            message = str(exc)
            if any(message in problem for problem in known_problems):
                continue
            _add(problems, f"{document.id}.{stage.id}: {message}")


def _reject_options(
    problems: list[str],
    label: str,
    adapter: BaseCLIAdapter,
    options: Mapping[str, str],
) -> None:
    try:
        adapter.validate_options(options)
    except ValueError as exc:
        _add(problems, f"{label}: {exc}")


def _workflow_failed(workflow_id: str, problems: list[str]) -> bool:
    prefix = f"{workflow_id}:"
    stage_prefix = f"{workflow_id}."
    return any(
        problem.startswith(prefix) or problem.startswith(stage_prefix) for problem in problems
    )


def _providers(
    executable_by_provider: Mapping[str, str],
    config_problems: tuple[str, ...],
    which: Callable[[str], Optional[str]],
) -> Check:
    if not executable_by_provider:
        problem = (
            "No providers could be resolved from config."
            if config_problems
            else "Config names no providers."
        )
        return _failed("providers", (problem,))
    items: list[str] = []
    problems: list[str] = []
    for provider in sorted(executable_by_provider):
        command = executable_by_provider[provider]
        path = which(command)
        if path:
            items.append(f"{provider}  {path}")
        else:
            problems.append(f"{command} is not on PATH")
    if problems:
        return _failed("providers", tuple(problems), tuple(items))
    return Check("providers", ok=True, items=tuple(items))


def _failed(name: str, problems: tuple[str, ...], items: tuple[str, ...] = ()) -> Check:
    return Check(name, ok=False, items=items, problems=problems)


def _add(problems: list[str], message: str) -> None:
    if message not in problems:
        problems.append(message)
