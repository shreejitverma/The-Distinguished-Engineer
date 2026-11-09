"""Unit tests for the HTTP/2 frame layer and the h2 4.4.1 stream state machine (RFC 9113)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.exceptions
import pytest
from h2.errors import ErrorCodes
from h2.stream import StreamState
from hyperframe.frame import Frame, HeadersFrame

from api_labs.f01_http_semantics_and_wire_protocols.h2_stream_states import (
    CLIENT_PREFACE,
    FLAG_END_HEADERS,
    FLAG_END_STREAM,
    MAX_FRAME_LENGTH,
    FrameType,
    Outcome,
    RemoteResetBudget,
    build_frame,
    connected_pair,
    decode_frame_header,
    decode_settings,
    encode_frame_header,
    feed,
    split_frames,
    stream_state,
    trace_exchange,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    Case = Callable[[], tuple[h2.connection.H2Connection, bytes]]

REQUEST = [(":method", "GET"), (":scheme", "https"), (":authority", "api.test"), (":path", "/")]
POST = [(":method", "POST"), (":scheme", "https"), (":authority", "api.test"), (":path", "/v1")]
END_STREAM_AND_HEADERS = FLAG_END_STREAM | FLAG_END_HEADERS


def _headers_frame(
    conn: h2.connection.H2Connection, stream_id: int, flags: int = END_STREAM_AND_HEADERS
) -> bytes:
    """A HEADERS frame whose field block is encoded with the client's own HPACK context."""
    return build_frame(FrameType.HEADERS, flags, stream_id, conn.encoder.encode(REQUEST))


@pytest.mark.unit
def test_frame_header_layout_is_24_8_8_1_31_big_endian() -> None:
    wire = encode_frame_header(21, FrameType.HEADERS, END_STREAM_AND_HEADERS, 1)
    assert wire == bytes.fromhex("000015010500000001")

    reference = HeadersFrame(1, data=b"\x00" * 21, flags=["END_STREAM", "END_HEADERS"])
    assert reference.serialize()[:9] == wire

    max_header = encode_frame_header((1 << 24) - 1, 0xFF, 0xFF, (1 << 31) - 1)
    assert max_header == b"\xff" * 3 + b"\xff\xff" + b"\x7f\xff\xff\xff"


@pytest.mark.unit
def test_get_request_is_one_headers_frame_with_end_stream_and_end_headers() -> None:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    client.initiate_connection()
    client.data_to_send()
    get = [(":method", "GET"), (":scheme", "https"), (":authority", "example.org")]
    client.send_headers(1, [*get, (":path", "/resource")], end_stream=True)

    wire = client.data_to_send()
    assert wire.hex(" ") == (
        "00 00 15 01 05 00 00 00 01 82 87 41 88 2f 91 d3 5d 05 5c f6 4d 44 87 62 c2 a0 f6 d8 42 ff"
    )
    (frame,) = split_frames(wire)
    assert frame.header == decode_frame_header(wire[:9])
    assert (frame.header.length, frame.header.type, frame.header.stream_id) == (21, 1, 1)


@pytest.mark.unit
def test_reserved_bit_is_ignored_on_receipt() -> None:
    with_reserved = encode_frame_header(0, FrameType.DATA, 0, 7, reserved=True)
    assert with_reserved[5] & 0x80
    assert decode_frame_header(with_reserved).stream_id == 7

    parsed, length = Frame.parse_frame_header(memoryview(with_reserved))
    assert (parsed.stream_id, length) == (7, 0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("length", "frame_type", "flags", "stream_id"),
    [(1 << 24, 0, 0, 1), (-1, 0, 0, 1), (0, 0x100, 0, 1), (0, 0, 0x100, 1), (0, 0, 0, 1 << 31)],
    ids=["length-25-bits", "length-negative", "type-9-bits", "flags-9-bits", "stream-32-bits"],
)
def test_encode_frame_header_rejects_fields_that_do_not_fit(
    length: int, frame_type: int, flags: int, stream_id: int
) -> None:
    with pytest.raises(ValueError, match="must fit"):
        encode_frame_header(length, frame_type, flags, stream_id)


