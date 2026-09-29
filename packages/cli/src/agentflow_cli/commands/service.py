from __future__ import annotations

import json
from typing import Annotated, Optional

import typer

from agentflow_registry.home import ensure_registry_home, registry_home_path
from agentflow_registry.server import DEFAULT_PORT
from agentflow_registry.service import (
    ServiceError,
    serve_foreground,
    service_status,
    start_detached,
    stop,
)

from ..display import print_error

service_app = typer.Typer(
    help="Run the local workspace registry service.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@service_app.command("start")
def start_command(
    port: Annotated[
        Optional[int],
        typer.Option("--port", help="Bind 127.0.0.1 on this port. The default is 47321."),
    ] = None,
    foreground: Annotated[
        bool,
        typer.Option("--foreground", help="Serve in this process instead of detaching."),
    ] = False,
) -> None:
    """Start the registry service, or adopt one that is already running.

    A detached start publishes service.json and returns. --foreground serves
    until SIGINT or SIGTERM and leaves service.json in place.
    """
    raise typer.Exit(run_service_start(port=port, foreground=foreground))


@service_app.command("stop")
def stop_command() -> None:
    """Stop the registry service when its process identity matches.

    Exit 0 when the service is already absent or was stopped. Exit 1 when
    the process is still alive or its identity cannot be verified.
    """
    raise typer.Exit(run_service_stop())


@service_app.command("status")
def status_command(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON object."),
    ] = False,
) -> None:
    """Report whether the registry service is running.

    running includes the endpoint. stopped is also used when a stale
    service.json is still on disk. The bearer token is not printed.
    """
    raise typer.Exit(run_service_status(as_json=as_json))


def run_service_start(*, port: int | None, foreground: bool) -> int:
    home = ensure_registry_home()
    chosen = DEFAULT_PORT if port is None else port
    try:
        if foreground:
            serve_foreground(home, chosen)
            return 0
        endpoint = start_detached(home, chosen)
    except ServiceError as exc:
        print_error(str(exc))
        return 1
    print(f"{endpoint.host}:{endpoint.port}")
    return 0


def run_service_stop() -> int:
    result = stop(ensure_registry_home())
    if result.outcome == "error":
        print_error(result.message or "Registry service did not stop.")
        return 1
    return 0


def run_service_status(*, as_json: bool) -> int:
    report = service_status(registry_home_path())
    if as_json:
        print(json.dumps(report))
    elif report["state"] == "running":
        print("running")
        print(f"{report['host']}:{report['port']} pid={report['pid']}")
    else:
        print("stopped")
    return 0
