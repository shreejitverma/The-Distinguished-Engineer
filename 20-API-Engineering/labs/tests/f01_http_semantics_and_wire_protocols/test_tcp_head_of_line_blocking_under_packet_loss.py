"""TCP head-of-line blocking under loss: real h2 over a TCP receive model versus real aioquic."""

from __future__ import annotations

from typing import TYPE_CHECKING

import h2.exceptions
import pytest
from hyperframe.frame import ExtensionFrame, Frame

from api_labs.f01_http_semantics_and_wire_protocols.transport_hol import (
    QuicLab,
    Segment,
    StreamProgress,
    TcpReceiveBuffer,
    Transport,
    completion_times_ms,
    feed_h2,
    h2_response_segments,
    round_robin,
    stall_probability,
)

if TYPE_CHECKING:
    from collections.abc import Callable

BODIES = (b"a" * 1000, b"b" * 1000, b"c" * 1000)
EMPTY = StreamProgress({}, frozenset())


@pytest.mark.unit
def test_tcp_receive_buffer_holds_bytes_above_a_gap() -> None:
    tcp = TcpReceiveBuffer()
    assert tcp.receive(Segment(0, b"abc")) == b"abc"
    assert tcp.receive(Segment(6, b"ghi")) == b""  # above the gap: queued, not readable
    assert tcp.buffered_out_of_order == 3
    assert tcp.rcv_nxt == 3
    assert tcp.receive(Segment(3, b"def")) == b"defghi"  # the hole fills, both release
    assert tcp.buffered_out_of_order == 0
    assert tcp.receive(Segment(3, b"def")) == b""  # duplicate retransmission
    assert tcp.receive(Segment(7, b"hijk")) == b"jk"  # overlap: only new bytes
    assert tcp.rcv_nxt == 11


@pytest.mark.unit
def test_h2_segment_layout_puts_each_response_body_in_its_own_segment() -> None:
    _, segments = h2_response_segments(BODIES)
    assert [s.streams for s in segments] == [
        frozenset({1, 3, 5}),
        frozenset({1}),
        frozenset({3}),
        frozenset({5}),
    ]
    assert [len(s.payload) for s in segments[1:]] == [1009, 1009, 1009]  # 9-octet header
    assert [s.seq for s in segments] == [0, 37, 1046, 2055]


@pytest.mark.unit
def test_one_lost_segment_stalls_every_later_http2_stream() -> None:
    client, segments = h2_response_segments(BODIES)
    tcp = TcpReceiveBuffer()
    progress = EMPTY
    for index in (0, 1, 3):  # segment 2 (stream 3's body) is lost
        progress = feed_h2(client, tcp.receive(segments[index]), progress)

    assert progress.ended == {1}
    assert set(progress.body) == {1}
    # Stream 5's whole response sits in the receive buffer, but h2 never sees it.
    assert segments[3].streams == {5}
    assert tcp.buffered_out_of_order == len(segments[3].payload)


@pytest.mark.unit
def test_retransmission_releases_all_blocked_streams_in_one_read() -> None:
    client, segments = h2_response_segments(BODIES)
    tcp = TcpReceiveBuffer()
    progress = EMPTY
    for index in (0, 1, 3):
        progress = feed_h2(client, tcp.receive(segments[index]), progress)

    readable = tcp.receive(segments[2])
    assert readable == segments[2].payload + segments[3].payload
    progress = feed_h2(client, readable, progress)
    assert progress.ended == {1, 3, 5}
    assert dict(progress.body) == {1: BODIES[0], 3: BODIES[1], 5: BODIES[2]}


@pytest.mark.unit
def test_http2_cannot_skip_a_gap_because_frames_carry_no_offset() -> None:
    client, segments = h2_response_segments(BODIES)
    wire = b"".join(s.payload for s in segments)
    client.receive_data(wire[: segments[1].seq])
    resume = segments[1].seq + 500  # first byte after a hole in the middle of a DATA frame

    bogus, length = Frame.parse_frame_header(memoryview(wire[resume : resume + 9]))
    assert isinstance(bogus, ExtensionFrame)
    assert length == 0x616161  # three body bytes b"aaa" read as a 24-bit length
    assert client.receive_data(wire[resume:]) == []  # h2 waits for a 6 MiB frame
    with pytest.raises(h2.exceptions.FrameTooLargeError, match="length 6381921, max 16384"):
        client.receive_data(bytes(length))


@pytest.mark.unit
def test_quic_without_loss_completes_every_stream_after_one_way_delay() -> None:
    lab = QuicLab()
    ids = lab.open_requests(3)
    delivery = lab.respond(dict(zip(ids, BODIES, strict=True)), packet_per_stream=True)
    assert ids == [0, 4, 8]
    assert dict(delivery.completed_s) == {0: 0.025, 4: 0.025, 8: 0.025}
    assert delivery.datagram_streams == (frozenset({0}), frozenset({4}), frozenset({8}))


@pytest.mark.unit
def test_quic_loss_blocks_only_the_stream_in_the_lost_packet() -> None:
    lab = QuicLab()
    ids = lab.open_requests(3)
    delivery = lab.respond(
        dict(zip(ids, BODIES, strict=True)), packet_per_stream=True, drop=frozenset({1})
    )
    assert delivery.datagram_streams[1] == {4}
    assert delivery.completed_s[0] == 0.025
    assert delivery.completed_s[8] == 0.025  # sent after the lost packet, not blocked by it
    # The ACK for streams 0 and 8 arrives at 51 ms; RFC 9002 time threshold 9/8 x 51 ms = 57.375 ms
    # declares stream 4's packet lost; the retransmission lands 25 ms later.
    assert delivery.completed_s[4] == 0.082375
    assert dict(delivery.bodies) == dict(zip(ids, BODIES, strict=True))


@pytest.mark.unit
def test_quic_packet_carrying_several_streams_blocks_all_of_them() -> None:
    lab = QuicLab()
    ids = lab.open_requests(3)
    small = {sid: bytes([65 + i]) * 300 for i, sid in enumerate(ids)}
    delivery = lab.respond(small, packet_per_stream=False, drop=frozenset({0}))
    assert delivery.datagram_streams == (frozenset(ids),)  # one packet, three streams
    assert set(delivery.completed_s.values()) == {0.15625}  # nothing acked: probe timeout
    assert dict(delivery.bodies) == small


@pytest.mark.unit
def test_quic_tail_loss_waits_for_the_probe_timeout() -> None:
    lab = QuicLab()
    ids = lab.open_requests(3)
    delivery = lab.respond(
        dict(zip(ids, BODIES, strict=True)), packet_per_stream=True, drop=frozenset({2})
    )
    assert delivery.completed_s[0] == delivery.completed_s[4] == 0.025
    # No later packet is acknowledged, so only the PTO can recover it (RFC 9002 Section 6.2).
    assert delivery.completed_s[8] == 0.143313
    assert delivery.completed_s[8] > 0.082375


@pytest.mark.unit
def test_quic_exchange_with_every_datagram_lost_raises_instead_of_spinning() -> None:
    lab = QuicLab()
    (sid,) = lab.open_requests(1)
    with pytest.raises(RuntimeError, match="stalled"):
        lab.respond({sid: b"x"}, packet_per_stream=True, drop=frozenset(range(100_000)))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (Transport.TCP, {0: 75, 1: 75, 2: 75}),
        (Transport.QUIC, {0: 75, 1: 25, 2: 25}),
    ],
)
def test_completion_model_blast_radius(transport: Transport, expected: dict[int, int]) -> None:
    owners = round_robin(3, 2)
    assert owners == (0, 1, 2, 0, 1, 2)
    times = completion_times_ms(
        owners, frozenset({3}), transport=transport, one_way_ms=25, recovery_ms=50
    )
    assert times == expected


@pytest.mark.unit
def test_completion_model_loss_after_a_stream_finished_does_not_touch_it() -> None:
    owners = (0, 0, 1, 1)
    tcp = completion_times_ms(
        owners, frozenset({2}), transport=Transport.TCP, one_way_ms=25, recovery_ms=50
    )
    assert tcp == {0: 25, 1: 75}  # TCP blocks only bytes after the hole


@pytest.mark.unit
def test_stall_probability_for_ten_interleaved_streams() -> None:
    owners = round_robin(10, 10)
    tcp = sum(stall_probability(owners, s, 0.01, transport=Transport.TCP) for s in range(10))
    quic = sum(stall_probability(owners, s, 0.01, transport=Transport.QUIC) for s in range(10))
    assert tcp == pytest.approx(6.1687, abs=1e-4)
    assert quic == pytest.approx(0.9562, abs=1e-4)
    single = (0,) * 10
    assert stall_probability(single, 0, 0.01, transport=Transport.TCP) == pytest.approx(
        stall_probability(single, 0, 0.01, transport=Transport.QUIC)
    )


@pytest.mark.unit
@pytest.mark.parametrize("transport", [Transport.TCP, Transport.QUIC])
def test_stall_probability_accepts_both_loss_rate_bounds(transport: Transport) -> None:
    owners = round_robin(2, 3)
    assert stall_probability(owners, 0, 0.0, transport=transport) == 0.0
    assert stall_probability(owners, 0, 1.0, transport=transport) == 1.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("call", "match"),
    [
        (
            lambda: completion_times_ms(
                (0,), frozenset(), transport=Transport.TCP, one_way_ms=-1, recovery_ms=0
            ),
            "non-negative",
        ),
        (
            lambda: completion_times_ms(
                (0,), frozenset({1}), transport=Transport.TCP, one_way_ms=1, recovery_ms=0
            ),
            "out of range",
        ),
        (lambda: stall_probability((0,), 0, 1.5, transport=Transport.TCP), r"within \[0, 1\]"),
        (lambda: stall_probability((0,), 0, -0.1, transport=Transport.TCP), r"within \[0, 1\]"),
        (lambda: stall_probability((0,), 7, 0.1, transport=Transport.TCP), "owns no packets"),
    ],
)
def test_model_rejects_invalid_input(call: Callable[[], object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        call()