@pytest.mark.unit
def test_split_frames_and_decoders_reject_truncation() -> None:
    whole = build_frame(FrameType.PING, 0, 0, b"12345678")
    assert len(split_frames(whole + whole)) == 2
    with pytest.raises(ValueError, match="truncated frame header"):
        split_frames(whole + whole[:8])
    with pytest.raises(ValueError, match="truncated frame payload"):
        split_frames(whole[:-1])
    with pytest.raises(ValueError, match="exactly 9 octets"):
        decode_frame_header(whole[:8])
    with pytest.raises(ValueError, match="multiple of 6"):
        decode_settings(b"\x00" * 7)


@pytest.mark.unit
def test_client_preface_then_settings_on_the_wire() -> None:
    fresh_client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    fresh_client.initiate_connection()
    wire = fresh_client.data_to_send()
    assert wire[:24] == bytes.fromhex("505249202a20485454502f322e300d0a0d0a534d0d0a0d0a")
    assert wire[:24] == CLIENT_PREFACE

    (settings,) = split_frames(wire[24:])
    assert (settings.header.type, settings.header.stream_id) == (FrameType.SETTINGS, 0)
    assert decode_settings(settings.payload) == {
        0x1: 4096,
        0x2: 1,
        0x3: 100,
        0x4: 65535,
        0x5: 16384,
        0x6: 65536,
        0x8: 0,
    }

    fresh_server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    fresh_server.initiate_connection()
    (server_settings,) = split_frames(fresh_server.data_to_send())
    assert decode_settings(server_settings.payload)[0x2] == 0


@pytest.mark.unit
def test_request_response_walks_the_rfc_9113_lifecycle_from_both_sides() -> None:
    steps = trace_exchange(POST, b'{"n":1}', b"ok")
    assert steps == [
        ("start", None, None),
        ("client sends HEADERS", StreamState.OPEN, None),
        ("server receives HEADERS", StreamState.OPEN, StreamState.OPEN),
        ("client sends DATA+END_STREAM", StreamState.HALF_CLOSED_LOCAL, StreamState.OPEN),
        (
            "server receives DATA+END_STREAM",
            StreamState.HALF_CLOSED_LOCAL,
            StreamState.HALF_CLOSED_REMOTE,
        ),
        (
            "server sends HEADERS, DATA+END_STREAM",
            StreamState.HALF_CLOSED_LOCAL,
            StreamState.CLOSED,
        ),
        ("client receives response", StreamState.CLOSED, StreamState.CLOSED),
    ]


@pytest.mark.unit
def test_headers_with_end_stream_is_two_transitions_in_one_frame() -> None:
    steps = trace_exchange(REQUEST, b"", b"")
    assert steps[1] == ("client sends HEADERS", StreamState.HALF_CLOSED_LOCAL, None)
    assert steps[2][2] is StreamState.HALF_CLOSED_REMOTE

    client, _server = connected_pair()
    client.send_headers(1, REQUEST, end_stream=True)
    (frame,) = split_frames(client.data_to_send())
    assert frame.header.flags == END_STREAM_AND_HEADERS


def _after_request(
    end_stream: bool,
) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client, server = connected_pair()
    client.send_headers(1, REQUEST, end_stream=end_stream)
    server.receive_data(client.data_to_send())
    return client, server


def _after_full_exchange() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client, server = _after_request(end_stream=True)
    server.send_headers(1, [(":status", "204")], end_stream=True)
    client.receive_data(server.data_to_send())
    return client, server


def _data_on_idle_stream() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = _after_request(end_stream=False)
    return server, build_frame(FrameType.DATA, FLAG_END_STREAM, 3, b"x")


def _window_update_on_idle_stream() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = _after_request(end_stream=False)
    return server, build_frame(FrameType.WINDOW_UPDATE, 0, 3, (1024).to_bytes(4, "big"))


