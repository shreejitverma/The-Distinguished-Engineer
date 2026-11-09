"""Strict RFC 9651 Structured Field Values parser and serializer, plus three field profiles."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

_DIGITS = frozenset("0123456789")
_ALPHA = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_LCALPHA = frozenset("abcdefghijklmnopqrstuvwxyz")
_TCHAR = _ALPHA | _DIGITS | frozenset("!#$%&'*+-.^_`|~")
_TOKEN_CHARS = _TCHAR | frozenset(":/")
_KEY_FIRST = _LCALPHA | frozenset("*")
_KEY_CHARS = _LCALPHA | _DIGITS | frozenset("_-.*")
_BASE64_CHARS = _ALPHA | _DIGITS | frozenset("+/=")
_LC_HEX = frozenset("0123456789abcdef")
_OWS = " \t"
_MAX_INTEGER = 999_999_999_999_999
_DECIMAL_LIMIT = Decimal(10) ** 12
_THOUSANDTH = Decimal("0.001")


class StructuredFieldError(ValueError):
    """Parsing or serialization failed; RFC 9651 Section 4.2 says ignore the whole field."""


@dataclass(frozen=True, slots=True)
class Token:
    """An sf-token, kept distinct from a String (RFC 9651 Appendix B)."""

    value: str


@dataclass(frozen=True, slots=True)
class Date:
    """An sf-date: signed seconds since 1970-01-01T00:00:00Z (RFC 9651 Section 3.3.7)."""

    seconds: int


@dataclass(frozen=True, slots=True)
class DisplayString:
    """An sf-displaystring: Unicode text, percent-encoded UTF-8 on the wire (Section 3.3.8)."""

    value: str


type BareItem = int | Decimal | str | Token | bytes | bool | Date | DisplayString
type Parameters = dict[str, BareItem]


@dataclass(frozen=True, slots=True)
class Item:
    """A bare item plus its ordered Parameters (RFC 9651 Section 3.3)."""

    value: BareItem
    params: Parameters = field(default_factory=dict[str, BareItem])


@dataclass(frozen=True, slots=True)
class InnerList:
    """A parenthesized array of Items plus its own Parameters (RFC 9651 Section 3.1.1)."""

    items: list[Item]
    params: Parameters = field(default_factory=dict[str, BareItem])


type Member = Item | InnerList
type SfList = list[Member]
type SfDictionary = dict[str, Member]
type FieldType = Literal["list", "dictionary", "item"]


class _Cursor:
    """Index-based view of the field value: O(n) parsing, no string re-slicing per step."""

    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        char = self.peek()
        if not char:
            raise StructuredFieldError("unexpected end of input")
        self.pos += 1
        return char

    def at_end(self) -> bool:
        return self.pos >= len(self.text)

    def skip(self, chars: str) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in chars:
            self.pos += 1


def combine_field_lines(lines: Iterable[bytes]) -> bytes:
    """Join same-named field lines with comma SP (RFC 9110 Section 5.3, RFC 9651 Section 4.2)."""
    return b", ".join(lines)


def _start(data: bytes | str) -> _Cursor:
    """Section 4.2 steps 1-2: require ASCII, then discard leading SP (not HTAB)."""
    if isinstance(data, bytes):
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise StructuredFieldError("field value is not ASCII") from exc
    elif data.isascii():
        text = data
    else:
        raise StructuredFieldError("field value is not ASCII")
    cur = _Cursor(text)
    cur.skip(" ")
    return cur


def _finish(cur: _Cursor) -> None:
    """Section 4.2 steps 6-7: discard trailing SP, then any leftover input is a failure."""
    cur.skip(" ")
    if not cur.at_end():
        raise StructuredFieldError(f"trailing characters at offset {cur.pos}")


def parse_list(data: bytes | str) -> SfList:
    """Parse a field value whose type is List (RFC 9651 Sections 4.2 and 4.2.1)."""
    cur = _start(data)
    result = _parse_list(cur)
    _finish(cur)
    return result


def parse_dictionary(data: bytes | str) -> SfDictionary:
    """Parse a field value whose type is Dictionary (RFC 9651 Sections 4.2 and 4.2.2)."""
    cur = _start(data)
    result = _parse_dictionary(cur)
    _finish(cur)
    return result


def parse_item(data: bytes | str) -> Item:
    """Parse a field value whose type is Item (RFC 9651 Sections 4.2 and 4.2.3)."""
    cur = _start(data)
    result = _parse_item(cur)
    _finish(cur)
    return result


def parse(data: bytes | str, field_type: FieldType) -> SfList | SfDictionary | Item:
    """Dispatch on the field definition's declared top-level type (Section 4.2 steps 3-5)."""
    if field_type == "list":
        return parse_list(data)
    if field_type == "dictionary":
        return parse_dictionary(data)
    return parse_item(data)


