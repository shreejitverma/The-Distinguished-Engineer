"""Header-name and duplicate-header disagreement across proxy tiers.

Real components (no sockets): uvicorn's h11 and httptools protocol classes, Gunicorn's
request parser and WSGI environ builder, the h2 state machine, Starlette ``Headers``, and
FastAPI ``Header``.
Pure models (each cites the source it mirrors): the nginx ``$http_*`` variable lookup, an
underscore policy shared by nginx, Envoy and Gunicorn, a gateway that overwrites a trusted
header, and RFC 9110 Section 7.6.1 hop-by-hop stripping.
"""

from __future__ import annotations

import asyncio
import io
import json
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Any, Literal

import h2.config
import h2.connection
import h2.events
from fastapi import FastAPI, Header
from gunicorn.config import Config as GunicornConfig
from gunicorn.http import wsgi
from gunicorn.http.parser import RequestParser
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.config import Config as UvicornConfig
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
from uvicorn.server import ServerState

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

RawHeaders = list[tuple[bytes, bytes]]
UvicornParser = Literal["h11", "httptools"]
HeaderMap = Literal["drop", "refuse", "dangerous"]

CLIENT_ADDR = ("127.0.0.1", 50000)
SERVER_ADDR = ("127.0.0.1", 8000)


class UnderscoreAction(Enum):
    """What a tier does with a request header whose name contains an underscore."""

    ALLOW = "allow"
    DROP = "drop"
    REJECT = "reject"


class UnderscoreRejected(Exception):
    """Raised when a tier refuses the whole request (Envoy REJECT_REQUEST, HTTP 400)."""


def apply_underscore_policy(headers: RawHeaders, action: UnderscoreAction) -> RawHeaders:
    """Model of the nginx default, Envoy underscore action, and Gunicorn ``header_map``.

    nginx 1.31.10 src/http/ngx_http_parse.c ``ngx_http_parse_header_line`` marks such a header
    invalid, and ``ignore_invalid_headers on`` (the default) skips it, which is ``DROP``.
    """
    if action is UnderscoreAction.ALLOW:
        return list(headers)
    kept: RawHeaders = []
    for name, value in headers:
        if b"_" in name:
            if action is UnderscoreAction.REJECT:
                raise UnderscoreRejected(name.decode("latin-1"))
            continue
        kept.append((name, value))
    return kept


def lookup_first(headers: RawHeaders, name: str) -> str | None:
    """First field line with this name, case-insensitively (Starlette ``Headers.get``)."""
    want = name.lower().encode("latin-1")
    for key, value in headers:
        if key.lower() == want:
            return value.decode("latin-1")
    return None


def lookup_combined(headers: RawHeaders, name: str) -> str | None:
    """RFC 9110 Section 5.2 combined field value: every line, in order, joined by ", "."""
    want = name.lower().encode("latin-1")
    values = [v.decode("latin-1") for k, v in headers if k.lower() == want]
    return ", ".join(values) if values else None


def nginx_http_variable(headers: RawHeaders, variable: str) -> str | None:
    """Model of nginx ``$http_<name>`` (src/http/ngx_http_variables.c, unknown_header lookup).

    Each received name is lowercased and ``-`` becomes ``_`` before comparison, so
    ``X-Tenant-Id`` and ``X_Tenant_Id`` both match ``$http_x_tenant_id``.
    Matches are joined by ", ".
    """
    suffix = variable.removeprefix("http_").encode("ascii")
    values = [
        value.decode("latin-1")
        for name, value in headers
        if name.lower().replace(b"-", b"_") == suffix
    ]
    return ", ".join(values) if values else None


def overwrite_trusted(headers: RawHeaders, name: str, value: str) -> RawHeaders:
    """A gateway that sets a trusted header: drop every exact-name match, append its own value.

    This is correct for the exact name and blind to the underscore spelling.
    """
    want = name.lower().encode("latin-1")
    kept = [(k, v) for k, v in headers if k.lower() != want]
    kept.append((want, value.encode("latin-1")))
    return kept


def strip_connection_listed(headers: RawHeaders) -> RawHeaders:
    """RFC 9110 Section 7.6.1: remove each field named in ``Connection``, then ``Connection``."""
    listed: set[bytes] = {b"connection"}
    for name, value in headers:
        if name.lower() == b"connection":
            listed.update(t.strip().lower() for t in value.split(b",") if t.strip())
    return [(k, v) for k, v in headers if k.lower() not in listed]


