"""HTTP/3 frames over QUIC streams and QPACK field-section prefixes (RFC 9114, RFC 9204).

The codec half is a from-scratch reader for the wire layouts.
The harness half drives two real aioquic H3Connection objects over an in-memory
QUIC stand-in, so a test decides the order in which each stream's bytes arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from aioquic.h3.connection import H3Connection
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import StreamDataReceived, StreamReset
from aioquic.quic.packet import QuicErrorCode

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from aioquic.h3.events import H3Event

VARINT_MAX = (1 << 62) - 1
FRAME_DATA = 0x00
FRAME_HEADERS = 0x01
FRAME_SETTINGS = 0x04
FRAME_MAX_PUSH_ID = 0x0D
STREAM_CONTROL = 0x00
STREAM_PUSH = 0x01
STREAM_QPACK_ENCODER = 0x02
STREAM_QPACK_DECODER = 0x03
SETTING_QPACK_MAX_TABLE_CAPACITY = 0x01
SETTING_MAX_FIELD_SECTION_SIZE = 0x06
SETTING_QPACK_BLOCKED_STREAMS = 0x07
# HTTP/2 setting identifiers with no HTTP/3 meaning (RFC 9114 Section 7.2.4.1, 11.2.2).
RESERVED_HTTP2_SETTINGS = frozenset({0x00, 0x02, 0x03, 0x04, 0x05})
# A 62-bit value needs at most nine 7-bit continuation bytes (RFC 7541 Section 5.1).
MAX_PREFIXED_INT_CONTINUATION_BYTES = 9
FIELD_LINE_OVERHEAD = 32


def is_reserved_type(value: int) -> bool:
    """Return True for the grease code points 0x1f * N + 0x21 (RFC 9114 Section 7.2.8)."""
    return value >= 0x21 and (value - 0x21) % 0x1F == 0


def encode_varint(value: int) -> bytes:
    """Encode *value* as a minimal QUIC variable-length integer (RFC 9000 Section 16)."""
    if not 0 <= value <= VARINT_MAX:
        raise ValueError(f"varint out of range: {value}")
    if value < 1 << 6:
        return value.to_bytes(1, "big")
    if value < 1 << 14:
        return (value | 0x4000).to_bytes(2, "big")
    if value < 1 << 30:
        return (value | 0x8000_0000).to_bytes(4, "big")
    return (value | 0xC000_0000_0000_0000).to_bytes(8, "big")


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Return (value, next_offset) for the varint at *offset* (RFC 9000 Appendix A.1)."""
    if offset >= len(data):
        raise ValueError("truncated varint")
    length = 1 << (data[offset] >> 6)
    end = offset + length
    if end > len(data):
        raise ValueError("truncated varint")
    value = data[offset] & 0x3F
    for byte in data[offset + 1 : end]:
        value = (value << 8) | byte
    return value, end


@dataclass(frozen=True, slots=True)
class Frame:
    """One HTTP/3 frame: Type (i), Length (i), Frame Payload (RFC 9114 Section 7.1)."""

    type: int
    payload: bytes


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    """Serialize one HTTP/3 frame."""
    return encode_varint(frame_type) + encode_varint(len(payload)) + payload


def split_frames(data: bytes) -> list[Frame]:
    """Split a complete stream payload into frames; a truncated last frame is an error."""
    frames: list[Frame] = []
    offset = 0
    while offset < len(data):
        frame_type, offset = decode_varint(data, offset)
        length, offset = decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise ValueError("H3_FRAME_ERROR: frame truncated by end of stream")
        frames.append(Frame(frame_type, data[offset:end]))
        offset = end
    return frames


def split_uni_stream(data: bytes) -> tuple[int, bytes]:
    """Return (stream_type, remainder) for a unidirectional stream (RFC 9114 Section 6.2)."""
    stream_type, offset = decode_varint(data)
    return stream_type, data[offset:]


