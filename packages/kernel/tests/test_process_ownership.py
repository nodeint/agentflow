from __future__ import annotations

from dataclasses import dataclass, field
import errno
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_kernel.command_executor import TERMINATION_GRACE_SEC
from agentflow_kernel.process_ownership import (
    ERROR_ALREADY_EXITED,
    ERROR_BOOT_MISMATCH,
    ERROR_REAPED,
    IDENTITY_FILENAME,
    PID_GONE,
    PID_PRESENT,
    PID_UNVERIFIED,
    REAP_ALREADY_EXITED,
    REAP_BOOT_MISMATCH,
    REAP_REAPED,
    REAP_STILL_RUNNING,
    REAP_UNVERIFIED,
    classify_orphaned_execution,
    wrapper_argv,
)


SENTINEL_SCRIPT = """
import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while not Path(sys.argv[2]).exists():
    time.sleep(0.01)
"""


@dataclass
class FakeInspector:
    hostname_value: str = "host.example"
    boot_id_value: Optional[str] = "boot-1"
    probes: Dict[int, str] = field(default_factory=dict)
    identities: Dict[int, Optional[str]] = field(default_factory=dict)
    pgids: Dict[int, Optional[int]] = field(default_factory=dict)
    lock_pid: Optional[int] = 4242
    identity_payload: Optional[Dict[str, Any]] = None
    missing_identity_file: bool = False
    killed: list[tuple[int, int]] = field(default_factory=list)

    def hostname(self) -> str:
        return self.hostname_value

    def boot_id(self) -> Optional[str]:
        return self.boot_id_value

    def probe_pid(self, pid: int) -> str:
        return self.probes.get(pid, PID_UNVERIFIED)

    def kernel_process_identity(self, pid: int) -> Optional[str]:
        return self.identities.get(pid)

    def getpgid(self, pid: int) -> Optional[int]:
        return self.pgids.get(pid)

    def identity_lock_holder(self, path: Path) -> Optional[int]:
        del path
        if self.missing_identity_file:
            return None
        return self.lock_pid

    def read_identity_file(self, path: Path) -> Optional[Dict[str, Any]]:
        del path
        if self.missing_identity_file:
            return None
        return self.identity_payload

    def killpg(self, pgid: int, sig: int) -> None:
        self.killed.append((pgid, sig))


def _inspector(*, owner_alive: bool, provider_probe: str) -> FakeInspector:
    return FakeInspector(
        probes={
            111: PID_PRESENT if owner_alive else PID_GONE,
            4242: provider_probe,
        },
        identities={111: "owner-key", 4242: "provider-key"},
        pgids={4242: 4242},
        identity_payload={
            "token": "aa" * 16,
            "pid": 4242,
            "pgid": 4242,
            "start_key": "provider-key",
        },
    )


def _metadata(**extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "run_id": "run-1",
        "execution_id": "0001-implement--attempt-01",
        "status": "running",
        "hostname": "host.example",
        "boot_id": "boot-1",
        "owner_pid": 111,
        "owner_start_key": "owner-key",
        "provider_pid": 4242,
        "provider_pgid": 4242,
        "provider_start_key": "provider-key",
        "identity_path": IDENTITY_FILENAME,
        "identity_token": "aa" * 16,
    }
    payload.update(extra)
    return payload


def _wait_until(predicate: Any, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout_sec}s")


def _process_group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


