"""HTTP/2 stream and connection flow control on h2 4.4.1 state machines (RFC 9113 Section 6.9).

Everything here is in-memory: two `h2.connection.H2Connection` objects exchange bytes through
`H2Pair.pump`, so tests can stall a receiver, inspect every WINDOW_UPDATE on the wire, and prove
which window (stream or connection) blocked a sender.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import h2.config
import h2.connection
from h2.events import Event
from hyperframe.frame import Frame, WindowUpdateFrame

DEFAULT_INITIAL_WINDOW = 65_535  # RFC 9113 Section 6.9.2
MAX_WINDOW = 2**31 - 1  # RFC 9113 Section 6.9.1
FRAME_HEADER_LEN = 9  # RFC 9113 Section 4.1
WINDOW_UPDATE_TYPE = 0x08  # RFC 9113 Section 6.9
_RESERVED_MASK = 0x7FFF_FFFF
# Length (24) as 8 + 16 bits, Type (8), Flags (8), Reserved (1) + Stream Identifier (31).
_FRAME_HEADER = struct.Struct(">BHBBL")
_UINT32 = struct.Struct(">L")

REQUEST_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b":method", b"GET"),
    (b":path", b"/large"),
    (b":scheme", b"https"),
    (b":authority", b"api.example"),
)


class WindowUpdateError(ValueError):
    """A WINDOW_UPDATE that RFC 9113 Section 6.9 says the receiver must reject."""

    def __init__(self, message: str, *, error_code: str, connection_error: bool) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.connection_error = connection_error


@dataclass(frozen=True, slots=True)
class WindowUpdate:
    """Decoded WINDOW_UPDATE: stream 0 targets the connection window."""

    stream_id: int
    increment: int


def encode_window_update(stream_id: int, increment: int) -> bytes:
    """Serialize a 13-octet WINDOW_UPDATE frame: 9-octet header plus a 4-octet payload."""
    if not 0 <= stream_id <= MAX_WINDOW:
        raise ValueError("stream_id must fit in 31 bits")
    if not 1 <= increment <= MAX_WINDOW:
        raise ValueError("increment must be between 1 and 2^31-1")
    header = _FRAME_HEADER.pack(0, 4, WINDOW_UPDATE_TYPE, 0, stream_id)
    return header + _UINT32.pack(increment)


def decode_window_update(frame: bytes) -> WindowUpdate:
    """Parse one WINDOW_UPDATE frame, applying the RFC 9113 Section 6.9 receiver rules."""
    if len(frame) < FRAME_HEADER_LEN:
        raise ValueError("truncated frame header")
    len_hi, len_lo, frame_type, _flags, raw_stream = _FRAME_HEADER.unpack_from(frame)
    length = (len_hi << 16) | len_lo
    if frame_type != WINDOW_UPDATE_TYPE:
        raise ValueError(f"not a WINDOW_UPDATE frame: type {frame_type:#04x}")
    if length != 4 or len(frame) != FRAME_HEADER_LEN + 4:
        raise WindowUpdateError(
            f"WINDOW_UPDATE length {length} != 4",
            error_code="FRAME_SIZE_ERROR",
            connection_error=True,
        )
    # RFC 9113 Section 4.1: reserved bits MUST be ignored when receiving.
    stream_id = raw_stream & _RESERVED_MASK
    (raw_increment,) = _UINT32.unpack_from(frame, FRAME_HEADER_LEN)
    increment = raw_increment & _RESERVED_MASK
    if increment == 0:
        raise WindowUpdateError(
            "WINDOW_UPDATE increment of 0",
            error_code="PROTOCOL_ERROR",
            connection_error=stream_id == 0,
        )
    return WindowUpdate(stream_id=stream_id, increment=increment)


def parse_frames(data: bytes) -> list[Frame]:
    """Split a byte string emitted by `data_to_send` into hyperframe frames."""
    frames: list[Frame] = []
    view = memoryview(data)
    while view:
        frame, length = Frame.parse_frame_header(view[:FRAME_HEADER_LEN])
        frame.parse_body(view[FRAME_HEADER_LEN : FRAME_HEADER_LEN + length])
        frames.append(frame)
        view = view[FRAME_HEADER_LEN + length :]
    return frames


def window_updates(frames: list[Frame]) -> list[tuple[int, int]]:
    """Return `(stream_id, increment)` for every WINDOW_UPDATE in *frames*, in wire order."""
    return [(f.stream_id, f.window_increment) for f in frames if isinstance(f, WindowUpdateFrame)]


@dataclass(slots=True)
class PumpResult:
    """Events raised and frames written by each side during one `H2Pair.pump`."""

    client_events: list[Event] = field(default_factory=list[Event])
    server_events: list[Event] = field(default_factory=list[Event])
    client_frames: list[Frame] = field(default_factory=list[Frame])
    server_frames: list[Frame] = field(default_factory=list[Frame])


@dataclass(slots=True)
class H2Pair:
    """A client and a server `H2Connection` wired back to back with no socket."""

    client: h2.connection.H2Connection
    server: h2.connection.H2Connection

    def pump(self) -> PumpResult:
        """Move bytes in both directions until neither side has anything left to send."""
        result = PumpResult()
        while True:
            to_server = self.client.data_to_send()
            to_client = self.server.data_to_send()
            if not to_server and not to_client:
                return result
            if to_server:
                result.client_frames.extend(parse_frames(to_server))
                result.server_events.extend(self.server.receive_data(to_server))
            if to_client:
                result.server_frames.extend(parse_frames(to_client))
                result.client_events.extend(self.client.receive_data(to_client))


def new_pair() -> H2Pair:
    """Create a client/server pair and exchange the connection preface and SETTINGS."""
    client = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=True, header_encoding=None)
    )
    server = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=False, header_encoding=None)
    )
    client.initiate_connection()
    server.initiate_connection()
    pair = H2Pair(client=client, server=server)
    pair.pump()
    return pair


def open_response_streams(pair: H2Pair, count: int) -> list[int]:
    """Open *count* GET streams and send `:status 200` headers without ending the responses."""
    stream_ids: list[int] = []
    for _ in range(count):
        stream_id = pair.client.get_next_available_stream_id()
        pair.client.send_headers(stream_id, list(REQUEST_HEADERS), end_stream=True)
        stream_ids.append(stream_id)
    pair.pump()
    for stream_id in stream_ids:
        pair.server.send_headers(stream_id, [(b":status", b"200")])
    pair.pump()
    return stream_ids


def send_permitted(conn: h2.connection.H2Connection, stream_id: int, data: bytes) -> int:
    """Send as much of *data* as both flow-control windows allow; return the bytes sent.

    `H2Connection.send_data` raises `FlowControlError` instead of buffering, so a correct
    sender asks `local_flow_control_window` (the minimum of the stream and connection
    windows) before every frame and keeps the remainder queued until a WINDOW_UPDATE arrives.
    """
    sent = 0
    while sent < len(data):
        window = conn.local_flow_control_window(stream_id)
        if window <= 0:
            break
        chunk = min(window, conn.max_outbound_frame_size, len(data) - sent)
        conn.send_data(stream_id, data[sent : sent + chunk])
        sent += chunk
    return sent


def window_limited_throughput(window_bytes: int, rtt_seconds: float) -> float:
    """Upper bound in bytes per second when a sender may have at most one window in flight."""
    if window_bytes < 0 or rtt_seconds <= 0:
        raise ValueError("window must be >= 0 and rtt must be > 0")
    return window_bytes / rtt_seconds
