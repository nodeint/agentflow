from __future__ import annotations

import argparse
import errno
import fcntl
import http.client
import json
import os
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentflow_kernel.process_wrapper import (
    current_boot_id,
    fcntl_flock,
    identity_lock_holder,
    kernel_process_identity,
)

from .home import ensure_registry_home
from .locks import (
    LockNotHeld,
    StartLockBusy,
    hold_start_lock,
    thread_holds,
)
from .server import DEFAULT_PORT, RegistryHTTPServer
from .store import RegistryStore, atomic_replace, utc_timestamp

START_LOCK_TIMEOUT_SEC = 8.0
IDENTITY_TIMEOUT_SEC = 5.0
READY_TIMEOUT_SEC = 5.0
HEALTH_TIMEOUT_SEC = 5.0
ACK_TIMEOUT_SEC = 5.0
TERM_GRACE_SEC = 5.0
KILL_GRACE_SEC = 5.0
_SECRET = re.compile(r"^[A-Za-z0-9_-]{43}$")
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
_READY_KEYS = {"pid", "port", "instance_id", "token"}
_CORRUPT = object()
_ACK = b"published\n"


class ServiceError(Exception):
    pass


class ServiceCorrupt(Exception):
    pass


class SignalBackendError(Exception):
    pass


@dataclass(frozen=True)
class ServiceEndpoint:
    host: str
    port: int
    token: str
    instance_id: str
    pid: int


@dataclass(frozen=True)
class StopResult:
    outcome: str
    message: str = ""


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


class LiveSignalBackend:
    def __init__(self) -> None:
        self.open = getattr(os, "pidfd_open", None)
        self.send = getattr(signal, "pidfd_send_signal", None)
        self.close = os.close
        self.kill = os.kill

    def supported(self) -> bool:
        return callable(self.open) and callable(self.send)


def service_json_path(home: Path) -> Path:
    return Path(home) / "service.json"


def service_lock_path(home: Path) -> Path:
    return Path(home) / "service.lock"


def start_lock_path(home: Path) -> Path:
    return Path(home) / "start.lock"


def before_service_json_publish(home: Path, record: dict[str, Any]) -> None:
    return None


def before_service_json_unlink(
    home: Path, instance_id: str, pid: int, start_key: str
) -> None:
    return None


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


def signal_verified_pid(
    pid: int,
    sig: int,
    still_matches: Callable[[], bool],
    backend: Any,
) -> None:
    if not backend.supported():
        _kill_if_still(pid, sig, still_matches, backend)
        return
    pidfd = -1
    try:
        pidfd = backend.open(pid)
    except OSError as exc:
        if exc.errno == errno.ENOSYS:
            _kill_if_still(pid, sig, still_matches, backend)
            return
        if exc.errno == errno.ESRCH:
            return
        raise SignalBackendError(f"pidfd_open failed with errno {exc.errno}.") from exc
    try:
        if not still_matches():
            return
        try:
            backend.send(pidfd, sig)
        except OSError as exc:
            if exc.errno == errno.ENOSYS:
                backend.close(pidfd)
                pidfd = -1
                _kill_if_still(pid, sig, still_matches, backend)
                return
            raise SignalBackendError(
                f"pidfd_send_signal failed with errno {exc.errno}."
            ) from exc
    finally:
        if pidfd >= 0:
            backend.close(pidfd)


def _kill_if_still(
    pid: int,
    sig: int,
    still_matches: Callable[[], bool],
    backend: Any,
) -> None:
    if still_matches():
        # A pid reissued after this check and before os.kill can still be signaled.
        # ESRCH means the checked process exited in that window.
        try:
            backend.kill(pid, sig)
        except ProcessLookupError:
            return


def publish_service_json(home: Path, record: dict[str, Any]) -> None:
    if not thread_holds(start_lock_path(home)):
        raise LockNotHeld("start.lock is not held.")
    before_service_json_publish(home, record)
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    atomic_replace(service_json_path(home), payload)


