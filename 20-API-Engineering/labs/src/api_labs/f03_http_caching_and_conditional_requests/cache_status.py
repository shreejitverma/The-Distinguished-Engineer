"""RFC 9211 Cache-Status: a strict RFC 9651 List codec and a tiered ASGI cache that emits it."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import math
import string
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# RFC 9651 Section 3.3.1: Integers are at most fifteen decimal digits, signed.
MAX_SF_INTEGER: Final = 999_999_999_999_999
_DIGITS: Final = frozenset(string.digits)
_ALPHA: Final = frozenset(string.ascii_letters)
_LCALPHA: Final = frozenset(string.ascii_lowercase)
_TCHAR: Final = frozenset("!#$%&'*+-.^_`|~") | _DIGITS | _ALPHA
_TOKEN_CHARS: Final = _TCHAR | {":", "/"}
_KEY_CHARS: Final = _LCALPHA | _DIGITS | {"_", "-", ".", "*"}
_BASE64_CHARS: Final = _ALPHA | _DIGITS | {"+", "/", "="}
_LOWER_HEX: Final = frozenset("0123456789abcdef")
_OWS: Final = " \t"

# RFC 9211 Section 2.2, most specific to least.
FWD_REASONS: Final = (
    "bypass",
    "method",
    "uri-miss",
    "vary-miss",
    "miss",
    "request",
    "stale",
    "partial",
)
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class StructuredFieldError(ValueError):
    """A field value failed RFC 9651 parsing, serialization, or a field-specific constraint."""


@dataclass(frozen=True, slots=True)
class Token:
    """An RFC 9651 Token (Section 3.3.4); distinct from a String on the wire."""

    value: str


@dataclass(frozen=True, slots=True)
class Date:
    """An RFC 9651 Date (Section 3.3.7): integer seconds since the Unix epoch."""

    seconds: int


@dataclass(frozen=True, slots=True)
class DisplayString:
    """An RFC 9651 Display String (Section 3.3.8): Unicode text, percent-encoded UTF-8."""

    value: str


type BareItem = bool | int | Decimal | str | Token | bytes | Date | DisplayString
type Parameters = dict[str, BareItem]


@dataclass(frozen=True, slots=True)
class InnerList:
    """An RFC 9651 Inner List (Section 3.1.1)."""

    items: tuple[tuple[BareItem, Parameters], ...]


type Member = tuple[BareItem | InnerList, Parameters]


class _Cursor:
    """The mutable input_string of the RFC 9651 Section 4.2 parsing algorithms."""

    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def at_end(self) -> bool:
        return self.pos >= len(self.text)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        char = self.peek()
        self.pos += 1
        return char

    def skip(self, chars: str) -> None:
        while not self.at_end() and self.text[self.pos] in chars:
            self.pos += 1

    def fail(self, reason: str) -> StructuredFieldError:
        return StructuredFieldError(f"{reason} at offset {self.pos} in {self.text!r}")


def parse_list(field_value: str) -> list[Member]:
    """Parse a combined field value as an RFC 9651 List (Sections 4.2 and 4.2.1)."""
    if not field_value.isascii():
        raise StructuredFieldError(f"field value is not ASCII: {field_value!r}")
    cursor = _Cursor(field_value)
    cursor.skip(" ")
    members: list[Member] = []
    while not cursor.at_end():
        if cursor.peek() == "(":
            members.append(_parse_inner_list(cursor))
        else:
            members.append(_parse_item(cursor))
        cursor.skip(_OWS)
        if cursor.at_end():
            return members
        if cursor.take() != ",":
            raise cursor.fail("expected ',' between list members")
        cursor.skip(_OWS)
        if cursor.at_end():
            raise cursor.fail("trailing comma")
    return members


def _parse_inner_list(cursor: _Cursor) -> Member:
    cursor.take()
    items: list[tuple[BareItem, Parameters]] = []
    while not cursor.at_end():
        cursor.skip(" ")
        if cursor.peek() == ")":
            cursor.take()
            return InnerList(tuple(items)), _parse_parameters(cursor)
        items.append(_parse_item(cursor))
        if cursor.peek() not in (" ", ")"):
            raise cursor.fail("expected SP or ')' in inner list")
    raise cursor.fail("unterminated inner list")


def _parse_item(cursor: _Cursor) -> tuple[BareItem, Parameters]:
    return _parse_bare_item(cursor), _parse_parameters(cursor)


def _parse_bare_item(cursor: _Cursor) -> BareItem:
    first = cursor.peek()
    if first == "-" or first in _DIGITS:
        return _parse_number(cursor)
    if first == '"':
        return _parse_string(cursor)
    if first in _ALPHA or first == "*":
        return _parse_token(cursor)
    if first == ":":
        return _parse_byte_sequence(cursor)
    if first == "?":
        return _parse_boolean(cursor)
    if first == "@":
        cursor.take()
        seconds = _parse_number(cursor)
        if isinstance(seconds, Decimal):
            raise cursor.fail("a Date must be an Integer")
        return Date(seconds)
    if first == "%":
        return _parse_display_string(cursor)
    raise cursor.fail("unrecognized item type")


def _parse_parameters(cursor: _Cursor) -> Parameters:
    parameters: Parameters = {}
    while cursor.peek() == ";":
        cursor.take()
        cursor.skip(" ")
        key = _parse_key(cursor)
        value: BareItem = True
        if cursor.peek() == "=":
            cursor.take()
            value = _parse_bare_item(cursor)
        # Section 4.2.3.2 step 7: a duplicate key overwrites the value in place.
        parameters[key] = value
    return parameters


def _parse_key(cursor: _Cursor) -> str:
    if cursor.peek() not in _LCALPHA and cursor.peek() != "*":
        raise cursor.fail("a key must start with lcalpha or '*'")
    start = cursor.pos
    while not cursor.at_end() and cursor.peek() in _KEY_CHARS:
        cursor.take()
    return cursor.text[start : cursor.pos]


def _parse_number(cursor: _Cursor) -> int | Decimal:
    sign = 1
    is_decimal = False
    digits: list[str] = []
    if cursor.peek() == "-":
        cursor.take()
        sign = -1
    if cursor.at_end() or cursor.peek() not in _DIGITS:
        raise cursor.fail("expected a digit")
    while not cursor.at_end():
        char = cursor.peek()
        if char in _DIGITS:
            digits.append(cursor.take())
        elif not is_decimal and char == ".":
            if len(digits) > 12:
                raise cursor.fail("decimal integer component longer than 12 digits")
            digits.append(cursor.take())
            is_decimal = True
        else:
            break
        if not is_decimal and len(digits) > 15:
            raise cursor.fail("integer longer than 15 digits")
        if is_decimal and len(digits) > 16:
            raise cursor.fail("decimal longer than 16 characters")
    number = "".join(digits)
    if not is_decimal:
        return sign * int(number)
    if number.endswith(".") or len(number) - number.index(".") - 1 > 3:
        raise cursor.fail("decimal needs one to three fractional digits")
    return sign * Decimal(number)


def _parse_string(cursor: _Cursor) -> str:
    cursor.take()
    out: list[str] = []
    while not cursor.at_end():
        char = cursor.take()
        if char == "\\":
            if cursor.at_end():
                raise cursor.fail("backslash at end of string")
            escaped = cursor.take()
            if escaped not in ('"', "\\"):
                raise cursor.fail("only DQUOTE and backslash may be escaped")
            out.append(escaped)
        elif char == '"':
            return "".join(out)
        elif not " " <= char <= "~":
            raise cursor.fail("non-printable character in string")
        else:
            out.append(char)
    raise cursor.fail("unterminated string")


def _parse_token(cursor: _Cursor) -> Token:
    start = cursor.pos
    cursor.take()
    while not cursor.at_end() and cursor.peek() in _TOKEN_CHARS:
        cursor.take()
    return Token(cursor.text[start : cursor.pos])


def _parse_byte_sequence(cursor: _Cursor) -> bytes:
    cursor.take()
    end = cursor.text.find(":", cursor.pos)
    if end < 0:
        raise cursor.fail("unterminated byte sequence")
    content = cursor.text[cursor.pos : end]
    cursor.pos = end + 1
    if any(char not in _BASE64_CHARS for char in content):
        raise cursor.fail("byte sequence outside the base64 alphabet")
    try:
        # Section 4.2.7 step 7: synthesize missing padding instead of failing.
        return base64.b64decode(content.rstrip("=") + "=" * (-len(content.rstrip("=")) % 4))
    except binascii.Error as exc:
        raise cursor.fail("invalid base64") from exc


def _parse_boolean(cursor: _Cursor) -> bool:
    cursor.take()
    value = cursor.take()
    if value == "1":
        return True
    if value == "0":
        return False
    raise cursor.fail("a Boolean must be ?0 or ?1")


def _parse_display_string(cursor: _Cursor) -> DisplayString:
    cursor.take()
    if cursor.take() != '"':
        raise cursor.fail('a Display String starts with %"')
    octets = bytearray()
    while not cursor.at_end():
        char = cursor.take()
        if not " " <= char <= "~":
            raise cursor.fail("non-printable character in display string")
        if char == "%":
            octet_hex = cursor.text[cursor.pos : cursor.pos + 2]
            cursor.pos += 2
            if len(octet_hex) != 2 or any(c not in _LOWER_HEX for c in octet_hex):
                raise cursor.fail("percent escape needs two lowercase hex digits")
            octets.append(int(octet_hex, 16))
        elif char == '"':
            try:
                return DisplayString(octets.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise cursor.fail("display string is not UTF-8") from exc
        else:
            octets.append(ord(char))
    raise cursor.fail("unterminated display string")


def serialize_bare_item(value: BareItem) -> str:
    """Serialize a bare Item (RFC 9651 Section 4.1.3.1); Decimal, Date, Display String omitted."""
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, int):
        if not -MAX_SF_INTEGER <= value <= MAX_SF_INTEGER:
            raise StructuredFieldError(f"integer out of range: {value}")
        return str(value)
    if isinstance(value, Token):
        text = value.value
        if not text or (text[0] not in _ALPHA and text[0] != "*"):
            raise StructuredFieldError(f"a Token must start with ALPHA or '*': {text!r}")
        if any(char not in _TOKEN_CHARS for char in text[1:]):
            raise StructuredFieldError(f"invalid Token character in {text!r}")
        return text
    if isinstance(value, str):
        if any(not " " <= char <= "~" for char in value):
            raise StructuredFieldError(f"a String must be printable ASCII: {value!r}")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bytes):
        return ":" + base64.b64encode(value).decode("ascii") + ":"
    raise StructuredFieldError(f"serializing {type(value).__name__} is not implemented")


def serialize_parameters(parameters: Iterable[tuple[str, BareItem]]) -> str:
    """Serialize Parameters (RFC 9651 Section 4.1.1.2); Boolean true omits its value."""
    out: list[str] = []
    for key, value in parameters:
        if not key or (key[0] not in _LCALPHA and key[0] != "*"):
            raise StructuredFieldError(f"a key must start with lcalpha or '*': {key!r}")
        if any(char not in _KEY_CHARS for char in key):
            raise StructuredFieldError(f"invalid key character in {key!r}")
        out.append(f";{key}" if value is True else f";{key}={serialize_bare_item(value)}")
    return "".join(out)


@dataclass(frozen=True, slots=True)
class CacheStatusEntry:
    """One `Cache-Status` List member: one cache's account of a request (RFC 9211 Section 2)."""

    cache: str | Token
    hit: bool = False
    fwd: str | None = None
    fwd_status: int | None = None
    ttl: int | None = None
    stored: bool | None = None
    collapsed: bool | None = None
    key: str | None = None
    detail: str | Token | None = None
    extensions: tuple[tuple[str, BareItem], ...] = ()

    def parameters(self) -> list[tuple[str, BareItem]]:
        """Return the RFC 9211 parameters in a fixed order, omitting the absent ones."""
        params: list[tuple[str, BareItem]] = []
        if self.hit:
            params.append(("hit", True))
        if self.fwd is not None:
            params.append(("fwd", Token(self.fwd)))
        optional: tuple[tuple[str, BareItem | None], ...] = (
            ("fwd-status", self.fwd_status),
            ("ttl", self.ttl),
            ("stored", self.stored),
            ("collapsed", self.collapsed),
            ("key", self.key),
            ("detail", self.detail),
        )
        params.extend((name, value) for name, value in optional if value is not None)
        params.extend(self.extensions)
        return params

    def serialize(self) -> str:
        """Serialize this member as it appears in one `Cache-Status` field line."""
        return serialize_bare_item(self.cache) + serialize_parameters(self.parameters())


