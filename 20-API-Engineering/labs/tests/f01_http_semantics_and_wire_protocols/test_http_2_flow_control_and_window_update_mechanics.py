"""HTTP/2 flow control: WINDOW_UPDATE wire rules and h2 4.4.1 backpressure (RFC 9113 6.9)."""

from __future__ import annotations

import h2.events
import h2.exceptions
import h2.settings
import pytest
from h2.errors import ErrorCodes
from hyperframe.frame import DataFrame, GoAwayFrame, RstStreamFrame, WindowUpdateFrame

from api_labs.f01_http_semantics_and_wire_protocols.http2_flow_control import (
    DEFAULT_INITIAL_WINDOW,
    MAX_WINDOW,
    H2Pair,
    WindowUpdate,
    WindowUpdateError,
    decode_window_update,
    encode_window_update,
    new_pair,
    open_response_streams,
    parse_frames,
    send_permitted,
    window_limited_throughput,
    window_updates,
)

BODY = b"r" * 100_000


@pytest.mark.unit
def test_window_update_wire_layout_matches_hyperframe() -> None:
    wire = encode_window_update(stream_id=1, increment=0x12345)
    assert wire.hex() == "00000408000000000100012345"
    assert wire == WindowUpdateFrame(1, window_increment=0x12345).serialize()
    assert decode_window_update(wire) == WindowUpdate(stream_id=1, increment=0x12345)
    assert len(encode_window_update(0, MAX_WINDOW)) == 13
    for bad_increment in (0, MAX_WINDOW + 1):
        with pytest.raises(ValueError, match="increment"):
            encode_window_update(1, bad_increment)
    with pytest.raises(ValueError, match="31 bits"):
        encode_window_update(MAX_WINDOW + 1, 1)
    with pytest.raises(ValueError, match="truncated"):
        decode_window_update(wire[:8])
    with pytest.raises(ValueError, match="not a WINDOW_UPDATE"):
        decode_window_update(bytes.fromhex("000004060000000000") + b"ping")


@pytest.mark.unit
def test_window_update_receiver_error_classes() -> None:
    zero_on_stream = bytes.fromhex("00000408000000000100000000")
    with pytest.raises(WindowUpdateError) as stream_err:
        decode_window_update(zero_on_stream)
    assert (stream_err.value.error_code, stream_err.value.connection_error) == (
        "PROTOCOL_ERROR",
        False,
    )

    zero_on_connection = bytes.fromhex("00000408000000000000000000")
    with pytest.raises(WindowUpdateError) as conn_err:
        decode_window_update(zero_on_connection)
    assert conn_err.value.connection_error is True

    five_octets = bytes.fromhex("0000050800000000000000000100")
    with pytest.raises(WindowUpdateError) as size_err:
        decode_window_update(five_octets)
    assert (size_err.value.error_code, size_err.value.connection_error) == (
        "FRAME_SIZE_ERROR",
        True,
    )

    # Length field says 4 but the buffer carries a trailing octet or lacks one.
    for inconsistent in (
        bytes.fromhex("00000408000000000100000010") + b"\x00",
        bytes.fromhex("000004080000000001000000"),
    ):
        with pytest.raises(WindowUpdateError) as inconsistent_err:
            decode_window_update(inconsistent)
        assert inconsistent_err.value.error_code == "FRAME_SIZE_ERROR"
        assert inconsistent_err.value.connection_error is True


@pytest.mark.unit
def test_reserved_bit_is_masked_here_but_rejected_by_h2() -> None:
    reserved_set = bytes.fromhex("00000408000000000080000010")
    assert decode_window_update(reserved_set) == WindowUpdate(stream_id=0, increment=16)
    # RFC 9113 Section 4.1: the header's reserved bit is ignored, so stream 0x80000001 is stream 1.
    header_reserved_set = bytes.fromhex("00000408008000000100000010")
    assert decode_window_update(header_reserved_set) == WindowUpdate(stream_id=1, increment=16)

    pair = new_pair()
    with pytest.raises(h2.exceptions.ProtocolError, match="non-compliant data"):
        pair.server.receive_data(reserved_set)
    goaway = parse_frames(pair.server.data_to_send())[-1]
    assert isinstance(goaway, GoAwayFrame)
    assert goaway.error_code == ErrorCodes.PROTOCOL_ERROR


