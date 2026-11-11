"""Opaque page tokens: strict base64url, HMAC or AEAD protection, and query binding (AIP-158).

Wire layouts before base64url encoding (RFC 4648 Section 5, padding omitted):

    HMAC-SHA-256-128, 42 bytes -> 56 characters:
        version=1:u8 | key_id:u8 | issued_at:u64 | create_time:i64 | row_id:i64 | tag[16]
    AES-256-GCM-SIV, 54 bytes -> 72 characters:
        version=2:u8 | key_id:u8 | nonce[12] | ciphertext[24] | tag[16]

The purpose label and the SHA-256 digest of the bound query parameters are
authenticated but never transmitted, so a token presented with different
parameters fails exactly like a forged one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from starlette.requests import Request

MAX_TOKEN_CHARS: Final = 512
DEFAULT_TTL_SECONDS: Final = 3 * 24 * 60 * 60  # AIP-158 rule of thumb: three days
HMAC_TAG_BYTES: Final = 16  # HMAC-SHA-256-128, RFC 4868 Section 2.3
AEAD_NONCE_BYTES: Final = 12
DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100

_HEADER: Final = struct.Struct(">BB")  # version, key_id
_POSITION: Final = struct.Struct(">Qqq")  # issued_at, create_time, row_id
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]*")
_FILTER: Final = re.compile(r'\s*status\s*=\s*"(open|closed)"\s*')
_PAGE_SIZE: Final = re.compile(r"-?[0-9]{1,6}")
_ORDER_BY: Final = {"create_time desc": True, "create_time asc": False}

Reason = Literal[
    "too_long",
    "not_base64url",
    "non_canonical",
    "bad_length",
    "unsupported_version",
    "unknown_key",
    "bad_tag",
    "expired",
]


class InvalidPageToken(ValueError):
    """The token is malformed, forged, expired, or was minted for other parameters."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


