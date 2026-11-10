"""RFC 9111 Section 3 storage decision and a minimal private or shared ASGI cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send


class CacheKind(Enum):
    """Who may reuse a stored response (RFC 9111 Section 1)."""

    PRIVATE = "private"  # dedicated to a single user, usually inside a user agent
    SHARED = "shared"  # reused for many users: forward proxies, gateways, CDNs


class Reason(Enum):
    """Outcome of the RFC 9111 Section 3 checks, in the order the lab evaluates them."""

    STORABLE = "storable"
    METHOD_NOT_UNDERSTOOD = "method not understood"
    STATUS_NOT_FINAL = "status not final"
    STATUS_NOT_UNDERSTOOD = "206, 304, or must-understand status not understood"
    NO_STORE = "no-store response directive"
    REQUEST_NO_STORE = "no-store request directive"
    PRIVATE = "private directive in a shared cache"
    AUTHORIZATION = "Authorization request without public, s-maxage, or must-revalidate"
    POST_NOT_CACHEABLE = "POST without explicit freshness and matching Content-Location"
    NO_PERMISSION = "no explicit freshness, public, private, or heuristic status"


# RFC 9110 Section 15.1: the status codes defined as heuristically cacheable.
HEURISTICALLY_CACHEABLE: frozenset[int] = frozenset(
    {200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501}
)
# Final status codes defined in RFC 9110 Section 15 whose caching rules this lab implements.
# 206 (needs Range support) and 304 (needs validation) are deliberately absent.
UNDERSTOOD_STATUSES: frozenset[int] = frozenset(
    {200, 201, 202, 203, 204, 205, 300, 301, 302, 303, 305, 307, 308}
    | set(range(400, 418))
    | {421, 422, 426, 500, 501, 502, 503, 504, 505}
)
UNDERSTOOD_METHODS: frozenset[str] = frozenset({"GET", "POST"})
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# RFC 9111 Section 3.5: directives that let a shared cache store an authenticated response.
SHARED_AUTH_OVERRIDES: frozenset[str] = frozenset({"public", "s-maxage", "must-revalidate"})
# RFC 9111 Section 3.1 proxy fields and RFC 9110 Section 7.6.1 connection-specific fields.
NEVER_STORED: frozenset[str] = frozenset(
    {
        "connection",
        "proxy-connection",
        "keep-alive",
        "te",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authentication-info",
        "proxy-authorization",
    }
)
DELTA_SECONDS_INFINITY = 2**31  # RFC 9111 Section 1.2.2


def field_values(headers: Sequence[tuple[str, str]], name: str) -> list[str]:
    """Return every value of field *name*, matched case-insensitively, in order."""
    wanted = name.lower()
    return [value for key, value in headers if key.lower() == wanted]


def parse_cache_control(values: Sequence[str]) -> dict[str, str | None]:
    """Parse ``#cache-directive`` (RFC 9111 Section 5.2); the first occurrence of a name wins."""
    text = ",".join(values)
    directives: dict[str, str | None] = {}
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t,":
            i += 1
        start = i
        while i < n and text[i] not in "=, \t":
            i += 1
        name = text[start:i].lower()
        arg: str | None = None
        if i < n and text[i] == "=":
            i += 1
            if i < n and text[i] == '"':
                i += 1
                chars: list[str] = []
                while i < n and text[i] != '"':
                    if text[i] == "\\" and i + 1 < n:
                        i += 1
                    chars.append(text[i])
                    i += 1
                i += 1
                arg = "".join(chars)
            else:
                start = i
                while i < n and text[i] not in ", \t":
                    i += 1
                arg = text[start:i]
        while i < n and text[i] != ",":
            i += 1
        if name and name not in directives:
            directives[name] = arg
    return directives


