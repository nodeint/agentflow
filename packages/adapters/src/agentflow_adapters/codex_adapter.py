from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from agentflow_kernel.base_adapter import BaseCLIAdapter, CommandSpec
from agentflow_kernel.provider_events import classify_provider_error, provider_error_message


class CodexAdapter(BaseCLIAdapter):
    def build_command(
        self,
        model: str,
        workspace: str,
        prompt: str,
        agent_id: Optional[str],
        new_agent_id: Optional[str],
        prompt_file: Path,
        last_message_file: Path,
        thinking: Optional[str] = None,
    ) -> CommandSpec:
        del new_agent_id, prompt_file
        cmd = [
            "codex",
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
        if thinking:
            cmd.extend(["-c", f'model_reasoning_effort="{thinking}"'])
        if agent_id:
            cmd.extend(["resume", agent_id, "-"])
        else:
            # `--approve-for-me` already routes through the workspace-write sandbox.
            # Passing `--sandbox` together is rejected by current `codex exec`.
            cmd.extend(["--approve-for-me", "-"])
        return CommandSpec(argv=cmd, stdin=prompt)

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
