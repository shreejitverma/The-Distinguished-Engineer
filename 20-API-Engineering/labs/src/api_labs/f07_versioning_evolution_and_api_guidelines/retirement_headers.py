"""RFC 9745 Deprecation and RFC 8594 Sunset: strict codecs, a FastAPI route class, telemetry."""

from __future__ import annotations

import base64
import re
import threading
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import httpx
    from fastapi import Request, Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

SF_INTEGER_MAX = 999_999_999_999_999  # RFC 9651 Section 3.3.1
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SCOPE_KEY = "api_labs.retirement_policy"
POLICY_ATTR = "__api_retirement_policy__"
RETIREMENT_HEADERS = ("Deprecation", "Sunset", "Link")

_DIGITS = frozenset("0123456789")
_ALPHA = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_TOKEN_REST = _ALPHA | _DIGITS | frozenset("!#$%&'*+-.^_`|~:/")  # tchar, ":" and "/"
_KEY_FIRST = frozenset("abcdefghijklmnopqrstuvwxyz*")
_KEY_REST = _KEY_FIRST | frozenset("0123456789_-.")
_B64 = _ALPHA | frozenset("0123456789+/=")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAY = "Mon|Tue|Wed|Thu|Fri|Sat|Sun"
_DAY_L = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MON = "|".join(_MONTHS)
_TIME = r"(?P<H>[0-9]{2}):(?P<M>[0-9]{2}):(?P<S>[0-9]{2})"
_IMF_FIXDATE = re.compile(
    rf"(?:{_DAY}), (?P<d>[0-9]{{2}}) (?P<mon>{_MON}) (?P<y>[0-9]{{4}}) {_TIME} GMT"
)
_RFC850_DATE = re.compile(
    rf"(?:{_DAY_L}), (?P<d>[0-9]{{2}})-(?P<mon>{_MON})-(?P<y>[0-9]{{2}}) {_TIME} GMT"
)
_ASCTIME_DATE = re.compile(
    rf"(?:{_DAY}) (?P<mon>{_MON}) (?P<d>[0-9]{{2}}| [0-9]) {_TIME} (?P<y>[0-9]{{4}})"
)


class FieldParseError(ValueError):
    """A field value failed strict parsing; the recipient ignores the whole field."""


@dataclass(frozen=True, slots=True)
class Token:
    value: str


@dataclass(frozen=True, slots=True)
class SfDate:
    seconds: int


type BareItem = int | Decimal | str | Token | bytes | bool | SfDate


@dataclass(frozen=True, slots=True)
class SfItem:
    value: BareItem
    params: dict[str, BareItem]


