from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .event_kinds import STATUS_PROVIDER_EVENTS
from .runtime import log_progress, utc_now
from .session_store import ExecutionContext, SessionStore

CONTROL_PLANE_WARNING_MARKERS = (
    "failed to refresh available models",
)


def provider_error_message(payload: Dict[str, Any]) -> Optional[str]:
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    error = payload.get("error")
    if isinstance(error, dict):
        nested = error.get("message")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def classify_provider_error(message: Optional[str]) -> Dict[str, str]:
    summary = message.strip() if isinstance(message, str) and message.strip() else ""
    if not summary:
        summary = "provider emitted an error"
    lowered = summary.lower()
    event = (
        "provider.warning"
        if any(marker in lowered for marker in CONTROL_PLANE_WARNING_MARKERS)
        else "provider.error"
    )
    if event not in STATUS_PROVIDER_EVENTS:
        event = "provider.error"
    return {"event": event, "summary": summary}


@dataclass(frozen=True)
class ProviderEvent:
    event: str
    provider: str
    run_id: str
    execution_id: str
    stage_id: str
    timestamp: str = field(default_factory=utc_now)
    summary: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {
            "event": self.event,
            "provider": self.provider,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "stage_id": self.stage_id,
            "timestamp": self.timestamp,
            **({"summary": self.summary} if self.summary else {}),
            **({"details": self.details} if self.details else {}),
        }


class ProviderEventReporter:
    """Publish provider lifecycle events to the terminal and workflow timeline."""

    def __init__(
        self,
        store: SessionStore,
        context: ExecutionContext,
        provider: str,
        model: str,
    ) -> None:
        self.store = store
        self.context = context
        self.provider = provider
        self.model = model

    def emit(
        self,
        event: str,
        summary: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        provider_event = ProviderEvent(
            event=event,
            provider=self.provider,
            run_id=self.context.run_id,
            execution_id=self.context.execution_id,
            stage_id=self.store.stage_id(self.context),
            summary=summary,
            details=details or {},
        )
        self.store.record_provider_event(self.context, provider_event.to_json())
        if event == "provider.stage_started":
            message = f"{provider_event.stage_id} · started · {summary or self.provider}"
        else:
            message = summary or event.removeprefix("provider.").replace("_", " ")
        log_progress(message)
