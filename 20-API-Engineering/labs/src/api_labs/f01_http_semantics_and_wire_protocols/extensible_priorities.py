"""Extensible Priorities (RFC 9218): Priority field parsing, PRIORITY_UPDATE, and scheduling."""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from decimal import Decimal

H2_FRAME_PRIORITY_UPDATE = 0x10
SETTINGS_NO_RFC7540_PRIORITIES = 0x9
DEFAULT_URGENCY = 3
DEFAULT_INCREMENTAL = False

type BareItem = int | Decimal | str | bytes | bool
type Params = dict[str, BareItem]
type Member = tuple[BareItem | list[tuple[BareItem, Params]], Params]

_LCALPHA = frozenset("abcdefghijklmnopqrstuvwxyz")
_DIGIT = frozenset("0123456789")
_ALPHA = _LCALPHA | frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_KEY_CHARS = _LCALPHA | _DIGIT | frozenset("_-.*")
_TCHAR = _ALPHA | _DIGIT | frozenset("!#$%&'*+-.^_`|~")
_B64 = _ALPHA | _DIGIT | frozenset("+/=")


class SFParseError(ValueError):
    """The input is not a valid Structured Field Dictionary (RFC 9651 Section 4.2)."""


class H2ProtocolError(ValueError):
    """An HTTP/2 connection error of type PROTOCOL_ERROR (RFC 9113 Section 5.4.1)."""


class H2FrameSizeError(ValueError):
    """An HTTP/2 connection error of type FRAME_SIZE_ERROR (RFC 9113 Section 4.2)."""


class _Cursor:
    """Mutable view over the field value, mirroring the RFC's "consume input_string"."""

    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        char = self.peek()
        if not char:
            raise SFParseError("unexpected end of input")
        self.pos += 1
        return char

    def skip(self, chars: str) -> None:
        while self.peek() and self.peek() in chars:
            self.pos += 1


def _parse_key(cur: _Cursor) -> str:
    if cur.peek() not in _LCALPHA and cur.peek() != "*":
        raise SFParseError("key must start with lcalpha or '*'")
    start = cur.pos
    while cur.peek() and cur.peek() in _KEY_CHARS:
        cur.pos += 1
    return cur.text[start : cur.pos]


def _parse_number(cur: _Cursor) -> int | Decimal:
    sign = 1
    if cur.peek() == "-":
        cur.take()
        sign = -1
    if cur.peek() not in _DIGIT:
        raise SFParseError("number must start with a DIGIT")
    digits = ""
    is_decimal = False
    while cur.peek() and (cur.peek() in _DIGIT or (cur.peek() == "." and not is_decimal)):
        char = cur.take()
        if char == ".":
            if len(digits) > 12:
                raise SFParseError("decimal integer part longer than 12 digits")
            is_decimal = True
        digits += char
        if not is_decimal and len(digits) > 15:
            raise SFParseError("integer longer than 15 digits")
        if is_decimal and len(digits) > 16:
            raise SFParseError("decimal longer than 16 characters")
    if not is_decimal:
        return sign * int(digits)
    if digits.endswith(".") or len(digits.split(".")[1]) > 3:
        raise SFParseError("decimal must have 1 to 3 fractional digits")
    return sign * Decimal(digits)


def _parse_string(cur: _Cursor) -> str:
    cur.take()
    out: list[str] = []
    while True:
        char = cur.take()
        if char == "\\":
            escaped = cur.take()
            if escaped not in ('"', "\\"):
                raise SFParseError("invalid escape in string")
            out.append(escaped)
        elif char == '"':
            return "".join(out)
        elif not 0x20 <= ord(char) <= 0x7E:
            raise SFParseError("non-VCHAR in string")
        else:
            out.append(char)


def _parse_token(cur: _Cursor) -> str:
    start = cur.pos
    cur.take()
    while cur.peek() and (cur.peek() in _TCHAR or cur.peek() in ":/"):
        cur.pos += 1
    return cur.text[start : cur.pos]


def _parse_bytes(cur: _Cursor) -> bytes:
    cur.take()
    end = cur.text.find(":", cur.pos)
    if end == -1:
        raise SFParseError("unterminated byte sequence")
    content = cur.text[cur.pos : end]
    cur.pos = end + 1
    if any(c not in _B64 for c in content):
        raise SFParseError("invalid base64 alphabet")
    try:
        return base64.b64decode(content + "=" * (-len(content) % 4))
    except binascii.Error as exc:
        raise SFParseError("invalid base64") from exc