def unlink_service_json_if_unchanged(
    home: Path, instance_id: str, pid: int, start_key: str
) -> str:
    if not thread_holds(start_lock_path(home)):
        raise LockNotHeld("start.lock is not held.")
    before_service_json_unlink(home, instance_id, pid, start_key)
    path = service_json_path(home)
    if not path.exists():
        return "absent"
    try:
        record = _parse_service_bytes(path.read_bytes())
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


def call_service(
    host: str,
    port: int,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any]]:
    del host
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Connection": "close"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(data))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
    finally:
        connection.close()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return status, payload


def endpoint_from_home(home: Path) -> ServiceEndpoint | None:
    loaded = _read_service(Path(home))
    if not isinstance(loaded, dict):
        return None
    return _endpoint(loaded)


def service_status(home: Path, inspector: Any | None = None) -> dict[str, Any]:
    loaded = _read_service(home)
    if not isinstance(loaded, dict):
        return {"state": "stopped"}
    checker = inspector or LiveInspector()
    if not service_identity_matches(loaded, checker, service_lock_path(home)):
        return {"state": "stopped"}
    found = authenticated_instance_id(int(loaded["port"]), str(loaded["token"]), 2.0)
    if found != loaded["instance_id"]:
        return {"state": "stopped"}
    return {
        "state": "running",
        "host": loaded["host"],
        "port": loaded["port"],
        "pid": loaded["pid"],
    }


def start_detached(
    home: Path,
    port: int = DEFAULT_PORT,
    *,
    inspector: Any | None = None,
    backend: Any | None = None,
    start_lock_timeout: float = START_LOCK_TIMEOUT_SEC,
    ready_timeout: float = READY_TIMEOUT_SEC,
    health_timeout: float = HEALTH_TIMEOUT_SEC,
    identity_timeout: float = IDENTITY_TIMEOUT_SEC,
    term_grace_sec: float = TERM_GRACE_SEC,
    kill_grace_sec: float = KILL_GRACE_SEC,
) -> ServiceEndpoint:
    directory = ensure_registry_home(Path(home))
    checker = inspector or LiveInspector()
    signal_backend = backend or LiveSignalBackend()
    try:
        with hold_start_lock(directory, start_lock_timeout):
            decision = _decide(
                directory, checker, identity_timeout=identity_timeout
            )
            if decision[0] == "adopt":
                return decision[1]
            if decision[0] == "error":
                raise ServiceError(decision[1])
            return _spawn_and_publish(
                directory,
                port,
                signal_backend,
                ready_timeout=ready_timeout,
                health_timeout=health_timeout,
                term_grace_sec=term_grace_sec,
                kill_grace_sec=kill_grace_sec,
            )
    except StartLockBusy as exc:
        raise ServiceError(str(exc)) from exc


def serve_foreground(
    home: Path,
    port: int = DEFAULT_PORT,
    *,
    inspector: Any | None = None,
    start_lock_timeout: float = START_LOCK_TIMEOUT_SEC,
    identity_timeout: float = IDENTITY_TIMEOUT_SEC,
    stop_event: threading.Event | None = None,
) -> ServiceEndpoint:
    directory = ensure_registry_home(Path(home))
    checker = inspector or LiveInspector()
    server: RegistryHTTPServer | None = None
    lock_fd: int | None = None
    try:
        with hold_start_lock(directory, start_lock_timeout):
            decision = _decide(directory, checker, identity_timeout=identity_timeout)
            if decision[0] == "adopt":
                return decision[1]
            if decision[0] == "error":
                raise ServiceError(decision[1])
            lock_fd = acquire_service_lock(directory)
            if lock_fd is None:
                raise ServiceError("service.lock is held.")
            try:
                server, endpoint = _listening_server(directory, port)
                publish_service_json(directory, _record_from_endpoint(endpoint))
            except Exception:
                if server is not None:
                    _close_server_socket(server)
                if lock_fd is not None:
                    os.close(lock_fd)
                raise
    except StartLockBusy as exc:
        raise ServiceError(str(exc)) from exc
    assert server is not None and lock_fd is not None
    sys.stderr.write(f"{endpoint.host}:{endpoint.port}\n")
    serve_until_signal(server, lock_fd, stop_event=stop_event)
    return endpoint