def parse_settings(payload: bytes) -> dict[int, int]:
    """Decode a SETTINGS payload, rejecting duplicates and HTTP/2-only identifiers."""
    settings: dict[int, int] = {}
    offset = 0
    while offset < len(payload):
        identifier, offset = decode_varint(payload, offset)
        value, offset = decode_varint(payload, offset)
        if identifier in RESERVED_HTTP2_SETTINGS:
            raise ValueError(f"H3_SETTINGS_ERROR: reserved setting 0x{identifier:x}")
        if identifier in settings:
            raise ValueError(f"H3_SETTINGS_ERROR: duplicate setting 0x{identifier:x}")
        settings[identifier] = value
    return settings


def decode_prefixed_int(data: bytes, offset: int, prefix_bits: int) -> tuple[int, int]:
    """Decode an N-bit prefix integer (RFC 9204 Section 4.1.1, RFC 7541 Section 5.1).

    The continuation-byte count is bounded so an endless run of 0x80 bytes fails fast.
    """
    if offset >= len(data):
        raise ValueError("truncated prefixed integer")
    mask = (1 << prefix_bits) - 1
    value = data[offset] & mask
    offset += 1
    if value < mask:
        return value, offset
    shift = 0
    for _ in range(MAX_PREFIXED_INT_CONTINUATION_BYTES):
        if offset >= len(data):
            raise ValueError("truncated prefixed integer")
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if value > VARINT_MAX:
                raise ValueError("prefixed integer exceeds 62 bits")
            return value, offset
    raise ValueError("prefixed integer has too many continuation bytes")