def _lower_stream_id_after_higher() -> tuple[h2.connection.H2Connection, bytes]:
    client, server = connected_pair()
    client.send_headers(3, REQUEST, end_stream=True)
    server.receive_data(client.data_to_send())
    return server, _headers_frame(client, 1)


def _even_stream_id_from_client() -> tuple[h2.connection.H2Connection, bytes]:
    client, server = connected_pair()
    return server, _headers_frame(client, 2)


def _ping_inside_header_block() -> tuple[h2.connection.H2Connection, bytes]:
    client, server = connected_pair()
    first = _headers_frame(client, 1, flags=FLAG_END_STREAM)
    return server, first + build_frame(FrameType.PING, 0, 0, b"\x00" * 8)


def _extension_frame_inside_header_block() -> tuple[h2.connection.H2Connection, bytes]:
    client, server = connected_pair()
    return server, _headers_frame(client, 1, flags=FLAG_END_STREAM) + build_frame(0xFA, 0, 1)


def _data_on_stream_zero() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = _after_request(end_stream=False)
    return server, build_frame(FrameType.DATA, 0, 0, b"x")


def _settings_on_stream_one() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = connected_pair()
    return server, build_frame(FrameType.SETTINGS, 0, 1)


def _headers_on_stream_closed_by_end_stream() -> tuple[h2.connection.H2Connection, bytes]:
    client, server = _after_full_exchange()
    return server, _headers_frame(client, 1)


def _frame_over_max_frame_size() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = _after_request(end_stream=False)
    return server, build_frame(FrameType.DATA, 0, 1, b"\x00" * 16385)


def _rst_stream_with_5_octets() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = _after_request(end_stream=False)
    return server, build_frame(FrameType.RST_STREAM, 0, 1, b"\x00" * 5)


def _ping_with_7_octets() -> tuple[h2.connection.H2Connection, bytes]:
    _client, server = connected_pair()
    return server, build_frame(FrameType.PING, 0, 0, b"\x00" * 7)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("make_case", "exception", "error_code", "last_stream_id"),
    [
        (_data_on_idle_stream, "NoSuchStreamError", ErrorCodes.PROTOCOL_ERROR, 1),
        (_window_update_on_idle_stream, "NoSuchStreamError", ErrorCodes.PROTOCOL_ERROR, 1),
        (_lower_stream_id_after_higher, "StreamIDTooLowError", ErrorCodes.PROTOCOL_ERROR, 3),
        (_even_stream_id_from_client, "ProtocolError", ErrorCodes.PROTOCOL_ERROR, 0),
        (_ping_inside_header_block, "ProtocolError", ErrorCodes.PROTOCOL_ERROR, 0),
        (_extension_frame_inside_header_block, "ProtocolError", ErrorCodes.PROTOCOL_ERROR, 0),
        (_data_on_stream_zero, "ProtocolError", ErrorCodes.PROTOCOL_ERROR, 1),
        (_settings_on_stream_one, "ProtocolError", ErrorCodes.PROTOCOL_ERROR, 0),
        (_headers_on_stream_closed_by_end_stream, "StreamClosedError", ErrorCodes.STREAM_CLOSED, 1),
        (_frame_over_max_frame_size, "FrameTooLargeError", ErrorCodes.FRAME_SIZE_ERROR, 1),
        (_rst_stream_with_5_octets, "FrameDataMissingError", ErrorCodes.FRAME_SIZE_ERROR, 1),
        (_ping_with_7_octets, "FrameDataMissingError", ErrorCodes.FRAME_SIZE_ERROR, 0),
    ],
    ids=[
        "data-idle",
        "window-update-idle",
        "stream-id-regression",
        "even-id-from-client",
        "ping-inside-field-block",
        "extension-inside-field-block",
        "data-stream-0",
        "settings-stream-1",
        "headers-after-closed",
        "frame-over-16384",
        "rst-stream-5-octets",
        "ping-7-octets",
    ],
)
def test_connection_errors_emit_goaway_with_the_rfc_error_code(
    make_case: Case, exception: str, error_code: ErrorCodes, last_stream_id: int
) -> None:
    conn, wire = make_case()
    outcome = feed(conn, wire)
    assert outcome.error == exception
    assert outcome.error_code is error_code
    assert outcome.emitted_types() == (FrameType.GOAWAY,)
    assert outcome.goaway() == (last_stream_id, error_code)