def stop(
    home: Path,
    term_grace_sec: float = TERM_GRACE_SEC,
    kill_grace_sec: float = KILL_GRACE_SEC,
    *,
    inspector: Any | None = None,
    backend: Any | None = None,
    start_lock_timeout: float = START_LOCK_TIMEOUT_SEC,
) -> StopResult:
    directory = ensure_registry_home(Path(home))
    checker = inspector or LiveInspector()
    signal_backend = backend or LiveSignalBackend()
    try:
        with hold_start_lock(directory, start_lock_timeout):
            return _stop_locked(
                directory,
                checker,
                signal_backend,
                term_grace_sec=term_grace_sec,
                kill_grace_sec=kill_grace_sec,
            )
    except StartLockBusy as exc:
        return StopResult("error", str(exc))


def serve_until_signal(
    server: RegistryHTTPServer,
    lock_fd: int,
    stop_event: threading.Event | None = None,
) -> None:
    stop = {"flag": False}
    event = stop_event
    previous: list[tuple[signal.Signals, Any]] = []
    if event is None and threading.current_thread() is threading.main_thread():
        def handler(signum: int, frame: Any) -> None:
            del signum, frame
            stop["flag"] = True
            try:
                server.socket.close()
            except OSError:
                pass

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous.append((sig, signal.signal(sig, handler)))
    try:
        server.timeout = 0.2
        while not stop["flag"] and not (event is not None and event.is_set()):
            try:
                server.handle_request()
            except InterruptedError:
                continue
            except OSError:
                if stop["flag"] or (event is not None and event.is_set()):
                    break
                raise
    finally:
        for sig, old in previous:
            signal.signal(sig, old)
        _close_server_socket(server)
        if lock_fd >= 0:
            try:
                os.close(lock_fd)
            except OSError:
                pass


def launch_child(home: Path, port: int) -> tuple[subprocess.Popen[bytes], socket.socket]:
    directory = ensure_registry_home(Path(home))
    parent, child = socket.socketpair()
    parent.setblocking(True)
    child.setblocking(True)
    child.set_inheritable(True)
    log_path = directory / "service.log"
    log_file = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentflow_registry",
                "--child",
                "--home",
                str(directory),
                "--port",
                str(port),
                "--handshake-fd",
                str(child.fileno()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
            pass_fds=(child.fileno(),),
            cwd=str(directory),
        )
    except Exception:
        parent.close()
        child.close()
        log_file.close()
        raise
    finally:
        if "proc" in locals():
            child.close()
            log_file.close()
    return proc, parent


def read_ready_line(source: socket.socket | int, deadline: float) -> bytes | None:
    fd = source if isinstance(source, int) else source.fileno()
    return _read_line(fd, deadline)