class _Reader:
    """Cursor over an ASCII field value, mirroring the RFC 9651 Section 4.2 algorithms."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        char = self.peek()
        self.pos += 1
        return char

    def skip_sp(self) -> None:
        while self.peek() == " ":
            self.pos += 1

    def number(self) -> int | Decimal:  # RFC 9651 Section 4.2.4
        sign = 1
        if self.peek() == "-":
            self.take()
            sign = -1
        if self.peek() not in _DIGITS:
            raise FieldParseError("expected a DIGIT")
        digits, is_decimal = "", False
        while (char := self.peek()) in _DIGITS or (char == "." and not is_decimal):
            if char == ".":
                if len(digits) > 12:
                    raise FieldParseError("decimal integer part longer than 12 digits")
                is_decimal = True
            digits += self.take()
            if not is_decimal and len(digits) > 15:
                raise FieldParseError("integer longer than 15 digits")
            if is_decimal and len(digits) > 16:
                raise FieldParseError("decimal longer than 16 characters")
        if not is_decimal:
            return sign * int(digits)
        if digits.endswith(".") or len(digits.split(".")[1]) > 3:
            raise FieldParseError("malformed decimal fraction")
        return sign * Decimal(digits)

    def bare_item(self) -> BareItem:  # RFC 9651 Section 4.2.3.1
        char = self.peek()
        if char == "-" or char in _DIGITS:
            return self.number()
        if char == '"':
            return self.string()
        if char in _ALPHA or char == "*":
            start = self.pos
            while self.peek() in _TOKEN_REST:
                self.pos += 1
            return Token(self.text[start : self.pos])
        if char == ":":
            return self.byte_sequence()
        if char == "?":
            self.take()
            flag = self.take()
            if flag not in ("0", "1"):
                raise FieldParseError("malformed boolean")
            return flag == "1"
        if char == "@":  # RFC 9651 Section 4.2.9
            self.take()
            value = self.number()
            if isinstance(value, Decimal):
                raise FieldParseError("a Date must be an integer")
            return SfDate(value)
        if char == "%":
            return self.display_string()
        raise FieldParseError(f"unrecognized item type at {char!r}")

    def string(self) -> str:  # RFC 9651 Section 4.2.5
        self.take()
        out: list[str] = []
        while self.peek():
            char = self.take()
            if char == "\\":
                escaped = self.take()
                if escaped not in ('"', "\\"):
                    raise FieldParseError("bad escape in string")
                out.append(escaped)
            elif char == '"':
                return "".join(out)
            elif not " " <= char <= "~":
                raise FieldParseError("control character in string")
            else:
                out.append(char)
        raise FieldParseError("unterminated string")

    def byte_sequence(self) -> bytes:  # RFC 9651 Section 4.2.7
        self.take()
        end = self.text.find(":", self.pos)
        if end == -1:
            raise FieldParseError("unterminated byte sequence")
        content = self.text[self.pos : end]
        self.pos = end + 1
        if any(char not in _B64 for char in content):
            raise FieldParseError("character outside the base64 alphabet")
        try:
            return base64.b64decode(content + "=" * (-len(content) % 4))
        except ValueError as exc:
            raise FieldParseError("invalid base64") from exc

    def display_string(self) -> str:  # RFC 9651 Section 4.2.10
        if self.text[self.pos : self.pos + 2] != '%"':
            raise FieldParseError("malformed display string")
        self.pos += 2
        out = bytearray()
        while self.peek():
            char = self.take()
            if not " " <= char <= "~":
                raise FieldParseError("control character in display string")
            if char == "%":
                octet = self.text[self.pos : self.pos + 2]
                self.pos += 2
                if len(octet) != 2 or any(c not in "0123456789abcdef" for c in octet):
                    raise FieldParseError("bad percent-encoding in display string")
                out.append(int(octet, 16))
            elif char == '"':
                try:
                    return out.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise FieldParseError("display string is not UTF-8") from exc
            else:
                out.append(ord(char))
        raise FieldParseError("unterminated display string")

    def params(self) -> dict[str, BareItem]:  # RFC 9651 Section 4.2.3.2
        params: dict[str, BareItem] = {}
        while self.peek() == ";":
            self.take()
            self.skip_sp()
            if self.peek() not in _KEY_FIRST:
                raise FieldParseError("malformed parameter key")
            start = self.pos
            while self.peek() in _KEY_REST:
                self.pos += 1
            key = self.text[start : self.pos]
            value: BareItem = True
            if self.peek() == "=":
                self.take()
                value = self.bare_item()
            params[key] = value  # duplicate keys: the last one wins
        return params


def parse_sf_item(field_value: str) -> SfItem:
    """Parse a combined field value as an RFC 9651 Item (Section 4.2), failing strictly."""
    if not field_value.isascii():
        raise FieldParseError("field value is not ASCII")
    reader = _Reader(field_value)
    reader.skip_sp()
    item = SfItem(reader.bare_item(), reader.params())
    reader.skip_sp()
    if reader.pos != len(field_value):
        raise FieldParseError(f"trailing characters: {field_value[reader.pos :]!r}")
    return item


def parse_deprecation(field_value: str) -> SfDate:
    """RFC 9745 Section 2.1: the value MUST be an sf-date; any other Item type is ignored."""
    item = parse_sf_item(field_value)
    if not isinstance(item.value, SfDate):
        raise FieldParseError(f"Deprecation must be a Date, got {type(item.value).__name__}")
    return item.value


def serialize_sf_date(seconds: int) -> str:
    """RFC 9651 Section 4.1.10: "@" followed by a serialized Integer (Section 4.1.4)."""
    if isinstance(seconds, bool) or not -SF_INTEGER_MAX <= seconds <= SF_INTEGER_MAX:
        raise ValueError(f"Date out of the sf-integer range: {seconds!r}")
    return f"@{seconds}"


def sf_date_to_datetime(date: SfDate) -> datetime:
    """Map epoch seconds to an aware UTC datetime with portable integer arithmetic."""
    try:
        return EPOCH + timedelta(seconds=date.seconds)
    except OverflowError as exc:
        raise FieldParseError(f"Date {date.seconds} is outside datetime's year 1..9999") from exc


def format_http_date(moment: datetime) -> str:
    """RFC 9110 Section 5.6.7: senders MUST generate IMF-fixdate."""
    utc = _as_utc_whole_seconds(moment)
    return f"{utc:%a}, {utc.day:02d} {_MONTHS[utc.month - 1]} {utc.year:04d} {utc:%H:%M:%S} GMT"


def parse_http_date(field_value: str, *, now: datetime) -> datetime:
    """Parse all three RFC 9110 Section 5.6.7 formats, case-sensitively, into UTC."""
    if match := _IMF_FIXDATE.fullmatch(field_value) or _ASCTIME_DATE.fullmatch(field_value):
        year, two_digit_year = int(match["y"]), False
    elif match := _RFC850_DATE.fullmatch(field_value):
        year, two_digit_year = now.year // 100 * 100 + int(match["y"]), True
    else:
        raise FieldParseError(f"not an HTTP-date: {field_value!r}")
    month, day = _MONTHS.index(match["mon"]) + 1, int(match["d"])
    hour, minute, second = int(match["H"]), int(match["M"]), int(match["S"])
    if hour > 23 or minute > 59 or second > 60:
        raise FieldParseError("time-of-day out of range")
    # A two-digit year more than 50 years ahead of the clock, compared as instants, is the
    # most recent past year with those digits.
    fifty_years_back = (year - 50, month, day, hour, minute, second)
    if two_digit_year and fifty_years_back > now.utctimetuple()[:6]:
        year -= 100
    try:
        moment = datetime(year, month, day, hour, minute, min(second, 59), tzinfo=UTC)
    except ValueError as exc:
        raise FieldParseError(f"impossible calendar date: {field_value!r}") from exc
    return moment + timedelta(seconds=second - 59) if second == 60 else moment


def _as_utc_whole_seconds(moment: datetime) -> datetime:
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError("naive datetime: HTTP dates are UTC instants, attach a timezone")
    if offset:
        raise ValueError(f"datetime must be UTC, got offset {offset}")
    if moment.microsecond:
        raise ValueError("sf-date and HTTP-date carry whole seconds only")
    return moment.astimezone(UTC)


def _link_target(uri: str) -> str:
    if any(char in uri for char in '<>" ,;') or not uri.isascii() or not uri.isprintable():
        raise ValueError(f"unsafe link target for a Link header: {uri!r}")
    return uri


@dataclass(frozen=True, slots=True)
class RetirementPolicy:
    """One endpoint's retirement schedule, validated against RFC 9745 Section 4."""

    deprecated_at: datetime
    sunset_at: datetime | None = None
    deprecation_doc: str | None = None
    sunset_doc: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "deprecated_at", _as_utc_whole_seconds(self.deprecated_at))
        if self.sunset_at is not None:
            sunset = _as_utc_whole_seconds(self.sunset_at)
            if sunset < self.deprecated_at:
                raise ValueError("Sunset MUST NOT be earlier than Deprecation (RFC 9745 Section 4)")
            object.__setattr__(self, "sunset_at", sunset)
        for uri in (self.deprecation_doc, self.sunset_doc):
            if uri is not None:
                _link_target(uri)

    def header_items(self) -> list[tuple[str, str]]:
        epoch_seconds = (self.deprecated_at - EPOCH) // timedelta(seconds=1)
        items = [("Deprecation", serialize_sf_date(epoch_seconds))]
        if self.sunset_at is not None:
            items.append(("Sunset", format_http_date(self.sunset_at)))
        links: list[str] = []
        if self.deprecation_doc is not None:
            links.append(f'<{self.deprecation_doc}>; rel="deprecation"; type="text/html"')
        if self.sunset_doc is not None:
            links.append(f'<{self.sunset_doc}>; rel="sunset"; type="text/html"')
        if links:
            items.append(("Link", ", ".join(links)))
        return items


