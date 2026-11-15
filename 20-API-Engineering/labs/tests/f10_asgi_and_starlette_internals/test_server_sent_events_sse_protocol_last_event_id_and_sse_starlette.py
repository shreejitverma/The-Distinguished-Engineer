"""Lab: WHATWG SSE framing, Last-Event-ID resumption, and sse-starlette 3.6.0 internals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import h2.config
import h2.connection
import h2.events
import h11
import httpx
import httpx2
import pytest
from sse_starlette import EventSourceResponse, ServerSentEvent
from sse_starlette.sse import AppStatus, SendTimeoutError
from starlette.middleware.gzip import GZipMiddleware

from api_labs.f10_asgi_and_starlette_internals.sse_resumption import (
    EventLog,
    EventStreamError,
    EventStreamParser,
    MessageEvent,
    Outcome,
    Replay,
    ResumingClient,
    create_app,
    event_stream,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.types import ASGIApp, Message

CRLF = b"\r\n"


def parse(stream: bytes, **kwargs: Any) -> tuple[list[MessageEvent], EventStreamParser]:
    parser = EventStreamParser(**kwargs)
    events = parser.feed(stream) + parser.close()
    return events, parser


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class ScriptedPeer:
    """A raw ASGI peer: records what the app sends and disconnects when told to."""

    def __init__(self, method: str = "GET", headers: dict[str, str] | None = None) -> None:
        raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        self.scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/events",
            "raw_path": b"/events",
            "query_string": b"",
            "root_path": "",
            "headers": raw,
            "server": ("test", 80),
            "client": ("127.0.0.1", 50000),
        }
        self.messages: list[Message] = []
        self.disconnected = anyio.Event()
        self.on_body: Callable[[bytes], None] | None = None
        self.stall_on_body: int | None = None
        self._request_sent = False

    async def receive(self) -> Message:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        self.messages.append(message)
        if message["type"] != "http.response.body":
            return
        bodies = sum(1 for m in self.messages if m["type"] == "http.response.body")
        if self.stall_on_body is not None and bodies >= self.stall_on_body:
            await anyio.sleep_forever()  # a client whose TCP window never opens
        if self.on_body is not None and not self.disconnected.is_set():
            self.on_body(message.get("body", b""))

    async def run(self, app: ASGIApp) -> None:
        with anyio.fail_after(5):
            await app(self.scope, self.receive, self.send)

    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages if "body" in m)


def ids(events: list[MessageEvent]) -> list[str]:
    return [e.last_event_id for e in events]


# --- HTML 9.2.6: the parser, checked against the specification's own examples ---------


@pytest.mark.unit
def test_spec_multi_line_data_is_joined_with_lf() -> None:
    events, _ = parse(b"data: YHOO\ndata: +2\ndata: 10\n\n")
    assert events == [MessageEvent("message", "YHOO\n+2\n10", "")]


@pytest.mark.unit
def test_spec_four_block_stream_sets_then_resets_last_event_id() -> None:
    stream = b": test stream\n\ndata: first event\nid: 1\n\ndata:second event\nid\n\n"
    stream += b"data:  third event\n\n"
    events, parser = parse(stream)
    assert [(e.data, e.last_event_id) for e in events] == [
        ("first event", "1"),
        ("second event", ""),  # `id` with no value resets: no Last-Event-ID is sent
        (" third event", ""),  # only one space after the colon is removed
    ]
    assert parser.last_event_id == ""


@pytest.mark.unit
def test_spec_bare_data_fields_and_unterminated_last_block() -> None:
    events, _ = parse(b"data\n\ndata\ndata\n\ndata:")
    assert [e.data for e in events] == ["", "\n"]  # the final block has no blank line


@pytest.mark.unit
def test_spec_space_after_colon_is_optional() -> None:
    events, _ = parse(b"data:test\n\ndata: test\n\n")
    assert [e.data for e in events] == ["test", "test"]


@pytest.mark.unit
def test_one_leading_bom_is_stripped_and_a_second_one_is_not() -> None:
    one, _ = parse(b"\xef\xbb\xbfdata: a\n\n")
    two, _ = parse(b"\xef\xbb\xbf\xef\xbb\xbfdata: a\n\n")
    assert [e.data for e in one] == ["a"]
    assert two == []  # the field name is now "\ufeffdata", which is ignored


@pytest.mark.unit
def test_parse_result_is_identical_for_every_chunk_split() -> None:
    stream = "\ufeffid: 1\r\nevent: tick\rdata: caf\u00e9 \u20ac\ndata: x\r\n\r\n".encode()
    stream += b": ping\r\rdata: y\r\n\n"
    whole, whole_parser = parse(stream)
    assert [(e.type, e.data) for e in whole] == [("tick", "caf\u00e9 \u20ac\nx"), ("message", "y")]
    for cut in range(len(stream) + 1):
        parser = EventStreamParser()
        events = parser.feed(stream[:cut]) + parser.feed(stream[cut:]) + parser.close()
        assert events == whole, f"split at byte {cut}"
    parser = EventStreamParser()
    one_by_one = [e for i in range(len(stream)) for e in parser.feed(stream[i : i + 1])]
    assert one_by_one + parser.close() == whole
    assert parser.last_event_id == whole_parser.last_event_id == "1"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"1000", 1000),
        (b"0", 0),
        (b"-1", None),
        (b"1_000", None),
        (b"1e3", None),
        (b" 5", None),  # one space is stripped, the second is not a digit
        ("\u0663".encode(), None),  # ARABIC-INDIC DIGIT THREE is a digit, not ASCII
        (b"", None),
    ],
)
def test_retry_accepts_only_ascii_digits(value: bytes, expected: int | None) -> None:
    _, parser = parse(b"retry: " + value + b"\n\n")
    assert parser.reconnection_time_ms == expected


@pytest.mark.unit
def test_id_with_nul_is_ignored_and_id_only_block_dispatches_nothing() -> None:
    events, parser = parse(b"id: 5\n\nid: 6\x007\ndata: x\n\n")
    assert events == [MessageEvent("message", "x", "5")]
    assert parser.last_event_id == "5"


@pytest.mark.unit
def test_line_bound_stops_a_line_that_never_ends() -> None:
    parser = EventStreamParser(max_line_chars=1024)
    accepted = 0
    with pytest.raises(EventStreamError, match="line longer"):  # noqa: PT012
        for _ in range(100):  # one unterminated line split across many chunks
            parser.feed(b"a" * 100)
            accepted += 1
    assert accepted == 10  # rejected on the chunk that crossed 1024, not at the end


@pytest.mark.unit
def test_event_bound_stops_data_lines_without_a_blank_line() -> None:
    parser = EventStreamParser(max_event_chars=10_000)
    with pytest.raises(EventStreamError, match="event larger"):
        parser.feed(b"data: x\n" * 10_000)


# --- sse-starlette 3.6.0 ServerSentEvent.encode and ensure_bytes ---------------------


@pytest.mark.unit
def test_encode_field_order_and_default_crlf_separator() -> None:
    event = ServerSentEvent(data="a\nb", event="tick", id="7", retry=1000, comment="hi")
    assert event.encode() == (
        b": hi\r\nid: 7\r\nevent: tick\r\ndata: a\r\ndata: b\r\nretry: 1000\r\n\r\n"
    )


@pytest.mark.unit
def test_encode_strips_newlines_from_id_silently_but_passes_nul() -> None:
    assert ServerSentEvent(id="7\n8").encode() == b"id: 78\r\n\r\n"
    wire = (
        ServerSentEvent(data="x", id="5").encode() + ServerSentEvent(data="y", id="a\x00b").encode()
    )
    events, parser = parse(wire)
    assert ids(events) == ["5", "5"]  # a browser ignores the id with NUL
    assert parser.last_event_id == "5"


@pytest.mark.unit
def test_bare_cr_in_data_does_not_survive_the_round_trip() -> None:
    events, _ = parse(ServerSentEvent(data="a\rb\r\nc").encode())
    assert events[0].data == "a\nb\nc"
    payload = json.dumps({"text": "a\rb"})  # JSON escapes CR, so it survives
    events, _ = parse(ServerSentEvent(data=payload).encode())
    assert json.loads(events[0].data) == {"text": "a\rb"}


@pytest.mark.unit
def test_retry_bool_is_serialized_and_then_ignored_by_the_parser() -> None:
    wire = ServerSentEvent(retry=True).encode()
    assert wire == b"retry: True\r\n\r\n"  # bool passes isinstance(retry, int)
    assert parse(wire)[1].reconnection_time_ms is None
    with pytest.raises(TypeError, match="retry argument must be int"):
        ServerSentEvent(retry="5").encode()  # type: ignore[arg-type]


async def _collect(response: EventSourceResponse) -> bytes:
    peer = ScriptedPeer()
    await peer.run(response)
    return peer.body()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_yielding_a_dict_mutates_the_callers_dict() -> None:
    stored: dict[str, Any] = {"data": "x", "id": "1"}

    async def source() -> AsyncIterator[dict[str, Any]]:
        yield stored

    body = await _collect(EventSourceResponse(source(), ping=0))
    assert body == b"id: 1\r\ndata: x\r\n\r\n"
    assert stored == {"data": "x", "id": "1", "sep": "\r\n"}  # ensure_bytes added "sep"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_event_object_keeps_its_own_separator_not_the_responses() -> None:
    async def source() -> AsyncIterator[str | ServerSentEvent]:
        yield "a"
        yield ServerSentEvent(data="b")

    body = await _collect(EventSourceResponse(source(), ping=0, sep="\n"))
    assert body == b"data: a\n\ndata: b\r\n\r\n"


# --- EventSourceResponse behavior ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_headers_and_media_type() -> None:
    log = EventLog()
    log.publish("tick", "1")
    async with client_for(create_app(log, max_events_per_connection=1)) as client:
        response = await client.get("/events", headers={"Last-Event-ID": "0"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["connection"] == "keep-alive"
    assert "content-length" not in response.headers
    assert (
        response.content
        == b": connected\r\nretry: 3000\r\n\r\nid: 1\r\nevent: tick\r\ndata: 1\r\n\r\n"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_returns_headers_without_starting_the_stream() -> None:
    log = EventLog()
    async with client_for(create_app(log)) as client:
        with anyio.fail_after(2):  # before 3.6.0 this request never completed (issue #218)
            response = await client.head("/events")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.content == b""
    assert log.subscribe_calls == 0  # the generator never ran


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idle_stream_gets_a_ping_comment() -> None:
    seen_ping = anyio.Event()

    async def source() -> AsyncIterator[str]:
        yield "start"
        await seen_ping.wait()
        yield "end"

    peer = ScriptedPeer()
    peer.on_body = lambda body: seen_ping.set() if body.startswith(b": ping - ") else None
    await peer.run(EventSourceResponse(source(), ping=0.01))
    chunks = [m["body"] for m in peer.messages if m.get("body")]
    assert chunks[0] == b"data: start\r\n\r\n"
    assert chunks[1].startswith(b": ping - ")
    assert chunks[1].endswith(b"+00:00\r\n\r\n")  # datetime.now(timezone.utc), a comment
    assert chunks[-1] == b"data: end\r\n\r\n"
    assert parse(peer.body())[0] == [
        MessageEvent("message", "start", ""),
        MessageEvent("message", "end", ""),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ping_zero_sends_no_pings() -> None:
    async def source() -> AsyncIterator[str]:
        yield "start"
        await anyio.sleep(0.05)
        yield "end"

    body = await _collect(EventSourceResponse(source(), ping=0))
    assert body == b"data: start\r\n\r\ndata: end\r\n\r\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disconnect_cancels_the_generator_and_unsubscribes() -> None:
    log = EventLog()
    peer = ScriptedPeer()
    peer.on_body = lambda body: peer.disconnected.set() if b"id: 0" in body else None
    await peer.run(create_app(log))
    assert peer.body() == b": connected\r\nretry: 3000\r\n\r\nid: 0\r\n\r\n"
    assert log.subscribe_calls == 1
    assert log.subscriber_count == 0  # a leaked queue would keep receiving every event


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_timeout_tears_down_a_stalled_client() -> None:
    log = EventLog()
    for n in range(3):
        log.publish("tick", str(n))
    peer = ScriptedPeer(headers={"Last-Event-ID": "0"})
    peer.stall_on_body = 2  # the first replayed event never leaves the socket buffer
    with pytest.raises(SendTimeoutError):
        await peer.run(create_app(log, send_timeout=0.05, ping_seconds=0))
    assert log.subscribe_calls == 1
    assert log.subscriber_count == 0  # the generator was closed, so finally ran


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gzip_middleware_skips_event_streams_by_default() -> None:
    log = EventLog()
    log.publish("tick", "x" * 200)
    headers = {"Accept-Encoding": "gzip", "Last-Event-ID": "0"}
    default = GZipMiddleware(create_app(log, max_events_per_connection=1), minimum_size=1)
    async with client_for(default) as client:
        plain = await client.get("/events", headers=headers)
    forced = GZipMiddleware(
        create_app(log, max_events_per_connection=1), minimum_size=1, exclude_content_types=()
    )
    async with client_for(forced) as client:
        squeezed = await client.get("/events", headers=headers)
    assert "content-encoding" not in plain.headers
    assert squeezed.headers["content-encoding"] == "gzip"  # buffered by zlib until flushed


@pytest.mark.unit
def test_importing_sse_starlette_patches_uvicorn_handle_exit() -> None:
    from uvicorn.server import Server  # the class uvicorn.main re-exports

    patched: object = vars(Server)["handle_exit"]
    original: object = vars(AppStatus)["original_handler"]
    assert getattr(patched, "__qualname__", "") == "AppStatus.handle_exit"
    assert getattr(original, "__qualname__", "") == "Server.handle_exit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shutdown_event_lets_the_generator_send_a_farewell() -> None:
    shutdown = anyio.Event()

    async def source() -> AsyncIterator[ServerSentEvent]:
        yield ServerSentEvent(data="hello", id="1")
        await shutdown.wait()
        yield ServerSentEvent(event="bye", data="draining", retry=7000)

    AppStatus.should_exit = True
    try:
        body = await _collect(
            EventSourceResponse(source(), ping=0, shutdown_event=shutdown, shutdown_grace_period=1)
        )
    finally:
        AppStatus.should_exit = False
    events, parser = parse(body)
    assert [(e.type, e.data) for e in events] == [("message", "hello"), ("bye", "draining")]
    assert parser.reconnection_time_ms == 7000


# --- Resumption ------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconnect_with_last_event_id_replays_without_gap_or_repeat() -> None:
    log = EventLog()
    for n in range(1, 10):
        log.publish("tick", str(n))
    app = create_app(log, max_events_per_connection=3)
    async with client_for(app) as client:
        browser = ResumingClient(client, last_event_id="0")
        outcomes = [await browser.connect_once() for _ in range(3)]
    assert outcomes == [Outcome.RECONNECT] * 3
    assert browser.sent_ids == ["0", "3", "6"]
    assert [e.data for e in browser.received] == [str(n) for n in range(1, 10)]
    assert browser.reconnection_time_ms == 3000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mid_stream_drop_then_resume_is_gapless() -> None:
    log = EventLog()
    for n in range(1, 6):
        log.publish("tick", str(n))
    parser = EventStreamParser()
    seen: list[MessageEvent] = []

    def on_body(body: bytes) -> None:
        seen.extend(parser.feed(body))
        if parser.last_event_id == "3":
            peer.disconnected.set()  # bytes the server writes after this are lost

    peer = ScriptedPeer(headers={"Last-Event-ID": "0"})
    peer.on_body = on_body
    await peer.run(create_app(log))
    assert b"id: 5" in peer.body()  # the server wrote 4 and 5 into a dead connection
    assert log.subscriber_count == 0
    for n in range(6, 9):
        log.publish("tick", str(n))
    async with client_for(create_app(log, max_events_per_connection=5)) as client:
        browser = ResumingClient(client, last_event_id=parser.last_event_id)
        await browser.connect_once()
    delivered = [e.data for e in seen + browser.received]
    assert browser.sent_ids == ["3"]
    assert delivered == [str(n) for n in range(1, 9)]


class RacingLog(EventLog):
    """Publishes while the backlog read is in flight, like a slow database query."""

    def __init__(self, *, before: int = 0, after: int = 0) -> None:
        super().__init__()
        self.before = before
        self.after = after

    async def snapshot_after(self, cursor: int) -> Replay:
        for _ in range(self.before):
            self.publish("tick", f"before-{self.head + 1}")
        replay = await super().snapshot_after(cursor)
        for _ in range(self.after):
            self.publish("tick", f"after-{self.head + 1}")
        return replay


async def _resume_from_zero(log: EventLog, *, max_events: int, subscribe_first: bool) -> list[str]:
    """Connect with `Last-Event-ID: 0`, then publish one late event while the stream is live."""
    app = create_app(log, max_events_per_connection=max_events, subscribe_first=subscribe_first)
    client = client_for(app)
    browser = ResumingClient(client, last_event_id="0")
    with anyio.fail_after(2):
        async with client, anyio.create_task_group() as tg:
            tg.start_soon(browser.connect_once)
            await anyio.sleep(0.05)
            log.publish("tick", "late")
    return ids(browser.received)


def _racing_log(*, before: int = 0, after: int = 0) -> RacingLog:
    log = RacingLog(before=before, after=after)
    log.publish("tick", "1")
    return log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_events_published_during_the_backlog_read_are_not_lost() -> None:
    correct = await _resume_from_zero(_racing_log(after=2), max_events=3, subscribe_first=True)
    naive = await _resume_from_zero(_racing_log(after=2), max_events=2, subscribe_first=False)
    assert correct == ["1", "2", "3"]
    assert naive == ["1", "4"]  # 2 and 3 were published after the snapshot, before subscribe


@pytest.mark.unit
@pytest.mark.asyncio
async def test_overlap_between_snapshot_and_subscription_is_deduplicated() -> None:
    received = await _resume_from_zero(_racing_log(before=2), max_events=4, subscribe_first=True)
    assert received == ["1", "2", "3", "4"]  # 2 and 3 were in the snapshot and the queue


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cursor_older_than_retention_gets_a_reset_event() -> None:
    log = EventLog(retention=3)
    for n in range(1, 11):
        log.publish("tick", str(n))
    peer = ScriptedPeer(headers={"Last-Event-ID": "2"})  # 3 to 7 have been evicted
    parser = EventStreamParser(last_event_id="2")
    first: list[MessageEvent] = []

    def on_body(body: bytes) -> None:
        first.extend(parser.feed(body))
        if first:
            peer.disconnected.set()

    peer.on_body = on_body
    await peer.run(create_app(log))
    log.publish("tick", "11")
    async with client_for(create_app(log, max_events_per_connection=1)) as client:
        browser = ResumingClient(client, last_event_id=parser.last_event_id)
        await browser.connect_once()
    assert first == [MessageEvent("reset", json.dumps({"head": 10, "reason": "gap"}), "10")]
    assert browser.sent_ids == ["10"]  # the reset moved the cursor to the head
    assert browser.received == [MessageEvent("tick", "11", "11")]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("abc", "invalid-id"),
        ("-1", "invalid-id"),
        ("1" * 20, "invalid-id"),
        (str(2**63), "invalid-id"),  # 19 digits, but past the signed 64-bit maximum
        ("99", "gap"),
    ],
)
async def test_unusable_last_event_id_gets_200_and_reset_not_400(header: str, reason: str) -> None:
    log = EventLog()
    log.publish("tick", "1")
    peer = ScriptedPeer(headers={"Last-Event-ID": header})
    parser = EventStreamParser(last_event_id=header)
    events: list[MessageEvent] = []

    def on_body(body: bytes) -> None:
        events.extend(parser.feed(body))
        if events:
            peer.disconnected.set()

    peer.on_body = on_body
    await peer.run(create_app(log))
    assert peer.messages[0]["status"] == 200  # a 400 would close an EventSource for good
    assert events == [MessageEvent("reset", json.dumps({"head": 1, "reason": reason}), "1")]
    assert parser.last_event_id == "1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fresh_connection_is_primed_with_the_current_head() -> None:
    log = EventLog()
    for n in range(1, 5):
        log.publish("tick", str(n))
    peer = ScriptedPeer()
    parser = EventStreamParser()

    def on_body(body: bytes) -> None:
        parser.feed(body)
        if parser.last_event_id:
            peer.disconnected.set()

    peer.on_body = on_body
    await peer.run(create_app(log))
    assert parser.last_event_id == "4"  # history before the first connect is not replayed
    assert parser.close() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_subscriber_is_evicted_and_recovers_by_replay() -> None:
    log = EventLog(subscriber_buffer=2)
    stream = event_stream(log, None, retry_ms=1000)
    assert (await anext(stream)).retry == 1000
    assert (await anext(stream)).id == "0"  # subscribed, now at the live head
    for n in range(1, 4):
        log.publish("tick", str(n))  # the third does not fit the two-slot queue
    rest = [event.id async for event in stream]
    assert rest == ["1", "2"]  # the stream ends, so the client reconnects
    assert log.evicted_subscribers == 1
    assert log.subscriber_count == 0
    resumed = event_stream(log, "2", retry_ms=1000, max_events=1)
    assert [event.id async for event in resumed][1:] == ["3"]


# --- The wire under the stream ---------------------------------------------------------


@pytest.mark.unit
def test_http11_chunked_framing_adds_hex_length_and_two_crlfs_per_event() -> None:
    server = h11.Connection(h11.SERVER)
    server.receive_data(b"GET /events HTTP/1.1\r\nHost: test\r\n\r\n")
    assert isinstance(server.next_event(), h11.Request)
    head = server.send(
        h11.Response(status_code=200, headers=[(b"content-type", b"text/event-stream")])
    )
    chunk = ServerSentEvent(data="x", id="1").encode()
    wire = server.send(h11.Data(data=chunk))
    assert head is not None
    assert b"Transfer-Encoding: chunked\r\n" in head  # no Content-Length on an endless body
    assert len(chunk) == 18
    assert wire == b"12" + CRLF + chunk + CRLF


@pytest.mark.unit
def test_http2_drops_the_connection_header_and_frames_each_event() -> None:
    def config(client_side: bool) -> h2.config.H2Configuration:
        return h2.config.H2Configuration(client_side=client_side, header_encoding=None)

    client = h2.connection.H2Connection(config=config(True))
    server = h2.connection.H2Connection(config=config(False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    stream_id = client.get_next_available_stream_id()
    request = [
        (b":method", b"GET"),
        (b":scheme", b"https"),
        (b":authority", b"t"),
        (b":path", b"/"),
    ]
    client.send_headers(stream_id, request, end_stream=True)
    server.receive_data(client.data_to_send())

    async def never() -> AsyncIterator[str]:
        yield ""

    sse_headers = EventSourceResponse(never()).raw_headers
    assert (b"connection", b"keep-alive") in sse_headers
    server.send_headers(stream_id, [(b":status", b"200"), *sse_headers])
    received = client.receive_data(server.data_to_send())
    chunk = ServerSentEvent(data="x", id="1").encode()
    server.send_data(stream_id, chunk)
    data_frame = server.data_to_send()
    headers = next(e for e in received if isinstance(e, h2.events.ResponseReceived)).headers
    assert headers is not None
    names = [bytes(name) for name, _ in headers]
    assert b"connection" not in names  # h2 strips it (RFC 9113 Section 8.2.2)
    assert b"x-accel-buffering" in names
    assert len(data_frame) == 9 + len(chunk)  # one DATA frame header per event


# --- Another Python client: httpx2 2.16.0 EventSource ----------------------------------


def _httpx2_events(body: bytes, content_type: str) -> list[tuple[str, str, str, int | None]]:
    request = httpx2.Request("GET", "http://test/events")
    response = httpx2.Response(
        200, headers={"content-type": content_type}, content=body, request=request
    )
    return [(e.event, e.data, e.id, e.retry) for e in httpx2.EventSource(response)]


@pytest.mark.unit
def test_httpx2_eventsource_diverges_from_whatwg_on_four_inputs() -> None:
    utf8 = "text/event-stream; charset=utf-8"
    bom = b"\xef\xbb\xbfdata: a\n\n"
    assert _httpx2_events(bom, utf8) == []
    assert [e.data for e in parse(bom)[0]] == ["a"]

    event_only = b"event: x\n\n"
    assert _httpx2_events(event_only, utf8) == [("x", "", "", None)]
    assert parse(event_only)[0] == []

    negative = b"retry: -5\ndata: a\n\n"
    assert _httpx2_events(negative, utf8) == [("message", "a", "", -5)]
    assert parse(negative)[1].reconnection_time_ms is None

    accented = "data: \u00e9\n\n".encode()
    assert _httpx2_events(accented, "text/event-stream; charset=latin-1")[0][1] == "\u00c3\u00a9"
    assert parse(accented)[0][0].data == "\u00e9"  # event streams are always UTF-8
