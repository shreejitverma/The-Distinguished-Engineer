"""The RFC 9110 Section 6 message abstraction mapped onto HTTP/1.1, HTTP/2, and HTTP/3 framing.

A message is control data, a header table, content, and a trailer table.
Each major HTTP version serializes the same four parts differently; this module encodes an
abstract exchange with a real wire library per version (h11, h2, aioquic plus pylsqpack),
decodes it back, and returns both the abstract result and the field blocks seen on the wire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.events
import h11
import pylsqpack
from aioquic.h3.connection import (
    FrameType,
    encode_frame,
    validate_request_headers,
    validate_response_headers,
    validate_trailers,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

Field = tuple[bytes, bytes]
FieldTable = dict[bytes, tuple[bytes, ...]]
Version = Literal["HTTP/1.1", "HTTP/2", "HTTP/3"]

# RFC 9110 Section 5.6.2 tchar, restricted to lowercase so every stored name is canonical.
_FIELD_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9a-z]+")
_FORBIDDEN_VALUE_OCTETS: Final = frozenset(b"\x00\r\n")
# RFC 9110 Section 7.6.1: Connection plus the fields it lists as connection-specific.
CONNECTION_SPECIFIC: Final = frozenset(
    {b"connection", b"proxy-connection", b"keep-alive", b"te", b"transfer-encoding", b"upgrade"}
)
# Host is control data (the target URI authority) and Content-Length is derived from content,
# so neither is stored in the abstract header table; each serializer regenerates what it needs.
_NOT_IN_TABLE: Final = frozenset({b"host", b"content-length"})
_H3_STREAM_ID: Final = 0  # first client-initiated bidirectional QUIC stream (RFC 9000 Section 2.1)


def validate_field_line(name: bytes, value: bytes) -> None:
    """Reject a field line that RFC 9110 Sections 5.1 and 5.5 make invalid or dangerous."""
    if not _FIELD_NAME.fullmatch(name):
        raise ValueError(f"field name is not a lowercase token: {name!r}")
    if _FORBIDDEN_VALUE_OCTETS.intersection(value):
        raise ValueError(f"CR, LF, or NUL in field value of {name!r}")
    if value[:1] in (b" ", b"\t") or value[-1:] in (b" ", b"\t"):
        raise ValueError(f"leading or trailing whitespace in field value of {name!r}")


def _check_section(fields: tuple[Field, ...]) -> None:
    for name, value in fields:
        validate_field_line(name, value)
        if name in CONNECTION_SPECIFIC or name in _NOT_IN_TABLE:
            raise ValueError(f"{name!r} is not end-to-end message data")


def field_table(fields: Iterable[Field]) -> FieldTable:
    """Build the lookup table RFC 9110 Section 6 describes.

    Order across different names is not significant; order within one name is (Section 5.3),
    so values are kept as an ordered tuple per name and the dict compares order-insensitively.
    """
    table: dict[bytes, list[bytes]] = {}
    for name, value in fields:
        table.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in table.items()}


def combine_field_values(fields: Iterable[Field], name: bytes) -> bytes | None:
    """Combine repeated field lines into one field value (RFC 9110 Sections 5.2 and 5.3)."""
    if name == b"set-cookie":
        raise ValueError("Set-Cookie field lines cannot be combined (RFC 9110 Section 5.3)")
    values = [value for field_name, value in fields if field_name == name]
    if not values:
        return None
    # Cookie crumbs rejoin with "; ", not ", " (RFC 9113 Section 8.2.3).
    separator = b"; " if name == b"cookie" else b", "
    return separator.join(values)


def strip_connection_specific(fields: Iterable[Field]) -> tuple[Field, ...]:
    """Drop Connection, every field it names, and the fixed hop-by-hop set (RFC 9110 7.6.1)."""
    materialized = tuple(fields)
    options = {
        option.strip().lower()
        for name, value in materialized
        if name == b"connection"
        for option in value.split(b",")
        if option.strip()
    }
    dropped = CONNECTION_SPECIFIC | options
    return tuple((name, value) for name, value in materialized if name not in dropped)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """Version-independent request: control data, headers, content, trailers."""

    method: bytes
    scheme: bytes
    authority: bytes
    path: bytes
    headers: tuple[Field, ...] = ()
    content: bytes = b""
    trailers: tuple[Field, ...] = ()

    def __post_init__(self) -> None:
        if not _FIELD_NAME.fullmatch(self.method.lower()):
            raise ValueError(f"method is not a token: {self.method!r}")
        if not (self.path.startswith(b"/") or self.path == b"*"):
            raise ValueError(f"path must be origin-form or '*': {self.path!r}")
        _check_section(self.headers)
        _check_section(self.trailers)

    def semantics(self) -> tuple[bytes, bytes, bytes, bytes, FieldTable, bytes, FieldTable]:
        """The parts a recipient may rely on; field order across names is excluded."""
        return (
            self.method,
            self.scheme,
            self.authority,
            self.path,
            field_table(self.headers),
            self.content,
            field_table(self.trailers),
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Version-independent final response; the HTTP/1.1 reason phrase is not part of it."""

    status: int
    headers: tuple[Field, ...] = ()
    content: bytes = b""
    trailers: tuple[Field, ...] = ()

    def __post_init__(self) -> None:
        if not 200 <= self.status <= 599:
            raise ValueError(f"final status code out of range: {self.status}")
        _check_section(self.headers)
        _check_section(self.trailers)

    def semantics(self) -> tuple[int, FieldTable, bytes, FieldTable]:
        """The parts a recipient may rely on; field order across names is excluded."""
        return (
            self.status,
            field_table(self.headers),
            self.content,
            field_table(self.trailers),
        )


