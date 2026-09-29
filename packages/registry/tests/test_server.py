from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_registry.server import MAX_BODY_BYTES, RegistryHTTPServer
from agentflow_registry.store import RegistryStore
from tests.support import make_workspace


TOOL = {"name": "agentflow", "version": "0.1.0"}


class ServerFixture:
    def __init__(self, root: Path, *, read_deadline_sec: float = 5.0) -> None:
        self.root = root
        self.home = root / "registry"
        self.home.mkdir()
        self.store = RegistryStore(self.home)
        self.server = RegistryHTTPServer(
            self.store,
            "token-under-test-token-under-test-token-01",
            "instance-under-test-instance-under-test-01",
            "127.0.0.1",
            0,
            read_deadline_sec=read_deadline_sec,
        )
        self.port = self.server.server_address[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.server.timeout = 0.2
        while not self.stop.is_set():
            try:
                self.server.handle_request()
            except OSError:
                break

    def close(self) -> None:
        self.stop.set()
        try:
            self.server.server_close()
        except OSError:
            pass
        self.thread.join(2)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        *,
        token: str | None = "token-under-test-token-under-test-token-01",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            payload_headers = {"Connection": "close"}
            if token is not None:
                payload_headers["Authorization"] = f"Bearer {token}"
            if body is not None:
                payload_headers["Content-Type"] = "application/json"
                payload_headers["Content-Length"] = str(len(body))
            if headers:
                payload_headers.update(headers)
            connection.request(method, path, body=body, headers=payload_headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return status, parsed


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._home = tempfile.mkdtemp()
        self._env = os.environ.get("AGENTFLOW_HOME")
        os.environ["AGENTFLOW_HOME"] = self._home
        self._cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        if self._env is None:
            os.environ.pop("AGENTFLOW_HOME", None)
        else:
            os.environ["AGENTFLOW_HOME"] = self._env
        shutil.rmtree(self._home, ignore_errors=True)
        self._tmp.cleanup()

    def test_daemon_threads_is_set_on_the_subclass_body(self) -> None:
        self.assertIn("daemon_threads", RegistryHTTPServer.__dict__)
        self.assertIs(RegistryHTTPServer.__dict__["daemon_threads"], True)

    def test_health_auth_and_workspace_routes(self) -> None:
        fixture = ServerFixture(self.root)
        self.addCleanup(fixture.close)
        self.assertIs(fixture.server.store, fixture.store)
        status, health = fixture.request("GET", "/v1/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(health, {"ok": True})
        status, authed = fixture.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(authed["instance_id"], fixture.server.instance_id)
        status, denied = fixture.request("GET", "/v1/health", token="nope")
        self.assertEqual(status, 401)
        status, denied = fixture.request("GET", "/v1/workspaces", token="nope")
        self.assertEqual(status, 401)

        workspace = make_workspace(self.root, "demo")
        created = self._post(fixture, workspace)
        self.assertEqual(created[0], 201)
        refreshed = self._post(fixture, workspace)
        self.assertEqual(refreshed[0], 200)
        self.assertEqual(refreshed[1]["id"], created[1]["id"])
        status, listing = fixture.request("GET", "/v1/workspaces")
        self.assertEqual(status, 200)
        self.assertTrue(listing["workspaces"][0]["available"])
        status, fetched = fixture.request("GET", f"/v1/workspaces/{created[1]['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(fetched["available"])
        status, removed = fixture.request("DELETE", f"/v1/workspaces/{created[1]['id']}")
        self.assertEqual(status, 200)
        status, missing = fixture.request("GET", f"/v1/workspaces/{created[1]['id']}")
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "workspace_not_found")
        status, bad = fixture.request("POST", "/v1/workspaces", b"not-json")
        self.assertEqual(status, 400)
        status, extra = fixture.request(
            "POST",
            "/v1/workspaces",
            json.dumps({"path": str(workspace), "tool": TOOL, "extra": 1}).encode(),
        )
        self.assertEqual(status, 400)
        registry = fixture.home / "registry.json"
        registry.write_bytes(b"{")
        status, corrupt = fixture.request(
            "POST",
            "/v1/workspaces",
            json.dumps({"path": str(workspace), "tool": TOOL}).encode(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(corrupt["error"], "registry_corrupt")
        self.assertEqual(registry.read_bytes(), b"{")

    def test_relative_path_is_rejected_even_when_cwd_contains_the_workspace(self) -> None:
        fixture = ServerFixture(self.root)
        self.addCleanup(fixture.close)
        workspace = make_workspace(self.root, "demo")
        os.chdir(self.root)
        status, _payload = self._post_path(fixture, workspace.name)
        self.assertEqual(status, 400)
        self.assertFalse((fixture.home / "registry.json").exists())
        status, created = self._post_path(fixture, str(workspace))
        self.assertEqual(status, 201)
        self.assertEqual(created["path"], str(workspace.resolve()))
        for relative in ("~/somewhere", "../escape"):
            status, _payload = self._post_path(fixture, relative)
            self.assertEqual(status, 400)
        listing = fixture.store.list()
        self.assertEqual([row["path"] for row in listing], [str(workspace.resolve())])

    def test_body_cap_rejects_before_the_body_arrives(self) -> None:
        fixture = ServerFixture(self.root)
        self.addCleanup(fixture.close)
        registry = fixture.home / "registry.json"
        header = (
            b"POST /v1/workspaces HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: " + str(MAX_BODY_BYTES + 1).encode() + b"\r\n"
            b"\r\n"
        )
        started = time.monotonic()
        sock = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        try:
            sock.sendall(header)
            raw = _read_response(sock)
        finally:
            sock.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn(b" 413 ", raw)
        self.assertFalse(registry.exists())
        missing = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        try:
            missing.sendall(
                b"POST /v1/workspaces HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            raw = _read_response(missing)
        finally:
            missing.close()
        self.assertIn(b" 400 ", raw)
        self.assertIn(b"Content-Length", raw)

    def test_keepalive_answers_a_second_request_on_the_same_connection(self) -> None:
        fixture = ServerFixture(self.root)
        self.addCleanup(fixture.close)
        sock = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        try:
            sock.sendall(b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            first = _read_http_message(sock)
            self.assertIn(b"HTTP/1.1 200 ", first)
            self.assertNotIn(b"Connection: close", first)
            self.assertIn(b'"ok":true', first)
            sock.sendall(
                b"GET /v1/health HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            second = _read_http_message(sock)
        finally:
            sock.close()
        self.assertIn(b"HTTP/1.1 200 ", second)
        self.assertIn(b'"ok":true', second)

    def test_oversized_digit_content_length_returns_413_without_reading_the_body(self) -> None:
        fixture = ServerFixture(self.root)
        self.addCleanup(fixture.close)
        registry = fixture.home / "registry.json"
        header = (
            b"POST /v1/workspaces HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Length: " + (b"9" * 4301) + b"\r\n"
            b"\r\n"
        )
        started = time.monotonic()
        sock = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        try:
            sock.sendall(header)
            raw = _read_http_message(sock)
        finally:
            sock.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn(b" 413 ", raw)
        self.assertIn(b"payload_too_large", raw)
        self.assertFalse(registry.exists())

    def test_stalled_body_returns_408_and_a_later_post_succeeds(self) -> None:
        fixture = ServerFixture(self.root, read_deadline_sec=0.2)
        self.addCleanup(fixture.close)
        previous = fixture.server.in_flight
        sock = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        try:
            _wait_until(lambda: fixture.server.in_flight == previous + 1, 1)
            started = time.monotonic()
            sock.sendall(
                b"POST /v1/workspaces HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: 20\r\n"
                b"\r\n"
                b"x"
            )
            raw = _read_response(sock)
            _wait_until(lambda: fixture.server.in_flight == previous, 1)
        finally:
            sock.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn(b" 408 ", raw)
        workspace = make_workspace(self.root, "later")
        status, _created = self._post(fixture, workspace)
        self.assertEqual(status, 201)

    def test_slow_drip_releases_the_handler_before_a_restarted_timeout_would(self) -> None:
        fixture = ServerFixture(self.root, read_deadline_sec=0.4)
        self.addCleanup(fixture.close)
        previous = fixture.server.in_flight
        payload = b"POST /v1/workspaces HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\n\r\n{}"
        self.assertGreater(len(payload.split(b"\r\n", 1)[0]), 15)
        sock = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        try:
            _wait_until(lambda: fixture.server.in_flight == previous + 1, 1)
            started = time.monotonic()
            for index, byte in enumerate(payload):
                try:
                    sock.send(bytes([byte]))
                except OSError:
                    break
                if fixture.server.in_flight == previous:
                    break
                if index + 1 < len(payload):
                    time.sleep(0.1)
            _wait_until(lambda: fixture.server.in_flight == previous, 2)
            elapsed = time.monotonic() - started
        finally:
            sock.close()
        self.assertLess(elapsed, 0.9)
        self.assertFalse((fixture.home / "registry.json").exists())
        workspace = make_workspace(self.root, "later")
        status, _created = self._post(fixture, workspace)
        self.assertEqual(status, 201)

    def test_overlapping_posts_both_remain(self) -> None:
        fixture = ServerFixture(self.root)
        self.addCleanup(fixture.close)
        first = make_workspace(self.root, "one")
        second = make_workspace(self.root, "two")
        entered = threading.Event()
        release = threading.Event()

        def hook() -> None:
            entered.set()
            release.wait(3)

        fixture.store._locked_hook = hook  # type: ignore[method-assign]
        results: list[tuple[int, dict[str, object]]] = []

        def post(workspace: Path) -> None:
            results.append(self._post(fixture, workspace))

        left = threading.Thread(target=post, args=(first,))
        left.start()
        self.assertTrue(entered.wait(2))
        right = threading.Thread(target=post, args=(second,))
        right.start()
        right.join(0.3)
        self.assertTrue(right.is_alive())
        release.set()
        left.join(3)
        right.join(3)
        self.assertEqual(sorted(status for status, _payload in results), [201, 201])
        listing = fixture.store.list()
        self.assertEqual({row["path"] for row in listing}, {str(first.resolve()), str(second.resolve())})

    def _post(self, fixture: ServerFixture, workspace: Path) -> tuple[int, dict[str, object]]:
        return self._post_path(fixture, str(workspace))

    def _post_path(self, fixture: ServerFixture, path: str) -> tuple[int, dict[str, object]]:
        body = json.dumps({"path": path, "tool": TOOL}).encode("utf-8")
        return fixture.request("POST", "/v1/workspaces", body)


def _read_response(sock: socket.socket) -> bytes:
    return _read_http_message(sock)


def _read_http_message(sock: socket.socket) -> bytes:
    sock.settimeout(2)
    chunks = bytearray()
    while b"\r\n\r\n" not in chunks:
        piece = sock.recv(4096)
        if not piece:
            break
        chunks.extend(piece)
    header, _, rest = bytes(chunks).partition(b"\r\n\r\n")
    length = _content_length_header(header)
    if length is None:
        return bytes(chunks)
    body = bytearray(rest)
    while len(body) < length:
        piece = sock.recv(4096)
        if not piece:
            break
        body.extend(piece)
    return header + b"\r\n\r\n" + bytes(body[:length])


def _content_length_header(header: bytes) -> int | None:
    for line in header.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if separator and name.lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met")


if __name__ == "__main__":
    unittest.main()
