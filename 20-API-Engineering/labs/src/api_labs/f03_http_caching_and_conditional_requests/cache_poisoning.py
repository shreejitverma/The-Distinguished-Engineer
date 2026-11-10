"""Web cache poisoning via unkeyed inputs and web cache deception, reproduced in process.

`EdgeCache` is a shared cache in front of an origin ASGI app.
Its primary cache key is the method, the `Host` field, the raw path, and the query minus any
unkeyed parameters (RFC 9111 Section 2); `Vary` adds secondary keys (RFC 9111 Section 4.1).
Every other request field is unkeyed, so an origin that lets one change the response lets one
request decide what the next user receives.

`build_origin` is a Starlette API whose switches turn each vulnerable behavior on or off.
Lab simplifications: only `GET` is stored, `Expires` is ignored, and a response carrying
`no-cache` is not stored at all, which RFC 9111 allows because storing is optional.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote_plus

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

HeaderList = list[tuple[bytes, bytes]]

# RFC 9110 Section 15.1: the status codes a cache may store with heuristic freshness.
HEURISTICALLY_CACHEABLE = frozenset({200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501})

# Media types a deception-armor check accepts for each static extension.
EXTENSION_MEDIA_TYPES: dict[str, frozenset[str]] = {
    ".css": frozenset({"text/css"}),
    ".js": frozenset({"text/javascript", "application/javascript"}),
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".svg": frozenset({"image/svg+xml"}),
}


def header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in headers if key.lower() == name]


def parse_cache_control(values: Iterable[str]) -> dict[str, str | None]:
    """Directive name -> argument; the first occurrence of a directive wins."""
    directives: dict[str, str | None] = {}
    for value in values:
        for part in value.split(","):
            name, sep, argument = part.strip().partition("=")
            if name:
                directives.setdefault(name.lower(), argument.strip('"') if sep else None)
    return directives


def keyed_query(query_string: bytes, unkeyed: frozenset[str]) -> str:
    """Drop unkeyed parameters, splitting on `&` only, as most caches do."""
    pieces = query_string.decode("latin-1").split("&")
    return "&".join(p for p in pieces if p and unquote_plus(p.partition("=")[0]) not in unkeyed)


def legacy_parse_qsl(query: str) -> list[tuple[str, str]]:
    """Split on `&` and then `;`, the rule `urllib.parse.parse_qsl` used before CVE-2021-23336."""
    pairs = [s2 for s1 in query.split("&") for s2 in s1.split(";")]
    result: list[tuple[str, str]] = []
    for pair in pairs:
        if pair:
            name, _, value = pair.partition("=")
            result.append((unquote_plus(name), unquote_plus(value)))
    return result


def static_extension(raw_path: str, extensions: frozenset[str]) -> str | None:
    last_segment = raw_path.rsplit("/", 1)[-1]
    dot = last_segment.rfind(".")
    if dot <= 0:
        return None
    extension = last_segment[dot:].lower()
    return extension if extension in extensions else None


def content_type_matches(extension: str, content_type: str | None) -> bool:
    """The deception-armor test: does the returned media type fit the URL's extension?"""
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/octet-stream":
        return True
    return media_type in EXTENSION_MEDIA_TYPES.get(extension, frozenset())


@dataclass(frozen=True, slots=True)
class EdgePolicy:
    keyed_headers: tuple[bytes, ...] = ()
    unkeyed_query_params: frozenset[str] = frozenset()
    honor_vary: bool = True
    forwarded_host: Literal["pass", "overwrite"] = "pass"
    static_extensions: frozenset[str] = frozenset()
    static_ttl: float = 3600.0
    extension_rule_overrides_origin: bool = False
    verify_content_type: bool = False
    heuristic_ttl: float = 0.0


@dataclass(frozen=True, slots=True)
class StoreDecision:
    ttl: float | None  # None: the response is not stored
    reason: str


