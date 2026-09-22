from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Annotated, Optional

import typer

from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.command_executor import CancellationRequested, raise_cancellation
from agentflow_kernel.config import ConfigurationError, resolve_role_target
from agentflow_kernel.session_records import create_agent_session, require_agent_session
from agentflow_kernel.runtime import log
from agentflow_kernel.session_store import SessionStore

from ..display import print_agent, print_error, print_notice
from ..workspace import find_workspace, invocation_cwd, resolve_prompt

agent_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_ROLE_HELP = "Key under roles in .agentflow/config.yaml. The key must exist."
_PROVIDER_HELP = "Provider for this role. Pass with --model, or omit both."
_MODEL_HELP = "Native model id for this role. Pass with --provider, or omit both."
_THINKING_HELP = (
    "Overrides options.thinking for this turn. "
    "With --provider and --model, this is the only option sent."
)
_PROMPT_HELP = "Prompt text."
_FILE_HELP = "Read the prompt from this file."
_STDIN_HELP = "Read the prompt from stdin."
_JSON_HELP = "Print the result as one JSON object."


@agent_app.callback()
def agent_callback() -> None:
    """Run one role outside a workflow.

    start creates a session and dispatches one turn. continue appends a turn.
    The role must exist under roles in .agentflow/config.yaml.
    Pass one of --prompt, --file, or --stdin.
    Pass --provider and --model together, or omit both.
    --task is only on start and defaults to the role key.
    continue resumes the agent id of the latest execution.

    Without --json, stdout is role, model, outcome, session, and execution.
    With --json, stdout is one result object. Notices are on stderr.

    Exit 0 when outcome_status is complete.
    Exit 1 when the turn did not complete.
    Exit 2 on a configuration error or an unreadable prompt.
    Exit 130 when cancelled.
    """


@agent_app.command("start")
def agent_start(
    role: Annotated[str, typer.Option("--role", metavar="ROLE", help=_ROLE_HELP)],
    task: Annotated[
        Optional[str],
        typer.Option(
            "--task",
            metavar="TASK",
            help="Label stored on the new session. Defaults to the role key.",
        ),
    ] = None,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help=_PROVIDER_HELP, show_default=False),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", help=_MODEL_HELP, show_default=False),
    ] = None,
    thinking: Annotated[
        Optional[str],
        typer.Option("--thinking", help=_THINKING_HELP, show_default=False),
    ] = None,
    prompt: Annotated[
        Optional[str],
        typer.Option("--prompt", help=_PROMPT_HELP, show_default=False),
    ] = None,
    prompt_file: Annotated[
        Optional[str],
        typer.Option("--file", metavar="PATH", help=_FILE_HELP, show_default=False),
    ] = None,
    prompt_stdin: Annotated[bool, typer.Option("--stdin", help=_STDIN_HELP)] = False,
    as_json: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
) -> None:
    """Create a session and dispatch one turn.

    Pass one of --prompt, --file, or --stdin.
    """
    raise typer.Exit(
        _run_agent(
            role=role,
            task=task,
            session_id=None,
            provider=provider,
            model=model,
            thinking=thinking,
            prompt=prompt,
            prompt_file=prompt_file,
            prompt_stdin=prompt_stdin,
            as_json=as_json,
        )
    )


@agent_app.command("continue")
def agent_continue(
    session: Annotated[str, typer.Argument(metavar="SESSION", help="Session to continue.")],
    role: Annotated[str, typer.Option("--role", metavar="ROLE", help=_ROLE_HELP)],
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help=_PROVIDER_HELP, show_default=False),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", help=_MODEL_HELP, show_default=False),
    ] = None,
    thinking: Annotated[
        Optional[str],
        typer.Option("--thinking", help=_THINKING_HELP, show_default=False),
    ] = None,
    prompt: Annotated[
        Optional[str],
        typer.Option("--prompt", help=_PROMPT_HELP, show_default=False),
    ] = None,
    prompt_file: Annotated[
        Optional[str],
        typer.Option("--file", metavar="PATH", help=_FILE_HELP, show_default=False),
    ] = None,
    prompt_stdin: Annotated[bool, typer.Option("--stdin", help=_STDIN_HELP)] = False,
    as_json: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
) -> None:
    """Append a turn to a session.

    Pass one of --prompt, --file, or --stdin.
    The turn resumes the agent id of the latest execution.
    """
    raise typer.Exit(
        _run_agent(
            role=role,
            task=None,
            session_id=session,
            provider=provider,
            model=model,
            thinking=thinking,
            prompt=prompt,
            prompt_file=prompt_file,
            prompt_stdin=prompt_stdin,
            as_json=as_json,
        )
    )


def _run_agent(
    *,
    role: str,
    task: Optional[str],
    session_id: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    thinking: Optional[str],
    prompt: Optional[str],
    prompt_file: Optional[str],
    prompt_stdin: bool,
    as_json: bool,
) -> int:
    if (provider is None) != (model is None):
        raise typer.BadParameter("Pass both --provider and --model, or omit both.")
    selected = (prompt is not None) + (prompt_file is not None) + int(prompt_stdin)
    if selected != 1:
        raise typer.BadParameter("Pass one of --prompt, --file, or --stdin.")
    return dispatch_agent(
        find_workspace(invocation_cwd()),
        role=role,
        task=task,
        session_id=session_id,
        provider=provider,
        model=model,
        thinking=thinking,
        prompt=prompt,
        prompt_file=prompt_file,
        prompt_stdin=prompt_stdin,
        as_json=as_json,
    )


def dispatch_agent(
    workspace: Path,
    *,
    role: str,
    task: Optional[str],
    session_id: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    thinking: Optional[str],
    prompt: Optional[str],
    prompt_file: Optional[str],
    prompt_stdin: bool,
    as_json: bool,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    target = resolve_role_target(
        workspace,
        role,
        provider=provider,
        model=model,
        thinking=thinking,
    )
    resolved_session_id, created_session = _resolve_session_id(
        workspace,
        session_id=session_id,
        role=role,
        task=task,
    )
    resume_execution_id = None
    if not created_session:
        previous = SessionStore(workspace).latest_execution(resolved_session_id)
        if previous is not None:
            resume_execution_id = previous.execution_id
    thinking_label = target.thinking or "default"
    print_notice(
        f"agent {role} · {target.provider}/{target.model} "
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
            options=dict(target.options),
            prompt=resolve_prompt(
                prompt=prompt,
                prompt_file=prompt_file,
                prompt_stdin=prompt_stdin,
            ),
            workspace=str(workspace),
            session_id=resolved_session_id,
            resume_execution_id=resume_execution_id,
            stage_id=role,
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
    if created_session:
        print_notice(f"session_id: {result['session_id']}")
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print_agent(result, role=role, provider=target.provider, model=target.model)
    if result.get("outcome_status") != "complete":
        return 1
    return 0


def _resolve_session_id(
    workspace: Path,
    session_id: Optional[str],
    role: str,
    task: Optional[str],
) -> tuple[str, bool]:
    if session_id is not None and isinstance(task, str) and task.strip():
        raise ConfigurationError("--task cannot be combined with a session.")
    if session_id is not None:
        require_agent_session(workspace, session_id)
        return session_id, False
    task_summary = (task if task is not None else role).strip()
    return create_agent_session(workspace, role, task_summary), True
