"""Resource identifier lab: UUIDv4 and UUIDv7 (RFC 9562), base32 text forms, and typed public IDs.

The module has four parts.
`uuid4_from_bytes`, `uuid7_from_parts`, and `UUIDv7Generator` build RFC 9562 layouts with an
injectable clock and entropy source, so monotonicity, counter rollover, and clock regression
can be tested deterministically.
`encode_base32` and `decode_base32` implement the strict 26-character Crockford alphabet used
by ULID and TypeID, and `decode_crockford_lenient` implements Crockford's forgiving decoder to
show why lenient parsing creates aliases.
`PublicId` is a TypeID-style `prefix_suffix` identifier, and `build_app` serves orders with it.
`measure_batch_insert` counts the pages one batch of inserts dirties in a SQLite B-tree.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from starlette.requests import Request

UNIX_TS_MS_MAX = (1 << 48) - 1
RAND_A_MAX = (1 << 12) - 1
RAND_B_MAX = (1 << 62) - 1
RAND_BITS = 74  # rand_a (12) + rand_b (62), RFC 9562 Section 5.7
VERSION_7_FLAGS = (0x7 << 76) | (0b10 << 62)

# Crockford base32 symbols in ascending ASCII order, so string order equals numeric order.
ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_VALUE = {symbol: index for index, symbol in enumerate(ALPHABET)}
# Crockford's decode table: case-insensitive, i and l read as 1, o reads as 0, hyphens ignored.
_LENIENT = {**_VALUE, "i": 1, "l": 1, "o": 0}
SUFFIX_LENGTH = 26  # 130 bits, the top two always zero
# TypeID spec 0.3.0 prefix grammar.
PREFIX = re.compile(r"([a-z]([a-z_]{0,61}[a-z])?)?")


class InvalidIdError(ValueError):
    """The text is not a well-formed public identifier."""


class WrongTypeError(InvalidIdError):
    """The identifier is well formed but names a different resource type."""


def uuid4_from_bytes(random16: bytes) -> uuid.UUID:
    """RFC 9562 Section 5.4: 128 random bits with the version and variant bits overwritten."""
    if len(random16) != 16:
        raise ValueError("UUIDv4 needs exactly 16 random bytes")
    value = int.from_bytes(random16, "big")
    value &= ~((0xF << 76) | (0b11 << 62))
    value |= (0x4 << 76) | (0b10 << 62)
    return uuid.UUID(int=value)


def uuid7_from_parts(unix_ts_ms: int, rand_a: int, rand_b: int) -> uuid.UUID:
    """RFC 9562 Section 5.7: unix_ts_ms (48) | ver (4) | rand_a (12) | var (2) | rand_b (62)."""
    if not 0 <= unix_ts_ms <= UNIX_TS_MS_MAX:
        raise ValueError("unix_ts_ms must fit in 48 bits")
    if not 0 <= rand_a <= RAND_A_MAX:
        raise ValueError("rand_a must fit in 12 bits")
    if not 0 <= rand_b <= RAND_B_MAX:
        raise ValueError("rand_b must fit in 62 bits")
    return uuid.UUID(int=(unix_ts_ms << 80) | (rand_a << 64) | rand_b | VERSION_7_FLAGS)


def uuid7_unix_ts_ms(value: uuid.UUID) -> int:
    """Read the 48-bit millisecond timestamp; anyone holding the ID can do this."""
    if value.version != 7 or value.variant != uuid.RFC_4122:
        raise ValueError(f"not an RFC 9562 UUIDv7: {value}")
    return value.int >> 80


@dataclass
class UUIDv7Generator:
    """Monotonic UUIDv7 with a fixed bit-length dedicated counter (RFC 9562 Section 6.2, Method 1).

    The counter occupies the leftmost `counter_bits` of the 74 random bits; the rest is fresh
    entropy per ID.
    A new millisecond reseeds the counter with its top bit zero as a rollover guard.
    The same or an earlier millisecond reuses the last timestamp and increments the counter.
    A counter rollover advances the timestamp by one millisecond instead of wrapping.
    """

    clock_ms: Callable[[], int]
    entropy: Callable[[int], bytes] = os.urandom
    counter_bits: int = 42
    _last_ms: int = field(default=-1, init=False)
    _counter: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        # RFC 9562 Section 6.2: "at least 12 bits but no longer than 42 bits".
        if not 12 <= self.counter_bits <= 42:
            raise ValueError("counter_bits must be between 12 and 42")

    def _random_bits(self, bits: int) -> int:
        return int.from_bytes(self.entropy((bits + 7) // 8), "big") >> (-bits % 8)

    def __call__(self) -> uuid.UUID:
        with self._lock:
            now = self.clock_ms()
            if now > self._last_ms:
                self._last_ms = now
                self._counter = self._random_bits(self.counter_bits - 1)
            else:
                self._counter += 1
                if self._counter >> self.counter_bits:
                    self._last_ms += 1
                    self._counter = self._random_bits(self.counter_bits - 1)
            tail_bits = RAND_BITS - self.counter_bits
            rand = (self._counter << tail_bits) | self._random_bits(tail_bits)
            return uuid7_from_parts(self._last_ms, rand >> 62, rand & RAND_B_MAX)


def encode_base32(value: int) -> str:
    """Encode 128 bits as 26 lowercase Crockford symbols, most significant first."""
    if not 0 <= value < 1 << 128:
        raise ValueError("value must fit in 128 bits")
    return "".join(ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


def decode_base32(text: str) -> int:
    """Strict decode: exactly 26 lowercase alphabet symbols and a first symbol of at most `7`."""
    if len(text) != SUFFIX_LENGTH or any(symbol not in _VALUE for symbol in text):
        raise InvalidIdError(f"suffix must be {SUFFIX_LENGTH} symbols from {ALPHABET!r}")
    if text[0] > "7":
        raise InvalidIdError("suffix encodes more than 128 bits")
    value = 0
    for symbol in text:
        value = (value << 5) | _VALUE[symbol]
    return value


def decode_crockford_lenient(text: str) -> int:
    """Crockford's own decoding rules; many distinct strings decode to one value."""
    value = 0
    for symbol in text.lower().replace("-", ""):
        if symbol not in _LENIENT:
            raise InvalidIdError(f"not a Crockford symbol: {symbol!r}")
        value = (value << 5) | _LENIENT[symbol]
    return value


