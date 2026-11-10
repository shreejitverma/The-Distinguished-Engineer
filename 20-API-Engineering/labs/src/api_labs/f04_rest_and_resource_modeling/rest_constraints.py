"""Fielding's REST constraints as executable invariants over in-process ASGI components.

Each piece maps to one constraint from Chapter 5 of Fielding's dissertation:
``build_origin`` is the server half of client-server, ``RoundRobin`` puts two
replicas behind a layer so statelessness becomes observable, ``SharedCache``
applies the cache constraint using only self-descriptive control data, and the
``links`` objects in every representation drive application state (hypermedia).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import httpx
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

PAGE_SIZE = 2
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})  # RFC 9110 Section 9.2.1
Headers = list[tuple[bytes, bytes]]


@dataclass
class Order:
    """One resource; its URI is assigned by the origin, never by the client."""

    id: int
    status: str = "pending"


@dataclass
class OrderStore:
    """Resource state shared by every replica, standing in for a database."""

    orders: dict[int, Order] = field(default_factory=dict[int, Order])

    @classmethod
    def with_orders(cls, count: int) -> OrderStore:
        return cls({i: Order(i) for i in range(1, count + 1)})


def _non_negative_int(text: str) -> int | None:
    """ASCII digits only: ``str.isdigit`` also accepts superscript two, which ``int`` rejects."""
    return int(text) if text.isascii() and text.isdigit() else None


def _order_body(order: Order, prefix: str) -> dict[str, Any]:
    links = {"self": f"{prefix}/{order.id}"}
    if order.status == "pending":
        links["cancel"] = f"{prefix}/{order.id}/cancel"
    return {"id": order.id, "status": order.status, "links": links}


def build_origin(
    store: OrderStore,
    *,
    prefix: str = "/orders",
    content_location: bool = True,
    max_age: int = 60,
) -> Starlette:
    """Stateless origin: every request carries all it needs (cursor in the URI)."""
    cache_control = {"Cache-Control": f"max-age={max_age}"}

    async def root(request: Request) -> Response:
        return JSONResponse({"links": {"orders": f"{prefix}?after=0"}}, headers=cache_control)

    async def collection(request: Request) -> Response:
        after = _non_negative_int(request.query_params.get("after", "0"))
        if after is None:
            return PlainTextResponse("after must be a non-negative integer", status_code=400)
        ids = sorted(i for i in store.orders if i > after)
        page = ids[:PAGE_SIZE]
        links: dict[str, str] = {}
        if len(ids) > PAGE_SIZE:
            links["next"] = f"{prefix}?after={page[-1]}"
        items = [_order_body(store.orders[i], prefix) for i in page]
        return JSONResponse({"items": items, "links": links})

    async def item(request: Request) -> Response:
        order = store.orders.get(request.path_params["order_id"])
        if order is None:
            return PlainTextResponse("no such order", status_code=404)
        if request.method == "DELETE":
            del store.orders[order.id]
            return Response(status_code=204)
        return JSONResponse(_order_body(order, prefix), headers=cache_control)

    async def cancel(request: Request) -> Response:
        order = store.orders.get(request.path_params["order_id"])
        if order is None:
            return PlainTextResponse("no such order", status_code=404)
        if order.status != "pending":
            return PlainTextResponse(f"order is {order.status}", status_code=409)
        order.status = "cancelled"
        headers = {"Content-Location": f"{prefix}/{order.id}"} if content_location else {}
        return JSONResponse(_order_body(order, prefix), headers=headers)

    return Starlette(
        routes=[
            Route("/", root, methods=["GET"]),
            Route(prefix, collection, methods=["GET"]),
            Route(prefix + "/{order_id:int}", item, methods=["GET", "DELETE"]),
            Route(prefix + "/{order_id:int}/cancel", cancel, methods=["POST"]),
        ]
    )


def build_session_pager(store: OrderStore, replica: str) -> Starlette:
    """Stateful anti-pattern: the paging cursor lives in this process, keyed by a cookie."""
    cursors: dict[str, int] = {}
    counter = itertools.count(1)

    async def next_page(request: Request) -> Response:
        sid = request.cookies.get("sid")
        if sid is None or sid not in cursors:
            sid = f"{replica}-{next(counter)}"
            cursors[sid] = 0
        start = cursors[sid]
        page = sorted(store.orders)[start : start + PAGE_SIZE]
        cursors[sid] = start + len(page)
        response = JSONResponse({"items": page})
        response.set_cookie("sid", sid)
        return response

    return Starlette(routes=[Route("/feed/next", next_page, methods=["GET"])])


class HitCounter:
    """Transparent layer that counts the HTTP requests reaching the wrapped app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.count = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.count += 1
        await self.app(scope, receive, send)


class RoundRobin:
    """Layered-system load balancer: each request goes to the next replica in turn."""

    def __init__(self, backends: Sequence[ASGIApp]) -> None:
        self._cycle = itertools.cycle(backends)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await next(self._cycle)(scope, receive, send)


def cache_directives(headers: Headers) -> dict[str, str | None]:
    """Parse every ``Cache-Control`` line into ``{directive: argument-or-None}``."""
    directives: dict[str, str | None] = {}
    for name, value in headers:
        if name.lower() != b"cache-control":
            continue
        for part in value.decode("latin-1").split(","):
            key, sep, arg = part.strip().partition("=")
            if key and key.lower() not in directives:  # first occurrence wins
                directives[key.lower()] = arg.strip('"') if sep else None
    return directives


