"""Granian 2.8.7 internal scope construction and ASGI vs RSGI protocol overhead analysis.

This module models and benchmarks the protocol mechanics of Granian 2.8.7:
1. ASGI 3.0 scope construction and message adaptation matching Granian's Rust engine.
2. RSGI 1.6 zero-allocation scope descriptors and synchronous C-API response emission.
3. Heap allocation tracking and dictionary instantiation accounting across both interfaces.
"""

from __future__ import annotations

import gc
import sys
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, cast
from urllib.parse import unquote

# ASGI scope and message types
ASGIScope = dict[str, Any]
ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


class ASGIScopeBuilder:
    """Builds standard ASGI 3.0 HTTP scope matching Granian 2.8.7 src/asgi/utils.rs."""

    # Static cached structures matching Granian's PyOnceLock caches: Granian binds the
    # same cached dict objects into every scope, so they are shared, not copied.
    _CACHED_ASGI_VERSION: ClassVar[dict[str, str]] = {"version": "3.0", "spec_version": "2.3"}
    _CACHED_EXTENSIONS: ClassVar[dict[str, dict[str, Any]]] = {
        "http.response.pathsend": {},
        "websocket.http.response": {},
    }

    @classmethod
    def build_http_scope(
        cls,
        *,
        method: str,
        path: str,
        query_string: bytes = b"",
        headers: Sequence[tuple[bytes, bytes]] = (),
        server: tuple[str, int] = ("127.0.0.1", 8000),
        client: tuple[str, int] = ("127.0.0.1", 50000),
        scheme: str = "http",
        http_version: str = "1.1",
        authority: str | None = None,
    ) -> ASGIScope:
        """Construct ASGI HTTP scope matching Granian's build_scope_http macro."""
        decoded_path = unquote(path)
        raw_path = path.encode("latin-1")

        header_list: list[tuple[bytes, bytes]] = list(headers)
        has_host = any(name.lower() == b"host" for name, _ in header_list)
        if not has_host and authority:
            header_list.insert(0, (b"host", authority.encode("latin-1")))

        return {
            "type": "http",
            "asgi": cls._CACHED_ASGI_VERSION,
            "http_version": http_version,
            "server": (server[0], str(server[1])),
            "client": (client[0], str(client[1])),
            "scheme": scheme,
            "method": method.upper(),
            "path": decoded_path,
            "raw_path": raw_path,
            "query_string": query_string,
            "headers": header_list,
            "extensions": cls._CACHED_EXTENSIONS,
        }


class RSGIHeadersWrapper(Mapping[str, str]):
    """Zero-allocation headers wrapper modeling Granian's RSGIHeaders PyClass."""

    __slots__ = ("_items", "_map")

    def __init__(self, headers: Sequence[tuple[str, str]]) -> None:
        self._items: list[tuple[str, str]] = [(k.lower(), v) for k, v in headers]
        # Multi-value dictionary mapping for get_all
        mapping: dict[str, list[str]] = {}
        for k, v in self._items:
            mapping.setdefault(k, []).append(v)
        self._map: dict[str, list[str]] = mapping

    def __getitem__(self, key: str) -> str:
        values = self._map.get(key.lower())
        if not values:
            raise KeyError(key)
        return values[0]

    def __iter__(self) -> Iterator[str]:
        return iter(self._map.keys())

    def __len__(self) -> int:
        return len(self._map)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key.lower() in self._map

    def get_all(self, key: str) -> list[str]:
        """Return all values associated with the header name."""
        return list(self._map.get(key.lower(), []))

    def keys(self) -> list[str]:  # type: ignore[override]
        return list(self._map.keys())

    def values(self) -> list[str]:  # type: ignore[override]
        return [v for _, v in self._items]

    def items(self) -> list[tuple[str, str]]:  # type: ignore[override]
        return list(self._items)


class RSGIScopeDescriptor:
    """Immutable descriptor modeling Granian's RSGIHTTPScope PyClass."""

    __slots__ = (
        "_authority",
        "_client",
        "_headers",
        "_http_version",
        "_method",
        "_path",
        "_proto",
        "_query_string",
        "_rsgi_version",
        "_scheme",
        "_server",
    )

    def __init__(
        self,
        *,
        method: str,
        path: str,
        query_string: str = "",
        headers: Sequence[tuple[str, str]] = (),
        server: tuple[str, int] = ("127.0.0.1", 8000),
        client: tuple[str, int] = ("127.0.0.1", 50000),
        scheme: str = "http",
        http_version: str = "1.1",
        authority: str | None = None,
    ) -> None:
        self._proto: str = "http"
        self._rsgi_version: str = "1.6"
        self._http_version: str = http_version
        self._server: str = f"{server[0]}:{server[1]}"
        self._client: str = f"{client[0]}:{client[1]}"
        self._scheme: str = scheme
        self._method: str = method.upper()
        self._path: str = path
        self._query_string: str = query_string
        self._headers: RSGIHeadersWrapper = RSGIHeadersWrapper(headers)
        self._authority: str | None = authority

    @property
    def proto(self) -> str:
        return self._proto

    @property
    def rsgi_version(self) -> str:
        return self._rsgi_version

    @property
    def http_version(self) -> str:
        return self._http_version

    @property
    def server(self) -> str:
        return self._server

    @property
    def client(self) -> str:
        return self._client

    @property
    def scheme(self) -> str:
        return self._scheme

    @property
    def method(self) -> str:
        return self._method

    @property
    def path(self) -> str:
        return self._path

    @property
    def query_string(self) -> str:
        return self._query_string

    @property
    def headers(self) -> RSGIHeadersWrapper:
        return self._headers

    @property
    def authority(self) -> str | None:
        return self._authority