@dataclass(frozen=True, slots=True)
class Exchange:
    """One request/response pair after a full encode -> wire -> decode trip."""

    version: Version
    request: HttpRequest
    response: HttpResponse
    request_wire: bytes
    response_wire: bytes
    request_field_block: tuple[Field, ...]
    response_field_block: tuple[Field, ...]


def _plain(fields: Iterable[tuple[bytes, bytes]]) -> tuple[Field, ...]:
    return tuple((bytes(name), bytes(value)) for name, value in fields)


def _split_pseudo(block: tuple[Field, ...]) -> tuple[dict[bytes, bytes], tuple[Field, ...]]:
    pseudo = {name: value for name, value in block if name.startswith(b":")}
    regular = tuple((name, value) for name, value in block if not name.startswith(b":"))
    return pseudo, regular


def _end_to_end(fields: tuple[Field, ...]) -> tuple[Field, ...]:
    return tuple(f for f in strip_connection_specific(fields) if f[0] not in _NOT_IN_TABLE)


def _require(pseudo: dict[bytes, bytes], name: bytes) -> bytes:
    if name not in pseudo:
        raise ValueError(f"missing pseudo-header field {name.decode()}")
    return pseudo[name]


def _request_from_block(
    block: tuple[Field, ...], content: bytes, trailers: tuple[Field, ...]
) -> HttpRequest:
    """Map HTTP/2 or HTTP/3 request pseudo-headers back to control data (RFC 9113 8.3.1)."""
    pseudo, regular = _split_pseudo(block)
    return HttpRequest(
        method=_require(pseudo, b":method"),
        scheme=_require(pseudo, b":scheme"),
        authority=_require(pseudo, b":authority"),
        path=_require(pseudo, b":path"),
        headers=_end_to_end(regular),
        content=content,
        trailers=_end_to_end(trailers),
    )


def _response_from_block(
    block: tuple[Field, ...], content: bytes, trailers: tuple[Field, ...]
) -> HttpResponse:
    pseudo, regular = _split_pseudo(block)
    return HttpResponse(
        status=int(_require(pseudo, b":status")),
        headers=_end_to_end(regular),
        content=content,
        trailers=_end_to_end(trailers),
    )


# --- HTTP/1.1 (RFC 9112) -------------------------------------------------------------------