def b64url_encode(raw: bytes) -> str:
    """Encode with the URL-safe alphabet and no padding (RFC 4648 Sections 3.2 and 5)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode_strict(text: str) -> bytes:
    """Decode unpadded base64url, rejecting every spelling but the canonical one."""
    if len(text) > MAX_TOKEN_CHARS:
        raise InvalidPageToken("too_long")
    if _B64URL.fullmatch(text) is None or len(text) % 4 == 1:
        raise InvalidPageToken("not_base64url")
    raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    if b64url_encode(raw) != text:  # non-zero pad bits, RFC 4648 Section 3.5
        raise InvalidPageToken("non_canonical")
    return raw


@dataclass(frozen=True, slots=True)
class QueryContext:
    """Request parameters that must match on every page (AIP-158); `page_size` may change."""

    principal: str
    parent: str
    filter: str
    order_by: str

    def digest(self) -> bytes:
        fields = [self.principal, self.parent, self.filter, self.order_by]
        canonical = json.dumps(fields, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("ascii")).digest()


@dataclass(frozen=True, slots=True)
class Position:
    """Keyset position of the last row served: the full sort key `(create_time, row_id)`."""

    create_time: int
    row_id: int


@dataclass(frozen=True, slots=True)
class Keyring:
    """Signing keys by one-byte id; `current` mints, every listed key still verifies."""

    keys: Mapping[int, bytes]
    current: int

    def __post_init__(self) -> None:
        # Snapshot the keys so codec checks made at construction cannot be bypassed later.
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))
        if self.current not in self.keys or any(not 0 <= kid <= 255 for kid in self.keys):
            raise ValueError("current key id must be listed and every key id must fit in a byte")


class PageTokenCodec(Protocol):
    def mint(self, position: Position, context: QueryContext) -> str: ...

    def open(self, token: str, context: QueryContext) -> Position: ...


def _wall_clock() -> int:
    return int(time.time())


def _bound_prefix(purpose: str, context: QueryContext, header: bytes) -> bytes:
    label = purpose.encode("utf-8")
    return struct.pack(">H", len(label)) + label + context.digest() + header


def _check_age(issued_at: int, now: int, ttl_seconds: int) -> None:
    if now - issued_at > ttl_seconds:
        raise InvalidPageToken("expired")


def _split_header(raw: bytes, version: int, length: int, keyring: Keyring) -> tuple[bytes, bytes]:
    if len(raw) < _HEADER.size:
        raise InvalidPageToken("bad_length")
    got_version, kid = _HEADER.unpack_from(raw)
    if got_version != version:
        raise InvalidPageToken("unsupported_version")
    if len(raw) != length:
        raise InvalidPageToken("bad_length")
    key = keyring.keys.get(kid)
    if key is None:
        raise InvalidPageToken("unknown_key")
    return raw[: _HEADER.size], key


@dataclass(frozen=True, slots=True)
class HmacPageTokens:
    """Integrity only: anyone can decode the position, nobody can forge or rebind it."""

    keyring: Keyring
    purpose: str
    clock: Callable[[], int] = _wall_clock
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    VERSION: ClassVar[int] = 1
    LENGTH: ClassVar[int] = _HEADER.size + _POSITION.size + HMAC_TAG_BYTES

    def __post_init__(self) -> None:
        if any(len(key) < hashlib.sha256().digest_size for key in self.keyring.keys.values()):
            raise ValueError("HMAC-SHA-256 keys shorter than 32 bytes weaken the MAC (RFC 2104)")

    def _tag(self, key: bytes, context: QueryContext, header: bytes, body: bytes) -> bytes:
        prefix = _bound_prefix(self.purpose, context, header)
        return hmac.digest(key, prefix + body, "sha256")[:HMAC_TAG_BYTES]

    def mint(self, position: Position, context: QueryContext) -> str:
        kid = self.keyring.current
        header = _HEADER.pack(self.VERSION, kid)
        body = _POSITION.pack(self.clock(), position.create_time, position.row_id)
        tag = self._tag(self.keyring.keys[kid], context, header, body)
        return b64url_encode(header + body + tag)

    def open(self, token: str, context: QueryContext) -> Position:
        raw = b64url_decode_strict(token)
        header, key = _split_header(raw, self.VERSION, self.LENGTH, self.keyring)
        body, tag = raw[_HEADER.size : -HMAC_TAG_BYTES], raw[-HMAC_TAG_BYTES:]
        if not hmac.compare_digest(tag, self._tag(key, context, header, body)):
            raise InvalidPageToken("bad_tag")
        issued_at, create_time, row_id = _POSITION.unpack(body)
        _check_age(issued_at, self.clock(), self.ttl_seconds)
        return Position(create_time, row_id)


@dataclass(frozen=True, slots=True)
class AeadPageTokens:
    """Confidentiality and integrity: AES-256-GCM-SIV (RFC 8452) with the binding as AAD."""

    keyring: Keyring
    purpose: str
    clock: Callable[[], int] = _wall_clock
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    nonce_source: Callable[[int], bytes] = os.urandom
    VERSION: ClassVar[int] = 2
    LENGTH: ClassVar[int] = _HEADER.size + AEAD_NONCE_BYTES + _POSITION.size + 16

    def __post_init__(self) -> None:
        if any(len(key) != 32 for key in self.keyring.keys.values()):
            raise ValueError("AES-256-GCM-SIV keys are exactly 32 bytes")

    def mint(self, position: Position, context: QueryContext) -> str:
        kid = self.keyring.current
        header = _HEADER.pack(self.VERSION, kid)
        nonce = self.nonce_source(AEAD_NONCE_BYTES)
        plaintext = _POSITION.pack(self.clock(), position.create_time, position.row_id)
        aad = _bound_prefix(self.purpose, context, header)
        sealed = AESGCMSIV(self.keyring.keys[kid]).encrypt(nonce, plaintext, aad)
        return b64url_encode(header + nonce + sealed)

    def open(self, token: str, context: QueryContext) -> Position:
        raw = b64url_decode_strict(token)
        header, key = _split_header(raw, self.VERSION, self.LENGTH, self.keyring)
        nonce = raw[_HEADER.size : _HEADER.size + AEAD_NONCE_BYTES]
        sealed = raw[_HEADER.size + AEAD_NONCE_BYTES :]
        aad = _bound_prefix(self.purpose, context, header)
        try:
            plaintext = AESGCMSIV(key).decrypt(nonce, sealed, aad)
        except InvalidTag:
            raise InvalidPageToken("bad_tag") from None
        issued_at, create_time, row_id = _POSITION.unpack(plaintext)
        _check_age(issued_at, self.clock(), self.ttl_seconds)
        return Position(create_time, row_id)


@dataclass(frozen=True, slots=True)
class Order:
    row_id: int
    store: str
    owner: str
    status: str
    create_time: int


def _sort_key(row: Order) -> tuple[int, int]:
    return (row.create_time, row.row_id)


def parse_filter(text: str) -> tuple[str | None, str]:
    """Return the status to match and the canonical filter text that the token binds."""
    if text.strip() == "":
        return None, ""
    match = _FILTER.fullmatch(text)
    if match is None:
        raise ValueError('filter must be empty or status = "open" or status = "closed"')
    return match.group(1), f'status = "{match.group(1)}"'


def parse_page_size(text: str) -> int:
    """AIP-158: 0 means the default, above the maximum is coerced, negative is an error."""
    if _PAGE_SIZE.fullmatch(text) is None:
        raise ValueError("page_size must be an integer")
    size = int(text)
    if size < 0:
        raise ValueError("page_size must not be negative")
    return DEFAULT_PAGE_SIZE if size == 0 else min(size, MAX_PAGE_SIZE)


def select_page(
    rows: Sequence[Order],
    *,
    principal: str,
    store: str,
    status: str | None,
    descending: bool,
    after: Position | None,
    page_size: int,
) -> tuple[list[Order], Position | None]:
    """Keyset page over `(create_time, row_id)`; authorization is applied on every page."""
    visible = [
        row
        for row in rows
        if row.owner == principal
        and row.store == store
        and (status is None or row.status == status)
    ]
    visible.sort(key=_sort_key, reverse=descending)
    if after is not None:
        cursor = (after.create_time, after.row_id)
        if descending:
            visible = [row for row in visible if _sort_key(row) < cursor]
        else:
            visible = [row for row in visible if _sort_key(row) > cursor]
    page = visible[:page_size]
    has_more = len(visible) > page_size
    return page, Position(page[-1].create_time, page[-1].row_id) if has_more else None


def _invalid_argument(message: str) -> JSONResponse:
    error = {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}
    return JSONResponse({"error": error}, status_code=400)


def build_app(rows: Sequence[Order], codec: PageTokenCodec) -> Starlette:
    """`GET /v1/stores/{store}/orders` with AIP-158 `page_size`, `page_token`, `next_page_token`."""

    async def list_orders(request: Request) -> JSONResponse:
        principal = request.headers.get("x-principal", "")  # stand-in for real authentication
        if principal == "":
            error = {"code": 401, "message": "unauthenticated", "status": "UNAUTHENTICATED"}
            return JSONResponse({"error": error}, status_code=401)
        store = str(request.path_params["store"])
        params = request.query_params
        try:
            page_size = parse_page_size(params.get("page_size", "0"))
            status, canonical_filter = parse_filter(params.get("filter", ""))
            order_by = params.get("order_by", "create_time desc")
            if order_by not in _ORDER_BY:
                raise ValueError("order_by must be 'create_time desc' or 'create_time asc'")
        except ValueError as exc:
            return _invalid_argument(str(exc))
        context = QueryContext(principal, f"stores/{store}", canonical_filter, order_by)
        after: Position | None = None
        page_token = params.get("page_token", "")
        if page_token:
            try:
                after = codec.open(page_token, context)
            except InvalidPageToken:
                return _invalid_argument(
                    "page_token is invalid, expired, or was issued for different parameters"
                )
        page, next_position = select_page(
            rows,
            principal=principal,
            store=store,
            status=status,
            descending=_ORDER_BY[order_by],
            after=after,
            page_size=page_size,
        )
        next_token = "" if next_position is None else codec.mint(next_position, context)
        orders = [{"id": row.row_id, "create_time": row.create_time} for row in page]
        return JSONResponse({"orders": orders, "next_page_token": next_token})

    return Starlette(routes=[Route("/v1/stores/{store}/orders", list_orders, methods=["GET"])])