def _parse_display_string(cur: _Cursor) -> str:
    cur.take()
    if cur.take() != '"':
        raise SFParseError('display string must start with %"')
    raw = bytearray()
    while True:
        char = cur.take()
        if not 0x20 <= ord(char) <= 0x7E:
            raise SFParseError("non-VCHAR in display string")
        if char == "%":
            octet = cur.take() + cur.take()
            if any(c not in "0123456789abcdef" for c in octet):
                raise SFParseError("display string escape must be lowercase hex")
            raw.append(int(octet, 16))
        elif char == '"':
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SFParseError("display string is not UTF-8") from exc
        else:
            raw.append(ord(char))


def _parse_bare_item(cur: _Cursor) -> BareItem:
    first = cur.peek()
    if first == "-" or first in _DIGIT:
        return _parse_number(cur)
    if first == '"':
        return _parse_string(cur)
    if first == "*" or first in _ALPHA:
        return _parse_token(cur)
    if first == ":":
        return _parse_bytes(cur)
    if first == "?":
        cur.take()
        flag = cur.take()
        if flag not in "01":
            raise SFParseError("boolean must be ?0 or ?1")
        return flag == "1"
    if first == "@":
        cur.take()
        date = _parse_number(cur)
        if isinstance(date, Decimal):
            raise SFParseError("date must be an integer")
        return date
    if first == "%":
        return _parse_display_string(cur)
    raise SFParseError(f"unrecognized bare item start {first!r}")


def _parse_params(cur: _Cursor) -> Params:
    params: Params = {}
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


def _parse_item_or_inner_list(cur: _Cursor) -> Member:
    if cur.peek() != "(":
        return _parse_bare_item(cur), _parse_params(cur)
    cur.take()
    inner: list[tuple[BareItem, Params]] = []
    while True:
        cur.skip(" ")
        if cur.peek() == ")":
            cur.take()
            return inner, _parse_params(cur)
        inner.append((_parse_bare_item(cur), _parse_params(cur)))
        if cur.peek() not in (" ", ")"):
            raise SFParseError("inner list members must be separated by SP")


def parse_sf_dictionary(field_value: str) -> dict[str, Member]:
    """Parse a Structured Field Dictionary (RFC 9651 Sections 4.2 and 4.2.2)."""
    if not field_value.isascii():
        raise SFParseError("field value is not ASCII")
    cur = _Cursor(field_value)
    cur.skip(" ")
    dictionary: dict[str, Member] = {}
    while cur.peek():
        key = _parse_key(cur)
        if cur.peek() == "=":
            cur.take()
            member = _parse_item_or_inner_list(cur)
        else:
            member = (True, _parse_params(cur))
        dictionary[key] = member  # a duplicate key overwrites: last one wins
        cur.skip(" \t")
        if not cur.peek():
            return dictionary
        if cur.take() != ",":
            raise SFParseError("dictionary members must be separated by ','")
        cur.skip(" \t")
        if not cur.peek():
            raise SFParseError("trailing comma")
    return dictionary


@dataclass(frozen=True, slots=True)
class Priority:
    """The two registered priority parameters (RFC 9218 Sections 4.1 and 4.2)."""

    urgency: int = DEFAULT_URGENCY
    incremental: bool = DEFAULT_INCREMENTAL


@dataclass(frozen=True, slots=True)
class PriorityParams:
    """Parameters actually present in a field value; ``None`` means omitted."""

    urgency: int | None = None
    incremental: bool | None = None


def parse_priority_params(field_value: str | None) -> PriorityParams:
    """Extract ``u`` and ``i``, ignoring unknown, out-of-range, or mistyped values.

    A field that fails Structured Field parsing is ignored as a whole (RFC 9651 Section 4.2).
    """
    if field_value is None:
        return PriorityParams()
    try:
        members = parse_sf_dictionary(field_value)
    except SFParseError:
        return PriorityParams()
    urgency: int | None = None
    incremental: bool | None = None
    u_value = members["u"][0] if "u" in members else None
    # bool is a subclass of int in Python: "u=?1" must not become urgency 1.
    if type(u_value) is int and 0 <= u_value <= 7:
        urgency = u_value
    i_value = members["i"][0] if "i" in members else None
    if type(i_value) is bool:
        incremental = i_value
    return PriorityParams(urgency=urgency, incremental=incremental)


def parse_priority(field_value: str | None) -> Priority:
    """Request-side view: an omitted parameter takes its default (RFC 9218 Section 4)."""
    params = parse_priority_params(field_value)
    return Priority(
        urgency=DEFAULT_URGENCY if params.urgency is None else params.urgency,
        incremental=DEFAULT_INCREMENTAL if params.incremental is None else params.incremental,
    )


def serialize_priority(priority: Priority) -> str:
    """Serialize, omitting default-valued parameters to save bytes on every request."""
    members: list[str] = []
    if priority.urgency != DEFAULT_URGENCY:
        members.append(f"u={priority.urgency}")
    if priority.incremental:
        members.append("i")
    return ", ".join(members)


