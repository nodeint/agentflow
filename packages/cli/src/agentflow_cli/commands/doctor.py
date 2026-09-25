from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated, Callable, Optional

import typer

from agentflow_adapters import default_adapters
from agentflow_kernel.doctor import Check, diagnose

from ..display import print_doctor
from ..migrate import require_current_schema
from ..workspace import find_workspace, invocation_cwd

doctor_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    subcommand_metavar="",
    rich_markup_mode="rich",
)


@doctor_app.callback(invoke_without_command=True)
def doctor_command(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print the checks as one JSON object."),
    ] = False,
) -> None:
    """Check config, workflows, and provider CLIs.

    Reads .agentflow/config.yaml and every workflow in .agentflow/workflows/.
    --json prints one object with ok and checks.
    Exit 0 when every check passes. Exit 1 when a check fails.
    Exit 2 when .agentflow/config.yaml cannot be found.
    """
    raise typer.Exit(run_doctor(find_workspace(invocation_cwd()), as_json=as_json))


def run_doctor(
    workspace: Path,
    *,
    as_json: bool = False,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> int:
    require_current_schema(workspace)
    checks = diagnose(
        workspace,
        adapters=default_adapters(),
        which=shutil.which if which is None else which,
    )
    if as_json:
        print(json.dumps(_payload(checks), ensure_ascii=False))
    else:
        print_doctor(checks)
    return 0 if all(check.ok for check in checks) else 1


def _payload(checks: tuple[Check, ...]) -> dict[str, object]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [
            {
                "name": check.name,
                "ok": check.ok,
                "items": list(check.items),
                "problems": list(check.problems),
            }
            for check in checks
        ],
    }
