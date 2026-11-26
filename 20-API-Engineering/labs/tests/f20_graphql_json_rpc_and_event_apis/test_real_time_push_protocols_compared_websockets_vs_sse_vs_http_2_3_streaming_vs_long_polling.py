"""Tests for the real-time push transport lab."""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import httpx
import pytest
import wsproto
import wsproto.events as wsevents
from hpack import Encoder
from hyperframe.frame import HeadersFrame
from starlette.testclient import TestClient, WebSocketDenialResponse
from wsproto.frame_protocol import FrameProtocol
from wsproto.utilities import RemoteProtocolError

from api_labs.f20_graphql_json_rpc_and_event_apis.real_time_push_protocols_compared_websockets_vs_sse_vs_http_2_3_streaming_vs_long_polling import (  # noqa: E501
    EventJournal,
    ExtendedConnectUnavailable,
    JournalEntry,
    Opcode,
    Reset,
    SlowConsumerPolicy,
    SseParser,
    Subscriber,
    WsProtocolError,
    apply_mask,
    build_app,
    decode_ws_frame,
    encode_ws_frame,
    follow,
    format_sse,
    h1_chunk_overhead,
    h2_data_overhead,
    h2_sse_streams,
    h2_websocket_over_extended_connect,
    h3_data_overhead,
    long_poll_overhead,
    origin_allowed,
    reconnect_delay,
    safe_heartbeat_interval,
    sse_overhead,
    websocket_accept,
    ws_header_size,
    ws_overhead,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.applications import Starlette
    from starlette.types import Message

ORIGIN = "https://app.example.com"
ALLOWED = frozenset({ORIGIN})
EVENTS = ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class Streamed:
    status: int
    headers: dict[str, str]
    body: bytes


async def read_stream(
    app: Starlette,
    path: str,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    stop: Callable[[bytes], bool] | None = None,
    window_s: float = 5.0,
) -> Streamed:
    """Drive one in-process ASGI request, then send `http.disconnect` like a dropped client."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": list(headers),
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
        "extensions": {},
    }
    disconnect, satisfied = asyncio.Event(), asyncio.Event()
    body = bytearray()
    head: dict[str, Any] = {}

    async def receive() -> Message:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            head.update(message)
            return
        body.extend(message.get("body", b""))
        if stop is not None and stop(bytes(body)):
            satisfied.set()

    task = asyncio.create_task(app(scope, receive, send))
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(satisfied.wait(), window_s)
    disconnect.set()
    await asyncio.wait_for(task, 5)
    decoded = {k.decode().lower(): v.decode() for k, v in head["headers"]}
    return Streamed(head["status"], decoded, bytes(body))


def parse_all(stream: bytes) -> list[str]:
    return [event.data for event in SseParser().feed(stream)]


def sse_count(stream: bytes) -> int:
    return len(SseParser().feed(stream))


async def journal_with(*items: str, capacity: int = 16) -> EventJournal:
    journal = EventJournal(capacity)
    for item in items:
        await journal.publish(item)
    return journal


def as_text(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


def at_least(n: int) -> Callable[[bytes], bool]:
    return lambda stream: sse_count(stream) >= n


def varint_len(n: int) -> int:
    return 1 if n < 64 else 2 if n < 16384 else 4 if n < 2**30 else 8


# ---------------------------------------------------------------------------
# WebSocket framing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_accept_key_matches_the_rfc_6455_example() -> None:
    assert websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.unit
def test_rfc_6455_section_5_7_example_frames_are_reproduced_byte_for_byte() -> None:
    assert encode_ws_frame(Opcode.TEXT, b"Hello") == bytes.fromhex("810548656c6c6f")
    masked = encode_ws_frame(Opcode.TEXT, b"Hello", mask_key=bytes.fromhex("37fa213d"))
    assert masked == bytes.fromhex("818537fa213d7f9f4d5158")
    assert encode_ws_frame(Opcode.TEXT, b"Hel", fin=False) == bytes.fromhex("010348656c")
    assert encode_ws_frame(Opcode.CONTINUATION, b"lo") == bytes.fromhex(
        "8002 6c6f".replace(" ", "")
    )
    assert encode_ws_frame(Opcode.PING, b"Hello") == bytes.fromhex("890548656c6c6f")
    assert encode_ws_frame(Opcode.BINARY, bytes(256))[:4] == bytes.fromhex("827e0100")
    assert encode_ws_frame(Opcode.BINARY, bytes(65536))[:10] == bytes.fromhex(
        "827f0000000000010000"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload_len", "header"),
    [(0, 2), (125, 2), (126, 4), (65535, 4), (65536, 10)],
)
def test_header_size_steps_at_the_length_encoding_boundaries(payload_len: int, header: int) -> None:
    assert ws_header_size(payload_len, masked=False) == header
    assert ws_header_size(payload_len, masked=True) == header + 4
    frame = encode_ws_frame(Opcode.BINARY, bytes(payload_len))
    assert len(frame) == payload_len + header


@pytest.mark.unit
def test_masking_adds_four_bytes_hides_the_payload_and_round_trips() -> None:
    key = bytes.fromhex("a1b2c3d4")
    payload = b"GET / HTTP/1.1\r\n"
    frame = encode_ws_frame(Opcode.TEXT, payload, mask_key=key)
    assert len(frame) == len(encode_ws_frame(Opcode.TEXT, payload)) + 4
    assert payload not in frame
    assert apply_mask(apply_mask(payload, key), key) == payload
    decoded = decode_ws_frame(frame)
    assert decoded is not None
    assert decoded.masked
    assert decoded.payload == payload
    assert decoded.wire_size == len(frame)


@pytest.mark.unit
@pytest.mark.parametrize("size", [0, 1, 125, 126, 1000, 65535, 65536])
def test_encoder_agrees_with_wsproto_in_both_directions(size: int) -> None:
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    server_side = FrameProtocol(client=False, extensions=[])
    assert bytes(server_side.send_data(payload, fin=True)) == encode_ws_frame(
        Opcode.BINARY, payload
    )
    client_frame = bytes(FrameProtocol(client=True, extensions=[]).send_data(payload, fin=True))
    ours = decode_ws_frame(client_frame)
    assert ours is not None
    assert ours.masked
    assert ours.payload == payload
    receiver = FrameProtocol(client=False, extensions=[])
    receiver.receive_bytes(encode_ws_frame(Opcode.BINARY, payload, mask_key=b"\x01\x02\x03\x04"))
    frames = list(receiver.received_frames())
    assert [f.payload for f in frames] == [payload]


@pytest.mark.unit
def test_decoder_needs_every_byte_before_it_reports_a_frame() -> None:
    frame = encode_ws_frame(Opcode.TEXT, b"x" * 300, mask_key=b"\x09\x08\x07\x06")
    assert all(decode_ws_frame(frame[:cut]) is None for cut in range(len(frame)))
    assert decode_ws_frame(frame) is not None


@pytest.mark.unit
def test_control_frames_are_limited_to_125_bytes_and_cannot_fragment() -> None:
    with pytest.raises(WsProtocolError):
        encode_ws_frame(Opcode.PING, bytes(126))
    with pytest.raises(WsProtocolError):
        encode_ws_frame(Opcode.CLOSE, b"", fin=False)
    with pytest.raises(WsProtocolError):
        decode_ws_frame(bytes([0x89, 126, 0, 126]) + bytes(126))


@pytest.mark.unit
def test_decoder_rejects_non_minimal_lengths_and_reserved_bits() -> None:
    with pytest.raises(WsProtocolError, match="minimally"):
        decode_ws_frame(bytes([0x82, 126, 0, 124]) + bytes(124))
    with pytest.raises(WsProtocolError, match="RSV"):
        decode_ws_frame(bytes([0xC1, 0]))


# ---------------------------------------------------------------------------
# Handshake through hop-by-hop-stripping intermediaries, and HTTP/2
# ---------------------------------------------------------------------------


def _handshake_request() -> bytes:
    client = wsproto.WSConnection(wsproto.ConnectionType.CLIENT)
    return client.send(wsevents.Request(host="t", target="/chat"))


@pytest.mark.unit
def test_wsproto_handshake_completes_with_the_rfc_accept_value() -> None:
    request = _handshake_request()
    key = next(
        line.split(b": ")[1].decode()
        for line in request.split(b"\r\n")
        if line.lower().startswith(b"sec-websocket-key")
    )
    server = wsproto.WSConnection(wsproto.ConnectionType.SERVER)
    server.receive_data(request)
    assert isinstance(next(server.events()), wsevents.Request)
    response = server.send(wsevents.AcceptConnection())
    assert response.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert f"Sec-WebSocket-Accept: {websocket_accept(key)}".encode() in response


@pytest.mark.unit
def test_a_proxy_that_drops_the_connection_upgrade_option_breaks_the_handshake() -> None:
    request = _handshake_request()
    stripped = request.replace(b"Connection: Upgrade", b"Connection: keep-alive")
    assert stripped != request
    server = wsproto.WSConnection(wsproto.ConnectionType.SERVER)
    with pytest.raises(RemoteProtocolError, match="Connection: Upgrade"):
        server.receive_data(stripped)


def _h2_connected_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    config = h2.config.H2Configuration
    client = h2.connection.H2Connection(config(client_side=True, header_encoding="utf-8"))
    server = h2.connection.H2Connection(config(client_side=False, header_encoding="utf-8"))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    return client, server


@pytest.mark.unit
def test_h2_silently_strips_the_legacy_upgrade_headers_and_rejects_them_inbound() -> None:
    client, server = _h2_connected_pair()
    base = [(":method", "GET"), (":scheme", "https"), (":authority", "t"), (":path", "/chat")]
    client.send_headers(
        1, [*base, ("connection", "upgrade"), ("upgrade", "websocket"), ("sec-websocket-key", "k")]
    )
    events = server.receive_data(client.data_to_send())
    request = next(e for e in events if isinstance(e, h2.events.RequestReceived))
    names = [as_text(name) for name, _ in request.headers or []]
    assert "connection" not in names
    assert "upgrade" not in names
    assert "sec-websocket-key" in names

    frame = HeadersFrame(3)
    frame.data = Encoder().encode(
        [(k.encode(), v.encode()) for k, v in [*base, ("connection", "upgrade")]]
    )
    frame.flags.add("END_HEADERS")
    frame.flags.add("END_STREAM")
    with pytest.raises(h2.exceptions.ProtocolError, match="Connection-specific"):
        server.receive_data(frame.serialize())


@pytest.mark.unit
def test_websocket_over_extended_connect_uses_a_pseudo_header_and_data_frames() -> None:
    result = h2_websocket_over_extended_connect(server_enables=True)
    headers = dict(result.request_headers)
    assert headers[":method"] == "CONNECT"
    assert headers[":protocol"] == "websocket"
    assert headers[":path"] == "/chat"
    assert not {"connection", "upgrade", "sec-websocket-key"} & headers.keys()
    assert result.response_status == "200"
    assert result.ws_payload == b"hi"


@pytest.mark.unit
def test_extended_connect_is_refused_until_the_server_opts_in() -> None:
    with pytest.raises(ExtendedConnectUnavailable):
        h2_websocket_over_extended_connect(server_enables=False)


@pytest.mark.unit
def test_each_open_sse_stream_holds_one_h2_concurrency_slot_until_it_ends() -> None:
    slots = h2_sse_streams(server_max_concurrent=3, attempts=5)
    assert slots.accepted == 3
    assert slots.refused_at == 4
    roomy = h2_sse_streams(server_max_concurrent=100, attempts=5)
    assert roomy.accepted == 5
    assert roomy.refused_at is None


# ---------------------------------------------------------------------------
# SSE wire format and parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_sse_emits_the_documented_field_lines() -> None:
    assert format_sse("hi", event="tick", id="7", retry=3000) == (
        b"event: tick\nid: 7\nretry: 3000\ndata: hi\n\n"
    )
    assert format_sse(comment="keepalive") == b": keepalive\n\n"
    assert format_sse("a\nb") == b"data: a\ndata: b\n\n"


@pytest.mark.unit
@pytest.mark.parametrize("bad", [{"id": "a\nb"}, {"id": "a\0b"}, {"event": "x\ry"}, {"retry": -1}])
def test_format_sse_rejects_values_that_would_corrupt_the_stream(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"must|retry"):
        format_sse("x", **bad)


@pytest.mark.unit
@pytest.mark.parametrize("data", ["", "a", "a\nb", "a\n", "\n", " lead", "trail ", "\u65e5\u672c\u8a9e \xe9"])
def test_data_round_trips_including_empty_and_trailing_newline(data: str) -> None:
    assert parse_all(format_sse(data)) == [data]


@pytest.mark.unit
def test_a_bare_cr_inside_data_comes_back_as_lf() -> None:
    assert parse_all(format_sse("a\rb")) == ["a\nb"]


@pytest.mark.unit
def test_whatwg_example_streams_dispatch_exactly_the_events_the_standard_lists() -> None:
    parser = SseParser()
    first = parser.feed(
        b": test stream\n\ndata: first event\nid: 1\n\n"
        b"data:second event\nid\n\ndata:  third event\n\n"
    )
    assert [(e.data, e.last_event_id) for e in first] == [
        ("first event", "1"),
        ("second event", ""),
        (" third event", ""),
    ]
    assert parse_all(b"data\n\ndata\ndata\n\ndata:") == ["", "\n"]
    assert parse_all(b"data:test\n\ndata: test\n\n") == ["test", "test"]


@pytest.mark.unit
def test_an_event_is_not_dispatched_until_its_blank_line_arrives() -> None:
    parser = SseParser()
    assert parser.feed(b"id: 5\ndata: half") == []
    assert parser.last_event_id == ""
    assert [e.data for e in parser.feed(b"\n\n")] == ["half"]
    assert parser.last_event_id == "5"


@pytest.mark.unit
@pytest.mark.parametrize("eol", [b"\n", b"\r\n", b"\r"], ids=["lf", "crlf", "cr"])
def test_every_chunking_of_the_stream_parses_the_same(eol: bytes) -> None:
    stream = eol.join([b"id: 1", "data: caf\xe9 \u65e5\u672c".encode(), b"", b"data: two", b"", b""])
    whole = SseParser().feed(stream)
    assert [e.data for e in whole] == ["caf\xe9 \u65e5\u672c", "two"]
    for step in (1, 2, 3, 5):
        parser = SseParser()
        pieces = [stream[i : i + step] for i in range(0, len(stream), step)]
        assert [event for piece in pieces for event in parser.feed(piece)] == whole


@pytest.mark.unit
def test_only_a_leading_bom_is_stripped() -> None:
    assert parse_all(b"\xef\xbb\xbfdata: x\n\n") == ["x"]
    assert parse_all(b"data: a\n\n\xef\xbb\xbfdata: b\n\n") == ["a"]


@pytest.mark.unit
def test_nul_ids_and_non_numeric_retry_values_are_ignored() -> None:
    parser = SseParser()
    parser.feed(b"retry: 1500\nretry: 15x\nretry: -1\nretry: \nid: a\x00b\ndata: x\n\n")
    assert parser.reconnection_time_ms == 1500
    assert parser.last_event_id == ""


@pytest.mark.unit
def test_last_event_id_survives_events_without_an_id_field() -> None:
    parser = SseParser()
    events = parser.feed(b"id: 9\ndata: a\n\ndata: b\n\n")
    assert [e.last_event_id for e in events] == ["9", "9"]


# ---------------------------------------------------------------------------
# Journal, replay, and reconnection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_journal_replays_after_the_cursor_and_flags_an_evicted_cursor() -> None:
    journal = await journal_with("a", "b", "c", "d", "e", capacity=3)
    replay = journal.since(3)
    assert [e.id for e in replay.entries] == [4, 5]
    assert not replay.gap
    stale = journal.since(1)
    assert stale.gap
    assert [e.id for e in stale.entries] == [3, 4, 5]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cursor_from_a_restarted_server_is_a_gap_not_an_endless_wait() -> None:
    journal = await journal_with("fresh")
    assert journal.since(40).gap
    replay = await asyncio.wait_for(journal.wait_after(40, None), 1)
    assert replay.gap


@pytest.mark.unit
@pytest.mark.asyncio
async def test_follow_yields_a_reset_marker_before_replaying_after_a_gap() -> None:
    journal = await journal_with("a", "b", "c", "d", capacity=2)
    stream = follow(journal, 1)
    items = [await anext(stream) for _ in range(3)]
    await stream.aclose()
    assert items == [Reset(4), JournalEntry(3, "c"), JournalEntry(4, "d")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sse_response_headers_disable_caching_and_proxy_buffering() -> None:
    journal = await journal_with("a")
    app = build_app(journal, allowed_origins=ALLOWED, ping_interval=0)
    got = await read_stream(app, "/sse", stop=lambda b: sse_count(b) >= 1)
    assert got.status == 200
    assert got.headers["content-type"].startswith("text/event-stream")
    assert got.headers["cache-control"] == "no-store"
    assert got.headers["x-accel-buffering"] == "no"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cut_at_every_byte_then_last_event_id_loses_and_repeats_nothing() -> None:
    journal = await journal_with(*EVENTS)
    app = build_app(journal, allowed_origins=ALLOWED, ping_interval=0)
    reference = (await read_stream(app, "/sse", stop=lambda b: sse_count(b) == 3)).body
    assert parse_all(reference) == EVENTS
    for cut in range(len(reference) + 1):
        parser = SseParser()
        delivered = [e.data for e in parser.feed(reference[:cut])]
        headers = (
            [(b"last-event-id", parser.last_event_id.encode())] if parser.last_event_id else []
        )
        remaining = len(EVENTS) - len(delivered)
        resumed = await read_stream(
            app,
            "/sse",
            headers=headers,
            stop=at_least(remaining),
            window_s=0.05 if remaining == 0 else 5.0,
        )
        assert delivered + parse_all(resumed.body) == EVENTS, f"cut at byte {cut}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_last_event_id_a_reconnect_replays_what_was_already_delivered() -> None:
    journal = await journal_with(*EVENTS)
    app = build_app(journal, allowed_origins=ALLOWED, ping_interval=0)
    again = await read_stream(app, "/sse", stop=lambda b: sse_count(b) == 3)
    assert parse_all(again.body) == EVENTS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_evicted_cursor_makes_the_sse_stream_start_with_a_reset_event() -> None:
    journal = await journal_with("a", "b", "c", "d", "e", capacity=2)
    app = build_app(journal, allowed_origins=ALLOWED, ping_interval=0)
    got = await read_stream(
        app, "/sse", headers=[(b"last-event-id", b"1")], stop=lambda b: sse_count(b) >= 3
    )
    events = SseParser().feed(got.body)
    assert [(e.event, e.data) for e in events] == [
        ("reset", "5"),
        ("message", "d"),
        ("message", "e"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idle_sse_streams_carry_comment_heartbeats_that_clients_never_see() -> None:
    app = build_app(EventJournal(4), allowed_origins=ALLOWED, ping_interval=0.05)
    got = await read_stream(app, "/sse", stop=lambda b: b.count(b": ping") >= 2, window_s=3.0)
    assert got.body.count(b": ping") >= 2
    assert SseParser().feed(got.body) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_with_pings_disabled_an_idle_sse_stream_sends_no_bytes() -> None:
    app = build_app(EventJournal(4), allowed_origins=ALLOWED, ping_interval=0)
    got = await read_stream(app, "/sse", window_s=0.2)
    assert got.status == 200
    assert got.body == b""


# ---------------------------------------------------------------------------
# Long polling
# ---------------------------------------------------------------------------


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_long_poll_answers_at_once_when_a_backlog_exists_and_forbids_caching() -> None:
    app = build_app(await journal_with("a", "b"), allowed_origins=ALLOWED)
    async with _client(app) as client:
        response = await asyncio.wait_for(client.get("/poll", params={"since": 0}), 1)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "events": [{"id": 1, "data": "a"}, {"id": 2, "data": "b"}],
        "cursor": 2,
        "reset": False,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_long_poll_holds_the_request_open_until_a_publish_arrives() -> None:
    journal = EventJournal(4)
    app = build_app(journal, allowed_origins=ALLOWED)
    async with _client(app) as client:
        pending = asyncio.create_task(client.get("/poll", params={"since": 0, "timeout": 5}))
        await asyncio.sleep(0.05)
        assert not pending.done()
        await journal.publish("late")
        response = await asyncio.wait_for(pending, 1)
    assert response.json()["events"] == [{"id": 1, "data": "late"}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_long_poll_timeout_is_a_200_with_no_events_and_the_same_cursor() -> None:
    app = build_app(await journal_with(*"abcdefg"), allowed_origins=ALLOWED)
    async with _client(app) as client:
        response = await client.get("/poll", params={"since": 7, "timeout": 0.05})
    assert response.status_code == 200
    assert response.json() == {"events": [], "cursor": 7, "reset": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_events_published_between_polls_are_batched_into_the_next_response() -> None:
    journal = await journal_with("one")
    app = build_app(journal, allowed_origins=ALLOWED)
    async with _client(app) as client:
        first = (await client.get("/poll", params={"since": 0})).json()
        for item in ("two", "three", "four"):
            await journal.publish(item)
        second = (await client.get("/poll", params={"since": first["cursor"]})).json()
    assert [e["data"] for e in second["events"]] == ["two", "three", "four"]
    assert second["cursor"] == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_poll_with_an_evicted_cursor_asks_the_client_to_reset() -> None:
    app = build_app(await journal_with("a", "b", "c", capacity=1), allowed_origins=ALLOWED)
    async with _client(app) as client:
        body = (await client.get("/poll", params={"since": 1})).json()
    assert body["reset"] is True
    assert body["cursor"] == 3


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_websocket_pushes_the_backlog_then_live_events() -> None:
    app = build_app(EventJournal(8), allowed_origins=ALLOWED)
    with TestClient(app) as client:
        client.post("/publish", content="backlog")
        with client.websocket_connect("/ws", headers={"origin": ORIGIN}) as ws:
            assert ws.receive_json() == {"id": 1, "data": "backlog"}
            assert client.post("/publish", content="live").status_code == 201
            assert ws.receive_json() == {"id": 2, "data": "live"}


@pytest.mark.unit
def test_a_client_message_on_one_websocket_reaches_another_connection() -> None:
    app = build_app(EventJournal(8), allowed_origins=ALLOWED)
    headers = {"origin": ORIGIN}
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws", headers=headers) as sender,
        client.websocket_connect("/ws", headers=headers) as listener,
    ):
        sender.send_text("from-sender")
        assert listener.receive_json() == {"id": 1, "data": "from-sender"}
        assert sender.receive_json() == {"id": 1, "data": "from-sender"}


@pytest.mark.unit
def test_a_websocket_handshake_from_an_unlisted_or_missing_origin_gets_403() -> None:
    app = build_app(EventJournal(8), allowed_origins=ALLOWED)
    with TestClient(app) as client:
        for headers in ({"origin": "https://evil.example"}, {}):
            with (
                pytest.raises(WebSocketDenialResponse) as denied,
                client.websocket_connect("/ws", headers=headers),
            ):
                pass
            assert denied.value.status_code == 403
        with client.websocket_connect("/ws", headers={"origin": ORIGIN.upper()}) as ws:
            client.post("/publish", content="ok")
            assert ws.receive_json()["data"] == "ok"


@pytest.mark.unit
def test_origin_policy_is_case_insensitive_and_strict_about_missing_headers() -> None:
    assert origin_allowed(ORIGIN.upper(), ALLOWED)
    assert not origin_allowed("https://app.example.com:8443", ALLOWED)
    assert not origin_allowed(None, ALLOWED)
    assert origin_allowed(None, ALLOWED, allow_missing=True)


# ---------------------------------------------------------------------------
# Framing overhead
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("size", [16, 125, 126, 1024])
def test_overhead_of_each_streaming_transport_follows_its_specification(size: int) -> None:
    event = format_sse("x" * size)
    ws_server = 2 if size <= 125 else 4
    assert ws_overhead(size, masked=False) == ws_server
    assert ws_overhead(size, masked=True) == ws_server + 4
    assert sse_overhead(size) == len("data: ") + len("\n\n")
    assert h1_chunk_overhead(event) == len(f"{len(event):x}") + 2 + 2
    assert h2_data_overhead(event) == 9
    assert h3_data_overhead(event) == 1 + varint_len(len(event))


@pytest.mark.unit
@pytest.mark.parametrize(("size", "extra"), [(63, 2), (64, 3), (16383, 3), (16384, 5)])
def test_http3_data_header_grows_with_the_varint_length(size: int, extra: int) -> None:
    assert h3_data_overhead(bytes(size)) == extra


@pytest.mark.unit
def test_long_polling_pays_a_full_request_and_response_head_per_delivery() -> None:
    request = b"GET /poll?since=7 HTTP/1.1\r\nHost: t\r\nAccept: application/json\r\n\r\n"
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Cache-Control: no-store\r\nContent-Length: 128\r\n\r\n"
    )
    cost = long_poll_overhead(bytes(128), cursor=7)
    assert cost == len(request) + len(response)
    assert cost > 10 * h2_data_overhead(bytes(128))


# ---------------------------------------------------------------------------
# Backpressure and policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_drop_oldest_keeps_the_newest_entries_and_counts_the_loss() -> None:
    sub = Subscriber(3, SlowConsumerPolicy.DROP_OLDEST)
    assert all(sub.offer(JournalEntry(i, str(i))) for i in range(1, 6))
    assert [e.id for e in sub.drain()] == [3, 4, 5]
    assert sub.dropped == 2


@pytest.mark.unit
def test_disconnect_policy_closes_the_subscriber_instead_of_buffering_without_bound() -> None:
    sub = Subscriber(2, SlowConsumerPolicy.DISCONNECT)
    assert sub.offer(JournalEntry(1, "a"))
    assert sub.offer(JournalEntry(2, "b"))
    assert not sub.offer(JournalEntry(3, "c"))
    assert sub.closed
    assert not sub.offer(JournalEntry(4, "d"))
    assert [e.id for e in sub.drain()] == [1, 2]


@pytest.mark.unit
def test_heartbeat_interval_is_half_the_shortest_idle_timeout_on_the_path() -> None:
    assert safe_heartbeat_interval([60.0, 125.0, 300.0]) == 30.0
    assert safe_heartbeat_interval([60.0], safety_factor=4) == 15.0
    with pytest.raises(ValueError, match="positive"):
        safe_heartbeat_interval([])


@pytest.mark.unit
def test_full_jitter_spreads_a_reconnecting_fleet_within_the_cap() -> None:
    rng = random.Random(1234)  # noqa: S311 - seeded for determinism
    delays = [reconnect_delay(10, rng) for _ in range(200)]
    assert max(delays) <= 30.0
    assert len({round(d, 3) for d in delays}) > 150
    assert max(reconnect_delay(0, rng) for _ in range(50)) <= 1.0