@pytest.mark.unit
def test_only_data_frames_consume_the_default_windows() -> None:
    pair = new_pair()
    (stream_id,) = open_response_streams(pair, 1)
    assert pair.server.local_flow_control_window(stream_id) == DEFAULT_INITIAL_WINDOW
    assert pair.server.outbound_flow_control_window == DEFAULT_INITIAL_WINDOW

    pair.server.ping(b"12345678")
    pair.pump()
    assert pair.server.local_flow_control_window(stream_id) == DEFAULT_INITIAL_WINDOW

    pair.server.send_data(stream_id, b"x" * 1000)
    assert pair.server.local_flow_control_window(stream_id) == DEFAULT_INITIAL_WINDOW - 1000
    assert pair.server.outbound_flow_control_window == DEFAULT_INITIAL_WINDOW - 1000


@pytest.mark.unit
def test_stalled_stream_starves_siblings_at_default_connection_window() -> None:
    pair = new_pair()
    slow, sibling = open_response_streams(pair, 2)

    assert send_permitted(pair.server, slow, BODY) == DEFAULT_INITIAL_WINDOW
    assert send_permitted(pair.server, sibling, BODY) == 0
    assert pair.server.outbound_flow_control_window == 0

    result = pair.pump()
    received = [e for e in result.client_events if isinstance(e, h2.events.DataReceived)]
    assert sum(len(e.data or b"") for e in received) == DEFAULT_INITIAL_WINDOW
    assert window_updates(result.client_frames) == []
    assert send_permitted(pair.server, sibling, BODY) == 0


@pytest.mark.unit
def test_connection_credit_isolates_a_stalled_stream_until_it_saturates() -> None:
    pair = new_pair()
    pair.client.increment_flow_control_window(2 * DEFAULT_INITIAL_WINDOW)
    streams = open_response_streams(pair, 4)
    assert pair.server.outbound_flow_control_window == 3 * DEFAULT_INITIAL_WINDOW

    sent = [send_permitted(pair.server, stream_id, BODY) for stream_id in streams]
    assert sent == [DEFAULT_INITIAL_WINDOW, DEFAULT_INITIAL_WINDOW, DEFAULT_INITIAL_WINDOW, 0]
    assert pair.server.local_flow_control_window(streams[0]) == 0
    assert pair.server.outbound_flow_control_window == 0


@pytest.mark.unit
def test_acknowledging_consumed_bytes_reopens_both_windows() -> None:
    pair = new_pair()
    slow, sibling = open_response_streams(pair, 2)
    send_permitted(pair.server, slow, BODY)
    pair.pump()

    pair.client.acknowledge_received_data(DEFAULT_INITIAL_WINDOW, slow)
    result = pair.pump()
    assert window_updates(result.client_frames) == [
        (0, DEFAULT_INITIAL_WINDOW),
        (slow, DEFAULT_INITIAL_WINDOW),
    ]
    assert send_permitted(pair.server, sibling, BODY) == DEFAULT_INITIAL_WINDOW


@pytest.mark.unit
def test_h2_batches_window_updates_at_half_window_or_on_a_closed_window() -> None:
    pair = new_pair()
    (stream_id,) = open_response_streams(pair, 1)
    send_permitted(pair.server, stream_id, b"q" * 40_000)
    pair.pump()
    pair.client.acknowledge_received_data(32_766, stream_id)
    assert window_updates(pair.pump().client_frames) == []
    pair.client.acknowledge_received_data(1, stream_id)
    assert window_updates(pair.pump().client_frames) == [(0, 32_767), (stream_id, 32_767)]

    pair = new_pair()
    (stream_id,) = open_response_streams(pair, 1)
    send_permitted(pair.server, stream_id, BODY)
    pair.pump()
    pair.client.acknowledge_received_data(1024, stream_id)
    assert window_updates(pair.pump().client_frames) == []
    pair.client.acknowledge_received_data(1, stream_id)
    assert window_updates(pair.pump().client_frames) == [(0, 1025), (stream_id, 1025)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ack_flow_controlled_length", "server_window_after_ack"),
    [(True, DEFAULT_INITIAL_WINDOW), (False, DEFAULT_INITIAL_WINDOW - 44)],
)
def test_padding_is_flow_controlled_and_acking_len_data_leaks_credit(
    *, ack_flow_controlled_length: bool, server_window_after_ack: int
) -> None:
    pair = new_pair()
    (stream_id,) = open_response_streams(pair, 1)
    for data_len in (16_373, 16_373, 16_373, 16_372):
        pair.server.send_data(stream_id, b"p" * data_len, pad_length=10)
    assert pair.server.local_flow_control_window(stream_id) == 0

    received = [e for e in pair.pump().client_events if isinstance(e, h2.events.DataReceived)]
    flow_len = sum(e.flow_controlled_length or 0 for e in received)
    data_len_total = sum(len(e.data or b"") for e in received)
    assert (flow_len, data_len_total) == (DEFAULT_INITIAL_WINDOW, DEFAULT_INITIAL_WINDOW - 44)

    pair.client.acknowledge_received_data(
        flow_len if ack_flow_controlled_length else data_len_total, stream_id
    )
    pair.pump()
    assert pair.server.local_flow_control_window(stream_id) == server_window_after_ack