def deprecated[F: Callable[..., Any]](policy: RetirementPolicy) -> Callable[[F], F]:
    """Route decorator: attach *policy* to an endpoint served by a ``RetirementRoute``."""

    def mark(endpoint: F) -> F:
        setattr(endpoint, POLICY_ATTR, policy)
        return endpoint

    return mark


def policy_of(endpoint: Callable[..., Any]) -> RetirementPolicy | None:
    policy: object = getattr(endpoint, POLICY_ATTR, None)
    return policy if isinstance(policy, RetirementPolicy) else None


class RetirementRoute(APIRoute):
    """Marks OpenAPI ``deprecated`` and tags the ASGI scope; the middleware writes the headers.

    The tag is set in ``get_route_handler`` because FastAPI 0.162 rebuilds the ASGI app of
    an included route from ``get_route_handler()``, so wrapping ``self.app`` would be lost.
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if policy_of(endpoint) is not None:
            kwargs["deprecated"] = True
        super().__init__(path, endpoint, **kwargs)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        policy = policy_of(self.endpoint)
        if policy is None:
            return handler

        async def tag_then_handle(request: Request) -> Response:
            request.scope[SCOPE_KEY] = policy
            return await handler(request)

        return tag_then_handle


class RetirementHeadersMiddleware:
    """Append the policy headers to every response of a tagged request, errors included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            policy: object = scope.get(SCOPE_KEY)
            if message["type"] == "http.response.start" and isinstance(policy, RetirementPolicy):
                extra = [(k.lower().encode(), v.encode()) for k, v in policy.header_items()]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ApiRetirementWarning(FutureWarning):
    """Shown by default: FutureWarning targets end users, DeprecationWarning is hidden."""


