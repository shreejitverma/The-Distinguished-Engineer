"""Full-duplex Starlette WebSocket endpoint with RFC 6455 handshake policy and bounded fan-out.

The endpoint checks `Origin`, negotiates one subprotocol from `scope["subprotocols"]`, then runs a
reader task and a writer task in one anyio task group.  Every connection owns a bounded outbox;
`Hub.broadcast` never awaits, so a slow consumer is evicted with close code 1013 instead of
stalling the publisher.  `InProcessWebSocket` drives any ASGI app over the `websocket` scope
without sockets, because `httpx.ASGITransport` only builds `http` scopes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import anyio
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import WebSocketRoute
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterable, Sequence

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from starlette.types import ASGIApp

# RFC 6455 Section 4.2.2 step 5.4: the GUID appended to Sec-WebSocket-Key before SHA-1.
WS_GUID: Final = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# RFC 6455 Section 7.4.1 codes plus 1012-1014 from the IANA WebSocket Close Code registry.
NORMAL_CLOSURE: Final = 1000
UNSUPPORTED_DATA: Final = 1003
POLICY_VIOLATION: Final = 1008
MESSAGE_TOO_BIG: Final = 1009
TRY_AGAIN_LATER: Final = 1013
# 1004 is reserved; 1005, 1006, and 1015 "MUST NOT be set as a status code in a Close control
# frame by an endpoint" (RFC 6455 Section 7.4.1): they exist only for local reporting.
_NEVER_ON_THE_WIRE: Final = frozenset({1004, 1005, 1006, 1015})


def accept_key(sec_websocket_key: str) -> str:
    """`Sec-WebSocket-Accept` = base64(SHA-1(key + GUID)) (RFC 6455 Section 4.2.2)."""
    digest = hashlib.sha1((sec_websocket_key + WS_GUID).encode("ascii"), usedforsecurity=False)
    return base64.b64encode(digest.digest()).decode("ascii")


def is_sendable_close_code(code: int) -> bool:
    """True when `code` may appear in a Close frame (RFC 6455 Sections 7.4.1 and 7.4.2)."""
    if code in _NEVER_ON_THE_WIRE:
        return False
    # 1000-2999 is reserved for the protocol; only the registered values may be sent.
    # 3000-3999 is IANA-registered for libraries and frameworks; 4000-4999 is private use.
    return 1000 <= code <= 1014 or 3000 <= code <= 4999


def select_subprotocol(offered: Iterable[str], supported: Sequence[str]) -> str | None:
    """Return the first server-supported subprotocol the client offered, else `None`.

    RFC 6455 Section 4.2.2 requires the value to be one the client sent; server preference
    order lets a server retire an old version by moving it down its own list.
    """
    offered_set = set(offered)
    for candidate in supported:
        if candidate in offered_set:
            return candidate
    return None


@dataclass
class Subscriber:
    conn_id: int
    outbox: MemoryObjectSendStream[str]
    evicted: anyio.Event = field(default_factory=anyio.Event)


@dataclass
class Hub:
    """Fan-out registry; `broadcast` is synchronous so no subscriber can block the publisher."""

    outbox_size: int
    subscribers: dict[int, Subscriber] = field(default_factory=dict[int, Subscriber])
    evictions: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    _next_id: int = 0

    def register(self) -> tuple[Subscriber, MemoryObjectReceiveStream[str]]:
        self._next_id += 1
        send, receive = anyio.create_memory_object_stream[str](max_buffer_size=self.outbox_size)
        subscriber = Subscriber(conn_id=self._next_id, outbox=send)
        self.subscribers[subscriber.conn_id] = subscriber
        return subscriber, receive

    def unregister(self, conn_id: int) -> None:
        subscriber = self.subscribers.pop(conn_id, None)
        if subscriber is not None:
            subscriber.outbox.close()

    def deliver(self, subscriber: Subscriber, text: str) -> bool:
        """Queue `text` without awaiting; a full outbox evicts the subscriber."""
        try:
            subscriber.outbox.send_nowait(text)
        except anyio.WouldBlock:
            self.evictions.append((subscriber.conn_id, TRY_AGAIN_LATER))
            subscriber.evicted.set()
            self.unregister(subscriber.conn_id)
            return False
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return False
        return True

    def broadcast(self, text: str) -> int:
        return sum(self.deliver(sub, text) for sub in list(self.subscribers.values()))


@dataclass(frozen=True)
class Policy:
    allowed_origins: frozenset[str]
    subprotocols: tuple[str, ...]  # server preference order
    max_message_chars: int = 4096
    close_timeout: float = 1.0


@dataclass
class _Session:
    close_code: int = NORMAL_CLOSURE
    close_reason: str = ""
    peer_gone: bool = False


def _handle_command(
    hub: Hub, subscriber: Subscriber, policy: Policy, message: Message
) -> _Session | None:
    """Apply one client message; return a `_Session` carrying a close code to stop."""
    text = message.get("text")
    if text is None:
        return _Session(UNSUPPORTED_DATA, "text frames only")
    if len(text) > policy.max_message_chars:
        return _Session(MESSAGE_TOO_BIG, f"limit {policy.max_message_chars} chars")
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return _Session(POLICY_VIOLATION, "malformed JSON")
    command = cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}
    op = command.get("op")
    if op == "ping":
        hub.deliver(subscriber, json.dumps({"op": "pong"}))
    elif op == "publish":
        sender, body = subscriber.conn_id, command.get("text")
        delivered = hub.broadcast(json.dumps({"op": "message", "from": sender, "text": body}))
        hub.deliver(subscriber, json.dumps({"op": "ack", "delivered": delivered}))
    else:
        return _Session(POLICY_VIOLATION, "unknown op")
    return None


async def _run_session(websocket: WebSocket, hub: Hub, policy: Policy) -> None:
    subscriber, inbox = hub.register()
    session = _Session()
    try:
        async with anyio.create_task_group() as tg:

            async def writer() -> None:
                async with inbox:
                    async for text in inbox:
                        try:
                            await websocket.send_text(text)
                        except WebSocketDisconnect:  # Starlette maps a server OSError to 1006
                            session.peer_gone = True
                            tg.cancel_scope.cancel()
                            return

            async def reader() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        session.peer_gone = True
                        tg.cancel_scope.cancel()
                        return
                    stop = _handle_command(hub, subscriber, policy, message)
                    if stop is not None:
                        session.close_code = stop.close_code
                        session.close_reason = stop.close_reason
                        tg.cancel_scope.cancel()
                        return

            async def eviction_watch() -> None:
                await subscriber.evicted.wait()
                session.close_code, session.close_reason = TRY_AGAIN_LATER, "slow consumer"
                tg.cancel_scope.cancel()

            tg.start_soon(writer)
            tg.start_soon(reader)
            tg.start_soon(eviction_watch)
    finally:
        hub.unregister(subscriber.conn_id)
    if not session.peer_gone:
        # The Close frame queues behind the same stalled socket; bound it, then return so the
        # server tears down TCP (the peer then reports 1006).
        with anyio.move_on_after(policy.close_timeout):
            await websocket.close(code=session.close_code, reason=session.close_reason)


def build_app(hub: Hub, policy: Policy) -> Starlette:
    async def chat(websocket: WebSocket) -> None:
        if websocket.headers.get("origin") not in policy.allowed_origins:
            # `websocket.close` before `websocket.accept`: the server answers HTTP 403.
            await websocket.close(code=POLICY_VIOLATION)
            return
        offered = websocket.scope.get("subprotocols", [])
        subprotocol = select_subprotocol(offered, policy.subprotocols)
        if subprotocol is None:
            if "websocket.http.response" in websocket.scope.get("extensions", {}):
                denial = PlainTextResponse("no common subprotocol", status_code=400)
                await websocket.send_denial_response(denial)
            else:
                await websocket.close(code=POLICY_VIOLATION)
            return
        await websocket.accept(subprotocol=subprotocol)
        await _run_session(websocket, hub, policy)

    async def naive(websocket: WebSocket) -> None:
        """Anti-pattern: one loop, so queued broadcasts wait until the client speaks."""
        await websocket.accept()
        subscriber, inbox = hub.register()
        try:
            async for text in websocket.iter_text():
                message: Message = {"type": "websocket.receive", "text": text}
                _handle_command(hub, subscriber, policy, message)
                while True:
                    try:
                        await websocket.send_text(inbox.receive_nowait())
                    except anyio.WouldBlock:
                        break
        finally:
            hub.unregister(subscriber.conn_id)
            inbox.close()

    return Starlette(routes=[WebSocketRoute("/ws", chat), WebSocketRoute("/naive", naive)])


@dataclass
class InProcessWebSocket:
    """The client and server halves of one ASGI `websocket` connection, joined by memory streams.

    `client_buffer=0` models a peer whose TCP window is closed: every app `send` blocks until the
    test calls `next_message`, the way Uvicorn's `send` waits on `writable` after
    `pause_writing`.
    """

    scope: dict[str, Any]
    _to_app: MemoryObjectSendStream[Message]
    _app_inbox: MemoryObjectReceiveStream[Message]
    _from_app: MemoryObjectSendStream[Message]
    _client_inbox: MemoryObjectReceiveStream[Message]
    app_done: anyio.Event = field(default_factory=anyio.Event)

    async def receive(self) -> Message:
        return await self._app_inbox.receive()

    async def send(self, message: Message) -> None:
        await self._from_app.send(message)

    async def send_text(self, text: str) -> None:
        await self._to_app.send({"type": "websocket.receive", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        await self._to_app.send({"type": "websocket.receive", "bytes": data})

    async def disconnect(self, code: int = NORMAL_CLOSURE) -> None:
        await self._to_app.send({"type": "websocket.disconnect", "code": code, "reason": ""})

    async def next_message(self, within: float = 1.0) -> Message:
        with anyio.fail_after(within):
            return await self._client_inbox.receive()

    def pending(self) -> list[Message]:
        """Everything the app has already sent, without waiting."""
        out: list[Message] = []
        while True:
            try:
                out.append(self._client_inbox.receive_nowait())
            except (anyio.WouldBlock, anyio.EndOfStream):
                return out

    async def run_app(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self.receive, self.send)
        finally:
            self._from_app.close()
            self.app_done.set()


@asynccontextmanager
async def connect(
    app: ASGIApp,
    path: str = "/ws",
    *,
    origin: str | None = "https://chat.example.com",
    subprotocols: Sequence[str] = (),
    client_buffer: float = math.inf,
    denial_extension: bool = True,
) -> AsyncGenerator[InProcessWebSocket]:
    headers: list[tuple[bytes, bytes]] = [(b"host", b"chat.example.com")]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if subprotocols:
        headers.append((b"sec-websocket-protocol", ", ".join(subprotocols).encode("ascii")))
    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
        "subprotocols": list(subprotocols),
        "extensions": {"websocket.http.response": {}} if denial_extension else {},
    }
    to_app, app_inbox = anyio.create_memory_object_stream[Message](max_buffer_size=math.inf)
    from_app, client_inbox = anyio.create_memory_object_stream[Message](client_buffer)
    conn = InProcessWebSocket(scope, to_app, app_inbox, from_app, client_inbox)
    await to_app.send({"type": "websocket.connect"})
    async with anyio.create_task_group() as tg:
        tg.start_soon(conn.run_app, app)
        try:
            yield conn
        finally:
            with anyio.fail_after(2):
                await conn.app_done.wait()
