"""Idempotency-Key enforcement per draft-ietf-httpapi-idempotency-key-header-08.

Three layers, each usable on its own:

* ``parse_idempotency_key``: RFC 9651 Item parser that accepts only a String bare item,
  as draft-08 Section 2.1 requires; any failure means the field is ignored (RFC 9651 Section 4.2).
* ``IdempotencyStore``: the per-key state machine (absent -> in_flight -> completed, with lease
  expiry, TTL expiry, and fencing tokens so a stale owner cannot overwrite a newer one).
* ``IdempotencyMiddleware``: a pure ASGI middleware that maps the store's decisions onto the
  draft-08 Section 2.7 responses (400, 422, 409) and replays completed responses byte for byte.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

type BareItem = int | float | str | bytes | bool

_LCALPHA = frozenset("abcdefghijklmnopqrstuvwxyz")
_DIGIT = frozenset("0123456789")
_ALPHA = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ") | _LCALPHA
_TCHAR = _ALPHA | _DIGIT | frozenset("!#$%&'*+-.^_`|~")
_KEY_CHARS = _LCALPHA | _DIGIT | frozenset("_-.*")
_B64_CHARS = _ALPHA | _DIGIT | frozenset("+/=")
_LCHEX = frozenset("0123456789abcdef")


class SfParseError(ValueError):
    """The bytes are not a valid RFC 9651 Item, or its bare item is not a String."""


@dataclass(frozen=True, slots=True)
class SfStringItem:
    """A parsed ``Idempotency-Key`` value: the String plus any (ignorable) Parameters."""

    value: str
    params: dict[str, BareItem]


class _Cursor:
    """Consumes an ASCII string left to right, as the RFC 9651 Section 4.2 algorithms do."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def take(self) -> str:
        char = self.peek()
        if not char:
            raise SfParseError("unexpected end of input")
        self.pos += 1
        return char

    def skip_sp(self) -> None:
        while self.peek() == " ":
            self.pos += 1

    def at_end(self) -> bool:
        return self.pos >= len(self.text)


def _parse_string(cur: _Cursor) -> str:
    # RFC 9651 Section 4.2.5.
    if cur.take() != '"':
        raise SfParseError("String must start with DQUOTE")
    out: list[str] = []
    while not cur.at_end():
        char = cur.take()
        if char == "\\":
            nxt = cur.take()
            if nxt not in ('"', "\\"):
                raise SfParseError('only \\" and \\\\ escapes are allowed')
            out.append(nxt)
        elif char == '"':
            return "".join(out)
        elif not 0x20 <= ord(char) <= 0x7E:
            raise SfParseError("String characters must be in %x20-7E")
        else:
            out.append(char)
    raise SfParseError("unterminated String")


def _parse_number(cur: _Cursor) -> int | float:
    # RFC 9651 Section 4.2.4.
    sign = 1
    if cur.peek() == "-":
        cur.take()
        sign = -1
    if cur.peek() not in _DIGIT:  # frozenset membership of "" is False, so EOF fails too
        raise SfParseError("number must start with a DIGIT")
    digits = ""
    is_decimal = False
    while (char := cur.peek()) and (char in _DIGIT or (char == "." and not is_decimal)):
        if char == ".":
            if len(digits) > 12:
                raise SfParseError("Decimal integer part longer than 12 digits")
            is_decimal = True
        digits += cur.take()
        if not is_decimal and len(digits) > 15:
            raise SfParseError("Integer longer than 15 digits")
        if is_decimal and len(digits) > 16:
            raise SfParseError("Decimal longer than 16 characters")
    if not is_decimal:
        return sign * int(digits)
    fraction = digits.split(".", 1)[1]
    if not fraction or len(fraction) > 3:
        raise SfParseError("Decimal needs 1 to 3 fractional digits")
    return sign * float(digits)