def _h1_framing(content: bytes, trailers: tuple[Field, ...], *, declare_empty: bool) -> list[Field]:
    if trailers:  # a trailer section exists only inside the chunked coding (RFC 9112 7.1.2)
        return [(b"transfer-encoding", b"chunked")]
    if content or declare_empty:  # never on 204 (RFC 9110 Section 8.6)
        return [(b"content-length", str(len(content)).encode("ascii"))]
    return []


def _h1_body(conn: h11.Connection) -> tuple[bytes, tuple[Field, ...]]:
    parts: list[bytes] = []
    while True:
        event = conn.next_event()
        if isinstance(event, h11.Data):
            parts.append(bytes(event.data))
        elif isinstance(event, h11.EndOfMessage):
            return b"".join(parts), _plain(event.headers)
        else:
            raise ValueError(f"incomplete HTTP/1.1 message: {event!r}")


def parse_h1_request(wire: bytes, *, secure: bool) -> tuple[HttpRequest, tuple[Field, ...]]:
    """Reconstruct the target URI and message from HTTP/1.1 bytes (RFC 9112 Section 3.3)."""
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(wire)
    head = conn.next_event()
    if not isinstance(head, h11.Request):
        raise ValueError(f"expected a request head, got {head!r}")
    content, trailers = _h1_body(conn)
    raw = _plain(head.headers)
    target = urlsplit(head.target)
    if target.scheme and target.netloc:  # absolute-form: Host is ignored (RFC 9112 3.2.2)
        scheme, authority = target.scheme, target.netloc
        path = (target.path or b"/") + (b"?" + target.query if target.query else b"")
    else:  # origin-form: scheme from the connection, authority from Host
        scheme = b"https" if secure else b"http"
        authority = dict(raw).get(b"host", b"")
        path = head.target
    request = HttpRequest(
        method=head.method,
        scheme=scheme,
        authority=authority,
        path=path,
        headers=_end_to_end(raw),
        content=content,
        trailers=_end_to_end(trailers),
    )
    return request, raw


def parse_h1_response(
    wire: bytes, *, request_method: bytes
) -> tuple[HttpResponse, tuple[Field, ...], bytes]:
    """Parse an HTTP/1.1 response; framing depends on the request method (RFC 9110 Section 6)."""
    conn = h11.Connection(h11.CLIENT)
    conn.send(h11.Request(method=request_method, target=b"/", headers=[(b"host", b"x")]))
    conn.send(h11.EndOfMessage())
    conn.receive_data(wire)
    head = conn.next_event()
    if not isinstance(head, h11.Response):
        raise ValueError(f"expected a final response head, got {head!r}")
    content, trailers = _h1_body(conn)
    raw = _plain(head.headers)
    response = HttpResponse(
        status=head.status_code,
        headers=_end_to_end(raw),
        content=content,
        trailers=_end_to_end(trailers),
    )
    return response, raw, bytes(head.reason)


def exchange_h1(
    request: HttpRequest, response: HttpResponse, *, secure: bool = True, reason: bytes = b""
) -> Exchange:
    """Serialize both messages with h11 and parse them back on the opposite role."""
    client = h11.Connection(h11.CLIENT)
    head_fields = [
        (b"host", request.authority),  # Host first (RFC 9110 Section 7.2)
        *request.headers,
        *_h1_framing(request.content, request.trailers, declare_empty=False),
    ]
    head = h11.Request(method=request.method, target=request.path, headers=head_fields)
    out = [client.send(head)]
    if request.content:
        out.append(client.send(h11.Data(data=request.content)))
    out.append(client.send(h11.EndOfMessage(headers=list(request.trailers))))
    request_wire = b"".join(out)

    parsed_request, request_block = parse_h1_request(request_wire, secure=secure)

    server = h11.Connection(h11.SERVER)
    server.receive_data(request_wire)
    server.next_event()  # the request head, already parsed above
    _h1_body(server)  # drain to EndOfMessage so h11 lets the server respond
    response_fields = [
        *response.headers,
        *_h1_framing(response.content, response.trailers, declare_empty=response.status != 204),
    ]
    sent = [
        server.send(
            h11.Response(status_code=response.status, headers=response_fields, reason=reason)
        )
    ]
    if response.content:
        sent.append(server.send(h11.Data(data=response.content)))
    sent.append(server.send(h11.EndOfMessage(headers=list(response.trailers))))
    response_wire = b"".join(sent)

    parsed_response, response_block, _ = parse_h1_response(
        response_wire, request_method=request.method
    )
    return Exchange(
        "HTTP/1.1",
        parsed_request,
        parsed_response,
        request_wire,
        response_wire,
        request_block,
        response_block,
    )


