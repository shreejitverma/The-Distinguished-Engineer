"""Streaming digest trailers (RFC 9530) over h11 and gRPC trailers-only status over h2."""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import quote, unquote

import h2.config
import h2.connection
import h2.errors
import h2.events
import h11
from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> None: ...
    def digest(self) -> bytes: ...


HASHERS: Final[Mapping[str, Callable[[], _Hasher]]] = {
    "sha-512": hashlib.sha512,
    "sha-256": hashlib.sha256,
}
"""Supported RFC 9530 Section 7.2 "Active" algorithms, strongest first."""

MERGEABLE_TRAILERS: Final = frozenset({b"repr-digest", b"content-digest"})
"""Fields whose definition permits merging into the header section (RFC 9530 Sections 2-3)."""

_KEY_RE: Final = re.compile(r"[a-z*][a-z0-9_\-.*]*")
_B64_RE: Final = re.compile(r"[A-Za-z0-9+/=]*")


# --- RFC 9530 Integrity field values (RFC 9651 Dictionary of Byte Sequences) ---


def serialize_digest_field(digests: Mapping[str, bytes]) -> bytes:
    """Serialize ``{algorithm: checksum}`` as an RFC 9651 Dictionary of Byte Sequences."""
    members = [f"{key}=:{base64.b64encode(raw).decode('ascii')}:" for key, raw in digests.items()]
    return ", ".join(members).encode("ascii")


def _decode_byte_sequence(b64: str) -> bytes:
    if _B64_RE.fullmatch(b64) is None:
        raise ValueError("byte sequence contains a non-base64 character")
    try:
        # RFC 9651 Section 4.2.7: synthesize missing padding, but excess padding fails.
        return base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
    except binascii.Error as exc:
        raise ValueError(f"invalid base64 in byte sequence: {b64!r}") from exc


