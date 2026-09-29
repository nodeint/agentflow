from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .locks import RegistryBusy, interprocess_lock, registry_busy

__all__ = [
    "InvalidRecord",
    "NotAWorkspace",
    "RegistryBusy",
    "RegistryCorrupt",
    "RegistryStore",
    "WorkspaceNotFound",
    "atomic_replace",
    "workspace_id_for",
]

_TOOL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TOP_KEYS = {"schema_version", "workspaces"}
_WORKSPACE_KEYS = {"id", "name", "path", "registered_at", "updated_at", "tool"}
_TOOL_KEYS = {"name", "version"}
_STRING_FIELDS = ("id", "name", "path", "registered_at", "updated_at")


class RegistryCorrupt(Exception):
    pass


class WorkspaceNotFound(Exception):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"Workspace was not found: {workspace_id}")
        self.workspace_id = workspace_id


class NotAWorkspace(Exception):
    pass


class InvalidRecord(Exception):
    pass


def workspace_id_for(path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    return f"ws_{digest}"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_replace(destination: Path, payload: bytes) -> None:
    directory = destination.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp_path = directory / f"{destination.name}.tmp.{secrets.token_hex(8)}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(tmp_path, flags, 0o600)
    replaced = False
    try:
        try:
            os.fchmod(fd, 0o600)
            view = payload
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write publishing registry file")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, destination)
        replaced = True
        os.chmod(destination, 0o600)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if not replaced:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


class RegistryStore:
    def __init__(self, directory: Path, timeout_sec: float = 2.0) -> None:
        self.directory = Path(directory)
        self.timeout_sec = timeout_sec

    def _locked_hook(self) -> None:
        return None

    def register(
        self, path: str, name: str | None, tool: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        stored, chosen_name, stored_tool = _prepare_register(path, name, tool)
        with self._lock(shared=False):
            records = self._load()
            if not _is_workspace(stored):
                raise NotAWorkspace(stored)
            self._locked_hook()
            now = utc_timestamp()
            created = True
            updated: dict[str, Any] | None = None
            for record in records:
                if record["path"] == stored:
                    created = False
                    record["name"] = chosen_name
                    record["tool"] = stored_tool
                    record["updated_at"] = now
                    updated = record
                    break
            if updated is None:
                updated = {
                    "id": workspace_id_for(stored),
                    "name": chosen_name,
                    "path": stored,
                    "registered_at": now,
                    "updated_at": now,
                    "tool": stored_tool,
                }
                records.append(updated)
            self._publish(records)
            return _public(updated), created

    def list(self) -> list[dict[str, Any]]:
        with self._lock(shared=True):
            records = self._load()
            ordered = sorted(records, key=lambda row: (row["registered_at"], row["id"]))
            return [_public(record) for record in ordered]

    def get(self, workspace_id: str) -> dict[str, Any]:
        with self._lock(shared=True):
            records = self._load()
            found = _find(records, workspace_id)
            return _public(found)

    def remove(self, workspace_id: str) -> dict[str, Any]:
        with self._lock(shared=False):
            records = self._load()
            found = _find(records, workspace_id)
            self._locked_hook()
            records = [record for record in records if record["id"] != workspace_id]
            self._publish(records)
            return _public(found)

    @contextmanager
    def _lock(self, *, shared: bool) -> Iterator[None]:
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        with interprocess_lock(
            self.directory / "registry.lock",
            shared=shared,
            timeout_sec=self.timeout_sec,
            busy_error=registry_busy,
        ):
            yield

    def _load(self) -> list[dict[str, Any]]:
        path = self.directory / "registry.json"
        if not path.exists():
            return []
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RegistryCorrupt("registry.json is unreadable.") from exc
        return _parse_registry(raw)

    def _publish(self, records: list[dict[str, Any]]) -> None:
        ordered = sorted(records, key=lambda row: (row["registered_at"], row["id"]))
        document = {"schema_version": 1, "workspaces": [_stored(record) for record in ordered]}
        payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        atomic_replace(self.directory / "registry.json", payload)


def _prepare_register(
    path: str, name: str | None, tool: dict[str, Any]
) -> tuple[str, str, dict[str, str]]:
    if not isinstance(path, str) or "\x00" in path or not PurePosixPath(path).is_absolute():
        raise NotAWorkspace("Workspace path is not absolute.")
    stored = str(Path(path).resolve())
    if not PurePosixPath(stored).is_absolute():
        raise NotAWorkspace("Workspace path is not absolute.")
    if name is None:
        chosen = Path(stored).name
    elif isinstance(name, str) and 1 <= len(name) <= 200:
        chosen = name
    else:
        raise InvalidRecord("Workspace name must be 1 to 200 characters.")
    if not chosen:
        raise InvalidRecord("Workspace name must be 1 to 200 characters.")
    return stored, chosen, _normalize_tool(tool)


def _normalize_tool(tool: dict[str, Any]) -> dict[str, str]:
    if not isinstance(tool, dict) or set(tool) - _TOOL_KEYS or "name" not in tool:
        raise InvalidRecord("Tool name is invalid.")
    tool_name = tool["name"]
    if (
        not isinstance(tool_name, str)
        or not _TOOL_NAME.fullmatch(tool_name)
        or not 1 <= len(tool_name) <= 80
    ):
        raise InvalidRecord("Tool name is invalid.")
    stored: dict[str, str] = {"name": tool_name}
    if "version" in tool:
        version = tool["version"]
        if not isinstance(version, str) or len(version) > 40:
            raise InvalidRecord("Tool version is invalid.")
        stored["version"] = version
    return stored


def _is_workspace(path: str) -> bool:
    return (Path(path) / ".agentflow" / "config.yaml").is_file()


def _parse_registry(raw: bytes) -> list[dict[str, Any]]:
    if raw.strip() == b"":
        raise RegistryCorrupt("registry.json is empty.")
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryCorrupt("registry.json is not valid JSON.") from exc
    if not isinstance(parsed, dict) or set(parsed) != _TOP_KEYS:
        raise RegistryCorrupt("registry.json has the wrong shape.")
    version = parsed.get("schema_version")
    # bool is a subclass of int, and True == 1. Only a real JSON integer 1 matches.
    if type(version) is not int or version != 1 or not isinstance(parsed.get("workspaces"), list):
        raise RegistryCorrupt("registry.json has the wrong shape.")
    records: list[dict[str, Any]] = []
    for item in parsed["workspaces"]:
        records.append(_parse_workspace(item))
    return records


def _parse_workspace(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) - _WORKSPACE_KEYS or set(_WORKSPACE_KEYS) - set(item):
        raise RegistryCorrupt("registry.json has the wrong shape.")
    record: dict[str, Any] = {}
    for key in _STRING_FIELDS:
        value = item[key]
        if not isinstance(value, str) or value == "":
            raise RegistryCorrupt("registry.json has the wrong shape.")
        record[key] = value
    tool = item["tool"]
    if not isinstance(tool, dict) or set(tool) - _TOOL_KEYS or "name" not in tool:
        raise RegistryCorrupt("registry.json has the wrong shape.")
    if not 1 <= len(record["name"]) <= 200:
        raise RegistryCorrupt("registry.json has the wrong shape.")
    if not _absolute_registry_path(record["path"]):
        raise RegistryCorrupt("registry.json has the wrong shape.")
    if record["id"] != workspace_id_for(record["path"]):
        raise RegistryCorrupt("registry.json has the wrong shape.")
    if not _is_timestamp(record["registered_at"]) or not _is_timestamp(record["updated_at"]):
        raise RegistryCorrupt("registry.json has the wrong shape.")
    tool_name = tool["name"]
    if (
        not isinstance(tool_name, str)
        or not _TOOL_NAME.fullmatch(tool_name)
        or not 1 <= len(tool_name) <= 80
    ):
        raise RegistryCorrupt("registry.json has the wrong shape.")
    stored_tool: dict[str, str] = {"name": tool_name}
    if "version" in tool:
        version = tool["version"]
        if not isinstance(version, str) or len(version) > 40:
            raise RegistryCorrupt("registry.json has the wrong shape.")
        stored_tool["version"] = version
    record["tool"] = stored_tool
    return record


def _absolute_registry_path(path: str) -> bool:
    if "\x00" in path:
        return False
    try:
        return PurePosixPath(path).is_absolute()
    except ValueError:
        return False


def _is_timestamp(value: str) -> bool:
    if _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _find(records: list[dict[str, Any]], workspace_id: str) -> dict[str, Any]:
    for record in records:
        if record["id"] == workspace_id:
            return record
    raise WorkspaceNotFound(workspace_id)


def _stored(record: dict[str, Any]) -> dict[str, Any]:
    tool = {"name": record["tool"]["name"]}
    if "version" in record["tool"]:
        tool["version"] = record["tool"]["version"]
    return {
        "id": record["id"],
        "name": record["name"],
        "path": record["path"],
        "registered_at": record["registered_at"],
        "updated_at": record["updated_at"],
        "tool": tool,
    }


def _public(record: dict[str, Any]) -> dict[str, Any]:
    body = _stored(record)
    body["available"] = _is_workspace(record["path"])
    return body