# --- HTTP/2 (RFC 9113) ---------------------------------------------------------------------


def h2_connected_pair(
    *, split_outbound_cookies: bool = False
) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    """A client and server h2 state machine with the preface and SETTINGS already exchanged."""
    client = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=True, split_outbound_cookies=split_outbound_cookies)
    )
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    return client, server


def _h2_send(
    conn: h2.connection.H2Connection,
    block: list[Field],
    content: bytes,
    trailers: tuple[Field, ...],
) -> bytes:
    stream_id = 1
    conn.send_headers(stream_id, block, end_stream=not content and not trailers)
    step = conn.max_outbound_frame_size
    for start in range(0, len(content), step):
        last = start + step >= len(content)
        conn.send_data(stream_id, content[start : start + step], end_stream=last and not trailers)
    if trailers:  # trailers are a HEADERS block that carries END_STREAM (RFC 9113 Section 8.1)
        conn.send_headers(stream_id, list(trailers), end_stream=True)
    return conn.data_to_send()


def h2_receive_message(
    conn: h2.connection.H2Connection, wire: bytes
) -> tuple[tuple[Field, ...], bytes, tuple[Field, ...]]:
    """Feed one stream's bytes to an h2 peer; return header block, content, and trailers."""
    block: tuple[Field, ...] = ()
    parts: list[bytes] = []
    trailers: tuple[Field, ...] = ()
    ended = False
    for event in conn.receive_data(wire):
        if isinstance(event, (h2.events.RequestReceived, h2.events.ResponseReceived)):
            block = _plain(event.headers)
        elif isinstance(event, h2.events.DataReceived):
            parts.append(event.data)
        elif isinstance(event, h2.events.TrailersReceived):
            trailers = _plain(event.headers)
        elif isinstance(event, h2.events.StreamEnded):
            ended = True
    if not ended:
        raise ValueError("HTTP/2 stream did not reach END_STREAM")
    return block, b"".join(parts), trailers


def exchange_h2(
    request: HttpRequest, response: HttpResponse, *, split_outbound_cookies: bool = False
) -> Exchange:
    """Serialize both messages on one h2 stream and parse them on the peer state machine."""
    client, server = h2_connected_pair(split_outbound_cookies=split_outbound_cookies)
    control = [
        (b":method", request.method),
        (b":scheme", request.scheme),
        (b":authority", request.authority),
        (b":path", request.path),
    ]
    request_wire = _h2_send(client, [*control, *request.headers], request.content, request.trailers)
    request_block, content, trailers = h2_receive_message(server, request_wire)
    parsed_request = _request_from_block(request_block, content, trailers)

    status = [(b":status", str(response.status).encode("ascii"))]
    response_wire = _h2_send(
        server, [*status, *response.headers], response.content, response.trailers
    )
    response_block, content, trailers = h2_receive_message(client, response_wire)
    parsed_response = _response_from_block(response_block, content, trailers)
    return Exchange(
        "HTTP/2",
        parsed_request,
        parsed_response,
        request_wire,
        response_wire,
        request_block,
        response_block,
    )


# --- HTTP/3 (RFC 9114) ---------------------------------------------------------------------


def _h3_encode(
    encoder: pylsqpack.Encoder, block: list[Field], content: bytes, trailers: tuple[Field, ...]
) -> bytes:
    def field_section(fields: list[Field]) -> bytes:
        encoder_stream, section = encoder.encode(_H3_STREAM_ID, fields)
        if encoder_stream:  # no dynamic table was negotiated, so no encoder-stream bytes
            raise RuntimeError("unexpected QPACK encoder stream data")
        return encode_frame(FrameType.HEADERS, section)

    frames = [field_section(block)]
    if content:
        frames.append(encode_frame(FrameType.DATA, content))
    if trailers:
        frames.append(field_section(list(trailers)))
    return b"".join(frames)


