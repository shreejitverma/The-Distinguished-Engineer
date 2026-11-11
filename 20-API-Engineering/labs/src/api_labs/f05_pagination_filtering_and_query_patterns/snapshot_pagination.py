"""Snapshot-consistent pagination: four cursor strategies over one versioned SQLite store.

Every committed write takes the next value of a logical commit clock, like a commit LSN.
Each row version carries the half-open interval [valid_from, valid_to) in that clock, so the
store can answer "what was visible at commit N" exactly, until garbage collection drops the
versions that ended at or before the retention horizon.
Ids come from a non-transactional allocator, like a PostgreSQL sequence: an id is fixed when a
writer starts and becomes visible only when the writer commits, possibly after larger ids.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, Self, cast

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.requests import Request

SCHEMA: Final = """
CREATE TABLE versions (
    id INTEGER NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    balance INTEGER NOT NULL,
    PRIMARY KEY (id, valid_from)
) WITHOUT ROWID;
"""

CLOSE_SQL: Final = "UPDATE versions SET valid_to = :seq WHERE id = :id AND valid_to IS NULL"
OPEN_SQL: Final = "INSERT INTO versions (id, valid_from, valid_to, balance) VALUES (?, ?, NULL, ?)"
LIVE_BALANCE_SQL: Final = "SELECT balance FROM versions WHERE id = ? AND valid_to IS NULL"
GC_SQL: Final = "DELETE FROM versions WHERE valid_to IS NOT NULL AND valid_to <= ?"


class Strategy(Enum):
    """What the cursor pins, written as the visibility predicate every page applies."""

    LIVE = "valid_to IS NULL"
    HIGH_WATER_MARK = "valid_to IS NULL AND id <= :bound"
    VISIBILITY_BOUND = "valid_to IS NULL AND valid_from <= :bound"
    AS_OF = "valid_from <= :bound AND (valid_to IS NULL OR valid_to > :bound)"


class Order(Enum):
    """Keyset sort keys; every key ends in the unique id."""

    ID = ("id",)
    LAST_MODIFIED = ("valid_from", "id")


class InvalidCursorError(ValueError):
    """The token is malformed or names a snapshot that does not exist yet."""


class CursorDidNotAdvanceError(RuntimeError):
    """A continuation cursor did not move past the previous one, so a walk would loop."""


class SnapshotExpiredError(LookupError):
    """The snapshot is older than the retention horizon, so its versions may be gone."""

    def __init__(self, as_of: int, horizon: int) -> None:
        super().__init__(f"snapshot {as_of} is older than the retention horizon {horizon}")
        self.as_of = as_of
        self.horizon = horizon


@dataclass(frozen=True, slots=True)
class Account:
    id: int
    balance: int
    version: int  # valid_from of the version served


@dataclass(frozen=True, slots=True)
class PendingInsert:
    """An id handed out by the allocator whose row is not committed yet."""

    id: int
    balance: int


@dataclass(frozen=True, slots=True)
class Cursor:
    strategy: Strategy
    order: Order
    bound: int | None  # pinned on page 1 and carried unchanged; None for LIVE
    after: tuple[int, ...] | None  # sort key of the last row served


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Account, ...]
    next_cursor: Cursor | None


def page_sql(strategy: Strategy, order: Order, *, after: bool) -> str:
    """Build the page query from module constants only; values travel as named binds."""
    cols = ", ".join(order.value)
    where = f"({strategy.value})"
    if after:
        marks = ", ".join(f":k{i}" for i in range(len(order.value)))
        where += f" AND ({cols}) > ({marks})"
    return (
        f"SELECT id, balance, valid_from FROM versions WHERE {where} "  # noqa: S608
        f"ORDER BY {cols} LIMIT :limit"
    )


class Store:
    """A single-connection versioned table with a logical commit clock."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.executescript(SCHEMA)
        self.clock = 0  # sequence of the last committed write
        self.horizon = 0  # oldest snapshot that is still readable
        self._next_id = 1

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def allocate(self, balance: int) -> PendingInsert:
        """Take the next id now, like nextval(); the row is invisible until commit_insert."""
        pending = PendingInsert(self._next_id, balance)
        self._next_id += 1
        return pending

    def commit_insert(self, pending: PendingInsert) -> int:
        return self._commit({pending.id: pending.balance})

    def insert(self, balance: int) -> int:
        pending = self.allocate(balance)
        self.commit_insert(pending)
        return pending.id

    def update(self, account_id: int, balance: int) -> int:
        self._live_balance(account_id)
        return self._commit({account_id: balance})

    def delete(self, account_id: int) -> int:
        self._live_balance(account_id)
        return self._commit({account_id: None})

    def transfer(self, src: int, dst: int, amount: int) -> int:
        """Move *amount* between two accounts in one commit, so the total is invariant."""
        src_balance, dst_balance = self._live_balance(src), self._live_balance(dst)
        return self._commit({src: src_balance - amount, dst: dst_balance + amount})

    def gc(self, horizon: int) -> int:
        """Drop versions no snapshot at or after *horizon* can see; return how many."""
        if not self.horizon <= horizon <= self.clock:
            raise ValueError(f"horizon {horizon} outside [{self.horizon}, {self.clock}]")
        removed = self.conn.execute(GC_SQL, (horizon,)).rowcount
        self.horizon = horizon
        return removed

    def snapshot_bound(self, strategy: Strategy) -> int | None:
        """The value page 1 pins into the cursor."""
        if strategy is Strategy.LIVE:
            return None
        if strategy is Strategy.HIGH_WATER_MARK:
            row = self.conn.execute("SELECT max(id) FROM versions WHERE valid_to IS NULL")
            return cast("tuple[int | None]", row.fetchone())[0] or 0
        return self.clock

    def _live_balance(self, account_id: int) -> int:
        row = self.conn.execute(LIVE_BALANCE_SQL, (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return cast("tuple[int]", row)[0]

    def _commit(self, changes: Mapping[int, int | None]) -> int:
        """Apply {id: new balance, or None to delete} as one commit at clock + 1."""
        seq = self.clock + 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for account_id, balance in changes.items():
                self.conn.execute(CLOSE_SQL, {"seq": seq, "id": account_id})
                if balance is not None:
                    self.conn.execute(OPEN_SQL, (account_id, seq, balance))
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        self.clock = seq
        return seq


def fetch_page(
    store: Store,
    cursor: Cursor | None,
    *,
    limit: int,
    strategy: Strategy = Strategy.AS_OF,
    order: Order = Order.ID,
) -> Page:
    """Serve one page; page 1 pins the bound, later pages reuse the cursor's bound."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if cursor is None:
        cursor = Cursor(strategy, order, store.snapshot_bound(strategy), None)
    bound = cursor.bound
    if cursor.strategy is Strategy.AS_OF and bound is not None:
        if bound > store.clock:
            raise InvalidCursorError(f"snapshot {bound} is in the future")
        if bound < store.horizon:
            raise SnapshotExpiredError(bound, store.horizon)
    params: dict[str, int | None] = {"bound": bound, "limit": limit + 1}
    if cursor.after is not None:
        params |= {f"k{i}": k for i, k in enumerate(cursor.after)}
    sql = page_sql(cursor.strategy, cursor.order, after=cursor.after is not None)
    rows = cast("list[tuple[int, int, int]]", store.conn.execute(sql, params).fetchall())
    items = tuple(Account(*row) for row in rows[:limit])
    if len(rows) <= limit:
        return Page(items, None)
    last = items[-1]
    key = (last.id,) if cursor.order is Order.ID else (last.version, last.id)
    return Page(items, Cursor(cursor.strategy, cursor.order, bound, key))


def walk(
    store: Store,
    *,
    limit: int,
    strategy: Strategy,
    order: Order = Order.ID,
    between_pages: Callable[[int], object] | None = None,
) -> list[Account]:
    """Follow next_cursor to the end, calling *between_pages(n)* after page n is served."""
    served: list[Account] = []
    cursor: Cursor | None = None
    page_number = 0
    while True:
        page = fetch_page(store, cursor, limit=limit, strategy=strategy, order=order)
        served.extend(page.items)
        page_number += 1
        nxt = page.next_cursor
        if nxt is None:
            return served
        # Ascending keyset: every continuation key must sort strictly after the previous one.
        if cursor is not None and cursor.after is not None and (nxt.after or ()) <= cursor.after:
            raise CursorDidNotAdvanceError(f"{nxt.after} does not follow {cursor.after}")
        if between_pages is not None:
            between_pages(page_number)
        cursor = nxt


# --- A server-held snapshot: one SQLite WAL read transaction kept open across pages ---


def open_wal(path: str) -> sqlite3.Connection:
    """Open *path* in WAL mode with autocommit, so BEGIN and COMMIT are explicit."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def checkpoint_truncate(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Run PRAGMA wal_checkpoint(TRUNCATE) and return (busy, log frames, checkpointed)."""
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return cast("tuple[int, int, int]", row)


# --- The HTTP surface: AS_OF snapshots in a decimal "<as_of>.<last id>" token ---

DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100
TOKEN_RE: Final = re.compile(r"(0|[1-9][0-9]{0,18})\.(0|[1-9][0-9]{0,18})")


def encode_token(cursor: Cursor) -> str:
    if cursor.strategy is not Strategy.AS_OF or cursor.bound is None or cursor.after is None:
        raise ValueError("only an AS_OF cursor past page 1 has a token")
    return f"{cursor.bound}.{cursor.after[0]}"


def decode_token(token: str) -> Cursor:
    match = TOKEN_RE.fullmatch(token)
    if match is None:
        raise InvalidCursorError("malformed page_token")
    as_of, after = int(match[1]), int(match[2])
    return Cursor(Strategy.AS_OF, Order.ID, as_of, (after,))


def parse_page_size(raw: str | None) -> int:
    """AIP-158: absent or 0 -> default, above max -> coerced, negative -> invalid."""
    if raw is None:
        return DEFAULT_PAGE_SIZE
    if re.fullmatch(r"-?[0-9]{1,9}", raw) is None:
        raise InvalidCursorError("page_size must be an integer")
    size = int(raw)
    if size < 0:
        raise InvalidCursorError("page_size must be >= 0")
    return min(size, MAX_PAGE_SIZE) if size else DEFAULT_PAGE_SIZE


def create_app(store: Store) -> Starlette:
    async def list_accounts(request: Request) -> JSONResponse:
        try:
            limit = parse_page_size(request.query_params.get("page_size"))
            token = request.query_params.get("page_token", "")
            cursor = decode_token(token) if token else None
            page = fetch_page(store, cursor, limit=limit)
        except InvalidCursorError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except SnapshotExpiredError as exc:
            body = {"error": "snapshot expired", "as_of": exc.as_of, "horizon": exc.horizon}
            return JSONResponse(body, status_code=410)
        as_of = cursor.bound if cursor is not None else store.clock
        nxt = page.next_cursor
        return JSONResponse(
            {
                "accounts": [{"id": a.id, "balance": a.balance} for a in page.items],
                "as_of": as_of,
                "next_page_token": encode_token(nxt) if nxt is not None else "",
            }
        )

    return Starlette(routes=[Route("/accounts", list_accounts, methods=["GET"])])