def _skip_ows(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def parse_digest_field(value: bytes) -> dict[str, bytes]:
    """Parse a ``Content-Digest`` or ``Repr-Digest`` value; raise ``ValueError`` if malformed.

    Only parameterless Byte Sequence members are accepted, which covers every value RFC 9530
    defines.  Duplicate keys keep the last value, as RFC 9651 Section 4.2.2 requires.
    """
    try:
        text = value.decode("ascii").lstrip(" ")
    except UnicodeDecodeError as exc:
        raise ValueError("digest field value is not ASCII") from exc
    members: dict[str, bytes] = {}
    pos = 0
    while pos < len(text):
        match = _KEY_RE.match(text, pos)
        if match is None:
            raise ValueError(f"invalid dictionary key at offset {pos}")
        key, pos = match.group(), match.end()
        if text[pos : pos + 2] != "=:":
            raise ValueError(f"member {key!r} is not a byte sequence")
        end = text.find(":", pos + 2)
        if end == -1:
            raise ValueError(f"member {key!r} has an unterminated byte sequence")
        members[key] = _decode_byte_sequence(text[pos + 2 : end])
        pos = _skip_ows(text, end + 1)
        if pos == len(text):
            break
        if text[pos] != ",":
            raise ValueError(f"expected ',' after member {key!r}")
        pos = _skip_ows(text, pos + 1)
        if pos == len(text):
            raise ValueError("trailing comma in dictionary")
    return members


class StreamingDigest:
    """Running checksum over content bytes, fed one chunk at a time as they are sent."""

    __slots__ = ("_hasher", "algorithm", "octets")

    def __init__(self, algorithm: str = "sha-256") -> None:
        factory = HASHERS.get(algorithm)
        if factory is None:
            raise ValueError(f"unsupported digest algorithm: {algorithm!r}")
        self.algorithm = algorithm
        self.octets = 0
        self._hasher = factory()

    def update(self, chunk: bytes) -> None:
        self._hasher.update(chunk)
        self.octets += len(chunk)

    def digest(self) -> bytes:
        return self._hasher.digest()

    def field_value(self) -> bytes:
        return serialize_digest_field({self.algorithm: self.digest()})


# --- HTTP/1.1 origin, intermediary, and verifying client on h11 state machines ---


class DigestOutcome(enum.Enum):
    VERIFIED = "verified"
    MISMATCH = "mismatch"
    ABSENT = "absent"
    UNSUPPORTED = "unsupported"


class IntegrityError(Exception):
    """Raised when content is requested before its digest has been verified."""


@dataclass(frozen=True, slots=True)
class VerifiedResponse:
    status: int
    body: bytes
    outcome: DigestOutcome
    digest_source: str | None
    announced_trailers: tuple[bytes, ...]
    trailers: tuple[tuple[bytes, bytes], ...]

    def verified_body(self) -> bytes:
        """Release content only after the digest matched (RFC 9530 Section 6.4)."""
        if self.outcome is not DigestOutcome.VERIFIED:
            raise IntegrityError(f"content not released: digest {self.outcome.value}")
        return self.body


def _send(conn: h11.Connection, event: h11.Event) -> bytes:
    data = conn.send(event)
    if data is None:  # pragma: no cover - only returned for ConnectionClosed
        raise RuntimeError("h11 produced no bytes")
    return data


def _drain(conn: h11.Connection) -> list[h11.Event]:
    events: list[h11.Event] = []
    while True:
        event = conn.next_event()
        if not isinstance(event, h11.Event):  # h11.NEED_DATA or h11.PAUSED sentinel
            return events
        events.append(event)
        if isinstance(event, h11.EndOfMessage):
            return events


def accept_request(conn: h11.Connection, raw_request: bytes) -> h11.Request:
    """Feed one bodyless request into a server-role connection and return it."""
    conn.receive_data(raw_request)
    events = _drain(conn)
    if not events or not isinstance(events[0], h11.Request):
        raise ValueError(f"expected a complete request, got {events!r}")
    return events[0]


def serve_streaming_response(
    conn: h11.Connection,
    chunks: Iterable[bytes],
    *,
    content_type: bytes = b"application/json",
    algorithm: str = "sha-256",
) -> bytes:
    """Stream a ``200`` response and put the running ``Repr-Digest`` in the trailer section.

    The checksum is updated per chunk, so the origin never buffers the representation.
    """
    hasher = StreamingDigest(algorithm)
    headers = [(b"content-type", content_type), (b"trailer", b"Repr-Digest")]
    wire = [_send(conn, h11.Response(status_code=200, headers=headers))]
    for chunk in chunks:
        hasher.update(chunk)
        wire.append(_send(conn, h11.Data(data=chunk)))
    wire.append(_send(conn, h11.EndOfMessage(headers=[(b"repr-digest", hasher.field_value())])))
    return b"".join(wire)


def _client_request(conn: h11.Connection, target: bytes) -> None:
    """Send a GET that promises not to discard trailers (RFC 9110 Section 10.1.4)."""
    headers = [(b"host", b"foo.example"), (b"te", b"trailers"), (b"connection", b"te")]
    _send(conn, h11.Request(method=b"GET", target=target, headers=headers))
    _send(conn, h11.EndOfMessage())


def _read_response(
    raw_response: bytes, *, target: bytes
) -> tuple[h11.Response, list[bytes], list[tuple[bytes, bytes]]]:
    conn = h11.Connection(h11.CLIENT)
    _client_request(conn, target)
    conn.receive_data(raw_response)
    events = _drain(conn)
    if len(events) < 2 or not isinstance(events[-1], h11.EndOfMessage):
        raise ValueError(f"incomplete response: {events!r}")
    response = events[0]
    if not isinstance(response, h11.Response):
        raise ValueError(f"expected a final response, got {response!r}")
    chunks = [bytes(e.data) for e in events if isinstance(e, h11.Data)]
    trailers = [(bytes(k), bytes(v)) for k, v in events[-1].headers]
    return response, chunks, trailers


def fetch_and_verify(raw_response: bytes, *, target: bytes = b"/items/123") -> VerifiedResponse:
    """Parse *raw_response* as a client, hashing each chunk on arrival, then check the digest.

    The algorithm is only known once the trailer arrives, so every supported algorithm runs
    in parallel over the content.  A trailer value takes precedence over a header value.
    """
    response, chunks, trailers = _read_response(raw_response, target=target)
    running = {name: StreamingDigest(name) for name in HASHERS}
    for chunk in chunks:
        for hasher in running.values():
            hasher.update(chunk)
    headers = [(bytes(k), bytes(v)) for k, v in response.headers]
    announced = tuple(
        name.strip().lower()
        for k, v in headers
        if k == b"trailer"
        for name in v.split(b",")
        if name.strip()
    )
    source, raw_value = None, None
    for origin, section in (("trailer", trailers), ("header", headers)):
        lines = [v for k, v in section if k == b"repr-digest"]
        if lines:
            source, raw_value = origin, b", ".join(lines)
            break
    outcome = _check_digest(raw_value, running)
    return VerifiedResponse(
        status=response.status_code,
        body=b"".join(chunks),
        outcome=outcome,
        digest_source=source,
        announced_trailers=announced,
        trailers=tuple(trailers),
    )


def _check_digest(raw_value: bytes | None, running: Mapping[str, StreamingDigest]) -> DigestOutcome:
    if raw_value is None:
        return DigestOutcome.ABSENT
    try:
        claimed = parse_digest_field(raw_value)
    except ValueError:
        return DigestOutcome.ABSENT  # RFC 9651 Section 4.2: a field that fails parsing is ignored
    for name in HASHERS:  # strongest supported algorithm first
        if name in claimed:
            ok = hmac.compare_digest(claimed[name], running[name].digest())
            return DigestOutcome.VERIFIED if ok else DigestOutcome.MISMATCH
    return DigestOutcome.UNSUPPORTED


def buffering_proxy(raw_response: bytes, *, merge_trailers: bool) -> bytes:
    """Model an HTTP/1.1 hop that de-chunks, buffers, and re-frames with ``Content-Length``.

    Trailers are dropped (the common case in RFC 9110 Section 6.5.1) unless *merge_trailers*
    is set, in which case only fields whose definition permits merging are moved into the
    header section and every other trailer field is still discarded.
    """
    response, chunks, trailers = _read_response(raw_response, target=b"/")
    body = b"".join(chunks)
    headers = [
        (bytes(k), bytes(v))
        for k, v in response.headers
        if k not in (b"transfer-encoding", b"content-length")
    ]
    if merge_trailers:
        headers += [(k, v) for k, v in trailers if k in MERGEABLE_TRAILERS]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    downstream = h11.Connection(h11.SERVER)
    accept_request(downstream, b"GET / HTTP/1.1\r\nHost: foo.example\r\n\r\n")
    return b"".join(
        [
            _send(downstream, h11.Response(status_code=response.status_code, headers=headers)),
            _send(downstream, h11.Data(data=body)),
            _send(downstream, h11.EndOfMessage()),
        ]
    )


# --- ASGI: Content-Digest computed over the bytes that actually leave the stack ---


def trailers_allowed(scope: Scope) -> bool:
    """True when the server offers the ASGI trailers extension and the client sent ``TE``."""
    if "http.response.trailers" not in scope.get("extensions", {}):
        return False
    te_values = [v for k, v in scope["headers"] if k == b"te"]
    return any(m.strip().lower() == b"trailers" for v in te_values for m in v.split(b","))


class ContentDigestMiddleware:
    """Emit ``Content-Digest`` for whatever content the wrapped app finally sends.

    With the trailers extension the checksum streams into a trailer; without it the body is
    buffered and the digest goes in the header section, which costs memory proportional to
    the response.  Place it outermost so it hashes bytes after every content transformation.
    """

    def __init__(self, app: ASGIApp, *, algorithm: str = "sha-256") -> None:
        StreamingDigest(algorithm)  # fail fast on an unsupported algorithm
        self.app = app
        self.algorithm = algorithm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hasher = StreamingDigest(self.algorithm)
        if trailers_allowed(scope):
            await self.app(scope, receive, self._trailer_sender(send, hasher))
        else:
            head = scope.get("method") == "HEAD"
            await self.app(scope, receive, self._buffering_sender(send, hasher, head=head))

    @staticmethod
    def _trailer_sender(send: Send, hasher: StreamingDigest) -> Send:
        app_trailers = False

        async def wrapped(message: Message) -> None:
            nonlocal app_trailers
            if message["type"] == "http.response.start":
                app_trailers = bool(message.get("trailers", False))
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                headers.append("trailer", "Content-Digest")
                await send({**message, "headers": headers.raw, "trailers": True})
            elif message["type"] == "http.response.body":
                hasher.update(message.get("body", b""))
                await send(message)
                if not message.get("more_body", False) and not app_trailers:
                    field = (b"content-digest", hasher.field_value())
                    await send({"type": "http.response.trailers", "headers": [field]})
            elif message["type"] == "http.response.trailers" and not message.get(
                "more_trailers", False
            ):
                field = (b"content-digest", hasher.field_value())
                await send({**message, "headers": [*message.get("headers", []), field]})
            else:
                await send(message)

        return wrapped

    @staticmethod
    def _buffering_sender(send: Send, hasher: StreamingDigest, *, head: bool) -> Send:
        start: Message | None = None
        parts: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body" and start is not None:
                parts.append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                body = b"".join(parts)
                hasher.update(body)
                headers = MutableHeaders(raw=list(start.get("headers", [])))
                headers["content-digest"] = hasher.field_value().decode("ascii")
                status = int(start["status"])
                if not (head or status < 200 or status in (204, 304)):
                    headers["content-length"] = str(len(body))
                await send({**start, "headers": headers.raw})
                await send({"type": "http.response.body", "body": body})
            else:
                await send(message)

        return wrapped


# --- gRPC over HTTP/2: status in trailers, or in a single Trailers-Only HEADERS frame ---


class GrpcCode(enum.IntEnum):
    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16


HTTP_TO_GRPC: Final[Mapping[int, GrpcCode]] = {
    400: GrpcCode.INTERNAL,
    401: GrpcCode.UNAUTHENTICATED,
    403: GrpcCode.PERMISSION_DENIED,
    404: GrpcCode.UNIMPLEMENTED,
    429: GrpcCode.UNAVAILABLE,
    502: GrpcCode.UNAVAILABLE,
    503: GrpcCode.UNAVAILABLE,
    504: GrpcCode.UNAVAILABLE,
}
"""grpc doc/http-grpc-status-mapping.md; only for responses without ``grpc-status``."""

RST_STREAM_TO_GRPC: Final[Mapping[int, GrpcCode]] = {
    h2.errors.ErrorCodes.REFUSED_STREAM: GrpcCode.UNAVAILABLE,
    h2.errors.ErrorCodes.CANCEL: GrpcCode.CANCELLED,
    h2.errors.ErrorCodes.ENHANCE_YOUR_CALM: GrpcCode.RESOURCE_EXHAUSTED,
    h2.errors.ErrorCodes.INADEQUATE_SECURITY: GrpcCode.PERMISSION_DENIED,
}
"""grpc doc/PROTOCOL-HTTP2.md "Errors"; every other code, including NO_ERROR, is INTERNAL."""

_GRPC_MESSAGE_SAFE: Final = "".join(chr(c) for c in range(0x20, 0x7F) if chr(c) != "%")


@dataclass(frozen=True, slots=True)
class GrpcOutcome:
    code: GrpcCode
    message: str
    trailers_only: bool
    synthesized: bool
    messages: tuple[bytes, ...]


def frame_grpc_message(payload: bytes) -> bytes:
    """Length-Prefixed-Message: 1-byte Compressed-Flag, 4-byte big-endian length, payload."""
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def parse_grpc_messages(data: bytes) -> tuple[bytes, ...]:
    out: list[bytes] = []
    pos = 0
    while pos < len(data):
        if pos + 5 > len(data):
            raise ValueError("truncated gRPC message prefix")
        length = int.from_bytes(data[pos + 1 : pos + 5], "big")
        end = pos + 5 + length
        if end > len(data):
            raise ValueError("truncated gRPC message body")
        out.append(data[pos + 5 : end])
        pos = end
    return tuple(out)


def _h2_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    return client, server


def reply_with_trailers(
    server: h2.connection.H2Connection, stream_id: int, payload: bytes = b"ok"
) -> None:
    """Normal response: HEADERS, DATA, then HEADERS(END_STREAM) carrying ``grpc-status: 0``."""
    server.send_headers(stream_id, [(":status", "200"), ("content-type", "application/grpc")])
    server.send_data(stream_id, frame_grpc_message(payload))
    server.send_headers(stream_id, [("grpc-status", "0")], end_stream=True)


def reply_trailers_only(
    server: h2.connection.H2Connection, stream_id: int, code: GrpcCode, message: str
) -> None:
    """Immediate error: one HEADERS frame with END_STREAM holding status and trailers."""
    server.send_headers(
        stream_id,
        [
            (":status", "200"),
            ("content-type", "application/grpc"),
            ("grpc-status", str(int(code))),
            ("grpc-message", quote(message.encode("utf-8"), safe=_GRPC_MESSAGE_SAFE)),
        ],
        end_stream=True,
    )


def reply_proxy_error(server: h2.connection.H2Connection, stream_id: int, status: int) -> None:
    """An intermediary answering with an HTML error page and no ``grpc-status`` at all."""
    server.send_headers(stream_id, [(":status", str(status)), ("content-type", "text/html")])
    server.send_data(stream_id, b"<html>upstream unavailable</html>", end_stream=True)


def run_unary_call(reply: Callable[[h2.connection.H2Connection, int], None]) -> GrpcOutcome:
    """Drive one unary gRPC call between in-memory h2 client and server state machines."""
    client, server = _h2_pair()
    stream_id = client.get_next_available_stream_id()
    client.send_headers(
        stream_id,
        [
            (":method", "POST"),
            (":scheme", "http"),
            (":path", "/inventory.v1.Items/Get"),
            (":authority", "grpc.test"),
            ("te", "trailers"),
            ("content-type", "application/grpc"),
        ],
    )
    client.send_data(stream_id, frame_grpc_message(b"item-42"), end_stream=True)
    server.receive_data(client.data_to_send())
    reply(server, stream_id)
    return resolve_grpc_status(client.receive_data(server.data_to_send()))


def _parse_grpc_status(value: bytes | None) -> GrpcCode | None:
    """``grpc-status`` is ``1*DIGIT``; an unparseable or unknown value counts as absent."""
    if value is None or not value.isdigit():
        return None
    try:
        return GrpcCode(int(value))
    except ValueError:
        return None


def resolve_grpc_status(events: Sequence[h2.events.Event]) -> GrpcOutcome:
    """Find ``grpc-status`` in trailers or a Trailers-Only block, else synthesize one."""
    headers: dict[bytes, bytes] = {}
    trailers: dict[bytes, bytes] | None = None
    trailers_only = False
    data = bytearray()
    for event in events:
        if isinstance(event, h2.events.ResponseReceived):
            headers = {bytes(k): bytes(v) for k, v in event.headers or []}
            trailers_only = event.stream_ended is not None
        elif isinstance(event, h2.events.DataReceived):
            data += event.data or b""
        elif isinstance(event, h2.events.TrailersReceived):
            trailers = {bytes(k): bytes(v) for k, v in event.headers or []}
        elif isinstance(event, h2.events.StreamReset):
            code = RST_STREAM_TO_GRPC.get(event.error_code or 0, GrpcCode.INTERNAL)
            return GrpcOutcome(code, f"RST_STREAM {event.error_code!r}", False, True, ())
    status_block = (headers if trailers_only else trailers) or {}
    status = _parse_grpc_status(status_block.get(b"grpc-status"))
    if status is not None:
        return GrpcOutcome(
            code=status,
            message=unquote(status_block.get(b"grpc-message", b"").decode("ascii")),
            trailers_only=trailers_only,
            synthesized=False,
            messages=parse_grpc_messages(bytes(data)),
        )
    http_status = int(headers.get(b":status", b"0"))
    code = HTTP_TO_GRPC.get(http_status, GrpcCode.UNKNOWN)  # 200 is UNKNOWN: OK needs grpc-status
    return GrpcOutcome(code, f"no grpc-status (HTTP {http_status})", trailers_only, True, ())
