"""Hop-by-hop header stripping and the Connection header abuse (RFC 9110 Section 7.6.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from starlette.requests import Request
    from starlette.types import ASGIApp

Header = tuple[str, str]

_TOKEN_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)

# Connection itself plus the fields RFC 9110 Section 7.6.1 says intermediaries SHOULD remove
# whether or not they are listed as connection options.
KNOWN_HOP_BY_HOP: frozenset[str] = frozenset(
    {"connection", "proxy-connection", "keep-alive", "te", "transfer-encoding", "upgrade"}
)

# Fields only the edge may assert; a client-supplied copy is a spoof.
EDGE_ASSERTED: frozenset[str] = frozenset({"x-internal-auth"})

# End-to-end fields that a sender MUST NOT list as connection options (RFC 9110 Section 7.6.1).
# The hardened edge refuses such a request instead of silently deleting the field.
NEVER_CONNECTION_OPTIONS: frozenset[str] = frozenset(
    {"host", "content-length", "authorization", "cookie"}
)


class ConnectionOptionError(ValueError):
    """A Connection header field the edge refuses to honor; the edge answers 400."""


def connection_options(headers: Iterable[Header]) -> frozenset[str]:
    """Return the lowercased connection-option tokens from every Connection field line."""
    options: set[str] = set()
    for name, value in headers:
        if name.lower() != "connection":
            continue
        for element in value.split(","):
            option = element.strip(" \t").lower()
            if not option:
                continue  # empty list elements are ignored (RFC 9110 Section 5.6.1.2)
            if not set(option) <= _TOKEN_CHARS:
                raise ConnectionOptionError(f"connection-option is not a token: {option!r}")
            options.add(option)
    return frozenset(options)


def strip_hop_by_hop(headers: Sequence[Header]) -> list[Header]:
    """Remove Connection, every field it nominates, and the known hop-by-hop fields."""
    drop = KNOWN_HOP_BY_HOP | connection_options(headers)
    return [(name, value) for name, value in headers if name.lower() not in drop]


def naive_forward(headers: Sequence[Header], identity: str) -> list[Header]:
    """Vulnerable edge: drops spoofed identity, stamps its own, and only then strips."""
    cleaned = [(n, v) for n, v in headers if n.lower() not in EDGE_ASSERTED]
    stamped = [*cleaned, ("x-internal-auth", identity)]
    return strip_hop_by_hop(stamped)  # the client's Connection list now covers the stamp


def hardened_forward(headers: Sequence[Header], identity: str) -> list[Header]:
    """Safe edge: refuse end-to-end options, strip the client hop, then stamp last."""
    refused = connection_options(headers) & NEVER_CONNECTION_OPTIONS
    if refused:
        raise ConnectionOptionError(f"end-to-end fields listed in Connection: {sorted(refused)}")
    stripped = [(n, v) for n, v in strip_hop_by_hop(headers) if n.lower() not in EDGE_ASSERTED]
    return [*stripped, ("x-internal-auth", identity)]


def relay_connection_verbatim(headers: Sequence[Header], identity: str) -> list[Header]:
    """Non-compliant front tier: stamps identity and forwards the client's Connection as is."""
    cleaned = [(n, v) for n, v in headers if n.lower() not in EDGE_ASSERTED]
    return [*cleaned, ("x-internal-auth", identity)]


def build_backend(*, fail_closed: bool) -> Starlette:
    """Origin that authorizes `/admin` from the edge-asserted `X-Internal-Auth` field."""

    async def admin(request: Request) -> Response:
        identity = request.headers.get("x-internal-auth")
        if identity is None:
            if fail_closed:
                return JSONResponse({"error": "missing edge identity"}, status_code=401)
            identity = "mesh-service"  # fail-open: absence is read as an internal caller
        if identity == "anonymous":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        seen = [name.decode("latin-1") for name, _ in request.headers.raw]
        return JSONResponse({"identity": identity, "seen": seen})

    return Starlette(routes=[Route("/admin", admin)])


def build_edge_proxy(
    upstream: ASGIApp, policy: Callable[[Sequence[Header]], list[Header]]
) -> Starlette:
    """ASGI reverse proxy that rewrites request headers with *policy* and forwards GETs."""

    async def forward(request: Request) -> Response:
        inbound = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in request.headers.raw
            if name != b"host"
        ]
        try:
            outbound = policy(inbound)
        except ConnectionOptionError as exc:
            body = {"error": str(exc)}
            return JSONResponse(body, status_code=400, headers={"Connection": "close"})
        transport = httpx.ASGITransport(app=upstream)
        async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
            upstream_response = await client.get(request.url.path, headers=outbound)
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type"),
        )

    return Starlette(routes=[Route("/{path:path}", forward)])
