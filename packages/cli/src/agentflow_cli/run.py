#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from .coordinator import build_coordinator_command
from agentflow_kernel.config import (
    ConfigurationError,
    load_yaml_mapping,
    resolve_coordinator,
    resolve_role_target,
)
from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.command_executor import CancellationRequested, raise_cancellation
from agentflow_kernel.execute import ExecuteRequest, execute_named_workflow
from agentflow_kernel.query import list_run_rows, read_execution_file, watch_snapshot
from agentflow_kernel.run_records import create_agent_run, require_agent_run
from agentflow_kernel.runtime import log
from agentflow_kernel.session_store import SessionStore
from .manual import render_help, topic_names


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentflow coordinator and workflow runner.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Open an interactive coordinator session.")
    subparsers.add_parser("runs", help="List workflow runs and their latest status.")
    execute_parser = subparsers.add_parser(
        "execute", help="Run a named workflow, or one eligible stage."
    )
    execute_parser.add_argument("--workflow-id")
    execute_parser.add_argument("--task")
    execute_parser.add_argument("--run-id")
    execute_parser.add_argument(
        "--stage-id",
        help="Dispatch this eligible stage once, then stop.",
    )
    execute_parser.add_argument(
        "--prior-run-id",
        help="Completed required workflow run; required when the workflow declares requires.",
    )
    execute_parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum failed executions per stage id on the run. Default: 3.",
    )
    agent_parser = subparsers.add_parser(
        "agent", help="Dispatch a specialist role without a workflow file."
    )
    agent_parser.add_argument(
        "--role",
        required=True,
        help="Role key from .agentflow/config.yaml roles.<key>.",
    )
    agent_parser.add_argument(
        "--task",
        help="Label stored on a new run; default is the role key.",
    )
    agent_parser.add_argument("--run-id")
    agent_parser.add_argument(
        "--provider",
        help="Override the provider resolved from the role and config.",
    )
    agent_parser.add_argument(
        "--model",
        help="Override the native model resolved from the role and config.",
    )
    agent_parser.add_argument(
        "--thinking",
        help="Override the thinking level resolved from the role and config.",
    )
    agent_parser.add_argument(
        "--runner-session-id",
        "--session-id",
        dest="runner_session_id",
        help="Existing runner session ID; --session-id is a legacy alias.",
    )
    agent_prompt = agent_parser.add_mutually_exclusive_group(required=True)
    agent_prompt.add_argument("--prompt")
    agent_prompt.add_argument("--prompt-file")
    agent_prompt.add_argument(
        "--prompt-stdin",
        action="store_true",
        help="Read the prompt from stdin.",
    )
    watch_parser = subparsers.add_parser("watch", help="Follow run events.")
    watch_parser.add_argument("run_id", help="Run ID to observe.")
    watch_parser.add_argument(
        "--execution-id",
        help="Filter events to one execution.",
    )
    watch_parser.add_argument(
        "--json",
        dest="json_lines",
        action="store_true",
        help="Print a snapshot object with --once, or events as JSONL when following.",
    )
    watch_parser.add_argument(
        "--cursor",
        type=int,
        metavar="BYTE",
        help="Follow events after this byte offset from a prior --once snapshot.",
    )
    watch_mode = watch_parser.add_mutually_exclusive_group()
    watch_mode.add_argument(
        "--once",
        action="store_true",
        help="Print the current run snapshot and events cursor, then exit.",
    )
    watch_mode.add_argument(
        "--until-terminal",
        action="store_true",
        help="Exit after the selected execution becomes terminal. Requires --execution-id.",
    )
    help_parser = subparsers.add_parser("help", help="Print Agentflow specification topics.")
    help_parser.add_argument(
        "topic",
        nargs="?",
        help=f"One of: {', '.join(topic_names())}.",
    )
    args = parser.parse_args(argv)
    if args.command == "watch" and args.until_terminal and not args.execution_id:
        watch_parser.error("--until-terminal requires --execution-id")
    if args.command == "watch" and args.cursor is not None and args.cursor < 0:
        watch_parser.error("--cursor must be >= 0")
    if args.command == "watch" and args.once and args.cursor is not None:
        watch_parser.error("--once cannot be combined with --cursor")
    return args


