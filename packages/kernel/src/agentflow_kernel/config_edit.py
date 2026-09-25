"""Edit `.agentflow/config.yaml` models and roles without rewriting the file."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .config import ConfigurationError, load_model_target, resolve_stage_spec
from .workflow import WorkflowDocument
from .yaml_subset import parse_yaml_document


class ConfigEditError(ConfigurationError):
    """Raised when a config edit is rejected before the file changes."""


class _UnsetType:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


@dataclass(frozen=True)
class StageRef:
    workflow: str
    stage: str
    model_name: Optional[str] = None
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ModelView:
    key: str
    provider: str
    model: str
    options: Mapping[str, str]
    roles: tuple[str, ...]
    stages: tuple[StageRef, ...]


@dataclass(frozen=True)
class RoleView:
    role: str
    model_name: str
    provider: str
    model: str
    options: Mapping[str, str]
    shared_with: tuple[str, ...]
    stages: tuple[StageRef, ...]


@dataclass(frozen=True)
class ConfigView:
    models: tuple[ModelView, ...]
    roles: tuple[RoleView, ...]


@dataclass(frozen=True)
class StageShield:
    workflow: str
    stage: str
    ignores: tuple[str, ...]


@dataclass(frozen=True)
class ConfigChange:
    config: dict[str, Any]
    created_model: Optional[str]
    reused_model: Optional[str]
    updated_model: Optional[str]
    roles: tuple[str, ...]
    stages_updated: tuple[tuple[str, str], ...]
    stages_unchanged: tuple[StageShield, ...]
    orphaned_models: tuple[str, ...]


@dataclass
class _Block:
    key: str
    indent: int
    line: int
    end: int
    value: str
    children: list["_Block"]


def inspect_config(
    config: Mapping[str, Any],
    workflows: Sequence[WorkflowDocument] = (),
) -> ConfigView:
    """Return the models, roles, and stage overrides a config editor shows."""
    models = _models(config)
    roles = _roles(config)
    model_views: list[ModelView] = []
    for key in models:
        target = load_model_target(config, key)
        used_by = tuple(
            name
            for name, role in roles.items()
            if role.get("default_model") == key
        )
        model_views.append(
            ModelView(
                key=key,
                provider=target.provider,
                model=target.model,
                options=dict(target.options),
                roles=used_by,
                stages=tuple(_stages_using(workflows, roles, key)),
            )
        )
    role_views: list[RoleView] = []
    for name, role in roles.items():
        model_name = _string(role.get("default_model"), f"roles.{name}.default_model")
        target = load_model_target(config, model_name)
        shared = tuple(
            other
            for other, other_role in roles.items()
            if other != name and other_role.get("default_model") == model_name
        )
        role_views.append(
            RoleView(
                role=name,
                model_name=model_name,
                provider=target.provider,
                model=target.model,
                options=_role_option_map(role),
                shared_with=shared,
                stages=tuple(_role_stages(workflows, name)),
            )
        )
    return ConfigView(models=tuple(model_views), roles=tuple(role_views))


def upsert_model(
    config: Mapping[str, Any],
    key: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    options: Any = UNSET,
    workflows: Sequence[WorkflowDocument] = (),
) -> ConfigChange:
    """Create a model name or update the fields that were passed.

    `options` maps option names to a new value, or to None to remove that option.
    """
    if (provider is None) != (model is None):
        raise ConfigEditError("Pass both --provider and --model, or omit both.")
    updated = _copy(config)
    models = _models(updated)
    exists = key in models
    if not exists:
        _require_name(key, "name")
    if not exists and provider is None:
        raise ConfigEditError("Pass --provider and --model to add a model.")
    if exists:
        body = models[key]
        if not isinstance(body, dict):
            raise ConfigurationError(f"Missing or invalid models.{key}.")
    else:
        body = {}
        models[key] = body
    if provider is not None and model is not None:
        body["provider"] = _plain(provider, "--provider")
        body["model"] = _plain(model, "--model")
    if options is not UNSET:
        _apply_model_options(body, key, options)
    return describe_change(config, updated, workflows)


def assign_roles(
    config: Mapping[str, Any],
    roles: Sequence[str],
    model_name: str,
    *,
    workflows: Sequence[WorkflowDocument] = (),
) -> ConfigChange:
    """Point each role at an existing model name, creating a missing role."""
    names = _role_names(roles)
    updated = _copy(config)
    models = _models(updated)
    if model_name not in models:
        raise ConfigEditError(f"Unknown name: {model_name}.")
    role_map = _roles(updated)
    for name in names:
        if name not in role_map:
            _require_name(name, "role")
            role_map[name] = {"default_model": model_name}
            continue
        role = role_map[name]
        if not isinstance(role, dict):
            raise ConfigurationError(f"Missing or invalid roles.{name}.")
        role["default_model"] = model_name
    return describe_change(config, updated, workflows)


def set_role_options(
    config: Mapping[str, Any],
    role: str,
    options: Mapping[str, Optional[str]],
    *,
    workflows: Sequence[WorkflowDocument] = (),
) -> ConfigChange:
    """Set or clear keys under `roles.<role>.options`."""
    _require_name(role, "role")
    updated = _copy(config)
    role_map = _roles(updated)
    if role not in role_map or not isinstance(role_map[role], dict):
        raise ConfigEditError(f"Unknown role: {role}.")
    _apply_role_options(role_map[role], role, options)
    return describe_change(config, updated, workflows)


def place_role_model(
    config: Mapping[str, Any],
    role: str,
    *,
    provider: str,
    model: str,
    options: Any = UNSET,
    workflows: Sequence[WorkflowDocument] = (),
) -> ConfigChange:
    """Point one role at a provider model without changing a shared entry.

    An existing entry is reused when its provider, model id, and options are
    all empty. Otherwise a new key is added. `options` are stored on the role.
    """
    _require_name(role, "role")
    provider_name = _plain(provider, "--provider")
    model_id = _plain(model, "--model")
    updated = _copy(config)
    models = _models(updated)
    match = matching_model_name(
        updated, provider=provider_name, model=model_id, options={}
    )
    created: Optional[str] = None
    reused: Optional[str] = None
    if match is None:
        match = suggest_model_name(updated, role)
        models[match] = {"provider": provider_name, "model": model_id}
        created = match
    else:
        reused = match
    role_map = _roles(updated)
    if role not in role_map:
        role_map[role] = {"default_model": match}
    else:
        body = role_map[role]
        if not isinstance(body, dict):
            raise ConfigurationError(f"Missing or invalid roles.{role}.")
        body["default_model"] = match
    if options is not UNSET:
        _apply_role_options(role_map[role], role, options)
    change = describe_change(config, updated, workflows, reused_model=reused)
    if created is not None:
        return ConfigChange(
            config=change.config,
            created_model=created,
            reused_model=None,
            updated_model=change.updated_model,
            roles=change.roles,
            stages_updated=change.stages_updated,
            stages_unchanged=change.stages_unchanged,
            orphaned_models=change.orphaned_models,
        )
    return change


def matching_model_name(
    config: Mapping[str, Any],
    *,
    provider: str,
    model: str,
    options: Mapping[str, str],
) -> Optional[str]:
    """Return the first model name with the same provider, model id, and options."""
    models = config.get("models")
    if not isinstance(models, dict):
        raise ConfigurationError("Missing or invalid models.")
    wanted = dict(options)
    for key in models:
        try:
            target = load_model_target(config, key)
        except ConfigurationError:
            continue
        if (
            target.provider == provider
            and target.model == model
            and dict(target.options) == wanted
        ):
            return str(key)
    return None


def suggest_model_name(config: Mapping[str, Any], role: str) -> str:
    """Pick a free model name from the role name."""
    models = config.get("models")
    taken = models if isinstance(models, dict) else {}
    base = role if _NAME.fullmatch(role) else "model"
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


def describe_change(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    workflows: Sequence[WorkflowDocument] = (),
    *,
    reused_model: Optional[str] = None,
) -> ConfigChange:
    """Describe roles, stages, and model names affected by a config edit."""
    after_copy = copy.deepcopy(dict(after))
    before_roles = _mapping_or_empty(before.get("roles"))
    after_roles = _mapping_or_empty(after_copy.get("roles"))
    before_models = _mapping_or_empty(before.get("models"))
    after_models = _mapping_or_empty(after_copy.get("models"))
    changed_roles = tuple(
        name
        for name in dict.fromkeys([*after_roles, *before_roles])
        if _binding(before_roles.get(name)) != _binding(after_roles.get(name))
        or _resolved_role(before, name) != _resolved_role(after_copy, name)
    )
    created = [key for key in after_models if key not in before_models]
    updated = [
        key
        for key in after_models
        if key in before_models and _signature(before_models[key]) != _signature(after_models[key])
    ]
    created_model = created[0] if len(created) == 1 else None
    updated_model = updated[0] if len(updated) == 1 else None
    if created_model is not None or updated_model is not None:
        reused_model = None
    stages_updated: list[tuple[str, str]] = []
    stages_unchanged: list[StageShield] = []
    for document in workflows:
        for stage in document.stages.values():
            before_target = _resolved_stage(before, stage)
            after_target = _resolved_stage(after_copy, stage)
            if before_target != after_target:
                stages_updated.append((document.id, stage.id))
                continue
            ignores = _shields(
                stage,
                before_roles,
                after_roles,
                before_models,
                after_models,
                updated,
            )
            if ignores:
                stages_unchanged.append(
                    StageShield(document.id, stage.id, tuple(ignores))
                )
    return ConfigChange(
        config=after_copy,
        created_model=created_model,
        reused_model=reused_model,
        updated_model=updated_model,
        roles=changed_roles,
        stages_updated=tuple(stages_updated),
        stages_unchanged=tuple(stages_unchanged),
        orphaned_models=_orphans(before, after_copy, workflows),
    )


def patch_config_text(original: str, updated: Mapping[str, Any]) -> str:
    """Apply model and role changes to the original YAML text."""
    if "\t" in original:
        raise ConfigEditError("Cannot edit a config that uses tabs.")
    current = parse_yaml_document(original)
    if not isinstance(current, dict):
        raise ConfigEditError("Invalid configuration: expected a mapping.")
    lines = original.splitlines()
    unit = _indent_unit(lines)
    blocks = _parse_blocks(lines, unit)
    edits: list[tuple[int, int, list[str]]] = []
    _sync_schema_version(lines, updated, edits)
    _sync_models(lines, blocks, current, updated, unit, edits)
    _sync_roles(lines, blocks, current, updated, unit, edits)
    numbered = list(enumerate(edits))
    for _order, (start, end, replacement) in sorted(
        numbered, key=lambda item: (item[1][0], item[1][1], item[0]), reverse=True
    ):
        lines[start:end] = replacement
    text = "\n".join(lines)
    if original.endswith("\n") or original == "":
        text += "\n"
    return text


def _sync_models(
    lines: list[str],
    blocks: list[_Block],
    current: Mapping[str, Any],
    updated: Mapping[str, Any],
    unit: int,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    current_models = _mapping_or_empty(current.get("models"))
    updated_models = _mapping_or_empty(updated.get("models"))
    section = _section(blocks, "models")
    fresh: list[str] = []
    for key, body in updated_models.items():
        if not isinstance(body, dict):
            raise ConfigurationError(f"Missing or invalid models.{key}.")
        if key not in current_models:
            fresh.extend(_model_lines(key, body, unit, unit))
            continue
        if section is None:
            raise ConfigEditError("Cannot edit models in this file.")
        block = _child(section, str(key))
        if block is None:
            raise ConfigEditError(f"Cannot edit models.{key} in this file.")
        _sync_scalar(lines, block, "provider", body.get("provider"), f"models.{key}.provider", edits)
        _sync_scalar(lines, block, "model", body.get("model"), f"models.{key}.model", edits)
        _sync_options(lines, block, current_models[key], body, str(key), unit, edits)
        _drop_removed_scalars(block, current_models[key], body, edits)
    if fresh:
        if section is None:
            edits.append((0, 0, ["models:", *fresh]))
        else:
            edits.append((section.end, section.end, fresh))


def _sync_roles(
    lines: list[str],
    blocks: list[_Block],
    current: Mapping[str, Any],
    updated: Mapping[str, Any],
    unit: int,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    current_roles = _mapping_or_empty(current.get("roles"))
    updated_roles = _mapping_or_empty(updated.get("roles"))
    section = _section(blocks, "roles")
    fresh: list[str] = []
    for key, body in updated_roles.items():
        if not isinstance(body, dict):
            raise ConfigurationError(f"Missing or invalid roles.{key}.")
        if key not in current_roles:
            fresh.extend(_role_lines(str(key), body, unit, unit))
            continue
        if section is None:
            raise ConfigEditError("Cannot edit roles in this file.")
        block = _child(section, str(key))
        if block is None:
            raise ConfigEditError(f"Cannot edit roles.{key} in this file.")
        _sync_scalar(
            lines,
            block,
            "default_model",
            body.get("default_model"),
            f"roles.{key}.default_model",
            edits,
        )
        _sync_role_options(lines, block, current_roles[key], body, str(key), unit, edits)
        _drop_removed_scalars(block, current_roles[key], body, edits)
    if not fresh:
        return
    if section is None:
        edits.append((len(lines), len(lines), ["roles:", *fresh]))
        return
    edits.append((section.end, section.end, fresh))


def _sync_schema_version(
    lines: list[str],
    updated: Mapping[str, Any],
    edits: list[tuple[int, int, list[str]]],
) -> None:
    value = updated.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int):
        return
    rendered = f"schema_version: {value}"
    for block in _parse_blocks(lines, _indent_unit(lines)):
        if block.indent != 0 or block.key != "schema_version":
            continue
        if _scalar(block.value) == str(value):
            return
        edits.append((block.line, block.line + 1, [_with_value(lines[block.line], str(value))]))
        return
    insert = next(
        (index for index, line in enumerate(lines) if _content_indent(line) is not None),
        len(lines),
    )
    edits.append((insert, insert, [rendered, ""]))


def _drop_removed_scalars(
    parent: _Block,
    before_body: Any,
    after_body: Mapping[str, Any],
    edits: list[tuple[int, int, list[str]]],
) -> None:
    """Remove a scalar key that a schema step dropped from a model or role."""
    if not isinstance(before_body, dict) or not isinstance(after_body, dict):
        return
    for key, value in before_body.items():
        if key in after_body or isinstance(value, (dict, list)):
            continue
        child = _child(parent, str(key))
        if child is None or not child.value.strip():
            continue
        edits.append((child.line, child.line + 1, []))


def _sync_scalar(
    lines: list[str],
    parent: _Block,
    key: str,
    value: Any,
    field: str,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    child = _child(parent, key)
    if child is None:
        raise ConfigEditError(f"Cannot edit {field} in this file.")
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Missing or invalid {field}.")
    current = _scalar(child.value)
    if current == value:
        return
    edits.append((child.line, child.line + 1, [_with_value(lines[child.line], value)]))


def _sync_options(
    lines: list[str],
    parent: _Block,
    before_body: Any,
    after_body: Mapping[str, Any],
    key: str,
    unit: int,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    _sync_option_map(
        lines,
        parent,
        _options_dict(before_body, key),
        _options_dict(after_body, key),
        f"models.{key}.options",
        unit,
        edits,
    )


def _sync_role_options(
    lines: list[str],
    role_block: _Block,
    before_body: Any,
    after_body: Mapping[str, Any],
    key: str,
    unit: int,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    _sync_option_map(
        lines,
        role_block,
        _role_option_map(before_body if isinstance(before_body, dict) else {}),
        _role_option_map(after_body),
        f"roles.{key}.options",
        unit,
        edits,
    )


def _sync_option_map(
    lines: list[str],
    parent: _Block,
    before: Mapping[str, str],
    after: Mapping[str, str],
    field: str,
    unit: int,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    if dict(before) == dict(after):
        return
    options = _child(parent, "options")
    if options is not None and options.value.strip():
        raise ConfigEditError(f"Cannot edit {field} in this file.")
    if not after:
        if options is not None:
            edits.append((options.line, options.end, []))
        return
    nested = " " * ((options.indent if options is not None else parent.indent + unit) + (0 if options is not None else unit))
    if options is None:
        indent = " " * (parent.indent + unit)
        nested = " " * (parent.indent + unit * 2)
        body = [f"{indent}options:"]
        for name in sorted(after):
            body.append(f"{nested}{name}: {_yaml_scalar(after[name])}")
        edits.append((parent.end, parent.end, body))
        return
    for name in sorted(set(before) - set(after)):
        child = _child(options, name)
        if child is not None:
            edits.append((child.line, child.end, []))
    for name in sorted(after):
        child = _child(options, name)
        rendered = f"{' ' * (options.indent + unit)}{name}: {_yaml_scalar(after[name])}"
        if child is None:
            edits.append((options.end, options.end, [rendered]))
            continue
        current = _scalar(child.value)
        if current != after[name]:
            edits.append((child.line, child.line + 1, [_with_value(lines[child.line], after[name])]))


def _model_lines(key: str, body: Mapping[str, Any], indent: int, unit: int) -> list[str]:
    parent = " " * indent
    child = " " * (indent + unit)
    lines = [
        f"{parent}{key}:",
        f"{child}provider: {_yaml_scalar(_string(body.get('provider'), f'models.{key}.provider'))}",
        f"{child}model: {_yaml_scalar(_string(body.get('model'), f'models.{key}.model'))}",
    ]
    options = _options_dict(body, key)
    if options:
        lines.append(f"{child}options:")
        nested = " " * (indent + unit * 2)
        for option, value in options.items():
            lines.append(f"{nested}{option}: {_yaml_scalar(value)}")
    return lines


def _role_lines(key: str, body: Mapping[str, Any], indent: int, unit: int) -> list[str]:
    parent = " " * indent
    child = " " * (indent + unit)
    lines = [
        f"{parent}{key}:",
        f"{child}default_model: {_yaml_scalar(_string(body.get('default_model'), f'roles.{key}.default_model'))}",
    ]
    role_options = _role_option_map(body)
    if role_options:
        lines.append(f"{child}options:")
        nested = " " * (indent + unit * 2)
        for name, value in role_options.items():
            lines.append(f"{nested}{name}: {_yaml_scalar(value)}")
    return lines


def _parse_blocks(lines: list[str], unit: int) -> list[_Block]:
    return _parse_level(lines, 0, len(lines), 0, unit)


def _parse_level(
    lines: list[str], start: int, end: int, indent: int, unit: int
) -> list[_Block]:
    blocks: list[_Block] = []
    index = start
    while index < end:
        found = _next_content(lines, index, end)
        if found is None:
            break
        line_index, line_indent = found
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ConfigEditError(f"Invalid YAML indent at line {line_index + 1}.")
        key, value = _split_key(lines[line_index], line_index)
        child_end = line_index + 1
        while child_end < end:
            later = _next_content(lines, child_end, end)
            if later is None:
                child_end = end
                break
            later_index, later_indent = later
            if later_indent <= indent:
                child_end = later_index
                break
            child_end = later_index + 1
        children = (
            _parse_level(lines, line_index + 1, child_end, indent + unit, unit)
            if not value.strip()
            else []
        )
        blocks.append(_Block(key, indent, line_index, child_end, value, children))
        index = child_end
    return blocks


def _next_content(lines: list[str], start: int, end: int) -> Optional[tuple[int, int]]:
    for index in range(start, end):
        indent = _content_indent(lines[index])
        if indent is not None:
            return index, indent
    return None


def _content_indent(line: str) -> Optional[int]:
    code, _comment = _split_comment(line)
    if not code.strip():
        return None
    return len(code) - len(code.lstrip(" "))


def _split_key(line: str, index: int) -> tuple[str, str]:
    code, _comment = _split_comment(line)
    if ":" not in code:
        raise ConfigEditError(f"Invalid YAML mapping at line {index + 1}.")
    key, _, value = code.partition(":")
    key = key.strip()
    if not key:
        raise ConfigEditError(f"Invalid YAML key at line {index + 1}.")
    return key, value.strip()


def _split_comment(line: str) -> tuple[str, str]:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index].rstrip(), line[index:]
    return line.rstrip(), ""


def _with_value(line: str, value: str) -> str:
    code, comment = _split_comment(line)
    key, _, _value = code.partition(":")
    updated = f"{key}: {_yaml_scalar(value)}"
    if comment:
        return f"{updated} {comment}"
    return updated


def _section(blocks: list[_Block], key: str) -> Optional[_Block]:
    for block in blocks:
        if block.indent == 0 and block.key == key:
            return block
    return None


def _child(parent: _Block, key: str) -> Optional[_Block]:
    for child in parent.children:
        if child.key == key:
            return child
    return None


def _indent_unit(lines: list[str]) -> int:
    indents = [
        indent
        for line in lines
        if (indent := _content_indent(line)) is not None and indent > 0
    ]
    return min(indents) if indents else 2


def _stages_using(
    workflows: Sequence[WorkflowDocument],
    roles: Mapping[str, Any],
    model_name: str,
) -> list[StageRef]:
    found: list[StageRef] = []
    for document in workflows:
        for stage in document.stages.values():
            key = stage.model_name
            if key is None:
                role = roles.get(stage.role)
                if isinstance(role, dict):
                    key = role.get("default_model")
            if key == model_name:
                found.append(StageRef(document.id, stage.id))
    return found


def _role_stages(workflows: Sequence[WorkflowDocument], role: str) -> list[StageRef]:
    found: list[StageRef] = []
    for document in workflows:
        for stage in document.stages.values():
            if stage.role != role:
                continue
            found.append(
                StageRef(
                    workflow=document.id,
                    stage=stage.id,
                    model_name=stage.model_name,
                    options=stage.options,
                )
            )
    return found


def _shields(
    stage: Any,
    before_roles: Mapping[str, Any],
    after_roles: Mapping[str, Any],
    before_models: Mapping[str, Any],
    after_models: Mapping[str, Any],
    updated_models: Sequence[str],
) -> list[str]:
    before_binding = _binding(before_roles.get(stage.role))
    after_binding = _binding(after_roles.get(stage.role))
    ignores: list[str] = []
    if stage.model_name is not None and before_binding[0] != after_binding[0]:
        ignores.append("role-model")
    if stage.options and (
        before_binding[1] != after_binding[1]
        or _model_options_changed(stage, after_binding, before_models, after_models, updated_models)
    ):
        ignores.append("role-options")
    return ignores


def _model_options_changed(
    stage: Any,
    after_binding: tuple[Any, Any],
    before_models: Mapping[str, Any],
    after_models: Mapping[str, Any],
    updated_models: Sequence[str],
) -> bool:
    key = stage.model_name or after_binding[0]
    if not isinstance(key, str) or key not in updated_models:
        return False
    before_options = _options_dict(before_models.get(key), key)
    after_options = _options_dict(after_models.get(key), key)
    return before_options != after_options


def _orphans(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    workflows: Sequence[WorkflowDocument],
) -> tuple[str, ...]:
    removed = _references(before, workflows) - _references(after, workflows)
    models = _mapping_or_empty(after.get("models"))
    return tuple(key for key in models if key in removed)


def _references(
    config: Mapping[str, Any], workflows: Sequence[WorkflowDocument]
) -> set[str]:
    found: set[str] = set()
    for role in _mapping_or_empty(config.get("roles")).values():
        if isinstance(role, dict) and isinstance(role.get("default_model"), str):
            found.add(role["default_model"])
    for document in workflows:
        for stage in document.stages.values():
            if stage.model_name:
                found.add(stage.model_name)
    return found


def _resolved_role(config: Mapping[str, Any], role: str) -> Optional[tuple[str, str, tuple[tuple[str, str], ...]]]:
    roles = config.get("roles")
    if not isinstance(roles, dict) or role not in roles:
        return None
    try:
        target = load_model_target(config, _string(
            roles[role].get("default_model") if isinstance(roles[role], dict) else None,
            f"roles.{role}.default_model",
        ))
    except ConfigurationError:
        return None
    body = roles[role]
    merged = dict(target.options)
    if isinstance(body, dict):
        merged.update(_role_option_map(body))
    return target.provider, target.model, tuple(sorted(merged.items()))


def _resolved_stage(config: Mapping[str, Any], stage: Any) -> Optional[tuple[str, str, tuple[tuple[str, str], ...]]]:
    try:
        target = resolve_stage_spec(config, stage)
    except ConfigurationError:
        return None
    return target.provider, target.model, tuple(sorted(target.options.items()))


def _binding(role: Any) -> tuple[Any, tuple[tuple[str, str], ...]]:
    if not isinstance(role, dict):
        return (None, ())
    return role.get("default_model"), tuple(sorted(_role_option_map(role).items()))


def _signature(body: Any) -> tuple[Any, ...]:
    if not isinstance(body, dict):
        return (body,)
    options = body.get("options")
    if isinstance(options, dict):
        rendered = tuple(sorted((str(key), str(value)) for key, value in options.items()))
    else:
        rendered = ()
    return (body.get("provider"), body.get("model"), rendered)


def _apply_model_options(body: dict[str, Any], key: str, updates: Any) -> None:
    if not isinstance(updates, Mapping):
        raise ConfigEditError("Options must be a mapping.")
    options = body.get("options")
    if options is None:
        options = {}
    elif not isinstance(options, dict):
        raise ConfigurationError(f"Missing or invalid models.{key}.options.")
    for name, value in updates.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigEditError("Option name is required.")
        if value is None:
            options.pop(name, None)
            continue
        if not isinstance(value, str) or not value.strip():
            raise ConfigEditError(f"{name} is required.")
        options[name] = value.strip()
    if options:
        body["options"] = options
    else:
        body.pop("options", None)


def _apply_role_options(role: dict[str, Any], name: str, updates: Any) -> None:
    if not isinstance(updates, Mapping):
        raise ConfigEditError("Options must be a mapping.")
    options = role.get("options")
    if options is None:
        options = {}
    elif not isinstance(options, dict):
        raise ConfigurationError(f"Missing or invalid roles.{name}.options.")
    for key, value in updates.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigEditError("Option name is required.")
        if value is None:
            options.pop(key, None)
            continue
        if not isinstance(value, str) or not value.strip():
            raise ConfigEditError(f"{key} is required.")
        options[key] = value.strip()
    if options:
        role["options"] = options
    else:
        role.pop("options", None)


def _role_option_map(role: Mapping[str, Any]) -> dict[str, str]:
    options = role.get("options")
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ConfigurationError("Missing or invalid roles.<role>.options.")
    return {
        str(key): value.strip()
        for key, value in options.items()
        if isinstance(value, str) and value.strip()
    }


def _options_dict(body: Any, key: str) -> dict[str, str]:
    if not isinstance(body, dict):
        return {}
    options = body.get("options")
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ConfigurationError(f"Missing or invalid models.{key}.options.")
    return {
        str(option): value.strip()
        for option, value in options.items()
        if isinstance(value, str) and value.strip()
    }


def _copy(config: Mapping[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(dict(config))
    _models(copied)
    _roles(copied)
    return copied


def _models(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("models")
    if not isinstance(value, dict):
        raise ConfigurationError("Missing or invalid models.")
    return value


def _roles(config: Mapping[str, Any]) -> dict[str, Any]:
    value = config.get("roles")
    if not isinstance(value, dict):
        raise ConfigurationError("Missing or invalid roles.")
    return value


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _role_names(roles: Sequence[str]) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(role.strip() for role in roles if role and role.strip()))
    if not names:
        raise ConfigEditError("Pass a role.")
    for name in names:
        _require_name(name, "role")
    return names


def _require_name(value: str, label: str) -> None:
    if _NAME.fullmatch(value) is None:
        raise ConfigEditError(f"Invalid {label}: {value}.")


def _plain(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigEditError(f"Missing or invalid {field}.")
    return value.strip()


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Missing or invalid {field}.")
    return value.strip()


def _scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _yaml_scalar(value: str) -> str:
    if _PLAIN_SCALAR.fullmatch(value) and value not in {"true", "false", "null"}:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
