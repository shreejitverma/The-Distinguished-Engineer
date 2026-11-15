"""Server-Sent Events: WHATWG parsing, Last-Event-ID resumption, and sse-starlette 3.6.0.

Three layers, all in-process and deterministic:

* `EventStreamParser` implements the WHATWG HTML rules for interpreting an event stream
  (HTML Living Standard section 9.2.6) incrementally, with explicit size bounds.
* `EventLog` is an append-only, bounded, in-memory log with integer event ids and bounded
  per-subscriber queues; it is the replay source for `Last-Event-ID`.
* `create_app` serves the log through `sse_starlette.EventSourceResponse`, subscribing
  before it reads the backlog so that no event published during the replay is lost.

`ResumingClient` plays the browser: it parses, remembers the last event id, and reconnects
with `Last-Event-ID`.
"""

from __future__ import annotations

import codecs
import json
import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.applications import Starlette
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from starlette.requests import Request

_LINE_END = re.compile(r"\r\n|\r|\n")
MAX_ID_DIGITS = 19  # bounds the work int() does on a hostile header
MAX_ID = 2**63 - 1  # a cursor must fit a signed 64-bit integer column


# --- Client side: HTML Living Standard 9.2.6 -------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """What a browser would dispatch: the event type, the data, and `lastEventId`."""

    type: str
    data: str
    last_event_id: str


class EventStreamError(Exception):
    """The peer exceeded a parser bound; the client should drop the connection."""


class EventStreamParser:
    """Incremental `text/event-stream` parser that follows HTML 9.2.6 line by line.

    `last_event_id` seeds the last event ID buffer, as Chromium does on reconnection.
    Lines and events are bounded in characters so a peer cannot grow memory without limit.
    """

    def __init__(
        self,
        *,
        last_event_id: str = "",
        max_line_chars: int = 65_536,
        max_event_chars: int = 1_048_576,
    ) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._at_start = True
        self._skip_lf = False
        self._line: list[str] = []
        self._line_len = 0
        self._data: list[str] = []
        self._data_len = 0
        self._event_type = ""
        self._id_buffer = last_event_id
        self.last_event_id = last_event_id
        self.reconnection_time_ms: int | None = None
        self.max_line_chars = max_line_chars
        self.max_event_chars = max_event_chars

    def feed(self, chunk: bytes) -> list[MessageEvent]:
        """Decode one network chunk as UTF-8 (always, whatever the charset) and parse it."""
        return self._consume(self._decoder.decode(chunk))

    def close(self) -> list[MessageEvent]:
        """End of stream: an event without its terminating blank line is discarded."""
        events = self._consume(self._decoder.decode(b"", final=True))
        self._line.clear()
        self._line_len = 0
        self._data.clear()
        self._data_len = 0
        self._event_type = ""
        return events

    def _consume(self, text: str) -> list[MessageEvent]:
        if self._at_start and text:
            self._at_start = False
            if text.startswith("\ufeff"):  # the UTF-8 decode algorithm strips one BOM
                text = text[1:]
        if self._skip_lf and text:
            self._skip_lf = False
            if text.startswith("\n"):  # the LF of a CRLF split across two chunks
                text = text[1:]
        events: list[MessageEvent] = []
        start = 0
        for match in _LINE_END.finditer(text):
            self._extend_line(text[start : match.start()])
            line = "".join(self._line)
            self._line.clear()
            self._line_len = 0
            event = self._process_line(line)
            if event is not None:
                events.append(event)
            start = match.end()
        self._extend_line(text[start:])
        if text.endswith("\r"):
            self._skip_lf = True
        return events

    def _extend_line(self, piece: str) -> None:
        if not piece:
            return
        self._line.append(piece)  # joined once per line, so a split line costs O(n)
        self._line_len += len(piece)
        if self._line_len > self.max_line_chars:
            raise EventStreamError(f"line longer than {self.max_line_chars} characters")

    def _process_line(self, line: str) -> MessageEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None  # a comment, which is how keep-alive pings travel
        name, colon, value = line.partition(":")
        if colon and value.startswith(" "):
            value = value[1:]
        if name == "event":
            self._event_type = value
        elif name == "data":
            self._data.append(value)
            self._data_len += len(value) + 1
            if self._data_len > self.max_event_chars:
                raise EventStreamError(f"event larger than {self.max_event_chars} characters")
        elif name == "id":
            if "\x00" not in value:
                self._id_buffer = value
        elif name == "retry" and value.isascii() and value.isdigit():
            self.reconnection_time_ms = int(value)
        return None

    def _dispatch(self) -> MessageEvent | None:
        self.last_event_id = self._id_buffer  # set even when nothing is dispatched
        if not self._data:  # the data buffer is the empty string
            self._event_type = ""
            return None
        # Appending LF after every data line and removing the last LF is a join.
        event = MessageEvent(
            type=self._event_type or "message",
            data="\n".join(self._data),
            last_event_id=self.last_event_id,
        )
        self._data.clear()
        self._data_len = 0
        self._event_type = ""
        return event


