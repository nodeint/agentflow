from __future__ import annotations

from pathlib import Path
from typing import Optional

from agentflow_kernel.config import ConfigurationError

MANUAL_DIR = Path(__file__).resolve().parent
TOPICS = {
    "index": "index.md",
    "config": "config.md",
    "workflow": "workflow.md",
    "stage": "stage.md",
    "execute": "execute.md",
    "agent": "agent.md",
    "runs": "runs.md",
    "watch": "watch.md",
}


def topic_names() -> tuple[str, ...]:
    return tuple(name for name in TOPICS if name != "index")


def render_manual(topic: Optional[str] = None) -> str:
    name = topic or "index"
    filename = TOPICS.get(name)
    if filename is None:
        known = ", ".join(topic_names())
        raise ConfigurationError(f"Unknown manual topic {name!r}. Topics: {known}.")
    path = MANUAL_DIR / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Unable to read manual topic {name!r}: {exc}") from exc
    if not text.endswith("\n"):
        text += "\n"
    return text