def _upload_then_shrink_initial_window(pair: H2Pair) -> int:
    """Client uploads 61,440 bytes, then the server lowers INITIAL_WINDOW_SIZE to 16,384."""
    stream_id = pair.client.get_next_available_stream_id()
    post = [(b":method", b"POST"), (b":path", b"/upload"), (b":scheme", b"https")]
    pair.client.send_headers(stream_id, [*post, (b":authority", b"api.example")])
    assert send_permitted(pair.client, stream_id, b"u" * 61_440) == 61_440
    pair.pump()

    pair.server.update_settings({h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: 16_384})
    pair.pump()
    return stream_id


@pytest.mark.unit
def test_settings_reduction_drives_the_stream_window_negative() -> None:
    pair = new_pair()
    stream_id = _upload_then_shrink_initial_window(pair)
    assert pair.client.local_flow_control_window(stream_id) == -45_056
    assert pair.client.outbound_flow_control_window == DEFAULT_INITIAL_WINDOW - 61_440
    with pytest.raises(h2.exceptions.FlowControlError):
        pair.client.send_data(stream_id, b"u")

    pair.server.increment_flow_control_window(45_057, stream_id=stream_id)
    pair.pump()
    assert pair.client.local_flow_control_window(stream_id) == 1
    assert send_permitted(pair.client, stream_id, b"uu") == 1


@pytest.mark.unit
def test_empty_end_stream_and_trailers_are_allowed_on_a_closed_window() -> None:
    pair = new_pair()
    stream_id, trailer_stream = open_response_streams(pair, 2)
    send_permitted(pair.server, stream_id, BODY)
    assert pair.server.local_flow_control_window(trailer_stream) == 0
    with pytest.raises(h2.exceptions.FlowControlError):
        pair.server.send_data(trailer_stream, b"x")

    pair.server.send_data(stream_id, b"", end_stream=True)
    pair.server.send_headers(trailer_stream, [(b"grpc-status", b"0")], end_stream=True)
    events = pair.pump().client_events
    ended = {e.stream_id for e in events if isinstance(e, h2.events.StreamEnded)}
    assert ended == {stream_id, trailer_stream}
    assert any(isinstance(e, h2.events.TrailersReceived) for e in events)

    # On a negative window only the `frame_size > 0` guard in send_data lets the
    # empty END_STREAM frame out, but h2 4.4.1 as receiver charges 0 octets to a
    # stream window that is already -45,056, and window_consumed raises.
    pair = new_pair()
    upload = _upload_then_shrink_initial_window(pair)
    assert pair.client.local_flow_control_window(upload) == -45_056
    pair.client.send_data(upload, b"", end_stream=True)
    empty_end_stream = pair.client.data_to_send()
    assert empty_end_stream.hex() == "000000000100000001"
    with pytest.raises(h2.exceptions.FlowControlError, match="below 0"):
        pair.server.receive_data(empty_end_stream)
    goaway = parse_frames(pair.server.data_to_send())[-1]
    assert isinstance(goaway, GoAwayFrame)
    assert goaway.error_code == ErrorCodes.FLOW_CONTROL_ERROR


