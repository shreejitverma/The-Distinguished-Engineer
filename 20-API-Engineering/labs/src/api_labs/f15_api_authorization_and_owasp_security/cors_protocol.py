"""Lab: the WHATWG Fetch CORS algorithms as a browser model, run against Starlette CORSMiddleware.

The server side is the real `starlette.middleware.cors.CORSMiddleware` (Starlette 1.10.0).
The client side is `BrowserModel`, a small re-implementation of the Fetch Standard steps that a
browser runs: the preflight decision (Fetch 4.1 and 4.4), the CORS-preflight fetch (Fetch 4.8), the
CORS-preflight cache (Fetch 4.9), and the CORS check (Fetch 4.10).
Nothing touches a socket: every request goes through `httpx.ASGITransport`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    import httpx
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

Credentials = Literal["omit", "include"]

SAFELISTED_METHODS = frozenset({"GET", "HEAD", "POST"})
SAFELISTED_CONTENT_TYPES = frozenset(
    {"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"}
)
NON_WILDCARD_HEADERS = frozenset({"authorization"})
MAX_SAFELISTED_VALUE_BYTES = 128
MAX_SAFELISTED_TOTAL_BYTES = 1024
API_KEY = "lab-api-key-EXAMPLE-NOT-REAL"

_UNSAFE_BYTES = frozenset(b'"():<>?@[\\]{}\x7f')
_LANGUAGE_BYTES = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz *,-.;="
)
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_RANGE = re.compile(r"bytes=([0-9]+)-([0-9]*)")
_HTTPS_ORIGIN = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+(?::[0-9]{1,5})?"
)


# --- Fetch 2.2.1 and 2.2.2: what makes a request "simple" -------------------------------------


def _is_unsafe_byte(byte: int) -> bool:
    """Fetch 2.2.2 CORS-unsafe request-header byte."""
    return (byte < 0x20 and byte != 0x09) or byte in _UNSAFE_BYTES


def _mime_essence(value: str) -> str | None:
    """Simplified MIME parse: lowercased `type/subtype` when both are tokens, else None."""
    essence = value.split(";", 1)[0].strip(" \t")
    parts = essence.split("/")
    if len(parts) != 2 or not all(_TOKEN.fullmatch(part) for part in parts):
        return None
    return essence.lower()


def is_cors_safelisted_request_header(name: str, value: str) -> bool:
    """Fetch 2.2.2 CORS-safelisted request-header, value by value."""
    raw = value.encode("latin-1")
    if len(raw) > MAX_SAFELISTED_VALUE_BYTES:
        return False
    match name.lower():
        case "accept":
            return not any(_is_unsafe_byte(b) for b in raw)
        case "accept-language" | "content-language":
            return all(b in _LANGUAGE_BYTES for b in raw)
        case "content-type":
            if any(_is_unsafe_byte(b) for b in raw):
                return False
            return _mime_essence(value) in SAFELISTED_CONTENT_TYPES
        case "range":
            match_ = _RANGE.fullmatch(value)
            if match_ is None:
                return False
            start, end = match_.group(1), match_.group(2)
            return not end or int(start) <= int(end)
        case _:
            return False


def cors_unsafe_request_header_names(header_list: Iterable[tuple[str, str]]) -> list[str]:
    """Fetch 2.2.2 CORS-unsafe request-header names: sorted, lowercased, 1024-byte budget.

    `header_list` is a list of (name, value) pairs because a Fetch header list may repeat a
    name; with unique names the five safelisted names cannot exceed 5 x 128 = 640 bytes.
    """
    unsafe: list[str] = []
    potentially_unsafe: list[str] = []
    safelist_value_size = 0
    for name, value in header_list:
        if is_cors_safelisted_request_header(name, value):
            potentially_unsafe.append(name)
            safelist_value_size += len(value.encode("latin-1"))
        else:
            unsafe.append(name)
    if safelist_value_size > MAX_SAFELISTED_TOTAL_BYTES:
        unsafe.extend(potentially_unsafe)
    return sorted({name.lower() for name in unsafe})


def needs_preflight(method: str, header_list: Iterable[tuple[str, str]]) -> bool:
    """Fetch 4.1 main fetch: a non-safelisted method or any CORS-unsafe header name."""
    return method not in SAFELISTED_METHODS or bool(cors_unsafe_request_header_names(header_list))


# --- Fetch 4.10 and 4.8: the checks the browser applies to responses -------------------------


def cors_check(
    *,
    request_origin: str,
    credentials: Credentials,
    allow_origin: str | None,
    allow_credentials: str | None,
) -> bool:
    """Fetch 4.10 CORS check; header values are as returned by Fetch "get" (joined with ", ")."""
    if allow_origin is None:
        return False
    if credentials != "include" and allow_origin == "*":
        return True
    if allow_origin != request_origin:
        return False
    if credentials != "include":
        return True
    return allow_credentials == "true"


def _header_list(value: str | None) -> list[str] | None:
    """Fetch "extracting header list values": None when absent; ValueError on a non-token."""
    if value is None:
        return None
    items = [item.strip(" \t") for item in value.split(",")]
    items = [item for item in items if item]
    if not all(_TOKEN.fullmatch(item) for item in items):
        raise ValueError(f"invalid Access-Control-Allow-* list: {value!r}")
    return items


def preflight_verdict(
    *,
    method: str,
    request_header_names: Sequence[str],
    unsafe_names: Sequence[str],
    credentials: Credentials,
    allow_methods: Sequence[str] | None,
    allow_headers: Sequence[str] | None,
) -> str | None:
    """Fetch 4.8 steps after a successful CORS check; returns a failure reason or None."""
    methods = list(allow_methods or [])
    if (
        method not in methods
        and method not in SAFELISTED_METHODS
        and (credentials == "include" or "*" not in methods)
    ):
        return f"method {method} not in Access-Control-Allow-Methods"
    allowed = {name.lower() for name in allow_headers or []}
    for name in request_header_names:
        if name.lower() in NON_WILDCARD_HEADERS and name.lower() not in allowed:
            return f"{name.lower()} must be listed by name"
    for name in unsafe_names:
        if name not in allowed and (credentials == "include" or "*" not in allowed):
            return f"header {name} not in Access-Control-Allow-Headers"
    return None


# --- Access-Control-Max-Age as each engine parses and clamps it -------------------------------


class Engine(StrEnum):
    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


DEFAULT_MAX_AGE = 5
MAX_AGE_CAP = {Engine.CHROMIUM: 7200, Engine.FIREFOX: 86400, Engine.WEBKIT: 600}


def preflight_ttl(raw: str | None, engine: Engine) -> int | None:
    """Seconds a preflight result is cached, or None when the engine does not cache it.

    Chromium: services/network/cors/preflight_result.cc, ParseAccessControlMaxAge.
    Firefox: netwerk/protocol/http/nsCORSListenerProxy.cpp, AddResultToCache.
    WebKit: Source/WebCore/loader/CrossOriginPreflightResultCache.cpp, parseAccessControlMaxAge.
    """
    cap = MAX_AGE_CAP[engine]
    if raw is None or raw == "":
        return DEFAULT_MAX_AGE
    if engine is Engine.FIREFOX:
        if not raw.isascii() or not raw.isdigit():
            return None
        seconds = min(int(raw), cap)
        return seconds or None
    if re.fullmatch(r"[+-]?[0-9]+", raw) is None:
        return DEFAULT_MAX_AGE
    return max(0, min(int(raw), cap))


# --- Fetch 4.9: the CORS-preflight cache ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightEntry:
    origin: str
    url: str
    credentials: bool
    method: str | None
    header: str | None
    expires_at: float


@dataclass(slots=True)
class FetchOutcome:
    shared: bool
    reason: str
    preflighted: bool
    status: int | None = None
    body: str | None = None


@dataclass
class BrowserModel:
    """One browser profile: a preflight cache, a clock, and a cookie for the victim API."""

    client: httpx.AsyncClient
    engine: Engine = Engine.CHROMIUM
    clock: Callable[[], float] = lambda: 0.0
    cookie: str = "session=victim"
    cache: list[PreflightEntry] = field(default_factory=list[PreflightEntry])
    preflights_sent: int = 0

    def _matches(self, entry: PreflightEntry, origin: str, url: str, cred: Credentials) -> bool:
        return (
            entry.origin == origin
            and entry.url == url
            and entry.expires_at > self.clock()
            and (entry.credentials or cred != "include")
        )

    def _method_cached(self, origin: str, url: str, cred: Credentials, method: str) -> bool:
        return any(
            self._matches(e, origin, url, cred) and e.method in (method, "*") for e in self.cache
        )

    def _header_cached(self, origin: str, url: str, cred: Credentials, name: str) -> bool:
        return any(
            self._matches(e, origin, url, cred)
            and e.header is not None
            and (e.header.lower() == name or (e.header == "*" and name not in NON_WILDCARD_HEADERS))
            for e in self.cache
        )

    async def _preflight(
        self, url: str, origin: str, method: str, headers: Mapping[str, str], cred: Credentials
    ) -> str | None:
        unsafe = cors_unsafe_request_header_names(headers.items())
        preflight_headers = {"Origin": origin, "Accept": "*/*"}
        preflight_headers["Access-Control-Request-Method"] = method
        if unsafe:
            preflight_headers["Access-Control-Request-Headers"] = ",".join(unsafe)
        self.preflights_sent += 1
        response = await self.client.options(url, headers=preflight_headers)  # never credentials
        if not cors_check(
            request_origin=origin,
            credentials=cred,
            allow_origin=response.headers.get("access-control-allow-origin"),
            allow_credentials=response.headers.get("access-control-allow-credentials"),
        ):
            return "preflight failed the CORS check"
        if not 200 <= response.status_code <= 299:
            return f"preflight status {response.status_code} is not an ok status"
        try:
            methods = _header_list(response.headers.get("access-control-allow-methods"))
            names = _header_list(response.headers.get("access-control-allow-headers"))
        except ValueError as exc:
            return str(exc)
        reason = preflight_verdict(
            method=method,
            request_header_names=list(headers),
            unsafe_names=unsafe,
            credentials=cred,
            allow_methods=methods,
            allow_headers=names,
        )
        if reason is not None:
            return reason
        ttl = preflight_ttl(response.headers.get("access-control-max-age"), self.engine)
        if ttl:
            expires = self.clock() + ttl
            credentialed = cred == "include"
            self.cache.extend(
                PreflightEntry(origin, url, credentialed, m, None, expires) for m in methods or []
            )
            self.cache.extend(
                PreflightEntry(origin, url, credentialed, None, h, expires) for h in names or []
            )
        return None

    async def fetch(
        self,
        url: str,
        *,
        origin: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        credentials: Credentials = "omit",
        content: bytes = b"",
    ) -> FetchOutcome:
        """A cross-origin `fetch(url, {mode: "cors", credentials})` issued by script at `origin`."""
        request_headers = dict(headers or {})
        unsafe = cors_unsafe_request_header_names(request_headers.items())
        preflighted = False
        if (
            not self._method_cached(origin, url, credentials, method)
            and method not in SAFELISTED_METHODS
        ) or any(not self._header_cached(origin, url, credentials, n) for n in unsafe):
            preflighted = True
            failure = await self._preflight(url, origin, method, request_headers, credentials)
            if failure is not None:
                return FetchOutcome(False, failure, preflighted)
        sent = {**request_headers, "Origin": origin}
        if credentials == "include":
            sent["Cookie"] = self.cookie
        response = await self.client.request(method, url, headers=sent, content=content)
        if not cors_check(
            request_origin=origin,
            credentials=credentials,
            allow_origin=response.headers.get("access-control-allow-origin"),
            allow_credentials=response.headers.get("access-control-allow-credentials"),
        ):
            self.cache = [e for e in self.cache if not (e.origin == origin and e.url == url)]
            return FetchOutcome(False, "response failed the CORS check", preflighted, None)
        return FetchOutcome(True, "shared", preflighted, response.status_code, response.text)


# --- The victim API and two hand-rolled CORS layers to contrast with Starlette ----------------


@dataclass(slots=True)
class VictimCounters:
    transfers: int = 0
    options_seen: int = 0
    options_handler_calls: int = 0


def victim_app(middleware: Sequence[Middleware] = ()) -> tuple[ASGIApp, VictimCounters]:
    """A cookie-authenticated API: `GET /me` returns a secret, `POST|PUT /transfer` mutates."""
    counters = VictimCounters()

    async def me(request: Request) -> Response:
        if request.method == "OPTIONS":
            counters.options_handler_calls += 1
            return Response(status_code=204, headers={"Allow": "GET, HEAD, OPTIONS"})
        if request.cookies.get("session") != "victim":
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        return JSONResponse({"user": "victim", "api_key": API_KEY})

    async def transfer(request: Request) -> Response:
        if request.cookies.get("session") != "victim":
            return JSONResponse({"error": "unauthenticated"}, status_code=401)
        counters.transfers += 1
        return JSONResponse({"transfers": counters.transfers})

    async def public(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    inner = Starlette(
        routes=[
            Route("/me", me, methods=["GET", "OPTIONS"]),
            Route("/transfer", transfer, methods=["POST", "PUT"]),
            Route("/public", public, methods=["GET"]),
        ],
        middleware=list(middleware),
    )

    async def edge(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            counters.options_seen += 1
        await inner(scope, receive, send)

    return edge, counters


class HandRolledCors:
    """Two classic hand-written CORS layers: a literal `*` with credentials, and reflect-no-Vary."""

    def __init__(
        self, app: ASGIApp, *, mode: Literal["literal-wildcard", "reflect-without-vary"]
    ) -> None:
        self.app = app
        self.mode = mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = Headers(scope=scope).get("origin")

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if self.mode == "literal-wildcard":
                    headers["Access-Control-Allow-Origin"] = "*"
                    headers["Access-Control-Allow-Credentials"] = "true"
                elif origin is not None:
                    headers["Access-Control-Allow-Origin"] = origin
                    headers["Access-Control-Allow-Credentials"] = "true"
            await send(message)

        await self.app(scope, receive, send_with_cors)


# --- A shared cache that honors or ignores Vary (RFC 9111 Section 4.1) -------------------------


@dataclass
class SharedCache:
    """GET-only shared cache keyed by URL; stores 200s and, if `honor_vary`, matches on Vary."""

    client: httpx.AsyncClient
    honor_vary: bool
    stored: dict[str, tuple[dict[str, str | None], httpx.Response]] = field(
        default_factory=dict[str, tuple[dict[str, str | None], "httpx.Response"]]
    )
    hits: int = 0

    async def get(self, url: str, headers: Mapping[str, str]) -> httpx.Response:
        lowered = {name.lower(): value for name, value in headers.items()}
        entry = self.stored.get(url)
        if entry is not None:
            nominated, response = entry
            if not self.honor_vary or all(lowered.get(n) == v for n, v in nominated.items()):
                self.hits += 1
                return response
        response = await self.client.get(url, headers=dict(headers))
        vary = [v.strip().lower() for v in response.headers.get("vary", "").split(",")]
        vary = [v for v in vary if v]
        if response.status_code == 200 and "*" not in vary:
            self.stored[url] = ({name: lowered.get(name) for name in vary}, response)
        return response


# --- A policy object that refuses the reflection foot-guns before Starlette sees them ---------


class UnsafeCorsPolicyError(ValueError):
    """Raised for a CORS configuration that shares credentialed responses too widely."""


@dataclass(frozen=True, slots=True)
class CorsPolicy:
    origins: frozenset[str]
    credentials: bool
    methods: tuple[str, ...] = ("GET",)
    headers: tuple[str, ...] = ()
    max_age: int = 600

    def __post_init__(self) -> None:
        if not self.origins:
            raise UnsafeCorsPolicyError("an empty allowlist: omit the middleware instead")
        if "*" in self.origins and self.credentials:
            raise UnsafeCorsPolicyError("'*' with credentials makes Starlette reflect any origin")
        if "null" in self.origins:
            raise UnsafeCorsPolicyError("'null' is the origin of every sandboxed iframe")
        for origin in self.origins - {"*"}:
            if _HTTPS_ORIGIN.fullmatch(origin) is None:
                raise UnsafeCorsPolicyError(f"not a serialized https origin: {origin!r}")
        if self.credentials and ("*" in self.methods or "*" in self.headers):
            raise UnsafeCorsPolicyError("wildcard methods or headers with credentials")
        if not 0 <= self.max_age <= MAX_AGE_CAP[Engine.CHROMIUM]:
            raise UnsafeCorsPolicyError("max_age outside 0..7200 is clamped by Chromium anyway")

    def middleware(self) -> Middleware:
        return Middleware(
            CORSMiddleware,
            allow_origins=sorted(self.origins),
            allow_credentials=self.credentials,
            allow_methods=self.methods,
            allow_headers=self.headers,
            max_age=self.max_age,
        )