def _parse_list(cur: _Cursor) -> SfList:
    members: SfList = []
    while not cur.at_end():
        members.append(_parse_item_or_inner_list(cur))
        cur.skip(_OWS)
        if cur.at_end():
            return members
        if cur.take() != ",":
            raise StructuredFieldError(f"expected ',' at offset {cur.pos - 1}")
        cur.skip(_OWS)
        if cur.at_end():
            raise StructuredFieldError("trailing comma in list")
    return members


def _parse_item_or_inner_list(cur: _Cursor) -> Member:
    if cur.peek() == "(":
        return _parse_inner_list(cur)
    return _parse_item(cur)


def _parse_inner_list(cur: _Cursor) -> InnerList:
    if cur.take() != "(":
        raise StructuredFieldError("expected '('")
    items: list[Item] = []
    while not cur.at_end():
        cur.skip(" ")
        if cur.peek() == ")":
            cur.take()
            return InnerList(items, _parse_parameters(cur))
        items.append(_parse_item(cur))
        if cur.peek() not in (" ", ")"):
            raise StructuredFieldError(f"expected SP or ')' at offset {cur.pos}")
    raise StructuredFieldError("inner list not closed")


def _parse_dictionary(cur: _Cursor) -> SfDictionary:
    dictionary: SfDictionary = {}
    while not cur.at_end():
        key = _parse_key(cur)
        member: Member
        if cur.peek() == "=":
            cur.take()
            member = _parse_item_or_inner_list(cur)
        else:
            member = Item(True, _parse_parameters(cur))
        # Section 4.2.2 step 2.4: overwrite in place; a Python dict keeps the first position.
        dictionary[key] = member
        cur.skip(_OWS)
        if cur.at_end():
            return dictionary
        if cur.take() != ",":
            raise StructuredFieldError(f"expected ',' at offset {cur.pos - 1}")
        cur.skip(_OWS)
        if cur.at_end():
            raise StructuredFieldError("trailing comma in dictionary")
    return dictionary


def _parse_item(cur: _Cursor) -> Item:
    value = _parse_bare_item(cur)
    return Item(value, _parse_parameters(cur))


def _parse_bare_item(cur: _Cursor) -> BareItem:
    # peek() returns "" at end of input, and "" is never a member of these sets.
    char = cur.peek()
    if char == "-" or char in _DIGITS:
        return _parse_number(cur)
    if char == '"':
        return _parse_string(cur)
    if char == "*" or char in _ALPHA:
        return _parse_token(cur)
    if char == ":":
        return _parse_byte_sequence(cur)
    if char == "?":
        return _parse_boolean(cur)
    if char == "@":
        return _parse_date(cur)
    if char == "%":
        return _parse_display_string(cur)
    raise StructuredFieldError(f"unrecognized item type at offset {cur.pos}")


