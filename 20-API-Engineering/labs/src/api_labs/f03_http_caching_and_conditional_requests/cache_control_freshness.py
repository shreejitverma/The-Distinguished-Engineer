"""Cache-Control parsing, freshness lifetime, and age per RFC 9111 Sections 4.2 and 5.2.

All times are integer seconds on the cache's own clock (POSIX epoch, UTC).
Nothing here reads the wall clock, so every result is a pure function of its inputs.
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# RFC 9111 Section 1.2.2: an overflowing delta-seconds is treated as 2^31 ("infinity").
DELTA_SECONDS_INFINITY = 2**31
# Ten digits always fit below 2^31 * 10; longer strings are clamped without calling int(),
# which raises ValueError past sys.get_int_max_str_digits() (4300 by default).
_MAX_DELTA_DIGITS = 10

# RFC 9110 Section 15.1: status codes defined as heuristically cacheable.
HEURISTIC_STATUSES = frozenset({200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501})

_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_OWS = " \t"


@dataclass(frozen=True, slots=True)
class CacheControl:
    """Parsed Cache-Control: lowercase directive name -> argument (None if absent)."""

    directives: Mapping[str, str | None]
    repeated: frozenset[str]  # names that appeared more than once (first value kept)
    malformed: frozenset[str]  # names whose syntax was broken, for example "max-age = 5"

    def has(self, name: str) -> bool:
        return name in self.directives or name in self.malformed

    def field_names(self, name: str) -> frozenset[str]:
        """Field names listed in a qualified no-cache or private argument, lowercased."""
        value = self.directives.get(name)
        if value is None:
            return frozenset()
        return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def _skip_element(text: str, pos: int) -> int:
    """Resynchronize after a malformed element: advance to the next comma outside quotes."""
    quoted = False
    while pos < len(text):
        char = text[pos]
        if quoted and char == "\\":
            pos += 2
            continue
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            return pos
        pos += 1
    return pos


def _quoted_string(text: str, pos: int) -> tuple[str, int] | None:
    """RFC 9110 Section 5.6.4 quoted-string starting at text[pos] == '"'."""
    out: list[str] = []
    pos += 1
    while pos < len(text):
        char = text[pos]
        if char == "\\" and pos + 1 < len(text):
            out.append(text[pos + 1])
            pos += 2
        elif char == '"':
            return "".join(out), pos + 1
        else:
            out.append(char)
            pos += 1
    return None


def parse_cache_control(lines: Iterable[str]) -> CacheControl:
    """Parse `#cache-directive` from every field line (RFC 9111 Section 5.2).

    Names compare case-insensitively; arguments may be token or quoted-string; empty list
    elements are skipped (RFC 9110 Section 5.6.1).
    """
    text = ", ".join(lines)
    directives: dict[str, str | None] = {}
    repeated: set[str] = set()
    malformed: set[str] = set()
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos] in _OWS + ",":
            pos += 1
        if pos >= len(text):
            break
        token = _TOKEN.match(text, pos)
        if token is None:
            pos = _skip_element(text, pos)
            continue
        name = token.group().lower()
        pos = token.end()
        value: str | None = None
        ok = True
        if pos < len(text) and text[pos] == "=":
            pos += 1
            if pos < len(text) and text[pos] == '"':
                quoted = _quoted_string(text, pos)
                if quoted is None:
                    ok, pos = False, len(text)
                else:
                    value, pos = quoted
            elif (arg := _TOKEN.match(text, pos)) is not None:
                value, pos = arg.group(), arg.end()
            else:
                ok = False
        while pos < len(text) and text[pos] in _OWS:
            pos += 1
        if pos < len(text) and text[pos] != ",":
            ok = False
            pos = _skip_element(text, pos)
        if not ok:
            malformed.add(name)
        elif name in directives:
            repeated.add(name)
        else:
            directives[name] = value
    return CacheControl(directives, frozenset(repeated), frozenset(malformed))


def delta_seconds(value: str | None) -> int | None:
    """RFC 9111 Section 1.2.2 `1*DIGIT`, clamped to 2^31; None when absent or invalid."""
    if value is None or not value or not value.isascii() or not value.isdigit():
        return None
    if len(value.lstrip("0")) > _MAX_DELTA_DIGITS:
        return DELTA_SECONDS_INFINITY
    return min(int(value), DELTA_SECONDS_INFINITY)


# --- HTTP-date (RFC 9110 Section 5.6.7), matched case-insensitively (RFC 9111 Section 4.2) ---

_MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}
_D2 = "([0-9]{2})"
_TIME = f"{_D2}:{_D2}:{_D2}"
_MON = "([A-Za-z]{3})"
_IMF_FIXDATE = re.compile(
    rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), {_D2} {_MON} ([0-9]{{4}}) {_TIME} GMT", re.I
)
_RFC850_DATE = re.compile(
    rf"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, {_D2}-{_MON}-{_D2} {_TIME} GMT", re.I
)
_ASCTIME_DATE = re.compile(
    rf"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) {_MON} ([0-9]{{2}}| [0-9]) {_TIME} ([0-9]{{4}})", re.I
)


def _to_epoch(year: int, mon: str, day: int, hh: int, mm: int, ss: int) -> int | None:
    month = _MONTHS.get(mon.capitalize())
    if month is None or not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    if hh > 23 or mm > 59 or ss > 60:
        return None
    # A leap second (:60) is represented as the nearest earlier second (RFC 9111 Section 4.2).
    return calendar.timegm((year, month, day, hh, mm, min(ss, 59), 0, 0, 0))


def parse_http_date(value: str, *, now: int) -> int | None:
    """Return epoch seconds, or None for anything that is not one of the three formats in GMT.

    `now` resolves rfc850-date two-digit years: a year more than 50 years in the future is the
    most recent past year with the same last two digits (RFC 9110 Section 5.6.7).
    """
    value = value.strip(_OWS)
    if m := _IMF_FIXDATE.fullmatch(value):
        day, mon, year, hh, mm, ss = m.groups()
        return _to_epoch(int(year), mon, int(day), int(hh), int(mm), int(ss))
    if m := _RFC850_DATE.fullmatch(value):
        day, mon, yy, hh, mm, ss = m.groups()
        now_year = time.gmtime(now).tm_year
        year = now_year // 100 * 100 + int(yy)
        if year > now_year + 50:
            year -= 100
        return _to_epoch(year, mon, int(day), int(hh), int(mm), int(ss))
    if m := _ASCTIME_DATE.fullmatch(value):
        mon, day, hh, mm, ss, year = m.groups()
        return _to_epoch(int(year), mon, int(day), int(hh), int(mm), int(ss))
    return None


# --- Stored responses, freshness lifetime (Section 4.2.1), and age (Section 4.2.3) ---


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: Sequence[tuple[str, str]]  # every field line as received, any name case
    request_time: int  # cache clock when the request was sent
    response_time: int  # cache clock when the response was received

    def lines(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name]

    @property
    def cache_control(self) -> CacheControl:
        return parse_cache_control(self.lines("cache-control"))

    def date_value(self) -> int:
        """Date, or response_time when absent or invalid (RFC 9110 Section 6.6.1)."""
        dates = self.lines("date")
        parsed = parse_http_date(dates[0], now=self.response_time) if dates else None
        return self.response_time if parsed is None else parsed


class Source(StrEnum):
    S_MAXAGE = "s-maxage"
    MAX_AGE = "max-age"
    EXPIRES = "expires"
    HEURISTIC = "heuristic"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Lifetime:
    seconds: int
    source: Source


DuplicatePolicy = Literal["first", "stale"]


def freshness_lifetime(
    response: StoredResponse,
    *,
    shared: bool,
    duplicates: DuplicatePolicy = "first",
    heuristic_percent: int = 10,
) -> Lifetime:
    """First match of RFC 9111 Section 4.2.1; invalid or (by policy) repeated values -> 0."""
    cc = response.cache_control
    for name, source, applies in (
        ("s-maxage", Source.S_MAXAGE, shared),
        ("max-age", Source.MAX_AGE, True),
    ):
        if applies and cc.has(name):
            seconds = delta_seconds(cc.directives.get(name))
            if seconds is None or (duplicates == "stale" and name in cc.repeated):
                return Lifetime(0, source)
            return Lifetime(seconds, source)
    date = response.date_value()
    expires = response.lines("expires")
    if expires:
        stamp = parse_http_date(expires[0], now=response.response_time)
        if stamp is None or (duplicates == "stale" and len(expires) > 1):
            return Lifetime(0, Source.EXPIRES)  # Section 5.3: invalid, including "0", is past
        return Lifetime(max(0, stamp - date), Source.EXPIRES)
    heuristic_ok = (
        response.status in HEURISTIC_STATUSES
        or cc.has("public")
        or (not shared and cc.has("private"))
    )
    modified = response.lines("last-modified")
    if heuristic_ok and modified:
        stamp = parse_http_date(modified[0], now=response.response_time)
        if stamp is not None and stamp <= date:
            return Lifetime((date - stamp) * heuristic_percent // 100, Source.HEURISTIC)
    return Lifetime(0, Source.NONE)


def age_value(response: StoredResponse) -> int:
    """First member of Age; an invalid value is ignored, i.e. 0 (RFC 9111 Section 5.1)."""
    lines = response.lines("age")
    if not lines:
        return 0
    first = lines[0].split(",")[0].strip(_OWS)
    return delta_seconds(first) or 0


def current_age(response: StoredResponse, *, now: int, conservative: bool = True) -> int:
    """RFC 9111 Section 4.2.3, line for line."""
    apparent_age = max(0, response.response_time - response.date_value())
    response_delay = response.response_time - response.request_time
    corrected_age_value = age_value(response) + response_delay
    if conservative:
        corrected_initial_age = max(apparent_age, corrected_age_value)
    else:
        corrected_initial_age = corrected_age_value
    resident_time = now - response.response_time
    return min(corrected_initial_age + resident_time, DELTA_SECONDS_INFINITY)


# --- Putting the directives together for one cache and one request ---


class Reuse(StrEnum):
    NOT_STORABLE = "not-storable"
    FRESH = "fresh"  # serve from cache without contacting the origin
    VALIDATE = "validate"  # forward a conditional request first
    STALE_OK = "stale-ok"  # stale, but the client's max-stale permits serving it


@dataclass(frozen=True, slots=True)
class Assessment:
    reuse: Reuse
    lifetime: Lifetime
    age: int
    strip_fields: frozenset[str]  # qualified no-cache / private fields to drop on reuse
    skip_reload_validation: bool  # RFC 8246 immutable: a reload need not revalidate


_SHARED_AUTH_OVERRIDES = ("public", "s-maxage", "must-revalidate")  # RFC 9111 Section 3.5


def _storable(
    response: StoredResponse,
    cc: CacheControl,
    request: CacheControl,
    *,
    shared: bool,
    authorized: bool,
    understood_statuses: frozenset[int],
) -> bool:
    """The directive-driven part of RFC 9111 Section 3."""
    if request.has("no-store"):
        return False
    if cc.has("must-understand"):
        if response.status not in understood_statuses:
            return False
    elif cc.has("no-store"):
        return False
    if shared and cc.has("private") and not cc.field_names("private"):
        return False
    if shared and authorized and not any(cc.has(d) for d in _SHARED_AUTH_OVERRIDES):
        return False
    explicit = (
        cc.has("public")
        or (not shared and cc.has("private"))
        or bool(response.lines("expires"))
        or cc.has("max-age")
        or (shared and cc.has("s-maxage"))
    )
    return explicit or response.status in HEURISTIC_STATUSES


def assess(
    response: StoredResponse,
    *,
    shared: bool,
    now: int,
    request_lines: Sequence[str] = (),
    authorized: bool = False,
    secure: bool = True,
    duplicates: DuplicatePolicy = "first",
    understood_statuses: frozenset[int] = HEURISTIC_STATUSES,
) -> Assessment:
    cc = response.cache_control
    request = parse_cache_control(request_lines)
    lifetime = freshness_lifetime(response, shared=shared, duplicates=duplicates)
    age = current_age(response, now=now)
    strip = cc.field_names("no-cache")
    if shared:
        strip |= cc.field_names("private")
    if not _storable(
        response,
        cc,
        request,
        shared=shared,
        authorized=authorized,
        understood_statuses=understood_statuses,
    ):
        return Assessment(Reuse.NOT_STORABLE, lifetime, age, frozenset(), False)
    fresh = lifetime.seconds > age  # Section 4.2: response_is_fresh
    must_validate = (cc.has("no-cache") and not cc.field_names("no-cache")) or request.has(
        "no-cache"
    )
    req_max_age = delta_seconds(request.directives.get("max-age"))
    req_min_fresh = delta_seconds(request.directives.get("min-fresh"))
    if req_max_age is not None and age > req_max_age and not request.has("max-stale"):
        must_validate = True
    if req_min_fresh is not None and lifetime.seconds - age < req_min_fresh:
        must_validate = True
    if must_validate:
        return Assessment(Reuse.VALIDATE, lifetime, age, strip, False)
    if fresh:
        skip = cc.has("immutable") and secure
        return Assessment(Reuse.FRESH, lifetime, age, strip, skip)
    # Section 4.2.4: stale reuse is forbidden by these directives, whatever the client says.
    forbidden = cc.has("must-revalidate") or (
        shared and (cc.has("s-maxage") or cc.has("proxy-revalidate"))
    )
    if request.has("max-stale") and not forbidden:
        limit = delta_seconds(request.directives.get("max-stale"))
        if limit is None or age - lifetime.seconds <= limit:
            return Assessment(Reuse.STALE_OK, lifetime, age, strip, False)
    return Assessment(Reuse.VALIDATE, lifetime, age, strip, False)
