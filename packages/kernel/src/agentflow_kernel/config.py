from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .workflow import StageSpec, load_named_workflow
from .yaml_subset import parse_yaml_document


class ConfigurationError(ValueError):
    """Raised when Agentflow configuration cannot be resolved."""


@dataclass(frozen=True)
class StageTarget:
    provider: str
    model: str
    options: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read an Agentflow configuration document as a mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Unable to read configuration: {path} ({exc})") from exc
    try:
        parsed = parse_yaml_document(text)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ConfigurationError("Invalid configuration: expected a mapping.")
    return parsed


def resolve_stage_target(
    workspace: Path,
    workflow_id: str,
    stage_id: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    options: Optional[Mapping[str, str]] = None,
) -> StageTarget:
    if (provider is None) != (model is None):
        raise ConfigurationError(
            "Pass both --provider and --model to override the resolved stage model, or omit both."
        )
    if provider is not None and model is not None:
        return StageTarget(provider=provider, model=model, options=dict(options or {}))
    workflow = load_named_workflow(workspace, workflow_id)
    stage = workflow.stages.get(stage_id)
    if stage is None:
        raise ConfigurationError(f"Stage not found: {stage_id}")
    config = load_yaml_mapping(workspace / ".agentflow/config.yaml")
    return resolve_stage_spec(config, stage, options=options)


def resolve_stage_spec(
    config: Mapping[str, Any],
    stage: StageSpec,
    *,
    options: Optional[Mapping[str, str]] = None,
) -> StageTarget:
    """Resolve one already loaded stage against a config mapping."""
    model_field = (
        f"stages.{stage.id}.model"
        if stage.model_name is not None
        else f"roles.{stage.role}.default_model"
    )
    declared = _resolve_role_cascade(
        config,
        stage.role,
        model_name=stage.model_name,
        model_field=model_field,
        options=dict(stage.options),
    )
    return StageTarget(
        provider=declared.provider,
        model=declared.model,
        options=_merge_options(declared.options, options),
    )


def resolve_role_target(
    workspace: Path,
    role: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    options: Optional[Mapping[str, str]] = None,
) -> StageTarget:
    if (provider is None) != (model is None):
        raise ConfigurationError(
            "Pass both --provider and --model to override the resolved model, or omit both."
        )
    config = load_yaml_mapping(workspace / ".agentflow/config.yaml")
    if provider is not None and model is not None:
        roles = _mapping(config.get("roles"), "roles")
        _mapping(roles.get(role), f"roles.{role}")
        return StageTarget(provider=provider, model=model, options=dict(options or {}))
    return _resolve_role_cascade(config, role, options=options)


def load_model_target(config: Mapping[str, Any], model_name: str) -> StageTarget:
    """Resolve one named model entry without role or stage overrides."""
    models = _mapping(config.get("models"), "models")
    model_config = _mapping(models.get(model_name), f"models.{model_name}")
    return StageTarget(
        provider=_string(model_config.get("provider"), f"models.{model_name}.provider"),
        model=_string(model_config.get("model"), f"models.{model_name}.model"),
        options=_options(model_config.get("options"), f"models.{model_name}.options"),
    )


def _resolve_role_cascade(
    config: dict[str, Any],
    role_key: str,
    *,
    model_name: Optional[str] = None,
    model_field: Optional[str] = None,
    options: Optional[Mapping[str, str]] = None,
) -> StageTarget:
    roles = _mapping(config.get("roles"), "roles")
    role = _mapping(roles.get(role_key), f"roles.{role_key}")
    resolved_field = model_field or f"roles.{role_key}.default_model"
    resolved_model_name = _string(model_name or role.get("default_model"), resolved_field)
    target = load_model_target(config, resolved_model_name)
    role_options = _options(role.get("options"), f"roles.{role_key}.options")
    merged = _merge_options(target.options, role_options, options)
    return StageTarget(provider=target.provider, model=target.model, options=merged)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Missing or invalid {field}.")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Missing or invalid {field}.")
    return value.strip()


_OPTION_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")


def _options(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    mapping = _mapping(value, field)
    parsed: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or _OPTION_KEY.fullmatch(key) is None:
            raise ConfigurationError(f"Missing or invalid {field} key.")
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"Missing or invalid {field}.{key}.")
        parsed[key] = item.strip()
    return parsed


def _merge_options(*layers: Optional[Mapping[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return merged