def serve_child(home: Path, port: int, handshake_fd: int) -> int:
    """Serve after the parent publishes. This process never touches service.json."""
    if not _handshake_open(handshake_fd):
        return 2
    directory = ensure_registry_home(Path(home))
    lock_fd = acquire_service_lock(directory)
    if lock_fd is None:
        return 1
    try:
        sock = _bind_socket(port)
    except OSError:
        os.close(lock_fd)
        return 1
    stop = {"flag": False}
    previous = _arm_signals(stop, sock)
    try:
        instance_id = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(32)
        bound_port = sock.getsockname()[1]
        ready = json.dumps(
            {
                "pid": os.getpid(),
                "port": bound_port,
                "instance_id": instance_id,
                "token": token,
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            written = os.write(handshake_fd, ready)
        except OSError:
            return 1
        if written != len(ready):
            return 1
        if not _read_ack(handshake_fd, time.monotonic() + ACK_TIMEOUT_SEC, stop):
            return 0 if stop["flag"] else 1
        try:
            os.close(handshake_fd)
        except OSError:
            pass
        server = _server_around(sock, directory, token, instance_id)
        sock = None
        _serve_loop(server, stop)
        return 0
    finally:
        _restore_signals(previous)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass


def child_main(argv: list[str]) -> int:
    if "--child" not in argv:
        return 2
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--home")
    parser.add_argument("--port", type=int)
    parser.add_argument("--handshake-fd", type=int)
    try:
        args, extra = parser.parse_known_args(argv)
    except SystemExit:
        return 2
    if (
        extra
        or not args.child
        or args.home is None
        or args.port is None
        or args.handshake_fd is None
        or args.handshake_fd < 0
    ):
        return 2
    return serve_child(Path(args.home), args.port, args.handshake_fd)


def _decide(
    home: Path, inspector: Any, *, identity_timeout: float
) -> tuple[str, Any]:
    lock_path = service_lock_path(home)
    holder = inspector.lock_holder(lock_path)
    if holder is not None:
        deadline = time.monotonic() + identity_timeout
        while True:
            loaded = _read_service(home)
            if isinstance(loaded, dict) and service_identity_matches(
                loaded, inspector, lock_path
            ):
                remaining = max(0.05, deadline - time.monotonic())
                found = authenticated_instance_id(
                    int(loaded["port"]), str(loaded["token"]), min(0.5, remaining)
                )
                if found == loaded["instance_id"]:
                    return "adopt", _endpoint(loaded)
            if time.monotonic() >= deadline:
                return "error", "Service identity is unverified."
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    loaded = _read_service(home)
    if loaded is None or loaded is _CORRUPT or (
        isinstance(loaded, dict) and record_is_confirmed_gone(loaded, inspector, lock_path)
    ):
        return "serve", None
    if isinstance(loaded, dict) and _alive_without_lock(loaded, inspector, lock_path):
        return "error", "Service process is alive but does not hold service.lock."
    return "error", "Service identity is unverified."


def _alive_without_lock(record: dict[str, Any], inspector: Any, lock_path: Path) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not inspector.pid_alive(pid):
        return False
    start = inspector.kernel_process_identity(pid)
    boot = inspector.boot_id()
    if not start or start != record.get("start_key"):
        return False
    if not boot or boot != record.get("boot_id"):
        return False
    return inspector.lock_holder(lock_path) != pid


def _spawn_and_publish(
    home: Path,
    port: int,
    backend: Any,
    *,
    ready_timeout: float,
    health_timeout: float,
    term_grace_sec: float,
    kill_grace_sec: float,
) -> ServiceEndpoint:
    proc, parent = launch_child(home, port)
    # Keep this snapshot for cleanup. A later lookup can see a reused pid.
    measured = _measure_child(proc.pid)
    published: dict[str, Any] | None = None
    outcome: ServiceEndpoint | None = None
    error: BaseException | None = None
    try:
        try:
            line = read_ready_line(parent, time.monotonic() + ready_timeout)
            ready = _validate_ready(line, proc.pid, port, measured)
            if ready is None or measured is None:
                raise ServiceError("Registry service did not become ready.")
            record = _service_record(measured, ready)
            publish_service_json(home, record)
            published = record
            os.write(parent.fileno(), _ACK)
            if not _wait_until_healthy(home, record, time.monotonic() + health_timeout):
                raise ServiceError("Registry service did not become ready.")
            _detach_popen(proc)
            outcome = _endpoint(record)
        except BaseException as exc:
            error = exc
    finally:
        if outcome is None:
            try:
                gone = _stop_spawned_child(
                    home,
                    proc,
                    measured,
                    backend,
                    term_grace_sec=term_grace_sec,
                    kill_grace_sec=kill_grace_sec,
                )
            except Exception:
                gone = False
            if published is not None and gone:
                try:
                    unlink_service_json_if_unchanged(
                        home,
                        str(published["instance_id"]),
                        int(published["pid"]),
                        str(published["start_key"]),
                    )
                except Exception:
                    if error is None:
                        raise
        try:
            parent.close()
        except OSError:
            pass
        if proc.returncode is None:
            # Handshake close is not a signal. Reap the child if that close made it exit.
            _reap(proc, kill_grace_sec)
    if error is not None:
        raise error
    if outcome is None:
        raise ServiceError("The process is still alive.")
    return outcome


def _wait_until_healthy(home: Path, record: dict[str, Any], deadline: float) -> bool:
    inspector = LiveInspector()
    while True:
        holder = identity_lock_holder(str(service_lock_path(home)))
        matches = service_identity_matches(record, inspector, service_lock_path(home))
        found = authenticated_instance_id(int(record["port"]), str(record["token"]), 0.5)
        if holder == record["pid"] and matches and found == record["instance_id"]:
            on_disk = _read_service(home)
            if isinstance(on_disk, dict) and on_disk.get("instance_id") == record["instance_id"]:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _stop_spawned_child(
    home: Path,
    proc: subprocess.Popen[bytes],
    measured: dict[str, Any] | None,
    backend: Any,
    *,
    term_grace_sec: float,
    kill_grace_sec: float,
) -> bool:
    if measured is None or not measured.get("start_key"):
        # No start key was captured after Popen, so there is no identity to check.
        return False
    pid = int(measured["pid"])
    start_key = str(measured["start_key"])
    record = {
        "pid": pid,
        "start_key": start_key,
        "boot_id": measured.get("boot_id") or "",
        "pgid": measured.get("pgid"),
        "hostname": measured.get("hostname") or "",
    }

    def still() -> bool:
        ident = kernel_process_identity(pid)
        return bool(ident) and ident == start_key

    lock_path = service_lock_path(home)
    try:
        signal_verified_pid(pid, signal.SIGTERM, still, backend)
    except SignalBackendError:
        pass
    if _wait_gone(lock_path, record, term_grace_sec):
        _reap(proc, 0.2)
        return True
    try:
        signal_verified_pid(pid, signal.SIGKILL, still, backend)
    except SignalBackendError:
        _reap(proc, 0.2)
        return _wait_gone(lock_path, record, 0.0)
    gone = _wait_gone(lock_path, record, kill_grace_sec)
    _reap(proc, 0.2)
    return gone


def _wait_gone(lock_path: Path, record: dict[str, Any], grace: float) -> bool:
    inspector = LiveInspector()
    deadline = time.monotonic() + grace
    while True:
        if record_is_confirmed_gone(record, inspector, lock_path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _stop_locked(
    home: Path,
    inspector: Any,
    backend: Any,
    *,
    term_grace_sec: float,
    kill_grace_sec: float,
) -> StopResult:
    loaded = _read_service(home)
    if loaded is None:
        return StopResult("absent")
    if loaded is _CORRUPT:
        return StopResult("error", "service.json is corrupt.")
    record = loaded
    lock_path = service_lock_path(home)
    if not service_identity_matches(record, inspector, lock_path):
        if record_is_confirmed_gone(record, inspector, lock_path):
            return _unlink_confirmed(home, record)
        return StopResult("error", "Service identity is unverified.")

    def still() -> bool:
        return service_identity_matches(record, inspector, lock_path)

    try:
        signal_verified_pid(int(record["pid"]), signal.SIGTERM, still, backend)
    except SignalBackendError as exc:
        if record_is_confirmed_gone(record, inspector, lock_path):
            return _unlink_confirmed(home, record)
        return StopResult("error", str(exc))
    if _poll_gone(record, inspector, lock_path, term_grace_sec):
        return _unlink_confirmed(home, record)
    try:
        signal_verified_pid(int(record["pid"]), signal.SIGKILL, still, backend)
    except SignalBackendError as exc:
        if record_is_confirmed_gone(record, inspector, lock_path):
            return _unlink_confirmed(home, record)
        return StopResult("error", str(exc))
    if _poll_gone(record, inspector, lock_path, kill_grace_sec):
        return _unlink_confirmed(home, record)
    return StopResult("error", "The process is still alive.")


def _poll_gone(
    record: dict[str, Any], inspector: Any, lock_path: Path, grace: float
) -> bool:
    deadline = time.monotonic() + grace
    while True:
        if record_is_confirmed_gone(record, inspector, lock_path):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _unlink_confirmed(home: Path, record: dict[str, Any]) -> StopResult:
    result = unlink_service_json_if_unchanged(
        home, str(record["instance_id"]), int(record["pid"]), str(record["start_key"])
    )
    # removed and absent delete the stopped instance. changed leaves the
    # replacement in place; this stop is still finished.
    if result in {"removed", "absent", "changed"}:
        return StopResult("stopped")
    return StopResult("error", "service.json changed before it could be removed.")


def _listening_server(home: Path, port: int) -> tuple[RegistryHTTPServer, ServiceEndpoint]:
    sock = _bind_socket(port)
    try:
        instance_id = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(32)
        endpoint = _self_endpoint(sock.getsockname()[1], instance_id, token)
        if endpoint is None:
            raise ServiceError("Process identity is unavailable.")
        server = _server_around(sock, home, token, instance_id)
    except Exception:
        sock.close()
        raise
    return server, endpoint


def _self_endpoint(port: int, instance_id: str, token: str) -> ServiceEndpoint | None:
    start_key = kernel_process_identity(os.getpid())
    boot_id = current_boot_id()
    if not start_key or not boot_id:
        return None
    try:
        pgid = os.getpgid(os.getpid())
        hostname = socket.gethostname()
    except OSError:
        return None
    if not hostname:
        return None
    return ServiceEndpoint("127.0.0.1", port, token, instance_id, os.getpid())


def _record_from_endpoint(endpoint: ServiceEndpoint) -> dict[str, Any]:
    start_key = kernel_process_identity(os.getpid())
    boot_id = current_boot_id()
    if not start_key or not boot_id:
        raise ServiceError("Process identity is unavailable.")
    return {
        "schema_version": 1,
        "instance_id": endpoint.instance_id,
        "pid": os.getpid(),
        "pgid": os.getpgid(os.getpid()),
        "start_key": start_key,
        "boot_id": boot_id,
        "hostname": socket.gethostname(),
        "host": "127.0.0.1",
        "port": endpoint.port,
        "token": endpoint.token,
        "started_at": utc_timestamp(),
    }


def _service_record(measured: dict[str, Any], ready: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "instance_id": ready["instance_id"],
        "pid": measured["pid"],
        "pgid": measured["pgid"],
        "start_key": measured["start_key"],
        "boot_id": measured["boot_id"],
        "hostname": measured["hostname"],
        "host": "127.0.0.1",
        "port": ready["port"],
        "token": ready["token"],
        "started_at": utc_timestamp(),
    }


def _measure_child(pid: int) -> dict[str, Any] | None:
    try:
        pgid = os.getpgid(pid)
        hostname = socket.gethostname()
    except OSError:
        return None
    start_key = kernel_process_identity(pid)
    boot_id = current_boot_id()
    if not start_key or not boot_id or not hostname:
        return None
    return {
        "pid": pid,
        "pgid": pgid,
        "start_key": start_key,
        "boot_id": boot_id,
        "hostname": hostname,
    }


def _validate_ready(
    line: bytes | None,
    proc_pid: int,
    requested_port: int,
    measured: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if line is None or measured is None:
        return None
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _READY_KEYS:
        return None
    pid = payload["pid"]
    port = payload["port"]
    instance_id = payload["instance_id"]
    token = payload["token"]
    if isinstance(pid, bool) or pid != proc_pid:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if requested_port and port != requested_port:
        return None
    if not isinstance(instance_id, str) or _SECRET.fullmatch(instance_id) is None:
        return None
    if not isinstance(token, str) or _SECRET.fullmatch(token) is None:
        return None
    return {
        "pid": pid,
        "port": port,
        "instance_id": instance_id,
        "token": token,
    }


def _endpoint(record: dict[str, Any]) -> ServiceEndpoint:
    return ServiceEndpoint(
        str(record["host"]),
        int(record["port"]),
        str(record["token"]),
        str(record["instance_id"]),
        int(record["pid"]),
    )


def _read_service(home: Path) -> dict[str, Any] | object | None:
    path = service_json_path(home)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return _CORRUPT
    try:
        return _parse_service_bytes(raw)
    except ServiceCorrupt:
        return _CORRUPT


def _parse_service_bytes(raw: bytes) -> dict[str, Any]:
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


def _bind_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def _server_around(
    sock: socket.socket, home: Path, token: str, instance_id: str
) -> RegistryHTTPServer:
    host, bound_port = sock.getsockname()[:2]
    server = RegistryHTTPServer(
        RegistryStore(home),
        token,
        instance_id,
        "127.0.0.1",
        bound_port,
        bind_and_activate=False,
    )
    try:
        server.socket.close()
    except OSError:
        pass
    server.socket = sock
    server.server_address = (host, bound_port)
    return server


def _serve_loop(server: RegistryHTTPServer, stop: dict[str, bool]) -> None:
    server.timeout = 0.2
    while not stop["flag"]:
        try:
            server.handle_request()
        except InterruptedError:
            continue
        except OSError:
            if stop["flag"]:
                break
            raise
    _close_server_socket(server)


def _close_server_socket(server: RegistryHTTPServer) -> None:
    try:
        server.server_close()
    except OSError:
        pass


def _handshake_open(fd: int) -> bool:
    try:
        os.fstat(fd)
        fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        return False
    return True


def _arm_signals(stop: dict[str, bool], sock: socket.socket) -> list[tuple[Any, Any]]:
    previous: list[tuple[Any, Any]] = []

    def handler(signum: int, frame: Any) -> None:
        del signum, frame
        stop["flag"] = True
        try:
            sock.close()
        except OSError:
            pass

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous.append((sig, signal.signal(sig, handler)))
    return previous


def _restore_signals(previous: list[tuple[Any, Any]]) -> None:
    for sig, old in previous:
        signal.signal(sig, old)


def _read_ack(fd: int, deadline: float, stop: dict[str, bool]) -> bool:
    chunks = bytearray()
    while len(chunks) < len(_ACK):
        if stop["flag"]:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            readable, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        except InterruptedError:
            continue
        if stop["flag"]:
            return False
        if not readable:
            continue
        try:
            data = os.read(fd, len(_ACK) - len(chunks))
        except InterruptedError:
            continue
        except OSError:
            return False
        if not data:
            return False
        chunks.extend(data)
        if chunks != _ACK[: len(chunks)]:
            return False
    return bytes(chunks) == _ACK


def _read_line(fd: int, deadline: float, limit: int = 65536) -> bytes | None:
    chunks = bytearray()
    while b"\n" not in chunks:
        if len(chunks) >= limit:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            return None
        try:
            data = os.read(fd, 4096)
        except InterruptedError:
            continue
        except OSError:
            return None
        if not data:
            return None
        chunks.extend(data)
    line, sep, rest = bytes(chunks).partition(b"\n")
    if sep != b"\n" or rest:
        return None
    return line


def _reap_if_child(pid: int) -> bool:
    """Reap a zombie child so a dead daemon counts as gone to its parent."""
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    except OSError:
        return False
    return waited == pid


def _detach_popen(proc: subprocess.Popen[bytes]) -> None:
    # The child is a session leader and keeps serving after this client returns.
    proc.returncode = 0


def _reap(proc: subprocess.Popen[bytes], timeout: float) -> None:
    if proc.returncode is not None and proc.returncode != 0:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return