@pytest.mark.unit
@pytest.mark.parametrize(
    "late_frame",
    [
        build_frame(FrameType.DATA, 0, 1, b"late"),
        build_frame(FrameType.HEADERS, END_STREAM_AND_HEADERS, 1, b"\x88"),
    ],
    ids=["data", "headers"],
)
def test_frame_after_peer_end_stream_is_a_stream_error_not_a_connection_error(
    late_frame: bytes,
) -> None:
    _client, server = _after_request(end_stream=True)
    assert stream_state(server, 1) is StreamState.HALF_CLOSED_REMOTE

    outcome = feed(server, late_frame)
    assert outcome.error is None
    assert outcome.events == ("StreamReset",)
    assert outcome.emitted_types() == (FrameType.RST_STREAM,)
    assert outcome.rst_stream() == (1, ErrorCodes.STREAM_CLOSED)
    assert stream_state(server, 1) is StreamState.CLOSED


@pytest.mark.unit
def test_priority_on_idle_stream_and_unknown_frame_types_are_not_errors() -> None:
    _client, server = connected_pair()
    priority = feed(server, build_frame(FrameType.PRIORITY, 0, 5, b"\x00\x00\x00\x00\x0f"))
    assert (priority.error, priority.events, priority.emitted) == (None, ("PriorityUpdated",), ())
    assert stream_state(server, 5) is None

    unknown = feed(server, build_frame(0xFA, 0xFF, 0, b"opaque"))
    assert (unknown.error, unknown.events, unknown.emitted) == (None, ("UnknownFrameReceived",), ())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("continuations", "accepted"),
    [(63, True), (64, False)],
    ids=["headers-plus-63", "headers-plus-64"],
)
def test_continuation_backlog_caps_a_field_block_at_64_frames(
    continuations: int, accepted: bool
) -> None:
    client, server = connected_pair()
    wire = _headers_frame(client, 1, flags=FLAG_END_STREAM)
    wire += build_frame(FrameType.CONTINUATION, 0, 1) * (continuations - 1)
    wire += build_frame(FrameType.CONTINUATION, FLAG_END_HEADERS, 1)

    outcome = feed(server, wire)
    if accepted:
        assert outcome.error is None
        assert outcome.events == ("RequestReceived", "StreamEnded")
    else:
        assert outcome.error == "ProtocolError"
        assert outcome.goaway() == (0, ErrorCodes.PROTOCOL_ERROR)


@pytest.mark.unit
def test_h2_buffers_an_oversize_frame_until_it_is_complete_before_frame_size_error() -> None:
    _client, server = _after_request(end_stream=False)
    header = encode_frame_header(MAX_FRAME_LENGTH, FrameType.DATA, 0, 1)
    partial = feed(server, header + b"\x00" * 100_000)
    assert (partial.error, partial.events, partial.emitted) == (None, (), ())

    rest = feed(server, b"\x00" * (MAX_FRAME_LENGTH - 100_000))
    assert rest.error == "FrameTooLargeError"
    assert rest.goaway() == (1, ErrorCodes.FRAME_SIZE_ERROR)


@pytest.mark.unit
def test_h2_ignores_rst_stream_on_idle_stream_although_rfc_9113_requires_protocol_error() -> None:
    _client, server = _after_request(end_stream=False)
    outcome = feed(server, build_frame(FrameType.RST_STREAM, 0, 3, (8).to_bytes(4, "big")))
    assert (outcome.error, outcome.events, outcome.emitted) == (None, (), ())
    assert server.highest_inbound_stream_id == 1
    assert stream_state(server, 3) is None


