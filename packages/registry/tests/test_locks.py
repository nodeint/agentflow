from __future__ import annotations

import errno
import fcntl
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.locks import (
    LockNotHeld,
    RegistryBusy,
    interprocess_lock,
    process_lock_for,
    registry_busy,
    thread_holds,
)
from agentflow_registry.service import (
    publish_service_json,
    unlink_service_json_if_unchanged,
)


class LockTests(unittest.TestCase):
    def test_contention_uses_setlk_and_times_out(self) -> None:
        commands: list[int] = []
        sleeps: list[float] = []

        def fake_flock(fd: int, cmd: int, lock_type: int = fcntl.F_WRLCK) -> tuple[int, int, int, int, int]:
            del fd
            commands.append(cmd)
            if lock_type == fcntl.F_UNLCK:
                return (fcntl.F_UNLCK, 0, 0, 0, 0)
            raise OSError(errno.EAGAIN, "held")

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.lock"
            started = time.monotonic()
            with patch("agentflow_registry.locks.fcntl_flock", fake_flock), patch(
                "agentflow_registry.locks.time.sleep", fake_sleep
            ):
                with self.assertRaises(RegistryBusy):
                    with interprocess_lock(
                        path, shared=False, timeout_sec=0.2, busy_error=registry_busy
                    ):
                        pass
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(commands)
        self.assertTrue(all(cmd == fcntl.F_SETLK for cmd in commands))
        self.assertNotIn(fcntl.F_SETLKW, commands)
        self.assertTrue(sleeps)

    def test_lock_acquired_when_it_frees_before_the_deadline(self) -> None:
        commands: list[int] = []
        state = {"tries": 0}

        def fake_flock(fd: int, cmd: int, lock_type: int = fcntl.F_WRLCK) -> tuple[int, int, int, int, int]:
            del fd
            commands.append(cmd)
            if lock_type == fcntl.F_UNLCK:
                return (fcntl.F_UNLCK, 0, 0, 0, 0)
            state["tries"] += 1
            if state["tries"] < 3:
                raise OSError(errno.EAGAIN, "held")
            return (lock_type, 0, 0, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.lock"
            with patch("agentflow_registry.locks.fcntl_flock", fake_flock), patch(
                "agentflow_registry.locks.time.sleep", lambda _seconds: None
            ):
                with interprocess_lock(
                    path, shared=False, timeout_sec=1, busy_error=registry_busy
                ):
                    self.assertTrue(thread_holds(path))
                self.assertFalse(thread_holds(path))
        self.assertNotIn(fcntl.F_SETLKW, commands)
        self.assertGreaterEqual(state["tries"], 3)

    def test_timeout_zero_fails_on_the_first_eagain_without_sleeping(self) -> None:
        sleeps: list[float] = []

        def fake_flock(fd: int, cmd: int, lock_type: int = fcntl.F_WRLCK) -> tuple[int, int, int, int, int]:
            del fd, cmd
            if lock_type == fcntl.F_UNLCK:
                return (fcntl.F_UNLCK, 0, 0, 0, 0)
            raise OSError(errno.EAGAIN, "held")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.lock"
            started = time.monotonic()
            with patch("agentflow_registry.locks.fcntl_flock", fake_flock), patch(
                "agentflow_registry.locks.time.sleep", lambda seconds: sleeps.append(seconds)
            ):
                with self.assertRaises(RegistryBusy):
                    with interprocess_lock(
                        path, shared=False, timeout_sec=0, busy_error=registry_busy
                    ):
                        pass
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(sleeps, [])

    def test_process_lock_timeout_does_not_open_the_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.lock"
            holder = process_lock_for(path)
            self.assertTrue(holder.acquire(blocking=False))
            opened: list[str] = []
            real_open = os.open

            def spy_open(file: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
                opened.append(str(file))
                return real_open(file, flags, mode)

            try:
                with patch("agentflow_registry.locks.os.open", spy_open):
                    with self.assertRaises(RegistryBusy):
                        with interprocess_lock(
                            path, shared=False, timeout_sec=0.1, busy_error=registry_busy
                        ):
                            pass
            finally:
                holder.release()
            self.assertEqual(opened, [])

    def test_publish_and_unlink_outside_start_lock_raise_lock_not_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "service.json"
            target.write_text('{"keep":true}\n', encoding="utf-8")
            before = target.read_bytes()
            with self.assertRaises(LockNotHeld):
                publish_service_json(home, {"schema_version": 1})
            with self.assertRaises(LockNotHeld):
                unlink_service_json_if_unchanged(home, "instance", 1, "start")
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
