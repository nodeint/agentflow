from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agentflow_registry.home import ensure_registry_home
from agentflow_registry.service import hold_start_lock, launch_child, read_ready_line


def make_workspace(root: Path, name: str = "demo") -> Path:
    workspace = root / name
    config = workspace / ".agentflow"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return workspace


def service_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "instance_id": "instance-under-test",
        "pid": 424242,
        "pgid": 424242,
        "start_key": "start-key",
        "boot_id": "boot-1",
        "hostname": "host.example",
        "host": "127.0.0.1",
        "port": 47321,
        "token": "token-under-test",
        "started_at": "2026-09-26T08:07:57Z",
    }
    record.update(overrides)
    return record


def write_service(home: Path, record: dict[str, object]) -> None:
    ensure_registry_home(home)
    (home / "service.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


class ScriptedInspector:
    def __init__(self, record: dict[str, object]) -> None:
        self.record = record
        self.alive = True
        self.hold_lock = True
        self.start_key = record["start_key"]
        self.boot = record["boot_id"]

    def boot_id(self) -> str | None:
        return None if self.boot is None else str(self.boot)

    def kernel_process_identity(self, pid: int) -> str | None:
        if pid != self.record["pid"]:
            return "other-start"
        if self.start_key is None:
            return None
        return str(self.start_key)

    def lock_holder(self, path: Path) -> int | None:
        del path
        if not self.hold_lock:
            return None
        pid = self.record["pid"]
        return int(pid) if isinstance(pid, int) else None

    def pgid(self, pid: int) -> int | None:
        if pid != self.record["pid"]:
            return None
        pgid = self.record["pgid"]
        return int(pgid) if isinstance(pgid, int) else None

    def hostname(self) -> str | None:
        host = self.record["hostname"]
        return str(host) if isinstance(host, str) and host else None

    def pid_alive(self, pid: int) -> bool:
        return self.alive and pid == self.record["pid"]

    def mark_gone(self) -> None:
        self.alive = False
        self.hold_lock = False
        self.start_key = "gone-start"


class RecordingBackend:
    def __init__(self, *, supported: bool = False, open_errno: int | None = None, send_errno: int | None = None) -> None:
        self._supported = supported
        self.open_errno = open_errno
        self.send_errno = send_errno
        self.log: list[object] = []
        self._fd = 80

    def supported(self) -> bool:
        return self._supported

    def open(self, pid: int) -> int:
        self.log.append(("open", pid))
        if self.open_errno is not None:
            error = self.open_errno
            raise OSError(error, "pidfd_open")
        fd = self._fd
        self._fd += 1
        return fd

    def send(self, pidfd: int, sig: int) -> None:
        self.log.append(("send", pidfd, sig))
        if self.send_errno is not None:
            error = self.send_errno
            self.send_errno = None
            raise OSError(error, "pidfd_send_signal")

    def close(self, fd: int) -> None:
        self.log.append(("close", fd))

    def kill(self, pid: int, sig: int) -> None:
        self.log.append(("kill", pid, sig))


def hold_start_lock_after_child_ready(home: str, port: int, sentinel: str) -> None:
    """Hold start.lock after the child is ready and do not publish service.json."""
    home_path = ensure_registry_home(Path(home))
    with hold_start_lock(home_path, 8):
        _proc, parent = launch_child(home_path, port)
        line = read_ready_line(parent, time.monotonic() + 5)
        if not line:
            return
        payload = json.loads(line.decode("utf-8"))
        Path(sentinel).write_text(str(payload["pid"]), encoding="utf-8")
        time.sleep(120)


def reap(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(0.05)
