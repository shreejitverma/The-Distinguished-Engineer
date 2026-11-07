"""HTTP/1.1 chunked transfer coding and h11 connection lifecycle helpers (RFC 9112)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import h11

if TYPE_CHECKING:
    from collections.abc import Sequence

_HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")
_MAX_CHUNK_HEX_DIGITS = 20
FORBIDDEN_TRAILER_FIELDS: frozenset[bytes] = frozenset(
    {b"host", b"content-length", b"transfer-encoding", b"trailer", b"authorization", b"te"}
)


@dataclass(frozen=True, slots=True)
class DecodedChunked:
    """Payload and validated trailer fields extracted from a chunked-body."""

    body: bytes
    trailers: tuple[tuple[bytes, bytes], ...]


@dataclass(frozen=True, slots=True)
class H11ExchangeSummary:
    """Result of one HTTP/1.1 request/response cycle on an h11 server connection."""

    method: bytes
    target: bytes
    body: bytes
    trailers: tuple[tuple[bytes, bytes], ...]
    wire_response: bytes
    reusable: bool


def encode_chunked(
    body: bytes,
    *,
    chunk_size: int = 4096,
    trailers: Sequence[tuple[bytes, bytes]] = (),
) -> bytes:
    """Encode *body* and *trailers* using HTTP/1.1 chunked coding (RFC 9112 Section 7.1)."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    parts: list[bytes] = []
    for start in range(0, len(body), chunk_size):
        chunk = body[start : start + chunk_size]
        size_line = f"{len(chunk):x}\r\n".encode("ascii")
        parts.append(size_line + chunk + b"\r\n")
    parts.append(b"0\r\n")
    for raw_name, raw_value in trailers:
        name = raw_name.strip().lower()
        if not name or name in FORBIDDEN_TRAILER_FIELDS:
            raise ValueError(f"forbidden trailer field: {raw_name!r}")
        parts.append(raw_name + b": " + raw_value + b"\r\n")
    parts.append(b"\r\n")
    return b"".join(parts)


def decode_chunked_with_trailers(wire: bytes) -> DecodedChunked:
    """Decode an HTTP/1.1 chunked-body into its payload and validated trailer fields."""
    out = bytearray()
    offset = 0
    while True:
        line_end = wire.find(b"\r\n", offset)
        if line_end == -1:
            raise ValueError("unterminated chunk size line")
        size_line = wire[offset:line_end]
        semi = size_line.find(b";")
        if semi != -1:
            if b"\r" in size_line[semi:]:
                raise ValueError("bare CR in chunk extension")
            hex_digits = size_line[:semi]
        else:
            hex_digits = size_line
        if not (1 <= len(hex_digits) <= _MAX_CHUNK_HEX_DIGITS) or not all(
            b in _HEX_BYTES for b in hex_digits
        ):
            raise ValueError("invalid chunk size hex digits")
        size = int(hex_digits, 16)
        offset = line_end + 2
        if size == 0:
            trailers: list[tuple[bytes, bytes]] = []
            while True:
                trailer_end = wire.find(b"\r\n", offset)
                if trailer_end == -1:
                    raise ValueError("missing or trailing bytes after terminating chunk")
                if trailer_end == offset:
                    if offset + 2 != len(wire):
                        raise ValueError("missing or trailing bytes after terminating chunk")
                    return DecodedChunked(body=bytes(out), trailers=tuple(trailers))
                field_line = wire[offset:trailer_end]
                colon = field_line.find(b":")
                if colon <= 0 or field_line[colon - 1 : colon] in (b" ", b"\t"):
                    raise ValueError("malformed trailer field line")
                name = field_line[:colon].lower()
                if name in FORBIDDEN_TRAILER_FIELDS:
                    raise ValueError(f"forbidden trailer field: {name!r}")
                value = field_line[colon + 1 :].strip(b" \t")
                trailers.append((name, value))
                offset = trailer_end + 2
        data_end = offset + size
        if data_end + 2 > len(wire) or wire[data_end : data_end + 2] != b"\r\n":
            raise ValueError("truncated chunk data or missing chunk CRLF")
        out.extend(wire[offset:data_end])
        offset = data_end + 2


def decode_chunked(wire: bytes) -> bytes:
    """Decode an HTTP/1.1 chunked-body byte sequence into the reconstructed payload."""
    return decode_chunked_with_trailers(wire).body


def step_h11_server_exchange(
    conn: h11.Connection,
    raw_request: bytes,
    *,
    response_status: int = 200,
    response_headers: Sequence[tuple[bytes, bytes]] = (),
    response_body: bytes = b"",
) -> H11ExchangeSummary:
    """Run one request/response cycle on *conn* and recycle if both roles reach DONE."""
    conn.receive_data(raw_request)
    first_event = conn.next_event()
    if not isinstance(first_event, h11.Request):
        raise ValueError(f"expected h11.Request, got {first_event!r}")
    body_parts: list[bytes] = []
    while True:
        event = conn.next_event()
        if isinstance(event, h11.Data):
            body_parts.append(bytes(event.data))
        elif isinstance(event, h11.EndOfMessage):
            trailers = tuple((k, v) for k, v in event.headers)
            break
        else:
            raise ValueError(f"incomplete request body: {event!r}")
    out_chunks: list[bytes] = [
        conn.send(h11.Response(status_code=response_status, headers=list(response_headers)))
    ]
    if response_body:
        out_chunks.append(conn.send(h11.Data(data=response_body)))
    out_chunks.append(conn.send(h11.EndOfMessage()))
    reusable = conn.our_state is h11.DONE and conn.their_state is h11.DONE
    if reusable:
        conn.start_next_cycle()
    return H11ExchangeSummary(
        method=first_event.method,
        target=first_event.target,
        body=b"".join(body_parts),
        trailers=trailers,
        wire_response=b"".join(out_chunks),
        reusable=reusable,
    )