def _parse_token(cur: _Cursor) -> str:
    # RFC 9651 Section 4.2.6.
    if cur.peek() not in _ALPHA and cur.peek() != "*":
        raise SfParseError("Token must start with ALPHA or *")
    out = cur.take()
    while (char := cur.peek()) and (char in _TCHAR or char in ":/"):
        out += cur.take()
    return out


def _parse_bytes(cur: _Cursor) -> bytes:
    # RFC 9651 Section 4.2.7 (validation only; padding leniency per the SHOULD NOT fail).
    cur.take()
    out = ""
    while (char := cur.take()) != ":":
        if char not in _B64_CHARS:
            raise SfParseError("invalid base64 character in Byte Sequence")
        out += char
    return out.encode("ascii")


def _parse_display_string(cur: _Cursor) -> str:
    # RFC 9651 Section 4.2.10.
    cur.take()
    if cur.take() != '"':
        raise SfParseError('Display String must start with %"')
    raw = bytearray()
    while True:
        char = cur.take()
        if char == "%":
            hex_pair = cur.take() + cur.take()
            if not set(hex_pair) <= _LCHEX:
                raise SfParseError("Display String escapes use lowercase hex")
            raw.append(int(hex_pair, 16))
        elif char == '"':
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SfParseError("Display String is not valid UTF-8") from exc
        elif not 0x20 <= ord(char) <= 0x7E:
            raise SfParseError("Display String characters must be in %x20-7E")
        else:
            raw.append(ord(char))


def _parse_bare_item(cur: _Cursor) -> BareItem:
    # RFC 9651 Section 4.2.3.1: the first character alone selects the type.
    char = cur.peek()
    if char == "-" or char in _DIGIT:
        return _parse_number(cur)
    if char == '"':
        return _parse_string(cur)
    if char == "*" or char in _ALPHA:
        return _parse_token(cur)
    if char == ":":
        return _parse_bytes(cur)
    if char == "?":
        cur.take()
        flag = cur.take()
        if flag not in "01":
            raise SfParseError("Boolean must be ?0 or ?1")
        return flag == "1"
    if char == "@":
        cur.take()
        value = _parse_number(cur)
        if isinstance(value, float):
            raise SfParseError("Date must be an Integer")
        return value
    if char == "%":
        return _parse_display_string(cur)
    raise SfParseError("unrecognized bare item")


def _parse_params(cur: _Cursor) -> dict[str, BareItem]:
    # RFC 9651 Section 4.2.3.2: a duplicate key overwrites, so the last value wins.
    params: dict[str, BareItem] = {}
    while cur.peek() == ";":
        cur.take()
        cur.skip_sp()
        if cur.peek() not in _LCALPHA and cur.peek() != "*":
            raise SfParseError("parameter key must start with lcalpha or *")
        key = cur.take()
        while (char := cur.peek()) and char in _KEY_CHARS:
            key += cur.take()
        value: BareItem = True
        if cur.peek() == "=":
            cur.take()
            value = _parse_bare_item(cur)
        params[key] = value
    return params


def parse_idempotency_key(field_lines: list[bytes]) -> SfStringItem:
    """Parse every ``Idempotency-Key`` field line as one RFC 9651 Item that must be a String.

    Field lines are combined with ``", "`` first (RFC 9651 Section 4.2), so a duplicated header
    leaves input after the Item and fails, exactly as a conforming generic parser would.
    """
    try:
        text = b", ".join(field_lines).decode("ascii")
    except UnicodeDecodeError as exc:
        raise SfParseError("field value is not ASCII") from exc
    cur = _Cursor(text)
    cur.skip_sp()
    if cur.peek() != '"':
        raise SfParseError("Idempotency-Key must be a String (draft-08 Section 2.1)")
    value = _parse_string(cur)
    params = _parse_params(cur)
    cur.skip_sp()
    if not cur.at_end():
        raise SfParseError("input left over after the Item")
    return SfStringItem(value=value, params=params)


# --- The per-key state machine -----------------------------------------------------------


class State(enum.Enum):
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


@dataclass(slots=True)
class Record:
    fingerprint: str
    state: State
    token: int
    lease_expires_at: float
    expires_at: float
    response: StoredResponse | None = None