def parse_delta_seconds(arg: str | None) -> int | None:
    """Parse ``delta-seconds``; ``None`` if invalid, capped at 2**31 (RFC 9111 Section 1.2.2)."""
    if not arg or not (arg.isascii() and arg.isdigit()):
        return None
    if len(arg) > 10:  # compare by length first: never int() an attacker-sized numeral
        return DELTA_SECONDS_INFINITY
    return min(int(arg), DELTA_SECONDS_INFINITY)


def listed_field_names(arg: str | None) -> frozenset[str]:
    """Return the lowercased field names of a qualified ``private`` or ``no-cache``."""
    if arg is None:
        return frozenset()
    return frozenset(part.strip().lower() for part in arg.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class StorageDecision:
    """Whether a cache may store a response, and which fields it must drop first."""

    reason: Reason
    omit_fields: frozenset[str] = frozenset()

    @property
    def storable(self) -> bool:
        return self.reason is Reason.STORABLE


def may_store(
    *,
    kind: CacheKind,
    method: str,
    target_uri: str,
    status: int,
    request_headers: Sequence[tuple[str, str]],
    response_headers: Sequence[tuple[str, str]],
    understood_methods: frozenset[str] = UNDERSTOOD_METHODS,
    understood_statuses: frozenset[int] = UNDERSTOOD_STATUSES,
) -> StorageDecision:
    """Apply the RFC 9111 Section 3 storage rules, plus RFC 9110 Section 9.3.3 for POST."""
    cc = parse_cache_control(field_values(response_headers, "cache-control"))
    shared = kind is CacheKind.SHARED
    if method not in understood_methods:  # method tokens are case-sensitive
        return StorageDecision(Reason.METHOD_NOT_UNDERSTOOD)
    if status < 200:
        return StorageDecision(Reason.STATUS_NOT_FINAL)
    status_understood = status in understood_statuses
    if (status in (206, 304) or "must-understand" in cc) and not status_understood:
        return StorageDecision(Reason.STATUS_NOT_UNDERSTOOD)
    if "no-store" in cc and not ("must-understand" in cc and status_understood):
        return StorageDecision(Reason.NO_STORE)
    if "no-store" in parse_cache_control(field_values(request_headers, "cache-control")):
        return StorageDecision(Reason.REQUEST_NO_STORE)
    omit: frozenset[str] = frozenset()
    if shared and "private" in cc:
        omit = listed_field_names(cc["private"])
        if not omit:
            return StorageDecision(Reason.PRIVATE)
    authorized = bool(field_values(request_headers, "authorization"))
    if shared and authorized and not SHARED_AUTH_OVERRIDES & cc.keys():
        return StorageDecision(Reason.AUTHORIZATION)
    explicit_freshness = (
        "max-age" in cc
        or (shared and "s-maxage" in cc)
        or bool(field_values(response_headers, "expires"))
    )
    if method == "POST":
        locations = field_values(response_headers, "content-location")
        same_uri = bool(locations) and urljoin(target_uri, locations[0]) == target_uri
        if not (explicit_freshness and same_uri):
            return StorageDecision(Reason.POST_NOT_CACHEABLE)
    permitted = (
        explicit_freshness
        or "public" in cc
        or (not shared and "private" in cc)
        or status in HEURISTICALLY_CACHEABLE
    )
    if not permitted:
        return StorageDecision(Reason.NO_PERMISSION)
    return StorageDecision(Reason.STORABLE, omit)


def _http_date(value: str) -> float | None:
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def freshness_lifetime(
    kind: CacheKind, response_headers: Sequence[tuple[str, str]], *, received_at: float
) -> int:
    """First-match explicit lifetime (RFC 9111 Section 4.2.1); this lab assigns no heuristic."""
    cc = parse_cache_control(field_values(response_headers, "cache-control"))
    if kind is CacheKind.SHARED and "s-maxage" in cc:
        return parse_delta_seconds(cc["s-maxage"]) or 0
    if "max-age" in cc:
        return parse_delta_seconds(cc["max-age"]) or 0
    expires = field_values(response_headers, "expires")
    if expires:
        expires_at = _http_date(expires[0])
        if expires_at is None:  # RFC 9111 Section 5.3: invalid, including "0", is in the past
            return 0
        dates = field_values(response_headers, "date")
        date = _http_date(dates[0]) if dates else None
        return max(0, int(expires_at - (date if date is not None else received_at)))
    return 0


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    stored_at: float
    initial_age: int
    lifetime: int
    needs_validation: bool


def _stored_fields(
    headers: Sequence[tuple[str, str]], omit: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    connection_options = {
        option.strip().lower()
        for value in field_values(headers, "connection")
        for option in value.split(",")
    }
    dropped = NEVER_STORED | connection_options | omit
    return tuple((k, v) for k, v in headers if k.lower() not in dropped)


class HTTPCache:
    """A deliberately small RFC 9111 cache in front of an ASGI origin.

    ``kind=CacheKind.SHARED`` models a gateway (reverse proxy) or CDN cache and
    ``kind=CacheKind.PRIVATE`` a single user's cache. It stores and reuses fresh
    responses only: no validation, no ranges, no heuristic freshness, and it
    declines to store any response carrying ``Vary``.
    """

    def __init__(
        self, app: ASGIApp, *, kind: CacheKind, clock: Callable[[], float] = time.time
    ) -> None:
        self.app = app
        self.kind = kind
        self._clock = clock
        self.entries: dict[str, StoredResponse] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method: str = scope["method"]
        request_headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        target_uri = _target_uri(scope, request_headers)
        request_cc = parse_cache_control(field_values(request_headers, "cache-control"))
        entry = self.entries.get(target_uri)
        if method == "GET" and entry is not None and "no-cache" not in request_cc:
            age = entry.initial_age + int(self._clock() - entry.stored_at)
            if not entry.needs_validation and age < entry.lifetime:
                await _replay(entry, age, send)
                return
        await self._forward(scope, receive, send, method, target_uri, request_headers)

    async def _forward(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        method: str,
        target_uri: str,
        request_headers: list[tuple[str, str]],
    ) -> None:
        status = 0
        response_headers: list[tuple[str, str]] = []
        body = bytearray()

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers.extend(
                    (k.decode("latin-1"), v.decode("latin-1")) for k, v in message["headers"]
                )
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
            await send(message)

        await self.app(scope, receive, capture)
        if method not in SAFE_METHODS and 200 <= status < 400:
            self.entries.pop(target_uri, None)  # RFC 9111 Section 4.4: invalidate the target URI
        decision = may_store(
            kind=self.kind,
            method=method,
            target_uri=target_uri,
            status=status,
            request_headers=request_headers,
            response_headers=response_headers,
        )
        if not decision.storable or field_values(response_headers, "vary"):
            return
        now = self._clock()
        cc = parse_cache_control(field_values(response_headers, "cache-control"))
        ages = field_values(response_headers, "age")
        self.entries[target_uri] = StoredResponse(
            status=status,
            headers=_stored_fields(response_headers, decision.omit_fields),
            body=bytes(body),
            stored_at=now,
            initial_age=(parse_delta_seconds(ages[0]) or 0) if ages else 0,
            lifetime=freshness_lifetime(self.kind, response_headers, received_at=now),
            needs_validation="no-cache" in cc,
        )


def _target_uri(scope: Scope, request_headers: Sequence[tuple[str, str]]) -> str:
    hosts = field_values(request_headers, "host")
    host = hosts[0] if hosts else "localhost"
    query: bytes = scope.get("query_string", b"")
    path: str = scope["path"]
    return f"{scope['scheme']}://{host}{path}" + (f"?{query.decode('latin-1')}" if query else "")


async def _replay(entry: StoredResponse, age: int, send: Send) -> None:
    headers = [(k, v) for k, v in entry.headers if k.lower() != "age"] + [("age", str(age))]
    await send(
        {
            "type": "http.response.start",
            "status": entry.status,
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        }
    )
    await send({"type": "http.response.body", "body": entry.body})
