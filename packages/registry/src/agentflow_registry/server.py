from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .routes import Call, build_router
from .store import RegistryStore

DEFAULT_PORT = 47321
MAX_BODY_BYTES = 65536


class RequestTimeout(Exception):
    pass


class _DeadlineBox:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline


class DeadlineReader:
    """Copy bytes from an internal buffer. The socket is read only in `_fill_one`."""

    def __init__(self, connection: socket.socket, clock: _DeadlineBox) -> None:
        self._connection = connection
        self._clock = clock
        self._buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            while self._fill_one():
                pass
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < size:
            if not self._fill_one():
                break
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def close(self) -> None:
        return None

    def readline(self, size: int = -1) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline != -1 and (size < 0 or newline + 1 <= size):
                end = newline + 1
                data = bytes(self._buffer[:end])
                del self._buffer[:end]
                return data
            if size >= 0 and len(self._buffer) >= size:
                data = bytes(self._buffer[:size])
                del self._buffer[:size]
                return data
            if not self._fill_one():
                data = bytes(self._buffer if size < 0 else self._buffer[:size])
                take = len(data)
                del self._buffer[:take]
                return data

    def _fill_one(self) -> bytes:
        remaining = self._clock.deadline - time.monotonic()
        if remaining <= 0:
            raise RequestTimeout()
        self._connection.settimeout(remaining)
        try:
            data = self._connection.recv(4096)
        except (socket.timeout, TimeoutError) as exc:
            raise RequestTimeout() from exc
        if data:
            self._buffer.extend(data)
        return data


class RegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: RegistryHTTPServer

    def setup(self) -> None:
        super().setup()
        self.requestline = ""
        self.request_version = "HTTP/1.1"
        self._clock = _DeadlineBox(time.monotonic() + self.server.read_deadline_sec)
        self.rfile = DeadlineReader(self.connection, self._clock)  # type: ignore[assignment]
        self._response_started = False

    def handle(self) -> None:
        self.server.adjust_in_flight(1)
        try:
            self.close_connection = True
            try:
                self.handle_one_request()
                while not self.close_connection:
                    self.handle_one_request()
            except RequestTimeout:
                self.close_connection = True
                self._write_timeout()
            except (ConnectionError, socket.timeout, TimeoutError, OSError):
                self.close_connection = True
        finally:
            self.server.adjust_in_flight(-1)

    def handle_one_request(self) -> None:
        self._response_started = False
        self._clock.deadline = time.monotonic() + self.server.read_deadline_sec
        self.raw_requestline = self.rfile.readline(65537)
        if not self.raw_requestline:
            self.close_connection = True
            return
        if len(self.raw_requestline) > 65536:
            self._send_json(
                400,
                _error("invalid_request", "Request line is too long."),
                close=True,
            )
            return
        if not self.parse_request():
            return
        self._dispatch()

    def log_message(self, fmt: str, *args: Any) -> None:
        return None

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        self._send_json(
            code,
            _error("invalid_request", message or "Invalid request."),
            close=True,
        )

    def _dispatch(self) -> None:
        path = unquote(urlsplit(self.path).path)
        method = self.command
        body: bytes | None = None
        if self.server.router.should_read_body(method, path):
            if not self._read_limited_body_if_needed():
                return
            body = self._body
        result = self.server.router.dispatch(
            Call(
                method=method,
                path=path,
                store=self.server.store,
                token=self.server.token,
                instance_id=self.server.instance_id,
                authorization=self.headers.get("Authorization"),
                body=body,
                before_store=self._clear_read_timeout,
            )
        )
        self._send_json(result.status, result.payload)

    def _clear_read_timeout(self) -> None:
        self.connection.settimeout(None)

    def _read_limited_body_if_needed(self) -> bool:
        if self.command != "POST":
            return True
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_json(
                400, _error("invalid_request", "Transfer-Encoding is not accepted.")
            )
            return False
        lengths = self.headers.get_all("Content-Length") or []
        kind, length = _content_length(lengths, self.server.max_body_bytes)
        if kind == "too_large":
            self._send_json(
                413,
                _error("payload_too_large", "Content-Length exceeds 65536 bytes."),
                close=True,
            )
            return False
        if kind != "ok" or length is None:
            self._send_json(
                400, _error("invalid_request", "Content-Length is missing or invalid.")
            )
            return False
        try:
            body = self._read_exact(length)
        except RequestTimeout:
            raise
        except ShortBody:
            self._send_json(400, _error("invalid_request", "Request body was truncated."))
            return False
        self._body = body
        return True

    def _read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            piece = self.rfile.read(length - len(chunks))
            if not piece:
                raise ShortBody()
            chunks.extend(piece)
        return bytes(chunks)

    def _write_timeout(self) -> None:
        if self._response_started:
            return
        try:
            self.connection.settimeout(self.server.write_deadline_sec)
            self._send_json(
                408,
                _error("request_timeout", "The request was not fully read before the deadline."),
                close=True,
            )
        except (OSError, TimeoutError, RequestTimeout):
            self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, Any], *, close: bool = False) -> None:
        if self._response_started:
            return
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._response_started = True
        if close:
            self.close_connection = True
        try:
            self.connection.settimeout(self.server.write_deadline_sec)
        except OSError:
            pass
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (OSError, TimeoutError):
            self.close_connection = True


class ShortBody(Exception):
    pass


class RegistryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        store: RegistryStore,
        token: str,
        instance_id: str,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        read_deadline_sec: float = 5.0,
        write_deadline_sec: float = 5.0,
        max_body_bytes: int = MAX_BODY_BYTES,
        bind_and_activate: bool = True,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("The registry service binds 127.0.0.1 only.")
        self.store = store
        self.token = token
        self.instance_id = instance_id
        self.read_deadline_sec = read_deadline_sec
        self.write_deadline_sec = write_deadline_sec
        self.max_body_bytes = max_body_bytes
        self.in_flight = 0
        self._in_flight_lock = threading.Lock()
        self.router = build_router()
        super().__init__((host, port), RegistryHandler, bind_and_activate)

    def adjust_in_flight(self, delta: int) -> None:
        with self._in_flight_lock:
            self.in_flight += delta

    def server_bind(self) -> None:
        super().server_bind()
        host = self.server_address[0]
        if host != "127.0.0.1":
            raise ValueError("The registry service binds 127.0.0.1 only.")


def _error(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}


def _content_length(values: list[str], cap: int) -> tuple[str, int | None]:
    if len(values) != 1:
        return "invalid", None
    text = values[0]
    if text.strip() != text or not text.isdigit():
        return "invalid", None
    # int() raises ValueError past its digit limit. Compare the digits as text.
    significant = text.lstrip("0")
    if significant == "":
        return "ok", 0
    cap_text = str(cap)
    if len(significant) > len(cap_text) or (
        len(significant) == len(cap_text) and significant > cap_text
    ):
        return "too_large", None
    return "ok", int(significant)
