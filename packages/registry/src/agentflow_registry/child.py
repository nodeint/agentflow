"""Child process that serves the registry after the parent publishes."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .home import ensure_registry_home
from .identity import acquire_service_lock
from .server import RegistryHTTPServer
from .store import RegistryStore

_ACK = b"published\n"
ACK_TIMEOUT_SEC = 5.0


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
