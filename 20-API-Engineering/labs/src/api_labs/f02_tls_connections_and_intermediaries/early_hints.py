"""103 Early Hints and informational 1xx responses (RFC 8297, RFC 9110 Section 15.2).

Server and client halves for HTTP/1.1 (h11), HTTP/2 (h2), the stdlib http.client
parser, and an ASGI app that uses the ``http.response.early_hint`` extension.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import h2.config
import h2.connection
import h2.events
import h11
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Scope

EARLY_HINTS = 103
PRELOAD_LINKS: tuple[str, ...] = (
    "</style.css>; rel=preload; as=style",
    "</app.js>; rel=preload; as=script",
)
PAGE = b"<!doctype html><title>ok</title>"


def should_send_early_hints(
    http_version: str, sec_fetch_mode: str | None, *, allow_http11: bool = False
) -> bool:
    """Decide whether a 103 is worth sending and safe to send on this hop.

    - HTTP/1.0 never: a server MUST NOT send 1xx to an HTTP/1.0 client (RFC 9110 Section 15.2).
    - HTTP/1.1 only when the operator knows the clients parse 1xx (RFC 8297 Section 3).
    - Only navigations: browsers act on hints for the top-level document only.
    """
    if sec_fetch_mode != "navigate":
        return False
    if http_version in {"2", "3"}:
        return True
    return http_version == "1.1" and allow_http11


# --- HTTP/1.1 with h11 -------------------------------------------------------------------------


def _h11_send(conn: h11.Connection, event: h11.Event) -> bytes:
    data = conn.send(event)
    return data if data is not None else b""


def h11_serve(request: bytes, links: Sequence[str], *, allow_http11: bool = True) -> bytes:
    """Parse one request and return the server's wire bytes: optional 103, then 200 OK."""
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(request)
    event = conn.next_event()
    if not isinstance(event, h11.Request):
        raise TypeError(f"expected a complete request head, got {event!r}")
    version = event.http_version.decode("ascii")
    mode = next((v.decode() for k, v in event.headers if k == b"sec-fetch-mode"), None)
    out = bytearray()
    if links and should_send_early_hints(version, mode, allow_http11=allow_http11):
        hints = [("link", link) for link in links]
        out += _h11_send(conn, h11.InformationalResponse(status_code=EARLY_HINTS, headers=hints))
    final = [("content-type", "text/html"), ("content-length", str(len(PAGE)))]
    final += [("link", link) for link in links]
    out += _h11_send(conn, h11.Response(status_code=200, headers=final, reason=b"OK"))
    out += _h11_send(conn, h11.Data(data=PAGE))
    out += _h11_send(conn, h11.EndOfMessage())
    return bytes(out)


@dataclass
class ParsedResponse:
    """What a client saw: every interim response in order, then the final one."""

    interim: list[tuple[int, list[tuple[str, str]]]] = field(default_factory=lambda: [])
    status: int = 0
    headers: list[tuple[str, str]] = field(default_factory=lambda: [])
    body: bytes = b""


