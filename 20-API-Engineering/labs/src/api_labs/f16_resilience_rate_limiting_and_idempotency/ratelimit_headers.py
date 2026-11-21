"""429, Retry-After, and RateLimit / RateLimit-Policy (draft-ietf-httpapi-ratelimit-headers-11).

Four layers, each usable alone:

1. A strict RFC 9651 List parser and a small serializer (the wire format of both fields).
2. Field profiles: `QuotaPolicy` (draft-11 Section 3) and `ServiceLimit` (draft-11 Section 4),
   where any malformed member makes the whole field ignored (draft-11 Section 7).
3. `Retry-After` (RFC 9110 Section 10.2.3) with a strict three-format HTTP-date parser
   (RFC 9110 Section 5.6.7), and `plan_next_request`, a client that applies the precedence rules.
4. `RateLimitMiddleware`, a pure ASGI fixed-window limiter that advertises its policies on every
   response and answers `429` with `Retry-After` and an RFC 9457 `quota-exceeded` problem.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------------------------
# 1. RFC 9651 List parsing and serialization
# ---------------------------------------------------------------------------------------------

_DIGITS = frozenset("0123456789")
_LCALPHA = frozenset("abcdefghijklmnopqrstuvwxyz")
_ALPHA = _LCALPHA | frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_TCHAR = _ALPHA | _DIGITS | frozenset("!#$%&'*+-.^_`|~")
_KEY_CHARS = _LCALPHA | _DIGITS | frozenset("_-.*")
_BASE64 = _ALPHA | _DIGITS | frozenset("+/=")
_LC_HEX = frozenset("0123456789abcdef")
_MAX_INTEGER = 999_999_999_999_999  # RFC 9651 Section 3.3.1


class StructuredFieldError(ValueError):
    """The field failed RFC 9651 parsing or a profile rule; the whole field is ignored."""


@dataclass(frozen=True, slots=True)
class Token:
    """An sf-token, distinct from an sf-string (RFC 9651 Section 3.3.4)."""

    value: str


@dataclass(frozen=True, slots=True)
class SfDate:
    """An sf-date, seconds since the epoch (RFC 9651 Section 3.3.7)."""

    seconds: int


@dataclass(frozen=True, slots=True)
class DisplayString:
    """An sf-displaystring, percent-encoded UTF-8 on the wire (RFC 9651 Section 3.3.8)."""

    value: str


type BareItem = int | Decimal | str | Token | bytes | bool | SfDate | DisplayString
type Params = dict[str, BareItem]


@dataclass(frozen=True, slots=True)
class Item:
    value: BareItem
    params: Params


@dataclass(frozen=True, slots=True)
class InnerList:
    items: tuple[Item, ...]
    params: Params


class _Cursor:
    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        ch = self.peek()
        self.pos += 1 if ch else 0
        return ch

    def skip(self, chars: str) -> None:
        while self.peek() and self.peek() in chars:
            self.pos += 1


def combine_field_lines(lines: Sequence[str]) -> str:
    """Join same-named field lines in order with comma SP (RFC 9110 Section 5.3)."""
    return ", ".join(lines)


def parse_sf_list(field_value: str) -> list[Item | InnerList]:
    """RFC 9651 Section 4.2 with field_type "list", then Section 4.2.1."""
    if not field_value.isascii():
        raise StructuredFieldError("field value is not ASCII")
    cur = _Cursor(field_value)
    cur.skip(" ")
    members: list[Item | InnerList] = []
    while cur.peek():
        members.append(_parse_inner_list(cur) if cur.peek() == "(" else _parse_item(cur))
        cur.skip(" \t")
        if not cur.peek():
            return members
        if cur.take() != ",":
            raise StructuredFieldError(f"expected ',' at offset {cur.pos - 1}")
        cur.skip(" \t")
        if not cur.peek():
            raise StructuredFieldError("trailing comma")
    return members


def _parse_inner_list(cur: _Cursor) -> InnerList:
    cur.take()  # "("
    items: list[Item] = []
    while cur.peek():
        cur.skip(" ")
        if cur.peek() == ")":
            cur.take()
            return InnerList(tuple(items), _parse_params(cur))
        items.append(_parse_item(cur))
        if cur.peek() not in (" ", ")"):
            raise StructuredFieldError("inner list members need SP or ')'")
    raise StructuredFieldError("inner list not closed")


def _parse_item(cur: _Cursor) -> Item:
    value = _parse_bare_item(cur)
    return Item(value, _parse_params(cur))


def _parse_bare_item(cur: _Cursor) -> BareItem:
    ch = cur.peek()
    if ch == "-" or (ch and ch in _DIGITS):
        return _parse_number(cur)
    if ch == '"':
        return _parse_string(cur)
    if ch == "*" or (ch and ch in _ALPHA):
        return _parse_token(cur)
    if ch == ":":
        return _parse_byte_sequence(cur)
    if ch == "?":
        cur.take()
        flag = cur.take()
        if flag not in ("0", "1"):
            raise StructuredFieldError("boolean must be ?0 or ?1")
        return flag == "1"
    if ch == "@":
        cur.take()
        seconds = _parse_number(cur)
        if not isinstance(seconds, int):
            raise StructuredFieldError("a date must be an integer")
        return SfDate(seconds)
    if ch == "%":
        return _parse_display_string(cur)
    raise StructuredFieldError(f"no bare item starts with {ch!r}")


def _parse_number(cur: _Cursor) -> int | Decimal:
    """RFC 9651 Section 4.2.4: 15 integer digits, or 12 + '.' + 3 decimal digits."""
    sign = -1 if cur.peek() == "-" else 1
    if sign == -1:
        cur.take()
    if not cur.peek() or cur.peek() not in _DIGITS:
        raise StructuredFieldError("number needs a digit")
    start = cur.pos
    is_decimal = False
    while (ch := cur.peek()) and (ch in _DIGITS or (ch == "." and not is_decimal)):
        if ch == ".":
            if cur.pos - start > 12:
                raise StructuredFieldError("decimal integer part longer than 12 digits")
            is_decimal = True
        cur.take()
        if cur.pos - start > (16 if is_decimal else 15):
            raise StructuredFieldError("number too long")
    text = cur.text[start : cur.pos]
    if not is_decimal:
        return sign * int(text)
    if text.endswith(".") or len(text.split(".")[1]) > 3:
        raise StructuredFieldError("decimal needs 1 to 3 fractional digits")
    return sign * Decimal(text)


def _parse_string(cur: _Cursor) -> str:
    cur.take()  # opening DQUOTE
    out: list[str] = []
    while ch := cur.take():
        if ch == "\\":
            escaped = cur.take()
            if escaped not in ('"', "\\"):
                raise StructuredFieldError('only \\" and \\\\ escapes are allowed')
            out.append(escaped)
        elif ch == '"':
            return "".join(out)
        elif not " " <= ch <= "~":
            raise StructuredFieldError("control character in string")
        else:
            out.append(ch)
    raise StructuredFieldError("string not closed")


def _parse_token(cur: _Cursor) -> Token:
    start = cur.pos
    cur.take()
    while cur.peek() and (cur.peek() in _TCHAR or cur.peek() in ":/"):
        cur.take()
    return Token(cur.text[start : cur.pos])


def _parse_byte_sequence(cur: _Cursor) -> bytes:
    cur.take()  # ":"
    end = cur.text.find(":", cur.pos)
    if end == -1:
        raise StructuredFieldError("byte sequence not closed")
    content = cur.text[cur.pos : end]
    cur.pos = end + 1
    if not set(content) <= _BASE64:
        raise StructuredFieldError("character outside the base64 alphabet")
    # Section 4.2.7: synthesize missing padding and do not fail on non-zero pad bits.
    unpadded = content.rstrip("=")
    try:
        return base64.b64decode(unpadded + "=" * (-len(unpadded) % 4), validate=True)
    except binascii.Error as exc:
        raise StructuredFieldError(f"invalid base64: {exc}") from exc


def _parse_display_string(cur: _Cursor) -> DisplayString:
    cur.take()  # "%"
    if cur.take() != '"':
        raise StructuredFieldError('display string must start with %"')
    raw = bytearray()
    while ch := cur.take():
        if ch == '"':
            try:
                return DisplayString(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise StructuredFieldError("display string is not UTF-8") from exc
        if not " " <= ch <= "~":
            raise StructuredFieldError("control character in display string")
        if ch == "%":
            pair = cur.text[cur.pos : cur.pos + 2]
            if len(pair) != 2 or not set(pair) <= _LC_HEX:
                raise StructuredFieldError("bad percent escape in display string")
            raw.append(int(pair, 16))
            cur.pos += 2
        else:
            raw.append(ord(ch))
    raise StructuredFieldError("display string not closed")


def _parse_params(cur: _Cursor) -> Params:
    """RFC 9651 Section 4.2.3.2: a later duplicate key overwrites the earlier value."""
    params: Params = {}
    while cur.peek() == ";":
        cur.take()
        cur.skip(" ")
        start = cur.pos
        if not cur.peek() or cur.peek() not in (_LCALPHA | {"*"}):
            raise StructuredFieldError("parameter key must start with lcalpha or '*'")
        while cur.peek() and cur.peek() in _KEY_CHARS:
            cur.take()
        key = cur.text[start : cur.pos]
        value: BareItem = True
        if cur.peek() == "=":
            cur.take()
            value = _parse_bare_item(cur)
        params[key] = value
    return params


def _serialize_bare(value: BareItem) -> str:
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, int):
        if not -_MAX_INTEGER <= value <= _MAX_INTEGER:
            raise StructuredFieldError("integer outside the 15-digit range")
        return str(value)
    if isinstance(value, str):
        if not all(" " <= ch <= "~" for ch in value):
            raise StructuredFieldError("strings must be printable ASCII")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bytes):
        return ":" + base64.b64encode(value).decode("ascii") + ":"
    if isinstance(value, Token):
        return value.value
    raise StructuredFieldError(f"this lab does not serialize {type(value).__name__}")


def serialize_item(value: BareItem, params: Sequence[tuple[str, BareItem | None]]) -> str:
    """Serialize one List member; None parameters are omitted, true Booleans lose `=?1`."""
    out = [_serialize_bare(value)]
    for key, param in params:
        if param is None:
            continue
        out.append(f";{key}" if param is True else f";{key}={_serialize_bare(param)}")
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# 2. RateLimit-Policy and RateLimit profiles (draft-ietf-httpapi-ratelimit-headers-11)
# ---------------------------------------------------------------------------------------------

REGISTERED_QUOTA_UNITS = frozenset({"requests", "content-bytes", "concurrent-requests"})


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """One Quota Policy Item (draft-11 Section 3.1)."""

    name: str
    quota: int
    window: int | None = None
    unit: str = "requests"
    partition_key: bytes | None = None

    def serialize(self) -> str:
        unit = None if self.unit == "requests" else self.unit
        params = [("q", self.quota), ("qu", unit), ("w", self.window), ("pk", self.partition_key)]
        return serialize_item(self.name, params)


@dataclass(frozen=True, slots=True)
class ServiceLimit:
    """One Service Limit Item (draft-11 Section 4.1)."""

    policy: str
    available: int
    effective_window: int | None = None
    partition_key: bytes | None = None

    def serialize(self) -> str:
        params = [("r", self.available), ("t", self.effective_window), ("pk", self.partition_key)]
        return serialize_item(self.policy, params)


def _non_negative_int(value: BareItem | None) -> int | None:
    # `type(...) is int`, not isinstance: a bare `;q` parses to True, and bool subclasses int.
    return value if type(value) is int and value >= 0 else None


def _items_with_string_names(field_lines: Sequence[str]) -> list[Item] | None:
    if not field_lines:
        return None
    try:
        members = parse_sf_list(combine_field_lines(field_lines))
    except StructuredFieldError:
        return None
    items: list[Item] = []
    for member in members:
        if not isinstance(member, Item) or not isinstance(member.value, str):
            return None
        items.append(member)
    names = [item.value for item in items]
    if not items or len(set(names)) != len(names):
        return None
    return items


def parse_ratelimit_policy(field_lines: Sequence[str]) -> tuple[QuotaPolicy, ...] | None:
    """Parse every `RateLimit-Policy` line; None means absent or malformed, so ignore it."""
    items = _items_with_string_names(field_lines)
    if items is None:
        return None
    policies: list[QuotaPolicy] = []
    for item in items:
        quota = _non_negative_int(item.params.get("q"))  # REQUIRED (Section 3.1.1)
        raw_window = item.params.get("w")
        window = _non_negative_int(raw_window)
        unit = item.params.get("qu", "requests")
        pk = item.params.get("pk")
        if quota is None or not isinstance(unit, str):
            return None
        if raw_window is not None and not window:  # Section 3.1.3: non-negative, non-zero
            return None
        if pk is not None and not isinstance(pk, bytes):
            return None
        policies.append(QuotaPolicy(str(item.value), quota, window, unit, pk))
    return tuple(policies)


def parse_ratelimit(field_lines: Sequence[str]) -> tuple[ServiceLimit, ...] | None:
    """Parse every `RateLimit` line; None means absent or malformed (draft-11 Section 7)."""
    items = _items_with_string_names(field_lines)
    if items is None:
        return None
    limits: list[ServiceLimit] = []
    for item in items:
        available = _non_negative_int(item.params.get("r"))  # REQUIRED (Section 4.1.1)
        raw_window = item.params.get("t")
        window = _non_negative_int(raw_window)
        pk = item.params.get("pk")
        if available is None or (raw_window is not None and window is None):
            return None
        if pk is not None and not isinstance(pk, bytes):
            return None
        limits.append(ServiceLimit(str(item.value), available, window, pk))
    return tuple(limits)


# ---------------------------------------------------------------------------------------------
# 3. Retry-After, HTTP-date, and the client decision
# ---------------------------------------------------------------------------------------------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY = "(?:" + "|".join(_DAY_NAMES) + ")"
_DAY_L = "(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MON = "(" + "|".join(_MONTHS) + ")"
_TIME = "([0-9]{2}):([0-9]{2}):([0-9]{2})"
_IMF_FIXDATE = re.compile(f"{_DAY}, ([0-9]{{2}}) {_MON} ([0-9]{{4}}) {_TIME} GMT")
_RFC850_DATE = re.compile(f"{_DAY_L}, ([0-9]{{2}})-{_MON}-([0-9]{{2}}) {_TIME} GMT")
_ASCTIME_DATE = re.compile(f"{_DAY} {_MON} ([0-9]{{2}}| [0-9]) {_TIME} ([0-9]{{4}})")


def _to_datetime(year: int, month: str, day: str, hh: str, mm: str, ss: str) -> datetime | None:
    try:
        # 23:59:60 is legal on the wire (leap second); datetime cannot hold it, so clamp.
        return datetime(
            year, _MONTHS.index(month) + 1, int(day), int(hh), int(mm), min(int(ss), 59), tzinfo=UTC
        )
    except ValueError:
        return None


def parse_http_date(value: str, *, now: datetime) -> datetime | None:
    """Accept the three formats of RFC 9110 Section 5.6.7, case-sensitively, always as UTC."""
    if m := _IMF_FIXDATE.fullmatch(value):
        day, month, year, hh, mm, ss = m.groups()
        return _to_datetime(int(year), month, day, hh, mm, ss)
    if m := _ASCTIME_DATE.fullmatch(value):
        month, day, hh, mm, ss, year = m.groups()
        return _to_datetime(int(year), month, day.strip(), hh, mm, ss)
    if m := _RFC850_DATE.fullmatch(value):
        day, month, yy, hh, mm, ss = m.groups()
        # Section 5.6.7: a two-digit year more than 50 years in the future means the past century.
        year = now.year - now.year % 100 + int(yy)
        if year > now.year + 50:
            year -= 100
        return _to_datetime(year, month, day, hh, mm, ss)
    return None


def format_http_date(when: datetime) -> str:
    """IMF-fixdate, the only format a sender may generate (RFC 9110 Section 5.6.7)."""
    u = when.astimezone(UTC)
    # Names come from fixed tables, not strftime("%a"), which follows the process locale.
    return (
        f"{_DAY_NAMES[u.weekday()]}, {u.day:02d} {_MONTHS[u.month - 1]} {u.year:04d} "
        f"{u.hour:02d}:{u.minute:02d}:{u.second:02d} GMT"
    )


def parse_retry_after(
    field_lines: Sequence[str], *, now: datetime, date_header: datetime | None = None
) -> float | None:
    """Seconds to wait, or None when absent or invalid (RFC 9110 Section 10.2.3).

    Retry-After is a singleton field, so two lines are invalid (RFC 9110 Section 5.3).
    An HTTP-date is measured against the response's own `Date` when present, which cancels
    client clock skew, and against the local clock otherwise.
    """
    if len(field_lines) != 1:
        return None
    value = field_lines[0].strip(" \t")
    if value and set(value) <= _DIGITS:  # delay-seconds = 1*DIGIT, ASCII digits only
        digits = value.lstrip("0") or "0"
        # Past 15 digits the wait is absurd; inf avoids float() overflow and int()'s digit limit.
        return float(int(digits)) if len(digits) <= 15 else math.inf
    when = parse_http_date(value, now=now)
    if when is None:
        return None
    return max(0.0, (when - (date_header or now)).total_seconds())


@dataclass(frozen=True, slots=True)
class Decision:
    action: Literal["send", "wait", "backoff", "give-up"]
    delay: float
    source: Literal["retry-after", "ratelimit", "none"]


def _lines(headers: Sequence[tuple[str, str]], name: str) -> list[str]:
    return [value for key, value in headers if key.lower() == name]


def plan_next_request(
    status: int,
    headers: Sequence[tuple[str, str]],
    *,
    now: datetime,
    max_delay: float = 600.0,
    partition_key: bytes | None = None,
) -> Decision:
    """Decide what to do after a response, following draft-11 Sections 7, 7.3, and 8.5.1."""
    date_lines = _lines(headers, "date")
    date_header = parse_http_date(date_lines[0], now=now) if len(date_lines) == 1 else None
    retry_after = parse_retry_after(
        _lines(headers, "retry-after"), now=now, date_header=date_header
    )
    if retry_after is not None:  # Section 7: Retry-After MUST take precedence
        if retry_after > max_delay:
            return Decision("give-up", retry_after, "retry-after")
        return Decision("wait" if retry_after else "send", retry_after, "retry-after")
    age_lines = _lines(headers, "age")
    # ASCII digits only: str.isdigit() accepts "\u00b2", which int() then rejects.
    age = age_lines[0].strip(" \t") if len(age_lines) == 1 else ""
    from_cache = bool(age) and set(age) <= _DIGITS and age.strip("0") != ""
    limits = None if from_cache else parse_ratelimit(_lines(headers, "ratelimit"))
    for limit in limits or ():
        if partition_key is not None and limit.partition_key not in (None, partition_key):
            continue
        if limit.available == 0 and limit.effective_window is not None:
            delay = float(limit.effective_window)
            if delay > max_delay:
                return Decision("give-up", delay, "ratelimit")
            return Decision("wait", delay, "ratelimit")
    if status in (429, 503):
        return Decision("backoff", 0.0, "none")  # no usable hint: caller's jittered backoff
    return Decision("send", 0.0, "none")


# ---------------------------------------------------------------------------------------------
# 4. Server: a fixed-window limiter that speaks the draft
# ---------------------------------------------------------------------------------------------

QUOTA_EXCEEDED = "https://iana.org/assignments/http-problem-types#quota-exceeded"
_PK_CONTEXT = b"ratelimit-pk-v1:"


def partition_from_api_key(scope: Scope) -> tuple[bytes, bytes | None]:
    """Return (state key, advertised pk).

    With exactly one `x-api-key`, pk is the first 9 bytes of SHA-256 over a fixed context and
    the key: the client can predict it (draft-11 Section 6.1) and the key is not disclosed.
    Anonymous callers are keyed by peer address and get no pk, because an unsalted hash of an
    IPv4 address is reversible by enumerating 2**32 inputs.
    """
    keys = [v for k, v in scope["headers"] if k == b"x-api-key"]
    if len(keys) == 1:
        pk = hashlib.sha256(_PK_CONTEXT + keys[0]).digest()[:9]
        return pk, pk
    client = scope.get("client")
    host = client[0] if client else "unknown"
    return b"ip:" + host.encode("ascii", "replace"), None


@dataclass(frozen=True, slots=True)
class PolicySpec:
    name: str
    quota: int
    window: int


@dataclass(slots=True)
class _Window:
    start: float
    used: int = 0


class RateLimitMiddleware:
    """Fixed windows per (partition, policy); rejected requests do not consume quota."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        policies: Sequence[PolicySpec],
        clock: Callable[[], float],
        partition: Callable[[Scope], tuple[bytes, bytes | None]] = partition_from_api_key,
        jitter: Callable[[], int] = lambda: 0,
    ) -> None:
        self.app = app
        self.policies = tuple(policies)
        self.clock = clock
        self.partition = partition
        self.jitter = jitter
        self.windows: dict[tuple[bytes, str], _Window] = {}

    def _window(self, key: bytes, policy: PolicySpec, now: float) -> _Window:
        window = self.windows.get((key, policy.name))
        if window is None or now >= window.start + policy.window:
            window = self.windows[(key, policy.name)] = _Window(start=now)
        return window

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        now = self.clock()
        key, pk = self.partition(scope)
        windows = [(p, self._window(key, p, now)) for p in self.policies]
        policy_line = ", ".join(
            QuotaPolicy(p.name, p.quota, p.window, partition_key=pk).serialize()
            for p in self.policies
        )
        violated = [(p, w) for p, w in windows if w.used >= p.quota]
        if not violated:
            for _, window in windows:
                window.used += 1

        def remaining_window(policy: PolicySpec, window: _Window) -> int:
            # ceil: flooring 0.4 s to t=0 would invite a request into a still-closed window.
            return math.ceil(window.start + policy.window - now)

        ratelimit_line = ", ".join(
            ServiceLimit(p.name, p.quota - w.used, remaining_window(p, w), pk).serialize()
            for p, w in windows
        )
        extra = [
            (b"ratelimit-policy", policy_line.encode("ascii")),
            (b"ratelimit", ratelimit_line.encode("ascii")),
        ]
        if violated:
            # Section 6: Retry-After SHOULD NOT point before the end of the effective window,
            # so a negative jitter is clamped to 0 rather than trusted.
            wait = max(remaining_window(p, w) for p, w in violated) + max(0, self.jitter())
            body = json.dumps(
                {
                    "type": QUOTA_EXCEEDED,
                    "title": "Quota Exceeded",
                    "status": 429,
                    "violated-policies": [p.name for p, _ in violated],
                },
                separators=(",", ":"),
            ).encode("ascii")
            headers = [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", str(wait).encode("ascii")),
                *extra,
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_fields(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Append, never replace: an upstream limit stays visible (draft-11 Section 7.2).
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_fields)


def utc(seconds: float) -> datetime:
    """Epoch seconds -> aware UTC datetime, for deterministic tests."""
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)
