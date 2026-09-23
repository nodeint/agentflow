from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from agentflow_kernel.base_adapter import BaseCLIAdapter, CommandSpec, ModelCatalog
from agentflow_kernel.provider_events import classify_provider_error, provider_error_message


class GrokAdapter(BaseCLIAdapter):
    command = "grok"
    display_name = "Grok"

    def __init__(self) -> None:
        self._tool_calls: Dict[str, Dict[str, Any]] = {}

    def build_command(
        self,
        model: str,
        workspace: str,
        prompt: str,
        agent_id: Optional[str],
        new_agent_id: Optional[str],
        prompt_file: Path,
        last_message_file: Path,
        options: Optional[Mapping[str, str]] = None,
    ) -> CommandSpec:
        del prompt, last_message_file
        self._tool_calls = {}
        self.validate_options(dict(options or {}))
        cmd = [
            self.command,
            "--prompt-file",
            str(prompt_file),
            "-m",
            model,
            "--cwd",
            workspace,
            "--output-format",
            "streaming-json",
            "--always-approve",
            "--verbatim",
        ]
        _append_options(cmd, options)
        if agent_id:
            cmd.extend(["--resume", agent_id])
        elif new_agent_id:
            cmd.extend(["--session-id", new_agent_id])
        return CommandSpec(argv=cmd)

    def model_catalog_command(self) -> list[str]:
        return [self.command, "models"]

    def parse_model_catalog(self, stdout: str) -> ModelCatalog:
        ids: list[str] = []
        default_id = None
        in_list = False
        for raw in stdout.splitlines():
            line = raw.strip()
            if line.lower().startswith("default model:"):
                default_id = line.split(":", 1)[1].strip()
                continue
            if line.lower().startswith("available models"):
                in_list = True
                continue
            if not in_list or not line.startswith(("*", "-")):
                continue
            name = line[1:].strip().split()[0]
            if name and name not in ids:
                ids.append(name)
            if "(default)" in line:
                default_id = name
        if not ids:
            raise ValueError("grok model catalog is empty.")
        if default_id not in ids:
            default_id = None
        return ModelCatalog(tuple(ids), default_id)

    def validate_options(self, options: Mapping[str, str]) -> None:
        for key, value in options.items():
            check = _GROK_OPTION_CHECKS.get(key)
            if check is None:
                raise ValueError(f"{key} is not a grok option.")
            if not value.strip():
                raise ValueError(f"{key} is empty.")
            check(value)

    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        del last_message_file
        segments: list[list[str]] = [[]]
        agent_id = None
        error_message = None
        saw_event = False
        for raw_line in stdout.splitlines():
            payload = _parse_json_object(raw_line)
            if payload is None:
                continue
            saw_event = True
            event_type = payload.get("type")
            if event_type == "tool_call" or (
                event_type == "tool_call_update"
                and payload.get("status") in {"completed", "failed"}
            ):
                if segments[-1]:
                    segments.append([])
            elif event_type == "text":
                data = payload.get("data")
                if isinstance(data, str) and data:
                    segments[-1].append(data)
            elif event_type == "end":
                session_id = payload.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    agent_id = session_id
            elif event_type == "error":
                message = payload.get("message")
                if isinstance(message, str) and message:
                    error_message = message
        text = _text_with_status_header(segments)
        if text:
            return text, agent_id
        if error_message:
            return error_message, agent_id
        if saw_event:
            return "", agent_id
        return stdout.strip(), None

    def parse_progress_event(self, line: str) -> Optional[Dict[str, Any]]:
        payload = _parse_json_object(line)
        if payload is None:
            return None
        event_type = payload.get("type")
        if event_type == "tool_call":
            self._remember_tool_call(payload)
            return {
                "event": "provider.item_started",
                "summary": _tool_summary(payload, "started"),
                "details": _tool_details(payload),
            }
        if event_type == "tool_call_update":
            status = payload.get("status")
            if status not in {"completed", "failed"}:
                return None
            merged = {**self._remembered_tool_call(payload), **payload}
            action = "failed" if status == "failed" else "completed"
            return {
                "event": "provider.item_completed",
                "summary": _tool_summary(merged, action),
                "details": _tool_details(merged),
            }
        if event_type == "error":
            return classify_provider_error(provider_error_message(payload))
        return None

    def _remember_tool_call(self, payload: Dict[str, Any]) -> None:
        tool_call_id = payload.get("toolCallId")
        if isinstance(tool_call_id, str) and tool_call_id:
            self._tool_calls[tool_call_id] = payload

    def _remembered_tool_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tool_call_id = payload.get("toolCallId")
        if isinstance(tool_call_id, str):
            return self._tool_calls.get(tool_call_id, {})
        return {}


