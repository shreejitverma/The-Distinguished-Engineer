"""HPACK (RFC 7541) wire inspection, h2 field-block capture, and compression-oracle probes."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import h2.exceptions
from h2.settings import SettingCodes
from hpack import Encoder
from hpack.hpack import decode_integer
from hyperframe.frame import Frame, GoAwayFrame, HeadersFrame

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hpack.struct import Header

STATIC_TABLE_LENGTH = 61  # RFC 7541 Appendix A
ENTRY_OVERHEAD = 32  # RFC 7541 Section 4.1
DEFAULT_TABLE_SIZE = 4096  # RFC 9113 Section 6.5.2, SETTINGS_HEADER_TABLE_SIZE initial value


class Kind(enum.Enum):
    """The five first-octet patterns of an HPACK field block (RFC 7541 Section 6)."""

    INDEXED = "indexed"  # 1xxxxxxx, Section 6.1
    INCREMENTAL = "incremental"  # 01xxxxxx, Section 6.2.1
    SIZE_UPDATE = "size-update"  # 001xxxxx, Section 6.3
    NEVER_INDEXED = "never-indexed"  # 0001xxxx, Section 6.2.3
    WITHOUT_INDEXING = "without-indexing"  # 0000xxxx, Section 6.2.2


_LITERALS = frozenset({Kind.INCREMENTAL, Kind.NEVER_INDEXED, Kind.WITHOUT_INDEXING})


@dataclass(frozen=True, slots=True)
class Representation:
    """One representation in a field block.

    `index` is the table index for `INDEXED`, the name index (0 means a literal name) for the
    three literal kinds, and the new maximum size for `SIZE_UPDATE`.
    """

    kind: Kind
    index: int
    octets: int


@dataclass(frozen=True, slots=True)
class ServerOutcome:
    """What an h2 server did with one HEADERS frame carrying a hand-built field block."""

    headers: tuple[Header, ...]
    goaway_error_code: int | None
    exception: str | None


def entry_size(name: bytes, value: bytes) -> int:
    """Dynamic-table cost of one entry: name octets + value octets + 32 (RFC 7541 Section 4.1)."""
    return ENTRY_OVERHEAD + len(name) + len(value)


def _classify(first: int) -> tuple[Kind, int]:
    if first & 0x80:
        return Kind.INDEXED, 7
    if first & 0x40:
        return Kind.INCREMENTAL, 6
    if first & 0x20:
        return Kind.SIZE_UPDATE, 5
    if first & 0x10:
        return Kind.NEVER_INDEXED, 4
    return Kind.WITHOUT_INDEXING, 4


def _skip_string(block: bytes, offset: int) -> int:
    length, consumed = decode_integer(block[offset:], 7)
    end = offset + consumed + length
    if end > len(block):
        raise ValueError("truncated string literal")
    return end


def parse_representations(block: bytes) -> list[Representation]:
    """Split a field block into representations without touching any dynamic table."""
    out: list[Representation] = []
    offset = 0
    while offset < len(block):
        kind, prefix_bits = _classify(block[offset])
        index, consumed = decode_integer(block[offset:], prefix_bits)
        end = offset + consumed
        if kind in _LITERALS:
            if index == 0:
                end = _skip_string(block, end)
            end = _skip_string(block, end)
        out.append(Representation(kind=kind, index=index, octets=end - offset))
        offset = end
    return out


def split_frames(wire: bytes) -> list[Frame]:
    """Parse a byte string of complete HTTP/2 frames (no connection preface)."""
    frames: list[Frame] = []
    offset = 0
    while offset < len(wire):
        frame, length = Frame.parse_frame_header(memoryview(wire[offset : offset + 9]))
        frame.parse_body(memoryview(wire[offset + 9 : offset + 9 + length]))
        frames.append(frame)
        offset += 9 + length
    return frames


def _client_server_pair(
    server_settings: dict[SettingCodes | int, int] | None = None,
    *,
    split_outbound_cookies: bool = False,
) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=True, split_outbound_cookies=split_outbound_cookies)
    )
    server = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=False, header_encoding=None)
    )
    client.initiate_connection()
    server.initiate_connection()
    if server_settings:
        server.update_settings(server_settings)
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    server.data_to_send()
    client.data_to_send()
    return client, server


def h2_request_blocks(
    requests: Sequence[Sequence[Header]],
    *,
    peer_header_table_size: int | None = None,
    split_outbound_cookies: bool = False,
) -> list[bytes]:
    """Send each header list on a new stream of one h2 client; return each HEADERS field block.

    `peer_header_table_size` is the server's `SETTINGS_HEADER_TABLE_SIZE`, which bounds the
    client's HPACK encoder (RFC 9113 Section 4.3.1).
    `split_outbound_cookies` turns on h2's `H2Configuration.split_outbound_cookies`.
    """
    settings: dict[SettingCodes | int, int] | None = None
    if peer_header_table_size is not None:
        settings = {SettingCodes.HEADER_TABLE_SIZE: peer_header_table_size}
    client, _ = _client_server_pair(settings, split_outbound_cookies=split_outbound_cookies)
    blocks: list[bytes] = []
    for headers in requests:
        stream_id = client.get_next_available_stream_id()
        client.send_headers(stream_id, list(headers), end_stream=True)
        frames = [f for f in split_frames(client.data_to_send()) if isinstance(f, HeadersFrame)]
        if len(frames) != 1:
            raise ValueError(f"expected one HEADERS frame, got {len(frames)}")
        blocks.append(bytes(frames[0].data))
    return blocks


def deliver_block_to_h2_server(block: bytes) -> ServerOutcome:
    """Feed one HEADERS frame (END_HEADERS, END_STREAM) with *block* to a fresh h2 server."""
    client, server = _client_server_pair()
    del client
    frame = HeadersFrame(1, data=block, flags=("END_HEADERS", "END_STREAM"))
    try:
        events = server.receive_data(frame.serialize())
    except h2.exceptions.ProtocolError as exc:
        goaways = [f for f in split_frames(server.data_to_send()) if isinstance(f, GoAwayFrame)]
        code = goaways[0].error_code if goaways else None
        return ServerOutcome(headers=(), goaway_error_code=code, exception=type(exc).__name__)
    for event in events:
        if isinstance(event, h2.events.RequestReceived):
            return ServerOutcome(
                headers=tuple(event.headers), goaway_error_code=None, exception=None
            )
    raise ValueError(f"no RequestReceived event in {events!r}")


def hpack_bomb_block(references: int, *, table_size: int = DEFAULT_TABLE_SIZE) -> bytes:
    """One literal that fills the whole dynamic table, then *references* 1-octet copies of it.

    This is the CVE-2016-6581 shape: each `0xbe` octet (index 62, the newest dynamic entry)
    decodes to `table_size` octets of header list.
    """
    name = b"x-bomb"
    value_length = table_size - ENTRY_OVERHEAD - len(name)
    if value_length <= 0:
        raise ValueError("table_size too small for the bomb entry")
    literal = Encoder().encode([(name, b"a" * value_length)], huffman=False)
    return literal + bytes([0x80 | (STATIC_TABLE_LENGTH + 1)]) * references


def typical_api_request(request_number: int, *, bearer_token: bytes) -> list[Header]:
    """A deterministic JSON API request whose `x-request-id` is unique per request."""
    return [
        (b":method", b"GET"),
        (b":scheme", b"https"),
        (b":authority", b"api.example.com"),
        (b":path", b"/v1/orders?limit=50"),
        (b"accept", b"application/json"),
        (b"user-agent", b"orders-client/3.4.1 python-h2/4.4.1"),
        (b"authorization", b"Bearer " + bearer_token),
        (b"x-request-id", str(uuid.UUID(int=request_number)).encode("ascii")),
    ]


def oracle_signal(victim: Sequence[Header], guess: Header) -> int:
    """Octets saved on *guess* because *victim* went first through the same encoder.

    A positive value means the shared dynamic table confirmed the guess (RFC 7541 Section 7.1).
    """
    shared = Encoder()
    shared.encode(list(victim))
    probe = len(shared.encode([guess]))
    control = len(Encoder().encode([guess]))
    return control - probe
