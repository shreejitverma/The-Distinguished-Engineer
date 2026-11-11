"""Keyset (seek) pagination with a (created_at, id) row-value cursor over SQLite.

The page query seeks past the last row of the previous page with the row-value
predicate ``(created_at, id) < (?, ?)`` and lets a composite B-tree index on
``(created_at, id)`` deliver rows already in ``ORDER BY created_at DESC, id DESC``.
The module also carries the predicates engineers write by mistake, so the lab can
show what each one returns, which plan SQLite picks, and how many virtual machine
instructions a page costs.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, cast

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.requests import Request

# `id` is a declared PRIMARY KEY in a WITHOUT ROWID table, so it is a real index
# column; in a rowid table `id INTEGER PRIMARY KEY` would alias the rowid instead.
TABLE_DDL: Final = (
    "CREATE TABLE events ("
    "id INTEGER NOT NULL PRIMARY KEY, "
    "created_at INTEGER NOT NULL, "
    "title TEXT NOT NULL"
    ") WITHOUT ROWID"
)
ROWID_ALIAS_TABLE_DDL: Final = (
    "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, title TEXT NOT NULL)"
)
INDEX_DDL: Final = "CREATE INDEX ix_events_created_id ON events (created_at, id)"

ORDER_BY: Final = "ORDER BY created_at DESC, id DESC"
DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100
_INT64_MAX: Final = 2**63 - 1
_CURSOR_RE: Final = re.compile(r"(0|[1-9][0-9]{0,18})\.(0|[1-9][0-9]{0,18})")


class Predicate(Enum):
    """WHERE clauses that claim to mean "rows after the cursor" in DESC order."""

    ROW_VALUE = "(created_at, id) < (?, ?)"
    EXPANDED_OR = "(created_at < ? OR (created_at = ? AND id < ?))"
    BOUNDED_OR = "created_at <= ? AND (created_at < ? OR id < ?)"
    TIMESTAMP_LT = "created_at < ?"
    TIMESTAMP_LE = "created_at <= ?"


@dataclass(frozen=True, slots=True, order=True)
class SeekKey:
    """The sort key of the last row a client has seen: the whole cursor payload."""

    created_at: int
    id: int


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    created_at: int
    title: str

    @property
    def key(self) -> SeekKey:
        return SeekKey(self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Event, ...]
    next_key: SeekKey | None


class CursorDidNotAdvanceError(RuntimeError):
    """A page ended at or before the cursor it started from, so the walk would loop."""


def open_store(table_ddl: str = TABLE_DDL, index_ddl: str | None = INDEX_DDL) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(table_ddl)
    if index_ddl is not None:
        conn.execute(index_ddl)
    return conn


def insert_events(conn: sqlite3.Connection, events: Iterable[Event]) -> None:
    conn.executemany(
        "INSERT INTO events (id, created_at, title) VALUES (?, ?, ?)",
        ((e.id, e.created_at, e.title) for e in events),
    )


_SELECT: Final = "SELECT id, created_at, title FROM events "
# Every statement is assembled from module constants only; values always travel as `?` binds.
_PAGE_SQL: Final = {
    p: _SELECT + "WHERE " + p.value + " " + ORDER_BY + " LIMIT ?" for p in Predicate
}
_FIRST_PAGE_SQL: Final = _SELECT + ORDER_BY + " LIMIT ?"
_OFFSET_SQL: Final = _SELECT + ORDER_BY + " LIMIT ? OFFSET ?"


def page_sql(predicate: Predicate | None) -> str:
    return _FIRST_PAGE_SQL if predicate is None else _PAGE_SQL[predicate]


def bind(predicate: Predicate, after: SeekKey) -> tuple[int, ...]:
    t, i = after.created_at, after.id
    match predicate:
        case Predicate.ROW_VALUE:
            return (t, i)
        case Predicate.EXPANDED_OR | Predicate.BOUNDED_OR:
            return (t, t, i)
        case Predicate.TIMESTAMP_LT | Predicate.TIMESTAMP_LE:
            return (t,)


def fetch_page(
    conn: sqlite3.Connection,
    *,
    after: SeekKey | None,
    limit: int,
    predicate: Predicate = Predicate.ROW_VALUE,
) -> Page:
    """Return up to *limit* rows after *after*, reading one extra row to detect the end."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    keys: tuple[int, ...] = ()
    if after is None:
        sql = page_sql(None)
    else:
        sql, keys = page_sql(predicate), bind(predicate, after)
    params = (*keys, limit + 1)
    rows = cast("list[tuple[int, int, str]]", conn.execute(sql, params).fetchall())
    items = tuple(Event(*row) for row in rows[:limit])
    has_more = len(rows) > limit
    return Page(items, items[-1].key if has_more else None)