# --- Server side: the replay log ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    id: int
    event: str
    data: str


@dataclass(frozen=True, slots=True)
class Replay:
    events: tuple[LoggedEvent, ...]
    head: int  # newest id at snapshot time, 0 when the log is empty
    complete: bool  # False when the cursor is outside the retained window


@dataclass(slots=True)
class Subscription:
    send: MemoryObjectSendStream[LoggedEvent]
    receive: MemoryObjectReceiveStream[LoggedEvent]


class EventLog:
    """Append-only log with monotonically increasing ids and bounded fan-out queues."""

    def __init__(self, *, retention: int = 1000, subscriber_buffer: int = 64) -> None:
        self._events: deque[LoggedEvent] = deque(maxlen=retention)
        self._next_id = 1
        self._subscribers: set[MemoryObjectSendStream[LoggedEvent]] = set()
        self.subscriber_buffer = subscriber_buffer
        self.subscribe_calls = 0
        self.evicted_subscribers = 0

    @property
    def head(self) -> int:
        return self._next_id - 1

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: str) -> LoggedEvent:
        item = LoggedEvent(self._next_id, event, data)
        self._next_id += 1
        self._events.append(item)
        for send in list(self._subscribers):
            try:
                send.send_nowait(item)
            except anyio.WouldBlock:
                # Cut a slow consumer off instead of buffering without bound; its client
                # reconnects with Last-Event-ID and catches up from the log.
                self._subscribers.discard(send)
                send.close()
                self.evicted_subscribers += 1
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                self._subscribers.discard(send)
        return item

    def subscribe(self) -> Subscription:
        self.subscribe_calls += 1
        send, receive = anyio.create_memory_object_stream[LoggedEvent](self.subscriber_buffer)
        self._subscribers.add(send)
        return Subscription(send, receive)

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription.send)
        subscription.send.close()
        subscription.receive.close()

    async def snapshot_after(self, cursor: int) -> Replay:
        """Events with id > cursor; async like the database read it stands in for."""
        await anyio.lowlevel.checkpoint()
        oldest = self._events[0].id if self._events else self._next_id
        complete = oldest - 1 <= cursor <= self.head
        events = tuple(item for item in self._events if item.id > cursor) if complete else ()
        return Replay(events, self.head, complete)


def parse_last_event_id(value: str | None) -> int | None:
    """`None` when the header is absent; `ValueError` when it is not one of our ids."""
    if value is None:
        return None
    if not (value.isascii() and value.isdigit()) or len(value) > MAX_ID_DIGITS:
        raise ValueError(f"not an event id: {value!r}")
    cursor = int(value)
    if cursor > MAX_ID:
        raise ValueError(f"event id out of range: {value!r}")
    return cursor