class _MockTransport(asyncio.Transport):
    """The smallest transport uvicorn's protocol classes accept."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = bytearray()
        self.closed = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return {"sockname": SERVER_ADDR, "peername": CLIENT_ADDR}.get(name, default)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.buffer += data

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        return None

    def resume_reading(self) -> None:
        return None

    def is_reading(self) -> bool:
        return True

    def get_write_buffer_size(self) -> int:
        return 0


async def serve_once(
    app: ASGIApp, raw_request: bytes, parser: UvicornParser = "h11"
) -> tuple[RawHeaders, bytes]:
    """Feed wire bytes to a real uvicorn protocol; return (ASGI scope headers, response bytes)."""
    scope_headers: RawHeaders = []
    done = asyncio.Event()

    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        scope_headers.extend(scope["headers"])
        await app(scope, receive, send)
        done.set()

    config = UvicornConfig(wrapper, log_level="critical", lifespan="off")
    config.load()
    protocol_class = H11Protocol if parser == "h11" else HttpToolsProtocol
    protocol = protocol_class(
        config=config,
        server_state=ServerState(),
        app_state={},
        _loop=asyncio.get_running_loop(),
    )
    transport = _MockTransport()
    protocol.connection_made(transport)  # type: ignore[arg-type]
    protocol.data_received(raw_request)
    await asyncio.wait_for(done.wait(), timeout=5)
    protocol.connection_lost(None)
    return scope_headers, bytes(transport.buffer)


def response_body(response_bytes: bytes) -> bytes:
    """Body of a single non-chunked HTTP/1.1 response written by uvicorn."""
    return response_bytes.split(b"\r\n\r\n", 1)[1]


async def probe_through_uvicorn(
    raw_request: bytes, parser: UvicornParser = "h11"
) -> dict[str, Any]:
    """Wire bytes -> uvicorn -> Starlette ``Headers`` -> JSON the app produced."""
    _, response = await serve_once(build_probe_app(), raw_request, parser)
    result: dict[str, Any] = json.loads(response_body(response))
    return result


def build_probe_app() -> Starlette:
    """Starlette app that reports how ``request.headers`` sees the tenant header spellings."""

    async def probe(request: Request) -> JSONResponse:
        headers = request.headers
        return JSONResponse(
            {
                "first": headers.get("x-tenant-id"),
                "all": headers.getlist("x-tenant-id"),
                "underscore": headers.getlist("x_tenant_id"),
                "via_dict_of_mapping": dict(headers).get("x-tenant-id"),
                "via_dict_of_items": dict(headers.items()).get("x-tenant-id"),
                "len": len(headers),
            }
        )

    return Starlette(routes=[Route("/probe", probe)])


def build_fastapi_app() -> FastAPI:
    """FastAPI app with a converted header parameter and a literal-underscore one."""
    app = FastAPI()

    @app.get("/converted")
    async def converted(
        x_tenant_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, str | None]:
        return {"value": x_tenant_id}

    @app.get("/literal")
    async def literal(
        x_tenant_id: Annotated[str | None, Header(convert_underscores=False)] = None,
    ) -> dict[str, str | None]:
        return {"value": x_tenant_id}

    return app


def gunicorn_environ(raw_request: bytes, header_map: HeaderMap) -> dict[str, str]:
    """Wire bytes -> Gunicorn's parser -> WSGI environ; return only the ``HTTP_*`` keys."""
    config = GunicornConfig()
    config.set("header_map", header_map)
    request = next(iter(RequestParser(config, io.BytesIO(raw_request), CLIENT_ADDR)))
    _, environ = wsgi.create(request, io.BytesIO(), CLIENT_ADDR, SERVER_ADDR, config)
    return {k: v for k, v in environ.items() if k.startswith("HTTP_")}


def h2_deliver(
    fields: Iterable[tuple[bytes, bytes]], *, client_validates: bool = True
) -> RawHeaders:
    """Send a request over h2 client -> server state machines; return the fields the server sees."""
    pseudo = [(b":method", b"GET"), (b":path", b"/"), (b":scheme", b"https"), (b":authority", b"x")]
    client = h2.connection.H2Connection(
        h2.config.H2Configuration(
            client_side=True,
            header_encoding=None,
            validate_outbound_headers=client_validates,
            normalize_outbound_headers=False,
        )
    )
    server = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=False, header_encoding=None)
    )
    client.initiate_connection()
    server.receive_data(client.data_to_send())
    client.send_headers(1, pseudo + list(fields), end_stream=True)
    for event in server.receive_data(client.data_to_send()):
        if isinstance(event, h2.events.RequestReceived):
            return [(bytes(k), bytes(v)) for k, v in event.headers or []]
    raise AssertionError("server saw no RequestReceived event")


def render_request(headers: RawHeaders, *, target: bytes = b"/probe") -> bytes:
    """HTTP/1.1 request bytes for a header list, exactly as given (names are not normalized)."""
    lines = [b"GET " + target + b" HTTP/1.1", b"Host: example.test"]
    lines.extend(name + b": " + value for name, value in headers)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"
