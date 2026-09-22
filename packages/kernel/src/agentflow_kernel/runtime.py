from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Dict, List, Optional, Sequence

AGENTFLOW_DIRNAME = ".agentflow"
EXECUTIONS_DIRNAME = "executions"
SESSIONS_DIRNAME = "sessions"
SESSION_FILENAME = "session.json"
EXECUTION_SCHEMA_VERSION = 1


def add_artifact_context(
    prompt: str, artifact_directory: Path, artifact_name: Optional[str] = None
) -> str:
    if not artifact_name:
        return prompt
    return (
        f"{prompt}\n\n"
        "Agentflow artifact directory:\n"
        f"{artifact_directory}\n"
        f"Write the file artifact as `{artifact_name}` in that directory."
    )


def add_stage_outcome_contract(
    prompt: str, decision_values: Sequence[str] = ()
) -> str:
    lines = [
        "Workflow stage response contract:",
        "Start the final response with `status: complete` or `status: blocked`.",
        "When blocked, put `blocker: <concise reason>` on the next line.",
    ]
    allowed = tuple(value.lower() for value in decision_values if value)
    if allowed:
        allowed_label = "|".join(allowed)
        lines.append(
            "When complete, put "
            f"`decision: {allowed_label}` on the next line."
        )
        lines.append("Do not put the decision only in prose.")
    lines.append("Use a blank line before the report body.")
    return f"{prompt}\n\n" + "\n".join(lines)


def flatten_history(history: List[Dict[str, str]]) -> str:
    return "\n".join(f"{item['role']}: {item['content']}" for item in history)


def log(message: str) -> None:
    print(f"[agentflow] {message}", file=sys.stderr, flush=True)


def log_progress(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def safe_component(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    ).strip("-_")
    if not normalized:
        raise ValueError("Stage ID must contain at least one letter or number.")
    return normalized


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
