from __future__ import annotations

import errno
import fcntl
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agentflow_kernel.process_wrapper import fcntl_flock


class RegistryBusy(Exception):
    pass


class StartLockBusy(Exception):
    pass


class LockNotHeld(Exception):
    pass


_guard = threading.Lock()
_process_locks: dict[str, threading.Lock] = {}
_local = threading.local()

_CONTENTION = {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}


def lock_key(lock_path: Path) -> str:
    return str(Path(lock_path).resolve())


def process_lock_for(lock_path: Path) -> threading.Lock:
    key = lock_key(lock_path)
    with _guard:
        lock = _process_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _process_locks[key] = lock
        return lock


def _held_paths() -> set[str]:
    paths = getattr(_local, "paths", None)
    if paths is None:
        paths = set()
        _local.paths = paths
    return paths


def thread_holds(lock_path: Path) -> bool:
    return lock_key(lock_path) in _held_paths()


def _acquire_process_lock(lock: threading.Lock, deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return lock.acquire(blocking=False)
    return lock.acquire(timeout=remaining)


def _acquire_record_lock(
    fd: int,
    lock_type: int,
    deadline: float,
    busy_error: Callable[[], BaseException],
) -> None:
    contended = False
    eintr = 0
    while True:
        remaining = deadline - time.monotonic()
        if contended and remaining <= 0:
            raise busy_error()
        try:
            fcntl_flock(fd, fcntl.F_SETLK, lock_type)
            return
        except OSError as exc:
            if exc.errno == errno.EINTR:
                eintr += 1
                if eintr > 100 and remaining <= 0:
                    raise busy_error() from exc
                continue
            if exc.errno in _CONTENTION:
                contended = True
                now = deadline - time.monotonic()
                if now <= 0:
                    raise busy_error() from exc
                time.sleep(min(0.05, now))
                continue
            raise


@contextmanager
def interprocess_lock(
    lock_path: Path,
    *,
    shared: bool,
    timeout_sec: float,
    busy_error: Callable[[], BaseException],
) -> Iterator[None]:
    path = Path(lock_path)
    process_lock = process_lock_for(path)
    key = lock_key(path)
    deadline = time.monotonic() + timeout_sec
    fd: int | None = None
    got_process = False
    got_record = False
    added = False
    try:
        got_process = _acquire_process_lock(process_lock, deadline)
        if not got_process:
            raise busy_error()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags, 0o600)
        os.chmod(path, 0o600)
        lock_type = fcntl.F_RDLCK if shared else fcntl.F_WRLCK
        _acquire_record_lock(fd, lock_type, deadline, busy_error)
        got_record = True
        _held_paths().add(key)
        added = True
        yield
    finally:
        if got_record and fd is not None:
            try:
                fcntl_flock(fd, fcntl.F_SETLK, fcntl.F_UNLCK)
            except OSError:
                pass
        if fd is not None:
            os.close(fd)
        if added:
            _held_paths().discard(key)
        if got_process:
            process_lock.release()


def registry_busy() -> RegistryBusy:
    return RegistryBusy("Timed out waiting for the registry lock.")


def start_lock_busy() -> StartLockBusy:
    return StartLockBusy("Timed out waiting for start.lock.")


@contextmanager
def hold_start_lock(home: Path, timeout_sec: float) -> Iterator[None]:
    with interprocess_lock(
        Path(home) / "start.lock",
        shared=False,
        timeout_sec=timeout_sec,
        busy_error=start_lock_busy,
    ):
        yield
