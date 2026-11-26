"""Lab for real-time push transports: WebSocket, SSE, HTTP/2-3 streaming, and long polling.

Everything here is deterministic and in-process: byte-exact encoders and parsers for the
RFC 6455 frame and the WHATWG event stream, real h11, h2, and aioquic framing for the overhead
numbers, an event journal with `Last-Event-ID` replay, and one Starlette app that serves the same
journal over SSE, long polling, and WebSocket.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import hashlib
import re
from collections import deque
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Final

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h11
from aioquic.h3.connection import FrameType, encode_frame
from h2.settings import SettingCodes
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute

if TYPE_CHECKING:
    import random
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from starlette.requests import Request
    from starlette.websockets import WebSocket

WS_GUID: Final = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
"""The GUID that RFC 6455 Section 4.2.2 concatenates with `Sec-WebSocket-Key`."""


# ---------------------------------------------------------------------------
# WebSocket framing (RFC 6455 Sections 4.2.2, 5.2, 5.3, 5.5)
# ---------------------------------------------------------------------------


class Opcode(IntEnum):
    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


class WsProtocolError(ValueError):
    """A frame violates RFC 6455 Section 5; a real endpoint fails the connection."""


@dataclass(frozen=True, slots=True)
class WsFrame:
    fin: bool
    opcode: int
    masked: bool
    payload: bytes
    wire_size: int


def websocket_accept(key: str) -> str:
    """Compute `Sec-WebSocket-Accept` from `Sec-WebSocket-Key` (RFC 6455 Section 4.2.2)."""
    digest = hashlib.sha1((key.strip() + WS_GUID).encode("ascii"), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_header_size(payload_len: int, *, masked: bool) -> int:
    """Frame header bytes: 2, plus 2 or 8 for extended length, plus 4 when masked."""
    if payload_len < 0 or payload_len >= 2**63:
        raise ValueError("payload length out of range")
    extended = 0 if payload_len <= 125 else 2 if payload_len <= 0xFFFF else 8
    return 2 + extended + (4 if masked else 0)


def apply_mask(data: bytes, key: bytes) -> bytes:
    """XOR `data` with the 4-byte key; the same call masks and unmasks (RFC 6455 Section 5.3)."""
    if len(key) != 4:
        raise ValueError("masking key must be 4 bytes")
    if not data:
        return b""
    stream = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def encode_ws_frame(
    opcode: int, payload: bytes, *, mask_key: bytes | None = None, fin: bool = True
) -> bytes:
    """Serialize one frame. Pass `mask_key` for client-to-server frames."""
    if opcode >= 0x8 and (len(payload) > 125 or not fin):
        raise WsProtocolError("control frames must be <= 125 bytes and unfragmented")
    first = (0x80 if fin else 0x00) | opcode
    mask_bit = 0x80 if mask_key is not None else 0x00
    n = len(payload)
    if n <= 125:
        header = bytes([first, mask_bit | n])
    elif n <= 0xFFFF:
        header = bytes([first, mask_bit | 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([first, mask_bit | 127]) + n.to_bytes(8, "big")
    if mask_key is None:
        return header + payload
    return header + mask_key + apply_mask(payload, mask_key)


def decode_ws_frame(buffer: bytes) -> WsFrame | None:
    """Parse one frame from the front of `buffer`, or return None while it is incomplete."""
    if len(buffer) < 2:
        return None
    b0, b1 = buffer[0], buffer[1]
    if b0 & 0x70:
        raise WsProtocolError("RSV bits set without a negotiated extension")
    fin, opcode, masked, length = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
    offset = 2
    if length in (126, 127):
        width = 2 if length == 126 else 8
        if len(buffer) < offset + width:
            return None
        length = int.from_bytes(buffer[offset : offset + width], "big")
        offset += width
        if length <= (125 if width == 2 else 0xFFFF) or length >= 2**63:
            raise WsProtocolError("payload length is not minimally encoded")
    if opcode >= 0x8 and (length > 125 or not fin):
        raise WsProtocolError("control frame too long or fragmented")
    key = b""
    if masked:
        if len(buffer) < offset + 4:
            return None
        key, offset = buffer[offset : offset + 4], offset + 4
    if len(buffer) < offset + length:
        return None
    body = buffer[offset : offset + length]
    return WsFrame(fin, opcode, masked, apply_mask(body, key) if masked else body, offset + length)


# ---------------------------------------------------------------------------
# Server-Sent Events (WHATWG HTML Standard 9.2.5 and 9.2.6)
# ---------------------------------------------------------------------------

_LINE_BREAK: Final = re.compile(r"\r\n|\r|\n")
_ASCII_DIGITS: Final = re.compile(r"[0-9]+")


def format_sse(
    data: str | None = None,
    *,
    event: str | None = None,
    id: str | None = None,
    retry: int | None = None,
    comment: str | None = None,
) -> bytes:
    """Serialize one event block terminated by a blank line, using LF line ends."""
    lines: list[str] = []
    if comment is not None:
        lines.extend(f": {part}" for part in _LINE_BREAK.split(comment))
    if event is not None:
        if _LINE_BREAK.search(event):
            raise ValueError("event type must not contain CR or LF")
        lines.append(f"event: {event}")
    if id is not None:
        if _LINE_BREAK.search(id) or "\0" in id:
            raise ValueError("event id must not contain CR, LF, or NUL")
        lines.append(f"id: {id}")
    if retry is not None:
        if retry < 0:
            raise ValueError("retry must be a non-negative number of milliseconds")
        lines.append(f"retry: {retry}")
    if data is not None:
        lines.extend(f"data: {part}" for part in _LINE_BREAK.split(data))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class SseEvent:
    data: str
    event: str
    last_event_id: str


class SseParser:
    """Incremental event stream parser following the WHATWG interpreting algorithm."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._at_start = True
        self._pending_cr = False
        self._line: list[str] = []
        self._data: list[str] = []
        self._event_type = ""
        self._id_buffer = ""
        self.last_event_id = ""
        self.reconnection_time_ms: int | None = None

    def feed(self, chunk: bytes) -> list[SseEvent]:
        """Consume bytes and return the events completed by them."""
        text = self._decoder.decode(chunk)
        if self._at_start and text:
            self._at_start = False
            text = text.removeprefix("\ufeff")
        events: list[SseEvent] = []
        for ch in text:
            if self._pending_cr:
                self._pending_cr = False
                if ch == "\n":
                    continue
            if ch in "\r\n":
                self._pending_cr = ch == "\r"
                line, self._line = "".join(self._line), []
                dispatched = self._process_line(line)
                if dispatched is not None:
                    events.append(dispatched)
            else:
                self._line.append(ch)
        return events

    def _process_line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._event_type = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            if "\0" not in value:
                self._id_buffer = value
        elif name == "retry" and _ASCII_DIGITS.fullmatch(value):
            self.reconnection_time_ms = int(value)
        return None

    def _dispatch(self) -> SseEvent | None:
        self.last_event_id = self._id_buffer
        if not self._data:
            self._event_type = ""
            return None
        event = SseEvent("\n".join(self._data), self._event_type or "message", self.last_event_id)
        self._data, self._event_type = [], ""
        return event