def _to_sse(item: LoggedEvent) -> ServerSentEvent:
    return ServerSentEvent(data=item.data, event=item.event, id=str(item.id))


async def event_stream(
    log: EventLog,
    last_event_id: str | None,
    *,
    retry_ms: int,
    max_events: int | None = None,
    subscribe_first: bool = True,
) -> AsyncIterator[ServerSentEvent]:
    """Replay what the client missed, then follow the live tail, without gaps or repeats.

    `subscribe_first=False` is the race this lab exists to show: an event published while
    the backlog read is in flight is in neither the snapshot nor the subscription.
    """
    yield ServerSentEvent(comment="connected", retry=retry_ms)  # sets reconnection time only
    try:
        cursor = parse_last_event_id(last_event_id)
        usable = cursor is not None
    except ValueError:
        cursor, usable = None, False
    early = log.subscribe() if subscribe_first else None
    subscription = early
    try:
        replay = await log.snapshot_after(cursor) if cursor is not None else None
        subscription = early if early is not None else log.subscribe()
        sent = 0
        if replay is not None and replay.complete:
            position = cursor if cursor is not None else 0
            for item in replay.events:
                yield _to_sse(item)
                position = item.id
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
        else:
            position = replay.head if replay is not None else log.head
            if last_event_id is None:
                yield ServerSentEvent(id=str(position))  # priming: sets lastEventId, no event
            else:
                reset = {"head": position, "reason": "gap" if usable else "invalid-id"}
                yield ServerSentEvent(data=json.dumps(reset), event="reset", id=str(position))
        async for item in subscription.receive:
            if item.id <= position:
                continue  # already delivered by the replay
            yield _to_sse(item)
            position = item.id
            sent += 1
            if max_events is not None and sent >= max_events:
                return
    finally:
        if subscription is not None:
            log.unsubscribe(subscription)


def create_app(
    log: EventLog,
    *,
    retry_ms: int = 3000,
    ping_seconds: float = 15,
    send_timeout: float | None = 30.0,
    max_events_per_connection: int | None = None,
    subscribe_first: bool = True,
) -> Starlette:
    async def events(request: Request) -> EventSourceResponse:
        stream = event_stream(
            log,
            request.headers.get("last-event-id"),
            retry_ms=retry_ms,
            max_events=max_events_per_connection,
            subscribe_first=subscribe_first,
        )
        return EventSourceResponse(stream, ping=ping_seconds, send_timeout=send_timeout)

    return Starlette(routes=[Route("/events", events)])


# --- A browser-like client ------------------------------------------------------------


class Outcome(Enum):
    RECONNECT = "reconnect"  # the body ended normally; EventSource reestablishes
    FAIL = "fail"  # not 200 or not text/event-stream (204 included); no reconnection


@dataclass
class ResumingClient:
    """Reconnects like `EventSource`: same URL, `Last-Event-ID` once an id has been seen."""

    client: httpx.AsyncClient
    url: str = "/events"
    last_event_id: str = ""
    reconnection_time_ms: int | None = None
    received: list[MessageEvent] = field(default_factory=list[MessageEvent])
    sent_ids: list[str | None] = field(default_factory=list[str | None])

    async def connect_once(self) -> Outcome:
        headers = {"Accept": "text/event-stream"}
        if self.last_event_id:
            headers["Last-Event-ID"] = self.last_event_id
        self.sent_ids.append(headers.get("Last-Event-ID"))
        response = await self.client.get(self.url, headers=headers)
        essence = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if response.status_code != 200 or essence != "text/event-stream":
            return Outcome.FAIL
        parser = EventStreamParser(last_event_id=self.last_event_id)
        self.received += parser.feed(response.content) + parser.close()
        self.last_event_id = parser.last_event_id
        if parser.reconnection_time_ms is not None:
            self.reconnection_time_ms = parser.reconnection_time_ms
        return Outcome.RECONNECT
