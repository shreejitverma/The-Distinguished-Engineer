"""C-accelerated HTTP framing and libuv loop replacement abstractions.

Contrasts C-extension llhttp streaming callback framing (httptools 0.9.0)
with pure-Python state machine parsing (h11 0.16.0), and inspects uvloop (0.23.0)
internals including libuv 1.52.1 binding and buffer allocation boundaries.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import h11
import httptools

try:
    import uvloop
    import uvloop.loop as _uvloop_loop
except ImportError:  # pragma: no cover
    uvloop = None  # type: ignore[assignment]
    _uvloop_loop = None  # type: ignore[assignment]

UVLOOP_AVAILABLE: bool = uvloop is not None


@dataclass(slots=True)
class ParsedHttpRequest:
    """Structured representation of a parsed HTTP request message."""

    method: str = ""
    url: bytes = b""
    http_version: str = "1.1"
    headers: list[tuple[bytes, bytes]] = field(default_factory=lambda: list[tuple[bytes, bytes]]())
    body: bytes = b""
    chunk_count: int = 0
    completed: bool = False
    upgrade: bool = False


class HttpToolsParserCollector:
    """Protocol collector bridging httptools C callbacks to structured messages.

    Demonstrates how httptools/llhttp streaming push-parser callbacks operate
    across arbitrary chunk boundaries without allocating intermediate Python
    event instances.
    """

    def __init__(self) -> None:
        self.parser: httptools.HttpRequestParser = httptools.HttpRequestParser(self)
        self.messages: list[ParsedHttpRequest] = []
        self.callback_log: list[str] = []
        self._current_msg: ParsedHttpRequest | None = None
        self._current_url: bytes = b""

    # httptools HttpRequestParser callback interface
    def on_message_begin(self) -> None:
        self.callback_log.append("on_message_begin")
        self._current_msg = ParsedHttpRequest()
        self._current_url = b""

    def on_url(self, url: bytes) -> None:
        self.callback_log.append(f"on_url:{url.decode('latin-1')}")
        self._current_url += url

    def on_header(self, name: bytes, value: bytes) -> None:
        name_lower = name.lower()
        self.callback_log.append(
            f"on_header:{name_lower.decode('latin-1')}={value.decode('latin-1')}"
        )
        if self._current_msg is not None:
            self._current_msg.headers.append((name_lower, value))

    def on_headers_complete(self) -> None:
        self.callback_log.append("on_headers_complete")
        if self._current_msg is not None:
            raw_method = self.parser.get_method()
            self._current_msg.method = raw_method.decode("ascii")
            self._current_msg.url = self._current_url
            self._current_msg.http_version = self.parser.get_http_version()
            self._current_msg.upgrade = bool(self.parser.should_upgrade())

    def on_body(self, body: bytes) -> None:
        self.callback_log.append(f"on_body:{len(body)}b")
        if self._current_msg is not None:
            self._current_msg.body += body

    def on_chunk_header(self) -> None:
        self.callback_log.append("on_chunk_header")
        if self._current_msg is not None:
            self._current_msg.chunk_count += 1

    def on_chunk_complete(self) -> None:
        self.callback_log.append("on_chunk_complete")

    def on_message_complete(self) -> None:
        self.callback_log.append("on_message_complete")
        if self._current_msg is not None:
            self._current_msg.completed = True
            self.messages.append(self._current_msg)
            self._current_msg = None

    def feed(self, data: bytes) -> None:
        """Feed a contiguous byte slice into the parser."""
        self.parser.feed_data(data)

    def feed_chunks(self, chunks: list[bytes]) -> None:
        """Feed multiple arbitrary byte chunks into the streaming parser."""
        for chunk in chunks:
            if chunk:
                self.parser.feed_data(chunk)


class H11ParserCollector:
    """Pull-based HTTP parser collector wrapping pure-Python h11."""

    def __init__(self) -> None:
        self.conn: h11.Connection = h11.Connection(h11.SERVER)
        self.messages: list[ParsedHttpRequest] = []
        self.event_log: list[str] = []
        self._current_msg: ParsedHttpRequest | None = None

    def feed(self, data: bytes) -> None:
        """Feed bytes into h11 and drain available events."""
        self.conn.receive_data(data)
        self._drain_events()

    def feed_chunks(self, chunks: list[bytes]) -> None:
        """Feed multiple byte chunks into h11 and drain events."""
        for chunk in chunks:
            if chunk:
                self.conn.receive_data(chunk)
                self._drain_events()

    def _drain_events(self) -> None:
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                break
            event_name = type(event).__name__
            self.event_log.append(event_name)

            if isinstance(event, h11.Request):
                self._current_msg = ParsedHttpRequest(
                    method=event.method.decode("ascii"),
                    url=event.target,
                    http_version=event.http_version.decode("ascii"),
                    headers=[(name.lower(), val) for name, val in event.headers],
                )
            elif isinstance(event, h11.Data):
                if self._current_msg is not None:
                    self._current_msg.body += bytes(event.data)
                    self._current_msg.chunk_count += 1
            elif isinstance(event, h11.EndOfMessage):
                if self._current_msg is not None:
                    self._current_msg.completed = True
                    self.messages.append(self._current_msg)
                    self._current_msg = None
            elif isinstance(event, h11.ConnectionClosed):
                break


class UvloopDiagnostics:
    """Introspection utilities for uvloop and libuv runtime parameters."""

    UV_STREAM_RECV_BUF_SIZE: int = 256000  # 250 KB fixed buffer in includes/consts.pxi

    @classmethod
    def is_available(cls) -> bool:
        return UVLOOP_AVAILABLE

    @classmethod
    def get_libuv_version_info(cls) -> dict[str, Any]:
        """Inspect and return libuv embedded version metadata."""
        if not UVLOOP_AVAILABLE or _uvloop_loop is None:
            return {"available": False, "version_hex": None, "version_string": None}

        get_ver = getattr(_uvloop_loop, "libuv_get_version", None)
        if get_ver is None:
            return {"available": True, "version_hex": None, "version_string": None}

        v_hex = int(get_ver())
        major = (v_hex >> 16) & 0xFF
        minor = (v_hex >> 8) & 0xFF
        patch = v_hex & 0xFF
        v_str = f"{major}.{minor}.{patch}"
        return {
            "available": True,
            "version_hex": hex(v_hex),
            "version_string": v_str,
            "major": major,
            "minor": minor,
            "patch": patch,
        }

    @classmethod
    def create_loop(cls) -> asyncio.AbstractEventLoop:
        """Create a new uvloop instance if available, or standard asyncio loop."""
        if UVLOOP_AVAILABLE and uvloop is not None:
            return uvloop.new_event_loop()
        return asyncio.new_event_loop()


def benchmark_parser_throughput(payload: bytes, iterations: int = 10000) -> dict[str, float]:
    """Benchmark httptools vs h11 throughput in req/s on a representative payload."""

    class _HttptoolsDummy:
        def on_message_begin(self) -> None:
            pass

        def on_url(self, u: bytes) -> None:
            pass

        def on_header(self, n: bytes, v: bytes) -> None:
            pass

        def on_headers_complete(self) -> None:
            pass

        def on_body(self, b: bytes) -> None:
            pass

        def on_message_complete(self) -> None:
            pass

    # Benchmark httptools
    dummy = _HttptoolsDummy()
    t0 = time.perf_counter()
    for _ in range(iterations):
        parser = httptools.HttpRequestParser(dummy)
        parser.feed_data(payload)
    t1 = time.perf_counter()
    httptools_elapsed = max(t1 - t0, 1e-9)
    httptools_rate = iterations / httptools_elapsed

    # Benchmark h11
    t0 = time.perf_counter()
    for _ in range(iterations):
        conn = h11.Connection(h11.SERVER)
        conn.receive_data(payload)
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                break
    t1 = time.perf_counter()
    h11_elapsed = max(t1 - t0, 1e-9)
    h11_rate = iterations / h11_elapsed

    return {
        "httptools_rate": httptools_rate,
        "h11_rate": h11_rate,
        "speedup_ratio": httptools_rate / h11_rate,
    }
