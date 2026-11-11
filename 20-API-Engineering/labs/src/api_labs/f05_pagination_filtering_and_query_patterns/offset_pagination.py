"""Offset/limit pagination on SQLite: linear skip cost and window drift under concurrent writes."""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import FastAPI, Query, Response
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

MAX_LIMIT = 100
MAX_OFFSET = 10_000

SCHEMA = (
    "CREATE TABLE items (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL)",
    "CREATE INDEX items_created_at_id ON items (created_at, id)",
)
# Newest first; `id` breaks ties so the order is total and every row has one position.
OFFSET_SQL = "SELECT id, created_at FROM items ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
# The same query with a probe in WHERE: SQLite calls visit() once for every row the scan reads,
# including the OFFSET rows it then discards.
COUNTED_OFFSET_SQL = (
    "SELECT id, created_at FROM items WHERE visit(id) "
    "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
)
# Keyset contrast: seek past the last row seen instead of counting rows to skip.
COUNTED_KEYSET_SQL = (
    "SELECT id, created_at FROM items WHERE (created_at, id) < (?, ?) AND visit(id) "
    "ORDER BY created_at DESC, id DESC LIMIT ?"
)

type SortKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Page:
    """One page of ids, the sort key of its last row, and how many rows the scan read."""

    ids: tuple[int, ...]
    last_key: SortKey | None
    rows_visited: int


class ItemStore:
    """An in-memory SQLite table of items ordered by (created_at DESC, id DESC)."""

    def __init__(self) -> None:
        # One connection shared by FastAPI's threadpool, serialized by self._lock.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._visited = 0
        self._conn.create_function("visit", 1, self._visit, deterministic=False)
        for statement in SCHEMA:
            self._conn.execute(statement)

    def _visit(self, _item_id: int) -> int:
        self._visited += 1
        return 1

    def insert_many(self, rows: Iterable[SortKey]) -> None:
        """Insert (id, created_at) rows."""
        with self._lock:
            self._conn.executemany("INSERT INTO items (id, created_at) VALUES (?, ?)", rows)

    def delete(self, item_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

    def all_ids(self) -> tuple[int, ...]:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM items ORDER BY created_at DESC, id DESC")
            return tuple(item_id for (item_id,) in rows)

    def _counted(self, sql: str, params: tuple[int, ...]) -> Page:
        with self._lock:
            self._visited = 0
            rows: list[SortKey] = self._conn.execute(sql, params).fetchall()
            visited = self._visited
        last_key = (rows[-1][1], rows[-1][0]) if rows else None
        return Page(tuple(item_id for item_id, _ in rows), last_key, visited)

    def offset_page(self, offset: int, limit: int) -> Page:
        return self._counted(COUNTED_OFFSET_SQL, (limit, offset))

    def keyset_page(self, after: SortKey | None, limit: int) -> Page:
        # (created_at, id) of the last row seen; None starts above every real key.
        created_at, item_id = after if after is not None else (2**62, 2**62)
        return self._counted(COUNTED_KEYSET_SQL, (created_at, item_id, limit))

    def vm_steps(self, offset: int, limit: int) -> int:
        """Count SQLite virtual machine instructions for the uninstrumented OFFSET_SQL."""
        steps = 0

        def tick() -> int:
            nonlocal steps
            steps += 1
            return 0  # nonzero would interrupt the query

        with self._lock:
            self._conn.set_progress_handler(tick, 1)
            try:
                self._conn.execute(OFFSET_SQL, (limit, offset)).fetchall()
            finally:
                self._conn.set_progress_handler(None, 1)
        return steps

    def query_plan(self, sql: str, params: tuple[int, ...]) -> list[str]:
        with self._lock:
            return [row[3] for row in self._conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]

    def opcodes(self, sql: str, params: tuple[int, ...]) -> list[str]:
        with self._lock:
            return [row[1] for row in self._conn.execute(f"EXPLAIN {sql}", params)]


@dataclass(frozen=True, slots=True)
class Walk:
    """Every id a client saw while paging to the end, and the total rows the database read."""

    seen: tuple[int, ...]
    rows_visited: int
    requests: int

    @property
    def duplicates(self) -> tuple[int, ...]:
        return tuple(sorted(i for i, n in Counter(self.seen).items() if n > 1))


def walk_offset(
    store: ItemStore, limit: int, between: Callable[[int], None], max_requests: int = 1_000
) -> Walk:
    """Page with offset += limit until a short page; call between(n) after the n-th page."""
    seen: list[int] = []
    offset = visited = requests = 0
    while True:
        if requests == max_requests:
            raise RuntimeError(f"pagination did not terminate after {max_requests} requests")
        page = store.offset_page(offset, limit)
        requests += 1
        visited += page.rows_visited
        seen.extend(page.ids)
        if len(page.ids) < limit:
            return Walk(tuple(seen), visited, requests)
        offset += limit
        between(requests)


def walk_keyset(
    store: ItemStore, limit: int, between: Callable[[int], None], max_requests: int = 1_000
) -> Walk:
    """Page by seeking past the last sort key; same stopping rule and mutation hook."""
    seen: list[int] = []
    after: SortKey | None = None
    visited = requests = 0
    while True:
        if requests == max_requests:
            raise RuntimeError(f"pagination did not terminate after {max_requests} requests")
        page = store.keyset_page(after, limit)
        requests += 1
        visited += page.rows_visited
        seen.extend(page.ids)
        if len(page.ids) < limit:
            return Walk(tuple(seen), visited, requests)
        after = page.last_key
        between(requests)


class ItemPage(BaseModel):
    items: list[int]
    next_offset: int | None


def create_app(store: ItemStore) -> FastAPI:
    """`GET /items?offset=&limit=` with bounded parameters and an RFC 8288 `next` link."""
    app = FastAPI()

    @app.get("/items")
    def list_items(  # pyright: ignore[reportUnusedFunction]
        response: Response,
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    ) -> ItemPage:
        page = store.offset_page(offset, limit)
        next_offset = offset + limit
        if len(page.ids) < limit or next_offset > MAX_OFFSET:
            return ItemPage(items=list(page.ids), next_offset=None)
        response.headers["Link"] = f'</items?offset={next_offset}&limit={limit}>; rel="next"'
        return ItemPage(items=list(page.ids), next_offset=next_offset)

    return app
