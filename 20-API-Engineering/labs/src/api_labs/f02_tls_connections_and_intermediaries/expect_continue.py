"""Expect: 100-continue upload admission before the body is sent (RFC 9110 Section 10.1.1)."""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import h11
from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.asyncio.task_group import TaskGroup
from hypercorn.asyncio.worker_context import WorkerContext
from hypercorn.config import Config as HypercornConfig
from hypercorn.events import Closed, RawData
from hypercorn.protocol.h11 import H11Protocol as HypercornH11Protocol
from hypercorn.typing import ConnectionState
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.config import Config as UvicornConfig
from uvicorn.protocols.http.h11_impl import H11Protocol as UvicornH11Protocol
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
from uvicorn.server import ServerState

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from hypercorn.events import Event as HypercornEvent
    from hypercorn.typing import ASGIFramework
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

Header = tuple[bytes, bytes]
CONTINUE = 100
CLOSE: Header = (b"connection", b"close")
_SETTLE_ROUNDS = 50


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """What an upload endpoint can decide from the method, target, and header section alone."""

    max_content_length: int
    bearer_tokens: frozenset[bytes]
    realm: bytes = b"uploads"


@dataclass(frozen=True, slots=True)
class Verdict:
    """`100` admits the body; any other status is a final answer sent before the body."""

    status: int
    headers: tuple[Header, ...] = ()

    @property
    def admitted(self) -> bool:
        return self.status == CONTINUE


def expectations(headers: Iterable[Header]) -> list[bytes]:
    """Every `Expect` list member, lowercased, across all `Expect` field lines."""
    members: list[bytes] = []
    for name, value in headers:
        if name.lower() == b"expect":
            members.extend(m.strip().lower() for m in value.split(b",") if m.strip())
    return members


def admit(headers: Sequence[Header], policy: UploadPolicy) -> Verdict:
    """Decide an upload from its header section alone: expectation, then identity, then size."""
    if any(member != b"100-continue" for member in expectations(headers)):
        return Verdict(417)
    fields = {name.lower(): value for name, value in headers}
    scheme, _, token = fields.get(b"authorization", b"").partition(b" ")
    if scheme.lower() != b"bearer" or token not in policy.bearer_tokens:
        challenge = b'Bearer realm="' + policy.realm + b'"'
        return Verdict(401, ((b"www-authenticate", challenge),))
    declared = fields.get(b"content-length")
    if declared is not None and int(declared) > policy.max_content_length:
        return Verdict(413)
    return Verdict(CONTINUE)


def reject(conn: h11.Connection, verdict: Verdict, *, close: bool) -> bytes:
    """Send a complete final response; `close` says whether the unread body will be drained."""
    headers = [*verdict.headers, (b"content-length", b"0")]
    if close:
        headers.append(CLOSE)
    wire = conn.send(h11.Response(status_code=verdict.status, headers=headers))
    return wire + conn.send(h11.EndOfMessage())


def answer_head(
    conn: h11.Connection, request: h11.Request, policy: UploadPolicy, *, close_on_reject: bool
) -> tuple[Verdict, bytes]:
    """Answer a request head before reading any body byte: `100 Continue` or a final status."""
    verdict = admit(list(request.headers), policy)
    if not verdict.admitted:
        return verdict, reject(conn, verdict, close=close_on_reject)
    if conn.they_are_waiting_for_100_continue:
        return verdict, conn.send(h11.InformationalResponse(status_code=CONTINUE, headers=[]))
    return verdict, b""


