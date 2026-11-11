"""Exact, capped, and planner-estimated total counts beside a keyset-paginated list endpoint."""

from __future__ import annotations

import sqlite3
import threading
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, Header, Query, Response
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_COUNT_CAP = 10_000
TENANT_INDEX = "ix_items_tenant_id_id"

_SCHEMA = (
    "CREATE TABLE items (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, name TEXT NOT NULL)",
    f"CREATE INDEX {TENANT_INDEX} ON items (tenant_id, id)",
)
_PAGE_SQL = "SELECT id, name FROM items WHERE tenant_id = ? AND id > ? ORDER BY id LIMIT ?"
_EXACT_SQL = "SELECT COUNT(*) FROM items WHERE tenant_id = ?"
_CAPPED_SQL = "SELECT COUNT(*) FROM (SELECT 1 FROM items WHERE tenant_id = ? LIMIT ?)"
_STAT_SQL = "SELECT stat FROM sqlite_stat1 WHERE tbl = 'items' AND idx = ?"


class CountRelation(StrEnum):
    """How `total.value` relates to the true count (Elasticsearch `hits.total.relation` style)."""

    EQ = "eq"
    GTE = "gte"
    APPROX = "approx"


class CountMode(StrEnum):
    """Values of the `Prefer: count=<mode>` preference (PostgREST vocabulary plus `capped`)."""

    EXACT = "exact"
    CAPPED = "capped"
    PLANNED = "planned"
    ESTIMATED = "estimated"


class TotalCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: int
    relation: CountRelation


class Item(BaseModel):
    id: int
    name: str


class ItemPage(BaseModel):
    items: list[Item]
    next_after: int | None
    total: TotalCount | None


class CountBudgetExceededError(Exception):
    """An exact count was aborted because it exceeded its SQLite VM step budget."""


