#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from rich_argparse import RichHelpFormatter

from .coordinator import build_coordinator_command
from .display import (
    print_error,
    print_manual,
    print_notice,
    print_agent,
    print_runs,
    print_watch_event,
    print_watch_snapshot,
)
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
from .manual import render_manual, topic_names


def _command(
    subparsers: Any,
    commands: Dict[str, argparse.ArgumentParser],
    name: str,
    help_text: str,
    *,
    description: Optional[str] = None,
    aliases: tuple[str, ...] = (),
    alias_map: Optional[Dict[str, str]] = None,
) -> argparse.ArgumentParser:
    command = subparsers.add_parser(
        name,
        aliases=list(aliases),
        help=help_text,
        description=description,
        formatter_class=RichHelpFormatter,
    )
    commands[name] = command
    for alias in aliases:
        commands[alias] = command
        if alias_map is not None:
            alias_map[alias] = name
    return command


def _add_prior(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prior",
        dest="prior_run_id",
        metavar="RUN",
        help=(
            "Completed run whose workflow and decision match requires. "
            "Required on create when the workflow declares requires."
        ),
    )


def _add_attempts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attempts",
        dest="max_attempts",
        type=int,
        default=3,
        metavar="N",
        help="Failed executions allowed for one stage. Default: 3.",
    )


def _add_agent_role(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--role",
        required=True,
        metavar="ROLE",
        help="Key under roles in .agentflow/config.yaml. The key must exist.",
    )


def _add_agent_model(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        help="Provider for this role. Pass with --model, or omit both.",
    )
    parser.add_argument(
        "--model",
        help="Native model id for this role. Pass with --provider, or omit both.",
    )
    parser.add_argument(
        "--thinking",
        help=(
            "Thinking level. Checked against the model allow-list unless "
            "--provider and --model are both set."
        ),
    )


def _add_agent_prompt(parser: argparse.ArgumentParser) -> None:
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="Prompt text.")
    prompt.add_argument("--file", dest="prompt_file", metavar="PATH", help="Read the prompt from this file.")
    prompt.add_argument(
        "--stdin",
        dest="prompt_stdin",
        action="store_true",
        help="Read the prompt from stdin.",
    )