@unittest.skipUnless(os.name == "posix", "POSIX process ownership")
class WrapperDeathPipeTests(unittest.TestCase):
    def test_closed_death_pipe_before_exec_does_not_start_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sentinel = tmp_path / "sentinel"
            release = tmp_path / "release"
            identity_path = tmp_path / IDENTITY_FILENAME
            death_r, death_w = os.pipe()
            os.close(death_w)
            process = subprocess.Popen(
                wrapper_argv(
                    [sys.executable, "-c", SENTINEL_SCRIPT, str(sentinel), str(release)],
                    death_fd=death_r,
                    identity_path=identity_path,
                    token="ab" * 16,
                    grace_sec=TERMINATION_GRACE_SEC,
                ),
                start_new_session=True,
                close_fds=True,
                pass_fds=(death_r,),
            )
            os.close(death_r)
            self.assertEqual(process.wait(timeout=2), 1)
            self.assertFalse(sentinel.exists())

    def test_owner_death_after_start_exits_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sentinel = tmp_path / "sentinel"
            release = tmp_path / "release"
            identity_path = tmp_path / IDENTITY_FILENAME
            death_r, death_w = os.pipe()
            fcntl.fcntl(death_w, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
            process = subprocess.Popen(
                wrapper_argv(
                    [sys.executable, "-c", SENTINEL_SCRIPT, str(sentinel), str(release)],
                    death_fd=death_r,
                    identity_path=identity_path,
                    token="ab" * 16,
                    grace_sec=0.5,
                ),
                start_new_session=True,
                close_fds=True,
                pass_fds=(death_r,),
            )
            os.close(death_r)
            try:
                _wait_until(lambda: sentinel.exists(), timeout_sec=2)
                os.close(death_w)
                death_w = -1
                _wait_until(lambda: process.poll() is not None, timeout_sec=2)
                self.assertIsNotNone(process.poll())
                _wait_until(lambda: _process_group_gone(process.pid), timeout_sec=2)
                self.assertTrue(_process_group_gone(process.pid))
            finally:
                if death_w >= 0:
                    os.close(death_w)
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=2)


class ReaperClassificationTests(unittest.TestCase):
    def test_dead_owner_and_gone_provider_already_exited(self) -> None:
        inspector = _inspector(owner_alive=False, provider_probe=PID_GONE)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / IDENTITY_FILENAME).write_text("{}", encoding="utf-8")
            decision = classify_orphaned_execution(_metadata(), directory, inspector)
        self.assertEqual(decision.action, REAP_ALREADY_EXITED)
        self.assertEqual(decision.error, ERROR_ALREADY_EXITED)
        self.assertEqual(inspector.killed, [])

    def test_matching_identity_is_reaped(self) -> None:
        inspector = _inspector(owner_alive=False, provider_probe=PID_PRESENT)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / IDENTITY_FILENAME).write_text("{}", encoding="utf-8")
            decision = classify_orphaned_execution(_metadata(), directory, inspector)
        self.assertEqual(decision.action, REAP_REAPED)
        self.assertEqual(decision.error, ERROR_REAPED)

    def test_live_owner_is_still_running(self) -> None:
        inspector = _inspector(owner_alive=True, provider_probe=PID_PRESENT)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / IDENTITY_FILENAME).write_text("{}", encoding="utf-8")
            decision = classify_orphaned_execution(_metadata(), directory, inspector)
        self.assertEqual(decision.action, REAP_STILL_RUNNING)
        self.assertEqual(inspector.killed, [])

    def test_boot_mismatch_does_not_kill(self) -> None:
        inspector = _inspector(owner_alive=False, provider_probe=PID_PRESENT)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / IDENTITY_FILENAME).write_text("{}", encoding="utf-8")
            decision = classify_orphaned_execution(
                _metadata(boot_id="boot-previous"), directory, inspector
            )
        self.assertEqual(decision.action, REAP_BOOT_MISMATCH)
        self.assertEqual(decision.error, ERROR_BOOT_MISMATCH)
        self.assertEqual(inspector.killed, [])

    def test_unverified_live_provider_does_not_kill(self) -> None:
        mutations = {
            "identity-mismatch": lambda inspector: inspector.identities.__setitem__(
                4242, "other-start-key"
            ),
            "missing-identity-file": lambda inspector: setattr(
                inspector, "missing_identity_file", True
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                inspector = _inspector(owner_alive=False, provider_probe=PID_PRESENT)
                mutate(inspector)
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    if label != "missing-identity-file":
                        (directory / IDENTITY_FILENAME).write_text(
                            "{}", encoding="utf-8"
                        )
                    decision = classify_orphaned_execution(
                        _metadata(), directory, inspector
                    )
                self.assertEqual(decision.action, REAP_UNVERIFIED)
                self.assertEqual(inspector.killed, [])

    def test_reused_owner_pid_is_treated_as_dead(self) -> None:
        inspector = _inspector(owner_alive=True, provider_probe=PID_PRESENT)
        inspector.identities[111] = "reused-owner-key"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / IDENTITY_FILENAME).write_text("{}", encoding="utf-8")
            decision = classify_orphaned_execution(_metadata(), directory, inspector)
        self.assertEqual(decision.action, REAP_REAPED)
        self.assertEqual(inspector.killed, [])


if __name__ == "__main__":
    unittest.main()