class Outcome(enum.Enum):
    PROCEED = "proceed"  # first request, or a takeover after lease or TTL expiry
    REPLAY = "replay"  # completed with the same fingerprint
    IN_PROGRESS = "in_progress"  # draft-08 Section 2.7: 409 Conflict
    MISMATCH = "mismatch"  # draft-08 Section 2.7: 422 Unprocessable Content


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    token: int = 0
    response: StoredResponse | None = None


@dataclass(slots=True)
class IdempotencyStore:
    """In-memory store; every transition happens under one lock, so it is atomic."""

    ttl_seconds: float = 86_400.0
    lease_seconds: float = 90.0
    clock: Callable[[], float] = time.monotonic
    _records: dict[tuple[str, str], Record] = field(default_factory=dict[tuple[str, str], Record])
    _next_token: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self, scope_key: tuple[str, str], fingerprint: str) -> Decision:
        now = self.clock()
        with self._lock:
            record = self._records.get(scope_key)
            if record is not None and now >= record.expires_at:
                record = None  # TTL elapsed: the key is forgotten, a reuse is a new request
            if record is not None:
                if record.fingerprint != fingerprint:
                    return Decision(Outcome.MISMATCH)
                if record.state is State.COMPLETED:
                    return Decision(Outcome.REPLAY, response=record.response)
                if now < record.lease_expires_at:
                    return Decision(Outcome.IN_PROGRESS)
                # Lease expired: the owner is presumed dead, so this request takes over.
            self._next_token += 1
            self._records[scope_key] = Record(
                fingerprint=fingerprint,
                state=State.IN_FLIGHT,
                token=self._next_token,
                lease_expires_at=now + self.lease_seconds,
                expires_at=now + self.ttl_seconds,
            )
            return Decision(Outcome.PROCEED, token=self._next_token)

    def complete(self, scope_key: tuple[str, str], token: int, response: StoredResponse) -> bool:
        """Store the response unless a newer owner holds the key (fencing token check)."""
        now = self.clock()
        with self._lock:
            record = self._records.get(scope_key)
            if record is None or record.token != token or record.state is not State.IN_FLIGHT:
                return False
            record.state = State.COMPLETED
            record.response = response
            record.expires_at = now + self.ttl_seconds
            return True

    def release(self, scope_key: tuple[str, str], token: int) -> bool:
        """Forget an in-flight key so a retry re-executes; a stale token is a no-op."""
        with self._lock:
            record = self._records.get(scope_key)
            if record is None or record.token != token or record.state is not State.IN_FLIGHT:
                return False
            del self._records[scope_key]
            return True

    def purge_expired(self) -> int:
        """The reaper: drop records past their TTL and return how many were dropped."""
        now = self.clock()
        with self._lock:
            dead = [key for key, rec in self._records.items() if now >= rec.expires_at]
            for key in dead:
                del self._records[key]
            return len(dead)

    def state_of(self, scope_key: tuple[str, str]) -> State | None:
        with self._lock:
            record = self._records.get(scope_key)
            return None if record is None else record.state


# --- The ASGI middleware -----------------------------------------------------------------

KEY_FORMAT = re.compile(r"[A-Za-z0-9_-]{16,255}")
REPLAYED_HEADER = (b"idempotent-replayed", b"true")


def anonymous_principal(scope: Scope) -> str:
    return "anonymous"


@dataclass(frozen=True, slots=True)
class IdempotencyConfig:
    methods: frozenset[str] = frozenset({"POST", "PATCH"})
    doc_url: str = "https://developer.example.com/idempotency"
    max_body_bytes: int = 1_048_576
    cache_server_errors: bool = False  # True is Stripe API v1 behavior (replay cached 500s)
    principal_of: Callable[[Scope], str] = anonymous_principal


