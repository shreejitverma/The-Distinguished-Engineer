"""A shared HTTP cache that serves stale responses per RFC 5861 on top of RFC 9111.

`StaleCache` is pure ASGI middleware in front of an origin ASGI app.
It stores `200 OK` responses to `GET`, and on each request it decides:

- fresh (`freshness_lifetime > current_age`, RFC 9111 Section 4.2): serve from cache;
- stale but inside `stale-while-revalidate` (RFC 5861 Section 3): serve from cache now
  and start at most one background conditional revalidation per key;
- otherwise forward synchronously, and if the origin fails with `500`, `502`, `503`,
  or `504` (RFC 5861 Section 4) while the entry is inside `stale-if-error`, serve the
  stale entry instead of the error.

`no-cache`, `must-revalidate`, `proxy-revalidate`, and (by default) `s-maxage` forbid
serving stale (RFC 9111 Section 4.2.4).
Time comes from an injected clock so the windows can be tested to the second.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

Headers = list[tuple[bytes, bytes]]

# RFC 9111 Section 1.2.2: overflowing delta-seconds is treated as 2^31.
DELTA_SECONDS_MAX = 2**31
# RFC 5861 Section 4: "an error is any situation that would result in a
# 500, 502, 503, or 504 HTTP response status code being returned."
STALE_IF_ERROR_STATUSES = frozenset({500, 502, 503, 504})


def parse_cache_control(values: list[bytes]) -> dict[str, str | None]:
    """Parse `Cache-Control` field lines into lowercase directive -> argument.

    Commas inside quoted strings do not split directives, quoted arguments are
    unquoted (RFC 9111 Section 5.2), and the first occurrence of a directive wins
    (RFC 9111 Section 4.2.1).
    """
    text = ",".join(v.decode("latin-1") for v in values)
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif in_quotes and char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))

    directives: dict[str, str | None] = {}
    for part in parts:
        name, sep, arg = part.partition("=")
        name = name.strip().lower()
        if not name or name in directives:
            continue
        value: str | None = None
        if sep:
            value = arg.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                inner = value[1:-1]
                unescaped: list[str] = []
                skip = False
                for char in inner:
                    if skip:
                        unescaped.append(char)
                        skip = False
                    elif char == "\\":
                        skip = True
                    else:
                        unescaped.append(char)
                value = "".join(unescaped)
        directives[name] = value
    return directives


def delta_seconds(value: str | None) -> int | None:
    """Return `delta-seconds = 1*DIGIT` as an int capped at 2^31, or None if invalid."""
    if value is None or not value.isascii() or not value.isdigit():
        return None
    return min(int(value), DELTA_SECONDS_MAX)


def header_values(headers: Headers, name: bytes) -> list[bytes]:
    return [v for k, v in headers if k.lower() == name]


@dataclass(frozen=True, slots=True)
class StalePolicy:
    """Freshness and stale-serving permissions derived from a stored response."""

    freshness_lifetime: int
    stale_while_revalidate: int
    stale_if_error: int
    stale_forbidden: bool
    no_cache: bool

    @classmethod
    def from_headers(cls, headers: Headers, *, s_maxage_blocks_stale: bool) -> StalePolicy:
        cc = parse_cache_control(header_values(headers, b"cache-control"))
        s_maxage = delta_seconds(cc.get("s-maxage"))
        max_age = delta_seconds(cc.get("max-age"))
        # RFC 9111 Section 4.2.1: a shared cache uses s-maxage first, then max-age.
        # Invalid or absent freshness information is treated as already stale.
        lifetime = s_maxage if s_maxage is not None else (max_age or 0)
        forbidden = (
            "no-cache" in cc
            or "must-revalidate" in cc
            or "proxy-revalidate" in cc
            or (s_maxage_blocks_stale and s_maxage is not None)
        )
        return cls(
            freshness_lifetime=lifetime,
            stale_while_revalidate=delta_seconds(cc.get("stale-while-revalidate")) or 0,
            stale_if_error=delta_seconds(cc.get("stale-if-error")) or 0,
            stale_forbidden=forbidden,
            no_cache="no-cache" in cc,
        )


@dataclass(slots=True)
class Entry:
    """A stored response plus the RFC 9111 Section 4.2.3 age inputs."""

    status: int
    headers: Headers
    body: bytes
    request_time: float
    response_time: float
    policy: StalePolicy

    def current_age(self, now: float) -> float:
        """RFC 9111 Section 4.2.3, using corrected_age_value as corrected_initial_age."""
        # RFC 9111 Section 5.1: use the first member of a list-based Age value.
        raw_age = next(iter(header_values(self.headers, b"age")), b"").split(b",")[0]
        age_value = delta_seconds(raw_age.strip().decode("latin-1"))
        response_delay = self.response_time - self.request_time
        corrected_initial_age = (age_value or 0) + response_delay
        resident_time = now - self.response_time
        return corrected_initial_age + resident_time

    def staleness(self, now: float) -> float:
        """Seconds past the freshness lifetime; negative while fresh."""
        return self.current_age(now) - self.policy.freshness_lifetime


class Outcome(Enum):
    HIT = "hit"
    STALE_WHILE_REVALIDATE = "stale-while-revalidate"
    STALE_IF_ERROR = "stale-if-error"
    FORWARDED = "forwarded"


@dataclass(slots=True)
class OriginResponse:
    status: int
    headers: Headers
    body: bytes


class OriginUnavailableError(Exception):
    """The origin app raised instead of answering; a gateway would emit 502 or 504."""


@dataclass
class StaleCache:
    """ASGI middleware implementing a shared cache with RFC 5861 stale extensions."""

    app: ASGIApp
    clock: Callable[[], float]
    s_maxage_blocks_stale: bool = True
    name: str = "lab"
    entries: dict[str, Entry] = field(default_factory=dict[str, Entry])
    outcomes: list[Outcome] = field(default_factory=list[Outcome])
    _inflight: dict[str, asyncio.Task[None]] = field(default_factory=dict[str, asyncio.Task[None]])
    _tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        key = scope["path"] + "?" + scope["query_string"].decode("latin-1")
        request_cc = parse_cache_control(header_values(scope["headers"], b"cache-control"))
        now = self.clock()
        entry = self.entries.get(key)

        if entry is not None:
            staleness = entry.staleness(now)
            # RFC 9111 Section 4: unqualified no-cache means validate on every reuse.
            if staleness < 0 and not entry.policy.no_cache:
                await self._send_stored(send, entry, now, Outcome.HIT)
                return
            policy = entry.policy
            if not policy.stale_forbidden and staleness <= policy.stale_while_revalidate:
                self._spawn_revalidation(key, scope, entry)
                await self._send_stored(send, entry, now, Outcome.STALE_WHILE_REVALIDATE)
                return

        try:
            origin = await self._fetch(scope, entry)
        except OriginUnavailableError:
            origin = None
        status = 502 if origin is None else origin.status

        if entry is not None and status in STALE_IF_ERROR_STATUSES:
            if self._stale_if_error_allows(entry, request_cc, now):
                await self._send_stored(send, entry, now, Outcome.STALE_IF_ERROR, fwd_status=status)
                return
            if origin is None and entry.policy.stale_forbidden:
                # RFC 9111 Section 5.2.2.2: a disconnected cache SHOULD answer 504.
                origin = OriginResponse(504, [(b"content-type", b"text/plain")], b"")
        if origin is None:
            origin = OriginResponse(502, [(b"content-type", b"text/plain")], b"")

        self.outcomes.append(Outcome.FORWARDED)
        if entry is not None and origin.status == 304:
            self._freshen(key, entry, origin, now)
            await self._send_stored(send, self.entries[key], self.clock(), Outcome.FORWARDED)
            return
        self._maybe_store(key, origin, now)
        await _send_response(send, origin.status, origin.headers, origin.body)

    def _stale_if_error_allows(
        self, entry: Entry, request_cc: dict[str, str | None], now: float
    ) -> bool:
        if entry.policy.stale_forbidden:
            return False
        # RFC 5861 Section 4: the request directive applies to that request, the
        # response directive to every request the stored response can satisfy.
        window = max(
            entry.policy.stale_if_error, delta_seconds(request_cc.get("stale-if-error")) or 0
        )
        return entry.staleness(now) <= window

    def _spawn_revalidation(self, key: str, scope: Scope, entry: Entry) -> None:
        """Start one background conditional request per key (request-triggered only)."""
        if key in self._inflight:
            return
        task = asyncio.create_task(self._revalidate(key, scope, entry))
        # The event loop keeps only weak references to tasks; hold a strong one.
        self._tasks.add(task)
        self._inflight[key] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _t: self._inflight.pop(key, None))

    async def _revalidate(self, key: str, scope: Scope, entry: Entry) -> None:
        request_time = self.clock()
        try:
            origin = await self._fetch(scope, entry)
        except OriginUnavailableError:
            return
        if origin.status == 304:
            self._freshen(key, entry, origin, request_time)
        elif origin.status in STALE_IF_ERROR_STATUSES:
            return  # keep serving the stored response; never cache the error
        else:
            # RFC 9111 Section 4.3.3: a full response replaces the stored one; if it is
            # not storable (say a 404), the old entry must not be reused either.
            self.entries.pop(key, None)
            self._maybe_store(key, origin, request_time)

    async def _fetch(self, scope: Scope, entry: Entry | None) -> OriginResponse:
        headers: Headers = [(k, v) for k, v in scope["headers"] if k.lower() != b"if-none-match"]
        if entry is not None:
            etags = header_values(entry.headers, b"etag")
            if etags:
                # RFC 9111 Section 4.3.1: MUST send the stored entity tag.
                headers.append((b"if-none-match", etags[0]))
        origin_scope = dict(scope)
        origin_scope["headers"] = headers
        captured: list[Message] = []

        async def empty_receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def capture(message: Message) -> None:
            captured.append(message)

        try:
            await self.app(origin_scope, empty_receive, capture)
        except Exception as exc:
            raise OriginUnavailableError(str(exc)) from exc
        start = next(m for m in captured if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in captured if m["type"] == "http.response.body")
        return OriginResponse(int(start["status"]), list(start.get("headers", [])), body)

    def _maybe_store(self, key: str, origin: OriginResponse, request_time: float) -> None:
        if origin.status != 200:
            return
        cc = parse_cache_control(header_values(origin.headers, b"cache-control"))
        if "no-store" in cc or "private" in cc:
            return
        self.entries[key] = Entry(
            status=origin.status,
            headers=origin.headers,
            body=origin.body,
            request_time=request_time,
            response_time=self.clock(),
            policy=StalePolicy.from_headers(
                origin.headers, s_maxage_blocks_stale=self.s_maxage_blocks_stale
            ),
        )

    def _freshen(self, key: str, entry: Entry, origin: OriginResponse, request_time: float) -> None:
        """RFC 9111 Sections 3.2 and 4.3.4: replace stored fields with the 304's fields."""
        replaced = {k.lower() for k, _ in origin.headers} - {b"content-length"}
        headers = [(k, v) for k, v in entry.headers if k.lower() not in replaced]
        headers += [(k, v) for k, v in origin.headers if k.lower() != b"content-length"]
        self.entries[key] = Entry(
            status=entry.status,
            headers=headers,
            body=entry.body,
            request_time=request_time,
            response_time=self.clock(),
            policy=StalePolicy.from_headers(
                headers, s_maxage_blocks_stale=self.s_maxage_blocks_stale
            ),
        )

    async def _send_stored(
        self,
        send: Send,
        entry: Entry,
        now: float,
        outcome: Outcome,
        *,
        fwd_status: int | None = None,
    ) -> None:
        if outcome is not Outcome.FORWARDED:
            self.outcomes.append(outcome)
        age = math.floor(entry.current_age(now))
        ttl = entry.policy.freshness_lifetime - age
        if fwd_status is not None:
            status_value = f"{self.name}; fwd=stale; fwd-status={fwd_status}; ttl={ttl}"
        elif outcome is Outcome.FORWARDED:
            status_value = f"{self.name}; fwd=stale; fwd-status=304; ttl={ttl}"
        else:
            status_value = f"{self.name}; hit; ttl={ttl}"
        # RFC 9111 Section 4: replace any stored Age with current_age. No Warning
        # header: RFC 9111 Section 5.5 obsoletes it although RFC 5861 predates that.
        headers = [(k, v) for k, v in entry.headers if k.lower() not in (b"age", b"cache-status")]
        headers.append((b"age", str(age).encode()))
        headers.append((b"cache-status", status_value.encode()))
        await _send_response(send, entry.status, headers, entry.body)

    async def drain(self) -> None:
        """Wait for every background revalidation; call from lifespan shutdown."""
        while self._tasks:
            await asyncio.gather(*self._tasks)


async def _send_response(send: Send, status: int, headers: Headers, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