class MockASGIProtocol:
    """Mocks Granian's ASGIHTTPProtocol message state machine and adapter."""

    def __init__(self, request_body: bytes = b"") -> None:
        self.request_body: bytes = request_body
        self.read_called: bool = False
        self.response_started: bool = False
        self.response_completed: bool = False
        self.status_code: int = 500
        self.response_headers: list[tuple[bytes, bytes]] = []
        self.response_body: bytearray = bytearray()
        self.messages_sent: int = 0

    async def receive(self) -> ASGIMessage:
        """Deliver inbound HTTP request body."""
        if not self.read_called:
            self.read_called = True
            return {
                "type": "http.request",
                "body": self.request_body,
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    async def send(self, message: ASGIMessage) -> None:
        """Validate ASGI send sequencing with the error strings of Granian ASGIHTTPProtocol.send."""
        self.messages_sent += 1
        msg_type = message.get("type")

        if msg_type == "http.response.start":
            if self.response_started:
                raise RuntimeError("ASGI flow error: Response already started")
            self.response_started = True
            self.status_code = int(message["status"])
            self.response_headers = list(message.get("headers", []))

        elif msg_type == "http.response.body":
            if not self.response_started:
                raise RuntimeError("ASGI flow error: Response not started")
            if self.response_completed:
                raise RuntimeError("ASGI flow error: Response already finished")
            body_chunk = message.get("body", b"")
            if isinstance(body_chunk, bytes | bytearray):
                self.response_body.extend(body_chunk)
            more_body = message.get("more_body", False)
            if not more_body:
                self.response_completed = True

        else:
            raise RuntimeError("Unsupported ASGI message")


class MockRSGIStreamTransport:
    """Stream transport returned by RSGI response_stream()."""

    def __init__(self, protocol: MockRSGIProtocol) -> None:
        self._protocol: MockRSGIProtocol = protocol

    async def send_bytes(self, data: bytes) -> None:
        self._protocol.response_body.extend(data)

    async def send_str(self, data: str) -> None:
        self._protocol.response_body.extend(data.encode("utf-8"))


class MockRSGIProtocol:
    """Mocks Granian's RSGIHTTPProtocol Rust C-API interface."""

    def __init__(self, request_body: bytes = b"") -> None:
        self.request_body: bytes = request_body
        self.status_code: int = 500
        self.response_headers: list[tuple[str, str]] = []
        self.response_body: bytearray = bytearray()
        self.response_completed: bool = False
        self.response_method: str = ""

    async def __call__(self) -> bytes:
        """Fetch request body as bytes directly (matching coroutine __call__)."""
        return self.request_body

    def response_empty(self, status: int, headers: list[tuple[str, str]]) -> None:
        """Synchronous response emission matching Rust RSGIHTTPProtocol::response_empty."""
        self.status_code = status
        self.response_headers = list(headers)
        self.response_body = bytearray()
        self.response_completed = True
        self.response_method = "response_empty"

    def response_bytes(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        """Synchronous response emission matching Rust RSGIHTTPProtocol::response_bytes."""
        self.status_code = status
        self.response_headers = list(headers)
        self.response_body = bytearray(body)
        self.response_completed = True
        self.response_method = "response_bytes"

    def response_str(self, status: int, headers: list[tuple[str, str]], body: str) -> None:
        """Synchronous response emission matching Rust RSGIHTTPProtocol::response_str."""
        self.status_code = status
        self.response_headers = list(headers)
        self.response_body = bytearray(body.encode("utf-8"))
        self.response_completed = True
        self.response_method = "response_str"

    def response_stream(
        self, status: int, headers: list[tuple[str, str]]
    ) -> MockRSGIStreamTransport:
        """Initiate streaming response matching Rust RSGIHTTPProtocol::response_stream."""
        self.status_code = status
        self.response_headers = list(headers)
        self.response_method = "response_stream"
        return MockRSGIStreamTransport(self)


# RSGI application type definition
RSGIApp = Callable[[RSGIScopeDescriptor, MockRSGIProtocol], Awaitable[None]]


@dataclass(frozen=True)
class ProtocolAllocationStats:
    """Recorded memory and object allocation metrics for a request cycle."""

    dicts_allocated: int
    tuples_allocated: int
    bytes_allocated: int
    total_blocks_delta: int


class AllocationTracker:
    """Measures Python heap allocations for protocol request workflows."""

    @staticmethod
    def count_objects_by_type() -> dict[type[object], int]:
        counts: dict[type[object], int] = {}
        for obj in gc.get_objects():
            t: type[object] = cast("type[object]", type(obj))
            counts[t] = counts.get(t, 0) + 1
        return counts

    @classmethod
    async def measure_asgi_request(
        cls,
        app: ASGIApp,
        method: str = "GET",
        path: str = "/health",
        headers: Sequence[tuple[bytes, bytes]] = ((b"accept", b"application/json"),),
        body: bytes = b"",
    ) -> tuple[MockASGIProtocol, ProtocolAllocationStats]:
        """Execute and measure allocation cost of an ASGI request cycle."""
        gc.collect()
        blocks_before = sys.getallocatedblocks()
        before_counts = cls.count_objects_by_type()

        # Step 1: Scope construction
        scope = ASGIScopeBuilder.build_http_scope(
            method=method,
            path=path,
            headers=headers,
        )
        # Granian callback wrapper update
        scope.update(root_path="", state={})

        protocol = MockASGIProtocol(request_body=body)
        await app(scope, protocol.receive, protocol.send)

        after_counts = cls.count_objects_by_type()
        blocks_after = sys.getallocatedblocks()

        stats = ProtocolAllocationStats(
            dicts_allocated=max(0, after_counts.get(dict, 0) - before_counts.get(dict, 0)),
            tuples_allocated=max(0, after_counts.get(tuple, 0) - before_counts.get(tuple, 0)),
            bytes_allocated=max(0, after_counts.get(bytes, 0) - before_counts.get(bytes, 0)),
            total_blocks_delta=blocks_after - blocks_before,
        )
        return protocol, stats

    @classmethod
    async def measure_rsgi_request(
        cls,
        app: RSGIApp,
        method: str = "GET",
        path: str = "/health",
        headers: Sequence[tuple[str, str]] = (("accept", "application/json"),),
        body: bytes = b"",
    ) -> tuple[MockRSGIProtocol, ProtocolAllocationStats]:
        """Execute and measure allocation cost of an RSGI request cycle."""
        gc.collect()
        blocks_before = sys.getallocatedblocks()
        before_counts = cls.count_objects_by_type()

        # Step 1: Scope descriptor construction
        scope = RSGIScopeDescriptor(
            method=method,
            path=path,
            headers=headers,
        )

        protocol = MockRSGIProtocol(request_body=body)
        await app(scope, protocol)

        after_counts = cls.count_objects_by_type()
        blocks_after = sys.getallocatedblocks()

        stats = ProtocolAllocationStats(
            dicts_allocated=max(0, after_counts.get(dict, 0) - before_counts.get(dict, 0)),
            tuples_allocated=max(0, after_counts.get(tuple, 0) - before_counts.get(tuple, 0)),
            bytes_allocated=max(0, after_counts.get(bytes, 0) - before_counts.get(bytes, 0)),
            total_blocks_delta=blocks_after - blocks_before,
        )
        return protocol, stats


def create_granian_compatible_asgi_app() -> ASGIApp:
    """Construct an ASGI application handling JSON ping and echo endpoints."""

    async def app(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope["type"] != "http":
            raise RuntimeError("Only HTTP protocol supported")

        path = scope.get("path", "/")
        method = scope.get("method", "GET")

        if path == "/ping" and method == "GET":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", b"16"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"ping": "pong"}',
                    "more_body": False,
                }
            )
            return

        if path == "/echo" and method == "POST":
            message = await receive()
            body = message.get("body", b"")
            resp_bytes = b'{"received": "' + body + b'"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(resp_bytes)).encode("latin-1")),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": resp_bytes,
                    "more_body": False,
                }
            )
            return

        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"content-length", b"9"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"Not Found",
                "more_body": False,
            }
        )

    return app


def create_rsgi_app() -> RSGIApp:
    """Construct an RSGI 1.6 application using synchronous response emission."""

    async def app(scope: RSGIScopeDescriptor, proto: MockRSGIProtocol) -> None:
        if scope.proto != "http":
            proto.response_str(400, [("content-type", "text/plain")], "Invalid protocol")
            return

        if scope.path == "/ping" and scope.method == "GET":
            proto.response_bytes(
                200,
                [("content-type", "application/json"), ("content-length", "16")],
                b'{"ping": "pong"}',
            )
            return

        if scope.path == "/echo" and scope.method == "POST":
            body = await proto()
            resp = b'{"received": "' + body + b'"}'
            proto.response_bytes(
                200,
                [("content-type", "application/json"), ("content-length", str(len(resp)))],
                resp,
            )
            return

        proto.response_bytes(
            404,
            [("content-type", "text/plain"), ("content-length", "9")],
            b"Not Found",
        )

    return app
