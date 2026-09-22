from __future__ import annotations

import errno
import json
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from .process_wrapper import (
    current_boot_id,
    identity_lock_holder,
    kernel_process_identity,
)

IDENTITY_FILENAME = "process.identity"
IDENTITY_LOCK_WAIT_SEC = 2.0
PID_PRESENT = "present"
PID_GONE = "gone"
PID_UNVERIFIED = "unverified"
REAP_STILL_RUNNING = "still-running"
REAP_ALREADY_EXITED = "already-exited"
REAP_BOOT_MISMATCH = "boot-mismatch"
REAP_REAPED = "reaped"
REAP_UNVERIFIED = "unverified"
REAP_NOOP = "noop"
ERROR_REAPED = "Owner process is not running; provider process group reaped."
ERROR_ALREADY_EXITED = (
    "Owner process is not running; provider process already exited."
)
ERROR_BOOT_MISMATCH = (
    "Owner process is not running; recorded boot id does not match this boot."
)
WRAPPER_PATH = Path(__file__).resolve().parent / "process_wrapper.py"


@dataclass(frozen=True)
class ProcessStartInfo:
    hostname: str
    boot_id: Optional[str]
    owner_pid: int
    owner_start_key: Optional[str]
    provider_pid: int
    provider_pgid: int
    provider_start_key: Optional[str]
    identity_path: Path
    identity_token: str


@dataclass(frozen=True)
class ReapDecision:
    action: str
    run_id: str
    execution_id: str
    owner_pid: Optional[int] = None
    error: Optional[str] = None


class ProcessInspector(Protocol):
    def hostname(self) -> str: ...

    def boot_id(self) -> Optional[str]: ...

    def probe_pid(self, pid: int) -> str: ...

    def kernel_process_identity(self, pid: int) -> Optional[str]: ...

    def getpgid(self, pid: int) -> Optional[int]: ...

    def identity_lock_holder(self, path: Path) -> Optional[int]: ...

    def read_identity_file(self, path: Path) -> Optional[Dict[str, Any]]: ...

    def killpg(self, pgid: int, sig: int) -> None: ...