def merge_response_priority(client: Priority, response_field: str | None) -> Priority:
    """Intermediary merge: an omitted response parameter keeps the client value (Section 8)."""
    server = parse_priority_params(response_field)
    return Priority(
        urgency=client.urgency if server.urgency is None else server.urgency,
        incremental=client.incremental if server.incremental is None else server.incremental,
    )


def encode_h2_priority_update(prioritized_stream_id: int, field_value: str) -> bytes:
    """Build an HTTP/2 PRIORITY_UPDATE frame (RFC 9218 Section 7.1, Figure 1)."""
    if not 0 < prioritized_stream_id < 2**31:
        raise ValueError("prioritized stream ID must be in 1..2^31-1")
    payload = struct.pack("!L", prioritized_stream_id) + field_value.encode("ascii")
    header = struct.pack("!L", len(payload))[1:] + bytes([H2_FRAME_PRIORITY_UPDATE, 0])
    return header + struct.pack("!L", 0) + payload


def decode_h2_priority_update(frame: bytes) -> tuple[int, str]:
    """Validate and decode an HTTP/2 PRIORITY_UPDATE frame received by a server."""
    length = int.from_bytes(frame[0:3], "big")
    if len(frame) < 9 or frame[3] != H2_FRAME_PRIORITY_UPDATE or length != len(frame) - 9:
        raise H2ProtocolError("not a well-formed PRIORITY_UPDATE frame")
    if length < 4:
        raise H2FrameSizeError("PRIORITY_UPDATE payload shorter than the Prioritized Stream ID")
    (stream_id,) = struct.unpack("!L", frame[5:9])
    if stream_id & 0x7FFFFFFF != 0:
        raise H2ProtocolError("PRIORITY_UPDATE must be sent on stream 0")
    (prioritized,) = struct.unpack("!L", frame[9:13])
    prioritized &= 0x7FFFFFFF
    if prioritized == 0:
        raise H2ProtocolError("prioritized stream ID 0 is a connection error")
    try:
        value = frame[13:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise H2ProtocolError("Priority Field Value is not ASCII") from exc
    return prioritized, value


@dataclass(slots=True)
class _Stream:
    priority: Priority
    remaining: int


class PriorityScheduler:
    """Server-side response scheduler following RFC 9218 Section 10.

    Lower urgency first; within an urgency, non-incremental responses one at a time in
    ascending stream ID, then incremental responses round-robin one chunk per turn.
    """

    def __init__(self, max_concurrent_streams: int = 100) -> None:
        self.max_concurrent_streams = max_concurrent_streams
        self._active: dict[int, _Stream] = {}
        self._pending_updates: dict[int, Priority] = {}
        self._closed: set[int] = set()
        self._last_incremental: dict[int, int] = {}

    def open(self, stream_id: int, priority_header: str | None, chunks: int) -> Priority:
        """Open a request stream; a buffered PRIORITY_UPDATE overrides the header."""
        priority = self._pending_updates.pop(stream_id, None) or parse_priority(priority_header)
        self._active[stream_id] = _Stream(priority=priority, remaining=chunks)
        return priority

    def priority_update(self, prioritized_stream_id: int, field_value: str) -> None:
        """Apply a PRIORITY_UPDATE; buffer it for an idle stream (RFC 9218 Sections 7, 7.1)."""
        try:
            parse_sf_dictionary(field_value)
        except SFParseError as exc:
            raise H2ProtocolError("unparseable Priority Field Value") from exc
        priority = parse_priority(field_value)
        if prioritized_stream_id in self._closed:
            return
        stream = self._active.get(prioritized_stream_id)
        if stream is not None:
            stream.priority = priority
            return
        if prioritized_stream_id not in self._pending_updates and (
            len(self._pending_updates) + len(self._active) >= self.max_concurrent_streams
        ):
            raise H2ProtocolError("idle prioritized plus active streams exceed the limit")
        self._pending_updates[prioritized_stream_id] = priority

    def next_chunk(self) -> int | None:
        """Return the stream ID that sends the next chunk, or ``None`` when idle."""
        if not self._active:
            return None
        urgency = min(s.priority.urgency for s in self._active.values())
        level = sorted(sid for sid, s in self._active.items() if s.priority.urgency == urgency)
        sequential = [sid for sid in level if not self._active[sid].priority.incremental]
        if sequential:
            chosen = sequential[0]
        else:
            last = self._last_incremental.get(urgency, -1)
            chosen = next((sid for sid in level if sid > last), level[0])
            self._last_incremental[urgency] = chosen
        stream = self._active[chosen]
        stream.remaining -= 1
        if stream.remaining <= 0:
            del self._active[chosen]
            self._closed.add(chosen)
        return chosen

    def drain(self) -> list[int]:
        """Run the scheduler until every open stream has sent all its chunks."""
        order: list[int] = []
        while (sid := self.next_chunk()) is not None:
            order.append(sid)
        return order