# RFC 9211 Section 2 types per parameter; RFC 9651 Section 2.2 says a violation voids the field.
_PARAM_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "hit": (bool,),
    "fwd": (Token,),
    "fwd-status": (int,),
    "ttl": (int,),
    "stored": (bool,),
    "collapsed": (bool,),
    "key": (str,),
    "detail": (str, Token),
}


def _checked(name: str, value: BareItem) -> BareItem:
    allowed = _PARAM_TYPES[name]
    is_bool_as_int = isinstance(value, bool) and bool not in allowed
    if is_bool_as_int or not isinstance(value, allowed):
        raise StructuredFieldError(f"Cache-Status parameter {name} has the wrong type: {value!r}")
    return value


def parse_cache_status(field_lines: Sequence[str]) -> tuple[CacheStatusEntry, ...]:
    """Parse every `Cache-Status` field line, origin-side member first (RFC 9211 Section 2)."""
    # RFC 9651 Section 4.2: combine all field lines into one comma-separated value first.
    members = parse_list(", ".join(field_lines))
    entries: list[CacheStatusEntry] = []
    for item, params in members:
        if not isinstance(item, (str, Token)):
            raise StructuredFieldError(f"a cache identifier must be a String or Token: {item!r}")
        known = {n: _checked(n, v) for n, v in params.items() if n in _PARAM_TYPES}
        fwd = known.get("fwd")
        detail = known.get("detail")
        entries.append(
            CacheStatusEntry(
                cache=item,
                hit=known.get("hit") is True,
                fwd=fwd.value if isinstance(fwd, Token) else None,
                fwd_status=_int_or_none(known.get("fwd-status")),
                ttl=_int_or_none(known.get("ttl")),
                stored=_bool_or_none(known.get("stored")),
                collapsed=_bool_or_none(known.get("collapsed")),
                key=_str_or_none(known.get("key")),
                detail=detail if isinstance(detail, (str, Token)) else None,
                extensions=tuple((n, v) for n, v in params.items() if n not in _PARAM_TYPES),
            )
        )
    return tuple(entries)