def find_workspace(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".agentflow/config.yaml").is_file():
            return candidate
    raise ConfigurationError("Could not find .agentflow/config.yaml in this directory or its parents.")


def resolve_prompt(args: argparse.Namespace) -> str:
    if getattr(args, "prompt_stdin", False):
        prompt = sys.stdin.read()
        if prompt == "":
            raise ValueError("Prompt from stdin is empty.")
        return prompt
    if args.prompt_file is None:
        return args.prompt
    try:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read prompt file: {args.prompt_file} ({exc})") from exc


def main(argv: Optional[list[str]] = None, cwd: Optional[Path] = None) -> int:
    args = parse_args(argv)
    if args.command == "help":
        print(render_help(args.topic), end="")
        return 0
    workspace = find_workspace((cwd or Path.cwd()).resolve())
    if args.command == "watch":
        return watch_run(
            workspace,
            args.run_id,
            once=args.once,
            execution_id=args.execution_id,
            until_terminal=args.until_terminal,
            json_lines=args.json_lines,
            cursor=args.cursor,
        )
    if args.command == "runs":
        return list_runs(workspace)
    if args.command == "execute":
        return execute_workflow(workspace, args)
    if args.command == "agent":
        return dispatch_agent(workspace, args)
    config = load_yaml_mapping(workspace / ".agentflow/config.yaml")
    coordinator = resolve_coordinator(config)
    command = build_coordinator_command(workspace, coordinator)
    print(
        f"[agentflow] Opening coordinator with {coordinator.provider}/{coordinator.model} "
        f"(thinking: {coordinator.thinking}).",
        file=sys.stderr,
    )
    os.execvp(command[0], command)
    return 127


_TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "cancelled"}


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
        raise ConfigurationError("--once cannot be combined with --until-terminal.")
    if once and cursor is not None:
        raise ConfigurationError("--once cannot be combined with --cursor.")
    if until_terminal and not execution_id:
        raise ConfigurationError("--until-terminal requires --execution-id.")
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
        snapshot = watch_snapshot(run_directory, execution_id=execution_id)
        if json_lines:
            print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        else:
            _render_watch_snapshot(snapshot)
        return 0
    print(f"[agentflow] Watching run {run_id}.", file=sys.stderr, flush=True)
    previous_handlers = {
        signal.SIGINT: signal.signal(signal.SIGINT, raise_cancellation),
        signal.SIGTERM: signal.signal(signal.SIGTERM, raise_cancellation),
    }
    try:
        with events_path.open("r", encoding="utf-8") as events_file:
            if cursor is not None:
                events_file.seek(cursor)
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
    _render_event(event)


def _render_watch_snapshot(snapshot: Dict[str, Any]) -> None:
    print(f"run_id: {snapshot.get('run_id') or '--'}", flush=True)
    print(f"status: {snapshot.get('status') or 'unknown'}", flush=True)
    workflow_id = snapshot.get("workflow_id")
    if isinstance(workflow_id, str) and workflow_id:
        print(f"workflow: {workflow_id}", flush=True)
    task = snapshot.get("task")
    if isinstance(task, str) and task:
        print(f"task: {task}", flush=True)
    execution_id = snapshot.get("execution_id")
    execution_status = snapshot.get("execution_status") or snapshot.get(
        "latest_execution_status"
    )
    if isinstance(execution_id, str) and execution_id:
        suffix = (
            f" · {execution_status}"
            if isinstance(execution_status, str) and execution_status
            else ""
        )
        print(f"execution: {execution_id}{suffix}", flush=True)
    outcome = snapshot.get("outcome")
    if isinstance(outcome, dict) and isinstance(outcome.get("status"), str):
        print(f"outcome: {outcome['status']}", flush=True)
        decision = outcome.get("decision")
        if isinstance(decision, str) and decision:
            print(f"decision: {decision}", flush=True)
    print(f"updated_at: {_format_watch_timestamp(snapshot.get('updated_at'))}", flush=True)
    outputs = snapshot.get("outputs")
    if isinstance(outputs, dict) and outputs:
        print(f"outputs: {', '.join(outputs)}", flush=True)
    else:
        print("outputs: none", flush=True)
    print(f"events_cursor: {snapshot.get('events_cursor', 0)}", flush=True)