class ItemStore:
    """SQLite collection whose every statement is metered with a progress handler.

    `sqlite3.Connection.set_progress_handler(handler, 1)` calls `handler` roughly once per
    virtual machine instruction, so the callback count is a deterministic cost proxy for one
    SQLite build; a non-zero return value aborts the statement.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self.cost_log: list[tuple[str, int]] = []

    @classmethod
    def create(cls, tenant_sizes: Mapping[int, int]) -> ItemStore:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        for statement in _SCHEMA:
            conn.execute(statement)
        store = cls(conn)
        for tenant_id, size in tenant_sizes.items():
            store.insert_items(tenant_id, size)
        return store

    def insert_items(self, tenant_id: int, count: int) -> None:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()
            start = int(row[0]) + 1
            rows = ((i, tenant_id, f"item-{i}") for i in range(start, start + count))
            self._conn.executemany("INSERT INTO items (id, tenant_id, name) VALUES (?, ?, ?)", rows)
            self._conn.commit()

    def analyze(self) -> None:
        with self._lock:
            self._conn.execute("ANALYZE")
            self._conn.commit()

    def explain(self, sql: str, params: tuple[int, ...] = ()) -> list[str]:
        """Return the VDBE opcode names SQLite compiles `sql` into."""
        with self._lock:
            return [str(row[1]) for row in self._conn.execute(f"EXPLAIN {sql}", params)]

    def run(
        self, label: str, sql: str, params: tuple[int, ...], budget: int | None = None
    ) -> list[Any]:
        steps = 0

        def on_progress() -> int:
            nonlocal steps
            steps += 1
            return 1 if budget is not None and steps > budget else 0

        with self._lock:
            self._conn.set_progress_handler(on_progress, 1)
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                if budget is not None and steps > budget:
                    raise CountBudgetExceededError(f"{label}: over {budget} steps") from exc
                raise
            finally:
                self._conn.set_progress_handler(None, 1)
        self.cost_log.append((label, steps))
        return rows

    def page(self, tenant_id: int, after: int, limit: int) -> tuple[list[Item], int | None]:
        """Keyset page: fetch `limit + 1` rows so `next_after` is set only if more rows exist."""
        rows = self.run("page", _PAGE_SQL, (tenant_id, after, limit + 1))
        items = [Item(id=int(r[0]), name=str(r[1])) for r in rows[:limit]]
        next_after = items[-1].id if len(rows) > limit else None
        return items, next_after

    def exact_count(self, tenant_id: int, budget: int | None = None) -> TotalCount:
        rows = self.run("count:exact", _EXACT_SQL, (tenant_id,), budget)
        return TotalCount(value=int(rows[0][0]), relation=CountRelation.EQ)

    def capped_count(self, tenant_id: int, cap: int) -> TotalCount:
        """Count at most `cap + 1` index entries: `eq` when <= cap, else `gte` cap."""
        rows = self.run("count:capped", _CAPPED_SQL, (tenant_id, cap + 1))
        seen = int(rows[0][0])
        if seen > cap:
            return TotalCount(value=cap, relation=CountRelation.GTE)
        return TotalCount(value=seen, relation=CountRelation.EQ)

    def planner_estimate(self) -> TotalCount | None:
        """Average rows per `tenant_id` from `sqlite_stat1`; `None` before the first ANALYZE."""
        with self._lock:
            has_stats = self._conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'sqlite_stat1'"
            ).fetchone()
            row = self._conn.execute(_STAT_SQL, (TENANT_INDEX,)).fetchone() if has_stats else None
        if row is None:
            return None
        # stat = "<rows in index> <avg rows per distinct tenant_id> <avg rows per (tenant_id, id)>"
        rows_per_tenant = int(str(row[0]).split()[1])
        return TotalCount(value=rows_per_tenant, relation=CountRelation.APPROX)


def pg_estimate_tuples(reltuples: float, relpages: int, curpages: int) -> float | None:
    """Model of PostgreSQL's density scaling in `table_block_relation_estimate_size`.

    The planner scales `pg_class.reltuples / relpages` (the density at the last VACUUM or
    ANALYZE) by the current block count. `None` marks the cases with no density, where
    PostgreSQL falls back to a width-based guess not modelled here: `relpages <= 0`, and the
    never-vacuumed case (`reltuples < 0`), which PostgreSQL first raises to at least 10 pages,
    so it never takes the empty-relation exit. Inheritance parents, exempt from that 10-page
    minimum, are not modelled.
    """
    if reltuples < 0:
        return None
    if curpages == 0:
        return 0.0
    if relpages <= 0:
        return None
    # C rint() rounds half to even under the default rounding mode, as round() does.
    return float(round(reltuples / relpages * curpages))


def parse_count_preference(prefer_values: list[str]) -> CountMode | None:
    """Return the first `count` preference across all `Prefer` field lines (RFC 7240 Section 2).

    Unrecognized tokens and values are ignored rather than rejected. Simplified parser: it does
    not handle commas inside quoted-string parameter values.
    """
    for field_value in prefer_values:
        for preference in field_value.split(","):
            token, _, value = preference.split(";", 1)[0].partition("=")
            if token.strip() != "count":
                continue
            try:
                return CountMode(value.strip().strip('"'))
            except ValueError:
                return None
    return None


def resolve_total(
    store: ItemStore, tenant_id: int, mode: CountMode, *, cap: int, allow_exact: bool
) -> tuple[TotalCount, CountMode]:
    """Apply the server's count policy; returns the total and the mode actually applied."""
    if mode is CountMode.EXACT and allow_exact:
        return store.exact_count(tenant_id), CountMode.EXACT
    if mode is CountMode.PLANNED:
        estimate = store.planner_estimate()
        if estimate is not None:
            return estimate, CountMode.PLANNED
    capped = store.capped_count(tenant_id, cap)
    if mode is CountMode.ESTIMATED and capped.relation is CountRelation.GTE:
        estimate = store.planner_estimate()
        if estimate is not None:
            # The capped scan proved at least `cap` rows, so never report fewer.
            value = max(estimate.value, cap)
            return TotalCount(value=value, relation=CountRelation.APPROX), CountMode.ESTIMATED
    if mode is CountMode.ESTIMATED:
        return capped, CountMode.ESTIMATED
    return capped, CountMode.CAPPED


def build_app(
    store: ItemStore, *, cap: int = DEFAULT_COUNT_CAP, allow_exact: bool = False
) -> FastAPI:
    app = FastAPI()

    @app.get("/tenants/{tenant_id}/items")
    def list_items(
        tenant_id: int,
        response: Response,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        prefer: Annotated[list[str] | None, Header()] = None,
    ) -> ItemPage:
        items, next_after = store.page(tenant_id, after, limit)
        mode = parse_count_preference(prefer or [])
        total: TotalCount | None = None
        if mode is not None:
            total, applied = resolve_total(store, tenant_id, mode, cap=cap, allow_exact=allow_exact)
            response.headers["Preference-Applied"] = f"count={applied.value}"
        return ItemPage(items=items, next_after=next_after, total=total)

    return app