def _int_or_none(value: BareItem | None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool_or_none(value: BareItem | None) -> bool | None:
    return value if isinstance(value, bool) else None


def _str_or_none(value: BareItem | None) -> str | None:
    return value if isinstance(value, str) else None


def cache_status_or_empty(field_lines: Sequence[str]) -> tuple[CacheStatusEntry, ...]:
    """Parse `Cache-Status`, treating any failure as an absent field (RFC 9651 Section 4.2)."""
    try:
        return parse_cache_status(field_lines)
    except StructuredFieldError:
        return ()


def effective_fwd_status(entry: CacheStatusEntry, response_status: int) -> int | None:
    """Return the next hop's status: `fwd-status`, else the sent status if `fwd` (Section 2.3)."""
    if entry.fwd is None:
        return None
    return entry.fwd_status if entry.fwd_status is not None else response_status


def lint_cache_status(entries: Sequence[CacheStatusEntry]) -> list[str]:
    """Report SHOULD-level and meaningfulness problems that a strict parser accepts."""
    problems: list[str] = []
    for index, entry in enumerate(entries):
        label = f"member {index} ({entry.serialize()})"
        if entry.hit and entry.fwd is not None:
            problems.append(f"{label}: hit and fwd are exclusive")
        if entry.fwd is None:
            fwd_only = (
                ("fwd-status", entry.fwd_status),
                ("stored", entry.stored),
                ("collapsed", entry.collapsed),
            )
            orphans = [name for name, value in fwd_only if value is not None]
            problems.extend(f"{label}: {name} is only meaningful with fwd" for name in orphans)
        elif entry.fwd not in FWD_REASONS:
            problems.append(f"{label}: fwd={entry.fwd} is not an RFC 9211 Section 2.2 reason")
    return problems


def _cache_control(raw_headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    """Parse `Cache-Control` directives into lowercase names (simplified, unquoted values)."""
    directives: dict[str, str] = {}
    for name, value in raw_headers:
        if name.lower() != b"cache-control":
            continue
        for part in value.decode("latin-1").split(","):
            directive, _, argument = part.strip().partition("=")
            if directive:
                directives.setdefault(directive.lower(), argument.strip())
    return directives


def _freshness_lifetime(raw_headers: list[tuple[bytes, bytes]]) -> int | None:
    """Shared-cache freshness lifetime: `s-maxage`, else `max-age` (RFC 9111 Section 4.2.1)."""
    directives = _cache_control(raw_headers)
    if "no-store" in directives or "private" in directives:
        return None
    for name in ("s-maxage", "max-age"):
        if name in directives:
            argument = directives[name]
            return int(argument) if argument.isdigit() else None
    return None


def _printable(raw: bytes) -> str:
    """Percent-escape bytes a Structured Field String cannot carry."""
    return "".join(chr(b) if 0x21 <= b <= 0x7E and b != 0x25 else f"%{b:02X}" for b in raw)


@dataclass(slots=True)
class _Captured:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    request_time: float
    response_time: float
    freshness_lifetime: int | None = None
    age_value: int = 0

    def current_age(self, now: float) -> float:
        # RFC 9111 Section 4.2.3, using corrected_age_value as corrected_initial_age.
        corrected_initial_age = self.age_value + (self.response_time - self.request_time)
        return corrected_initial_age + (now - self.response_time)


@dataclass(slots=True)
class SharedCacheLayer:
    """A shared cache as ASGI middleware that appends its own `Cache-Status` member.

    It stores whole `200` `GET` responses with `s-maxage` or `max-age`, collapses
    concurrent misses, and never revalidates; only `Cache-Status` is modeled faithfully.
    """

    app: ASGIApp
    name: str | Token
    clock: Callable[[], float]
    bypass_prefixes: tuple[str, ...] = ()
    debug_token: str | None = None
    max_target_bytes: int = 2048
    _store: dict[str, _Captured] = field(default_factory=dict[str, _Captured])
    _inflight: dict[str, asyncio.Future[_Captured | None]] = field(
        default_factory=dict[str, asyncio.Future[_Captured | None]]
    )
    collapsed_waiters: int = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
        query: bytes = scope["query_string"]
        if len(raw_path) + len(query) > self.max_target_bytes:
            # Generated locally, not from a stored response: no member (RFC 9211 Section 2).
            await _emit(send, _Captured(414, [(b"content-length", b"0")], b"", 0.0, 0.0), None)
            return
        key = _printable(raw_path + (b"?" + query if query else b""))
        request_headers: list[tuple[bytes, bytes]] = scope["headers"]
        expose_key = self._authorized(request_headers)
        method: str = scope["method"]
        if scope["path"].startswith(self.bypass_prefixes) or method in _SAFE_METHODS - {"GET"}:
            await self._forward_and_send(scope, receive, send, "bypass", key, expose_key)
            return
        if method != "GET":
            await self._forward_and_send(scope, receive, send, "method", key, expose_key)
            return
        stored = self._store.get(key)
        now = self.clock()
        fresh = stored is not None and _is_fresh(stored, now)
        if stored is not None and fresh and not _request_forbids_reuse(request_headers):
            ttl = _ttl(stored, now)
            entry = CacheStatusEntry(self.name, hit=True, ttl=ttl, key=key if expose_key else None)
            await _emit(send, stored, entry, age=math.floor(stored.current_age(now)))
            return
        reason = "uri-miss" if stored is None else ("request" if fresh else "stale")
        leader = self._inflight.get(key)
        if reason == "request" or leader is None:
            await self._forward_and_send(scope, receive, send, reason, key, expose_key)
            return
        self.collapsed_waiters += 1
        try:
            shared = await leader
        finally:
            self.collapsed_waiters -= 1
        if shared is None:
            await self._forward_and_send(
                scope, receive, send, reason, key, expose_key, collapsed=False
            )
            return
        entry = CacheStatusEntry(
            self.name,
            fwd=reason,
            ttl=_ttl(shared, self.clock()),
            collapsed=True,
            key=key if expose_key else None,
        )
        await _emit(send, shared, entry)

    def _authorized(self, request_headers: list[tuple[bytes, bytes]]) -> bool:
        if self.debug_token is None:
            return False
        expected = self.debug_token.encode("latin-1")
        presented = [value for name, value in request_headers if name.lower() == b"cache-debug"]
        return len(presented) == 1 and hmac.compare_digest(presented[0], expected)

    async def _forward_and_send(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        reason: str,
        key: str,
        expose_key: bool,
        *,
        collapsed: bool | None = None,
    ) -> None:
        cacheable = reason in ("uri-miss", "stale", "request")
        future: asyncio.Future[_Captured | None] | None = None
        if cacheable and collapsed is None and key not in self._inflight:
            future = asyncio.get_running_loop().create_future()
            self._inflight[key] = future
        shareable: _Captured | None = None
        try:
            captured = await self._forward(scope, receive)
            if cacheable and captured.status == 200:
                captured.freshness_lifetime = _freshness_lifetime(captured.headers)
            if captured.freshness_lifetime is not None:
                self._store[key] = captured
                shareable = captured
        finally:
            # Waiters must never hang: a failed or uncacheable forward releases them with None.
            if future is not None:
                del self._inflight[key]
                future.set_result(shareable)
        is_stored = shareable is not None
        now = self.clock()
        entry = CacheStatusEntry(
            self.name,
            fwd=reason,
            ttl=_ttl(captured, now) if is_stored else None,
            stored=is_stored,
            collapsed=collapsed,
            key=key if expose_key else None,
        )
        await _emit(send, captured, entry)

    async def _forward(self, scope: Scope, receive: Receive) -> _Captured:
        captured = _Captured(0, [], b"", request_time=self.clock(), response_time=0.0)
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured.status = message["status"]
                captured.headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        captured.body = b"".join(chunks)
        captured.response_time = self.clock()
        ages = [value for name, value in captured.headers if name.lower() == b"age"]
        captured.age_value = int(ages[0]) if ages and ages[0].isdigit() else 0
        return captured


def _is_fresh(stored: _Captured, now: float) -> bool:
    # RFC 9111 Section 4.2: response_is_fresh = (freshness_lifetime > current_age).
    lifetime = stored.freshness_lifetime
    return lifetime is not None and lifetime > stored.current_age(now)


def _ttl(stored: _Captured, now: float) -> int:
    lifetime = stored.freshness_lifetime if stored.freshness_lifetime is not None else 0
    return math.floor(lifetime - stored.current_age(now))


def _request_forbids_reuse(request_headers: list[tuple[bytes, bytes]]) -> bool:
    directives = _cache_control(request_headers)
    return "no-cache" in directives or directives.get("max-age") == "0"


async def _emit(
    send: Send, response: _Captured, entry: CacheStatusEntry | None, *, age: int | None = None
) -> None:
    """Send *response*, appending (never replacing) this cache's member (RFC 9211 Section 2)."""
    headers = [(n, v) for n, v in response.headers if age is None or n.lower() != b"age"]
    if age is not None:
        headers.append((b"age", str(age).encode("ascii")))
    if entry is not None:
        headers.append((b"cache-status", entry.serialize().encode("ascii")))
    await send({"type": "http.response.start", "status": response.status, "headers": headers})
    await send({"type": "http.response.body", "body": response.body})
