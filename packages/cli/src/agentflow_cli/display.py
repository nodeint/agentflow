from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

_STATUS_STYLES = {
    "completed": "green",
    "active": "cyan",
    "failed": "red",
    "blocked": "yellow",
    "cancelled": "dim",
}


def print_agent(result: dict[str, Any], *, role: str, provider: str, model: str) -> None:
    console = _stdout()
    outcome = str(result.get("outcome_status") or "failed")
    decision = str(result.get("outcome_decision") or "")
    if decision:
        outcome = f"{outcome} {decision}"
    rows = (
        ("role", role),
        ("model", f"{provider}/{model}"),
        ("outcome", outcome),
        ("run", str(result.get("run_id") or "")),
        ("execution", str(result.get("execution_id") or "")),
    )
    for label, value in rows:
        line = Text()
        line.append(f"{label}  ", style="dim")
        line.append(value)
        console.print(line)


def print_runs(
    rows: list[tuple[str, str, str, str, str, str, str, str]],
) -> None:
    console = _stdout()
    if not rows:
        console.print(Text("No runs.", style="dim"))
        return
    table = Table(header_style="bold")
    for title in (
        "Run",
        "Workflow",
        "Status",
        "Execution",
        "Decision",
        "Outputs",
        "Task",
    ):
        table.add_column(title)
    for row in rows:
        _, run_id, workflow_id, status, execution_id, decision, outputs, task = row
        table.add_row(
            Text(run_id),
            Text(workflow_id),
            _status_text(status),
            Text(execution_id),
            Text(decision),
            Text(outputs),
            Text(task),
        )
    console.print(table)


def print_watch_snapshot(snapshot: dict[str, Any]) -> None:
    table = Table(show_header=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("run_id", Text(str(snapshot.get("run_id") or "--")))
    table.add_row("status", _status_text(str(snapshot.get("status") or "unknown")))
    workflow_id = snapshot.get("workflow_id")
    if isinstance(workflow_id, str) and workflow_id:
        table.add_row("workflow", Text(workflow_id))
    task = snapshot.get("task")
    if isinstance(task, str) and task:
        table.add_row("task", Text(task))
    execution_id = snapshot.get("execution_id")
    execution_status = snapshot.get("execution_status") or snapshot.get(
        "latest_execution_status"
    )
    if isinstance(execution_id, str) and execution_id:
        value = execution_id
        if isinstance(execution_status, str) and execution_status:
            value = f"{execution_id} · {execution_status}"
        table.add_row("execution", Text(value))
    outcome = snapshot.get("outcome")
    if isinstance(outcome, dict) and isinstance(outcome.get("status"), str):
        table.add_row("outcome", _status_text(outcome["status"]))
        decision = outcome.get("decision")
        if isinstance(decision, str) and decision:
            table.add_row("decision", Text(decision))
    table.add_row("updated_at", Text(format_watch_timestamp(snapshot.get("updated_at"))))
    outputs = snapshot.get("outputs")
    if isinstance(outputs, dict) and outputs:
        table.add_row("outputs", Text(", ".join(outputs)))
    else:
        table.add_row("outputs", Text("none"))
    table.add_row("events_cursor", Text(str(snapshot.get("events_cursor", 0))))
    _stdout().print(table)


def print_watch_event(event: dict[str, Any]) -> None:
    event_name = event.get("event")
    stage_id = event.get("stage_id")
    if not isinstance(event_name, str) or not isinstance(stage_id, str):
        return
    line = Text()
    line.append(format_watch_timestamp(event.get("timestamp")), style="dim")
    line.append(" ")
    line.append(stage_id, style="bold")
    line.append(" ")
    line.append(event_name.removeprefix("provider.").replace("_", " "))
    summary = event.get("summary")
    if isinstance(summary, str) and summary:
        line.append(" · ")
        line.append(summary, style="dim")
    _stdout().print(line)


def print_notice(message: str) -> None:
    line = Text()
    line.append("[agentflow] ", style="bold cyan")
    line.append(message)
    _stderr().print(line)


def print_error(message: str) -> None:
    line = Text()
    line.append("agentflow: ", style="bold red")
    line.append(message)
    _stderr().print(line)


def format_watch_timestamp(timestamp: Any) -> str:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return "--"
    text = timestamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return timestamp.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(sep=" ", timespec="seconds")


def _status_text(status: str) -> Text:
    return Text(status, style=_STATUS_STYLES.get(status, ""))


def _stdout() -> Console:
    return _console(sys.stdout)


def _stderr() -> Console:
    return _console(sys.stderr)


def _console(file: Any) -> Console:
    isatty = getattr(file, "isatty", lambda: False)()
    if isatty:
        return Console(file=file, highlight=False)
    return Console(file=file, highlight=False, force_terminal=False, width=120)