def decide_storage(
    policy: EdgePolicy,
    *,
    method: str,
    raw_path: str,
    request_headers: Sequence[tuple[bytes, bytes]],
    status: int,
    response_headers: Sequence[tuple[bytes, bytes]],
) -> StoreDecision:
    """RFC 9111 Section 3 for a shared cache, plus the CDN extension rule that can override it."""
    if method != "GET":
        return StoreDecision(None, "method")
    extension = static_extension(raw_path, policy.static_extensions)
    if extension is not None and policy.extension_rule_overrides_origin:
        content_type = next(iter(header_values(response_headers, b"content-type")), None)
        if policy.verify_content_type and not content_type_matches(extension, content_type):
            return StoreDecision(None, "content-type mismatch")
        return StoreDecision(policy.static_ttl, f"extension rule {extension}")

    cc = parse_cache_control(header_values(response_headers, b"cache-control"))
    for directive in ("no-store", "private", "no-cache"):
        if directive in cc:
            return StoreDecision(None, directive)
    explicitly_shared = {"public", "s-maxage", "must-revalidate"} & cc.keys()
    if header_values(request_headers, b"authorization") and not explicitly_shared:
        return StoreDecision(None, "authorization")  # RFC 9111 Section 3.5
    for directive in ("s-maxage", "max-age"):  # s-maxage overrides max-age in a shared cache
        argument = cc.get(directive)
        if argument is not None and argument.isascii() and argument.isdigit():
            return StoreDecision(float(argument), directive)
    if extension is not None:
        return StoreDecision(policy.static_ttl, f"extension rule {extension}")
    if (status in HEURISTICALLY_CACHEABLE or "public" in cc) and policy.heuristic_ttl > 0:
        return StoreDecision(policy.heuristic_ttl, "heuristic")
    return StoreDecision(None, "no explicit freshness")


def selecting_values(
    request_headers: Sequence[tuple[bytes, bytes]], names: Sequence[str]
) -> tuple[str | None, ...]:
    """RFC 9111 Section 4.1: combine field lines, trim whitespace, absent stays absent."""
    values: list[str | None] = []
    for name in names:
        lines = [v.strip() for v in header_values(request_headers, name.encode("latin-1"))]
        values.append(", ".join(lines) if lines else None)
    return tuple(values)


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    stored_at: float
    ttl: float
    vary: tuple[str, ...]
    selected: tuple[str | None, ...]


@dataclass
class EdgeCache:
    """A shared cache as an ASGI app; it reports its decisions in `Cache-Status` (RFC 9211)."""

    origin: ASGIApp
    policy: EdgePolicy = field(default_factory=EdgePolicy)
    clock: Callable[[], float] = lambda: 0.0
    store: dict[tuple[str, ...], list[StoredResponse]] = field(
        default_factory=dict[tuple[str, ...], list[StoredResponse]]
    )
    forwarded: int = 0

    def primary_key(self, scope: Scope) -> tuple[str, ...]:
        headers: HeaderList = scope["headers"]
        host = next(iter(header_values(headers, b"host")), "").lower()
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode()
        query = keyed_query(scope["query_string"], self.policy.unkeyed_query_params)
        extra = tuple(", ".join(header_values(headers, n)) for n in self.policy.keyed_headers)
        return (scope["method"], host, raw_path.decode("latin-1"), query, *extra)

    def lookup(
        self, key: tuple[str, ...], request_headers: HeaderList
    ) -> tuple[StoredResponse | None, str]:
        now = self.clock()
        fresh = [s for s in self.store.get(key, []) if now - s.stored_at < s.ttl]
        if not fresh:
            return None, "uri-miss" if key not in self.store else "stale"
        for stored in reversed(fresh):  # most recent first (RFC 9111 Section 4)
            if not self.policy.honor_vary:
                return stored, "hit"
            if "*" not in stored.vary and selecting_values(request_headers, stored.vary) == (
                stored.selected
            ):
                return stored, "hit"
        return None, "vary-miss"

    def forward_scope(self, scope: Scope) -> Scope:
        headers: HeaderList = list(scope["headers"])
        if self.policy.forwarded_host == "overwrite":
            # The edge is the only writer of X-Forwarded-Host: drop the client's, add its own.
            host = header_values(headers, b"host")
            headers = [(k, v) for k, v in headers if k.lower() != b"x-forwarded-host"]
            if host:
                headers.append((b"x-forwarded-host", host[0].encode("latin-1")))
        return {**scope, "headers": headers}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.origin(scope, receive, send)
            return
        key = self.primary_key(scope)
        request_headers: HeaderList = scope["headers"]
        if scope["method"] == "GET":
            stored, outcome = self.lookup(key, request_headers)
            if stored is not None:
                age = str(int(self.clock() - stored.stored_at)).encode()
                headers = [*stored.headers, (b"age", age), (b"cache-status", b"edge; hit")]
                await _emit(send, stored.status, headers, stored.body)
                return
        else:
            outcome = "method"

        status, response_headers, body = await _capture(
            self.origin, self.forward_scope(scope), receive
        )
        self.forwarded += 1
        raw_path = key[2]
        decision = decide_storage(
            self.policy,
            method=scope["method"],
            raw_path=raw_path,
            request_headers=request_headers,
            status=status,
            response_headers=response_headers,
        )
        cache_status = f"edge; fwd={outcome}"
        if decision.ttl is not None:
            vary = tuple(
                name.strip().lower()
                for value in header_values(response_headers, b"vary")
                for name in value.split(",")
                if name.strip()
            )
            entry = StoredResponse(
                status=status,
                headers=tuple(response_headers),
                body=body,
                stored_at=self.clock(),
                ttl=decision.ttl,
                vary=vary,
                selected=selecting_values(request_headers, vary),
            )
            variants = self.store.get(key, []) if self.policy.honor_vary else []
            others = [v for v in variants if (v.vary, v.selected) != (entry.vary, entry.selected)]
            self.store[key] = [*others, entry]
            cache_status += "; stored"
        response_headers.append((b"cache-status", cache_status.encode()))
        await _emit(send, status, response_headers, body)


