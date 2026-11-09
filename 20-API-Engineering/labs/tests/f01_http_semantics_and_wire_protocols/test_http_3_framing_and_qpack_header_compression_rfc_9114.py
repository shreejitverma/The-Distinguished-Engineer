"""HTTP/3 frame mapping over QUIC streams and QPACK out-of-order decoding (RFC 9114, RFC 9204)."""

from __future__ import annotations

import pylsqpack
import pytest
from aioquic.buffer import encode_uint_var
from aioquic.h3.connection import ErrorCode
from aioquic.h3.connection import encode_frame as aioquic_encode_frame
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.events import StreamDataReceived

from api_labs.f01_http_semantics_and_wire_protocols.h3_qpack import (
    FRAME_HEADERS,
    FRAME_MAX_PUSH_ID,
    FRAME_SETTINGS,
    SETTING_MAX_FIELD_SECTION_SIZE,
    STREAM_CONTROL,
    STREAM_QPACK_DECODER,
    STREAM_QPACK_ENCODER,
    FieldSectionPrefix,
    Frame,
    H3Pair,
    decode_prefixed_int,
    decode_required_insert_count,
    decode_varint,
    encode_frame,
    encode_varint,
    enforce_field_section_limit,
    field_section_size,
    is_reserved_type,
    parse_field_section_prefix,
    parse_settings,
    split_frames,
    split_uni_stream,
)

REQUEST = [
    (b":method", b"GET"),
    (b":scheme", b"https"),
    (b":authority", b"api.example"),
    (b":path", b"/v1/items"),
    (b"x-tenant", b"acme-prod"),
]
HEALTH = [
    (b":method", b"GET"),
    (b":scheme", b"https"),
    (b":authority", b"status.example"),
    (b":path", b"/healthz"),
]
CLIENT_CONTROL, CLIENT_ENCODER, CLIENT_DECODER = 2, 6, 10
SERVER_CONTROL, SERVER_ENCODER, SERVER_DECODER = 3, 7, 11


def warmed_pair() -> H3Pair:
    """Handshake, then one request so lsqpack has seen REQUEST once."""
    pair = H3Pair()
    pair.handshake()
    pair.client.send_headers(0, REQUEST, end_stream=True)
    pair.deliver("client", [CLIENT_ENCODER, 0])
    return pair


def headers_block(stream_bytes: bytes) -> bytes:
    first = split_frames(stream_bytes)[0]
    assert first.type == FRAME_HEADERS
    return first.payload


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wire", "value"),
    [
        ("c2197c5eff14e88c", 151_288_809_941_952_652),
        ("9d7f3e7d", 494_878_333),
        ("7bbd", 15_293),
        ("25", 37),
        ("4025", 37),
    ],
)
def test_varint_decodes_rfc_9000_samples(wire: str, value: int) -> None:
    assert decode_varint(bytes.fromhex(wire)) == (value, len(wire) // 2)


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 63, 64, 16_383, 16_384, 2**30 - 1, 2**30, 2**62 - 1])
def test_varint_encoding_is_minimal_and_matches_aioquic(value: int) -> None:
    wire = encode_varint(value)
    assert wire == encode_uint_var(value)
    assert decode_varint(wire) == (value, len(wire))
    with pytest.raises(ValueError, match="out of range"):
        encode_varint(2**62)


@pytest.mark.unit
def test_frame_layout_matches_aioquic_and_truncation_is_frame_error() -> None:
    payload = b"x" * 300
    wire = encode_frame(FRAME_HEADERS, payload)
    assert wire[:3] == b"\x01\x41\x2c"  # type 0x01, length 300 as a 2-byte varint
    assert wire == aioquic_encode_frame(FRAME_HEADERS, payload)
    assert split_frames(wire + encode_frame(0x21, b"")) == [
        Frame(FRAME_HEADERS, payload),
        Frame(0x21, b""),
    ]
    with pytest.raises(ValueError, match="H3_FRAME_ERROR"):
        split_frames(wire[:-1])


@pytest.mark.unit
def test_reserved_grease_code_points() -> None:
    assert [n for n in range(0xA0) if is_reserved_type(n)] == [0x21, 0x40, 0x5F, 0x7E, 0x9D]
    assert not is_reserved_type(0x02)  # HTTP/2 PRIORITY is reserved differently: an error


