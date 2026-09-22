from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from agentflow_kernel.query import list_run_rows

from ..display import print_runs
from ..workspace import find_workspace, invocation_cwd

runs_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    subcommand_metavar="",
    rich_markup_mode="rich",
)


@runs_app.callback(invoke_without_command=True)
def runs_command(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the same rows as a JSON array."),
    ] = False,
) -> None:
    """List runs and their latest status.

    Rows come from .agentflow/runs/, newest first.
    Columns are run, workflow, status, latest execution, latest decision,
    published outputs, and task. --json prints those rows as a JSON array.
    """
    raise typer.Exit(list_runs(find_workspace(invocation_cwd()), as_json=as_json))


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