_GROK_THINKING = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
GrokAdapter.thinking_values = _GROK_THINKING
_GROK_PERMISSION_MODES = (
    "default",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
    "plan",
)


def _one_of(value: str, allowed: tuple[str, ...], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}.")


def _grok_thinking(value: str) -> None:
    _one_of(value, _GROK_THINKING, "thinking")


def _grok_max_turns(value: str) -> None:
    if not value.isdigit() or int(value) < 1:
        raise ValueError("max_turns must be an integer of at least 1.")


def _grok_permission_mode(value: str) -> None:
    _one_of(value, _GROK_PERMISSION_MODES, "permission_mode")


def _grok_text(value: str) -> None:
    del value


_GROK_OPTION_CHECKS = {
    "thinking": _grok_thinking,
    "max_turns": _grok_max_turns,
    "tools": _grok_text,
    "disallowed_tools": _grok_text,
    "permission_mode": _grok_permission_mode,
    "rules": _grok_text,
    "allow": _grok_text,
    "deny": _grok_text,
    "sandbox": _grok_text,
}


def _append_options(cmd: list[str], options: Optional[Mapping[str, str]]) -> None:
    for key, value in (options or {}).items():
        flag = _GROK_FLAGS.get(key, key.replace("_", "-"))
        cmd.extend([f"--{flag}", value])


# Semantic option names that do not match the grok long flag.
_GROK_FLAGS = {"thinking": "reasoning-effort"}


def _text_with_status_header(segments: list[list[str]]) -> str:
    last_nonempty = ""
    for segment in reversed(segments):
        text = "".join(segment).strip()
        if not text:
            continue
        if not last_nonempty:
            last_nonempty = text
        trimmed = _trim_to_status_header(text)
        if _STATUS_HEADER.search(trimmed) or _GLUED_STATUS_HEADER.search(trimmed):
            return trimmed
    if last_nonempty:
        return _trim_to_status_header(last_nonempty)
    return ""


_STATUS_HEADER = re.compile(r"(?im)(?:^|\n)[ \t]*status:[ \t]*(complete|blocked)\b")
_GLUED_STATUS_HEADER = re.compile(r"(?i)status:[ \t]*(complete|blocked)\b")


def _trim_to_status_header(text: str) -> str:
    match = _STATUS_HEADER.search(text)
    if match:
        return text[match.start():].lstrip()
    match = _GLUED_STATUS_HEADER.search(text)
    if match:
        return text[match.start():].strip()
    return text


def _parse_json_object(line: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _tool_summary(payload: Dict[str, Any], action: str) -> str:
    raw_input = payload.get("rawInput")
    command = _input_string(raw_input, "command", "cmd")
    if command:
        return f"command {action}: {_compact(command)}"
    path = _input_string(raw_input, "path", "target_file")
    tool_name = _tool_name(payload)
    if path:
        return f"{tool_name} {action}: {_compact(path)}"
    return f"{tool_name} {action}"


def _tool_details(payload: Dict[str, Any]) -> Dict[str, str]:
    tool_name = payload.get("toolName")
    if isinstance(tool_name, str) and tool_name:
        return {"item_type": tool_name}
    return {}


def _tool_name(payload: Dict[str, Any]) -> str:
    for key in ("toolName", "title", "kind"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return "tool"


def _input_string(raw_input: Any, *keys: str) -> Optional[str]:
    if not isinstance(raw_input, dict):
        return None
    for key in keys:
        value = raw_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _compact(value: str, limit: int = 160) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
