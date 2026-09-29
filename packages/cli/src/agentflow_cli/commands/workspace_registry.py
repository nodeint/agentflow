from __future__ import annotations

import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from agentflow_kernel.config import ConfigurationError
from agentflow_registry.home import ensure_registry_home
from agentflow_registry.service import (
    ServiceError,
    call_service,
    endpoint_from_home,
    start_detached,
)

from ..display import print_error
from ..workspace import find_workspace, invocation_cwd

workspace_app = typer.Typer(
    help="Register this machine's Agentflow workspaces.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@workspace_app.command("register")
def register_command(
    path: Annotated[
        Optional[str],
        typer.Option("--path", help="Workspace root or a directory inside it."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", help="Display name. Defaults to the directory name."),
    ] = None,
) -> None:
    """Register the workspace that contains .agentflow/config.yaml.

    A relative --path is resolved from the invocation directory. The stored
    path is the absolute workspace root. Progress is written to stderr and
    the record is written to stdout.
    """
    raise typer.Exit(run_register(path=path, name=name))


@workspace_app.command("list")
def list_command(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON object."),
    ] = False,
) -> None:
    """List registered workspaces.

    Each row includes id, name, path, and whether .agentflow/config.yaml
    is still present.
    """
    raise typer.Exit(run_list(as_json=as_json))


@workspace_app.command("remove")
def remove_command(
    workspace_id: Annotated[str, typer.Argument(help="Workspace id to remove.")],
) -> None:
    """Remove one registered workspace by id.

    Exit 2 when the id is unknown. The registry file is not rewritten in
    that case.
    """
    raise typer.Exit(run_remove(workspace_id))


def run_register(*, path: str | None, name: str | None) -> int:
    root = _workspace_root(path)
    if not root.is_absolute():
        print_error("Workspace path is not absolute.")
        return 2
    sys.stderr.write(f"Registering {root}\n")
    try:
        endpoint = start_detached(ensure_registry_home())
    except ServiceError as exc:
        print_error(str(exc))
        return 1
    body: dict[str, Any] = {"path": str(root), "tool": _tool_payload()}
    if name is not None:
        body["name"] = name
    if not Path(body["path"]).is_absolute():
        print_error("Workspace path is not absolute.")
        return 2
    return _finish("POST", "/v1/workspaces", endpoint, body, ok_empty=False)


def run_list(*, as_json: bool) -> int:
    endpoint = endpoint_from_home(ensure_registry_home())
    if endpoint is None:
        print_error("Registry service is not running.")
        return 1
    try:
        status, payload = call_service(
            endpoint.host,
            endpoint.port,
            endpoint.token,
            "GET",
            "/v1/workspaces",
        )
    except OSError as exc:
        print_error(f"Registry request failed: {exc}")
        return 1
    if status != 200:
        return _status_exit(status, payload)
    rows = payload.get("workspaces")
    if not isinstance(rows, list):
        print_error("Registry response was invalid.")
        return 1
    if as_json:
        print(json.dumps({"workspaces": rows}))
    else:
        for row in rows:
            available = "true" if row.get("available") else "false"
            print(f"{row.get('id')}  {row.get('name')}  {row.get('path')}  {available}")
    return 0


def run_remove(workspace_id: str) -> int:
    endpoint = endpoint_from_home(ensure_registry_home())
    if endpoint is None:
        print_error("Registry service is not running.")
        return 1
    return _finish("DELETE", f"/v1/workspaces/{workspace_id}", endpoint, None, ok_empty=False)


def _finish(
    method: str,
    path: str,
    endpoint: Any,
    body: dict[str, Any] | None,
    *,
    ok_empty: bool,
) -> int:
    del ok_empty
    try:
        status, payload = call_service(
            endpoint.host, endpoint.port, endpoint.token, method, path, body
        )
    except OSError as exc:
        print_error(f"Registry request failed: {exc}")
        return 1
    if status in {200, 201}:
        print(json.dumps(payload))
        return 0
    return _status_exit(status, payload)


def _status_exit(status: int, payload: dict[str, Any]) -> int:
    message = payload.get("message")
    print_error(message if isinstance(message, str) and message else "Registry request failed.")
    if status == 404:
        return 2
    return 1


def _workspace_root(path: str | None) -> Path:
    if path is None:
        return find_workspace(invocation_cwd()).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = invocation_cwd() / candidate
    return find_workspace(candidate).resolve()


def _tool_payload() -> dict[str, str]:
    try:
        installed = version("agentflow-cli")
    except PackageNotFoundError:
        installed = "0.1.0"
    return {"name": "agentflow", "version": installed}


# ConfigurationError is raised by find_workspace and handled by main.
_ = ConfigurationError