def _pull_varint(wire: bytes, offset: int) -> tuple[int, int]:
    """Read a QUIC variable-length integer: the top 2 bits give a 1/2/4/8-byte length."""
    if offset >= len(wire):
        raise ValueError("truncated HTTP/3 frame header")
    length = 1 << (wire[offset] >> 6)
    if offset + length > len(wire):
        raise ValueError("truncated HTTP/3 frame header")
    value = wire[offset] & 0x3F
    for octet in wire[offset + 1 : offset + length]:
        value = (value << 8) | octet
    return value, offset + length


def h3_decode_stream(wire: bytes) -> tuple[tuple[Field, ...], bytes, tuple[Field, ...]]:
    """Split one HTTP/3 request stream into header section, content, and trailer section."""
    decoder = pylsqpack.Decoder(0, 0)
    sections: list[tuple[Field, ...]] = []
    parts: list[bytes] = []
    offset = 0
    while offset < len(wire):
        frame_type, offset = _pull_varint(wire, offset)
        length, offset = _pull_varint(wire, offset)
        if offset + length > len(wire):
            raise ValueError("truncated HTTP/3 frame payload")
        payload, offset = wire[offset : offset + length], offset + length
        if frame_type == FrameType.HEADERS:
            if len(sections) == 2:
                raise ValueError("HEADERS after the trailer section (RFC 9114 Section 4.1)")
            sections.append(_plain(decoder.feed_header(_H3_STREAM_ID, payload)[1]))
        elif frame_type == FrameType.DATA:
            if len(sections) != 1:
                raise ValueError("DATA outside the content position (RFC 9114 Section 4.1)")
            parts.append(payload)
        else:
            raise ValueError(f"unexpected frame type {frame_type:#x} on a request stream")
    if not sections:
        raise ValueError("request stream carried no HEADERS frame")
    return sections[0], b"".join(parts), sections[1] if len(sections) == 2 else ()


def parse_h3_request(wire: bytes) -> tuple[HttpRequest, tuple[Field, ...]]:
    """Decode a request stream, run aioquic's RFC 9114 validators, then map control data."""
    block, content, trailers = h3_decode_stream(wire)
    validate_request_headers(list(block))
    validate_trailers(list(trailers))
    return _request_from_block(block, content, trailers), block


def parse_h3_response(wire: bytes) -> tuple[HttpResponse, tuple[Field, ...]]:
    """Decode a response on the same stream and validate it as aioquic's client would."""
    block, content, trailers = h3_decode_stream(wire)
    validate_response_headers(list(block))
    validate_trailers(list(trailers))
    return _response_from_block(block, content, trailers), block


def exchange_h3(request: HttpRequest, response: HttpResponse) -> Exchange:
    """Serialize both messages as HTTP/3 frames on one QUIC stream and parse them back.

    No QUIC transport runs here: the stream bytes are built with aioquic's frame encoder and
    pylsqpack, then checked with aioquic's own RFC 9114 field validators.
    """
    control = [
        (b":method", request.method),
        (b":scheme", request.scheme),
        (b":authority", request.authority),
        (b":path", request.path),
    ]
    request_wire = _h3_encode(
        pylsqpack.Encoder(), [*control, *request.headers], request.content, request.trailers
    )
    parsed_request, request_block = parse_h3_request(request_wire)
    status = [(b":status", str(response.status).encode("ascii"))]
    response_wire = _h3_encode(
        pylsqpack.Encoder(), [*status, *response.headers], response.content, response.trailers
    )
    parsed_response, response_block = parse_h3_response(response_wire)
    return Exchange(
        "HTTP/3",
        parsed_request,
        parsed_response,
        request_wire,
        response_wire,
        request_block,
        response_block,
    )