class PosixProcessInspector:
    def hostname(self) -> str:
        return socket.gethostname()

    def boot_id(self) -> Optional[str]:
        return current_boot_id()

    def probe_pid(self, pid: int) -> str:
        return probe_pid(pid)

    def kernel_process_identity(self, pid: int) -> Optional[str]:
        return kernel_process_identity(pid)

    def getpgid(self, pid: int) -> Optional[int]:
        try:
            return os.getpgid(pid)
        except OSError:
            return None

    def identity_lock_holder(self, path: Path) -> Optional[int]:
        return identity_lock_holder(str(path))

    def read_identity_file(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def killpg(self, pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)


def probe_pid(pid: int) -> str:
    try:
        os.kill(pid, 0)
        return PID_PRESENT
    except ProcessLookupError:
        return PID_GONE
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return PID_PRESENT
        if exc.errno == errno.ESRCH:
            return PID_GONE
        return PID_UNVERIFIED


def wait_for_identity_lock(path: Path, pid: int, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while True:
        if identity_lock_holder(str(path)) == pid:
            return True
        if probe_pid(pid) == PID_GONE:
            return False
        if time.monotonic() >= deadline:
            return identity_lock_holder(str(path)) == pid
        time.sleep(0.01)


def wrapper_argv(
    spec_argv: list[str],
    *,
    death_fd: int,
    identity_path: Path,
    token: str,
    grace_sec: float,
    python_executable: Optional[str] = None,
) -> list[str]:
    return [
        python_executable or sys.executable,
        str(WRAPPER_PATH),
        "--death-fd",
        str(death_fd),
        "--identity-path",
        str(identity_path),
        "--token",
        token,
        "--grace-sec",
        str(grace_sec),
        "--",
        *spec_argv,
    ]


def collect_process_start_info(
    process_pid: int,
    identity_path: Path,
    identity_token: str,
) -> ProcessStartInfo:
    try:
        provider_pgid = os.getpgid(process_pid)
    except OSError:
        provider_pgid = process_pid
    owner_pid = os.getpid()
    return ProcessStartInfo(
        hostname=socket.gethostname(),
        boot_id=current_boot_id(),
        owner_pid=owner_pid,
        owner_start_key=kernel_process_identity(owner_pid),
        provider_pid=process_pid,
        provider_pgid=provider_pgid,
        provider_start_key=kernel_process_identity(process_pid),
        identity_path=identity_path,
        identity_token=identity_token,
    )


def still_running_message(run_id: str, execution_id: str, owner_pid: int) -> str:
    return (
        f"Run {run_id} execution {execution_id} is still running under owner pid {owner_pid}."
    )


def unverified_process_message(run_id: str, execution_id: str) -> str:
    return (
        f"Run {run_id} execution {execution_id} has an unverified live provider process."
    )


def raise_reap_decision(decision: ReapDecision) -> None:
    if decision.action == REAP_STILL_RUNNING:
        owner_pid = decision.owner_pid if decision.owner_pid is not None else 0
        raise ValueError(
            still_running_message(decision.run_id, decision.execution_id, owner_pid)
        )
    if decision.action == REAP_UNVERIFIED:
        raise ValueError(
            unverified_process_message(decision.run_id, decision.execution_id)
        )


def classify_orphaned_execution(
    metadata: Dict[str, Any],
    execution_directory: Path,
    inspector: ProcessInspector,
) -> ReapDecision:
    run_id = str(metadata.get("run_id") or "")
    execution_id = str(metadata.get("execution_id") or execution_directory.name)
    if metadata.get("status") != "running":
        return ReapDecision(REAP_NOOP, run_id, execution_id)
    hostname = metadata.get("hostname")
    if isinstance(hostname, str) and hostname != inspector.hostname():
        return ReapDecision(REAP_UNVERIFIED, run_id, execution_id)
    owner_pid = _optional_int(metadata.get("owner_pid"))
    owner_state = _owner_state(metadata, owner_pid, inspector)
    if owner_state == "alive":
        return ReapDecision(
            REAP_STILL_RUNNING, run_id, execution_id, owner_pid=owner_pid
        )
    if owner_state in {"unreadable", "unverified", "missing"}:
        return ReapDecision(REAP_UNVERIFIED, run_id, execution_id, owner_pid=owner_pid)
    provider_pid = _optional_int(metadata.get("provider_pid"))
    if provider_pid is None:
        return ReapDecision(REAP_UNVERIFIED, run_id, execution_id, owner_pid=owner_pid)
    provider_probe = inspector.probe_pid(provider_pid)
    if provider_probe == PID_GONE:
        return ReapDecision(
            REAP_ALREADY_EXITED,
            run_id,
            execution_id,
            owner_pid=owner_pid,
            error=ERROR_ALREADY_EXITED,
        )
    if provider_probe != PID_PRESENT:
        return ReapDecision(REAP_UNVERIFIED, run_id, execution_id, owner_pid=owner_pid)
    stored_boot = metadata.get("boot_id")
    current_boot = inspector.boot_id()
    if (
        isinstance(stored_boot, str)
        and stored_boot
        and current_boot is not None
        and stored_boot != current_boot
    ):
        return ReapDecision(
            REAP_BOOT_MISMATCH,
            run_id,
            execution_id,
            owner_pid=owner_pid,
            error=ERROR_BOOT_MISMATCH,
        )
    if process_identity_matches(metadata, execution_directory, inspector):
        return ReapDecision(
            REAP_REAPED,
            run_id,
            execution_id,
            owner_pid=owner_pid,
            error=ERROR_REAPED,
        )
    return ReapDecision(REAP_UNVERIFIED, run_id, execution_id, owner_pid=owner_pid)


def process_identity_matches(
    metadata: Dict[str, Any],
    execution_directory: Path,
    inspector: ProcessInspector,
) -> bool:
    if metadata.get("hostname") != inspector.hostname():
        return False
    stored_boot = _present_str(metadata.get("boot_id"))
    current_boot = _present_str(inspector.boot_id())
    if stored_boot is None or stored_boot != current_boot:
        return False
    return identity_kill_checks_match(metadata, execution_directory, inspector)


def identity_kill_checks_match(
    metadata: Dict[str, Any],
    execution_directory: Path,
    inspector: ProcessInspector,
) -> bool:
    identity_path = _identity_path(metadata, execution_directory)
    if not identity_path.is_file():
        return False
    holder = inspector.identity_lock_holder(identity_path)
    provider_pid = _optional_int(metadata.get("provider_pid"))
    provider_pgid = _optional_int(metadata.get("provider_pgid"))
    provider_start_key = _present_str(metadata.get("provider_start_key"))
    token = metadata.get("identity_token")
    if holder is None or provider_pid is None or holder != provider_pid:
        return False
    if provider_start_key is None:
        return False
    payload = inspector.read_identity_file(identity_path)
    if payload is None:
        return False
    payload_start_key = _present_str(payload.get("start_key"))
    live_start_key = _present_str(inspector.kernel_process_identity(holder))
    if (
        payload.get("token") != token
        or _optional_int(payload.get("pid")) != provider_pid
        or _optional_int(payload.get("pgid")) != provider_pgid
        or payload_start_key != provider_start_key
        or live_start_key != provider_start_key
    ):
        return False
    if inspector.getpgid(holder) != provider_pgid:
        return False
    return True


def stop_verified_process_group(
    metadata: Dict[str, Any],
    execution_directory: Path,
    inspector: ProcessInspector,
    grace_sec: float,
) -> bool:
    if not identity_kill_checks_match(metadata, execution_directory, inspector):
        return False
    provider_pid = _optional_int(metadata.get("provider_pid"))
    provider_pgid = _optional_int(metadata.get("provider_pgid"))
    identity_path = _identity_path(metadata, execution_directory)
    if provider_pid is None or provider_pgid is None:
        return False
    try:
        inspector.killpg(provider_pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if inspector.probe_pid(provider_pid) == PID_GONE:
            return True
        if inspector.identity_lock_holder(identity_path) is None:
            return True
        time.sleep(0.05)
    if not identity_kill_checks_match(metadata, execution_directory, inspector):
        return False
    try:
        inspector.killpg(provider_pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    return True


def process_metadata_payload(info: ProcessStartInfo) -> Dict[str, Any]:
    return {
        "hostname": info.hostname,
        "boot_id": info.boot_id,
        "owner_pid": info.owner_pid,
        "owner_start_key": info.owner_start_key,
        "provider_pid": info.provider_pid,
        "provider_pgid": info.provider_pgid,
        "provider_start_key": info.provider_start_key,
        "identity_path": IDENTITY_FILENAME,
        "identity_token": info.identity_token,
    }


def _owner_state(
    metadata: Dict[str, Any],
    owner_pid: Optional[int],
    inspector: ProcessInspector,
) -> str:
    if owner_pid is None:
        return "missing"
    probe = inspector.probe_pid(owner_pid)
    if probe == PID_GONE:
        return "dead"
    if probe != PID_PRESENT:
        return "unverified"
    stored_key = _present_str(metadata.get("owner_start_key"))
    current_key = _present_str(inspector.kernel_process_identity(owner_pid))
    if stored_key is None or current_key is None:
        return "unreadable"
    if stored_key == current_key:
        return "alive"
    return "dead"


def _identity_path(metadata: Dict[str, Any], execution_directory: Path) -> Path:
    recorded = metadata.get("identity_path")
    if isinstance(recorded, str) and recorded:
        path = Path(recorded)
        if path.is_absolute():
            return path
        return execution_directory / path
    return execution_directory / IDENTITY_FILENAME


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _present_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    return None