def _decoded(headers: Sequence[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers]


def h11_client_parse(wire: bytes) -> ParsedResponse:
    """Feed server bytes to an h11 client that sent one GET and collect its events."""
    conn = h11.Connection(h11.CLIENT)
    _h11_send(conn, h11.Request(method="GET", target="/", headers=[("host", "example.test")]))
    _h11_send(conn, h11.EndOfMessage())
    conn.receive_data(wire)
    parsed = ParsedResponse()
    while True:
        event = conn.next_event()
        if isinstance(event, h11.InformationalResponse):
            parsed.interim.append((event.status_code, _decoded(event.headers)))
        elif isinstance(event, h11.Response):
            parsed.status, parsed.headers = event.status_code, _decoded(event.headers)
        elif isinstance(event, h11.Data):
            parsed.body += event.data
        elif isinstance(event, h11.EndOfMessage):
            return parsed
        else:
            raise RuntimeError(f"incomplete response, h11 returned {event!r}")


# --- HTTP/1.1 with the stdlib parser ------------------------------------------------------------


class _SegmentReader(io.RawIOBase):
    """Raw reader over a shared queue of TCP segments: one read never crosses a segment."""

    def __init__(self, segments: list[bytes]) -> None:
        self._segments = segments

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        if not self._segments:
            return 0
        head = self._segments[0]
        n = min(len(buffer), len(head))
        buffer[:n] = head[:n]
        if n == len(head):
            self._segments.pop(0)
        else:
            self._segments[0] = head[n:]
        return n


class ReplaySocket:
    """Socket double for ``http.client``: records what is sent, replays segments in order.

    Like ``socket.makefile``, each call returns a new buffered reader over the same stream,
    so a response object that closes its reader does not lose bytes still in later segments.
    """

    def __init__(self, segments: Sequence[bytes]) -> None:
        self._segments = list(segments)
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def settimeout(self, timeout: float | None) -> None:
        del timeout

    def makefile(self, mode: str, buffering: int = -1) -> io.BufferedReader:
        del mode, buffering
        return io.BufferedReader(_SegmentReader(self._segments))


# --- HTTP/2 with h2 -----------------------------------------------------------------------------

REQUEST_HEADERS: list[tuple[str, str]] = [
    (":method", "GET"),
    (":scheme", "https"),
    (":authority", "example.test"),
    (":path", "/"),
    ("sec-fetch-mode", "navigate"),
]


def h2_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    """A connected client and server with stream 1 open and the request fully sent."""
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    client.send_headers(1, REQUEST_HEADERS, end_stream=True)
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    return client, server


def h2_serve_with_hints(server: h2.connection.H2Connection, links: Sequence[str]) -> bytes:
    """Server side: one HEADERS frame for 103, one for 200, then DATA with END_STREAM."""
    server.send_headers(1, [(":status", str(EARLY_HINTS)), *(("link", link) for link in links)])
    final = [(":status", "200"), ("content-type", "text/html")]
    server.send_headers(1, final + [("link", link) for link in links])
    server.send_data(1, PAGE, end_stream=True)
    return server.data_to_send()


def h2_frames(wire: bytes) -> list[tuple[int, int, int]]:
    """Split HTTP/2 bytes into (type, flags, stream id) per 9-octet frame header (RFC 9113 4.1)."""
    frames: list[tuple[int, int, int]] = []
    offset = 0
    while offset < len(wire):
        length = int.from_bytes(wire[offset : offset + 3], "big")
        frame_type, flags = wire[offset + 3], wire[offset + 4]
        stream_id = int.from_bytes(wire[offset + 5 : offset + 9], "big") & 0x7FFFFFFF
        frames.append((frame_type, flags, stream_id))
        offset += 9 + length
    return frames


def h2_client_parse(events: Sequence[h2.events.Event]) -> ParsedResponse:
    """Fold h2 client events for stream 1 into a ParsedResponse."""
    parsed = ParsedResponse()
    for event in events:
        if isinstance(event, h2.events.InformationalResponseReceived) and event.headers:
            headers = _decoded(event.headers)
            status = int(dict(headers)[":status"])
            parsed.interim.append((status, [(k, v) for k, v in headers if k[0] != ":"]))
        elif isinstance(event, h2.events.ResponseReceived) and event.headers:
            headers = _decoded(event.headers)
            parsed.status = int(dict(headers)[":status"])
            parsed.headers = [(k, v) for k, v in headers if k[0] != ":"]
        elif isinstance(event, h2.events.DataReceived) and event.data:
            parsed.body += event.data
    return parsed


# --- ASGI: the http.response.early_hint extension -----------------------------------------------


async def homepage(request: Request) -> HTMLResponse:
    """Send hints first (a no-op unless the server advertises the extension), then the page."""
    mode = request.headers.get("sec-fetch-mode")
    if should_send_early_hints(request.scope["http_version"], mode):
        await request.send_early_hints(PRELOAD_LINKS[0], *PRELOAD_LINKS[1:])
    # The final response repeats the links: hints never replace final fields (RFC 8297 Section 2).
    return HTMLResponse(PAGE, headers={"link": ", ".join(PRELOAD_LINKS)})


def build_app() -> Starlette:
    return Starlette(routes=[Route("/", homepage)])


async def drive_asgi(
    app: ASGIApp,
    *,
    http_version: str,
    advertise: bool,
    extra_headers: Sequence[tuple[bytes, bytes]] = (),
) -> list[Message]:
    """Call the app once like a server would and return every message it sent, in order."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": http_version,
        "method": "GET",
        "scheme": "https",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"example.test"), (b"sec-fetch-mode", b"navigate"), *extra_headers],
        "client": ("192.0.2.1", 50000),
        "server": ("example.test", 443),
        "extensions": {"http.response.early_hint": {}} if advertise else {},
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent
