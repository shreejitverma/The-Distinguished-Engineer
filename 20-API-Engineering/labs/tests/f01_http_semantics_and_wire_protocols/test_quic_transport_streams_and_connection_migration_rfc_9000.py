"""QUIC transport labs: varints, stream IDs, headers, loss isolation, and migration (RFC 9000)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aioquic._buffer import Buffer
from aioquic.buffer import UINT_VAR_MAX, encode_uint_var
from aioquic.quic.events import StreamDataReceived
from aioquic.quic.packet import QuicPacketType, pull_quic_header

from api_labs.f01_http_semantics_and_wire_protocols.quic_transport import (
    VARINT_MAX,
    QuicLab,
    StreamType,
    classify_stream_id,
    decode_varint,
    drain_events,
    encode_varint,
    frames_logged,
    nth_stream_id,
    parse_header,
)

if TYPE_CHECKING:
    from aioquic.quic.events import QuicEvent

NEW_ADDR = ("203.0.113.99", 61000)
ATTACKER_ADDR = ("203.0.113.66", 4444)


def connected_lab() -> QuicLab:
    lab = QuicLab()
    lab.handshake()
    drain_events(lab.client)
    drain_events(lab.server)
    return lab


def stream_bytes(events: list[QuicEvent], stream_id: int) -> bytes:
    return b"".join(
        e.data for e in events if isinstance(e, StreamDataReceived) and e.stream_id == stream_id
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wire_hex", "value"),
    [
        ("c2197c5eff14e88c", 151_288_809_941_952_652),
        ("9d7f3e7d", 494_878_333),
        ("7bbd", 15_293),
        ("25", 37),
    ],
)
def test_varint_matches_rfc_9000_appendix_a1_and_aioquic(wire_hex: str, value: int) -> None:
    wire = bytes.fromhex(wire_hex)
    assert encode_varint(value) == wire
    assert decode_varint(wire) == (value, len(wire))
    assert encode_uint_var(value) == wire


@pytest.mark.unit
def test_varint_length_boundaries_and_non_minimal_forms() -> None:
    assert [len(encode_varint(v)) for v in (63, 64, 16_383, 16_384)] == [1, 2, 2, 4]
    assert [len(encode_varint(v)) for v in (2**30 - 1, 2**30, 2**62 - 1)] == [4, 8, 8]
    assert VARINT_MAX == UINT_VAR_MAX == 2**62 - 1
    assert decode_varint(bytes.fromhex("4025")) == (37, 2)
    assert decode_varint(b"\x00\x25\x7b\xbd", offset=2) == (15_293, 4)
    with pytest.raises(ValueError, match="out of range"):
        encode_varint(2**62)
    with pytest.raises(ValueError, match="out of range"):
        encode_varint(-1)
    with pytest.raises(ValueError, match="truncated"):
        decode_varint(bytes.fromhex("9d7f3e"))
    with pytest.raises(ValueError, match="truncated"):
        decode_varint(b"")


@pytest.mark.unit
def test_stream_id_low_bits_encode_initiator_and_direction() -> None:
    assert [classify_stream_id(i) for i in range(4)] == [
        StreamType("client", "bidi"),
        StreamType("server", "bidi"),
        StreamType("client", "uni"),
        StreamType("server", "uni"),
    ]
    assert [nth_stream_id(n, initiator="client", unidirectional=False) for n in range(3)] == [
        0,
        4,
        8,
    ]
    assert nth_stream_id(2, initiator="server", unidirectional=True) == 11
    assert classify_stream_id(11) == StreamType("server", "uni")
    with pytest.raises(ValueError, match="out of range"):
        classify_stream_id(2**62)
    with pytest.raises(ValueError, match="non-negative"):
        nth_stream_id(-1, initiator="client", unidirectional=False)

    lab = connected_lab()
    assert lab.client.get_next_available_stream_id() == 0
    assert lab.client.get_next_available_stream_id(is_unidirectional=True) == 2
    assert lab.server.get_next_available_stream_id() == 1
    assert lab.server.get_next_available_stream_id(is_unidirectional=True) == 3
    lab.client.send_stream_data(0, b"x")
    assert lab.client.get_next_available_stream_id() == 4


@pytest.mark.unit
def test_client_initial_uses_long_header_then_short_header_after_handshake() -> None:
    lab = QuicLab()
    first = lab.client_send()[0]
    view = parse_header(first, short_dcid_length=8)
    assert (view.long_header, view.fixed_bit, view.version, view.long_type) == (
        True,
        True,
        0x00000001,
        "initial",
    )
    assert len(view.dcid) >= 8
    assert len(first) >= 1200
    reference = pull_quic_header(Buffer(data=first), host_cid_length=8)
    assert reference.packet_type is QuicPacketType.INITIAL
    assert (reference.destination_cid, reference.source_cid) == (view.dcid, view.scid)

    lab.pump()
    lab.client.send_stream_data(0, b"after handshake")
    one_rtt = lab.client_send()[0]
    short = parse_header(one_rtt, short_dcid_length=8)
    assert (short.long_header, short.fixed_bit, short.version, short.scid) == (
        False,
        True,
        None,
        None,
    )
    assert short.dcid == lab.server.host_cid
    assert short.dcid != view.dcid


@pytest.mark.unit
@pytest.mark.parametrize(
    ("datagram", "match"),
    [
        (b"", "empty datagram"),
        (b"\x40\x01\x02", "short header truncated"),
        (b"\xc0\x00\x00\x00", "long header truncated"),
        (b"\xc0\x00\x00\x00\x01\x00", "long header truncated"),
        (b"\xc0\x00\x00\x00\x01" + bytes([21]) + bytes(21) + b"\x00", "exceeds 20"),
        (b"\xc0\x00\x00\x00\x01\x08\x01\x02", "long header truncated"),
    ],
)
def test_parse_header_rejects_malformed_headers(datagram: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_header(datagram, short_dcid_length=8)


@pytest.mark.unit
def test_parse_header_applies_v1_rules_only_to_version_1() -> None:
    no_fixed_bit = parse_header(b"\x00" + bytes(8), short_dcid_length=8)
    assert (no_fixed_bit.long_header, no_fixed_bit.fixed_bit) == (False, False)

    # 0x?a?a?a?a versions are reserved to exercise version negotiation (RFC 9000 Section 15);
    # the invariants allow connection IDs up to 255 bytes (RFC 8999 Section 5.1).
    reserved = parse_header(
        b"\xc0" + bytes.fromhex("0a0a0a0a") + bytes([21]) + bytes(21) + b"\x00",
        short_dcid_length=8,
    )
    assert (reserved.version, reserved.long_type, reserved.dcid, reserved.scid) == (
        0x0A0A0A0A,
        None,
        bytes(21),
        b"",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("type_bits", "name"),
    [(0, "initial"), (1, "0rtt"), (2, "handshake"), (3, "retry")],
)
def test_parse_header_maps_v1_long_packet_types_per_rfc_9000_table_5(
    type_bits: int, name: str
) -> None:
    first = 0xC0 | (type_bits << 4)
    datagram = bytes([first]) + (1).to_bytes(4, "big") + b"\x04abcd\x02ef"
    view = parse_header(datagram, short_dcid_length=8)
    assert (view.long_type, view.dcid, view.scid) == (name, b"abcd", b"ef")


@pytest.mark.unit
def test_server_exists_only_after_the_first_client_datagram() -> None:
    lab = QuicLab()
    lab.advance(0.001)
    with pytest.raises(RuntimeError, match="has not received a datagram"):
        _ = lab.server
    first = lab.client_send()[0]
    odcid = parse_header(first, short_dcid_length=8).dcid
    assert lab.server.original_destination_connection_id == odcid


@pytest.mark.unit
def test_lost_packet_on_one_stream_does_not_block_another_stream() -> None:
    lab = connected_lab()
    lab.client.send_stream_data(0, b"A" * 100, end_stream=True)
    lab.client_send(drop=True)
    lab.client.send_stream_data(4, b"B" * 100, end_stream=True)
    lab.client_send()

    before_recovery = drain_events(lab.server)
    assert stream_bytes(before_recovery, 4) == b"B" * 100
    assert stream_bytes(before_recovery, 0) == b""

    recovered: list[QuicEvent] = []
    for _ in range(100):
        lab.advance(0.01)
        lab.server_send()
        lab.client_send()
        recovered += drain_events(lab.server)
        if stream_bytes(recovered, 0):
            break
    assert stream_bytes(recovered, 0) == b"A" * 100


@pytest.mark.unit
def test_server_first_flight_is_capped_at_three_times_client_bytes() -> None:
    lab = QuicLab(padding_names=200)
    lab.client_send()
    assert lab.bytes_from[lab.client_addr] == 1200
    lab.server_send()
    assert lab.bytes_to[lab.client_addr] == 3 * 1200
    assert lab.server.datagrams_to_send(lab.now) == []
    assert not any(type(e).__name__ == "HandshakeCompleted" for e in drain_events(lab.client))

    lab.client_send()
    assert lab.server_send()
    lab.pump()
    assert any(type(e).__name__ == "HandshakeCompleted" for e in drain_events(lab.client))

    small = QuicLab()
    small.client_send()
    small.server_send()
    assert small.bytes_to[small.client_addr] < 3 * small.bytes_from[small.client_addr]


@pytest.mark.unit
def test_migration_keeps_stream_state_and_rotates_connection_ids() -> None:
    lab = connected_lab()
    lab.client.send_stream_data(0, b"GET /a ")
    dcid_to_server_before = parse_header(lab.client_send()[0], short_dcid_length=8).dcid
    lab.server.send_stream_data(0, b"ack-1 ")
    dcid_to_client_before = parse_header(lab.server_send()[0][0], short_dcid_length=8).dcid
    lab.pump()
    drain_events(lab.server)
    drain_events(lab.client)

    lab.migrate_client(NEW_ADDR)
    lab.client.send_stream_data(0, b"POST /b", end_stream=True)
    after = parse_header(lab.client_send()[0], short_dcid_length=8)
    assert after.dcid != dcid_to_server_before
    assert stream_bytes(drain_events(lab.server), 0) == b"POST /b"
    assert lab.server.get_peer_address() == NEW_ADDR

    lab.server.send_stream_data(0, b"ack-2", end_stream=True)
    reply, addr = lab.server_send()[0]
    assert addr == NEW_ADDR
    assert parse_header(reply, short_dcid_length=8).dcid != dcid_to_client_before
    lab.pump()
    assert stream_bytes(drain_events(lab.client), 0) == b"ack-2"


@pytest.mark.unit
def test_nat_rebinding_without_new_cid_still_moves_the_path() -> None:
    lab = connected_lab()
    lab.client.send_stream_data(0, b"one ")
    dcid_before = parse_header(lab.client_send()[0], short_dcid_length=8).dcid
    lab.pump()

    lab.migrate_client(NEW_ADDR, rotate_cid=False)
    lab.client.send_stream_data(0, b"two", end_stream=True)
    assert parse_header(lab.client_send()[0], short_dcid_length=8).dcid == dcid_before
    assert lab.server.get_peer_address() == NEW_ADDR
    assert stream_bytes(drain_events(lab.server), 0) == b"one two"


@pytest.mark.unit
def test_reordered_older_packet_from_old_address_does_not_move_the_path_back() -> None:
    lab = connected_lab()
    old_addr = lab.client_addr
    lab.client.send_stream_data(0, b"older ")
    delayed = [data for data, _ in lab.client.datagrams_to_send(lab.now)]

    lab.migrate_client(NEW_ADDR, rotate_cid=False)
    lab.client.send_stream_data(0, b"newer", end_stream=True)
    lab.client_send()
    assert lab.server.get_peer_address() == NEW_ADDR

    for data in delayed:
        lab.server.receive_datagram(data, old_addr, lab.now)
    assert lab.server.get_peer_address() == NEW_ADDR
    assert stream_bytes(drain_events(lab.server), 0) == b"older newer"


@pytest.mark.unit
def test_unvalidated_new_path_is_limited_until_path_response() -> None:
    lab = connected_lab()
    lab.client.send_stream_data(0, b"GET /big")
    lab.pump()

    lab.migrate_client(NEW_ADDR)
    lab.client.send_stream_data(0, b" again")
    lab.client_send()
    lab.server.send_stream_data(0, b"R" * 30_000, end_stream=True)
    lab.server_send()
    assert lab.bytes_to[NEW_ADDR] <= 3 * lab.bytes_from[NEW_ADDR]
    assert lab.bytes_to[NEW_ADDR] < 1000
    assert "path_challenge" in frames_logged(lab.server_log, "sent")

    lab.pump()
    assert "path_response" in frames_logged(lab.client_log, "sent")
    assert lab.bytes_to[NEW_ADDR] > 3 * lab.bytes_from[NEW_ADDR]
    assert stream_bytes(drain_events(lab.client), 0) == b"R" * 30_000


@pytest.mark.unit
def test_spoofed_copy_migrates_until_higher_packet_from_real_address() -> None:
    lab = connected_lab()
    legit = lab.client_addr
    lab.client.send_stream_data(0, b"hello")
    lab.pump()
    drain_events(lab.server)

    lab.client.send_stream_data(0, b" world")
    copied = [data for data, _ in lab.client.datagrams_to_send(lab.now)]
    for data in copied:
        lab.server.receive_datagram(data, ATTACKER_ADDR, lab.now)
    to_attacker = lab.server.datagrams_to_send(lab.now)
    assert lab.server.get_peer_address() == ATTACKER_ADDR
    assert {addr for _, addr in to_attacker} == {ATTACKER_ADDR}
    assert sum(len(d) for d, _ in to_attacker) <= 3 * sum(len(d) for d in copied)

    for data in copied:
        lab.server.receive_datagram(data, legit, lab.now)
    assert lab.server.get_peer_address() == ATTACKER_ADDR

    lab.client.send_stream_data(0, b"!", end_stream=True)
    lab.client_send()
    assert lab.server.get_peer_address() == legit
    lab.pump()
    assert stream_bytes(drain_events(lab.server), 0) == b" world!"


@pytest.mark.unit
def test_datagram_with_unknown_connection_id_is_dropped_silently() -> None:
    lab = connected_lab()
    stranger = bytes([0x40]) + bytes.fromhex("0123456789abcdef") + bytes(30)
    assert stranger[1:9] != lab.server.host_cid
    lab.server.receive_datagram(stranger, lab.client_addr, lab.now)
    assert lab.server.datagrams_to_send(lab.now) == []
    trace = lab.server_log.to_dict()["traces"][0]["events"]
    triggers = [e["data"]["trigger"] for e in trace if e["name"] == "transport:packet_dropped"]
    assert triggers[-1] == "unknown_connection_id"


@pytest.mark.unit
def test_fake_network_delivers_only_datagrams_addressed_to_the_client() -> None:
    lab = connected_lab()
    lab.client.send_stream_data(0, b"hello")
    lab.pump()
    received_before = len(frames_logged(lab.client_log, "received"))

    lab.client.send_stream_data(0, b" world")
    copied = [data for data, _ in lab.client.datagrams_to_send(lab.now)]
    for data in copied:
        lab.server.receive_datagram(data, ATTACKER_ADDR, lab.now)
    sent = lab.server_send()
    assert sent
    assert {addr for _, addr in sent} == {ATTACKER_ADDR}
    assert lab.bytes_to[ATTACKER_ADDR] > 0
    assert len(frames_logged(lab.client_log, "received")) == received_before
