"""Workspace registry routes."""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .store import (
    InvalidRecord,
    NotAWorkspace,
    RegistryBusy,
    RegistryCorrupt,
    WorkspaceNotFound,
)
from .workspace import WorkspaceLocationError, locate_workspace

Auth = Literal["required", "health"]


def _noop() -> None:
    return None


class WorkspaceRecords(Protocol):
    def list(self) -> list[dict[str, Any]]: ...

    def get(self, workspace_id: str) -> dict[str, Any]: ...

    def remove(self, workspace_id: str) -> dict[str, Any]: ...

    def register(
        self, path: str, name: str | None, tool: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]: ...


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Result:
    status: int
    payload: dict[str, Any]


@dataclass
class Call:
    method: str
    path: str
    store: WorkspaceRecords
    token: str
    instance_id: str
    authorization: str | None = None
    body: bytes | None = None
    before_store: Callable[[], None] = field(default=_noop, repr=False)
    params: dict[str, str] = field(default_factory=dict)

    def json_object(self, allowed: set[str], required: set[str]) -> dict[str, Any]:
        """Return a JSON object whose keys are inside `allowed` and cover `required`."""
        raw = self.body if self.body is not None else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, "invalid_request", "Request body is not JSON.") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) - allowed
            or not required <= set(payload)
        ):
            raise HttpError(400, "invalid_request", "Request fields are invalid.")
        return payload


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    handler: Callable[[Call], Result]
    auth: Auth = "required"
    answer_before_body: bool = False


class Router:
    """Match a request to a route function.

    Register a route with `route`. An exact pattern is `/v1/workspaces`.
    A pattern ending in `{name}` captures one path segment. `answer_before_body`
    answers a method mismatch before the server reads a POST body.
    """

    def __init__(self) -> None:
        self._routes: list[Route] = []

    def route(
        self,
        method: str,
        pattern: str,
        *,
        auth: Auth = "required",
        answer_before_body: bool = False,
    ) -> Callable[[Callable[[Call], Result]], Callable[[Call], Result]]:
        def decorate(handler: Callable[[Call], Result]) -> Callable[[Call], Result]:
            _check_pattern(pattern)
            self._routes.append(
                Route(method, pattern, handler, auth, answer_before_body)
            )
            return handler

        return decorate

    def should_read_body(self, method: str, path: str) -> bool:
        if method != "POST":
            return False
        for route in self._routes:
            if route.answer_before_body and match_path(route.pattern, path) is not None:
                return False
        return True

    def dispatch(self, call: Call) -> Result:
        call.store = _GatedStore(call.store, call.before_store)
        matched: list[tuple[Route, dict[str, str]]] = []
        for route in self._routes:
            params = match_path(route.pattern, call.path)
            if params is not None:
                matched.append((route, params))
        chosen = [item for item in matched if item[0].method == call.method]
        if not chosen:
            if auth_state(call.authorization, call.token) != "ok":
                return _unauthorized()
            if not matched:
                return _failure(404, "workspace_not_found", "Not found.")
            return _failure(405, "method_not_allowed", "Method is not allowed.")
        route, params = chosen[0]
        call.params = params
        if route.auth != "health" and auth_state(call.authorization, call.token) != "ok":
            return _unauthorized()
        try:
            return route.handler(call)
        except HttpError as exc:
            return _failure(exc.status, exc.code, exc.message)
        except (
            RegistryBusy,
            RegistryCorrupt,
            WorkspaceNotFound,
            InvalidRecord,
            NotAWorkspace,
        ) as exc:
            return _failure_for_store(exc)


def match_path(pattern: str, path: str) -> dict[str, str] | None:
    marker = pattern.rfind("{")
    if marker == -1:
        return {} if pattern == path else None
    prefix = pattern[:marker]
    name = pattern[marker + 1 : -1]
    if not path.startswith(prefix):
        return None
    value = path[len(prefix) :]
    if value == "" or "/" in value:
        return None
    return {name: value}


