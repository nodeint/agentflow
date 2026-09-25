from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from agentflow_kernel.agent_runner import AgentToolRunner
from agentflow_kernel.execute import (
    DEFAULT_MAX_DISPATCHES,
    ExecuteRequest,
    execute_named_workflow,
)

from ..migrate import require_current_schema
from ..workspace import find_workspace, invocation_cwd

workflow_commands = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
)

_PRIOR_HELP = (
    "Completed session whose workflow and decision match requires. "
    "Required on create when the workflow declares requires."
)
_ATTEMPTS_HELP = "Failed executions allowed for one stage. Default: 3."
_DISPATCHES_HELP = (
    "Stage dispatches allowed in this command. "
    f"Default: {DEFAULT_MAX_DISPATCHES}. "
    "Pass --unlimited-dispatches to remove the ceiling."
)
_UNLIMITED_DISPATCHES_HELP = "Remove the dispatch ceiling for this command."


@workflow_commands.command("start")
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
    max_dispatches: Annotated[
        Optional[int],
        typer.Option(
            "--max-dispatches",
            metavar="N",
            help=_DISPATCHES_HELP,
            show_default=False,
        ),
    ] = None,
    unlimited_dispatches: Annotated[
        bool,
        typer.Option("--unlimited-dispatches", help=_UNLIMITED_DISPATCHES_HELP),
    ] = False,
) -> None:
    """Create a session and continue until it stops.

    WORKFLOW is the workflow id. --task is recorded on the new session.
    --prior is required when the workflow declares requires. It must be a
    completed session of that workflow with the required decision.
    --attempts limits failed executions of one stage. The default is 3.
    A value below 1 is an error.
    --max-dispatches limits stage dispatches in this command. The default is 20.
    A value below 1 is an error. --unlimited-dispatches removes that ceiling.
    A stage max_revisions limits how many times a route may run that stage again
    after its first success. The review that closes the last revision still runs.

    Provider, model, and prompt come from the workflow and config.yaml.
    Stdout is one JSON object: session_id, session_status, stop_reason, outputs, stages.
    Progress is on stderr.

    Exit 0 when the session is completed.
    Exit 1 when the session is blocked.
    Exit 2 on a configuration error.
    Exit 3 when --attempts is spent. The session stays active.
    Exit 4 when a route would exceed max_revisions. The session stays active.
    Exit 5 when --max-dispatches is spent. The session stays active.
    Exit 130 when cancelled.
    """
    ceiling = _dispatch_ceiling(max_dispatches, unlimited_dispatches)
    raise typer.Exit(
        execute_workflow(
            find_workspace(invocation_cwd()),
            action="start",
            workflow_id=workflow,
            task=task,
            prior_session_id=prior,
            max_attempts=attempts,
            max_dispatches=ceiling,
        )
    )


@workflow_commands.command("continue")
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
    max_dispatches: Annotated[
        Optional[int],
        typer.Option(
            "--max-dispatches",
            metavar="N",
            help=_DISPATCHES_HELP,
            show_default=False,
        ),
    ] = None,
    unlimited_dispatches: Annotated[
        bool,
        typer.Option("--unlimited-dispatches", help=_UNLIMITED_DISPATCHES_HELP),
    ] = False,
) -> None:
    """Resume a session until it stops.

    A completed, blocked, or cancelled session is not dispatched again.
    The attempt count is kept.
    --prior is the completed session named by requires.
    --attempts limits failed executions of one stage. The default is 3.
    A value below 1 is an error.
    --max-dispatches limits stage dispatches in this command. The default is 20.
    A value below 1 is an error. --unlimited-dispatches removes that ceiling.
    The revision count is kept.

    Provider, model, and prompt come from the workflow and config.yaml.
    Stdout is one JSON object: session_id, session_status, stop_reason, outputs, stages.
    Progress is on stderr.

    Exit 0 when the session is completed.
    Exit 1 when the session is blocked.
    Exit 2 on a configuration error.
    Exit 3 when --attempts is spent. The session stays active.
    Exit 4 when a route would exceed max_revisions. The session stays active.
    Exit 5 when --max-dispatches is spent. The session stays active.
    Exit 130 when cancelled.
    """
    ceiling = _dispatch_ceiling(max_dispatches, unlimited_dispatches)
    raise typer.Exit(
        execute_workflow(
            find_workspace(invocation_cwd()),
            action="continue",
            session_id=session,
            prior_session_id=prior,
            max_attempts=attempts,
            max_dispatches=ceiling,
        )
    )


@workflow_commands.command("stage")
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
    The command takes no stage id.
    --prior is required on create when the workflow declares requires.
    A route back to a stage that has used max_revisions is refused.
    This command has no dispatch ceiling.

    Provider, model, and prompt come from the workflow and config.yaml.
    Stdout is one JSON object: session_id, session_status, stop_reason, outputs, stages.
    Progress is on stderr.

    Exit 0 when the stage outcome is complete, or the session is already completed.
    Exit 1 when the stage is not complete, or the session is blocked.
    Exit 2 on a configuration error.
    Exit 4 when a route would exceed max_revisions. The session stays active.
    Exit 130 when cancelled.
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
            max_dispatches=None,
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
    max_dispatches: Optional[int] = DEFAULT_MAX_DISPATCHES,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    require_current_schema(workspace)
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
            max_dispatches=None if once else max_dispatches,
            once=once,
        ),
        runner=runner or AgentToolRunner(),
    )


def _dispatch_ceiling(max_dispatches: Optional[int], unlimited: bool) -> Optional[int]:
    if unlimited and max_dispatches is not None:
        raise typer.BadParameter(
            "--max-dispatches cannot be combined with --unlimited-dispatches."
        )
    if unlimited:
        return None
    if max_dispatches is None:
        return DEFAULT_MAX_DISPATCHES
    return max_dispatches


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
