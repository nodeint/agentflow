from __future__ import annotations

from typing import Any


def parse_yaml_document(text: str) -> Any:
    entries: list[tuple[int, str, int]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        content = raw_line.split("#", maxsplit=1)[0].rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        entries.append((indent, content.strip(), line_number))
    if not entries:
        return {}
    value, index = _parse_node(entries, 0, entries[0][0])
    if index != len(entries):
        raise ValueError(f"Unexpected YAML content at line {entries[index][2]}.")
    return value


def _parse_node(entries: list[tuple[int, str, int]], index: int, indent: int) -> tuple[Any, int]:
    text = entries[index][1]
    if text == "-" or text.startswith("- "):
        return _parse_list(entries, index, indent)
    return _parse_mapping(entries, index, indent)


def _parse_mapping(
    entries: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while index < len(entries):
        item_indent, text, line = entries[index]
        if item_indent < indent:
            break
        if item_indent > indent:
            raise ValueError(f"Invalid YAML indent at line {line}.")
        if text == "-" or text.startswith("- "):
            break
        if ":" not in text:
            raise ValueError(f"Invalid YAML mapping at line {line}.")
        key, _, rest = text.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not key:
            raise ValueError(f"Invalid YAML key at line {line}.")
        index += 1
        if rest:
            mapping[key] = _parse_scalar(rest)
            continue
        if index < len(entries) and entries[index][0] > indent:
            mapping[key], index = _parse_node(entries, index, entries[index][0])
        else:
            mapping[key] = {}
    return mapping, index


def _parse_list(
    entries: list[tuple[int, str, int]], index: int, indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(entries):
        item_indent, text, line = entries[index]
        if item_indent < indent:
            break
        if item_indent > indent:
            raise ValueError(f"Invalid YAML indent at line {line}.")
        if text != "-" and not text.startswith("- "):
            break
        rest = "" if text == "-" else text[2:].strip()
        index += 1
        if not rest:
            if index < len(entries) and entries[index][0] > indent:
                value, index = _parse_node(entries, index, entries[index][0])
                items.append(value)
            else:
                items.append(None)
            continue
        if ":" in rest:
            key, _, raw_value = rest.partition(":")
            key = key.strip()
            raw_value = raw_value.strip()
            if not key:
                raise ValueError(f"Invalid YAML key at line {line}.")
            item: dict[str, Any] = {key: _parse_scalar(raw_value) if raw_value else {}}
            if index < len(entries) and entries[index][0] > indent:
                nested, index = _parse_mapping(entries, index, entries[index][0])
                overlap = set(nested) & set(item)
                if overlap:
                    duplicated = ", ".join(sorted(overlap))
                    raise ValueError(f"Duplicate YAML key {duplicated} at line {line}.")
                item.update(nested)
            items.append(item)
            continue
        if index < len(entries) and entries[index][0] > indent:
            raise ValueError(f"Unexpected nested YAML under scalar list item at line {line}.")
        items.append(_parse_scalar(rest))
    return items, index


def _parse_scalar(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        return [
            _parse_scalar(item.strip())
            for item in value[1:-1].split(",")
            if item.strip()
        ]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
        return value[1:-1]
    return value
