from __future__ import annotations

import json
import signal
import time
from pathlib import Path
from typing import Annotated, Any, Dict, Optional, TextIO

import typer

from agentflow_kernel.command_executor import CancellationRequested, raise_cancellation
from agentflow_kernel.config import ConfigurationError
from agentflow_kernel.query import read_execution_file, watch_snapshot
from agentflow_kernel.runtime import log
from agentflow_kernel.session_store import SessionStore

from ..display import print_notice, print_watch_event, print_watch_snapshot
from ..workspace import find_workspace, invocation_cwd

_TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "cancelled"}

watch_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    subcommand_metavar="",
    rich_markup_mode="rich",
)


@watch_app.callback(invoke_without_command=True)
def watch_command(
    run: Annotated[str, typer.Argument(metavar="RUN", help="Run to follow.")],
    execution: Annotated[
        Optional[str],
        typer.Option(
            "--execution",
            metavar="ID",
            help=(
                "Limit followed events and --until to this execution. "
                "With --once, show this execution in the snapshot."
            ),
            show_default=False,
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="With --once, print one JSON snapshot. While following, print JSON lines.",
        ),
    ] = False,
    cursor: Annotated[
        Optional[int],
        typer.Option(
            "--cursor",
            metavar="BYTE",
            help="Replay events.jsonl from this byte offset. 0 replays the whole log.",
            show_default=False,
        ),
    ] = None,
    once: Annotated[
        bool,
        typer.Option(
            "--once",
            help="Print the snapshot and events cursor, then exit. Cannot be combined with --cursor.",
        ),
    ] = False,
    until: Annotated[
        bool,
        typer.Option(
            "--until",
            help="Exit when --execution reaches completed, failed, or cancelled. Requires --execution.",
        ),
    ] = False,
) -> None:
    """Follow events for a run.

    With no --cursor, print the snapshot, then follow events appended after it.
    --cursor replays events.jsonl from that byte. 0 replays the whole log.
    --once prints the snapshot and events cursor, then exits.
    It cannot be combined with --cursor or --until.
    --until exits when --execution is completed, failed, or cancelled.
    --json with --once prints one snapshot object.
    While following, --json prints one JSON object per event.
    """
    raise typer.Exit(
        watch_run(
            find_workspace(invocation_cwd()),
            run,
            once=once,
            execution_id=execution,
            until_terminal=until,
            json_lines=as_json,
            cursor=cursor,
        )
    )


def watch_run(
    workspace: Path,
    run_id: str,
    *,
    once: bool = False,
    execution_id: Optional[str] = None,
    until_terminal: bool = False,
    json_lines: bool = False,
    cursor: Optional[int] = None,
) -> int:
    if once and until_terminal:
        raise ConfigurationError("--once cannot be combined with --until.")
    if once and cursor is not None:
        raise ConfigurationError("--once cannot be combined with --cursor.")
    if until_terminal and not execution_id:
        raise ConfigurationError("--until requires --execution.")
    if cursor is not None and cursor < 0:
        raise ConfigurationError("--cursor must be >= 0.")
    run_directory = workspace / ".agentflow" / "runs" / run_id
    events_path = run_directory / "events.jsonl"
    if not events_path.is_file():
        raise ConfigurationError(f"No events found for run: {run_id}")
    SessionStore(workspace).reap_orphaned_executions(run_id, on_live_owner="ignore")
    execution_path: Optional[Path] = None
    if execution_id is not None:
        execution_path = run_directory / "executions" / execution_id / "execution.json"
        if not execution_path.is_file():
            raise ConfigurationError(f"No execution found: {execution_id}")
    if once:
        _print_watch_snapshot(run_directory, execution_id, json_lines)
        return 0
    follow_cursor = cursor
    if follow_cursor is None:
        snapshot = _print_watch_snapshot(run_directory, execution_id, json_lines)
        follow_cursor = int(snapshot.get("events_cursor") or 0)
    print_notice(f"Watching run {run_id}.")
    previous_handlers = {
        signal.SIGINT: signal.signal(signal.SIGINT, raise_cancellation),
        signal.SIGTERM: signal.signal(signal.SIGTERM, raise_cancellation),
    }
    try:
        with events_path.open("r", encoding="utf-8") as events_file:
            events_file.seek(follow_cursor)
            pending = ""
            while True:
                lines, pending = _read_complete_event_lines(events_file, pending)
                for line in lines:
                    _emit_watch_event(
                        line,
                        execution_id=execution_id,
                        json_lines=json_lines,
                    )
                if lines:
                    continue
                if until_terminal:
                    assert execution_path is not None
                    record = read_execution_file(execution_path)
                    if (
                        record is not None
                        and record.get("status") in _TERMINAL_EXECUTION_STATUSES
                    ):
                        drain_lines, pending = _read_complete_event_lines(
                            events_file, pending
                        )
                        for line in drain_lines:
                            _emit_watch_event(
                                line,
                                execution_id=execution_id,
                                json_lines=json_lines,
                            )
                        return _until_terminal_exit_code(record)
                time.sleep(0.25)
    except CancellationRequested as exc:
        log(str(exc))
        return 130
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def _read_complete_event_lines(
    events_file: TextIO, pending: str
) -> tuple[list[str], str]:
    try:
        events_file.seek(events_file.tell())
    except OSError:
        pass
    chunk = events_file.read()
    if not chunk:
        return [], pending
    pending += chunk
    pieces = pending.split("\n")
    pending = pieces.pop()
    return pieces, pending


def _until_terminal_exit_code(record: Dict[str, Any]) -> int:
    status = record.get("status")
    if status == "completed":
        outcome = record.get("outcome")
        if isinstance(outcome, dict) and outcome.get("status") == "complete":
            return 0
        return 1
    if status in {"failed", "cancelled"}:
        return 1
    return 1


def _emit_watch_event(
    line: str, *, execution_id: Optional[str], json_lines: bool
) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    if execution_id is not None and event.get("execution_id") != execution_id:
        return
    if json_lines:
        print(json.dumps(event, ensure_ascii=False), flush=True)
        return
    print_watch_event(event)


def _print_watch_snapshot(
    run_directory: Path, execution_id: Optional[str], json_lines: bool
) -> dict[str, Any]:
    snapshot = watch_snapshot(run_directory, execution_id=execution_id)
    if json_lines:
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
    else:
        print_watch_snapshot(snapshot)
    return snapshot