def ulid_text(value: uuid.UUID) -> str:
    """The canonical ULID rendering of the same 128 bits: upper-case Crockford base32."""
    return encode_base32(value.int).upper()


@dataclass(frozen=True, slots=True)
class PublicId:
    """A TypeID-style public identifier: `prefix_` plus the UUID in 26 base32 symbols."""

    prefix: str
    value: uuid.UUID

    def __post_init__(self) -> None:
        if PREFIX.fullmatch(self.prefix) is None:
            raise InvalidIdError(f"prefix must match {PREFIX.pattern!r}")

    def __str__(self) -> str:
        suffix = encode_base32(self.value.int)
        return f"{self.prefix}_{suffix}" if self.prefix else suffix

    @classmethod
    def parse(cls, text: str, expected_prefix: str | None = None) -> PublicId:
        # The prefix may contain `_`, so the separator is the last one.
        prefix, separator, suffix = text.rpartition("_")
        if separator and not prefix:
            raise InvalidIdError("a separator needs a non-empty prefix")
        parsed = cls(prefix, uuid.UUID(int=decode_base32(suffix)))
        if expected_prefix is not None and parsed.prefix != expected_prefix:
            raise WrongTypeError(f"expected a {expected_prefix!r} ID, got {parsed.prefix!r}")
        return parsed


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": status, "status": code, "message": message}}, status)


def build_app(new_uuid: Callable[[], uuid.UUID]) -> Starlette:
    """Orders keyed by 16-byte UUIDs inside, exposed as `ord_...` strings at the boundary."""
    orders: dict[uuid.UUID, dict[str, object]] = {}

    async def create_order(request: Request) -> JSONResponse:
        body = await request.json()
        order_id = PublicId("ord", new_uuid())
        orders[order_id.value] = {"id": str(order_id), "sku": body["sku"]}
        return JSONResponse(
            orders[order_id.value], 201, headers={"Location": f"/v1/orders/{order_id}"}
        )

    async def get_order(request: Request) -> JSONResponse:
        try:
            order_id = PublicId.parse(request.path_params["order_id"], expected_prefix="ord")
        except InvalidIdError as exc:
            # Wrong type and malformed text are client errors, decided before any lookup.
            return error(400, "INVALID_ARGUMENT", str(exc))
        order = orders.get(order_id.value)
        if order is None:
            return error(404, "NOT_FOUND", str(order_id))
        return JSONResponse(order)

    return Starlette(
        routes=[
            Route("/v1/orders", create_order, methods=["POST"]),
            Route("/v1/orders/{order_id}", get_order, methods=["GET"]),
        ]
    )


@dataclass(frozen=True, slots=True)
class BatchCost:
    """What one committed batch of inserts cost the B-tree that holds the key."""

    wal_frames: int  # pages written to the WAL by the batch commit
    key_leaf_pages: int  # leaf pages of the B-tree ordered by the key, after the batch


Layout = Literal["clustered", "heap_plus_index"]


def measure_batch_insert(
    path: Path,
    existing: Sequence[bytes],
    batch: Sequence[bytes],
    layout: Layout = "clustered",
    payload_bytes: int = 100,
) -> BatchCost:
    """Load `existing`, checkpoint, commit `batch` in one transaction, and count WAL frames.

    `clustered` stores rows in key order (`WITHOUT ROWID`, the InnoDB shape); `heap_plus_index`
    appends rows by rowid and keeps a separate unique index on the key (the PostgreSQL shape).
    """
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        if layout == "clustered":
            connection.execute("CREATE TABLE t (id BLOB PRIMARY KEY, body BLOB) WITHOUT ROWID")
            tree = "t"
        else:
            connection.execute("CREATE TABLE t (id BLOB NOT NULL, body BLOB)")
            connection.execute("CREATE UNIQUE INDEX t_id ON t (id)")
            tree = "t_id"

        def commit(keys: Sequence[bytes]) -> None:
            connection.execute("BEGIN")
            connection.executemany(
                "INSERT INTO t (id, body) VALUES (?, zeroblob(?))",
                ((key, payload_bytes) for key in keys),
            )
            connection.execute("COMMIT")

        commit(existing)
        # Empty the WAL so the next count holds only the pages the batch dirtied.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        commit(batch)
        _busy, frames, _done = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        (leaves,) = connection.execute(
            "SELECT count(*) FROM dbstat WHERE name = ? AND pagetype = 'leaf'", (tree,)
        ).fetchone()
        return BatchCost(wal_frames=int(frames), key_leaf_pages=int(leaves))
    finally:
        connection.close()