class UploadServer:
    """One HTTP/1.1 upload endpoint on an h11 server connection, fed bytes by the caller."""

    def __init__(self, policy: UploadPolicy, *, close_on_reject: bool = True) -> None:
        self.conn = h11.Connection(h11.SERVER)
        self.policy = policy
        self.close_on_reject = close_on_reject
        self.verdict: Verdict | None = None
        self.body_bytes_received = 0

    @property
    def reusable(self) -> bool:
        return self.conn.our_state is h11.DONE and self.conn.their_state is h11.DONE

    def feed(self, data: bytes) -> bytes:
        """Process received bytes and return the bytes this server writes back."""
        if self.conn.our_state is h11.MUST_CLOSE:
            return b""  # closing: bytes still in flight are never read (the peer may see RST)
        if data:  # h11 reads b"" as end of stream, and a Content-Length EndOfMessage is b""
            self.conn.receive_data(data)
        out = bytearray()
        while self.conn.our_state is not h11.MUST_CLOSE:
            event = self.conn.next_event()
            if event is h11.NEED_DATA or event is h11.PAUSED:
                break
            if isinstance(event, h11.Request):
                self.verdict, wire = answer_head(
                    self.conn, event, self.policy, close_on_reject=self.close_on_reject
                )
                out += wire
            elif isinstance(event, h11.Data):
                self.body_bytes_received += len(event.data)
                over = self.body_bytes_received > self.policy.max_content_length
                if over and self.conn.our_state is h11.SEND_RESPONSE:
                    out += reject(self.conn, Verdict(413), close=True)  # streamed past the cap
            elif isinstance(event, h11.EndOfMessage) and self.conn.our_state is h11.SEND_RESPONSE:
                done = h11.Response(status_code=201, headers=[(b"content-length", b"0")])
                out += self.conn.send(done) + self.conn.send(h11.EndOfMessage())
        return bytes(out)


class ClientMode(enum.Enum):
    """How the uploading client uses `Expect`."""

    NO_EXPECT = "writes the whole body, then reads (httpcore 1.0.9)"
    EXPECT_WAIT = "waits for 100 or a final status before the body (curl, Go net/http)"
    EXPECT_NO_WAIT = "sends Expect but streams at once (a waiter whose timeout fired)"


@dataclass(frozen=True, slots=True)
class UploadTrace:
    """What crossed the wire in one simulated HTTP/1.1 upload."""

    interim_statuses: tuple[int, ...]
    final_status: int
    body_bytes_sent: int
    server_reusable: bool


def simulate_upload(
    server: UploadServer,
    *,
    body_size: int,
    headers: Sequence[Header],
    mode: ClientMode,
    chunked: bool = False,
    chunk_size: int = 64 * 1024,
) -> UploadTrace:
    """Upload `body_size` bytes from an h11 client to `server` in memory; count body bytes sent.

    A client that sees a final response stops sending only if it says `Connection: close`;
    otherwise it sends the rest so the connection stays framed, as Go 1.25.0 net/http does.
    """
    client = h11.Connection(h11.CLIENT)
    if chunked:
        framing = (b"transfer-encoding", b"chunked")
    else:
        framing = (b"content-length", str(body_size).encode("ascii"))
    request_headers = [(b"host", b"uploads.example"), framing, *headers]
    if mode is not ClientMode.NO_EXPECT:
        request_headers.append((b"expect", b"100-continue"))
    head = client.send(h11.Request(method="PUT", target="/up", headers=request_headers))
    to_client = bytearray(server.feed(head))
    interim: list[int] = []
    final: h11.Response | None = None

    def read() -> None:
        nonlocal final
        if to_client:
            client.receive_data(bytes(to_client))
            to_client.clear()
        while final is None and (event := client.next_event()) is not h11.NEED_DATA:
            if isinstance(event, h11.InformationalResponse):
                interim.append(event.status_code)
            elif isinstance(event, h11.Response):
                final = event

    if mode is ClientMode.EXPECT_WAIT:
        read()
    sent = 0
    chunk = b"\0" * chunk_size
    while sent < body_size and not (final is not None and _closes(final)):
        piece = chunk[: min(chunk_size, body_size - sent)]
        wire = client.send(h11.Data(data=piece))
        sent += len(piece)
        to_client += server.feed(wire)
        if mode is not ClientMode.NO_EXPECT:
            read()
    if sent == body_size:
        to_client += server.feed(client.send(h11.EndOfMessage()))
    read()
    if final is None:
        raise RuntimeError("server never sent a final response")
    return UploadTrace(tuple(interim), final.status_code, sent, server.reusable)


def _closes(response: h11.Response) -> bool:
    return any(n == b"connection" and b"close" in v.lower() for n, v in response.headers)


