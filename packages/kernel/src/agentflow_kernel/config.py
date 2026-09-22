from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .workflow import WorkflowDocument, load_workflow_document
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

    @property
    def thinking(self) -> Optional[str]:
        value = self.options.get("thinking")
        return value or None


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
    thinking: Optional[str] = None,
) -> StageTarget:
    if (provider is None) != (model is None):
        raise ConfigurationError(
            "Pass both --provider and --model to override the resolved stage model, or omit both."
        )
    if provider is not None and model is not None:
        return StageTarget(
            provider=provider,
            model=model,
            options=_thinking_options(thinking),
        )
    workflow = _load_stage_workflow(workspace, workflow_id)
    stage = workflow.stages.get(stage_id)
    if stage is None:
        raise ConfigurationError(f"Stage not found: {stage_id}")
    config = load_yaml_mapping(workspace / ".agentflow/config.yaml")
    model_field = (
        f"stages.{stage_id}.model"
        if stage.model_key is not None
        else f"roles.{stage.role}.default_model"
    )
    declared = _resolve_role_cascade(
        config,
        stage.role,
        model_key=stage.model_key,
        model_field=model_field,
        thinking=stage.thinking,
        thinking_label=f"Stage {stage_id}",
    )
    return StageTarget(
        provider=declared.provider,
        model=declared.model,
        options=_with_thinking(declared.options, thinking),
    )


def resolve_role_target(
    workspace: Path,
    role: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    thinking: Optional[str] = None,
) -> StageTarget:
    if (provider is None) != (model is None):
        raise ConfigurationError(
            "Pass both --provider and --model to override the resolved model, or omit both."
        )
    config = load_yaml_mapping(workspace / ".agentflow/config.yaml")
    if provider is not None and model is not None:
        roles = _mapping(config.get("roles"), "roles")
        _mapping(roles.get(role), f"roles.{role}")
        return StageTarget(
            provider=provider,
            model=model,
            options=_thinking_options(thinking),
        )
    return _resolve_role_cascade(config, role, thinking=thinking)


def _load_stage_workflow(workspace: Path, workflow_id: str) -> WorkflowDocument:
    workflow_path = workspace / ".agentflow" / "workflows" / f"{workflow_id}.yaml"
    if not workflow_path.is_file():
        raise ConfigurationError(f"Workflow not found: {workflow_id}")
    try:
        return load_workflow_document(workflow_path)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def _resolve_role_cascade(
    config: dict[str, Any],
    role_key: str,
    *,
    model_key: Optional[str] = None,
    model_field: Optional[str] = None,
    thinking: Optional[str] = None,
    thinking_label: Optional[str] = None,
) -> StageTarget:
    roles = _mapping(config.get("roles"), "roles")
    role = _mapping(roles.get(role_key), f"roles.{role_key}")
    models = _mapping(config.get("models"), "models")
    resolved_field = model_field or f"roles.{role_key}.default_model"
    resolved_model_key = _string(model_key or role.get("default_model"), resolved_field)
    model_config = _mapping(models.get(resolved_model_key), f"models.{resolved_model_key}")
    if "thinking" in model_config:
        raise ConfigurationError(
            f"models.{resolved_model_key}.thinking is not supported. "
            f"Put CLI parameters under models.{resolved_model_key}.options."
        )
    provider = _string(model_config.get("provider"), f"models.{resolved_model_key}.provider")
    native_model = _string(model_config.get("model"), f"models.{resolved_model_key}.model")
    options = _options(model_config.get("options"), f"models.{resolved_model_key}.options")
    role_thinking = role.get("thinking")
    if isinstance(role_thinking, str):
        options["thinking"] = _string(role_thinking, f"roles.{role_key}.thinking")
    if thinking is not None:
        options["thinking"] = _string(
            thinking, thinking_label or f"roles.{role_key}.thinking"
        )
    return StageTarget(provider=provider, model=native_model, options=options)


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


def _thinking_options(thinking: Optional[str]) -> dict[str, str]:
    if thinking is None:
        return {}
    return {"thinking": _string(thinking, "thinking")}


def _with_thinking(options: Mapping[str, str], thinking: Optional[str]) -> dict[str, str]:
    resolved = dict(options)
    if thinking is not None:
        resolved["thinking"] = _string(thinking, "thinking")
    return resolved