type SignalState = Literal[
    "announced", "deprecated", "sunset-scheduled", "sunset-passed", "invalid"
]


@dataclass(frozen=True, slots=True)
class RetirementSignal:
    method: str
    path: str
    state: SignalState
    deprecated_at: datetime | None
    sunset_at: datetime | None
    docs: dict[str, str]
    problems: tuple[str, ...]


class RetirementTelemetry:
    """Client-side httpx hook: parse strictly, warn once per signal, count every call."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: set[tuple[str | None, ...]] = set()
        self.signals: list[RetirementSignal] = []
        self.calls: Counter[tuple[str, str]] = Counter()

    def observe(self, response: httpx.Response) -> RetirementSignal | None:
        raw_deprecation = response.headers.get("deprecation")
        raw_sunset = response.headers.get("sunset")
        if raw_deprecation is None and raw_sunset is None:
            return None
        now = self._clock()
        problems: list[str] = []
        deprecated_at = sunset_at = None
        if raw_deprecation is not None:
            try:
                deprecated_at = sf_date_to_datetime(parse_deprecation(raw_deprecation))
            except FieldParseError as exc:
                problems.append(f"Deprecation ignored: {exc}")
        if raw_sunset is not None:
            try:
                sunset_at = parse_http_date(raw_sunset, now=now)
            except FieldParseError as exc:
                problems.append(f"Sunset ignored: {exc}")
        if deprecated_at is not None and sunset_at is not None and sunset_at < deprecated_at:
            problems.append("Sunset earlier than Deprecation (RFC 9745 Section 4)")
        state: SignalState = "invalid"  # fields were present and none of them parsed
        if sunset_at is not None and sunset_at <= now:
            state = "sunset-passed"  # RFC 8594 Section 3: a past Sunset means "now"
        elif deprecated_at is not None:
            state = "deprecated" if deprecated_at <= now else "announced"
        elif sunset_at is not None:
            state = "sunset-scheduled"  # RFC 8594 alone: retiring without being deprecated
        docs = {
            rel: link["url"]
            for rels, link in response.links.items()
            if rels is not None
            for rel in rels.split()
            if rel in ("deprecation", "sunset")
        }
        key = (response.request.method, response.request.url.path)
        signal = RetirementSignal(
            key[0], key[1], state, deprecated_at, sunset_at, docs, tuple(problems)
        )
        dedup_key = (*key, state, raw_deprecation, raw_sunset)
        with self._lock:
            self.calls[key] += 1
            first = dedup_key not in self._seen
            if first:
                self._seen.add(dedup_key)
                self.signals.append(signal)
        if first:
            warnings.warn(
                f"{key[0]} {key[1]} is {state}; sunset={sunset_at}; docs={docs or None}",
                ApiRetirementWarning,
                stacklevel=2,
            )
        return signal

    async def async_hook(self, response: httpx.Response) -> None:
        self.observe(response)
