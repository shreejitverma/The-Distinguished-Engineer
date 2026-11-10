"""An RFC 9111 cache as an `httpx.AsyncBaseTransport` wrapper.

The wrapper sits between `httpx.AsyncClient` and a real transport.
It stores `GET` responses as raw (still content-coded) bytes, serves fresh entries
with a computed `Age`, revalidates stale entries with `If-None-Match` and
`If-Modified-Since`, freshens entries from `304 Not Modified`, and invalidates on
successful unsafe methods.

Deliberate limits, each a policy rather than an RFC rule:
- one stored variant per URL (a `Vary` mismatch is a miss that replaces the entry);
- no heuristic freshness (RFC 9111 Section 4.2.2 makes heuristics a MAY);
- no stale serving (RFC 9111 Section 4.2.4, RFC 5861);
- requests that already carry their own preconditions or `Range` bypass the cache;
- in-memory LRU store bounded by entry count and body size.
"""

from __future__ import annotations

import calendar
import email.utils
import re
from collections import OrderedDict
from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

type Headers = tuple[tuple[str, str], ...]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})  # RFC 9110 Section 9.2.1
# RFC 9110 Section 15.1 minus 206, which this cache does not understand (RFC 9111 Section 3).
HEURISTICALLY_CACHEABLE = frozenset({200, 203, 204, 300, 301, 308, 404, 405, 410, 414, 501})
# RFC 9110 Section 7.6.1 plus the proxy fields of RFC 9111 Section 3.1.
HOP_BY_HOP = frozenset(
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
CALLER_PRECONDITIONS = frozenset(
    {"if-match", "if-none-match", "if-modified-since", "if-unmodified-since", "if-range", "range"}
)
_TCHARS = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_DIRECTIVE = re.compile(rf'\s*({_TCHARS})\s*(?:=\s*("(?:[^"\\]|\\.)*"|[^,\s]*))?')
_DELTA = re.compile(r"[0-9]+")
DELTA_SECONDS_CAP = 2**31  # RFC 9111 Section 1.2.2


def parse_cache_control(values: Sequence[str]) -> dict[str, str | None]:
    """Parse all `Cache-Control` lines; the first occurrence wins (RFC 9111 Section 4.2.1)."""
    directives: dict[str, str | None] = {}
    text = ",".join(values)
    pos = 0
    while pos < len(text):
        match = _DIRECTIVE.match(text, pos)
        if match is None or match.end() == pos:
            comma = text.find(",", pos)
            pos = len(text) if comma == -1 else comma + 1
            continue
        name, arg = match.group(1).lower(), match.group(2)
        if arg is not None and arg.startswith('"'):
            arg = re.sub(r"\\(.)", r"\1", arg[1:-1])
        directives.setdefault(name, arg)
        comma = text.find(",", match.end())
        pos = len(text) if comma == -1 else comma + 1
    return directives


def parse_delta_seconds(value: str | None) -> int | None:
    """`delta-seconds = 1*DIGIT` (RFC 9111 Section 1.2.2); anything else is invalid.

    Values past 2**31 are capped as that section requires; capping before `int()` also
    avoids CPython's 4300-digit `int` conversion limit and float overflow later.
    """
    if value is None or _DELTA.fullmatch(value) is None:
        return None
    digits = value.lstrip("0")
    if len(digits) > len(str(DELTA_SECONDS_CAP)):
        return DELTA_SECONDS_CAP
    return min(int(digits or "0"), DELTA_SECONDS_CAP)


def parse_http_date(value: str) -> float | None:
    """Return POSIX seconds for an `HTTP-date`, or None if invalid.

    A zone other than `GMT` is invalid for expiration (RFC 9111 Section 4.2), and the
    local time zone never influences the result (same section): asctime dates are UTC.
    """
    parts = email.utils.parsedate_tz(value)
    if parts is None:
        return None
    last = value.strip().rsplit(" ", 1)[-1].upper()
    # IMF-fixdate and rfc850-date end in GMT; asctime-date ends in a 4-digit year and is UTC.
    if last != "GMT" and re.fullmatch(r"[0-9]{4}", last) is None:
        return None
    return float(calendar.timegm((*parts[:6], 0, 0, 0)))


def _values(headers: Headers, name: str) -> list[str]:
    return [value for key, value in headers if key == name]


def _first(headers: Headers, name: str) -> str | None:
    values = _values(headers, name)
    return values[0] if values else None


def storable_headers(raw: Sequence[tuple[str, str]]) -> Headers:
    """Lowercase names and drop hop-by-hop fields, including those named in `Connection`."""
    lowered = [(key.lower(), value) for key, value in raw]
    nominated = {
        option.strip().lower()
        for key, value in lowered
        if key == "connection"
        for option in value.split(",")
    }
    return tuple((k, v) for k, v in lowered if k not in HOP_BY_HOP and k not in nominated)


def merge_304_headers(stored: Headers, update: Headers) -> Headers:
    """RFC 9111 Section 3.2: replace stored fields by name, never `Content-Length`."""
    incoming = [(k, v) for k, v in storable_headers(update) if k != "content-length"]
    replaced = {k for k, _ in incoming}
    return tuple((k, v) for k, v in stored if k not in replaced) + tuple(incoming)


@dataclass(frozen=True, slots=True)
class Entry:
    """One stored response plus the clock readings RFC 9111 Section 4.2.3 needs."""

    status: int
    headers: Headers
    body: bytes
    request_time: float
    response_time: float
    vary: Headers  # nominated request fields, normalized

    def directives(self) -> dict[str, str | None]:
        return parse_cache_control(_values(self.headers, "cache-control"))

    def current_age(self, now: float) -> float:
        """RFC 9111 Section 4.2.3, using the conservative corrected_initial_age."""
        date_header = _first(self.headers, "date")
        date_value = parse_http_date(date_header) if date_header is not None else None
        if date_value is None:
            date_value = self.response_time  # RFC 9110 Section 6.6.1: use the receipt time
        age_value = parse_delta_seconds(_first(self.headers, "age")) or 0
        apparent_age = max(0.0, self.response_time - date_value)
        response_delay = self.response_time - self.request_time
        corrected_age_value = age_value + response_delay
        corrected_initial_age = max(apparent_age, corrected_age_value)
        resident_time = now - self.response_time
        return corrected_initial_age + resident_time

    def freshness_lifetime(self, *, shared: bool) -> float:
        """RFC 9111 Section 4.2.1, first match; invalid values mean stale; no heuristics."""
        cc = self.directives()
        for name in ("s-maxage", "max-age") if shared else ("max-age",):
            if name in cc:
                return float(parse_delta_seconds(cc[name]) or 0)
        expires_values = _values(self.headers, "expires")
        if expires_values:
            expires = parse_http_date(expires_values[0])
            if expires is None:
                return 0.0  # RFC 9111 Section 5.3: invalid, e.g. "0", means already expired
            date_header = _first(self.headers, "date")
            date = parse_http_date(date_header) if date_header is not None else None
            return max(0.0, expires - (self.response_time if date is None else date))
        return 0.0


def _normalize(request_headers: httpx.Headers, name: str) -> str:
    """Combine field lines and trim whitespace (RFC 9111 Section 4.1)."""
    return ", ".join(v.strip() for v in request_headers.get_list(name))


def _vary_names(headers: Headers) -> list[str]:
    return [n.strip().lower() for v in _values(headers, "vary") for n in v.split(",") if n.strip()]


def vary_matches(entry: Entry, request_headers: httpx.Headers) -> bool:
    if any(name == "*" for name, _ in entry.vary):
        return False  # RFC 9111 Section 4.1: Vary "*" always fails to match
    return all(_normalize(request_headers, name) == value for name, value in entry.vary)


def _strong_etag(value: str | None) -> str | None:
    return None if value is None or value.startswith("W/") else value


def _opaque(value: str) -> str:
    return value.removeprefix("W/")


def selects_for_update(entry: Entry, not_modified: Headers) -> bool:
    """RFC 9111 Section 4.3.4 for a store that holds one candidate per URL."""
    new_etag = _first(not_modified, "etag")
    stored_etag = _first(entry.headers, "etag")
    if new_etag is not None:
        if _strong_etag(new_etag) is not None:
            return _strong_etag(stored_etag) == new_etag
        return stored_etag is not None and _opaque(stored_etag) == _opaque(new_etag)
    new_lm = _first(not_modified, "last-modified")
    if new_lm is not None:
        return _first(entry.headers, "last-modified") == new_lm
    return stored_etag is None and _first(entry.headers, "last-modified") is None


class CachingTransport(httpx.AsyncBaseTransport):
    """Wrap any async transport with an in-memory RFC 9111 cache.

    `shared=False` is a private cache (RFC 9111 Section 1): correct only when every
    request through this transport is made on behalf of one user.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        shared: bool = False,
        max_entries: int = 256,
        max_body_bytes: int = 1 << 20,
        clock: Callable[[], float] = time,
    ) -> None:
        self._inner = inner
        self._shared = shared
        self._max_entries = max_entries
        self._max_body_bytes = max_body_bytes
        self._clock = clock
        self._store: OrderedDict[str, Entry] = OrderedDict()

    @property
    def store(self) -> Mapping[str, Entry]:
        return self._store

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method not in SAFE_METHODS:
            response = await self._inner.handle_async_request(request)
            if 200 <= response.status_code < 400:  # RFC 9111 Section 4.4: non-error only
                self._invalidate(request.url, response)
            return response
        if request.method != "GET" or CALLER_PRECONDITIONS & set(request.headers.keys()):
            return _tag(await self._inner.handle_async_request(request), "bypass")

        key = str(request.url)
        req_cc = parse_cache_control(request.headers.get_list("cache-control"))
        entry = self._store.get(key)
        if entry is not None and not vary_matches(entry, request.headers):
            entry = None
        if entry is not None and self._is_fresh(entry, req_cc):
            self._store.move_to_end(key)
            return self._replay(request, entry, "hit", age=entry.current_age(self._clock()))
        if "only-if-cached" in req_cc:  # RFC 9111 Section 5.2.1.7
            return _tag(httpx.Response(504, request=request), "only-if-cached-miss")

        outbound = request if entry is None else _conditional(request, entry)
        request_time = self._clock()
        response = await self._inner.handle_async_request(outbound)
        response_time = self._clock()

        if entry is not None and response.status_code == 304:
            not_modified = storable_headers(response.headers.multi_items())
            await response.aclose()
            if selects_for_update(entry, not_modified):
                fresh = Entry(
                    status=entry.status,
                    headers=merge_304_headers(entry.headers, not_modified),
                    body=entry.body,
                    request_time=request_time,
                    response_time=response_time,
                    vary=entry.vary,
                )
                self._put(key, fresh)
                return self._replay(request, fresh, "revalidated", age=None)
            # The 304 names no stored response: it cannot be used (RFC 9111 Section 4.3.4).
            # pop, not del: a concurrent unsafe request may have invalidated the key meanwhile.
            self._store.pop(key, None)
            request_time = self._clock()
            response = await self._inner.handle_async_request(request)
            response_time = self._clock()

        if not self._may_store(request, response, req_cc):
            return _tag(response, "miss")
        body = await _read_raw(response)
        stored = Entry(
            status=response.status_code,
            headers=storable_headers(response.headers.multi_items()),
            body=body,
            request_time=request_time,
            response_time=response_time,
            vary=tuple(
                (name, "" if name == "*" else _normalize(request.headers, name))
                for name in _vary_names(storable_headers(response.headers.multi_items()))
            ),
        )
        if len(body) <= self._max_body_bytes:
            self._put(key, stored)
        else:
            self._store.pop(key, None)
        return self._replay(request, stored, "miss", age=None)

    def _is_fresh(self, entry: Entry, req_cc: Mapping[str, str | None]) -> bool:
        if "no-cache" in req_cc or "no-cache" in entry.directives():
            return False  # RFC 9111 Sections 5.2.1.4 and 5.2.2.4
        age = entry.current_age(self._clock())
        if "max-age" in req_cc:  # RFC 9111 Section 5.2.1.1
            limit = parse_delta_seconds(req_cc["max-age"])
            if limit is None or age > limit:
                return False
        return entry.freshness_lifetime(shared=self._shared) > age  # RFC 9111 Section 4.2

    def _may_store(
        self, request: httpx.Request, response: httpx.Response, req_cc: Mapping[str, str | None]
    ) -> bool:
        """RFC 9111 Section 3, for `GET` and the directives this cache understands."""
        cc = parse_cache_control(response.headers.get_list("cache-control"))
        if response.status_code < 200 or response.status_code in (206, 304):
            return False
        if "no-store" in cc or "no-store" in req_cc:
            return False
        if self._shared:
            if "private" in cc:
                return False
            if "authorization" in request.headers and not (
                {"public", "s-maxage", "must-revalidate"} & cc.keys()
            ):
                return False  # RFC 9111 Section 3.5
        explicit = {"public", "max-age"} | ({"s-maxage"} if self._shared else {"private"})
        return (
            bool(explicit & cc.keys())
            or "expires" in response.headers
            or response.status_code in HEURISTICALLY_CACHEABLE
        )

    def _invalidate(self, target: httpx.URL, response: httpx.Response) -> None:
        self._store.pop(str(target), None)
        for name in ("location", "content-location"):
            ref = response.headers.get(name)
            if ref is None:
                continue
            url = httpx.URL(urljoin(str(target), ref))
            if (url.scheme, url.host, url.port) == (target.scheme, target.host, target.port):
                self._store.pop(str(url), None)  # same origin only (RFC 9111 Section 4.4)

    def _put(self, key: str, entry: Entry) -> None:
        self._store[key] = entry
        self._store.move_to_end(key)
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def _replay(
        self, request: httpx.Request, entry: Entry, status: str, *, age: float | None
    ) -> httpx.Response:
        headers = [(k, v) for k, v in entry.headers if k != "age" or age is None]
        if age is not None:
            headers.append(("age", str(int(age))))  # RFC 9111 Section 4: replace Age
        response = httpx.Response(
            entry.status, headers=headers, content=entry.body, request=request
        )
        return _tag(response, status)


def _tag(response: httpx.Response, status: str) -> httpx.Response:
    response.extensions["cache_status"] = status
    return response


def _conditional(request: httpx.Request, entry: Entry) -> httpx.Request:
    """RFC 9111 Section 4.3.1: send the stored validators; keep `extensions` (timeouts)."""
    headers = request.headers.copy()
    etag = _first(entry.headers, "etag")
    last_modified = _first(entry.headers, "last-modified")
    if etag is not None:
        headers["if-none-match"] = etag
    if last_modified is not None:
        headers["if-modified-since"] = last_modified
    return httpx.Request(
        request.method, request.url, headers=headers, extensions=request.extensions
    )


async def _read_raw(response: httpx.Response) -> bytes:
    """Read the still content-coded body and always release the connection.

    Iterate `response.stream`, not `aiter_raw()`: a `Response(content=...)` built by
    `httpx.MockTransport` handlers is already read in `__init__`, and `aiter_raw()` on it
    raises `StreamConsumed`; `aread()` would return decoded bytes.
    """
    stream = response.stream
    if not isinstance(stream, httpx.AsyncByteStream):
        raise TypeError("the inner transport returned a sync stream")
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await response.aclose()