# ---------------------------------------------------------------------------
# Event journal: the server-side half of resumable delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalEntry:
    id: int
    data: str


@dataclass(frozen=True, slots=True)
class Reset:
    """Tell the client its cursor is unusable and it must resynchronize from a snapshot."""

    last_id: int


@dataclass(frozen=True, slots=True)
class Replay:
    entries: tuple[JournalEntry, ...]
    gap: bool


class EventJournal:
    """Bounded in-memory log with monotonic integer ids, shared by all three transports."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._entries: deque[JournalEntry] = deque(maxlen=capacity)
        self._next_id = 1
        self._changed = asyncio.Condition()

    @property
    def last_id(self) -> int:
        return self._next_id - 1

    async def publish(self, data: str) -> JournalEntry:
        async with self._changed:
            entry = JournalEntry(self._next_id, data)
            self._next_id += 1
            self._entries.append(entry)
            self._changed.notify_all()
        return entry

    def since(self, last_id: int) -> Replay:
        """Entries after `last_id`; `gap` if the cursor was evicted or is from the future."""
        oldest = self._entries[0].id if self._entries else self._next_id
        gap = last_id + 1 < oldest or last_id > self.last_id
        entries = tuple(e for e in self._entries if gap or e.id > last_id)
        return Replay(entries, gap)

    async def wait_after(self, last_id: int, wait_s: float | None) -> Replay:
        """Block until something differs from `last_id`, or `wait_s` seconds pass."""
        async with self._changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self.last_id != last_id), wait_s
                )
            return self.since(last_id)


async def follow(journal: EventJournal, last_id: int) -> AsyncGenerator[JournalEntry | Reset]:
    """Replay what the client missed, then follow live; shared by the SSE and WebSocket routes."""
    cursor = last_id
    replay = journal.since(cursor)
    while True:
        if replay.gap:
            yield Reset(journal.last_id)
            cursor = journal.last_id
        for entry in replay.entries:
            yield entry
            cursor = entry.id
        replay = await journal.wait_after(cursor, None)


class SlowConsumerPolicy(StrEnum):
    DROP_OLDEST = "drop-oldest"
    DISCONNECT = "disconnect"


class Subscriber:
    """A bounded per-connection queue: the only place a slow consumer can be handled."""

    def __init__(self, capacity: int, policy: SlowConsumerPolicy) -> None:
        self._queue: deque[JournalEntry] = deque()
        self._capacity = capacity
        self._policy = policy
        self.dropped = 0
        self.closed = False

    def offer(self, entry: JournalEntry) -> bool:
        """Queue an entry; False means the connection must be closed."""
        if self.closed:
            return False
        if len(self._queue) >= self._capacity:
            if self._policy is SlowConsumerPolicy.DISCONNECT:
                self.closed = True
                return False
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(entry)
        return True

    def drain(self) -> list[JournalEntry]:
        items = list(self._queue)
        self._queue.clear()
        return items


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------


def origin_allowed(
    origin: str | None, allowed: frozenset[str], *, allow_missing: bool = False
) -> bool:
    """Server-side Origin check that browsers do not do for WebSockets (RFC 6455 Section 10.2)."""
    if origin is None:
        return allow_missing
    return origin.lower() in allowed


def safe_heartbeat_interval(
    idle_timeouts_s: Sequence[float], *, safety_factor: float = 2.0
) -> float:
    """Heartbeat period that survives the shortest idle timeout on the path, with margin."""
    if not idle_timeouts_s or min(idle_timeouts_s) <= 0 or safety_factor < 1:
        raise ValueError("need positive idle timeouts and a safety factor >= 1")
    return min(idle_timeouts_s) / safety_factor


def reconnect_delay(
    attempt: int, rng: random.Random, *, base: float = 1.0, cap: float = 30.0
) -> float:
    """Full-jitter exponential backoff, so a dropped fleet does not reconnect in lockstep."""
    return rng.uniform(0.0, min(cap, base * 2.0**attempt))


# ---------------------------------------------------------------------------
# Framing overhead, measured with the real encoders
# ---------------------------------------------------------------------------


def ws_overhead(payload_len: int, *, masked: bool) -> int:
    payload = b"x" * payload_len
    key = b"\x01\x02\x03\x04" if masked else None
    return len(encode_ws_frame(Opcode.TEXT, payload, mask_key=key)) - payload_len


def sse_overhead(payload_len: int) -> int:
    return len(format_sse("x" * payload_len)) - payload_len


def h1_chunk_overhead(body: bytes) -> int:
    """Bytes h11 adds around `body` as one chunk of a chunked body (RFC 9112 Section 7.1)."""
    server = h11.Connection(h11.SERVER)
    server.receive_data(b"GET /s HTTP/1.1\r\nHost: t\r\n\r\n")
    server.next_event()
    server.send(h11.Response(status_code=200, headers=[("Transfer-Encoding", "chunked")]))
    return len(server.send(h11.Data(data=body)) or b"") - len(body)


def _h2_pair(
    server_settings: dict[SettingCodes | int, int] | None = None,
) -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    client = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
    )
    server = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
    )
    client.initiate_connection()
    server.initiate_connection()
    if server_settings:
        server.update_settings(server_settings)
    _pump(client, server)
    return client, server


def _pump(
    client: h2.connection.H2Connection, server: h2.connection.H2Connection
) -> list[h2.events.Event]:
    """Exchange pending bytes until quiet; return the events the server observed."""
    seen: list[h2.events.Event] = []
    for _ in range(4):
        to_server = client.data_to_send()
        if to_server:
            seen.extend(server.receive_data(to_server))
        to_client = server.data_to_send()
        if to_client:
            client.receive_data(to_client)
    return seen


def h2_data_overhead(body: bytes) -> int:
    """Bytes h2 adds around `body` in one DATA frame (a 9-octet header, RFC 9113 Section 4.1)."""
    client, server = _h2_pair()
    stream_id = client.get_next_available_stream_id()
    client.send_headers(
        stream_id,
        [(":method", "GET"), (":scheme", "https"), (":authority", "t"), (":path", "/s")],
    )
    _pump(client, server)
    server.send_headers(stream_id, [(":status", "200")])
    _pump(client, server)
    server.send_data(stream_id, body)
    return len(server.data_to_send()) - len(body)


def h3_data_overhead(body: bytes) -> int:
    """HTTP/3 DATA frame header: type 0x00 plus a varint length (RFC 9114 Section 7.2.1)."""
    return len(encode_frame(FrameType.DATA, body)) - len(body)


def long_poll_overhead(body: bytes, cursor: int) -> int:
    """Bytes of one request plus response head per delivery, with only the headers shown below.

    This is a floor: real servers add `Date`, `Server`, cookies, and `Vary`.
    """
    client = h11.Connection(h11.CLIENT)
    request = client.send(
        h11.Request(
            method="GET",
            target=f"/poll?since={cursor}",
            headers=[("Host", "t"), ("Accept", "application/json")],
        )
    )
    client.send(h11.EndOfMessage())
    server = h11.Connection(h11.SERVER)
    server.receive_data(request or b"")
    server.next_event()
    response = server.send(
        h11.Response(
            status_code=200,
            reason=b"OK",
            headers=[
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
                ("Content-Length", str(len(body))),
            ],
        )
    )
    return len(request or b"") + len(response or b"")


# ---------------------------------------------------------------------------
# HTTP/2: stream slots and WebSocket over extended CONNECT (RFC 9113, RFC 8441)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamSlots:
    accepted: int
    refused_at: int | None


def h2_sse_streams(server_max_concurrent: int, attempts: int) -> StreamSlots:
    """Open long-lived `GET` streams until the client hits the server's concurrency limit."""
    client, server = _h2_pair({SettingCodes.MAX_CONCURRENT_STREAMS: server_max_concurrent})
    accepted = 0
    for attempt in range(1, attempts + 1):
        stream_id = client.get_next_available_stream_id()
        try:
            client.send_headers(
                stream_id,
                [
                    (":method", "GET"),
                    (":scheme", "https"),
                    (":authority", "t"),
                    (":path", "/events"),
                    ("accept", "text/event-stream"),
                ],
            )
        except h2.exceptions.TooManyStreamsError:
            return StreamSlots(accepted, attempt)
        accepted += 1
    _pump(client, server)
    return StreamSlots(accepted, None)


