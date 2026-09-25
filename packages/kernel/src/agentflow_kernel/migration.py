"""Upgrade config and workflow documents from the version they record.

Adapters supply the model, role, and stage rewrite for each version step.
This module walks those steps and writes the resulting `schema_version`.
A missing `schema_version` is version 0.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Optional

from .base_adapter import BaseCLIAdapter
from .config import ConfigurationError
from .config_edit import ConfigEditError, _yaml_scalar
from .yaml_subset import parse_yaml_document

SCHEMA_VERSION = 1
SCHEMA_VERSION_FIELD = "schema_version"


def document_version(document: Mapping[str, Any]) -> int:
    """Return the schema version stored on a config or workflow document."""
    raw = document.get(SCHEMA_VERSION_FIELD)
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ConfigurationError(f"{SCHEMA_VERSION_FIELD} must be a non-negative integer.")
    if isinstance(raw, str):
        text = raw.strip()
        if not text.isdigit():
            raise ConfigurationError(f"{SCHEMA_VERSION_FIELD} must be a non-negative integer.")
        raw = int(text)
    if raw < 0:
        raise ConfigurationError(f"{SCHEMA_VERSION_FIELD} must be a non-negative integer.")
    return int(raw)


def migrate_config(
    config: dict[str, Any],
    adapters: Mapping[str, BaseCLIAdapter],
    *,
    current: int = SCHEMA_VERSION,
) -> tuple[str, ...]:
    """Upgrade `config` in place through each version until `current`."""
    version = _require_current(document_version(config), current, "config")
    if version == current:
        return ()
    before = copy.deepcopy(config)
    while version < current:
        _migrate_models(config, adapters, version)
        _migrate_roles(config, adapters, version)
        version += 1
    config[SCHEMA_VERSION_FIELD] = current
    return _document_notes(before, config, "models", "roles")


def migrate_workflow(
    workflow: dict[str, Any],
    config: Mapping[str, Any],
    adapters: Mapping[str, BaseCLIAdapter],
    *,
    current: int = SCHEMA_VERSION,
) -> tuple[str, ...]:
    """Upgrade one workflow document in place. Stage steps use `config` to find a provider."""
    version = _require_current(document_version(workflow), current, "workflow")
    if version == current:
        return ()
    before = copy.deepcopy(workflow)
    while version < current:
        _migrate_stages(workflow, config, adapters, version)
        version += 1
    workflow[SCHEMA_VERSION_FIELD] = current
    return _stage_notes(before, workflow)


def patch_workflow_text(original: str, updated: Mapping[str, Any]) -> str:
    """Write schema and stage-option changes into a workflow file."""
    if "\t" in original:
        raise ConfigEditError("Cannot edit a workflow that uses tabs.")
    current = parse_yaml_document(original)
    if not isinstance(current, dict):
        raise ConfigEditError("Workflow must be a mapping.")
    lines = original.splitlines()
    edits: list[tuple[int, int, list[str]]] = []
    _insert_schema_version(lines, updated, edits)
    _sync_changed_stages(lines, current, updated, edits)
    return _apply_edits(original, lines, edits)


def _require_current(version: int, current: int, label: str) -> int:
    if version > current:
        raise ConfigurationError(
            f"{label} {SCHEMA_VERSION_FIELD} {version} is newer than this Agentflow ({current})."
        )
    return version


def _migrate_models(
    config: dict[str, Any],
    adapters: Mapping[str, BaseCLIAdapter],
    version: int,
) -> None:
    models = config.get("models")
    if not isinstance(models, dict):
        return
    for name, body in models.items():
        if not isinstance(body, dict):
            continue
        provider = _provider_name(body.get("provider"))
        if provider is None:
            continue
        adapter = adapters.get(provider)
        if adapter is None:
            continue
        _apply(adapter.model_migrations(), body, version)


def _migrate_roles(
    config: dict[str, Any],
    adapters: Mapping[str, BaseCLIAdapter],
    version: int,
) -> None:
    roles = config.get("roles")
    if not isinstance(roles, dict):
        return
    for name, body in roles.items():
        if not isinstance(body, dict):
            continue
        provider = _role_provider(config, body)
        if provider is None:
            continue
        adapter = adapters.get(provider)
        if adapter is None:
            continue
        _apply(adapter.role_migrations(), body, version)


def _migrate_stages(
    workflow: dict[str, Any],
    config: Mapping[str, Any],
    adapters: Mapping[str, BaseCLIAdapter],
    version: int,
) -> None:
    stages = workflow.get("stages")
    if not isinstance(stages, list):
        return
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        provider = _stage_provider(config, stage)
        if provider is None:
            continue
        adapter = adapters.get(provider)
        if adapter is None:
            continue
        _apply(adapter.stage_migrations(), stage, version)


def _apply(migrations: Mapping[int, Any], body: dict[str, Any], version: int) -> None:
    step = migrations.get(version)
    if step is not None:
        step(body)


def _role_provider(config: Mapping[str, Any], role: Mapping[str, Any]) -> Optional[str]:
    model_name = role.get("default_model")
    if not isinstance(model_name, str):
        return None
    return _model_provider(config, model_name)


def _stage_provider(config: Mapping[str, Any], stage: Mapping[str, Any]) -> Optional[str]:
    model_name = stage.get("model")
    if isinstance(model_name, str) and model_name.strip():
        return _model_provider(config, model_name)
    role_name = stage.get("role")
    if not isinstance(role_name, str):
        return None
    roles = config.get("roles")
    if not isinstance(roles, dict):
        return None
    role = roles.get(role_name.strip())
    if not isinstance(role, dict):
        return None
    return _role_provider(config, role)


def _model_provider(config: Mapping[str, Any], model_name: str) -> Optional[str]:
    models = config.get("models")
    if not isinstance(models, dict):
        return None
    model = models.get(model_name.strip())
    if not isinstance(model, dict):
        return None
    return _provider_name(model.get("provider"))


def _provider_name(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _document_notes(
    before: Mapping[str, Any], after: Mapping[str, Any], models_key: str, roles_key: str
) -> tuple[str, ...]:
    notes = [f"{SCHEMA_VERSION_FIELD} -> {after.get(SCHEMA_VERSION_FIELD)}"]
    notes.extend(_named_notes(before.get(models_key), after.get(models_key), "models"))
    notes.extend(_named_notes(before.get(roles_key), after.get(roles_key), "roles"))
    return tuple(notes)


def _stage_notes(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    notes = [f"{SCHEMA_VERSION_FIELD} -> {after.get(SCHEMA_VERSION_FIELD)}"]
    before_stages = _stages_by_id(before.get("stages"))
    after_stages = _stages_by_id(after.get("stages"))
    for stage_id in before_stages:
        if stage_id not in after_stages:
            continue
        notes.extend(_body_notes(before_stages[stage_id], after_stages[stage_id], f"stages.{stage_id}"))
    return tuple(notes)


def _named_notes(before: Any, after: Any, label: str) -> list[str]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    notes: list[str] = []
    for name in before:
        if name in after and isinstance(before[name], dict) and isinstance(after[name], dict):
            notes.extend(_body_notes(before[name], after[name], f"{label}.{name}"))
    return notes


def _stages_by_id(raw: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return found
    for stage in raw:
        if not isinstance(stage, dict):
            continue
        stage_id = stage.get("id")
        if isinstance(stage_id, str) and stage_id.strip():
            found[stage_id.strip()] = stage
    return found


def _body_notes(before: Mapping[str, Any], after: Mapping[str, Any], label: str) -> list[str]:
    notes: list[str] = []
    before_options = before.get("options") if isinstance(before.get("options"), dict) else {}
    after_options = after.get("options") if isinstance(after.get("options"), dict) else {}
    removed = [key for key in before_options if key not in after_options]
    added = [key for key in after_options if key not in before_options]
    renamed_option = removed == ["thinking"] and len(added) == 1
    moved_scalar = (
        "thinking" in before
        and "thinking" not in after
        and not removed
        and len(added) == 1
    )
    if renamed_option:
        notes.append(f"{label}.options.thinking -> {added[0]}")
    elif moved_scalar:
        notes.append(f"{label}.thinking -> options.{added[0]}")
    else:
        notes.extend(f"{label}.options.{key} removed" for key in removed)
        notes.extend(f"{label}.options.{key} set" for key in added)
        if "thinking" in before and "thinking" not in after:
            notes.append(f"{label}.thinking removed")
    return notes


def _insert_schema_version(
    lines: list[str], updated: Mapping[str, Any], edits: list[tuple[int, int, list[str]]]
) -> None:
    value = updated.get(SCHEMA_VERSION_FIELD)
    if isinstance(value, bool) or not isinstance(value, int):
        return
    rendered = f"{SCHEMA_VERSION_FIELD}: {value}"
    for index, line in enumerate(lines):
        key, _rest, _comment = _mapping_key(line)
        if key == SCHEMA_VERSION_FIELD and _content_indent(line) == 0:
            current, comment = _split_comment(line)
            _key, _sep, raw = current.partition(":")
            if raw.strip() == str(value):
                return
            replacement = f"{SCHEMA_VERSION_FIELD}: {value}"
            if comment:
                replacement = f"{replacement} {comment}"
            edits.append((index, index + 1, [replacement]))
            return
    insert = next(
        (index for index, line in enumerate(lines) if _content_indent(line) is not None),
        len(lines),
    )
    edits.append((insert, insert, [rendered, ""]))


def _sync_changed_stages(
    lines: list[str],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    edits: list[tuple[int, int, list[str]]],
) -> None:
    before_stages = _stages_by_id(before.get("stages"))
    after_stages = _stages_by_id(after.get("stages"))
    spans = _stage_spans(lines)
    for stage_id, start, end in spans:
        if stage_id not in before_stages or stage_id not in after_stages:
            continue
        previous = before_stages[stage_id]
        updated = after_stages[stage_id]
        if previous == updated:
            continue
        _patch_stage(lines, start, end, previous, updated, edits)


def _patch_stage(
    lines: list[str],
    start: int,
    end: int,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    edits: list[tuple[int, int, list[str]]],
) -> None:
    info = [_line(line) for line in lines[start:end]]
    content = [(index, item) for index, item in enumerate(info) if item is not None]
    if not content:
        return
    first_indent = content[0][1][0]
    sibling_indent = _sibling_indent(content, first_indent)
    unit = sibling_indent - first_indent if sibling_indent > first_indent else 2
    options_at: Optional[int] = None
    option_indent: Optional[int] = None
    in_options = False
    option_lines: dict[str, int] = {}
    thinking_at: Optional[int] = None
    for index, (indent, key, rest, _comment) in content:
        if in_options and indent <= sibling_indent:
            in_options = False
        if in_options and key is not None and rest:
            if option_indent is None:
                option_indent = indent
            if indent == option_indent:
                option_lines[key] = start + index
            continue
        if indent != sibling_indent or key is None:
            continue
        if key == "options" and not rest:
            options_at = start + index
            in_options = True
            continue
        if key == "thinking" and rest:
            thinking_at = start + index
    before_options = before.get("options") if isinstance(before.get("options"), dict) else {}
    after_options = after.get("options") if isinstance(after.get("options"), dict) else {}
    if dict(before_options) != dict(after_options):
        _sync_stage_options(
            lines,
            after_options,
            before_options,
            option_lines,
            options_at,
            option_indent if option_indent is not None else sibling_indent + unit,
            sibling_indent,
            end,
            edits,
        )
    if "thinking" in before and "thinking" not in after and thinking_at is not None:
        edits.append((thinking_at, thinking_at + 1, []))


def _sync_stage_options(
    lines: list[str],
    after: Mapping[str, Any],
    before: Mapping[str, Any],
    option_lines: Mapping[str, int],
    options_at: Optional[int],
    option_indent: int,
    sibling_indent: int,
    stage_end: int,
    edits: list[tuple[int, int, list[str]]],
) -> None:
    after_text = {
        str(key): value.strip()
        for key, value in after.items()
        if isinstance(value, str) and value.strip()
    }
    before_keys = {str(key) for key in before}
    if not after_text:
        if options_at is not None:
            edits.append((options_at, _options_end(lines, options_at, sibling_indent, stage_end), []))
        return
    if options_at is None:
        indent = " " * sibling_indent
        nested = " " * option_indent
        block = [f"{indent}options:"]
        for name in sorted(after_text):
            block.append(f"{nested}{name}: {_yaml_scalar(after_text[name])}")
        edits.append((stage_end, stage_end, block))
        return
    for name in sorted(before_keys - set(after_text)):
        line_index = option_lines.get(name)
        if line_index is not None:
            edits.append((line_index, line_index + 1, []))
    for name in sorted(after_text):
        rendered = f"{' ' * option_indent}{name}: {_yaml_scalar(after_text[name])}"
        line_index = option_lines.get(name)
        if line_index is None:
            insert_at = _options_end(lines, options_at, sibling_indent, stage_end)
            edits.append((insert_at, insert_at, [rendered]))
            continue
        current, _comment = _split_comment(lines[line_index])
        _key, _sep, raw = current.partition(":")
        if _unquote(raw.strip()) != after_text[name]:
            code, comment = _split_comment(lines[line_index])
            key, _sep, _rest = code.partition(":")
            updated = f"{key}: {_yaml_scalar(after_text[name])}"
            if comment:
                updated = f"{updated} {comment}"
            edits.append((line_index, line_index + 1, [updated]))


def _options_end(lines: list[str], options_at: int, sibling_indent: int, stage_end: int) -> int:
    end = options_at + 1
    for index in range(options_at + 1, stage_end):
        item = _line(lines[index])
        if item is None:
            continue
        if item[0] <= sibling_indent:
            break
        end = index + 1
    return end


def _sibling_indent(content: list[tuple[int, tuple[int, Optional[str], str, str]]], first_indent: int) -> int:
    sibling: Optional[int] = None
    for _index, (indent, _key, _rest, _comment) in content[1:]:
        if indent <= first_indent:
            continue
        sibling = indent if sibling is None else min(sibling, indent)
    return first_indent if sibling is None else sibling


def _stage_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    in_stages = False
    list_indent: Optional[int] = None
    start: Optional[int] = None
    stage_id: Optional[str] = None
    for index, line in enumerate(lines):
        item = _line(line)
        if item is None:
            continue
        indent, key, rest, _comment = item
        if not in_stages:
            if indent == 0 and key == "stages" and not rest:
                in_stages = True
            continue
        if indent == 0:
            if start is not None and stage_id is not None:
                spans.append((stage_id, start, index))
            break
        text = _code(line)
        if list_indent is None and (text.startswith("- ") or text == "-"):
            list_indent = indent
        if list_indent is not None and indent == list_indent and (
            text.startswith("- ") or text == "-"
        ):
            if start is not None and stage_id is not None:
                spans.append((stage_id, start, index))
            start = index
            stage_id = _id_on_dash(text)
            continue
        if start is not None and stage_id is None and key == "id" and rest:
            stage_id = _unquote(rest)
    if start is not None and stage_id is not None:
        spans.append((stage_id, start, len(lines)))
    return spans


def _id_on_dash(text: str) -> Optional[str]:
    body = text[2:].strip() if text.startswith("- ") else ""
    if not body:
        return None
    key, _sep, rest = body.partition(":")
    if key.strip() != "id" or not rest.strip():
        return None
    return _unquote(rest.strip().split()[0])


def _line(line: str) -> Optional[tuple[int, Optional[str], str, str]]:
    indent = _content_indent(line)
    if indent is None:
        return None
    key, rest, comment = _mapping_key(line)
    return indent, key, rest, comment


def _mapping_key(line: str) -> tuple[Optional[str], str, str]:
    code, comment = _split_comment(line)
    text = code.strip()
    if text.startswith("- "):
        text = text[2:].strip()
    elif text == "-":
        return None, "", comment
    if ":" not in text:
        return None, "", comment
    key, _sep, rest = text.partition(":")
    key = key.strip()
    if not key:
        return None, "", comment
    return key, rest.strip(), comment


def _code(line: str) -> str:
    code, _comment = _split_comment(line)
    return code.strip()


def _content_indent(line: str) -> Optional[int]:
    code, _comment = _split_comment(line)
    if not code.strip():
        return None
    return len(code) - len(code.lstrip(" "))


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


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _apply_edits(original: str, lines: list[str], edits: list[tuple[int, int, list[str]]]) -> str:
    numbered = list(enumerate(edits))
    for _order, (start, end, replacement) in sorted(
        numbered, key=lambda item: (item[1][0], item[1][1], item[0]), reverse=True
    ):
        lines[start:end] = replacement
    text = "\n".join(lines)
    if original.endswith("\n") or original == "":
        text += "\n"
    return text
