from __future__ import annotations

STATUS_PROVIDER_EVENTS = frozenset(
    {
        "provider.stage_started",
        "provider.turn_completed",
        "provider.turn_failed",
        "provider.cancelled",
        "provider.error",
        "provider.warning",
        "provider.heartbeat",
    }
)
