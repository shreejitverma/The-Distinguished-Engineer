"""HTTP/2 frame header codec and h2 stream state machine drivers (RFC 9113 Sections 4 and 5)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import h2.exceptions
from h2.errors import ErrorCodes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from h2.stream import StreamState

FRAME_HEADER_LEN = 9
MAX_FRAME_LENGTH = (1 << 24) - 1
MAX_STREAM_ID = (1 << 31) - 1
RESERVED_BIT = 1 << 31
CLIENT_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

FLAG_END_STREAM = 0x01
FLAG_ACK = 0x01
FLAG_END_HEADERS = 0x04
FLAG_PADDED = 0x08
FLAG_PRIORITY = 0x20


class FrameType(IntEnum):
    """Frame type codes registered by RFC 9113 Section 6."""

    DATA = 0x00
    HEADERS = 0x01
    PRIORITY = 0x02
    RST_STREAM = 0x03
    SETTINGS = 0x04
    PUSH_PROMISE = 0x05
    PING = 0x06
    GOAWAY = 0x07
    WINDOW_UPDATE = 0x08
    CONTINUATION = 0x09


@dataclass(frozen=True, slots=True)
class FrameHeader:
    """The fixed 9-octet frame header (RFC 9113 Section 4.1)."""

    length: int
    type: int
    flags: int
    stream_id: int


@dataclass(frozen=True, slots=True)
class RawFrame:
    """One decoded frame: header plus the payload octets it announced."""

    header: FrameHeader
    payload: bytes


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one call to ``H2Connection.receive_data`` did."""

    error: str | None
    error_code: ErrorCodes | None
    events: tuple[str, ...]
    emitted: tuple[RawFrame, ...]

    def emitted_types(self) -> tuple[int, ...]:
        """Frame type codes the endpoint queued in response."""
        return tuple(frame.header.type for frame in self.emitted)

    def goaway(self) -> tuple[int, int]:
        """``(last_stream_id, error_code)`` of the single GOAWAY the endpoint queued."""
        payloads = [f.payload for f in self.emitted if f.header.type == FrameType.GOAWAY]
        if len(payloads) != 1:
            raise ValueError(f"expected exactly one GOAWAY, got {len(payloads)}")
        last = int.from_bytes(payloads[0][0:4], "big") & MAX_STREAM_ID
        return last, int.from_bytes(payloads[0][4:8], "big")

    def rst_stream(self) -> tuple[int, int]:
        """``(stream_id, error_code)`` of the single RST_STREAM the endpoint queued."""
        frames = [f for f in self.emitted if f.header.type == FrameType.RST_STREAM]
        if len(frames) != 1:
            raise ValueError(f"expected exactly one RST_STREAM, got {len(frames)}")
        return frames[0].header.stream_id, int.from_bytes(frames[0].payload, "big")


def encode_frame_header(
    length: int, frame_type: int, flags: int, stream_id: int, *, reserved: bool = False
) -> bytes:
    """Serialize Length(24) Type(8) Flags(8) R(1) Stream Identifier(31), big-endian."""
    if not 0 <= length <= MAX_FRAME_LENGTH:
        raise ValueError("length must fit in 24 bits")
    if not 0 <= frame_type <= 0xFF or not 0 <= flags <= 0xFF:
        raise ValueError("type and flags must fit in 8 bits")
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError("stream identifier must fit in 31 bits")
    word = stream_id | (RESERVED_BIT if reserved else 0)
    return length.to_bytes(3, "big") + bytes((frame_type, flags)) + word.to_bytes(4, "big")


def decode_frame_header(data: bytes) -> FrameHeader:
    """Parse a 9-octet header; the reserved bit is ignored on receipt (RFC 9113 Section 4.1)."""
    if len(data) != FRAME_HEADER_LEN:
        raise ValueError("frame header is exactly 9 octets")
    return FrameHeader(
        length=int.from_bytes(data[0:3], "big"),
        type=data[3],
        flags=data[4],
        stream_id=int.from_bytes(data[5:9], "big") & MAX_STREAM_ID,
    )


