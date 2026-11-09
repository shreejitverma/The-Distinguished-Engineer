"""Unit tests for RFC 9218 Extensible Priorities versus the deprecated RFC 7540 priority tree."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import httpx
import pytest
from hyperframe.frame import ExtensionFrame, SettingsFrame
from priority.priority import PriorityTree, TooManyStreamsError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.extensible_priorities import (
    H2ProtocolError,
    Priority,
    PriorityScheduler,
    SFParseError,
    decode_h2_priority_update,
    encode_h2_priority_update,
    merge_response_priority,
    parse_priority,
    parse_sf_dictionary,
    serialize_priority,
)

if TYPE_CHECKING:
    from starlette.requests import Request

H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"


def _h2_server() -> h2.connection.H2Connection:
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    conn.initiate_connection()
    conn.clear_outbound_data_buffer()
    return conn


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_value", "expected"),
    [
        (None, Priority(3, False)),
        ("", Priority(3, False)),
        ("u=0", Priority(0, False)),
        ("u=5, i", Priority(5, True)),
        ("i", Priority(3, True)),
        ("u=7", Priority(7, False)),
    ],
)
def test_parse_priority_rfc_9218_examples_and_defaults(
    field_value: str | None, expected: Priority
) -> None:
    assert parse_priority(field_value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_value", "expected"),
    [
        ("u=8", Priority(3, False)),
        ("u=-1", Priority(3, False)),
        ("u=?1", Priority(3, False)),
        ("u=1.0", Priority(3, False)),
        ('u="0"', Priority(3, False)),
        ("i=1", Priority(3, False)),
        ("i=?0", Priority(3, False)),
        ('u=2, x-vendor="abc", i', Priority(2, True)),
        ("u=1;foo=bar, i;q", Priority(1, True)),
        ("u=(1 2), i", Priority(3, True)),
    ],
)
def test_unknown_out_of_range_and_mistyped_parameters_are_ignored(
    field_value: str, expected: Priority
) -> None:
    assert parse_priority(field_value) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_value",
    ["u=0, i,", "U=0", "u=0 i", "u=0,,i", "u=0, i=?2", "u=1\u00e9", "u=0;", "u=", "u=0, 1"],
)
def test_structured_field_parse_failure_ignores_the_whole_field(field_value: str) -> None:
    assert parse_priority(field_value) == Priority(3, False)


@pytest.mark.unit
def test_duplicate_dictionary_key_last_one_wins() -> None:
    assert parse_priority("u=1, i, u=6") == Priority(6, True)


@pytest.mark.unit
def test_sf_dictionary_parser_covers_every_bare_item_type() -> None:
    parsed = parse_sf_dictionary(
        'a=-12, b=4.5, c="q\\"x", d=tok/en:1, e=:aGk=:, f=?0, g=@1700000000, '
        'h=%"caf%c3%a9", j=(1 "s");p, k;w=1'
    )
    assert parsed == {
        "a": (-12, {}),
        "b": (Decimal("4.5"), {}),
        "c": ('q"x', {}),
        "d": ("tok/en:1", {}),
        "e": (b"hi", {}),
        "f": (False, {}),
        "g": (1700000000, {}),
        "h": ("caf\u00e9", {}),
        "j": ([(1, {}), ("s", {})], {"p": True}),
        "k": (True, {"w": 1}),
    }
    bad_values = [
        "a=1234567890123456",
        "a=1234567890123.5",
        "a=123456789012.1234",
        "a=1.2345",
        "a=1.",
        "a=-",
        'a="\\n"',
        'a="\x01"',
        'a="open',
        "a=:!:",
        "a=:abc",
        "a=:a:",
        "a=@1.5",
        "a=%x",
        'a=%"\x01"',
        'a=%"%C3%A9"',
        'a=%"%ff"',
        "a=(1,2)",
        'a=(1"s")',
        'a=(1;p=2"s")',
        "a=1\u00e9",
    ]
    for bad in bad_values:
        with pytest.raises(SFParseError):
            parse_sf_dictionary(bad)


@pytest.mark.unit
def test_sf_number_length_limits_accept_the_exact_boundary() -> None:
    # RFC 9651 Section 4.2.4: at most 15 integer digits, at most 12 digits before the dot.
    assert parse_sf_dictionary("a=123456789012345, b=-123456789012.123") == {
        "a": (123456789012345, {}),
        "b": (Decimal("-123456789012.123"), {}),
    }


@pytest.mark.unit
def test_serialize_omits_defaults_and_roundtrips() -> None:
    assert serialize_priority(Priority()) == ""
    assert serialize_priority(Priority(urgency=5, incremental=True)) == "u=5, i"
    assert serialize_priority(Priority(urgency=3, incremental=True)) == "i"
    for urgency in range(8):
        for incremental in (False, True):
            original = Priority(urgency, incremental)
            assert parse_priority(serialize_priority(original)) == original


@pytest.mark.unit
def test_merge_keeps_client_parameters_the_server_omits() -> None:
    client = parse_priority("u=5, i")
    assert merge_response_priority(client, "u=1") == Priority(1, True)
    assert merge_response_priority(client, None) == Priority(5, True)
    assert merge_response_priority(client, "i=?0") == Priority(5, False)
    assert merge_response_priority(client, "u=9") == Priority(5, True)


@pytest.mark.unit
def test_h2_priority_update_wire_layout() -> None:
    frame = encode_h2_priority_update(5, "u=0")
    assert frame == bytes.fromhex("00000710000000000000000005") + b"u=0"
    assert decode_h2_priority_update(frame) == (5, "u=0")
    with pytest.raises(ValueError, match="prioritized stream ID"):
        encode_h2_priority_update(0, "u=0")
    # Stream identifiers are 31 bits: 2**31 - 1 is the largest, 2**31 would set the Reserved bit.
    largest = 2**31 - 1
    assert decode_h2_priority_update(encode_h2_priority_update(largest, "u=0")) == (largest, "u=0")
    with pytest.raises(ValueError, match="prioritized stream ID"):
        encode_h2_priority_update(2**31, "u=0")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("frame", "match"),
    [
        (bytes.fromhex("00000410000000000100000003"), "must be sent on stream 0"),
        (bytes.fromhex("00000410000000000000000000"), "stream ID 0"),
        (bytes.fromhex("0000050200000000030000000010"), "not a well-formed"),
        (bytes.fromhex("00000910000000000000000003") + b"u=0", "not a well-formed"),
        (bytes.fromhex("00000510000000000000000003ff"), "not ASCII"),
        (bytes.fromhex("000003100000000000000000"), "shorter than"),
    ],
)
def test_h2_priority_update_validation(frame: bytes, match: str) -> None:
    with pytest.raises(H2ProtocolError, match=match):
        decode_h2_priority_update(frame)


@pytest.mark.unit
def test_h2_priority_update_ignores_reserved_bits_on_receipt() -> None:
    # RFC 9113 Section 4.1: the Reserved bit MUST be ignored when receiving; RFC 9218
    # Section 7.1, Figure 1 puts one before both the Stream Identifier and the target ID.
    frame = bytes.fromhex("00000710008000000080000005") + b"u=0"
    assert decode_h2_priority_update(frame) == (5, "u=0")


@pytest.mark.unit
def test_h2_4_4_1_surfaces_priority_update_as_an_unknown_frame() -> None:
    server = _h2_server()
    settings = SettingsFrame(0).serialize()
    events = server.receive_data(H2_PREFACE + settings + encode_h2_priority_update(1, "u=0"))
    unknown = [e for e in events if isinstance(e, h2.events.UnknownFrameReceived)]
    assert len(unknown) == 1
    frame = unknown[0].frame
    assert isinstance(frame, ExtensionFrame)
    assert frame.type == 0x10
    assert frame.body == b"\x00\x00\x00\x01u=0"


@pytest.mark.unit
def test_h2_4_4_1_passes_settings_no_rfc7540_priorities_through_unvalidated() -> None:
    server = _h2_server()
    events = server.receive_data(H2_PREFACE + SettingsFrame(0, settings={0x9: 1}).serialize())
    changed = next(e for e in events if isinstance(e, h2.events.RemoteSettingsChanged))
    assert changed.changed_settings[0x9].new_value == 1

    # RFC 9218 Section 2.1 says any value other than 0 or 1 MUST be a PROTOCOL_ERROR;
    # h2 4.4.1 does not know setting 0x9, so the application has to enforce it.
    other = _h2_server()
    events = other.receive_data(H2_PREFACE + SettingsFrame(0, settings={0x9: 2}).serialize())
    changed = next(e for e in events if isinstance(e, h2.events.RemoteSettingsChanged))
    assert changed.changed_settings[0x9].new_value == 2
    assert not any(isinstance(e, h2.events.ConnectionTerminated) for e in events)


@pytest.mark.unit
def test_scheduler_sends_lower_urgency_first_then_ascending_stream_id() -> None:
    sched = PriorityScheduler()
    sched.open(1, None, chunks=2)
    sched.open(3, "u=0", chunks=2)
    sched.open(5, None, chunks=1)
    sched.open(7, "u=7", chunks=1)
    assert sched.drain() == [3, 3, 1, 1, 5, 7]


@pytest.mark.unit
def test_scheduler_interleaves_incremental_and_serializes_non_incremental() -> None:
    incremental = PriorityScheduler()
    sequential = PriorityScheduler()
    for sid in (1, 3, 5):
        incremental.open(sid, "u=5, i", chunks=2)
        sequential.open(sid, "u=5", chunks=2)
    assert incremental.drain() == [1, 3, 5, 1, 3, 5]
    assert sequential.drain() == [1, 1, 3, 3, 5, 5]


@pytest.mark.unit
def test_same_urgency_non_incremental_starves_later_incremental() -> None:
    # RFC 9218 Section 10, challenge 1: a large non-incremental response delays a small
    # incremental one at the same urgency. This scheduler accepts that trade-off.
    sched = PriorityScheduler()
    sched.open(1, None, chunks=4)
    sched.open(3, "i", chunks=1)
    assert sched.drain() == [1, 1, 1, 1, 3]


@pytest.mark.unit
def test_priority_update_reprioritizes_an_in_flight_prefetch() -> None:
    sched = PriorityScheduler()
    sched.open(1, "u=7", chunks=3)
    sched.open(3, None, chunks=3)
    assert sched.next_chunk() == 3
    sched.priority_update(1, "u=0")
    assert sched.drain() == [1, 1, 1, 3, 3]


@pytest.mark.unit
def test_priority_update_for_idle_stream_is_buffered_and_overrides_header() -> None:
    sched = PriorityScheduler()
    sched.priority_update(9, "u=1")
    sched.priority_update(9, "u=0, i")
    assert sched.open(9, "u=6", chunks=1) == Priority(0, True)
    assert sched.open(11, "u=6", chunks=1) == Priority(6, False)
    assert sched.drain() == [9, 11]


@pytest.mark.unit
def test_priority_update_for_closed_stream_is_discarded_not_buffered() -> None:
    sched = PriorityScheduler(max_concurrent_streams=1)
    sched.open(1, None, chunks=1)
    assert sched.drain() == [1]
    sched.priority_update(1, "u=0")
    sched.priority_update(3, "u=1")
    assert sched.open(3, None, chunks=1) == Priority(1, False)


@pytest.mark.unit
def test_priority_update_enforces_max_concurrent_streams_and_parse_errors() -> None:
    sched = PriorityScheduler(max_concurrent_streams=2)
    sched.open(1, None, chunks=1)
    sched.priority_update(3, "u=1")
    sched.priority_update(3, "u=2")
    with pytest.raises(H2ProtocolError, match="exceed the limit"):
        sched.priority_update(5, "u=1")
    with pytest.raises(H2ProtocolError, match="unparseable"):
        sched.priority_update(1, "u=0,")


@pytest.mark.unit
def test_rfc7540_weights_split_bandwidth_between_siblings() -> None:
    tree = PriorityTree()
    tree.insert_stream(1, weight=4)
    tree.insert_stream(3, weight=12)
    counts = Counter(next(tree) for _ in range(160))
    assert counts == Counter({1: 40, 3: 120})


@pytest.mark.unit
def test_rfc7540_exclusive_insert_reparents_existing_children() -> None:
    tree = PriorityTree()
    tree.insert_stream(1)
    tree.insert_stream(3)
    tree.insert_stream(5, exclusive=True)
    assert [next(tree) for _ in range(3)] == [5, 5, 5]
    tree.block(5)
    assert {next(tree) for _ in range(4)} == {1, 3}


@pytest.mark.unit
def test_priority_2_0_0_remove_stream_keeps_child_weights() -> None:
    # RFC 7540 Section 5.3.4: the removed stream's weight is redistributed to its children.
    # priority 2.0.0 instead reattaches children with their own weights.
    tree = PriorityTree()
    tree.insert_stream(1, weight=16)
    tree.insert_stream(3, weight=16)
    tree.insert_stream(5, depends_on=1, weight=16)
    tree.insert_stream(7, depends_on=1, weight=16)
    tree.block(1)
    tree.block(7)
    before = Counter(next(tree) for _ in range(100))
    assert before == Counter({5: 50, 3: 50})
    tree.remove_stream(1)
    after = Counter(next(tree) for _ in range(100))
    assert after == Counter({5: 50, 3: 50})


@pytest.mark.unit
def test_priority_tree_default_cap_counts_idle_grouping_nodes() -> None:
    tree = PriorityTree()
    for sid in range(1, 2 * 999, 2):
        tree.insert_stream(sid)
    with pytest.raises(TooManyStreamsError):
        tree.insert_stream(2001)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_priority_is_an_ordinary_end_to_end_header_through_asgi() -> None:
    async def font(request: Request) -> PlainTextResponse:
        client_view = parse_priority(request.headers.get("priority"))
        return PlainTextResponse(
            f"client u={client_view.urgency}",
            headers={"Priority": serialize_priority(Priority(urgency=1))},
        )

    app = Starlette(routes=[Route("/font.woff2", font)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/font.woff2", headers={"Priority": "u=5, i"})
    assert resp.text == "client u=5"
    merged = merge_response_priority(Priority(5, True), resp.headers["priority"])
    assert merged == Priority(1, True)