def _parse_parameters(cur: _Cursor) -> Parameters:
    params: Parameters = {}
    while cur.peek() == ";":
        cur.take()
        cur.skip(" ")
        key = _parse_key(cur)
        value: BareItem = True
        if cur.peek() == "=":
            cur.take()
            value = _parse_bare_item(cur)
        params[key] = value
    return params


def _parse_key(cur: _Cursor) -> str:
    if cur.peek() not in _KEY_FIRST:
        raise StructuredFieldError(f"key must start with lcalpha or '*' at offset {cur.pos}")
    start = cur.pos
    while cur.peek() in _KEY_CHARS:
        cur.pos += 1
    return cur.text[start : cur.pos]


def _parse_number(cur: _Cursor) -> int | Decimal:
    sign = 1
    if cur.peek() == "-":
        cur.take()
        sign = -1
    if cur.at_end():
        raise StructuredFieldError("empty integer")
    if cur.peek() not in _DIGITS:
        raise StructuredFieldError(f"expected DIGIT at offset {cur.pos}")
    is_decimal = False
    digits: list[str] = []
    while not cur.at_end():
        char = cur.peek()
        if char in _DIGITS:
            digits.append(char)
        elif not is_decimal and char == ".":
            if len(digits) > 12:
                raise StructuredFieldError("decimal integer component exceeds 12 digits")
            digits.append(char)
            is_decimal = True
        else:
            break
        cur.pos += 1
        if not is_decimal and len(digits) > 15:
            raise StructuredFieldError("integer exceeds 15 digits")
        if is_decimal and len(digits) > 16:
            raise StructuredFieldError("decimal exceeds 16 characters")
    number = "".join(digits)
    if not is_decimal:
        return sign * int(number)
    if number.endswith("."):
        raise StructuredFieldError("decimal ends with '.'")
    if len(number) - number.index(".") - 1 > 3:
        raise StructuredFieldError("decimal has more than 3 fractional digits")
    return sign * Decimal(number)


def _parse_string(cur: _Cursor) -> str:
    if cur.take() != '"':
        raise StructuredFieldError("expected DQUOTE")
    out: list[str] = []
    while not cur.at_end():
        char = cur.take()
        if char == "\\":
            if cur.at_end():
                raise StructuredFieldError("backslash at end of input")
            nxt = cur.take()
            if nxt not in ('"', "\\"):
                raise StructuredFieldError(f"invalid escape \\{nxt}")
            out.append(nxt)
        elif char == '"':
            return "".join(out)
        elif not " " <= char <= "~":
            raise StructuredFieldError("control character in string")
        else:
            out.append(char)
    raise StructuredFieldError("string not closed")


def _parse_token(cur: _Cursor) -> Token:
    if cur.peek() not in _ALPHA and cur.peek() != "*":
        raise StructuredFieldError("token must start with ALPHA or '*'")
    start = cur.pos
    while cur.peek() in _TOKEN_CHARS:
        cur.pos += 1
    return Token(cur.text[start : cur.pos])


