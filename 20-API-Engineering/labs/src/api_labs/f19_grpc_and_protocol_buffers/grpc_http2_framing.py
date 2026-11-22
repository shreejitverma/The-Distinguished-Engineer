"""gRPC over HTTP/2: 5-byte message envelopes, request header rules, and Trailers-Only status.

Everything here runs on in-memory ``h2`` state machines and plain bytes; there is no network.
Normative source: grpc/grpc ``doc/PROTOCOL-HTTP2.md`` and ``doc/compression.md``.
"""

from __future__ import annotations

import base64
import enum
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

import h2.config
import h2.connection
import h2.errors
import h2.events
from hyperframe.frame import Frame

PREFIX_LEN: Final = 5
"""1-byte Compressed-Flag plus 4-byte big-endian Message-Length."""

MAX_WIRE_LENGTH: Final = 2**32 - 1
"""Largest value the 4-byte Message-Length field can carry."""

DEFAULT_MAX_RECV: Final = 4 * 1024 * 1024
"""Default receive limit (4 MiB), documented for ``grpc.max_receive_message_length``."""


class Code(enum.IntEnum):
    """The 17 canonical gRPC status codes (grpc/grpc doc/statuscodes.md)."""

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


class FramingError(Exception):
    """A violation of the message envelope, carrying the gRPC status the peer should see."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- Length-Prefixed-Message ---


@dataclass(frozen=True, slots=True)
class Message:
    compressed: bool
    payload: bytes


def encode_message(payload: bytes, *, compressed: bool = False) -> bytes:
    """Return ``Compressed-Flag (1) || Message-Length (4, big endian) || Message``."""
    if len(payload) > MAX_WIRE_LENGTH:
        raise FramingError(Code.RESOURCE_EXHAUSTED, f"message too large ({len(payload)} bytes)")
    return bytes([1 if compressed else 0]) + len(payload).to_bytes(4, "big") + payload


class MessageDecoder:
    """Incremental decoder: DATA frame boundaries have no relation to message boundaries.

    The length is checked against ``max_message_size`` as soon as the 5-byte prefix is complete,
    before any of the body is buffered, which is where grpc core and grpc-go also check it.
    """

    __slots__ = ("_buf", "max_message_size")

    def __init__(self, max_message_size: int = DEFAULT_MAX_RECV) -> None:
        self.max_message_size = max_message_size
        self._buf = bytearray()

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[Message]:
        self._buf += data
        out: list[Message] = []
        while len(self._buf) >= PREFIX_LEN:
            flag = self._buf[0]
            if flag not in (0, 1):
                raise FramingError(Code.INTERNAL, f"Bad GRPC frame type 0x{flag:02x}")
            length = int.from_bytes(self._buf[1:PREFIX_LEN], "big")
            if length > self.max_message_size:
                raise FramingError(
                    Code.RESOURCE_EXHAUSTED,
                    f"received message larger than max ({length} vs. {self.max_message_size})",
                )
            end = PREFIX_LEN + length
            if len(self._buf) < end:
                break
            out.append(Message(compressed=flag == 1, payload=bytes(self._buf[PREFIX_LEN:end])))
            del self._buf[:end]
        return out

    def finish(self) -> None:
        """Call at END_STREAM: leftover bytes mean the stream ended mid-message."""
        if self._buf:
            raise FramingError(Code.INTERNAL, "unexpected EOF inside a length-prefixed message")


# --- Message-Encoding (grpc-encoding) ---

_WBITS: Final = {"gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}
"""``deflate`` is the zlib structure (RFC 1950), never raw deflate (doc/compression.md)."""


def compress(payload: bytes, encoding: str) -> bytes:
    """Compress one message with a fresh context (contexts never span messages)."""
    wbits = _WBITS[encoding]
    comp = zlib.compressobj(wbits=wbits)
    return comp.compress(payload) + comp.flush()


def decode_payload(
    message: Message,
    encoding: str | None,
    *,
    is_server: bool,
    max_size: int = DEFAULT_MAX_RECV,
) -> bytes:
    """Apply the Compressed-Flag rules and return the message bytes.

    Mirrors grpc-go ``checkRecvPayload`` and ``decompress`` for the three failure shapes:
    flag set with ``identity`` or no ``grpc-encoding`` is ``INTERNAL``; an unknown encoding is
    ``UNIMPLEMENTED`` on a server and ``INTERNAL`` on a client; decompressed output larger
    than ``max_size`` is ``RESOURCE_EXHAUSTED`` and is detected without inflating the rest.
    """
    if not message.compressed:
        return message.payload
    if encoding is None or encoding == "identity":
        raise FramingError(Code.INTERNAL, "compressed flag set with identity or empty encoding")
    wbits = _WBITS.get(encoding)
    if wbits is None:
        code = Code.UNIMPLEMENTED if is_server else Code.INTERNAL
        raise FramingError(code, f"Decompressor is not installed for grpc-encoding {encoding!r}")
    inflater = zlib.decompressobj(wbits=wbits)
    try:
        out = inflater.decompress(message.payload, max_size + 1)
    except zlib.error as exc:
        raise FramingError(Code.INTERNAL, f"failed to decompress the message: {exc}") from exc
    if len(out) > max_size:
        raise FramingError(
            Code.RESOURCE_EXHAUSTED, f"message after decompression larger than max ({max_size})"
        )
    return out


# --- grpc-message percent encoding and -bin metadata ---

_MESSAGE_SAFE: Final = "".join(chr(c) for c in range(0x20, 0x7F) if chr(c) != "%")


def encode_status_message(text: str) -> str:
    """UTF-8, then percent-encode everything outside ``%x20-7E`` except ``%`` (upper-case hex)."""
    return quote(text.encode("utf-8"), safe=_MESSAGE_SAFE)


def decode_status_message(value: str) -> str:
    """Permissive decode: a broken ``%`` escape is kept as-is and invalid UTF-8 is replaced."""
    out = bytearray()
    i = 0
    raw = value.encode("ascii", "replace")
    while i < len(raw):
        if raw[i : i + 1] == b"%" and len(raw) - i >= 3:
            try:
                out += bytes([int(raw[i + 1 : i + 3], 16)])
                i += 3
                continue
            except ValueError:
                pass
        out += raw[i : i + 1]
        i += 1
    return out.decode("utf-8", "replace")


def encode_binary_metadata(value: bytes) -> str:
    """Emit un-padded base64 for a ``-bin`` header, as the spec says senders should."""
    return base64.b64encode(value).decode("ascii").rstrip("=")


def decode_binary_metadata(value: str) -> list[bytes]:
    """Split on ``,`` first, then base64-decode each part, accepting padded and un-padded."""
    return [base64.b64decode(p.strip() + "=" * (-len(p.strip()) % 4)) for p in value.split(",")]


# --- Request-Headers ---

Headers = Sequence[tuple[str, str]]


def build_request_headers(
    service: str,
    method: str,
    *,
    authority: str,
    scheme: str = "http",
    content_type: str = "application/grpc",
    timeout: str | None = None,
    encoding: str | None = None,
    metadata: Iterable[tuple[str, str]] = (),
) -> list[tuple[str, str]]:
    """Call-Definition in the order the spec recommends, then Custom-Metadata."""
    headers = [
        (":method", "POST"),
        (":scheme", scheme),
        (":path", f"/{service}/{method}"),
        (":authority", authority),
    ]
    if timeout is not None:
        headers.append(("grpc-timeout", timeout))
    headers += [("te", "trailers"), ("content-type", content_type)]
    if encoding is not None:
        headers.append(("grpc-encoding", encoding))
    headers += list(metadata)
    return headers


@dataclass(frozen=True, slots=True)
class EarlyAbort:
    """A server rejection decided from the request headers alone, before any message."""

    http_status: int
    code: Code
    message: str


def check_request_headers(headers: Headers) -> EarlyAbort | None:
    """Server-side gate on the Call-Definition; ``None`` means the call may proceed.

    A non-gRPC ``content-type`` gets HTTP 415 so that a generic HTTP/2 client never mistakes
    the response (whose gRPC form is always HTTP 200) for success.
    """
    seen = dict(headers)
    if seen.get(":method") != "POST":
        return EarlyAbort(405, Code.INTERNAL, f"invalid :method {seen.get(':method')!r}")
    content_type = seen.get("content-type", "").lower()
    base = "application/grpc"
    if content_type != base and not (
        content_type.startswith(base) and content_type[len(base)] in "+;"
    ):
        return EarlyAbort(
            415, Code.INVALID_ARGUMENT, f"invalid gRPC request content-type {content_type!r}"
        )
    path = seen.get(":path", "")
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "" or not parts[1] or not parts[2]:
        return EarlyAbort(200, Code.UNIMPLEMENTED, f"malformed method {path!r}")
    return None


# --- Response: HEADERS + DATA + trailers, or Trailers-Only ---


class Shape(enum.Enum):
    TRAILERS_ONLY = "trailers-only"
    HEADERS_AND_TRAILERS = "headers+trailers"
    SYNTHESIZED = "synthesized"


@dataclass(frozen=True, slots=True)
class CallResult:
    shape: Shape
    code: Code
    message: str
    messages: tuple[bytes, ...]
    frames_before_status: int


HTTP_TO_GRPC: Final = {
    400: Code.INTERNAL,
    401: Code.UNAUTHENTICATED,
    403: Code.PERMISSION_DENIED,
    404: Code.UNIMPLEMENTED,
    429: Code.UNAVAILABLE,
    502: Code.UNAVAILABLE,
    503: Code.UNAVAILABLE,
    504: Code.UNAVAILABLE,
}
"""doc/http-grpc-status-mapping.md; used only when the response carries no ``grpc-status``."""

RST_TO_GRPC: Final = {
    h2.errors.ErrorCodes.REFUSED_STREAM: Code.UNAVAILABLE,
    h2.errors.ErrorCodes.CANCEL: Code.CANCELLED,
    h2.errors.ErrorCodes.ENHANCE_YOUR_CALM: Code.RESOURCE_EXHAUSTED,
    h2.errors.ErrorCodes.INADEQUATE_SECURITY: Code.PERMISSION_DENIED,
}
"""doc/PROTOCOL-HTTP2.md "Errors"; every other RST_STREAM code, including NO_ERROR, is INTERNAL."""


def _status_of(block: dict[str, str]) -> Code | None:
    raw = block.get("grpc-status")
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    try:
        return Code(int(raw))
    except ValueError:
        return None


def resolve_call(events: Iterable[h2.events.Event]) -> CallResult:
    """Fold client-side ``h2`` events into one result, synthesizing a status when none came."""
    headers: dict[str, str] = {}
    trailers: dict[str, str] = {}
    trailers_only = False
    decoder = MessageDecoder(max_message_size=MAX_WIRE_LENGTH)
    messages: list[bytes] = []
    data_frames = 0
    is_grpc = False
    for event in events:
        if isinstance(event, h2.events.ResponseReceived):
            headers = {_s(k): _s(v) for k, v in event.headers or []}
            trailers_only = event.stream_ended is not None
            is_grpc = headers.get("content-type", "").lower().startswith("application/grpc")
        elif isinstance(event, h2.events.DataReceived):
            data_frames += 1
            if is_grpc:  # an HTML error body from a proxy is not a Length-Prefixed-Message
                messages += [m.payload for m in decoder.feed(event.data or b"")]
        elif isinstance(event, h2.events.TrailersReceived):
            trailers = {_s(k): _s(v) for k, v in event.headers or []}
        elif isinstance(event, h2.events.StreamReset):
            code = RST_TO_GRPC.get(event.error_code or 0, Code.INTERNAL)
            return CallResult(Shape.SYNTHESIZED, code, f"RST_STREAM {event.error_code}", (), 0)
    block = headers if trailers_only else trailers
    status = _status_of(block)
    if status is None:
        http = int(headers.get(":status", "0"))
        code = HTTP_TO_GRPC.get(http, Code.UNKNOWN)  # 200 maps to UNKNOWN: OK needs grpc-status
        return CallResult(Shape.SYNTHESIZED, code, f"no grpc-status (HTTP {http})", (), 0)
    shape = Shape.TRAILERS_ONLY if trailers_only else Shape.HEADERS_AND_TRAILERS
    message = decode_status_message(block.get("grpc-message", ""))
    return CallResult(shape, status, message, tuple(messages), data_frames)


def _s(value: bytes | str) -> str:
    return value.decode("ascii") if isinstance(value, bytes) else value


# --- In-memory HTTP/2 exchange and a frame dump of the raw bytes ---

Responder = Callable[[h2.connection.H2Connection, int], None]


@dataclass(frozen=True, slots=True)
class Exchange:
    request_bytes: bytes
    response_bytes: bytes
    result: CallResult


def h2_pair(
    max_frame: int = 16384,
) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.max_outbound_frame_size = max_frame
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    return client, server


def run_unary(
    request: bytes,
    respond: Responder,
    *,
    headers: Headers | None = None,
    max_frame: int = 16384,
) -> Exchange:
    """Send one request message, let ``respond`` write the response, return bytes and result."""
    client, server = h2_pair(max_frame)
    sid = client.get_next_available_stream_id()
    client.send_headers(
        sid, headers or build_request_headers("demo.v1.Echo", "Say", authority="grpc.test")
    )
    client.send_data(sid, encode_message(request), end_stream=True)
    request_bytes = client.data_to_send()
    server.receive_data(request_bytes)
    respond(server, sid)
    response_bytes = server.data_to_send()
    return Exchange(
        request_bytes, response_bytes, resolve_call(client.receive_data(response_bytes))
    )


def send_chunked(server: h2.connection.H2Connection, sid: int, data: bytes) -> None:
    """Split ``data`` into DATA frames; ``h2`` raises ``FrameTooLargeError`` for a bigger one."""
    step = server.max_outbound_frame_size
    for i in range(0, len(data), step):
        server.send_data(sid, data[i : i + step])


def respond_ok(payload: bytes) -> Responder:
    """HEADERS, DATA, then HEADERS(END_STREAM) holding ``grpc-status: 0``."""

    def respond(server: h2.connection.H2Connection, sid: int) -> None:
        server.send_headers(sid, [(":status", "200"), ("content-type", "application/grpc+proto")])
        send_chunked(server, sid, encode_message(payload))
        server.send_headers(sid, [("grpc-status", "0")], end_stream=True)

    return respond


def respond_trailers_only(code: Code, message: str) -> Responder:
    """One HEADERS frame with END_STREAM carrying status together with ``:status`` and type."""

    def respond(server: h2.connection.H2Connection, sid: int) -> None:
        server.send_headers(
            sid,
            [
                (":status", "200"),
                ("content-type", "application/grpc"),
                ("grpc-status", str(int(code))),
                ("grpc-message", encode_status_message(message)),
            ],
            end_stream=True,
        )

    return respond


def dump_frames(raw: bytes) -> list[str]:
    """List ``TYPE flags length`` for every HTTP/2 frame in ``raw`` (hyperframe wire parser)."""
    lines: list[str] = []
    pos = 0
    while pos < len(raw):
        frame, length = Frame.parse_frame_header(memoryview(raw[pos : pos + 9]))
        frame.parse_body(memoryview(raw[pos + 9 : pos + 9 + length]))
        flags = "|".join(sorted(frame.flags)) or "-"
        lines.append(f"{type(frame).__name__.removesuffix('Frame').upper()} {flags} len={length}")
        pos += 9 + length
    return lines