def request_fingerprint(method: str, path: str, query: bytes, body: bytes) -> str:
    """Draft-08 Section 2.4 'checksum of the entire request payload', bound to the target."""
    digest = hashlib.sha256()
    for part in (method.encode("ascii"), path.encode("utf-8"), query, body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp, store: IdempotencyStore, config: IdempotencyConfig) -> None:
        self.app = app
        self.store = store
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in self.config.methods:
            await self.app(scope, receive, send)
            return
        # ASGI: request header names "should be lowercased, but it is not required".
        lines = [bytes(v) for k, v in scope["headers"] if bytes(k).lower() == b"idempotency-key"]
        try:
            key = parse_idempotency_key(lines).value if lines else None
        except SfParseError:
            key = None  # RFC 9651 Section 4.2: ignore the field, as if it were absent
        if key is None:
            detail = "This operation is idempotent and requires correct usage of Idempotency Key."
            if lines:
                detail = "Idempotency-Key is not a Structured Field String, so it was ignored."
            await self._problem(send, 400, "Idempotency-Key is missing", detail)
            return
        if KEY_FORMAT.fullmatch(key) is None:
            detail = "Idempotency-Key must be 16 to 255 characters of A-Z a-z 0-9 _ -."
            await self._problem(send, 400, "Idempotency-Key is invalid", detail)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.config.max_body_bytes:
                await self._problem(send, 413, "Content Too Large", "Request body over limit.")
                return
            if not message.get("more_body", False):
                break

        scope_key = (self.config.principal_of(scope), key)
        fingerprint = request_fingerprint(
            scope["method"], scope["path"], scope["query_string"], bytes(body)
        )
        decision = self.store.begin(scope_key, fingerprint)
        if decision.outcome is Outcome.MISMATCH:
            detail = (
                "This operation is idempotent and it requires correct usage of Idempotency Key."
                " Idempotency Key must not be reused across different payloads of this operation."
            )
            await self._problem(send, 422, "Idempotency-Key is already used", detail)
            return
        if decision.outcome is Outcome.IN_PROGRESS:
            detail = (
                "A request with the same Idempotency-Key for the same operation"
                " is being processed or is outstanding."
            )
            await self._problem(
                send, 409, "A request is outstanding for this Idempotency-Key", detail
            )
            return
        if decision.outcome is Outcome.REPLAY:
            if decision.response is None:  # pragma: no cover - begin() always sets it
                raise RuntimeError("REPLAY decision without a stored response")
            await self._replay(send, decision.response)
            return

        await self._execute(scope, send, bytes(body), scope_key, decision.token, receive)

    async def _execute(
        self,
        scope: Scope,
        send: Send,
        body: bytes,
        scope_key: tuple[str, str],
        token: int,
        receive: Receive,
    ) -> None:
        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status = 0
        headers: list[tuple[bytes, bytes]] = []
        chunks: list[bytes] = []
        finished = False

        async def capture_send(message: Message) -> None:
            nonlocal status, finished
            if message["type"] == "http.response.start":
                status = message["status"]
                headers.extend((bytes(k), bytes(v)) for k, v in message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(bytes(message.get("body", b"")))
                finished = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        except BaseException:
            self.store.release(scope_key, token)  # crash before commit: let a retry re-execute
            raise
        if not finished or (status >= 500 and not self.config.cache_server_errors):
            self.store.release(scope_key, token)
            return
        stored = StoredResponse(status, tuple(headers), b"".join(chunks))
        self.store.complete(scope_key, token, stored)

    async def _replay(self, send: Send, response: StoredResponse) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": [*response.headers, REPLAYED_HEADER],
            }
        )
        await send({"type": "http.response.body", "body": response.body, "more_body": False})

    async def _problem(self, send: Send, status: int, title: str, detail: str) -> None:
        # Draft-08 Section 2.7: RFC 9457 problem details plus a Link to the documentation.
        payload = {"type": self.config.doc_url, "title": title, "status": status, "detail": detail}
        body = json.dumps(payload).encode("utf-8")
        link = f'<{self.config.doc_url}>; rel="describedby"; type="text/html"'
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-language", b"en"),
                    (b"link", link.encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