class ExtendedConnectUnavailable(RuntimeError):
    """The server did not send `SETTINGS_ENABLE_CONNECT_PROTOCOL = 1` (RFC 8441 Section 3)."""


@dataclass(frozen=True, slots=True)
class ExtendedConnectResult:
    request_headers: tuple[tuple[str, str], ...]
    response_status: str
    ws_payload: bytes


def h2_websocket_over_extended_connect(*, server_enables: bool) -> ExtendedConnectResult:
    """Open a WebSocket on one HTTP/2 stream and send one masked text frame as DATA."""
    settings: dict[SettingCodes | int, int] | None = (
        {SettingCodes.ENABLE_CONNECT_PROTOCOL: 1} if server_enables else None
    )
    client, server = _h2_pair(settings)
    if client.remote_settings.enable_connect_protocol != 1:
        raise ExtendedConnectUnavailable("server did not advertise extended CONNECT")
    stream_id = client.get_next_available_stream_id()
    client.send_headers(
        stream_id,
        [
            (":method", "CONNECT"),
            (":protocol", "websocket"),
            (":scheme", "https"),
            (":path", "/chat"),
            (":authority", "t"),
            ("sec-websocket-version", "13"),
            ("origin", "https://t"),
        ],
    )
    events = _pump(client, server)
    request = next(e for e in events if isinstance(e, h2.events.RequestReceived))
    headers = tuple((_text(k), _text(v)) for k, v in request.headers or [])
    server.send_headers(stream_id, [(":status", "200")])
    client.send_data(stream_id, encode_ws_frame(Opcode.TEXT, b"hi", mask_key=b"\x0a\x0b\x0c\x0d"))
    events = _pump(client, server)
    data = b"".join(e.data for e in events if isinstance(e, h2.events.DataReceived))
    frame = decode_ws_frame(data)
    if frame is None:
        raise RuntimeError("server did not receive a complete WebSocket frame")
    return ExtendedConnectResult(headers, "200", frame.payload)


