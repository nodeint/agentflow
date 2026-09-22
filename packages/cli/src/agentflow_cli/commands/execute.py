from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.execute import ExecuteRequest, execute_named_workflow

from ..workspace import find_workspace, invocation_cwd

execute_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_PRIOR_HELP = (
    "Completed session whose workflow and decision match requires. "
    "Required on create when the workflow declares requires."
)
_ATTEMPTS_HELP = "Failed executions allowed for one stage. Default: 3."


@execute_app.callback()
def execute_callback() -> None:
    """Run a named workflow.

    start creates a session and continues until the session stops.
    continue resumes a session and does the same. It does not reset the attempt count.
    stage dispatches the next eligible stage once. It takes no stage id.
    On a new session, pass --workflow and --task instead of SESSION.

    --prior is the completed session named by requires. It is required on create
    when the workflow declares requires, and it must match that workflow and
    decision. --attempts limits failed executions of one stage on start and
    continue. The default is 3. A value below 1 is an error. stage ignores it.

    Provider, model, and prompt come from the workflow and config.yaml.
    Stdout is one JSON object: session_id, session_status, stop_reason, outputs, stages.
    Progress is on stderr.

    Exit 0 when the session is completed, or a one-shot stage outcome is complete.
    Exit 1 when the session is blocked, or a one-shot stage is not complete.
    Exit 2 on a configuration error.
    Exit 3 when start or continue spends --attempts. The session stays active.
    Exit 130 when cancelled.
    """


@execute_app.command("start")
def execute_start(
    workflow: Annotated[str, typer.Argument(metavar="WORKFLOW", help="Workflow id.")],
    task: Annotated[
        str,
        typer.Option("--task", metavar="TASK", help="Task recorded on the new session."),
    ],
    prior: Annotated[
        Optional[str],
        typer.Option("--prior", metavar="SESSION", help=_PRIOR_HELP, show_default=False),
    ] = None,
    attempts: Annotated[
        int,
        typer.Option(
            "--attempts",
            metavar="N",
            help=_ATTEMPTS_HELP,
            show_default=False,
        ),
    ] = 3,
) -> None:
    """Create a session and continue until it stops.

    WORKFLOW is the workflow id. --task is recorded on the new session.
    """
    raise typer.Exit(
        execute_workflow(
            find_workspace(invocation_cwd()),
            action="start",
            workflow_id=workflow,
            task=task,
            prior_session_id=prior,
            max_attempts=attempts,
        )
    )


@execute_app.command("continue")
def execute_continue(
    session: Annotated[str, typer.Argument(metavar="SESSION", help="Session to resume.")],
    prior: Annotated[
        Optional[str],
        typer.Option("--prior", metavar="SESSION", help=_PRIOR_HELP, show_default=False),
    ] = None,
    attempts: Annotated[
        int,
        typer.Option(
            "--attempts",
            metavar="N",
            help=_ATTEMPTS_HELP,
            show_default=False,
        ),
    ] = 3,
) -> None:
    """Resume a session until it stops.

    A completed, blocked, or cancelled session is not dispatched again.
    """
    raise typer.Exit(
        execute_workflow(
            find_workspace(invocation_cwd()),
            action="continue",
            session_id=session,
            prior_session_id=prior,
            max_attempts=attempts,
        )
    )


@execute_app.command("stage")
def execute_stage(
    session: Annotated[
        Optional[str],
        typer.Argument(
            metavar="SESSION",
            help="Session to advance. Omit when passing --workflow.",
        ),
    ] = None,
    workflow: Annotated[
        Optional[str],
        typer.Option(
            "--workflow",
            help="Workflow id. Creates a session and dispatches its next stage once.",
        ),
    ] = None,
    task: Annotated[
        Optional[str],
        typer.Option(
            "--task",
            metavar="TASK",
            help="Task recorded on the new session. Required with --workflow.",
        ),
    ] = None,
    prior: Annotated[
        Optional[str],
        typer.Option("--prior", metavar="SESSION", help=_PRIOR_HELP, show_default=False),
    ] = None,
) -> None:
    """Dispatch the next eligible stage once.

    Pass SESSION, or both --workflow and --task. Not both forms.
    """
    has_session = _text(session) is not None
    has_workflow = _text(workflow) is not None
    has_task = _text(task) is not None
    if has_session and (has_workflow or has_task):
        raise typer.BadParameter("Pass SESSION, or --workflow and --task, not both.")
    if not has_session and not (has_workflow and has_task):
        raise typer.BadParameter("Pass SESSION, or both --workflow and --task.")
    raise typer.Exit(
        execute_workflow(
            find_workspace(invocation_cwd()),
            action="stage",
            workflow_id=workflow,
            task=task,
            session_id=session,
            prior_session_id=prior,
            max_attempts=None,
        )
    )


def execute_workflow(
    workspace: Path,
    *,
    action: str,
    workflow_id: Optional[str] = None,
    task: Optional[str] = None,
    session_id: Optional[str] = None,
    prior_session_id: Optional[str] = None,
    max_attempts: Optional[int] = 3,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    once = action == "stage"
    creating = action == "start" or (once and not _text(session_id))
    return execute_named_workflow(
        workspace,
        ExecuteRequest(
            workflow_id=workflow_id if creating else None,
            task=task if creating else None,
            session_id=None if creating else session_id,
            prior_session_id=prior_session_id,
            max_attempts=None if once else max_attempts,
            once=once,
        ),
        runner=runner or AgentToolRunner(),
    )


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