def _check_pattern(pattern: str) -> None:
    if "{" not in pattern and "}" not in pattern:
        return
    marker = pattern.rfind("{")
    name = pattern[marker + 1 : -1]
    if (
        pattern.count("{") != 1
        or not pattern.endswith("}")
        or "/" in pattern[marker:]
        or name == ""
        or not pattern[:marker].endswith("/")
    ):
        raise ValueError("A route pattern is an exact path or one trailing {name} segment.")


def auth_state(authorization: str | None, token: str) -> str:
    if authorization is None:
        return "absent"
    scheme, _, rest = authorization.partition(" ")
    presented = rest.strip()
    if scheme != "Bearer" or presented == "" or " " in presented:
        return "bad"
    try:
        matches = hmac.compare_digest(presented, token)
    except (TypeError, ValueError):
        return "bad"
    return "ok" if matches else "bad"


def build_router() -> Router:
    router = Router()

    @router.route("GET", "/v1/health", auth="health", answer_before_body=True)
    def health(call: Call) -> Result:
        state = auth_state(call.authorization, call.token)
        if state == "bad":
            return _unauthorized()
        if state == "ok":
            return Result(200, {"ok": True, "instance_id": call.instance_id})
        return Result(200, {"ok": True})

    @router.route("GET", "/v1/workspaces")
    def list_workspaces(call: Call) -> Result:
        return Result(200, {"workspaces": call.store.list()})

    @router.route("POST", "/v1/workspaces")
    def create_workspace(call: Call) -> Result:
        payload = call.json_object({"path", "name", "tool"}, {"path"})
        raw_path = payload["path"]
        if not isinstance(raw_path, str):
            raise HttpError(400, "invalid_request", "Workspace path is not absolute.")
        name = payload.get("name")
        if "name" in payload and (not isinstance(name, str) or not 1 <= len(name) <= 200):
            raise HttpError(400, "invalid_request", "Workspace name is invalid.")
        if "tool" not in payload:
            raise HttpError(400, "invalid_request", "Tool name is invalid.")
        try:
            root = locate_workspace(raw_path)
        except WorkspaceLocationError as exc:
            raise HttpError(400, "invalid_request", str(exc)) from exc
        record, created = call.store.register(
            str(root), name if isinstance(name, str) else None, payload["tool"]
        )
        return Result(201 if created else 200, record)

    @router.route("GET", "/v1/workspaces/{workspace_id}")
    def get_workspace(call: Call) -> Result:
        return Result(200, call.store.get(call.params["workspace_id"]))

    @router.route("DELETE", "/v1/workspaces/{workspace_id}")
    def delete_workspace(call: Call) -> Result:
        return Result(200, call.store.remove(call.params["workspace_id"]))

    return router


class _GatedStore:
    """Clear the read deadline before the registry lock is taken."""

    def __init__(self, store: WorkspaceRecords, before_store: Callable[[], None]) -> None:
        self._store = store
        self._before_store = before_store

    def list(self) -> list[dict[str, Any]]:
        self._before_store()
        return self._store.list()

    def get(self, workspace_id: str) -> dict[str, Any]:
        self._before_store()
        return self._store.get(workspace_id)

    def remove(self, workspace_id: str) -> dict[str, Any]:
        self._before_store()
        return self._store.remove(workspace_id)

    def register(
        self, path: str, name: str | None, tool: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        self._before_store()
        return self._store.register(path, name, tool)


def _unauthorized() -> Result:
    return _failure(401, "unauthorized", "Bearer token is missing or incorrect.")


def _failure(status: int, code: str, message: str) -> Result:
    return Result(status, {"error": code, "message": message})


def _failure_for_store(exc: Exception) -> Result:
    if isinstance(exc, RegistryBusy):
        return _failure(409, "registry_busy", "The registry is busy.")
    if isinstance(exc, RegistryCorrupt):
        return _failure(
            409,
            "registry_corrupt",
            "registry.json is corrupt and was left unchanged.",
        )
    if isinstance(exc, WorkspaceNotFound):
        return _failure(404, "workspace_not_found", str(exc))
    return _failure(400, "invalid_request", str(exc))