@pytest.mark.unit
def test_window_overflow_is_a_stream_error_or_a_connection_error() -> None:
    pair = new_pair()
    (stream_id,) = open_response_streams(pair, 1)
    events = pair.server.receive_data(encode_window_update(stream_id, MAX_WINDOW))
    assert isinstance(events[0], h2.events.StreamReset)
    assert events[0].error_code == ErrorCodes.FLOW_CONTROL_ERROR
    (rst,) = parse_frames(pair.server.data_to_send())
    assert isinstance(rst, RstStreamFrame)
    assert rst.error_code == ErrorCodes.FLOW_CONTROL_ERROR

    with pytest.raises(h2.exceptions.FlowControlError):
        pair.server.receive_data(encode_window_update(0, MAX_WINDOW))
    goaway = parse_frames(pair.server.data_to_send())[-1]
    assert isinstance(goaway, GoAwayFrame)
    assert goaway.error_code == ErrorCodes.FLOW_CONTROL_ERROR


@pytest.mark.unit
def test_h2_escalates_zero_increment_and_bad_length_to_connection_errors() -> None:
    pair = new_pair()
    open_response_streams(pair, 1)
    with pytest.raises(h2.exceptions.ProtocolError):
        pair.server.receive_data(bytes.fromhex("00000408000000000100000000"))
    goaway = parse_frames(pair.server.data_to_send())[-1]
    assert isinstance(goaway, GoAwayFrame)
    assert goaway.error_code == ErrorCodes.PROTOCOL_ERROR

    pair = new_pair()
    with pytest.raises(h2.exceptions.FrameDataMissingError):
        pair.server.receive_data(bytes.fromhex("0000050800000000000000000100"))
    goaway = parse_frames(pair.server.data_to_send())[-1]
    assert isinstance(goaway, GoAwayFrame)
    assert goaway.error_code == ErrorCodes.FRAME_SIZE_ERROR


@pytest.mark.unit
def test_peer_overrunning_the_window_gets_goaway_flow_control_error() -> None:
    pair = new_pair()
    stream_id = pair.client.get_next_available_stream_id()
    post = [(b":method", b"POST"), (b":path", b"/upload"), (b":scheme", b"https")]
    pair.client.send_headers(stream_id, [*post, (b":authority", b"api.example")])
    pair.pump()

    frame = DataFrame(stream_id, data=b"a" * 16_384).serialize()
    for _ in range(3):
        pair.server.receive_data(frame)
    pair.server.receive_data(DataFrame(stream_id, data=b"a" * 16_383).serialize())
    assert pair.server.remote_flow_control_window(stream_id) == 0
    with pytest.raises(h2.exceptions.FlowControlError):
        pair.server.receive_data(DataFrame(stream_id, data=b"a").serialize())
    goaway = parse_frames(pair.server.data_to_send())[-1]
    assert isinstance(goaway, GoAwayFrame)
    assert goaway.error_code == ErrorCodes.FLOW_CONTROL_ERROR


@pytest.mark.unit
def test_data_on_a_reset_stream_still_credits_the_connection_window() -> None:
    pair = new_pair()
    (stream_id,) = open_response_streams(pair, 1)
    assert send_permitted(pair.server, stream_id, b"z" * 40_000) == 40_000
    in_flight = pair.server.data_to_send()
    pair.client.reset_stream(stream_id, ErrorCodes.CANCEL)
    pair.client.data_to_send()

    assert pair.client.receive_data(in_flight) == []
    frames = parse_frames(pair.client.data_to_send())
    assert window_updates(frames) == [(0, 32_768)]
    rsts = [f for f in frames if isinstance(f, RstStreamFrame)]
    assert [f.error_code for f in rsts] == [ErrorCodes.STREAM_CLOSED] * 3
    assert pair.client.inbound_flow_control_window == DEFAULT_INITIAL_WINDOW - 40_000 + 32_768


@pytest.mark.unit
def test_window_limited_throughput_bound() -> None:
    assert window_limited_throughput(DEFAULT_INITIAL_WINDOW, 0.100) == pytest.approx(655_350.0)
    assert window_limited_throughput(1024 * 1024, 0.100) == pytest.approx(10_485_760.0)
    for bad_rtt in (0.0, -0.1):
        with pytest.raises(ValueError, match="rtt"):
            window_limited_throughput(DEFAULT_INITIAL_WINDOW, bad_rtt)
    with pytest.raises(ValueError, match="window"):
        window_limited_throughput(-1, 0.1)