def _parse_byte_sequence(cur: _Cursor) -> bytes:
    if cur.take() != ":":
        raise StructuredFieldError("expected ':'")
    end = cur.text.find(":", cur.pos)
    if end == -1:
        raise StructuredFieldError("byte sequence not closed")
    content = cur.text[cur.pos : end]
    cur.pos = end + 1
    if not set(content) <= _BASE64_CHARS:
        raise StructuredFieldError("character outside the base64 alphabet")
    # Section 4.2.7: SHOULD NOT fail on missing "=" padding or non-zero pad bits.
    unpadded = content.rstrip("=")
    padded = unpadded + "=" * (-len(unpadded) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except binascii.Error as exc:
        raise StructuredFieldError(f"invalid base64: {exc}") from exc


def _parse_boolean(cur: _Cursor) -> bool:
    if cur.take() != "?":
        raise StructuredFieldError("expected '?'")
    char = cur.peek()
    if char == "1":
        cur.take()
        return True
    if char == "0":
        cur.take()
        return False
    raise StructuredFieldError("boolean must be ?0 or ?1")


def _parse_date(cur: _Cursor) -> Date:
    if cur.take() != "@":
        raise StructuredFieldError("expected '@'")
    number = _parse_number(cur)
    if isinstance(number, Decimal):
        raise StructuredFieldError("date must be an integer")
    return Date(number)


def _parse_display_string(cur: _Cursor) -> DisplayString:
    if cur.text[cur.pos : cur.pos + 2] != '%"':
        raise StructuredFieldError("expected '%\"'")
    cur.pos += 2
    octets = bytearray()
    while not cur.at_end():
        char = cur.take()
        if not " " <= char <= "~":
            raise StructuredFieldError("control character in display string")
        if char == "%":
            hex_pair = cur.text[cur.pos : cur.pos + 2]
            if len(hex_pair) != 2 or not set(hex_pair) <= _LC_HEX:
                raise StructuredFieldError("percent-encoding must be two lowercase hex digits")
            cur.pos += 2
            octets.append(int(hex_pair, 16))
        elif char == '"':
            try:
                return DisplayString(octets.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise StructuredFieldError("display string is not valid UTF-8") from exc
        else:
            octets.append(ord(char))
    raise StructuredFieldError("display string not closed")


def serialize_list(members: Sequence[Member]) -> str | None:
    """Serialize a List (Section 4.1.1); None means omit the field (Section 4.1 step 1)."""
    if not members:
        return None
    return ", ".join(_serialize_member(member) for member in members)


def serialize_dictionary(members: Mapping[str, Member]) -> str | None:
    """Serialize a Dictionary (Section 4.1.2); None means omit the field."""
    if not members:
        return None
    parts: list[str] = []
    for key, member in members.items():
        if isinstance(member, Item) and member.value is True:
            parts.append(_serialize_key(key) + _serialize_parameters(member.params))
        else:
            parts.append(_serialize_key(key) + "=" + _serialize_member(member))
    return ", ".join(parts)


def serialize_item(item: Item) -> str:
    """Serialize an Item (Section 4.1.3)."""
    return _serialize_bare_item(item.value) + _serialize_parameters(item.params)


def _serialize_member(member: Member) -> str:
    if isinstance(member, InnerList):
        inner = " ".join(serialize_item(item) for item in member.items)
        return "(" + inner + ")" + _serialize_parameters(member.params)
    return serialize_item(member)


def _serialize_parameters(params: Mapping[str, BareItem]) -> str:
    out: list[str] = []
    for key, value in params.items():
        out.append(";" + _serialize_key(key))
        if value is not True:
            out.append("=" + _serialize_bare_item(value))
    return "".join(out)


def _serialize_key(key: str) -> str:
    if not key or key[0] not in _KEY_FIRST or not set(key) <= _KEY_CHARS:
        raise StructuredFieldError(f"invalid key {key!r}")
    return key


def _serialize_bare_item(value: BareItem) -> str:
    # bool is a subclass of int in Python, so it must be dispatched before int.
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, int):
        return _serialize_integer(value)
    if isinstance(value, Decimal):
        return _serialize_decimal(value)
    if isinstance(value, str):
        return _serialize_string(value)
    if isinstance(value, Token):
        return _serialize_token(value.value)
    if isinstance(value, bytes):
        return ":" + base64.b64encode(value).decode("ascii") + ":"
    if isinstance(value, Date):
        return "@" + _serialize_integer(value.seconds)
    return _serialize_display_string(value.value)


def _serialize_integer(value: int) -> str:
    if not -_MAX_INTEGER <= value <= _MAX_INTEGER:
        raise StructuredFieldError(f"integer out of range: {value}")
    return str(value)


def _serialize_decimal(value: Decimal) -> str:
    if not value.is_finite() or abs(value) >= _DECIMAL_LIMIT:
        raise StructuredFieldError(f"decimal out of range: {value}")
    rounded = value.quantize(_THOUSANDTH, rounding=ROUND_HALF_EVEN)
    if abs(rounded) >= _DECIMAL_LIMIT:
        raise StructuredFieldError(f"decimal exceeds 12 integer digits after rounding: {value}")
    integer_part, fraction = f"{abs(rounded):f}".split(".")
    sign = "-" if rounded < 0 else ""
    return f"{sign}{integer_part}.{fraction.rstrip('0') or '0'}"


def _serialize_string(value: str) -> str:
    if not all(" " <= char <= "~" for char in value):
        raise StructuredFieldError("string contains a non-printable or non-ASCII character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _serialize_token(value: str) -> str:
    if not value or (value[0] not in _ALPHA and value[0] != "*"):
        raise StructuredFieldError(f"invalid token {value!r}")
    if not set(value[1:]) <= _TOKEN_CHARS:
        raise StructuredFieldError(f"invalid token {value!r}")
    return value


def _serialize_display_string(value: str) -> str:
    try:
        octets = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StructuredFieldError("display string contains a surrogate") from exc
    out = ['%"']
    for octet in octets:
        if octet in (0x25, 0x22) or octet <= 0x1F or octet >= 0x7F:
            out.append(f"%{octet:02x}")
        else:
            out.append(chr(octet))
    out.append('"')
    return "".join(out)


@dataclass(frozen=True, slots=True)
class Priority:
    """RFC 9218 Section 4 priority parameters with their defaults."""

    urgency: int = 3
    incremental: bool = False


def priority_from_field_lines(lines: Sequence[bytes]) -> Priority:
    """Interpret Priority: a Dictionary; bad parse -> defaults; bad member -> that default."""
    if not lines:
        return Priority()
    try:
        members = parse_dictionary(combine_field_lines(lines))
    except StructuredFieldError:
        return Priority()
    urgency, incremental = 3, False
    u = members.get("u")
    # type(...) is int rejects bool: u=?1 is a Boolean, not urgency 1 (RFC 9218 Section 4).
    if isinstance(u, Item) and type(u.value) is int and 0 <= u.value <= 7:
        urgency = u.value
    i = members.get("i")
    if isinstance(i, Item) and isinstance(i.value, bool):
        incremental = i.value
    return Priority(urgency, incremental)


def deprecation_from_field(value: bytes) -> datetime | None:
    """Interpret Deprecation (RFC 9745 Section 2.1): an Item that MUST be a Date."""
    try:
        item = parse_item(value)
    except StructuredFieldError:
        return None
    if not isinstance(item.value, Date):
        return None
    try:
        return datetime.fromtimestamp(item.value.seconds, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True, slots=True)
class ServiceLimit:
    """One RateLimit Service Limit Item (draft-ietf-httpapi-ratelimit-headers-11 Section 4.1)."""

    policy: str
    remaining: int
    window: int | None
    partition_key: bytes | None


def _non_negative_int(value: BareItem | None) -> int | None:
    return value if type(value) is int and value >= 0 else None


def ratelimit_from_field_lines(lines: Sequence[bytes]) -> list[ServiceLimit] | None:
    """Interpret RateLimit; a malformed field MUST be ignored, returned here as None."""
    if not lines:
        return None
    try:
        members = parse_list(combine_field_lines(lines))
    except StructuredFieldError:
        return None
    limits: list[ServiceLimit] = []
    for member in members:
        if not isinstance(member, Item) or not isinstance(member.value, str):
            return None
        remaining = _non_negative_int(member.params.get("r"))
        if remaining is None:
            return None
        raw_window = member.params.get("t")
        window = _non_negative_int(raw_window)
        if raw_window is not None and window is None:
            return None
        pk = member.params.get("pk")
        if pk is not None and not isinstance(pk, bytes):
            return None
        limits.append(ServiceLimit(member.value, remaining, window, pk))
    return limits