# ---------------------------------------------------------------------------
# One Starlette app, three transports over the same journal
# ---------------------------------------------------------------------------


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _cursor(raw: str | None) -> int:
    return int(raw) if raw is not None and raw.isdecimal() else 0


def build_app(
    journal: EventJournal,
    *,
    allowed_origins: frozenset[str],
    ping_interval: float = 15.0,
    poll_max_wait: float = 30.0,
) -> Starlette:
    async def sse(request: Request) -> Response:
        last = _cursor(request.headers.get("last-event-id"))

        async def events() -> AsyncIterator[ServerSentEvent]:
            async for item in follow(journal, last):
                if isinstance(item, Reset):
                    yield ServerSentEvent(str(item.last_id), event="reset")
                else:
                    yield ServerSentEvent(item.data, id=str(item.id))

        return EventSourceResponse(events(), ping=ping_interval)

    async def publish(request: Request) -> Response:
        entry = await journal.publish((await request.body()).decode("utf-8"))
        return JSONResponse({"id": entry.id}, status_code=201)

    async def poll(request: Request) -> Response:
        since = _cursor(request.query_params.get("since"))
        wait = min(float(request.query_params.get("timeout", poll_max_wait)), poll_max_wait)
        replay = journal.since(since)
        if not replay.entries and not replay.gap:
            replay = await journal.wait_after(since, wait)
        cursor = (
            replay.entries[-1].id if replay.entries else journal.last_id if replay.gap else since
        )
        body = {
            "events": [{"id": e.id, "data": e.data} for e in replay.entries],
            "cursor": cursor,
            "reset": replay.gap,
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    async def ws_endpoint(ws: WebSocket) -> None:
        if not origin_allowed(ws.headers.get("origin"), allowed_origins):
            if "websocket.http.response" in ws.scope.get("extensions", {}):
                await ws.send_denial_response(PlainTextResponse("origin not allowed", 403))
            else:
                await ws.close(code=1008)
            return
        await ws.accept()

        async def push() -> None:
            async for item in follow(journal, _cursor(ws.query_params.get("since"))):
                if isinstance(item, Reset):
                    await ws.send_json({"type": "reset", "last_id": item.last_id})
                else:
                    await ws.send_json({"id": item.id, "data": item.data})

        pusher = asyncio.create_task(push())
        try:
            async for text in ws.iter_text():
                await journal.publish(text)
        finally:
            pusher.cancel()

    return Starlette(
        routes=[
            Route("/sse", sse),
            Route("/publish", publish, methods=["POST"]),
            Route("/poll", poll),
            WebSocketRoute("/ws", ws_endpoint),
        ]
    )
