from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def read_status_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_execution_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def watch_snapshot(
    run_directory: Path, execution_id: Optional[str] = None
) -> dict[str, Any]:
    status = read_status_file(run_directory / "status.json")
    events_path = run_directory / "events.jsonl"
    try:
        cursor = events_path.stat().st_size
    except OSError:
        cursor = 0
    selected_id = execution_id or status.get("latest_execution_id")
    execution = None
    if isinstance(selected_id, str) and selected_id:
        execution = read_execution_file(
            run_directory / "executions" / selected_id / "execution.json"
        )
    outcome = None
    if isinstance(execution, dict) and isinstance(execution.get("outcome"), dict):
        outcome = execution["outcome"]
    outputs = status.get("outputs") if isinstance(status.get("outputs"), dict) else {}
    return {
        "run_id": status.get("run_id") or run_directory.name,
        "status": status.get("status") or "unknown",
        "workflow_id": status.get("workflow_id"),
        "task": status.get("task"),
        "latest_execution_id": status.get("latest_execution_id"),
        "latest_execution_status": status.get("latest_execution_status"),
        "execution_id": selected_id,
        "execution_status": execution.get("status") if isinstance(execution, dict) else None,
        "outcome": outcome,
        "updated_at": status.get("updated_at"),
        "outputs": outputs,
        "events_cursor": cursor,
    }


def list_run_rows(workspace: Path) -> list[tuple[str, str, str, str, str, str, str, str]]:
    runs_directory = workspace / ".agentflow" / "runs"
    if not runs_directory.exists():
        return []
    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    for run_directory in runs_directory.iterdir():
        status_path = run_directory / "status.json"
        if not run_directory.is_dir() or not status_path.is_file():
            continue
        status = read_status_file(status_path)
        if not status:
            continue
        outputs = status.get("outputs")
        output_label = (
            ",".join(outputs)
            if isinstance(outputs, dict) and outputs
            else "none"
        )
        rows.append(
            (
                str(status.get("updated_at", "")),
                run_directory.name,
                str(status.get("workflow_id", "legacy/unknown")),
                str(status.get("status", "unknown")),
                str(status.get("latest_execution_id", "not started")),
                str(status.get("latest_decision") or "--"),
                output_label,
                str(status.get("task", "")),
            )
        )
    return sorted(rows, reverse=True)
