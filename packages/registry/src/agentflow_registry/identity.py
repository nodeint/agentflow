"""Published service.json identity."""

from __future__ import annotations

import errno
import fcntl
import http.client
import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentflow_kernel.process_wrapper import (
    current_boot_id,
    fcntl_flock,
    identity_lock_holder,
    kernel_process_identity,
)

from .locks import LockNotHeld, thread_holds
from .store import atomic_replace

_SERVICE_KEYS = {
    "schema_version",
    "instance_id",
    "pid",
    "pgid",
    "start_key",
    "boot_id",
    "hostname",
    "host",
    "port",
    "token",
    "started_at",
}
CORRUPT = object()


class ServiceCorrupt(Exception):
    pass


class LiveInspector:
    def boot_id(self) -> str | None:
        return current_boot_id()

    def kernel_process_identity(self, pid: int) -> str | None:
        return kernel_process_identity(pid)

    def lock_holder(self, path: Path) -> int | None:
        return identity_lock_holder(str(path))

    def pgid(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    def hostname(self) -> str | None:
        try:
            name = socket.gethostname()
        except OSError:
            return None
        return name or None

    def pid_alive(self, pid: int) -> bool:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        if _reap_if_child(pid):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def service_json_path(home: Path) -> Path:
    return Path(home) / "service.json"


def service_lock_path(home: Path) -> Path:
    return Path(home) / "service.lock"


def start_lock_path(home: Path) -> Path:
    return Path(home) / "start.lock"


def service_identity_matches(record: dict[str, Any], inspector: Any, lock_path: Path) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    holder = inspector.lock_holder(lock_path)
    if holder is None or holder != pid:
        return False
    boot = inspector.boot_id()
    recorded_boot = record.get("boot_id")
    if not boot or not recorded_boot or boot != recorded_boot:
        return False
    start = inspector.kernel_process_identity(pid)
    recorded_start = record.get("start_key")
    if not start or not recorded_start or start != recorded_start:
        return False
    pgid = inspector.pgid(pid)
    if pgid is None or pgid != record.get("pgid"):
        return False
    host = inspector.hostname()
    recorded_host = record.get("hostname")
    if not host or not recorded_host or host != recorded_host:
        return False
    return True


def record_is_confirmed_gone(record: dict[str, Any], inspector: Any, lock_path: Path) -> bool:
    if inspector.lock_holder(lock_path) is not None:
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if not inspector.pid_alive(pid):
        return True
    boot = inspector.boot_id()
    recorded_boot = record.get("boot_id")
    if boot and recorded_boot and boot != recorded_boot:
        return True
    start = inspector.kernel_process_identity(pid)
    recorded_start = record.get("start_key")
    if start and recorded_start and start != recorded_start:
        return True
    return False


def write_service_json(
    home: Path, record: dict[str, Any], before_publish: Callable[[Path, dict[str, Any]], None]
) -> None:
    if not thread_holds(start_lock_path(home)):
        raise LockNotHeld("start.lock is not held.")
    before_publish(home, record)
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    atomic_replace(service_json_path(home), payload)


def remove_service_json_if_unchanged(
    home: Path,
    instance_id: str,
    pid: int,
    start_key: str,
    before_unlink: Callable[[Path, str, int, str], None],
) -> str:
    if not thread_holds(start_lock_path(home)):
        raise LockNotHeld("start.lock is not held.")
    before_unlink(home, instance_id, pid, start_key)
    path = service_json_path(home)
    if not path.exists():
        return "absent"
    try:
        record = parse_service_bytes(path.read_bytes())
    except (OSError, ServiceCorrupt):
        return "changed"
    if (
        record.get("instance_id") == instance_id
        and record.get("pid") == pid
        and record.get("start_key") == start_key
    ):
        try:
            os.unlink(path)
        except FileNotFoundError:
            return "absent"
        return "removed"
    return "changed"


def acquire_service_lock(home: Path) -> int | None:
    path = service_lock_path(home)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl_flock(fd, fcntl.F_SETLK, fcntl.F_WRLCK)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return fd


def authenticated_instance_id(port: int, token: str, timeout: float) -> str | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(
            "GET",
            "/v1/health",
            headers={"Authorization": f"Bearer {token}", "Connection": "close"},
        )
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    except (OSError, http.client.HTTPException, TimeoutError, ValueError):
        return None
    finally:
        connection.close()
    if status != 200:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    instance_id = payload.get("instance_id")
    if not isinstance(instance_id, str):
        return None
    return instance_id


def read_service(home: Path) -> dict[str, Any] | object | None:
    path = service_json_path(home)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return CORRUPT
    try:
        return parse_service_bytes(raw)
    except ServiceCorrupt:
        return CORRUPT


def parse_service_bytes(raw: bytes) -> dict[str, Any]:
    if raw.strip() == b"":
        raise ServiceCorrupt()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ServiceCorrupt() from exc
    if not isinstance(parsed, dict) or set(parsed) != _SERVICE_KEYS:
        raise ServiceCorrupt()
    if parsed["schema_version"] != 1:
        raise ServiceCorrupt()
    for key in ("instance_id", "start_key", "boot_id", "hostname", "host", "token", "started_at"):
        if not isinstance(parsed[key], str) or parsed[key] == "":
            raise ServiceCorrupt()
    for key in ("pid", "pgid", "port"):
        if isinstance(parsed[key], bool) or not isinstance(parsed[key], int):
            raise ServiceCorrupt()
    if not 1 <= parsed["port"] <= 65535:
        raise ServiceCorrupt()
    return parsed


def _reap_if_child(pid: int) -> bool:
    """Reap a zombie child so a dead daemon counts as gone to its parent."""
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return False
    return waited == pid