def walk(
    conn: sqlite3.Connection, *, limit: int, predicate: Predicate = Predicate.ROW_VALUE
) -> list[int]:
    """Follow next_key to the end and return every id served, in order."""
    served: list[int] = []
    after: SeekKey | None = None
    while True:
        page = fetch_page(conn, after=after, limit=limit, predicate=predicate)
        served.extend(e.id for e in page.items)
        if page.next_key is None:
            return served
        # DESC order: every next cursor must sort strictly below the previous one.
        if after is not None and page.next_key >= after:
            raise CursorDidNotAdvanceError(f"{page.next_key} does not precede {after}")
        after = page.next_key


def offset_page(conn: sqlite3.Connection, *, offset: int, limit: int) -> tuple[Event, ...]:
    rows = cast("list[tuple[int, int, str]]", conn.execute(_OFFSET_SQL, (limit, offset)).fetchall())
    return tuple(Event(*row) for row in rows)


def query_plan(conn: sqlite3.Connection, sql: str, params: tuple[int, ...]) -> tuple[str, ...]:
    """The `detail` column of EXPLAIN QUERY PLAN, one string per plan node."""
    cursor = conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    rows = cast("list[tuple[int, int, int, str]]", cursor.fetchall())
    return tuple(row[3] for row in rows)


def vm_steps(conn: sqlite3.Connection, sql: str, params: tuple[int, ...]) -> int:
    """Count SQLite virtual machine instructions to run *sql* to completion."""
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0  # non-zero would abort the statement

    conn.set_progress_handler(tick, 1)
    try:
        conn.execute(sql, params).fetchall()
    finally:
        conn.set_progress_handler(None, 1)
    return steps


def encode_cursor(key: SeekKey) -> str:
    # Transparent on purpose: signing and opacity are a separate concern (AIP-158).
    return f"{key.created_at}.{key.id}"


def decode_cursor(raw: str) -> SeekKey:
    match = _CURSOR_RE.fullmatch(raw)
    if match is None:
        raise ValueError("malformed cursor")
    created_at, id_ = int(match[1]), int(match[2])
    if created_at > _INT64_MAX or id_ > _INT64_MAX:
        raise ValueError("cursor component exceeds int64")
    return SeekKey(created_at, id_)


def parse_page_size(raw: str | None) -> int:
    """AIP-158 rules: absent or 0 -> default, above max -> coerced, negative -> error."""
    if raw is None:
        return DEFAULT_PAGE_SIZE
    if not re.fullmatch(r"-?[0-9]{1,9}", raw):
        raise ValueError("page_size must be an integer")
    size = int(raw)
    if size < 0:
        raise ValueError("page_size must not be negative")
    return DEFAULT_PAGE_SIZE if size == 0 else min(size, MAX_PAGE_SIZE)


def create_app(conn: sqlite3.Connection) -> Starlette:
    async def list_events(request: Request) -> JSONResponse:
        try:
            size = parse_page_size(request.query_params.get("page_size"))
            raw_token = request.query_params.get("page_token")
            after = decode_cursor(raw_token) if raw_token else None
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # In-memory SQLite answers in microseconds; a networked driver must not block the loop.
        page = fetch_page(conn, after=after, limit=size)
        return JSONResponse(
            {
                "events": [
                    {"id": e.id, "created_at": e.created_at, "title": e.title} for e in page.items
                ],
                "next_page_token": "" if page.next_key is None else encode_cursor(page.next_key),
            }
        )

    return Starlette(routes=[Route("/events", list_events, methods=["GET"])])