def decode_required_insert_count(encoded: int, max_table_capacity: int, total_inserts: int) -> int:
    """Reconstruct the Required Insert Count (RFC 9204 Section 4.5.1.1)."""
    max_entries = max_table_capacity // 32
    full_range = 2 * max_entries
    if encoded == 0:
        return 0
    if encoded > full_range:
        raise ValueError("QPACK_DECOMPRESSION_FAILED: encoded insert count out of range")
    max_value = total_inserts + max_entries
    max_wrapped = (max_value // full_range) * full_range
    required = max_wrapped + encoded - 1
    if required > max_value:
        if required <= full_range:
            raise ValueError("QPACK_DECOMPRESSION_FAILED: insert count did not wrap")
        required -= full_range
    if required == 0:
        raise ValueError("QPACK_DECOMPRESSION_FAILED: zero must be encoded as zero")
    return required


@dataclass(frozen=True, slots=True)
class FieldSectionPrefix:
    """Required Insert Count and Base of one encoded field section (RFC 9204 Section 4.5.1)."""

    required_insert_count: int
    base: int


def parse_field_section_prefix(
    block: bytes, max_table_capacity: int, total_inserts: int
) -> FieldSectionPrefix:
    """Decode the two-integer prefix that tells a decoder whether it must block."""
    encoded, offset = decode_prefixed_int(block, 0, 8)
    required = decode_required_insert_count(encoded, max_table_capacity, total_inserts)
    if offset >= len(block):
        raise ValueError("truncated field section prefix")
    sign = block[offset] & 0x80
    delta, _ = decode_prefixed_int(block, offset, 7)
    if sign:
        if required <= delta:
            raise ValueError("QPACK_DECOMPRESSION_FAILED: negative Base")
        return FieldSectionPrefix(required, required - delta - 1)
    return FieldSectionPrefix(required, required + delta)


def field_section_size(headers: Iterable[tuple[bytes, bytes]]) -> int:
    """Uncompressed size: name + value + 32 bytes per field line (RFC 9114 Section 4.2.2)."""
    return sum(len(name) + len(value) + FIELD_LINE_OVERHEAD for name, value in headers)


def enforce_field_section_limit(headers: Sequence[tuple[bytes, bytes]], limit: int) -> None:
    """Reject a decoded field section above *limit*; the caller answers `431`."""
    size = field_section_size(headers)
    if size > limit:
        raise ValueError(f"field section {size} bytes exceeds limit {limit}")


class LoopbackQuic(QuicConnection):
    """The QuicConnection surface H3Connection uses, with per-stream send buffers.

    QuicConnection.__init__ is deliberately not called, as in aioquic's own
    tests/test_h3.py FakeQuicConnection: no TLS, no packets, only stream bytes.
    """

    def __init__(self, *, is_client: bool) -> None:
        self._configuration = QuicConfiguration(is_client=is_client)
        self._quic_logger = None
        self._remote_max_datagram_frame_size = None
        self._next_bidi = 0 if is_client else 1
        self._next_uni = 2 if is_client else 3
        self.sent: dict[int, bytearray] = {}
        self.fin: set[int] = set()
        self.fin_delivered: set[int] = set()
        self.delivered: dict[int, int] = {}
        self.close_error: int | None = None
        self.resets: dict[int, int] = {}

    def close(
        self,
        error_code: int = QuicErrorCode.NO_ERROR,
        frame_type: int | None = None,
        reason_phrase: str = "",
    ) -> None:
        self.close_error = error_code

    def get_next_available_stream_id(self, is_unidirectional: bool = False) -> int:
        if is_unidirectional:
            stream_id, self._next_uni = self._next_uni, self._next_uni + 4
        else:
            stream_id, self._next_bidi = self._next_bidi, self._next_bidi + 4
        return stream_id

    def reset_stream(self, stream_id: int, error_code: int) -> None:
        self.resets[stream_id] = error_code

    def send_stream_data(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:
        self.sent.setdefault(stream_id, bytearray()).extend(data)
        if end_stream:
            self.fin.add(stream_id)

    def take(self, stream_id: int) -> StreamDataReceived | None:
        """Return the bytes of *stream_id* not yet handed to the peer, or None."""
        buffer = self.sent.get(stream_id, bytearray())
        start = self.delivered.get(stream_id, 0)
        fin = stream_id in self.fin and stream_id not in self.fin_delivered
        if start == len(buffer) and not fin:
            return None
        self.delivered[stream_id] = len(buffer)
        if fin:
            self.fin_delivered.add(stream_id)
        return StreamDataReceived(data=bytes(buffer[start:]), end_stream=fin, stream_id=stream_id)

    def pending(self, stream_id: int) -> bytes:
        """Bytes written to *stream_id* that the peer has not received yet."""
        return bytes(self.sent.get(stream_id, b"")[self.delivered.get(stream_id, 0) :])


Side = Literal["client", "server"]


@dataclass(slots=True)
class H3Pair:
    """A client and server H3Connection joined by LoopbackQuic stream buffers."""

    client_quic: LoopbackQuic = field(default_factory=lambda: LoopbackQuic(is_client=True))
    server_quic: LoopbackQuic = field(default_factory=lambda: LoopbackQuic(is_client=False))
    client: H3Connection = field(init=False)
    server: H3Connection = field(init=False)

    def __post_init__(self) -> None:
        self.client = H3Connection(self.client_quic)
        self.server = H3Connection(self.server_quic)

    def deliver(self, sender: Side, stream_ids: Iterable[int]) -> list[H3Event]:
        """Hand each listed stream's pending bytes to the peer, in the order given."""
        source = self.client_quic if sender == "client" else self.server_quic
        target = self.server if sender == "client" else self.client
        events: list[H3Event] = []
        for stream_id in stream_ids:
            chunk = source.take(stream_id)
            if chunk is not None:
                events.extend(target.handle_event(chunk))
        return events

    def deliver_uni(self, sender: Side) -> list[H3Event]:
        """Deliver every unidirectional stream (control, QPACK encoder and decoder)."""
        source = self.client_quic if sender == "client" else self.server_quic
        return self.deliver(sender, sorted(s for s in source.sent if s & 0x2))

    def handshake(self) -> None:
        """Exchange control and QPACK streams both ways, as after QUIC 1-RTT."""
        self.deliver_uni("client")
        self.deliver_uni("server")

    def reset(self, sender: Side, stream_id: int, error_code: int) -> list[H3Event]:
        """Deliver a QUIC RESET_STREAM for *stream_id* to the peer."""
        target = self.server if sender == "client" else self.client
        return target.handle_event(StreamReset(error_code=error_code, stream_id=stream_id))
