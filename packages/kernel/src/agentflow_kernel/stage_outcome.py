from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence

OUTCOME_STATUSES = {"complete", "blocked"}


@dataclass(frozen=True)
class StageOutcome:
    status: str
    blocker: Optional[str] = None
    decision: Optional[str] = None

    def to_json(self) -> Dict[str, str]:
        outcome = {"status": self.status}
        if self.blocker:
            outcome["blocker"] = self.blocker
        if self.decision:
            outcome["decision"] = self.decision
        return outcome


def parse_stage_outcome(
    response: str,
    required: bool,
    decision_values: Sequence[str] = (),
) -> Optional[StageOutcome]:
    headers = _leading_headers(response)
    raw_status = headers.get("status")
    if raw_status is None:
        if required:
            raise ValueError(
                "Workflow stage response must start with `status: complete` or "
                "`status: blocked`."
            )
        return None

    status = raw_status.lower()
    if status not in OUTCOME_STATUSES:
        allowed = ", ".join(sorted(OUTCOME_STATUSES))
        raise ValueError(f"Invalid workflow stage status: {raw_status}. Allowed: {allowed}.")
    decision = _parse_decision(headers.get("decision"), status, decision_values)
    return StageOutcome(
        status=status,
        blocker=headers.get("blocker"),
        decision=decision,
    )


def _parse_decision(
    raw_decision: Optional[str],
    status: str,
    decision_values: Sequence[str],
) -> Optional[str]:
    allowed = tuple(value.lower() for value in decision_values if value)
    if status != "complete":
        return None
    if not allowed:
        return raw_decision.lower() if raw_decision else None
    if raw_decision is None or not raw_decision.strip():
        allowed_label = "|".join(allowed)
        raise ValueError(
            "Workflow stage response must include "
            f"`decision: {allowed_label}` when status is complete."
        )
    decision = raw_decision.strip().lower()
    if decision not in set(allowed):
        allowed_label = ", ".join(allowed)
        raise ValueError(
            f"Invalid workflow stage decision: {raw_decision}. Allowed: {allowed_label}."
        )
    return decision


def _leading_headers(response: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for line in response.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(":")
        if not separator or not key or not value.strip():
            break
        headers[key.strip().lower()] = value.strip()
    return headers
