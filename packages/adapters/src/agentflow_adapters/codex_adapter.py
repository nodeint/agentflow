from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from agentflow_kernel.base_adapter import BaseCLIAdapter, CommandSpec, ModelCatalog, ProviderOption
from agentflow_kernel.provider_events import classify_provider_error, provider_error_message


class CodexAdapter(BaseCLIAdapter):
    command = "codex"
    display_name = "Codex"

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
        del new_agent_id, prompt_file
        self.validate_options(dict(options or {}))
        cmd = [
            self.command,
            "exec",
            "-C",
            workspace,
            "-m",
            model,
            "--color",
            "never",
            "--json",
            "--output-last-message",
            str(last_message_file),
        ]
        _append_options(cmd, options)
        if agent_id:
            cmd.extend(["resume", agent_id, "-"])
        else:
            # `--approve-for-me` already routes through the workspace-write sandbox.
            # Passing `--sandbox` together is rejected by current `codex exec`.
            cmd.extend(["--approve-for-me", "-"])
        return CommandSpec(argv=cmd, stdin=prompt)

    def model_catalog_command(self) -> list[str]:
        return [self.command, "debug", "models"]

    def parse_model_catalog(self, stdout: str) -> ModelCatalog:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("codex model catalog is not JSON.") from exc
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise ValueError("codex model catalog has no models.")
        ids: list[str] = []
        option_values: list[tuple[str, str, tuple[str, ...]]] = []
        for item in models:
            if not isinstance(item, dict) or item.get("visibility") not in {None, "list"}:
                continue
            slug = item.get("slug")
            if isinstance(slug, str) and slug and slug not in ids:
                ids.append(slug)
                option_values.append(
                    (slug, "model_reasoning_effort", _reasoning_levels(item.get("supported_reasoning_levels")))
                )
        if not ids:
            raise ValueError("codex model catalog is empty.")
        return ModelCatalog(tuple(ids), option_values=tuple(option_values))

    def provider_options(self) -> tuple[ProviderOption, ...]:
        return (
            ProviderOption(
                "model_reasoning_effort",
                prompt=True,
                allow_default=True,
                overridable=True,
            ),
        )

    def option_values_command(self, name: str, model: str) -> list[str] | None:
        del model
        if name != "model_reasoning_effort":
            return None
        return self.model_catalog_command()

    def parse_option_values(self, name: str, text: str, *, model: str) -> tuple[str, ...]:
        if name != "model_reasoning_effort":
            raise ValueError(f"codex does not list values for {name}.")
        values = self.parse_model_catalog(text).values_for(model, name)
        if values is None:
            raise ValueError(f"codex did not list {name} values for {model}.")
        return values

    def validate_options(self, options: Mapping[str, str]) -> None:
        for key, value in options.items():
            if _OPTION_KEY.fullmatch(key) is None:
                raise ValueError(f"{key} is not a codex option.")
            if not value.strip():
                raise ValueError(f"{key} is empty.")

    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        agent_id = None
        last_text = None
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    agent_id = thread_id
            elif event_type == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        last_text = text
            elif event_type == "turn.failed":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                if isinstance(message, str) and message:
                    last_text = message
            elif event_type == "error":
                message = event.get("message")
                if isinstance(message, str) and message:
                    last_text = message
        if last_message_file.exists():
            file_text = last_message_file.read_text(encoding="utf-8").strip()
            if file_text:
                last_text = file_text
        return (last_text if last_text is not None else stdout.strip()), agent_id

    def parse_progress_event(self, line: str) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return None
        if event_type in {"item.started", "item.completed"}:
            item = payload.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            action = "started" if event_type.endswith("started") else "completed"
            summary = _item_summary(item, item_type, action)
            return {
                "event": "provider.item_started" if event_type.endswith("started") else "provider.item_completed",
                "summary": summary,
                "details": {"item_type": item_type} if isinstance(item_type, str) else {},
            }
        if event_type == "turn.failed":
            failed = classify_provider_error(provider_error_message(payload))
            if failed["event"] == "provider.warning":
                return failed
            return {"event": "provider.turn_failed", "summary": failed["summary"]}
        if event_type == "error":
            return classify_provider_error(provider_error_message(payload))
        return None


_OPTION_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")


def _reasoning_levels(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for level in raw:
        if isinstance(level, dict):
            effort = level.get("effort")
        elif isinstance(level, str):
            effort = level
        else:
            continue
        if isinstance(effort, str) and effort and effort not in values:
            values.append(effort)
    return tuple(values)


def _append_options(cmd: list[str], options: Optional[Mapping[str, str]]) -> None:
    for key, value in (options or {}).items():
        config_key = _CODEX_CONFIG_KEYS.get(key, key)
        cmd.extend(["-c", f"{config_key}={_toml_literal(value)}"])


# Semantic option names that do not match a Codex config key.
_CODEX_CONFIG_KEYS: dict[str, str] = {}
_TOML_BARE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?|true|false")


def _toml_literal(value: str) -> str:
    if _TOML_BARE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _item_summary(item: Any, item_type: Any, action: str) -> str:
    if isinstance(item, dict) and item_type == "command_execution":
        command = item.get("command")
        if isinstance(command, str) and command:
            return f"command {action}: {_compact(command)}"
    if isinstance(item, dict) and item_type == "agent_message":
        text = item.get("text")
        if isinstance(text, str) and text:
            return f"agent: {_compact(text)}"
    return f"{item_type or 'provider item'} {action}"


def _compact(value: str, limit: int = 160) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