def _header(headers: Headers, name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def freshness_lifetime(directives: dict[str, str | None]) -> int | None:
    """RFC 9111 Section 4.2.1 for a shared cache: s-maxage, then max-age."""
    for name in ("s-maxage", "max-age"):
        value = directives.get(name)
        if value is not None:
            return _non_negative_int(value) or 0  # invalid -> stale
    return None


def shared_storable(request_headers: Headers, status: int, response_headers: Headers) -> bool:
    """Subset of RFC 9111 Section 3 for a shared cache with no heuristic freshness."""
    directives = cache_directives(response_headers)
    if status != 200 or {"no-store", "no-cache", "private"} & directives.keys():
        return False  # no-cache: this lab cache never revalidates (RFC 9111 Section 5.2.2.4)
    if "no-store" in cache_directives(request_headers):
        return False  # RFC 9111 Section 5.2.1.5

    if _header(response_headers, b"vary") is not None:
        return False  # this lab cache does not build Vary secondary keys
    if _header(request_headers, b"authorization") is not None and not (
        {"public", "s-maxage", "must-revalidate"} & directives.keys()
    ):
        return False  # RFC 9111 Section 3.5
    return freshness_lifetime(directives) is not None


@dataclass
class _Entry:
    status: int
    headers: Headers
    body: bytes
    stored_at: float
    lifetime: int


@dataclass
class _Captured:
    status: int = 0
    headers: Headers = field(default_factory=list[tuple[bytes, bytes]])
    chunks: list[bytes] = field(default_factory=list[bytes])


def _cache_key(scope: Scope) -> str:
    path: str = scope["path"]
    query: bytes = scope.get("query_string", b"")
    return path + ("?" + query.decode("latin-1") if query else "")


class SharedCache:
    """Shared cache that decides everything from method, status, and header fields.

    It knows nothing about orders: the uniform interface is what lets it work.
    """

    def __init__(self, app: ASGIApp, clock: Callable[[], float]) -> None:
        self.app = app
        self.clock = clock
        self.entries: dict[str, _Entry] = {}
        self.hits = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = _cache_key(scope)
        request_headers: Headers = list(scope["headers"])
        method: str = scope["method"]
        authorized = _header(request_headers, b"authorization") is not None
        entry = self.entries.get(key) if method == "GET" and not authorized else None
        if entry is not None:
            age = int(self.clock() - entry.stored_at)
            if age < entry.lifetime:  # fresh while age has not reached the lifetime
                self.hits += 1
                headers = [*entry.headers, (b"age", str(age).encode())]
                await self._replay(send, entry.status, headers, entry.body)
                return
            del self.entries[key]

        captured = _Captured()

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured.status = message["status"]
                captured.headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                captured.chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        status, headers, body = captured.status, captured.headers, b"".join(captured.chunks)
        if method not in SAFE_METHODS and 200 <= status < 400:
            self._invalidate(key, headers)  # RFC 9111 Section 4.4
        elif method == "GET" and shared_storable(request_headers, status, headers):
            lifetime = freshness_lifetime(cache_directives(headers)) or 0
            self.entries[key] = _Entry(status, headers, body, self.clock(), lifetime)
        await self._replay(send, status, headers, body)

    def _invalidate(self, target: str, response_headers: Headers) -> None:
        self.entries.pop(target, None)
        for name in (b"location", b"content-location"):
            value = _header(response_headers, name)
            if value is not None and value.startswith("/"):  # same origin by construction
                self.entries.pop(value, None)

    @staticmethod
    async def _replay(send: Send, status: int, headers: Headers, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


async def hypermedia_order_ids(client: httpx.AsyncClient, entry: str = "/") -> list[int]:
    """Client that knows one URI and the link relations ``orders`` and ``next``."""
    href: str | None = (await client.get(entry)).raise_for_status().json()["links"]["orders"]
    ids: list[int] = []
    for _ in range(1000):
        if href is None:
            return ids
        page = (await client.get(href)).raise_for_status().json()
        ids.extend(item["id"] for item in page["items"])
        href = page["links"].get("next")
    raise RuntimeError("next links did not terminate")


async def session_order_ids(client: httpx.AsyncClient, max_requests: int) -> list[int]:
    """Client of the stateful pager: it relies on the server remembering its cursor."""
    ids: list[int] = []
    for _ in range(max_requests):
        page: list[int] = (await client.get("/feed/next")).raise_for_status().json()["items"]
        if not page:
            break
        ids.extend(page)
    return ids


def build_allow_demo(*, split_routes: bool) -> Starlette:
    """Same resource, two route layouts: one ``Route`` for all methods, or one per method."""

    async def read(request: Request) -> Response:
        return PlainTextResponse("item")

    async def delete(request: Request) -> Response:
        return Response(status_code=204)

    if split_routes:
        routes = [
            Route("/items/{item_id}", read, methods=["GET"]),
            Route("/items/{item_id}", delete, methods=["DELETE"]),
        ]
    else:

        async def both(request: Request) -> Response:
            return await (delete(request) if request.method == "DELETE" else read(request))

        routes = [Route("/items/{item_id}", both, methods=["GET", "DELETE"])]
    return Starlette(routes=routes)