def build_frame(frame_type: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    """Prefix *payload* with a frame header whose Length field equals ``len(payload)``."""
    return encode_frame_header(len(payload), frame_type, flags, stream_id) + payload


def split_frames(wire: bytes) -> tuple[RawFrame, ...]:
    """Cut a byte stream that holds only whole frames into frames."""
    frames: list[RawFrame] = []
    offset = 0
    while offset < len(wire):
        if offset + FRAME_HEADER_LEN > len(wire):
            raise ValueError("truncated frame header")
        header = decode_frame_header(wire[offset : offset + FRAME_HEADER_LEN])
        end = offset + FRAME_HEADER_LEN + header.length
        if end > len(wire):
            raise ValueError("truncated frame payload")
        frames.append(RawFrame(header, wire[offset + FRAME_HEADER_LEN : end]))
        offset = end
    return tuple(frames)


def decode_settings(payload: bytes) -> dict[int, int]:
    """Decode SETTINGS parameters: 16-bit identifier, 32-bit value (RFC 9113 Section 6.5.1)."""
    if len(payload) % 6:
        raise ValueError("SETTINGS payload must be a multiple of 6 octets")
    return {
        int.from_bytes(payload[i : i + 2], "big"): int.from_bytes(payload[i + 2 : i + 6], "big")
        for i in range(0, len(payload), 6)
    }


def connected_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    """Return ``(client, server)`` after both prefaces and both SETTINGS ACKs were exchanged."""
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    return client, server


def stream_state(conn: h2.connection.H2Connection, stream_id: int) -> StreamState | None:
    """The endpoint's own view of a stream, or ``None`` if it holds no stream object."""
    stream = conn.streams.get(stream_id)
    return None if stream is None else stream.state_machine.state


def feed(conn: h2.connection.H2Connection, wire: bytes) -> Outcome:
    """Deliver *wire* to *conn* and record the events, the error, and the frames it queued."""
    try:
        events = conn.receive_data(wire)
    except h2.exceptions.ProtocolError as exc:
        return Outcome(
            error=type(exc).__name__,
            error_code=ErrorCodes(exc.error_code),
            events=(),
            emitted=split_frames(conn.data_to_send()),
        )
    return Outcome(
        error=None,
        error_code=None,
        events=tuple(type(event).__name__ for event in events),
        emitted=split_frames(conn.data_to_send()),
    )


def trace_exchange(
    request: Sequence[tuple[str, str]], request_body: bytes, response_body: bytes
) -> list[tuple[str, StreamState | None, StreamState | None]]:
    """Walk one request/response on stream 1 and snapshot both endpoints after each step."""
    client, server = connected_pair()
    steps: list[tuple[str, StreamState | None, StreamState | None]] = []

    def snap(label: str) -> None:
        steps.append((label, stream_state(client, 1), stream_state(server, 1)))

    snap("start")
    client.send_headers(1, request, end_stream=not request_body)
    snap("client sends HEADERS")
    server.receive_data(client.data_to_send())
    snap("server receives HEADERS")
    if request_body:
        client.send_data(1, request_body, end_stream=True)
        snap("client sends DATA+END_STREAM")
        server.receive_data(client.data_to_send())
        snap("server receives DATA+END_STREAM")
    server.send_headers(1, [(":status", "200")])
    server.send_data(1, response_body, end_stream=True)
    snap("server sends HEADERS, DATA+END_STREAM")
    client.receive_data(server.data_to_send())
    snap("client receives response")
    return steps


class RemoteResetBudget:
    """Close the connection with ENHANCE_YOUR_CALM once peer resets exceed a budget.

    A sketch of the per-connection reset accounting that CVE-2023-44487 (Rapid Reset)
    mitigations use; the stream concurrency limit alone does not bound this work.
    """

    def __init__(self, conn: h2.connection.H2Connection, max_remote_resets: int) -> None:
        self.conn = conn
        self.max_remote_resets = max_remote_resets
        self.remote_resets = 0
        self.tripped = False

    def receive(self, wire: bytes) -> Outcome:
        """Feed *wire*, count peer RST_STREAMs, and emit GOAWAY when the budget is exceeded."""
        if self.tripped:
            raise RuntimeError("connection already closed by the reset budget")
        events = self.conn.receive_data(wire)
        self.remote_resets += sum(
            1 for e in events if isinstance(e, h2.events.StreamReset) and e.remote_reset
        )
        if self.remote_resets > self.max_remote_resets:
            self.tripped = True
            self.conn.close_connection(ErrorCodes.ENHANCE_YOUR_CALM)
        return Outcome(
            error=None,
            error_code=None,
            events=tuple(type(event).__name__ for event in events),
            emitted=split_frames(self.conn.data_to_send()),
        )