@pytest.mark.unit
def test_each_endpoint_opens_control_and_qpack_unidirectional_streams() -> None:
    pair = H3Pair()
    client = {sid: bytes(data) for sid, data in pair.client_quic.sent.items()}
    server = {sid: bytes(data) for sid, data in pair.server_quic.sent.items()}
    assert sorted(client) == [CLIENT_CONTROL, CLIENT_ENCODER, CLIENT_DECODER]
    assert sorted(server) == [SERVER_CONTROL, SERVER_ENCODER, SERVER_DECODER]
    assert all(sid & 0x3 == 0x2 for sid in client)  # client-initiated unidirectional
    assert all(sid & 0x3 == 0x3 for sid in server)  # server-initiated unidirectional

    stream_type, rest = split_uni_stream(client[CLIENT_CONTROL])
    assert stream_type == STREAM_CONTROL
    frames = split_frames(rest)
    assert [f.type for f in frames] == [FRAME_SETTINGS, FRAME_MAX_PUSH_ID]
    assert parse_settings(frames[0].payload) == {0x01: 4096, 0x07: 16, 0x08: 1, 0x21: 1}
    assert SETTING_MAX_FIELD_SECTION_SIZE not in parse_settings(frames[0].payload)
    assert split_uni_stream(client[CLIENT_ENCODER]) == (STREAM_QPACK_ENCODER, b"")
    assert split_uni_stream(client[CLIENT_DECODER]) == (STREAM_QPACK_DECODER, b"")


@pytest.mark.unit
def test_settings_reject_duplicates_and_http2_only_identifiers() -> None:
    assert parse_settings(encode_varint(0x06) + encode_varint(16_384)) == {0x06: 16_384}
    with pytest.raises(ValueError, match="duplicate"):
        parse_settings(encode_varint(0x06) + encode_varint(1) + encode_varint(0x06) + b"\x02")
    with pytest.raises(ValueError, match="reserved setting 0x2"):
        parse_settings(encode_varint(0x02) + encode_varint(1))


@pytest.mark.unit
def test_static_only_field_section_decodes_before_any_qpack_stream_arrives() -> None:
    pair = H3Pair()  # no SETTINGS exchanged: the client encoder's table capacity is 0
    pair.client.send_headers(0, REQUEST, end_stream=True)
    block = headers_block(pair.client_quic.pending(0))
    assert block[:2] == b"\x00\x00"
    assert parse_field_section_prefix(block, 4096, 0) == FieldSectionPrefix(0, 0)

    events = pair.deliver("client", [0])  # control, encoder, decoder streams still in flight
    assert events == [HeadersReceived(headers=REQUEST, stream_id=0, stream_ended=True)]


@pytest.mark.unit
def test_dynamic_reference_blocks_only_its_own_stream_until_encoder_stream_arrives() -> None:
    pair = warmed_pair()
    pair.client.send_headers(4, REQUEST)
    pair.client.send_data(4, b"{}", end_stream=True)
    pair.client.send_headers(8, HEALTH, end_stream=True)

    assert len(headers_block(bytes(pair.client_quic.sent[0]))) == 38  # first sight: literals
    assert headers_block(pair.client_quic.pending(4)) == bytes.fromhex("0482d1d7101112")
    blocked = parse_field_section_prefix(headers_block(pair.client_quic.pending(4)), 4096, 0)
    assert blocked == FieldSectionPrefix(required_insert_count=3, base=0)
    assert headers_block(pair.client_quic.pending(8))[:2] == b"\x00\x00"  # static and literal
    free = parse_field_section_prefix(headers_block(pair.client_quic.pending(8)), 4096, 0)
    assert free.required_insert_count == 0
    assert pair.client_quic.pending(CLIENT_ENCODER)[:1] == b"\xc0"  # insert, static name 0
    assert len(pair.client_quic.pending(CLIENT_ENCODER)) == 34  # three inserts, still in flight

    # Request streams 4 and 8 overtake the encoder stream, as QUIC permits.
    early = pair.deliver("client", [4, 8])
    assert early == [HeadersReceived(headers=HEALTH, stream_id=8, stream_ended=True)]

    late = pair.deliver("client", [CLIENT_ENCODER])
    assert late == [
        HeadersReceived(headers=REQUEST, stream_id=4, stream_ended=False),
        DataReceived(data=b"{}", stream_id=4, stream_ended=True),
    ]
    assert pair.server_quic.pending(SERVER_DECODER) == b"\x84"  # Section Acknowledgment(4)
    assert pair.server_quic.close_error is None


@pytest.mark.unit
def test_reset_of_blocked_stream_emits_stream_cancellation() -> None:
    pair = warmed_pair()
    pair.client.send_headers(4, REQUEST, end_stream=True)
    assert pair.deliver("client", [4]) == []

    assert pair.reset("client", 4, ErrorCode.H3_REQUEST_CANCELLED) == []
    assert pair.server_quic.pending(SERVER_DECODER) == b"\x44"  # Stream Cancellation(4)
    assert pair.server_quic.resets == {4: ErrorCode.H3_REQUEST_INCOMPLETE}
    assert pair.deliver("client", [CLIENT_ENCODER]) == []
    assert pair.server_quic.close_error is None