def _build_parser() -> tuple[
    argparse.ArgumentParser, Dict[str, argparse.ArgumentParser], Dict[str, str]
]:
    parser = argparse.ArgumentParser(
        prog="agentflow",
        description="Run named workflows and standalone roles from .agentflow/.",
        epilog="Command usage: agentflow help <command>. Specification: agentflow man.",
        formatter_class=RichHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=False)
    commands: Dict[str, argparse.ArgumentParser] = {}
    alias_map: Dict[str, str] = {}
    _command(
        subparsers,
        commands,
        "coordinator",
        "Start the interactive coordinator.",
        description=(
            "Replace this process with the coordinator selected by "
            "runtime.coordinator. The coordinator is not a workflow role. "
            "See 'agentflow man config'."
        ),
    )
    runs_parser = _command(
        subparsers,
        commands,
        "runs",
        "List runs and their latest status.",
        description=(
            "List runs under .agentflow/runs/, newest first. "
            "Columns are run, workflow, status, latest execution, "
            "latest decision, published outputs, and task."
        ),
    )
    runs_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print the same rows as a JSON array.",
    )
    execute_parser = _command(
        subparsers,
        commands,
        "execute",
        "Run a named workflow.",
        description=(
            "start creates a run and continues until it stops. "
            "continue resumes a run until it stops. "
            "stage dispatches the next eligible stage once. "
            "See 'agentflow man execute'."
        ),
    )
    execute_subs = execute_parser.add_subparsers(dest="execute_action", required=True)
    start_parser = execute_subs.add_parser(
        "start",
        help="Create a run and continue until it stops.",
        formatter_class=RichHelpFormatter,
    )
    start_parser.add_argument("workflow", metavar="WORKFLOW", help="Workflow id.")
    start_parser.add_argument(
        "--task", required=True, help="Task recorded on the new run."
    )
    _add_prior(start_parser)
    _add_attempts(start_parser)
    continue_parser = execute_subs.add_parser(
        "continue",
        help="Resume a run until it stops.",
        formatter_class=RichHelpFormatter,
    )
    continue_parser.add_argument("run", metavar="RUN", help="Run to resume.")
    _add_prior(continue_parser)
    _add_attempts(continue_parser)
    stage_parser = execute_subs.add_parser(
        "stage",
        help="Dispatch the next eligible stage once.",
        formatter_class=RichHelpFormatter,
    )
    stage_parser.add_argument(
        "run",
        nargs="?",
        metavar="RUN",
        help="Run to advance. Omit when passing --workflow.",
    )
    stage_parser.add_argument(
        "--workflow",
        help="Workflow id. Creates a run and dispatches its next stage once.",
    )
    stage_parser.add_argument(
        "--task",
        help="Task recorded on the new run. Required with --workflow.",
    )
    _add_prior(stage_parser)
    agent_flags = argparse.ArgumentParser(add_help=False)
    agent_flags.add_argument(
        "--json",
        action="store_true",
        help="Print the result as one JSON object.",
    )
    agent_parser = _command(
        subparsers,
        commands,
        "agent",
        "Run one role outside a workflow.",
        description=(
            "start creates an agent run. continue appends a turn to RUN. "
            "--role is always checked against .agentflow/config.yaml. "
            "--provider and --model together replace that role's model. "
            "See 'agentflow man agent'."
        ),
    )
    agent_subs = agent_parser.add_subparsers(dest="agent_action", required=True)
    agent_start = agent_subs.add_parser(
        "start",
        help="Create an agent run and dispatch one turn.",
        parents=[agent_flags],
        formatter_class=RichHelpFormatter,
    )
    _add_agent_role(agent_start)
    agent_start.add_argument(
        "--task",
        help="Label stored on the new run. Defaults to the role key.",
    )
    _add_agent_model(agent_start)
    _add_agent_prompt(agent_start)
    agent_continue = agent_subs.add_parser(
        "continue",
        help="Append a turn to an agent run.",
        parents=[agent_flags],
        formatter_class=RichHelpFormatter,
    )
    agent_continue.add_argument("run", metavar="RUN", help="Agent run to continue.")
    _add_agent_role(agent_continue)
    _add_agent_model(agent_continue)
    agent_continue.add_argument(
        "--session",
        dest="runner_session_id",
        metavar="ID",
        help="Runner session to continue on this run.",
    )
    _add_agent_prompt(agent_continue)
    watch_parser = _command(
        subparsers,
        commands,
        "watch",
        "Follow events for a run.",
        description=(
            "With no --cursor, print the current snapshot and follow new events. "
            "--cursor BYTE replays from that offset, including 0 for the whole log. "
            "--once --json prints one snapshot object. "
            "Follow --json prints events as JSON lines. "
            "See 'agentflow man watch'."
        ),
    )
    watch_parser.usage = (
        "agentflow watch RUN --once [--json]\n"
        "       agentflow watch RUN [--json] [--cursor BYTE] [--execution ID] [--until]"
    )
    watch_parser.add_argument("run_id", metavar="RUN", help="Run to follow.")
    watch_parser.add_argument(
        "--execution",
        dest="execution_id",
        metavar="ID",
        help=(
            "Limit followed events and --until to this execution. "
            "With --once, show this execution in the snapshot."
        ),
    )
    watch_parser.add_argument(
        "--json",
        dest="json_lines",
        action="store_true",
        help="With --once, print one JSON snapshot. While following, print JSON lines.",
    )
    watch_parser.add_argument(
        "--cursor",
        type=int,
        metavar="BYTE",
        help="Replay events.jsonl from this byte offset. 0 replays the whole log.",
    )
    watch_mode = watch_parser.add_mutually_exclusive_group()
    watch_mode.add_argument(
        "--once",
        action="store_true",
        help="Print the snapshot and events cursor, then exit. Cannot be combined with --cursor.",
    )
    watch_mode.add_argument(
        "--until",
        dest="until_terminal",
        action="store_true",
        help="Exit when --execution reaches completed, failed, or cancelled. Requires --execution.",
    )
    help_parser = _command(
        subparsers,
        commands,
        "help",
        "Show usage for a command.",
        description="Show usage for agentflow, or for one command. Same text as --help.",
    )
    manual_parser = _command(
        subparsers,
        commands,
        "manual",
        "Show a specification topic.",
        description="Show one specification topic. With no topic, show the index.",
        aliases=("man",),
        alias_map=alias_map,
    )
    manual_parser.prog = "agentflow manual (man)"
    help_parser.add_argument(
        "command_name",
        nargs="?",
        metavar="command",
        choices=tuple(commands),
        help="Command to describe. Omit to list every command.",
    )
    manual_parser.add_argument(
        "topic",
        nargs="?",
        metavar="topic",
        choices=topic_names(),
        help=f"Topic to show: {', '.join(topic_names())}. Omit to show the index.",
    )
    return parser, commands, alias_map


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser, commands, alias_map = _build_parser()
    args = parser.parse_args(argv)
    args.command = alias_map.get(args.command, args.command)
    if args.command == "execute":
        _reject_execute_shape(commands["execute"], args)
    if args.command == "agent":
        _reject_agent_shape(commands["agent"], args)
    watch_parser = commands["watch"]
    if args.command == "watch" and args.until_terminal and not args.execution_id:
        watch_parser.error("--until requires --execution")
    if args.command == "watch" and args.cursor is not None and args.cursor < 0:
        watch_parser.error("--cursor must be >= 0")
    if args.command == "watch" and args.once and args.cursor is not None:
        watch_parser.error("--once cannot be combined with --cursor")
    return args