async def _capture(app: ASGIApp, scope: Scope, receive: Receive) -> tuple[int, HeaderList, bytes]:
    status = 0
    headers: HeaderList = []
    chunks: list[bytes] = []

    async def send(message: Message) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return status, headers, b"".join(chunks)


async def _emit(send: Send, status: int, headers: HeaderList, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


# --- Origin ---------------------------------------------------------------------------------

PROFILES = {"alice": {"user": "alice", "email": "alice@example.com", "api_key": "ak_test_alice"}}
SESSIONS = {"s-alice": "alice"}
TOKENS = {"t-alice": "alice"}
PRICES = {"USD": "10.00", "EUR": "9.20"}

LinkBase = Literal["forwarded-host", "request-host", "canonical"]


@dataclass(frozen=True, slots=True)
class OriginConfig:
    link_base: LinkBase = "canonical"
    canonical_base: str = "https://api.example.com"
    allowed_forwarded_hosts: frozenset[str] | None = None  # None trusts any value
    vary_on_forwarded_host: bool = False
    trusted_hosts: tuple[str, ...] | None = None
    lenient_suffix_route: bool = False
    me_cache_control: str = "private, no-store"
    legacy_semicolon_query: bool = False
    honor_method_override: bool = False


def product_self_link(request: Request, config: OriginConfig, product_id: int) -> str:
    """The hypermedia gadget: where the `self` link's authority comes from."""
    if config.link_base == "request-host":
        return str(request.url_for("product", product_id=product_id))  # reflects `Host`
    if config.link_base == "forwarded-host":
        forwarded = request.headers.get("x-forwarded-host")
        allowed = config.allowed_forwarded_hosts
        if forwarded and (allowed is None or forwarded.lower() in allowed):
            return f"https://{forwarded}/api/products/{product_id}"
    return f"{config.canonical_base}/api/products/{product_id}"


def authenticate(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token in TOKENS:
        return TOKENS[token]
    return SESSIONS.get(request.cookies.get("session", ""))


def build_origin(config: OriginConfig) -> ASGIApp:
    async def product(request: Request) -> Response:
        product_id: int = request.path_params["product_id"]
        body = {"id": product_id, "links": {"self": product_self_link(request, config, product_id)}}
        headers = {"cache-control": "public, max-age=300"}
        if config.vary_on_forwarded_host:
            headers["vary"] = "x-forwarded-host"
        return JSONResponse(body, headers=headers)

    async def me(request: Request) -> Response:
        user = authenticate(request)
        if user is None:
            return JSONResponse({"error": "unauthenticated"}, 401, {"cache-control": "no-store"})
        return JSONResponse(PROFILES[user], headers={"cache-control": config.me_cache_control})

    async def prices(request: Request) -> Response:
        if config.legacy_semicolon_query:
            params = dict(legacy_parse_qsl(request.url.query))  # last value wins
        else:
            params = dict(request.query_params)  # Starlette: parse_qsl, last value wins
        currency = params.get("currency", "USD")
        if currency not in PRICES:
            return JSONResponse({"error": "unknown currency"}, 400)
        body = json.dumps({"currency": currency, "price": PRICES[currency]})
        headers = {"cache-control": "public, max-age=300"}
        return Response(body, media_type="application/json", headers=headers)

    routes = [
        Route("/api/products/{product_id:int}", product, name="product"),
        Route("/api/me", me),
        Route("/api/prices", prices),
    ]
    if config.lenient_suffix_route:
        # The PATH_INFO-style tolerance from Gil (2017): any suffix still reaches the handler.
        routes.append(Route("/api/me/{rest:path}", me))
    middleware: list[Middleware] = []
    if config.trusted_hosts is not None:
        middleware.append(Middleware(TrustedHostMiddleware, allowed_hosts=config.trusted_hosts))
    app: ASGIApp = Starlette(routes=routes, middleware=middleware)
    if config.honor_method_override:
        app = method_override(app)
    return app


def method_override(app: ASGIApp) -> ASGIApp:
    """Tunnel a method through `X-HTTP-Method-Override`, the CPDoS HMO gadget."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            override = header_values(scope["headers"], b"x-http-method-override")
            if override and override[0].strip().upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                scope = {**scope, "method": override[0].strip().upper()}
        await app(scope, receive, send)

    return wrapped