def _format_watch_timestamp(timestamp: Any) -> str:
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


def _render_event(event: Dict[str, Any]) -> None:
    event_name = event.get("event")
    stage_id = event.get("stage_id")
    timestamp = event.get("timestamp")
    if not isinstance(event_name, str) or not isinstance(stage_id, str):
        return
    time_label = _format_watch_timestamp(timestamp)
    summary = event.get("summary")
    suffix = f" · {summary}" if isinstance(summary, str) and summary else ""
    print(
        f"[{time_label}] {stage_id} · {event_name.removeprefix('provider.').replace('_', ' ')}{suffix}",
        flush=True,
    )


def execute_workflow(
    workspace: Path,
    args: argparse.Namespace,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    return execute_named_workflow(
        workspace,
        ExecuteRequest(
            workflow_id=getattr(args, "workflow_id", None),
            task=getattr(args, "task", None),
            run_id=getattr(args, "run_id", None),
            stage_id=_requested_stage_id(args),
            prior_run_id=getattr(args, "prior_run_id", None),
            max_attempts=getattr(args, "max_attempts", 3),
        ),
        runner=runner,
    )


def _requested_stage_id(args: argparse.Namespace) -> Optional[str]:
    stage_id = getattr(args, "stage_id", None)
    if not isinstance(stage_id, str):
        return None
    stripped = stage_id.strip()
    return stripped or None


def dispatch_agent(
    workspace: Path,
    args: argparse.Namespace,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    target = resolve_role_target(
        workspace,
        args.role,
        provider=args.provider,
        model=args.model,
        thinking=args.thinking,
    )
    run_id, created_run = _resolve_agent_run_id(
        workspace,
        run_id=args.run_id,
        runner_session_id=args.runner_session_id,
        role=args.role,
        task=args.task,
    )
    thinking_label = target.thinking or "default"
    print(
        f"[agentflow] agent {args.role} · {target.provider}/{target.model} "
        f"(thinking: {thinking_label}).",
        file=sys.stderr,
        flush=True,
    )
    previous_handlers = {
        signal.SIGINT: signal.signal(signal.SIGINT, raise_cancellation),
        signal.SIGTERM: signal.signal(signal.SIGTERM, raise_cancellation),
    }
    try:
        result = (runner or AgentToolRunner()).run(
            provider=target.provider,
            model=target.model,
            thinking=target.thinking,
            prompt=resolve_prompt(args),
            workspace=str(workspace),
            runner_session_id=args.runner_session_id,
            run_id=run_id,
            stage_id=args.role,
        )
    except CancellationRequested as exc:
        log(str(exc))
        return 130
    except ValueError as exc:
        print(f"agentflow: {exc}", file=sys.stderr)
        return 2
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)
    if created_run:
        print(f"[agentflow] run_id: {result['run_id']}", file=sys.stderr, flush=True)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _resolve_agent_run_id(
    workspace: Path,
    run_id: Optional[str],
    runner_session_id: Optional[str],
    role: str,
    task: Optional[str],
) -> tuple[str, bool]:
    session_run_id = None
    if runner_session_id:
        previous = SessionStore(workspace).latest_execution(runner_session_id)
        if previous is not None:
            session_run_id = previous.run_id
    if run_id is not None and session_run_id is not None and run_id != session_run_id:
        raise ConfigurationError(
            f"--run-id {run_id} does not match runner session run {session_run_id}."
        )
    chosen = run_id if run_id is not None else session_run_id
    if chosen is not None:
        require_agent_run(workspace, chosen)
        return chosen, False
    task_summary = (task if task is not None else role).strip()
    return create_agent_run(workspace, role, task_summary), True


def list_runs(workspace: Path) -> int:
    for _, run_id, workflow_id, status, execution_id, decision, outputs, task in list_run_rows(
        workspace
    ):
        task_suffix = f" · {task}" if task else ""
        print(
            f"{run_id} · {workflow_id} · {status} · {execution_id} · "
            f"{decision} · {outputs}{task_suffix}"
        )
    return 0


def console_main(argv: Optional[list[str]] = None) -> None:
    try:
        raise SystemExit(main(argv))
    except ConfigurationError as exc:
        print(f"agentflow: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    console_main()
