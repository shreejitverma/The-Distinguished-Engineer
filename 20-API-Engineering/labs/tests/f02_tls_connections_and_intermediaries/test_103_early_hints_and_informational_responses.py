"""103 Early Hints across h11, h2, http.client, urllib3, httpcore, Starlette, and three servers.

The servers are Hypercorn, Uvicorn, and Granian, each driven on a loopback socket.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import threading
from typing import TYPE_CHECKING, cast

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h11
import httpcore
import httpx
import pytest
import requests
import uvicorn
from granian.constants import HTTPModes, Interfaces
from granian.server.embed import Server as GranianServer
from hypercorn.asyncio import serve  # pyright: ignore[reportUnknownVariableType]
from hypercorn.config import Config
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Route
from urllib3.connection import HTTPConnection as Urllib3Connection

from api_labs.f02_tls_connections_and_intermediaries.early_hints import (
    PAGE,
    PRELOAD_LINKS,
    ReplaySocket,
    build_app,
    drive_asgi,
    h2_client_parse,
    h2_frames,
    h2_pair,
    h2_serve_with_hints,
    h11_client_parse,
    h11_serve,
    homepage,
    should_send_early_hints,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

NAV_GET = b"GET / HTTP/1.1\r\nhost: example.test\r\nsec-fetch-mode: navigate\r\n\r\n"
HINT_BLOCK = (
    b"HTTP/1.1 103 \r\n"
    b"link: </style.css>; rel=preload; as=style\r\n"
    b"link: </app.js>; rel=preload; as=script\r\n\r\n"
)
SECOND = b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: 7\r\n\r\nsecond!"
LINKS = [("link", link) for link in PRELOAD_LINKS]


# --- policy -------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version", "mode", "allow_http11", "expected"),
    [
        ("2", "navigate", False, True),
        ("3", "navigate", False, True),
        ("1.1", "navigate", False, False),
        ("1.1", "navigate", True, True),
        ("1.0", "navigate", True, False),
        ("2", "no-cors", False, False),
        ("2", None, False, False),
    ],
    ids=["h2-nav", "h3-nav", "h1-default", "h1-opt-in", "h10-never", "h2-subresource", "h2-none"],
)
def test_gate_never_hints_http10_defaults_off_for_http11_and_needs_navigation(
    version: str, mode: str | None, allow_http11: bool, expected: bool
) -> None:
    assert should_send_early_hints(version, mode, allow_http11=allow_http11) is expected


# --- HTTP/1.1 wire (h11) ------------------------------------------------------------------------


@pytest.mark.unit
def test_h11_wire_puts_a_bodyless_103_head_before_the_final_status_line() -> None:
    wire = h11_serve(NAV_GET, PRELOAD_LINKS)
    # h11 omits the optional reason phrase; the 103 ends at the first empty line.
    assert wire.startswith(HINT_BLOCK + b"HTTP/1.1 200 OK\r\n")
    assert wire.count(b"HTTP/1.1 ") == 2
    assert wire.endswith(b"\r\n\r\n" + PAGE)


@pytest.mark.unit
def test_h11_client_reports_interim_then_final_response() -> None:
    parsed = h11_client_parse(h11_serve(NAV_GET, PRELOAD_LINKS))
    assert parsed.interim == [(103, LINKS)]
    assert parsed.status == 200
    assert parsed.body == PAGE
    assert [h for h in parsed.headers if h[0] == "link"] == LINKS


@pytest.mark.unit
def test_hint_fields_do_not_become_final_response_fields() -> None:
    final_without_link = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok"
    parsed = h11_client_parse(HINT_BLOCK + final_without_link)
    assert parsed.interim[0][1] == LINKS
    assert [name for name, _ in parsed.headers] == ["content-length"]


@pytest.mark.unit
def test_h11_will_send_103_to_an_http10_client_so_the_gate_lives_above_it() -> None:
    http10 = NAV_GET.replace(b"HTTP/1.1", b"HTTP/1.0")
    raw = h11.Connection(h11.SERVER)
    raw.receive_data(http10)
    raw.next_event()
    emitted = raw.send(h11.InformationalResponse(status_code=103, headers=LINKS))
    assert emitted is not None
    assert emitted.startswith(b"HTTP/1.1 103 ")
    assert h11_serve(http10, PRELOAD_LINKS).startswith(b"HTTP/1.1 200 OK\r\n")


@pytest.mark.unit
def test_h11_http11_hints_are_opt_in_and_absent_without_navigation() -> None:
    assert h11_serve(NAV_GET, PRELOAD_LINKS, allow_http11=False).startswith(b"HTTP/1.1 200 OK")
    subresource = NAV_GET.replace(b"navigate", b"no-cors")
    assert h11_serve(subresource, PRELOAD_LINKS).startswith(b"HTTP/1.1 200 OK")


@pytest.mark.unit
def test_h11_refuses_an_informational_response_after_the_final_one() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(NAV_GET)
    conn.next_event()
    conn.send(h11.Response(status_code=200, headers=[("content-length", "0")]))
    with pytest.raises(h11.LocalProtocolError):
        conn.send(h11.InformationalResponse(status_code=103, headers=LINKS))


@pytest.mark.unit
def test_h11_refuses_101_without_an_upgrade_request() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(NAV_GET)
    conn.next_event()
    with pytest.raises(h11.LocalProtocolError):
        conn.send(h11.InformationalResponse(status_code=101, headers=[("upgrade", "h2c")]))


@pytest.mark.unit
def test_h11_serve_refuses_a_partial_request_head() -> None:
    with pytest.raises(TypeError, match="NEED_DATA"):
        h11_serve(NAV_GET[:-2], PRELOAD_LINKS)


@pytest.mark.unit
def test_a_103_alone_is_not_a_complete_response() -> None:
    with pytest.raises(RuntimeError, match="NEED_DATA"):
        h11_client_parse(HINT_BLOCK)


# --- HTTP/1.1 clients that are not h11 ----------------------------------------------------------


def _stdlib_pair(segments: list[bytes]) -> tuple[bytes, int, bytes, int, bytes]:
    conn = http.client.HTTPConnection("example.test")
    replay = ReplaySocket(segments)
    conn.sock = cast("socket.socket", replay)
    conn.request("GET", "/")
    first = conn.getresponse()
    first_status, first_body = first.status, first.read()
    conn.request("GET", "/second")
    second = conn.getresponse()
    return bytes(replay.sent), first_status, first_body, second.status, second.read()


@pytest.mark.unit
def test_http_client_takes_103_as_final_and_hands_the_200_to_the_next_request() -> None:
    wire = h11_serve(NAV_GET, PRELOAD_LINKS)
    # The 103 and the 200 arrive in separate TCP reads, as they do when the origin is slow.
    sent, s1, b1, s2, b2 = _stdlib_pair([wire[: len(HINT_BLOCK)], wire[len(HINT_BLOCK) :], SECOND])
    assert sent.count(b"GET ") == 2
    assert (s1, b1) == (103, b"")
    assert (s2, b2) == (200, PAGE)


@pytest.mark.unit
def test_http_client_loses_the_real_response_when_103_and_200_share_a_read() -> None:
    _, s1, b1, s2, b2 = _stdlib_pair([h11_serve(NAV_GET, PRELOAD_LINKS), SECOND])
    assert (s1, b1) == (103, b"")
    assert (s2, b2) == (200, b"second!")


@pytest.mark.unit
def test_urllib3_inherits_the_http_client_misparse() -> None:
    wire = h11_serve(NAV_GET, PRELOAD_LINKS)
    conn = Urllib3Connection("example.test")
    segments = [wire[: len(HINT_BLOCK)], wire[len(HINT_BLOCK) :]]
    conn.sock = cast("socket.socket", ReplaySocket(segments))
    conn.request("GET", "/")
    response = conn.getresponse()
    assert response.status == 103
    assert response.data == b""


def _serve_raw_once(listener: socket.socket, replies: list[list[bytes]]) -> None:
    """Accept one keep-alive connection; per request, write each reply segment separately."""
    conn, _ = listener.accept()
    with conn:
        for segments in replies:
            conn.recv(65535)
            for segment in segments:
                conn.sendall(segment)


@pytest.mark.socket
def test_requests_session_inherits_the_shift_by_one_on_a_real_socket() -> None:
    wire = h11_serve(NAV_GET, PRELOAD_LINKS)
    listener = _listening_socket()
    port = listener.getsockname()[1]
    # A slow origin: the 200 for "/" is written only after the client has sent "/second",
    # so the two responses can never share one read and the outcome is deterministic.
    replies = [[wire[: len(HINT_BLOCK)]], [wire[len(HINT_BLOCK) :], SECOND]]
    server = threading.Thread(target=_serve_raw_once, args=(listener, replies))
    server.start()
    try:
        with requests.Session() as session:
            first = session.get(f"http://127.0.0.1:{port}/", timeout=5)
            second = session.get(f"http://127.0.0.1:{port}/second", timeout=5)
    finally:
        server.join(5)
        listener.close()
    assert not server.is_alive()
    assert (first.status_code, first.content) == (103, b"")
    assert (second.status_code, second.content) == (200, PAGE)


@pytest.mark.unit
def test_httpcore_skips_103_and_exposes_only_final_fields() -> None:
    final_without_link = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok"
    backend = httpcore.MockBackend([HINT_BLOCK, final_without_link])
    with httpcore.ConnectionPool(network_backend=backend) as pool:
        response = pool.request("GET", "http://example.test/")
    assert response.status == 200
    assert response.content == b"ok"
    assert response.headers == [(b"content-length", b"2")]


# --- HTTP/2 (h2) --------------------------------------------------------------------------------

HEADERS, DATA = 0x1, 0x0
END_STREAM, END_HEADERS = 0x1, 0x4


@pytest.mark.unit
def test_h2_103_is_a_headers_frame_without_end_stream() -> None:
    _, server = h2_pair()
    frames = h2_frames(h2_serve_with_hints(server, PRELOAD_LINKS))
    assert frames == [(HEADERS, END_HEADERS, 1), (HEADERS, END_HEADERS, 1), (DATA, END_STREAM, 1)]


@pytest.mark.unit
def test_h2_client_sees_the_informational_event_before_the_response() -> None:
    client, server = h2_pair()
    events = client.receive_data(h2_serve_with_hints(server, PRELOAD_LINKS))
    assert [type(e).__name__ for e in events] == [
        "InformationalResponseReceived",
        "ResponseReceived",
        "DataReceived",
        "StreamEnded",
    ]
    parsed = h2_client_parse(events)
    assert parsed.interim == [(103, LINKS)]
    assert (parsed.status, parsed.body) == (200, PAGE)


@pytest.mark.unit
def test_h2_refuses_end_stream_on_an_informational_response() -> None:
    _, server = h2_pair()
    with pytest.raises(h2.exceptions.ProtocolError, match="END_STREAM"):
        server.send_headers(1, [(":status", "103"), *LINKS], end_stream=True)


@pytest.mark.unit
def test_h2_refuses_an_informational_response_after_the_final_one() -> None:
    _, server = h2_pair()
    server.send_headers(1, [(":status", "200")])
    with pytest.raises(h2.exceptions.ProtocolError, match="after final response"):
        server.send_headers(1, [(":status", "103"), *LINKS])


@pytest.mark.unit
def test_h2_441_does_not_reject_status_101_on_send_or_receive() -> None:
    client, server = h2_pair()
    server.send_headers(1, [(":status", "101")])
    events = client.receive_data(server.data_to_send())
    assert [type(e) for e in events] == [h2.events.InformationalResponseReceived]


# --- ASGI ---------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_version", "advertise", "expected"),
    [
        ("2", True, ["http.response.early_hint", "http.response.start", "http.response.body"]),
        ("2", False, ["http.response.start", "http.response.body"]),
        ("1.1", True, ["http.response.start", "http.response.body"]),
    ],
    ids=["h2-advertised", "h2-not-advertised", "h1-gated"],
)
async def test_starlette_sends_the_hint_message_first_and_only_when_allowed(
    http_version: str, advertise: bool, expected: list[str]
) -> None:
    sent = await drive_asgi(build_app(), http_version=http_version, advertise=advertise)
    assert [m["type"] for m in sent] == expected
    if advertise and http_version == "2":
        assert list(sent[0]["links"]) == [link.encode() for link in PRELOAD_LINKS]


class _Passthrough(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        return await call_next(request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_base_and_gzip_middleware_forward_the_hint_message() -> None:
    app = Starlette(
        routes=[Route("/", homepage)],
        middleware=[Middleware(GZipMiddleware, minimum_size=1), Middleware(_Passthrough)],
    )
    sent = await drive_asgi(
        app, http_version="2", advertise=True, extra_headers=[(b"accept-encoding", b"gzip")]
    )
    assert [m["type"] for m in sent][:2] == ["http.response.early_hint", "http.response.start"]
    start_headers = dict(sent[1]["headers"])
    assert start_headers[b"content-encoding"] == b"gzip"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_transport_does_not_advertise_so_the_final_response_must_carry_links() -> None:
    seen: list[str] = []
    inner = build_app()

    async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
        async def record(message: Message) -> None:
            seen.append(message["type"])
            await send(message)

        assert "http.response.early_hint" not in scope.get("extensions", {})
        await inner(scope, receive, record)

    transport = httpx.ASGITransport(app=recorder)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", headers={"sec-fetch-mode": "navigate"})
    assert response.status_code == 200
    assert response.headers["link"] == ", ".join(PRELOAD_LINKS)
    assert seen == ["http.response.start", "http.response.body"]


# --- real servers on loopback -------------------------------------------------------------------


async def _unguarded(scope: Scope, receive: Receive, send: Send) -> None:
    """Sends the hint without checking scope["extensions"]; ASGI says servers advertise it."""
    del scope, receive
    await send({"type": "http.response.early_hint", "links": [b"</a.css>; rel=preload"]})
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"x"})


def _listening_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)  # connects queue in the backlog before the server starts accepting
    return sock


async def _http11_roundtrip(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(NAV_GET.replace(b"\r\n\r\n", b"\r\nconnection: close\r\n\r\n"))
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    return data


async def _h2c_roundtrip(port: int) -> list[h2.events.Event]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    request = [(":method", "GET"), (":scheme", "http"), (":authority", "t"), (":path", "/")]
    conn.send_headers(1, [*request, ("sec-fetch-mode", "navigate")], end_stream=True)
    writer.write(conn.data_to_send())
    events: list[h2.events.Event] = []
    while not any(isinstance(e, h2.events.StreamEnded) for e in events):
        data = await reader.read(65535)
        if not data:
            break
        events += conn.receive_data(data)
        writer.write(conn.data_to_send())
    writer.close()
    await writer.wait_closed()
    return events


async def _with_hypercorn[T](app: ASGIApp, client: Callable[[int], Awaitable[T]]) -> T:
    sock = _listening_socket()
    port = sock.getsockname()[1]
    config = Config()
    config.bind = [f"fd://{sock.detach()}"]  # Hypercorn now owns and closes the descriptor
    config.accesslog = None
    config.errorlog = None
    stop = asyncio.Event()
    server = asyncio.create_task(serve(app, config, shutdown_trigger=stop.wait))  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(10):
            return await client(port)
    finally:
        stop.set()
        await server


async def _with_uvicorn[T](app: ASGIApp, client: Callable[[int], Awaitable[T]]) -> T:
    sock = _listening_socket()
    config = uvicorn.Config(app, http="h11", lifespan="off", log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            return await client(sock.getsockname()[1])
    finally:
        server.should_exit = True
        await task
        sock.close()


async def _with_granian[T](
    app: ASGIApp, client: Callable[[int], Awaitable[T]], http: HTTPModes = HTTPModes.http1
) -> T:
    # Granian binds in Rust, so pick a free loopback port and retry until it listens.
    probe = _listening_socket()
    port = probe.getsockname()[1]
    probe.close()
    server = GranianServer(
        app,
        address="127.0.0.1",
        port=port,
        interface=Interfaces.ASGINL,
        http=http,
        websockets=False,
        log_enabled=False,
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    return await client(port)
                except ConnectionRefusedError:
                    await asyncio.sleep(0.01)
    finally:
        server.stop()  # type: ignore[no-untyped-call]
        await task


@pytest.mark.socket
@pytest.mark.asyncio
async def test_hypercorn_http11_sends_no_103_and_the_page_still_works() -> None:
    parsed = h11_client_parse(await _with_hypercorn(build_app(), _http11_roundtrip))
    assert parsed.interim == []
    assert (parsed.status, parsed.body) == (200, PAGE)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_hypercorn_h2c_sends_103_with_links_before_the_200() -> None:
    events = await _with_hypercorn(build_app(), _h2c_roundtrip)
    parsed = h2_client_parse(events)
    assert len(parsed.interim) == 1
    status, fields = parsed.interim[0]
    assert status == 103
    assert [f for f in fields if f[0] == "link"] == LINKS
    assert (parsed.status, parsed.body) == (200, PAGE)
    kinds = [type(e) for e in events]
    first_hint = kinds.index(h2.events.InformationalResponseReceived)
    assert first_hint < kinds.index(h2.events.ResponseReceived)


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["hypercorn", "uvicorn", "granian"])
async def test_unguarded_hint_message_on_http11_turns_into_500(server: str) -> None:
    runners = {"hypercorn": _with_hypercorn, "uvicorn": _with_uvicorn, "granian": _with_granian}
    parsed = h11_client_parse(await runners[server](_unguarded, _http11_roundtrip))
    assert parsed.interim == []
    assert parsed.status == 500


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_h11_has_no_extensions_so_starlette_hints_are_a_noop() -> None:
    scopes: list[Scope] = []
    inner = build_app()

    async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
        scopes.append(scope)
        await inner(scope, receive, send)

    parsed = h11_client_parse(await _with_uvicorn(recorder, _http11_roundtrip))
    assert "extensions" not in scopes[0]
    assert parsed.interim == []
    assert (parsed.status, parsed.body) == (200, PAGE)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_granian_advertises_no_early_hint_on_http11_or_h2c() -> None:
    scopes: list[Scope] = []
    inner = build_app()

    async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
        scopes.append(scope)
        await inner(scope, receive, send)

    h1 = h11_client_parse(await _with_granian(recorder, _http11_roundtrip))
    h2_events = await _with_granian(recorder, _h2c_roundtrip, HTTPModes.http2)
    h2_parsed = h2_client_parse(h2_events)
    assert [s["http_version"] for s in scopes] == ["1.1", "2"]
    assert all("http.response.early_hint" not in s.get("extensions", {}) for s in scopes)
    assert (h1.interim, h1.status, h1.body) == ([], 200, PAGE)
    assert (h2_parsed.interim, h2_parsed.status, h2_parsed.body) == ([], 200, PAGE)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_granian_turns_an_unguarded_hint_message_into_500_on_h2c() -> None:
    parsed = h2_client_parse(await _with_granian(_unguarded, _h2c_roundtrip, HTTPModes.http2))
    assert parsed.interim == []
    assert parsed.status == 500
