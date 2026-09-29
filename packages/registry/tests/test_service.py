from __future__ import annotations

import errno
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_kernel.process_wrapper import identity_lock_holder
from agentflow_registry.locks import hold_start_lock, thread_holds
from agentflow_registry.server import RegistryHTTPServer
from agentflow_registry.service import (
    LiveSignalBackend,
    ServiceEndpoint,
    ServiceError,
    acquire_service_lock,
    authenticated_instance_id,
    call_service,
    launch_child,
    read_ready_line,
    service_identity_matches,
    service_json_path,
    service_lock_path,
    service_status,
    signal_verified_pid,
    start_detached,
    start_lock_path,
    serve_foreground,
    stop,
)
from tests.support import (
    RecordingBackend,
    ScriptedInspector,
    reap,
    service_record,
    write_service,
)


POSIX = os.name == "posix"


class SignalTests(unittest.TestCase):
    def test_unsupported_backend_kills_only_after_a_true_recheck(self) -> None:
        backend = RecordingBackend(supported=False)
        pid = 42

        def still() -> bool:
            backend.log.append("recheck")
            return True

        signal_verified_pid(pid, signal.SIGTERM, still, backend)
        self.assertEqual(backend.log[0], "recheck")
        self.assertEqual(
            backend.log[1],
            ("kill", pid, signal.SIGTERM),
            "a pid reused after the re-check and before os.kill still receives the signal",
        )

    def test_exit_after_recheck_before_kill_is_not_an_error(self) -> None:
        backend = RecordingBackend(supported=False)

        def kill(pid: int, sig: int) -> None:
            backend.log.append(("kill", pid, sig))
            raise ProcessLookupError(errno.ESRCH, "No such process")

        backend.kill = kill  # type: ignore[method-assign]
        signal_verified_pid(42, signal.SIGTERM, lambda: True, backend)
        self.assertEqual(backend.log, [("kill", 42, signal.SIGTERM)])

    def test_false_recheck_sends_nothing(self) -> None:
        backend = RecordingBackend(supported=False)

        def still() -> bool:
            backend.log.append("recheck")
            return False

        signal_verified_pid(42, signal.SIGTERM, still, backend)
        self.assertEqual(backend.log, ["recheck"])

    def test_esrch_sends_nothing(self) -> None:
        backend = RecordingBackend(supported=True, open_errno=errno.ESRCH)
        signal_verified_pid(42, signal.SIGTERM, lambda: True, backend)
        self.assertEqual(backend.log, [("open", 42)])

    def test_open_then_false_recheck_closes_without_sending(self) -> None:
        backend = RecordingBackend(supported=True)

        def still() -> bool:
            backend.log.append("recheck")
            return False

        signal_verified_pid(42, signal.SIGTERM, still, backend)
        self.assertEqual(backend.log[0], ("open", 42))
        self.assertIn("recheck", backend.log)
        self.assertIn(("close", 80), backend.log)
        self.assertFalse(any(item[0] == "send" for item in backend.log if isinstance(item, tuple)))
        self.assertFalse(any(item[0] == "kill" for item in backend.log if isinstance(item, tuple)))

    def test_true_recheck_sends_on_that_pidfd(self) -> None:
        backend = RecordingBackend(supported=True)

        def still() -> bool:
            backend.log.append("recheck")
            return True

        signal_verified_pid(42, signal.SIGTERM, still, backend)
        self.assertIn(("send", 80, signal.SIGTERM), backend.log)
        self.assertLess(backend.log.index(("open", 42)), backend.log.index("recheck"))
        self.assertLess(backend.log.index("recheck"), backend.log.index(("send", 80, signal.SIGTERM)))

    def test_enosys_from_open_uses_the_recheck_fallback(self) -> None:
        backend = RecordingBackend(supported=True, open_errno=errno.ENOSYS)

        def still() -> bool:
            backend.log.append("recheck")
            return True

        signal_verified_pid(42, signal.SIGTERM, still, backend)
        self.assertEqual(backend.log[0], ("open", 42))
        self.assertIn("recheck", backend.log)
        self.assertIn(("kill", 42, signal.SIGTERM), backend.log)

    def test_sigkill_opens_a_new_pidfd(self) -> None:
        backend = RecordingBackend(supported=True)
        signal_verified_pid(42, signal.SIGTERM, lambda: True, backend)
        signal_verified_pid(42, signal.SIGKILL, lambda: True, backend)
        opens = [item for item in backend.log if isinstance(item, tuple) and item[0] == "open"]
        sends = [item for item in backend.log if isinstance(item, tuple) and item[0] == "send"]
        self.assertEqual(opens, [("open", 42), ("open", 42)])
        self.assertEqual(sends[0][1], 80)
        self.assertEqual(sends[1][1], 81)
        self.assertNotEqual(sends[0][1], sends[1][1])

    def test_unsupported_sigkill_rechecks_again(self) -> None:
        backend = RecordingBackend(supported=False)
        checks: list[int] = []

        def still() -> bool:
            checks.append(signal.SIGTERM if len(checks) == 0 else signal.SIGKILL)
            return True

        signal_verified_pid(42, signal.SIGTERM, still, backend)
        signal_verified_pid(42, signal.SIGKILL, still, backend)
        self.assertEqual(checks, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(
            [item for item in backend.log if isinstance(item, tuple)],
            [("kill", 42, signal.SIGTERM), ("kill", 42, signal.SIGKILL)],
        )

    @unittest.skipUnless(LiveSignalBackend().supported(), "pidfd is not available")
    def test_production_send_is_pidfd_send_signal(self) -> None:
        self.assertIs(LiveSignalBackend().send, signal.pidfd_send_signal)


@unittest.skipUnless(POSIX, "requires posix locks and processes")
class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "registry"
        self.home.mkdir()
        self._env = os.environ.get("AGENTFLOW_HOME")
        os.environ["AGENTFLOW_HOME"] = str(self._tmp.name)
        self._children: list[int] = []

    def tearDown(self) -> None:
        for pid in self._children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            reap(pid)
        if self._env is None:
            os.environ.pop("AGENTFLOW_HOME", None)
        else:
            os.environ["AGENTFLOW_HOME"] = self._env
        self._tmp.cleanup()

    def test_start_adopts_a_verified_instance_and_status_requires_auth(self) -> None:
        published: list[dict[str, object]] = []

        def observe(home: Path, record: dict[str, object]) -> None:
            self.assertTrue(thread_holds(start_lock_path(home)))
            current = service_json_path(home).read_text(encoding="utf-8") if service_json_path(home).exists() else ""
            self.assertNotIn(str(record["instance_id"]), current)
            published.append(record)

        with patch("agentflow_registry.service.before_service_json_publish", observe):
            endpoint = start_detached(self.home, 0, term_grace_sec=0.3, kill_grace_sec=0.3)
        self._children.append(endpoint.pid)
        path = service_json_path(self.home)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn(str(endpoint.pid), text)
        self.assertIn(endpoint.instance_id, text)
        self.assertTrue(published)
        self.assertTrue(thread_holds(start_lock_path(self.home)) is False)
        self.assertEqual(
            authenticated_instance_id(endpoint.port, endpoint.token, 2),
            endpoint.instance_id,
        )
        report = service_status(self.home)
        self.assertEqual(report, {"state": "running", "host": "127.0.0.1", "port": endpoint.port, "pid": endpoint.pid})
        self.assertNotIn("token", report)
        again = start_detached(self.home, 0)
        self.assertEqual(again.token, endpoint.token)
        self.assertEqual(again.instance_id, endpoint.instance_id)
        self.assertEqual(again.pid, endpoint.pid)
        call_service(endpoint.host, endpoint.port, endpoint.token, "GET", "/v1/health")
        self.assertEqual(identity_lock_holder(str(service_lock_path(self.home))), endpoint.pid)
        self.assertIsNone(self._unauthenticated_instance(endpoint.port))
        stopped = stop(self.home, 0.4, 0.4)
        self.assertEqual(stopped.outcome, "stopped")
        self._children.remove(endpoint.pid)
        reap(endpoint.pid)

    def test_unauthenticated_ok_does_not_count_as_running(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        thread = threading.Thread(target=_serve_ok, args=(sock,), daemon=True)
        thread.start()
        record = service_record(port=port, pid=os.getpid(), pgid=os.getpgid(os.getpid()))
        record["start_key"] = "start-key"
        record["boot_id"] = "boot-1"
        record["hostname"] = "host.example"
        write_service(self.home, record)
        inspector = ScriptedInspector(record)
        self.assertIsNone(authenticated_instance_id(port, str(record["token"]), 1))
        self.assertEqual(service_status(self.home, inspector)["state"], "stopped")
        sock.close()

    def test_foreground_order_and_shutdown_keeps_service_json(self) -> None:
        events: list[str] = []
        real_hold = hold_start_lock
        real_service_lock = acquire_service_lock
        real_get_request = RegistryHTTPServer.get_request

        @contextmanager
        def tracking_hold(home: Path, timeout: float):
            events.append("acquire-start")
            with real_hold(home, timeout):
                yield
            events.append("release-start")

        def tracking_service_lock(home: Path) -> int | None:
            events.append("acquire-service")
            return real_service_lock(home)

        stop_event = threading.Event()

        def tracking_get_request(server):
            result = real_get_request(server)
            events.append("accept")
            stop_event.set()
            return result

        holder: list[object] = []

        def run() -> None:
            try:
                holder.append(serve_foreground(self.home, 0, stop_event=stop_event))
            except Exception as exc:
                holder.append(exc)

        with patch("agentflow_registry.service.hold_start_lock", tracking_hold), patch(
            "agentflow_registry.service.acquire_service_lock", tracking_service_lock
        ), patch("agentflow_registry.service.before_service_json_publish", lambda *_a, **_k: events.append("publish")), patch(
            "agentflow_registry.server.RegistryHTTPServer.get_request", tracking_get_request
        ):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(_wait_for(lambda: "release-start" in events, 5))
            document = json.loads(service_json_path(self.home).read_text(encoding="utf-8"))
            probe = socket.create_connection(("127.0.0.1", int(document["port"])), timeout=2)
            probe.sendall(b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            probe.close()
            worker.join(5)
        self.assertFalse(worker.is_alive(), holder)
        self.assertEqual(
            events[:5],
            ["acquire-start", "acquire-service", "publish", "release-start", "accept"],
        )
        self.assertTrue(service_json_path(self.home).exists())
        self.assertIsInstance(holder[0], ServiceEndpoint)

    def test_module_without_child_or_handshake_exits_2(self) -> None:
        env = _child_env(self.home)
        bare = subprocess.run(
            [sys.executable, "-m", "agentflow_registry"],
            cwd=self.home,
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bare.returncode, 2)
        missing = subprocess.run(
            [sys.executable, "-m", "agentflow_registry", "--child", "--home", str(self.home), "--port", "9"],
            cwd=self.home,
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertFalse(service_json_path(self.home).exists())
        self.assertFalse(start_lock_path(self.home).exists())

    def test_child_exits_when_service_lock_is_held_and_leaves_the_file(self) -> None:
        original = b'{"untouched": true}\n'
        service_json_path(self.home).write_bytes(original)
        lock_fd = acquire_service_lock(self.home)
        self.assertIsNotNone(lock_fd)
        proc, parent = launch_child(self.home, 0)
        try:
            started = time.monotonic()
            line = read_ready_line(parent, time.monotonic() + 2)
            code = proc.wait(timeout=1)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            parent.close()
            if lock_fd is not None:
                os.close(lock_fd)
        self.assertIsNone(line)
        self.assertNotEqual(code, 0)
        self.assertEqual(service_json_path(self.home).read_bytes(), original)
        self.assertFalse(start_lock_path(self.home).exists())

    def test_sigterm_leaves_service_json_bytes_unchanged(self) -> None:
        endpoint = start_detached(self.home, 0, term_grace_sec=0.3, kill_grace_sec=0.3)
        self._children.append(endpoint.pid)
        before = service_json_path(self.home).read_bytes()
        os.kill(endpoint.pid, signal.SIGTERM)
        self.assertTrue(_wait_for(lambda: not _pid_alive(endpoint.pid), 2))
        self.assertEqual(service_json_path(self.home).read_bytes(), before)
        reap(endpoint.pid)
        self._children.remove(endpoint.pid)

    def test_parent_death_before_publish_leaves_service_json_absent(self) -> None:
        sentinel = self.home / "sentinel"
        env = _child_env(self.home)
        code = (
            "import sys\n"
            "from tests.support import hold_start_lock_after_child_ready\n"
            "hold_start_lock_after_child_ready(sys.argv[1], int(sys.argv[2]), sys.argv[3])\n"
        )
        support = subprocess.Popen(
            [sys.executable, "-c", code, str(self.home), "0", str(sentinel)],
            cwd=str(PACKAGE_ROOT),
            env=env,
        )
        child_pid = 0
        try:
            self.assertTrue(_wait_for(sentinel.exists, 5), _log_text(self.home))
            self.assertFalse(service_json_path(self.home).exists())
            child_pid = int(sentinel.read_text(encoding="utf-8"))
            self._children.append(child_pid)
            os.kill(support.pid, signal.SIGKILL)
            support.wait(timeout=2)
            self.assertTrue(_wait_for(lambda: not _pid_alive(child_pid), 1))
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                self.assertFalse(service_json_path(self.home).exists())
                time.sleep(0.05)
        finally:
            if support.poll() is None:
                os.kill(support.pid, signal.SIGKILL)
                support.wait(timeout=2)
        self.assertFalse(service_json_path(self.home).exists())
        called: list[object] = []
        with patch("agentflow_registry.service.before_service_json_unlink", lambda *_a, **_k: called.append(True)):
            result = stop(self.home, 0.2, 0.2)
        self.assertEqual(result.outcome, "absent")
        self.assertEqual(called, [])
        self.assertFalse(service_json_path(self.home).exists())
        self.assertIsNone(identity_lock_holder(str(service_lock_path(self.home))))
        if child_pid:
            reap(child_pid)
            if child_pid in self._children:
                self._children.remove(child_pid)

    def test_stop_sends_term_then_kill_only_after_the_identity_stays(self) -> None:
        record = service_record()
        write_service(self.home, record)
        before = service_json_path(self.home).read_bytes()
        inspector = ScriptedInspector(record)
        backend = RecordingBackend(supported=False)
        mismatched = ScriptedInspector(record)
        mismatched.start_key = "other-start"
        quiet = RecordingBackend(supported=False)
        result = stop(self.home, 0.2, 0.2, inspector=mismatched, backend=quiet)
        self.assertEqual(result.outcome, "error")
        self.assertEqual(quiet.log, [])
        self.assertEqual(service_json_path(self.home).read_bytes(), before)

        checks: list[str] = []

        def wrapped(loaded, checker, lock_path):
            checks.append("match")
            return service_identity_matches(loaded, checker, lock_path)

        with patch("agentflow_registry.service.service_identity_matches", wrapped):
            result = stop(self.home, 0.2, 0.2, inspector=inspector, backend=backend)
        self.assertEqual(result.outcome, "error")
        self.assertNotIn(result.outcome, {"absent", "stopped"})
        self.assertEqual(
            [item for item in backend.log if isinstance(item, tuple)],
            [("kill", record["pid"], signal.SIGTERM), ("kill", record["pid"], signal.SIGKILL)],
        )
        self.assertGreaterEqual(checks.count("match"), 3)
        self.assertEqual(service_json_path(self.home).read_bytes(), before)

    def test_stop_unlinks_when_the_process_disappears_during_term_grace(self) -> None:
        record = service_record()
        write_service(self.home, record)
        inspector = ScriptedInspector(record)
        backend = RecordingBackend(supported=False)

        def kill(pid: int, sig: int) -> None:
            backend.log.append(("kill", pid, sig))
            inspector.mark_gone()

        backend.kill = kill  # type: ignore[method-assign]
        result = stop(self.home, 0.2, 0.2, inspector=inspector, backend=backend)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(backend.log, [("kill", record["pid"], signal.SIGTERM)])
        self.assertFalse(service_json_path(self.home).exists())

    def test_stop_confirms_gone_when_the_process_exits_before_kill(self) -> None:
        record = service_record()
        write_service(self.home, record)
        inspector = ScriptedInspector(record)
        backend = RecordingBackend(supported=False)

        def kill(pid: int, sig: int) -> None:
            backend.log.append(("kill", pid, sig))
            inspector.mark_gone()
            raise ProcessLookupError(errno.ESRCH, "No such process")

        backend.kill = kill  # type: ignore[method-assign]
        result = stop(self.home, 0.2, 0.2, inspector=inspector, backend=backend)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(backend.log, [("kill", record["pid"], signal.SIGTERM)])
        self.assertFalse(service_json_path(self.home).exists())

    def test_stop_still_alive_after_sigkill_keeps_the_file(self) -> None:
        record = service_record()
        write_service(self.home, record)
        before = service_json_path(self.home).read_bytes()
        inspector = ScriptedInspector(record)
        backend = RecordingBackend(supported=False)
        called: list[object] = []
        started = time.monotonic()
        with patch("agentflow_registry.service.before_service_json_unlink", lambda *_a, **_k: called.append(True)):
            result = stop(self.home, 0.2, 0.2, inspector=inspector, backend=backend)
        elapsed = time.monotonic() - started
        self.assertEqual(result.outcome, "error")
        self.assertNotIn(result.outcome, {"absent", "stopped"})
        self.assertIn("still alive", result.message)
        self.assertEqual(service_json_path(self.home).read_bytes(), before)
        self.assertEqual(called, [])
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 1.2)
        self.assertIn(("kill", record["pid"], signal.SIGKILL), backend.log)

    def test_stop_unlinks_after_sigkill_once_the_process_is_gone(self) -> None:
        record = service_record()
        write_service(self.home, record)
        inspector = ScriptedInspector(record)
        backend = RecordingBackend(supported=False)

        def kill(pid: int, sig: int) -> None:
            backend.log.append(("kill", pid, sig))
            if sig == signal.SIGKILL:
                inspector.mark_gone()

        backend.kill = kill  # type: ignore[method-assign]
        result = stop(self.home, 0.2, 0.2, inspector=inspector, backend=backend)
        self.assertEqual(result.outcome, "stopped")
        self.assertIn(("kill", record["pid"], signal.SIGKILL), backend.log)
        self.assertFalse(service_json_path(self.home).exists())

    def test_stop_holds_start_lock_across_the_unlink(self) -> None:
        record = service_record()
        write_service(self.home, record)
        original = service_json_path(self.home).read_bytes()
        inspector = ScriptedInspector(record)
        inspector.alive = False
        inspector.hold_lock = False
        entered = threading.Event()
        release = threading.Event()
        published: list[str] = []

        def unlink_hook(*_args, **_kwargs) -> None:
            entered.set()
            release.wait(5)

        def publish_hook(_home, replacement: dict[str, object]) -> None:
            published.append(str(replacement["instance_id"]))

        stopped: list[object] = []
        started_result: list[object] = []

        def run_stop() -> None:
            stopped.append(stop(self.home, 0.2, 0.2, inspector=inspector, backend=RecordingBackend()))

        def run_start() -> None:
            try:
                started_result.append(
                    start_detached(self.home, 0, term_grace_sec=0.3, kill_grace_sec=0.3)
                )
            except Exception as exc:
                started_result.append(exc)

        with patch("agentflow_registry.service.before_service_json_unlink", unlink_hook), patch(
            "agentflow_registry.service.before_service_json_publish", publish_hook
        ):
            stopper = threading.Thread(target=run_stop)
            stopper.start()
            self.assertTrue(entered.wait(2))
            starter = threading.Thread(target=run_start)
            starter.start()
            starter.join(0.3)
            self.assertEqual(published, [])
            self.assertEqual(service_json_path(self.home).read_bytes(), original)
            release.set()
            stopper.join(3)
            starter.join(8)
        self.assertEqual(stopped[0].outcome, "stopped")
        self.assertTrue(started_result, _log_text(self.home))
        endpoint = started_result[0]
        self.assertFalse(isinstance(endpoint, Exception), f"{endpoint}\n{_log_text(self.home)}")
        self._children.append(endpoint.pid)
        document = json.loads(service_json_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(document["instance_id"], endpoint.instance_id)
        self.assertNotEqual(document["instance_id"], record["instance_id"])
        self.assertEqual(published, [endpoint.instance_id])
        stop(self.home, 0.4, 0.4)
        reap(endpoint.pid)
        self._children.remove(endpoint.pid)

    def test_unlink_reread_keeps_a_replaced_document(self) -> None:
        record = service_record()
        write_service(self.home, record)
        inspector = ScriptedInspector(record)
        backend = RecordingBackend(supported=False)
        replacement_pid = 999111

        def kill(pid: int, sig: int) -> None:
            backend.log.append(("kill", pid, sig))
            inspector.mark_gone()

        backend.kill = kill  # type: ignore[method-assign]

        def unlink_hook(home, _instance_id, _pid, _start_key) -> None:
            replacement = service_record(
                instance_id="replacement-instance",
                pid=replacement_pid,
                pgid=replacement_pid,
                start_key="replacement-start",
            )
            service_json_path(home).write_text(json.dumps(replacement) + "\n", encoding="utf-8")

        with patch("agentflow_registry.service.before_service_json_unlink", unlink_hook):
            result = stop(self.home, 0.2, 0.2, inspector=inspector, backend=backend)
        text = service_json_path(self.home).read_text(encoding="utf-8")
        self.assertIn("replacement-instance", text)
        self.assertEqual(result.outcome, "stopped")
        self.assertEqual(result.message, "")
        signaled = [item[1] for item in backend.log if isinstance(item, tuple) and item[0] == "kill"]
        self.assertIn(record["pid"], signaled)
        self.assertNotIn(replacement_pid, signaled)

    def test_second_start_waits_for_the_lock_and_adopts_only_authenticated_health(self) -> None:
        record = service_record()
        write_service(self.home, record)
        original = service_json_path(self.home).read_bytes()
        spawned: list[object] = []

        def forbid_spawn(*_args, **_kwargs):
            spawned.append(True)
            raise AssertionError("start spawned")

        with hold_start_lock(self.home, 2):
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    start_detached(self.home, 0, start_lock_timeout=0.3)
                except ServiceError as exc:
                    errors.append(exc)

            with patch("agentflow_registry.service.launch_child", forbid_spawn):
                worker = threading.Thread(target=run)
                worker.start()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(spawned, [])
            self.assertTrue(errors)
            self.assertEqual(service_json_path(self.home).read_bytes(), original)

        inspector = ScriptedInspector(record)
        with patch("agentflow_registry.service.launch_child", forbid_spawn), patch(
            "agentflow_registry.service.authenticated_instance_id",
            lambda *_args, **_kwargs: record["instance_id"],
        ):
            adopted = start_detached(self.home, 0, inspector=inspector, identity_timeout=0.2)
        self.assertEqual(adopted.token, record["token"])
        self.assertEqual(spawned, [])

        with patch("agentflow_registry.service.launch_child", forbid_spawn), patch(
            "agentflow_registry.service.authenticated_instance_id",
            lambda *_args, **_kwargs: None,
        ):
            with self.assertRaises(ServiceError):
                start_detached(self.home, 0, inspector=inspector, identity_timeout=0.2)
        self.assertEqual(service_json_path(self.home).read_bytes(), original)

    def test_stale_pid_with_a_different_start_key_is_replaced_without_a_signal(self) -> None:
        record = service_record(pid=424242, pgid=424242, start_key="old-start")
        write_service(self.home, record)
        inspector = ScriptedInspector(record)
        inspector.hold_lock = False
        inspector.start_key = "live-start"
        backend = RecordingBackend(supported=False)
        endpoint = start_detached(
            self.home,
            0,
            inspector=inspector,
            backend=backend,
            term_grace_sec=0.3,
            kill_grace_sec=0.3,
        )
        self._children.append(endpoint.pid)
        document = json.loads(service_json_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(document["pid"], endpoint.pid)
        self.assertNotEqual(document["pid"], 424242)
        self.assertNotIn(424242, [item[1] for item in backend.log if isinstance(item, tuple) and item[0] == "kill"])
        self.assertTrue(thread_holds(start_lock_path(self.home)) is False)
        stop(self.home, 0.4, 0.4)
        reap(endpoint.pid)
        self._children.remove(endpoint.pid)

    def test_failed_start_does_not_signal_when_identity_is_unavailable(self) -> None:
        backend = RecordingBackend(supported=False)
        spawned: list[int] = []
        kills: list[int] = []

        def launch(home: Path, port: int):
            proc, parent = launch_child(home, port)
            spawned.append(proc.pid)
            original = proc.kill

            def kill() -> None:
                kills.append(proc.pid)
                original()

            proc.kill = kill  # type: ignore[method-assign]
            return proc, parent

        with patch("agentflow_registry.service.launch_child", launch), patch(
            "agentflow_registry.service.kernel_process_identity", lambda _pid: None
        ):
            with self.assertRaises(ServiceError):
                start_detached(
                    self.home,
                    0,
                    backend=backend,
                    ready_timeout=2,
                    term_grace_sec=0.2,
                    kill_grace_sec=0.2,
                )
        self.assertTrue(spawned)
        self._children.extend(spawned)
        self.assertEqual(kills, [])
        self.assertEqual(backend.log, [])
        self.assertFalse(service_json_path(self.home).exists())

    def test_failed_start_signals_only_the_identity_captured_after_popen(self) -> None:
        backend = RecordingBackend(supported=False)
        spawned: list[int] = []
        kills: list[int] = []
        child_pid: dict[str, int] = {}
        lookups: list[str] = []

        def identity(pid: int) -> str | None:
            if pid != child_pid.get("pid"):
                return None
            lookups.append("hit")
            if len(lookups) == 1:
                return "captured-start"
            return "reused-start"

        def launch(home: Path, port: int):
            proc, parent = launch_child(home, port)
            child_pid["pid"] = proc.pid
            spawned.append(proc.pid)
            original = proc.kill

            def kill() -> None:
                kills.append(proc.pid)
                original()

            proc.kill = kill  # type: ignore[method-assign]
            return proc, parent

        with patch("agentflow_registry.service.launch_child", launch), patch(
            "agentflow_registry.service.kernel_process_identity", identity
        ), patch("agentflow_registry.service.read_ready_line", lambda *_args, **_kwargs: None):
            with self.assertRaises(ServiceError):
                start_detached(
                    self.home,
                    0,
                    backend=backend,
                    ready_timeout=0.3,
                    term_grace_sec=0.2,
                    kill_grace_sec=0.2,
                )
        self.assertTrue(spawned)
        self._children.extend(spawned)
        self.assertGreaterEqual(len(lookups), 2)
        self.assertEqual(kills, [])
        self.assertFalse(any(isinstance(item, tuple) and item[0] == "kill" for item in backend.log))
        self.assertFalse(service_json_path(self.home).exists())

    def test_same_start_key_without_the_service_lock_blocks_start(self) -> None:
        record = service_record()
        write_service(self.home, record)
        original = service_json_path(self.home).read_bytes()
        inspector = ScriptedInspector(record)
        inspector.hold_lock = False
        backend = RecordingBackend(supported=False)

        def forbid_spawn(*_args, **_kwargs):
            raise AssertionError("start spawned")

        with patch("agentflow_registry.service.launch_child", forbid_spawn):
            with self.assertRaises(ServiceError):
                start_detached(self.home, 0, inspector=inspector, backend=backend, identity_timeout=0.2)
        self.assertEqual(backend.log, [])
        self.assertEqual(service_json_path(self.home).read_bytes(), original)

    def _unauthenticated_instance(self, port: int) -> str | None:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/v1/health", headers={"Connection": "close"})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(payload.get("ok"), True)
        self.assertNotIn("instance_id", payload)
        return payload.get("instance_id") if isinstance(payload.get("instance_id"), str) else None


def _child_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AGENTFLOW_HOME"] = str(home.parent)
    env["PYTHONPATH"] = str(PACKAGE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _pid_alive(pid: int) -> bool:
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        waited = 0
    except OSError:
        waited = 0
    if waited == pid:
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


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _log_text(home: Path) -> str:
    path = home / "service.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _serve_ok(sock: socket.socket) -> None:
    try:
        connection, _addr = sock.accept()
    except OSError:
        return
    try:
        connection.recv(4096)
        body = b'{"ok": true}'
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
