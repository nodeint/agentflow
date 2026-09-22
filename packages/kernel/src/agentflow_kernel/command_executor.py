from __future__ import annotations

import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from .base_adapter import CommandSpec
from .process_ownership import (
    IDENTITY_LOCK_WAIT_SEC,
    ProcessStartInfo,
    collect_process_start_info,
    wait_for_identity_lock,
    wrapper_argv,
)
from .runtime import log

DEFAULT_TIMEOUT_SEC: Optional[float] = None
PROGRESS_INTERVAL_SEC = 30
TERMINATION_GRACE_SEC = 5


class CancellationRequested(Exception):
    def __init__(self, signal_number: int) -> None:
        super().__init__(f"Received signal {signal_number}.")


class CommandExecutor:
    def __init__(
        self,
        timeout_sec: Optional[float] = DEFAULT_TIMEOUT_SEC,
        progress_interval_sec: int = PROGRESS_INTERVAL_SEC,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.progress_interval_sec = progress_interval_sec

    def run(
        self,
        spec: CommandSpec,
        cwd: Path,
        on_output: Optional[Callable[[str], None]] = None,
        on_heartbeat: Optional[Callable[[int], None]] = None,
        on_start: Optional[Callable[[ProcessStartInfo], None]] = None,
        identity_path: Optional[Path] = None,
        identity_token: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        death_w: Optional[int] = None
        process: Optional[subprocess.Popen[str]] = None
        temp_identity: Optional[Path] = None
        try:
            if os.name != "posix":
                process = self._start_windows(spec, cwd)
                if process is None:
                    missing = spec.argv[0] if spec.argv else "command"
                    return "", f"CLI executable not found: {missing}"
                return self._wait(process, spec, on_output, on_heartbeat)

            token = identity_token or os.urandom(16).hex()
            if identity_path is None:
                handle = tempfile.NamedTemporaryFile(
                    prefix="agentflow_identity_", delete=False
                )
                handle.close()
                identity_path = Path(handle.name)
                temp_identity = identity_path
            death_r, death_w = os.pipe()
            import fcntl

            fcntl.fcntl(death_w, fcntl.F_SETFD, fcntl.FD_CLOEXEC)
            try:
                process = self._start_posix(
                    spec, cwd, death_r=death_r, identity_path=identity_path, token=token
                )
            finally:
                os.close(death_r)
            if process is None:
                missing = spec.argv[0] if spec.argv else "command"
                return "", f"CLI executable not found: {missing}"
            locked = wait_for_identity_lock(
                identity_path, process.pid, IDENTITY_LOCK_WAIT_SEC
            )
            if process.poll() is not None and not locked:
                if process.returncode == 127:
                    return "", f"CLI executable not found: {spec.argv[0]}"
            elif not locked:
                stop_process_group(process)
                return "", "Provider process did not acquire the identity lock."
            elif on_start is not None:
                on_start(
                    collect_process_start_info(process.pid, identity_path, token)
                )
            stdout, error = self._wait(process, spec, on_output, on_heartbeat)
            if error is not None and process.returncode == 127:
                return stdout, f"CLI executable not found: {spec.argv[0]}"
            return stdout, error
        except CancellationRequested:
            log("Cancellation requested; stopping the sub-agent group.")
            if process is not None:
                stop_process_group(process)
            log("Sub-agent group stopped.")
            raise
        finally:
            if death_w is not None:
                try:
                    os.close(death_w)
                except OSError:
                    pass
            if temp_identity is not None:
                try:
                    temp_identity.unlink()
                except OSError:
                    pass

    def _wait(
        self,
        process: subprocess.Popen[str],
        spec: CommandSpec,
        on_output: Optional[Callable[[str], None]],
        on_heartbeat: Optional[Callable[[int], None]],
    ) -> Tuple[str, Optional[str]]:
        started_at = time.monotonic()
        output_lines: "queue.Queue[Optional[str]]" = queue.Queue()
        output_thread = threading.Thread(
            target=self._read_output,
            args=(process, output_lines),
            daemon=True,
        )
        output_thread.start()
        if process.stdin is not None:
            process.stdin.write(spec.stdin or "")
            process.stdin.close()
        stdout_chunks = []
        output_closed = False
        while True:
            wait_timeout = self.progress_interval_sec
            if self.timeout_sec is not None:
                remaining_sec = self.timeout_sec - (time.monotonic() - started_at)
                if remaining_sec <= 0:
                    return self._timeout(process)
                wait_timeout = min(wait_timeout, remaining_sec)
            if output_closed:
                try:
                    process.wait(timeout=wait_timeout)
                    break
                except subprocess.TimeoutExpired:
                    line = None
            else:
                try:
                    line = output_lines.get(timeout=wait_timeout)
                except queue.Empty:
                    line = ""
            if line == "":
                elapsed_sec = int(time.monotonic() - started_at)
                if on_heartbeat:
                    on_heartbeat(elapsed_sec)
                continue
            if line is None:
                output_closed = True
                continue
            stdout_chunks.append(line)
            if on_output:
                on_output(line.rstrip("\r\n"))

        process.wait()
        output_thread.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        stdout = "".join(stdout_chunks).strip()
        if process.returncode == 0:
            return stdout, None
        error = f"CLI exited with code {process.returncode}."
        return stdout, error

    def _read_output(
        self, process: subprocess.Popen[str], output_lines: "queue.Queue[Optional[str]]"
    ) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                output_lines.put(line)
        except ValueError:
            pass
        finally:
            output_lines.put(None)

    def _start_posix(
        self,
        spec: CommandSpec,
        cwd: Path,
        *,
        death_r: int,
        identity_path: Path,
        token: str,
    ) -> Optional[subprocess.Popen[str]]:
        argv = wrapper_argv(
            spec.argv,
            death_fd=death_r,
            identity_path=identity_path,
            token=token,
            grace_sec=TERMINATION_GRACE_SEC,
        )
        try:
            return subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if spec.stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                cwd=str(cwd),
                start_new_session=True,
                close_fds=True,
                pass_fds=(death_r,),
            )
        except FileNotFoundError:
            return None

    def _start_windows(
        self, spec: CommandSpec, cwd: Path
    ) -> Optional[subprocess.Popen[str]]:
        try:
            return subprocess.Popen(
                spec.argv,
                stdin=subprocess.PIPE if spec.stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                cwd=str(cwd),
                start_new_session=False,
            )
        except FileNotFoundError:
            return None

    def _timeout(self, process: subprocess.Popen[str]) -> Tuple[str, str]:
        assert self.timeout_sec is not None
        stop_process_group(process)
        error = f"Sub-agent timed out after {self.timeout_sec}s."
        return "", error


def raise_cancellation(signal_number: int, _frame: Any) -> None:
    raise CancellationRequested(signal_number)


def stop_process_group(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SEC)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        process.wait()
    if process.stdout is not None:
        process.stdout.close()