@pytest.mark.unit
@pytest.mark.parametrize(("blocked_streams", "expect_blocked"), [(0, False), (16, True)])
def test_qpack_blocked_streams_setting_bounds_encoder_risk(
    blocked_streams: int, expect_blocked: bool
) -> None:
    encoder = pylsqpack.Encoder()
    decoder = pylsqpack.Decoder(4096, blocked_streams)
    encoder.apply_settings(4096, blocked_streams)  # decoder never sees the encoder stream
    outcomes: list[bool] = []
    encoder_stream_bytes = 0
    for stream_id in (0, 4, 8):
        instructions, block = encoder.encode(stream_id, REQUEST)
        encoder_stream_bytes += len(instructions)
        try:
            decoder.feed_header(stream_id, block)
            outcomes.append(False)
        except pylsqpack.StreamBlocked:
            outcomes.append(True)
    assert outcomes == [False, expect_blocked, expect_blocked]
    assert encoder_stream_bytes == 34  # entries are inserted either way; only references wait


@pytest.mark.unit
def test_decoder_fails_when_more_streams_block_than_it_advertised() -> None:
    encoder = pylsqpack.Encoder()
    decoder = pylsqpack.Decoder(4096, 1)
    decoder.feed_encoder(encoder.apply_settings(4096, 16))  # a lying encoder
    encoder.encode(0, REQUEST)
    _, first = encoder.encode(4, [*REQUEST, (b"x-req", b"4")])
    _, second = encoder.encode(8, [*REQUEST, (b"x-req", b"8")])
    with pytest.raises(pylsqpack.StreamBlocked):
        decoder.feed_header(4, first)
    with pytest.raises(pylsqpack.DecompressionFailed):
        decoder.feed_header(8, second)


@pytest.mark.unit
def test_required_insert_count_and_base_match_rfc_9204_examples() -> None:
    # Section 4.5.1.1: 100-byte table, 10 inserts received, encoded 4 -> 9.
    assert decode_required_insert_count(4, 100, 10) == 9
    # Appendix B.2: capacity 220, prefix 0x03 0x81 -> Required Insert Count 2, Base 0.
    assert parse_field_section_prefix(bytes.fromhex("038110"), 220, 2) == FieldSectionPrefix(2, 0)
    # Section 4.5.1.2: Required Insert Count 9, Sign 1, Delta Base 2 -> Base 6.
    assert parse_field_section_prefix(bytes([4, 0x82]), 100, 10) == FieldSectionPrefix(9, 6)
    with pytest.raises(ValueError, match="out of range"):
        decode_required_insert_count(7, 100, 10)
    with pytest.raises(ValueError, match="negative Base"):
        parse_field_section_prefix(bytes([3, 0x82]), 220, 2)
    # Encoded 6 with 0 inserts would need Required Insert Count 5 > MaxValue 3 without a wrap.
    with pytest.raises(ValueError, match="did not wrap"):
        decode_required_insert_count(6, 100, 0)
    # Encoded 1 with 0 inserts reconstructs 0, which must have been sent as 0.
    with pytest.raises(ValueError, match="zero must be encoded as zero"):
        decode_required_insert_count(1, 100, 0)
    with pytest.raises(ValueError, match="truncated field section prefix"):
        parse_field_section_prefix(b"\x00", 4096, 0)


@pytest.mark.unit
def test_prefixed_integer_decodes_capacity_and_bounds_continuations() -> None:
    assert decode_prefixed_int(b"\x3f\xe1\x1f", 0, 5) == (4096, 3)  # Set Capacity=4096
    assert decode_prefixed_int(b"\x84", 0, 7) == (4, 1)
    with pytest.raises(ValueError, match="too many continuation bytes"):
        decode_prefixed_int(b"\x3f" + b"\x80" * 64, 0, 5)
    with pytest.raises(ValueError, match="exceeds 62 bits"):
        decode_prefixed_int(b"\xff" + b"\xff" * 8 + b"\x7f", 0, 8)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("frame", "error"),
    [
        (encode_frame(0x02, b"\x00"), ErrorCode.H3_FRAME_UNEXPECTED),  # HTTP/2 PRIORITY
        (encode_frame(0x00, b"hi"), ErrorCode.H3_FRAME_UNEXPECTED),  # DATA before HEADERS
        (encode_frame(FRAME_SETTINGS, b""), ErrorCode.H3_FRAME_UNEXPECTED),
        (encode_frame(0x21, b"pad"), None),  # reserved type: ignored
    ],
)
def test_request_stream_frame_rules(frame: bytes, error: ErrorCode | None) -> None:
    pair = H3Pair()
    pair.server.handle_event(StreamDataReceived(data=frame, end_stream=False, stream_id=0))
    assert pair.server_quic.close_error == error


