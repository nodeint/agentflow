from __future__ import annotations

from typing import Annotated

import typer

from ..migrate import run_migrate
from ..workspace import find_workspace, invocation_cwd


def migrate_command(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Upgrade without asking. Use this when no terminal is available.",
        ),
    ] = False,
) -> None:
    """Upgrade config and workflows to the current schema version.

    Prints the changes and asks before writing. Pass --yes to write without
    a question. Exit 0 when the schema is current, the upgrade is declined,
    or the files are written. Exit 2 when input is not a terminal and --yes
    was omitted.
    """
    raise typer.Exit(run_migrate(find_workspace(invocation_cwd()), assume_yes=yes))