@pytest.mark.unit
def test_h2_treats_its_own_goaway_as_terminal_for_streams_at_or_below_last_stream_id() -> None:
    client, server = _after_request(end_stream=False)
    server.close_connection(ErrorCodes.NO_ERROR)
    (goaway,) = split_frames(server.data_to_send())
    assert goaway.header.type == FrameType.GOAWAY
    assert int.from_bytes(goaway.payload[:4], "big") == 1

    client.send_data(1, b"in-flight", end_stream=True)
    outcome = feed(server, client.data_to_send())
    assert outcome.error == "ProtocolError"
    assert outcome.error_code is ErrorCodes.PROTOCOL_ERROR


@pytest.mark.unit
def test_101st_concurrent_stream_is_refused_as_a_connection_error() -> None:
    client, server = connected_pair()
    for stream_id in range(1, 201, 2):
        assert feed(server, _headers_frame(client, stream_id, flags=FLAG_END_HEADERS)).error is None
    assert server.open_inbound_streams == 100

    outcome = feed(server, _headers_frame(client, 201, flags=FLAG_END_HEADERS))
    assert outcome.error == "TooManyStreamsError"
    assert outcome.goaway() == (199, ErrorCodes.PROTOCOL_ERROR)


@pytest.mark.unit
def test_rapid_reset_runs_1000_requests_through_a_100_stream_limit() -> None:
    client, server = connected_pair()
    for stream_id in range(1, 2001, 2):
        client.send_headers(stream_id, REQUEST)
        client.reset_stream(stream_id, ErrorCodes.CANCEL)

    outcome = feed(server, client.data_to_send())
    assert outcome.error is None
    assert outcome.events.count("RequestReceived") == 1000
    assert outcome.events.count("StreamReset") == 1000
    assert server.open_inbound_streams == 0
    assert server.local_settings.max_concurrent_streams == 100


@pytest.mark.unit
def test_remote_reset_budget_sends_goaway_enhance_your_calm_after_budget() -> None:
    client, server = connected_pair()
    guard = RemoteResetBudget(server, max_remote_resets=10)
    outcomes: list[Outcome] = []
    for stream_id in range(1, 23, 2):
        client.send_headers(stream_id, REQUEST)
        client.reset_stream(stream_id, ErrorCodes.CANCEL)
        outcomes.append(guard.receive(client.data_to_send()))

    assert [o.emitted for o in outcomes[:10]] == [()] * 10
    assert outcomes[10].goaway() == (21, ErrorCodes.ENHANCE_YOUR_CALM)
    assert guard.remote_resets == 11
    with pytest.raises(RuntimeError, match="already closed"):
        guard.receive(b"")


@pytest.mark.unit
def test_remote_reset_budget_ignores_resets_the_server_sends_itself() -> None:
    client, server = connected_pair()
    guard = RemoteResetBudget(server, max_remote_resets=10)
    for stream_id in range(1, 23, 2):
        late_data = build_frame(FrameType.DATA, 0, stream_id, b"late")
        outcome = guard.receive(_headers_frame(client, stream_id) + late_data)
        assert outcome.events == ("RequestReceived", "StreamEnded", "StreamReset")
        assert FrameType.GOAWAY not in outcome.emitted_types()
        assert outcome.rst_stream() == (stream_id, ErrorCodes.STREAM_CLOSED)

    assert (guard.remote_resets, guard.tripped) == (0, False)


@pytest.mark.unit
def test_local_send_on_closed_stream_raises_before_any_bytes_are_queued() -> None:
    client, _server = _after_full_exchange()
    assert stream_state(client, 1) is StreamState.CLOSED
    with pytest.raises(h2.exceptions.StreamClosedError):
        client.send_data(1, b"too late")
    assert client.data_to_send() == b""