class BodyTooLargeError(Exception):
    """Raised inside `receive()` once a streamed body passes the policy limit."""


async def _send_reject(send: Send, status: int, headers: Iterable[Header]) -> None:
    start_headers = [*headers, (b"content-length", b"0"), CLOSE]
    await send({"type": "http.response.start", "status": status, "headers": start_headers})
    await send({"type": "http.response.body", "body": b""})


class ExpectAdmissionMiddleware:
    """Pure ASGI admission: reject before the first `receive()`, then cap the streamed body."""

    def __init__(self, app: ASGIApp, policy: UploadPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        verdict = admit(cast("list[Header]", scope["headers"]), self.policy)
        if not verdict.admitted:
            await _send_reject(send, verdict.status, verdict.headers)
            return
        received = 0
        started = False

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.policy.max_content_length:
                    raise BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, capped_receive, tracking_send)
        except BodyTooLargeError:
            if started:
                raise
            await _send_reject(send, 413, ())


async def _store_upload(request: Request) -> JSONResponse:
    body = await request.body()
    return JSONResponse({"received": len(body)}, status_code=201)


def make_upload_app(policy: UploadPolicy) -> Starlette:
    """`PUT /uploads`, guarded inside Starlette's `ServerErrorMiddleware` so a 413 beats a 500."""
    return Starlette(
        routes=[Route("/uploads", _store_upload, methods=["PUT"])],
        middleware=[Middleware(ExpectAdmissionMiddleware, policy=policy)],
    )


@dataclass(frozen=True, slots=True)
class ServerWire:
    """Bytes a real ASGI server's HTTP/1.1 protocol wrote, split at the end of the request head."""

    after_head: bytes
    total: bytes
    closed: bool  # the server closed the connection itself; the driver's hang-up is not counted


class _RecordingTransport(asyncio.Transport):
    """In-memory transport: records writes, never touches a socket."""

    def __init__(self) -> None:
        super().__init__()
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes | bytearray | memoryview[Any]) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None


async def _settle() -> None:
    for _ in range(_SETTLE_ROUNDS):
        await asyncio.sleep(0)


async def drive_uvicorn(
    app: ASGIApp,
    head: bytes,
    body: bytes = b"",
    *,
    http: Literal["h11", "httptools"] = "h11",
) -> ServerWire:
    """Feed one request to a Uvicorn HTTP/1.1 protocol in process; `body` follows the head."""
    config = UvicornConfig(app=app, http=http, lifespan="off", log_config=None)
    config.proxy_headers = False
    protocol_class = UvicornH11Protocol if http == "h11" else HttpToolsProtocol
    protocol = protocol_class(config, ServerState(), {}, _loop=asyncio.get_running_loop())
    transport = _RecordingTransport()
    protocol.connection_made(transport)
    protocol.data_received(head)
    await _settle()
    after_head = bytes(transport.written)
    if body and not transport.closed:
        protocol.data_received(body)
        await _settle()
    closed = transport.closed  # the server's own close, before the driver hangs up
    if not closed:
        protocol.connection_lost(None)
        await _settle()
    return ServerWire(after_head, bytes(transport.written), closed)


async def drive_hypercorn(app: ASGIApp, head: bytes) -> ServerWire:
    """Feed one request head to Hypercorn's h11 protocol in process and let the app answer."""
    written = bytearray()
    closed = False

    async def send(event: HypercornEvent) -> None:
        nonlocal closed
        if isinstance(event, RawData):
            written.extend(event.data)
        closed = closed or isinstance(event, Closed)

    config = HypercornConfig()
    config.include_date_header = False
    config.include_server_header = False
    async with TaskGroup(asyncio.get_running_loop()) as task_group:
        protocol = HypercornH11Protocol(
            ASGIWrapper(cast("ASGIFramework", app)),
            config,
            WorkerContext(None),
            task_group,
            ConnectionState({}),
            False,  # ssl
            None,
            None,
            send,
        )
        await protocol.handle(RawData(data=head))
        after_head = bytes(written)  # captured before the app task has run at all
        await _settle()
        if not closed:
            await protocol.handle(Closed())
    return ServerWire(after_head, bytes(written), closed)
