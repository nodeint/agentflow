#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from agentflow_kernel.config import ConfigurationError

from .commands.agent import agent_app, dispatch_agent
from .commands.execute import execute_workflow, workflow_commands
from .commands.sessions import list_sessions, sessions_app
from .commands.watch import watch_app, watch_session
from .display import print_error
from .workspace import (
    find_workspace,
    invocation_cwd,
    reset_invocation_cwd,
    resolve_prompt,
    set_invocation_cwd,
)

app = typer.Typer(
    name="agentflow",
    help=(
        "Run named workflows and standalone roles from .agentflow/.\n\n"
        "config.yaml declares models and roles. workflows/<id>.yaml is one "
        "workflow. sessions/ is local execution state. Commit config and workflows."
    ),
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


app.add_typer(sessions_app, name="sessions")
app.add_typer(workflow_commands)
app.add_typer(agent_app, name="agent")
app.add_typer(watch_app, name="watch")

__all__ = [
    "app",
    "console_main",
    "dispatch_agent",
    "execute_workflow",
    "find_workspace",
    "list_sessions",
    "main",
    "resolve_prompt",
    "watch_session",
]


def main(argv: Optional[list[str]] = None, cwd: Optional[Path] = None) -> int:
    token = set_invocation_cwd(cwd)
    try:
        app(args=argv, prog_name="agentflow")
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            return 0
        if isinstance(code, int):
            return code
        return 1
    except ConfigurationError as exc:
        print_error(str(exc))
        return 2
    else:
        return 0
    finally:
        reset_invocation_cwd(token)


def console_main(argv: Optional[list[str]] = None) -> None:
    try:
        raise SystemExit(main(argv))
    except ConfigurationError as exc:
        print_error(str(exc))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    console_main()
