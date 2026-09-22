from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .workflow import WorkflowDocument, load_workflow_document
from .yaml_subset import parse_yaml_document


class ConfigurationError(ValueError):
    """Raised when Agentflow configuration cannot be resolved."""


SUPPORTED_COORDINATOR_PROVIDERS = frozenset({"codex", "grok"})


@dataclass(frozen=True)
class CoordinatorConfig:
    model_key: str
    provider: str
    model: str
    thinking: str


@dataclass(frozen=True)
class StageTarget:
    provider: str
    model: str
    thinking: Optional[str]


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


def resolve_coordinator(config: dict[str, Any]) -> CoordinatorConfig:
    runtime = _mapping(config.get("runtime"), "runtime")
    coordinator = _mapping(runtime.get("coordinator"), "runtime.coordinator")
    model_key = _string(coordinator.get("model"), "runtime.coordinator.model")
    models = _mapping(config.get("models"), "models")
    model_config = _mapping(models.get(model_key), f"models.{model_key}")
    provider = _string(model_config.get("provider"), f"models.{model_key}.provider")
    if provider not in SUPPORTED_COORDINATOR_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_COORDINATOR_PROVIDERS))
        raise ConfigurationError(
            f"runtime.coordinator.model resolved to unsupported provider {provider!r}. "
            f"Supported: {supported}."
        )
    model = _string(model_config.get("model"), f"models.{model_key}.model")
    thinking = coordinator.get("thinking")
    if thinking is None:
        thinking_config = _mapping(model_config.get("thinking"), f"models.{model_key}.thinking")
        thinking = thinking_config.get("default")
    resolved_thinking = _string(thinking, "runtime.coordinator.thinking")
    thinking_config = _mapping(model_config.get("thinking"), f"models.{model_key}.thinking")
    allowed = _strings(thinking_config.get("allowed"), f"models.{model_key}.thinking.allowed")
    if resolved_thinking not in allowed:
        raise ConfigurationError(
            f"runtime.coordinator.thinking {resolved_thinking!r} is not allowed by models.{model_key}."
        )
    return CoordinatorConfig(
        model_key=model_key,
        provider=provider,
        model=model,
        thinking=resolved_thinking,
    )


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
        return StageTarget(provider=provider, model=model, thinking=thinking)
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
        thinking=thinking if thinking is not None else declared.thinking,
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
        return StageTarget(provider=provider, model=model, thinking=thinking)
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
    provider = _string(model_config.get("provider"), f"models.{resolved_model_key}.provider")
    native_model = _string(model_config.get("model"), f"models.{resolved_model_key}.model")
    resolved_thinking = thinking
    if resolved_thinking is None:
        resolved_thinking = role.get("thinking") if isinstance(role.get("thinking"), str) else None
    if resolved_thinking is None:
        thinking_config = _mapping(
            model_config.get("thinking"), f"models.{resolved_model_key}.thinking"
        )
        resolved_thinking = thinking_config.get("default")
    resolved_thinking = _string(
        resolved_thinking, f"models.{resolved_model_key}.thinking.default"
    )
    thinking_config = _mapping(
        model_config.get("thinking"), f"models.{resolved_model_key}.thinking"
    )
    allowed = _strings(
        thinking_config.get("allowed"), f"models.{resolved_model_key}.thinking.allowed"
    )
    if resolved_thinking not in allowed:
        label = thinking_label or f"roles.{role_key}"
        raise ConfigurationError(
            f"{label} thinking {resolved_thinking!r} is not allowed by models.{resolved_model_key}."
        )
    return StageTarget(provider=provider, model=native_model, thinking=resolved_thinking)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Missing or invalid {field}.")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Missing or invalid {field}.")
    return value.strip()


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigurationError(f"Missing or invalid {field}.")
    return value