def _reject_execute_shape(command: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.execute_action != "stage":
        return
    has_run = isinstance(args.run, str) and bool(args.run.strip())
    has_workflow = isinstance(args.workflow, str) and bool(args.workflow.strip())
    has_task = isinstance(args.task, str) and bool(args.task.strip())
    if has_run and (has_workflow or has_task):
        command.error("Pass RUN, or --workflow and --task, not both.")
    if not has_run and not (has_workflow and has_task):
        command.error("Pass RUN, or both --workflow and --task.")


def _reject_agent_shape(command: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if (args.provider is None) != (args.model is None):
        command.error("Pass both --provider and --model, or omit both.")


def print_usage(command: Optional[str] = None) -> None:
    parser, commands, _alias_map = _build_parser()
    if command is None:
        parser.print_help()
        return
    commands[command].print_help()


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
    if args.command is None:
        print_usage()
        return 0
    if args.command == "help":
        print_usage(args.command_name)
        return 0
    if args.command == "manual":
        print_manual(render_manual(args.topic))
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
        return list_runs(workspace, as_json=args.as_json)
    if args.command == "execute":
        return execute_workflow(workspace, args)
    if args.command == "agent":
        return dispatch_agent(workspace, args)
    if args.command != "coordinator":
        raise ConfigurationError(f"Unknown command {args.command!r}.")
    config = load_yaml_mapping(workspace / ".agentflow/config.yaml")
    coordinator = resolve_coordinator(config)
    command = build_coordinator_command(workspace, coordinator)
    print_notice(
        f"Opening coordinator with {coordinator.provider}/{coordinator.model} "
        f"(thinking: {coordinator.thinking})."
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


def execute_workflow(
    workspace: Path,
    args: argparse.Namespace,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    once = args.execute_action == "stage"
    creating = args.execute_action == "start" or (
        once and not _text(getattr(args, "run", None))
    )
    return execute_named_workflow(
        workspace,
        ExecuteRequest(
            workflow_id=getattr(args, "workflow", None) if creating else None,
            task=getattr(args, "task", None) if creating else None,
            run_id=None if creating else getattr(args, "run", None),
            prior_run_id=getattr(args, "prior_run_id", None),
            max_attempts=None if once else getattr(args, "max_attempts", 3),
            once=once,
        ),
        runner=runner,
    )


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
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
        run_id=None if args.agent_action == "start" else args.run,
        runner_session_id=getattr(args, "runner_session_id", None),
        role=args.role,
        task=getattr(args, "task", None),
    )
    thinking_label = target.thinking or "default"
    print_notice(
        f"agent {args.role} · {target.provider}/{target.model} "
        f"(thinking: {thinking_label})."
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
            runner_session_id=getattr(args, "runner_session_id", None),
            run_id=run_id,
            stage_id=args.role,
        )
    except CancellationRequested as exc:
        log(str(exc))
        return 130
    except ValueError as exc:
        print_error(str(exc))
        return 2
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)
    if created_run:
        print_notice(f"run_id: {result['run_id']}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print_agent(result, role=args.role, provider=target.provider, model=target.model)
    if result.get("outcome_status") != "complete":
        return 1
    return 0


def _resolve_agent_run_id(
    workspace: Path,
    run_id: Optional[str],
    runner_session_id: Optional[str],
    role: str,
    task: Optional[str],
) -> tuple[str, bool]:
    if runner_session_id and not run_id:
        raise ConfigurationError("--session requires a run.")
    if run_id is not None and isinstance(task, str) and task.strip():
        raise ConfigurationError("--task cannot be combined with a run.")
    if run_id is not None and runner_session_id:
        previous = SessionStore(workspace).latest_execution(runner_session_id)
        if previous is not None and previous.run_id != run_id:
            raise ConfigurationError(
                f"Run {run_id} does not match runner session run {previous.run_id}."
            )
    if run_id is not None:
        require_agent_run(workspace, run_id)
        return run_id, False
    task_summary = (task if task is not None else role).strip()
    return create_agent_run(workspace, role, task_summary), True


def list_runs(workspace: Path, *, as_json: bool = False) -> int:
    rows = list_run_rows(workspace)
    if as_json:
        payload = [
            {
                "run": run_id,
                "workflow": workflow_id,
                "status": status,
                "execution": execution_id,
                "decision": decision,
                "outputs": outputs,
                "task": task,
            }
            for _, run_id, workflow_id, status, execution_id, decision, outputs, task in rows
        ]
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    print_runs(rows)
    return 0


def _print_watch_snapshot(
    run_directory: Path, execution_id: Optional[str], json_lines: bool
) -> dict[str, Any]:
    snapshot = watch_snapshot(run_directory, execution_id=execution_id)
    if json_lines:
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
    else:
        print_watch_snapshot(snapshot)
    return snapshot


def console_main(argv: Optional[list[str]] = None) -> None:
    try:
        raise SystemExit(main(argv))
    except ConfigurationError as exc:
        print_error(str(exc))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    console_main()