@pytest.mark.unit
def test_frame_truncated_by_fin_is_frame_error() -> None:
    pair = H3Pair()
    truncated = encode_frame(FRAME_HEADERS, b"\x00\x00\xd1")[:-1]
    pair.server.handle_event(StreamDataReceived(data=truncated, end_stream=True, stream_id=0))
    assert pair.server_quic.close_error == ErrorCode.H3_FRAME_ERROR


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "fin", "error"),
    [
        (encode_frame(FRAME_SETTINGS, b""), True, ErrorCode.H3_CLOSED_CRITICAL_STREAM),
        (encode_frame(0x07, b"\x00"), False, ErrorCode.H3_MISSING_SETTINGS),  # GOAWAY first
        (encode_frame(0x21, b""), False, ErrorCode.H3_MISSING_SETTINGS),  # grease first
        (encode_frame(FRAME_SETTINGS, b"\x02\x01"), False, ErrorCode.H3_SETTINGS_ERROR),
        (encode_frame(FRAME_SETTINGS, b"") * 2, False, ErrorCode.H3_FRAME_UNEXPECTED),
        (
            encode_frame(FRAME_SETTINGS, b"") + encode_frame(FRAME_HEADERS, b"\x00\x00"),
            False,
            ErrorCode.H3_FRAME_UNEXPECTED,
        ),
        (encode_frame(FRAME_SETTINGS, b"") + encode_frame(0x21, b"xx"), False, None),
    ],
)
def test_control_stream_rules(body: bytes, fin: bool, error: ErrorCode | None) -> None:
    pair = H3Pair()
    wire = encode_varint(STREAM_CONTROL) + body
    pair.server.handle_event(StreamDataReceived(data=wire, end_stream=fin, stream_id=2))
    assert pair.server_quic.close_error == error


@pytest.mark.unit
@pytest.mark.parametrize("stream_type", [STREAM_QPACK_ENCODER, STREAM_QPACK_DECODER])
def test_aioquic_closes_on_qpack_stream_reset_but_not_on_clean_fin(stream_type: int) -> None:
    header = encode_varint(stream_type)
    fin = H3Pair()
    fin.server.handle_event(StreamDataReceived(data=header, end_stream=True, stream_id=6))
    # RFC 9204 Section 4.2 requires H3_CLOSED_CRITICAL_STREAM here; aioquic 1.7.2 does not.
    assert fin.server_quic.close_error is None

    reset = H3Pair()
    reset.server.handle_event(StreamDataReceived(data=header, end_stream=False, stream_id=6))
    reset.reset("client", 6, ErrorCode.H3_NO_ERROR)
    assert reset.server_quic.close_error == ErrorCode.H3_CLOSED_CRITICAL_STREAM


@pytest.mark.unit
def test_one_dynamic_entry_amplifies_three_byte_sections() -> None:
    encoder = pylsqpack.Encoder()
    decoder = pylsqpack.Decoder(4096, 16)
    decoder.feed_encoder(encoder.apply_settings(4096, 16))
    blob = [(b"x-blob", b"a" * 4000)]
    sizes: list[tuple[int, int, int]] = []
    for stream_id in range(0, 40, 4):
        instructions, block = encoder.encode(stream_id, blob)
        decoder.feed_encoder(instructions)
        feedback, headers = decoder.feed_header(stream_id, block)
        encoder.feed_decoder(feedback)
        sizes.append((len(instructions), len(block), field_section_size(headers)))
    assert sizes[0] == (0, 2511, 4038)  # first sight: literal
    assert sizes[1] == (2509, 3, 4038)  # insert with literal name, then a reference
    assert all(size == (1, 3, 4038) for size in sizes[2:])  # Duplicate + reference
    assert field_section_size(blob) // len(encode_frame(FRAME_HEADERS, b"\x00" * 3)) == 807


@pytest.mark.unit
def test_aioquic_does_not_enforce_a_decoded_field_section_limit() -> None:
    pair = H3Pair()
    pair.handshake()
    big = [*REQUEST, *((f"x-pad-{i:02d}".encode(), b"v" * 1000) for i in range(20))]
    pair.client.send_headers(0, big, end_stream=True)
    events = pair.deliver("client", [CLIENT_ENCODER, 0])
    assert events == [HeadersReceived(headers=big, stream_id=0, stream_ended=True)]
    assert pair.server_quic.close_error is None
    # The loopback hands a stream's bytes and its FIN to the peer exactly once.
    assert pair.client_quic.take(0) is None
    assert field_section_size(big) == 21_034
    enforce_field_section_limit(big, 32_768)
    with pytest.raises(ValueError, match="exceeds limit 16384"):
        enforce_field_section_limit(big, 16_384)
