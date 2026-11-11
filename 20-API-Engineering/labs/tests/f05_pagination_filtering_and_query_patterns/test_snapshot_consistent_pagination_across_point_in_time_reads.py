"""Lab: snapshot-consistent pagination with point-in-time read tokens."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns import snapshot_pagination
from api_labs.f05_pagination_filtering_and_query_patterns.snapshot_pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Account,
    Cursor,
    CursorDidNotAdvanceError,
    InvalidCursorError,
    Order,
    Page,
    PendingInsert,
    SnapshotExpiredError,
    Store,
    Strategy,
    checkpoint_truncate,
    create_app,
    decode_token,
    encode_token,
    fetch_page,
    open_wal,
    page_sql,
    walk,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


OPEN_STORES: list[Store] = []


@pytest.fixture(autouse=True)
def close_stores() -> Iterator[None]:
    """Close every Store a test opened, so no sqlite3 connection leaks a ResourceWarning."""
    yield
    while OPEN_STORES:
        OPEN_STORES.pop().close()


def new_store() -> Store:
    store = Store()
    OPEN_STORES.append(store)
    return store


class Ledger:
    """Drives a Store and keeps an independent dict model of the state after every commit."""

    def __init__(self, accounts: int = 0, balance: int = 100) -> None:
        self.store = new_store()
        self.states: dict[int, dict[int, int]] = {0: {}}
        for _ in range(accounts):
            self.insert(balance)

    @property
    def live(self) -> dict[int, int]:
        return self.states[self.store.clock]

    def _record(self, changes: dict[int, int | None]) -> None:
        state = dict(self.states[max(self.states)])  # the state before this commit
        for account_id, balance in changes.items():
            if balance is None:
                del state[account_id]
            else:
                state[account_id] = balance
        self.states[self.store.clock] = state

    def insert(self, balance: int = 100) -> int:
        account_id = self.store.insert(balance)
        self._record({account_id: balance})
        return account_id

    def allocate(self, balance: int = 100) -> PendingInsert:
        return self.store.allocate(balance)

    def commit_insert(self, pending: PendingInsert) -> None:
        self.store.commit_insert(pending)
        self._record({pending.id: pending.balance})

    def update(self, account_id: int, balance: int) -> None:
        self.store.update(account_id, balance)
        self._record({account_id: balance})

    def delete(self, account_id: int) -> None:
        self.store.delete(account_id)
        self._record({account_id: None})

    def transfer(self, src: int, dst: int, amount: int) -> None:
        live = self.live
        self.store.transfer(src, dst, amount)
        self._record({src: live[src] - amount, dst: live[dst] + amount})


def as_map(served: list[Account]) -> dict[int, int]:
    return {a.id: a.balance for a in served}


def ids(served: list[Account]) -> list[int]:
    return [a.id for a in served]


def matches_some_snapshot(ledger: Ledger, served: list[Account]) -> bool:
    return len(set(ids(served))) == len(served) and as_map(served) in ledger.states.values()


# --- Inserts and late commits ---


@pytest.mark.unit
def test_live_keyset_serves_rows_inserted_after_page_one() -> None:
    ledger = Ledger(accounts=10)
    as_of = ledger.store.clock
    served = walk(
        ledger.store,
        limit=4,
        strategy=Strategy.LIVE,
        between_pages=lambda n: ledger.insert() if n == 1 else None,
    )
    assert ids(served) == list(range(1, 12))
    assert as_map(served) != ledger.states[as_of]


@pytest.mark.parametrize(
    "strategy", [Strategy.HIGH_WATER_MARK, Strategy.VISIBILITY_BOUND, Strategy.AS_OF]
)
@pytest.mark.unit
def test_every_pinned_bound_excludes_rows_inserted_after_page_one(strategy: Strategy) -> None:
    ledger = Ledger(accounts=10)
    as_of = ledger.store.clock
    served = walk(
        ledger.store,
        limit=4,
        strategy=strategy,
        between_pages=lambda n: ledger.insert() if n == 1 else None,
    )
    assert as_map(served) == ledger.states[as_of]


@pytest.mark.unit
def test_high_water_mark_admits_a_late_commit_and_matches_no_snapshot() -> None:
    ledger = Ledger(accounts=10)
    slow = ledger.allocate()  # id 11 is fixed now and committed later
    ledger.insert()  # id 12 commits first, so max(id) = 12 on page 1
    as_of = ledger.store.clock

    def interleave(page: int) -> None:
        if page == 1:
            ledger.insert()  # id 13, excluded by id <= 12
            ledger.commit_insert(slow)  # id 11 commits last, but 11 <= 12

    served = walk(
        ledger.store, limit=4, strategy=Strategy.HIGH_WATER_MARK, between_pages=interleave
    )
    assert ids(served) == list(range(1, 13))
    assert 11 not in ledger.states[as_of]
    # Every snapshot that contains 11 also contains 13: the walk equals no commit state.
    assert not matches_some_snapshot(ledger, served)


@pytest.mark.unit
def test_high_water_mark_is_the_largest_committed_id_not_the_largest_allocated() -> None:
    ledger = Ledger(accounts=10)
    ledger.allocate()  # id 11 is taken but not committed
    assert ledger.store.snapshot_bound(Strategy.HIGH_WATER_MARK) == 10


@pytest.mark.parametrize("strategy", [Strategy.VISIBILITY_BOUND, Strategy.AS_OF])
@pytest.mark.unit
def test_commit_sequence_bound_excludes_the_late_commit(strategy: Strategy) -> None:
    ledger = Ledger(accounts=10)
    slow = ledger.allocate()
    ledger.insert()
    as_of = ledger.store.clock

    def interleave(page: int) -> None:
        if page == 1:
            ledger.insert()
            ledger.commit_insert(slow)

    served = walk(ledger.store, limit=4, strategy=strategy, between_pages=interleave)
    assert ids(served) == [*range(1, 11), 12]
    assert as_map(served) == ledger.states[as_of]


# --- Updates and deletes ---


def transfer_after_page_one(ledger: Ledger) -> None:
    ledger.transfer(2, 15, 50)  # 2 is already served, 15 is not


@pytest.mark.parametrize(
    ("strategy", "rows", "total"),
    [
        (Strategy.LIVE, 20, 2050),
        (Strategy.HIGH_WATER_MARK, 20, 2050),
        (Strategy.VISIBILITY_BOUND, 19, 1900),
        (Strategy.AS_OF, 20, 2000),
    ],
    ids=["live-mixes-times", "hwm-mixes-times", "visibility-drops-row", "as-of-exact"],
)
@pytest.mark.unit
def test_transfer_during_export_breaks_the_total_unless_read_as_of(
    strategy: Strategy, rows: int, total: int
) -> None:
    ledger = Ledger(accounts=20)
    as_of = ledger.store.clock
    served = walk(
        ledger.store,
        limit=5,
        strategy=strategy,
        between_pages=lambda n: transfer_after_page_one(ledger) if n == 1 else None,
    )
    assert (len(served), sum(a.balance for a in served)) == (rows, total)
    assert (as_map(served) == ledger.states[as_of]) is (strategy is Strategy.AS_OF)


@pytest.mark.parametrize(
    ("strategy", "served_deleted_row"),
    [
        (Strategy.LIVE, False),
        (Strategy.HIGH_WATER_MARK, False),
        (Strategy.VISIBILITY_BOUND, False),
        (Strategy.AS_OF, True),
    ],
)
@pytest.mark.unit
def test_row_deleted_ahead_of_the_cursor_survives_only_in_an_as_of_read(
    strategy: Strategy, served_deleted_row: bool
) -> None:
    ledger = Ledger(accounts=12)
    served = walk(
        ledger.store,
        limit=4,
        strategy=strategy,
        between_pages=lambda n: ledger.delete(9) if n == 1 else None,
    )
    assert (9 in ids(served)) is served_deleted_row


@pytest.mark.parametrize(
    ("strategy", "duplicates"),
    [(Strategy.LIVE, [3]), (Strategy.HIGH_WATER_MARK, [3]), (Strategy.AS_OF, [])],
)
@pytest.mark.unit
def test_last_modified_order_serves_an_updated_row_twice_unless_read_as_of(
    strategy: Strategy, duplicates: list[int]
) -> None:
    ledger = Ledger(accounts=10)
    served = walk(
        ledger.store,
        limit=4,
        strategy=strategy,
        order=Order.LAST_MODIFIED,
        between_pages=lambda n: ledger.update(3, 7) if n == 1 else None,
    )
    seen = ids(served)
    assert sorted({i for i in seen if seen.count(i) > 1}) == duplicates


@pytest.mark.unit
def test_seeded_interleavings_as_of_always_equals_the_page_one_snapshot() -> None:
    diverged = dict.fromkeys(Strategy, 0)
    for seed in range(300):
        for strategy in Strategy:
            rng = random.Random(seed)  # noqa: S311 - deterministic test data, not crypto
            ledger = Ledger(accounts=rng.randint(1, 30))
            pending: list[PendingInsert] = [ledger.allocate() for _ in range(rng.randint(0, 2))]
            as_of = ledger.store.clock

            def interleave(
                page: int,
                rng: random.Random = rng,
                ledger: Ledger = ledger,
                pending: list[PendingInsert] = pending,
            ) -> None:
                if page > 6:  # bounded: an unbounded live walk can chase its own inserts
                    return
                for _ in range(rng.randint(0, 3)):
                    live = sorted(ledger.live)
                    op = rng.choice(["insert", "update", "delete", "transfer", "late"])
                    if op == "late" and pending:
                        ledger.commit_insert(pending.pop(0))
                    elif op == "insert" or len(live) < 2:
                        ledger.insert(rng.randint(1, 500))
                    elif op == "update":
                        ledger.update(rng.choice(live), rng.randint(1, 500))
                    elif op == "delete":
                        ledger.delete(rng.choice(live))
                    else:
                        src, dst = rng.sample(live, 2)
                        ledger.transfer(src, dst, rng.randint(1, 50))

            order = rng.choice(list(Order))
            served = walk(
                ledger.store,
                limit=rng.randint(1, 6),
                strategy=strategy,
                order=order,
                between_pages=interleave,
            )
            exact = as_map(served) == ledger.states[as_of] and len(served) == len(
                ledger.states[as_of]
            )
            if strategy is Strategy.AS_OF:
                assert exact, f"seed {seed}"
            diverged[strategy] += not exact
    assert diverged[Strategy.AS_OF] == 0
    assert min(diverged[s] for s in Strategy if s is not Strategy.AS_OF) > 30


class PageBudgetExceededError(Exception):
    pass


@pytest.mark.parametrize(("strategy", "pages"), [(Strategy.LIVE, None), (Strategy.AS_OF, 10)])
@pytest.mark.unit
def test_live_walk_chases_inserts_that_outpace_it_while_as_of_ends(
    strategy: Strategy, pages: int | None
) -> None:
    ledger = Ledger(accounts=10)

    def two_inserts_per_page(page: int) -> None:
        if page >= 1000:
            raise PageBudgetExceededError
        ledger.insert()
        ledger.insert()

    if pages is None:
        with pytest.raises(PageBudgetExceededError):
            walk(ledger.store, limit=1, strategy=strategy, between_pages=two_inserts_per_page)
        assert len(ledger.live) == 10 + 2 * 999
    else:
        served = walk(ledger.store, limit=1, strategy=strategy, between_pages=two_inserts_per_page)
        assert len(served) == pages


# --- Retention, expiry, and the cost of history ---


@pytest.mark.unit
def test_gc_at_the_horizon_keeps_that_snapshot_exact() -> None:
    ledger = Ledger(accounts=10)
    for account_id in range(1, 11):
        ledger.update(account_id, 200 + account_id)
    horizon = ledger.store.clock
    ledger.delete(4)
    ledger.update(5, 1)
    removed = ledger.store.gc(horizon)
    assert removed == 10  # the ten balance-100 versions closed at or before the horizon
    page = fetch_page(ledger.store, Cursor(Strategy.AS_OF, Order.ID, horizon, None), limit=100)
    assert as_map(list(page.items)) == ledger.states[horizon]


@pytest.mark.unit
def test_resuming_a_token_older_than_the_horizon_raises_instead_of_dropping_rows() -> None:
    ledger = Ledger(accounts=10)
    first = fetch_page(ledger.store, None, limit=4)
    assert first.next_cursor is not None
    ledger.delete(7)
    ledger.store.gc(ledger.store.clock)
    with pytest.raises(SnapshotExpiredError) as info:
        fetch_page(ledger.store, first.next_cursor, limit=4)
    assert (info.value.as_of, info.value.horizon) == (10, 11)


@pytest.mark.unit
def test_a_future_snapshot_is_rejected() -> None:
    ledger = Ledger(accounts=3)
    future = Cursor(Strategy.AS_OF, Order.ID, ledger.store.clock + 1, None)
    with pytest.raises(InvalidCursorError):
        fetch_page(ledger.store, future, limit=2)


@pytest.mark.unit
def test_gc_rejects_a_horizon_beyond_the_clock_or_behind_the_last_one() -> None:
    ledger = Ledger(accounts=3)
    with pytest.raises(ValueError, match="outside"):
        ledger.store.gc(ledger.store.clock + 1)
    ledger.store.gc(2)
    with pytest.raises(ValueError, match="outside"):
        ledger.store.gc(1)


def vm_steps(store: Store, cursor: Cursor) -> int:
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    store.conn.set_progress_handler(tick, 1)
    try:
        fetch_page(store, cursor, limit=20)
    finally:
        store.conn.set_progress_handler(None, 1)
    return steps


@pytest.mark.unit
def test_as_of_page_cost_grows_with_retained_versions_until_gc() -> None:
    store = new_store()
    for _ in range(400):
        store.insert(100)
    fresh = vm_steps(store, Cursor(Strategy.AS_OF, Order.ID, store.clock, (200,)))
    for round_ in range(20):
        for account_id in range(1, 401):
            store.update(account_id, 101 + round_)
    deep = vm_steps(store, Cursor(Strategy.AS_OF, Order.ID, store.clock, (200,)))
    assert deep >= 8 * fresh
    assert store.gc(store.clock) == 8000
    assert vm_steps(store, Cursor(Strategy.AS_OF, Order.ID, store.clock, (200,))) <= 1.1 * fresh


@pytest.mark.unit
def test_as_of_page_seeks_the_primary_key_without_a_sort() -> None:
    sql = page_sql(Strategy.AS_OF, Order.ID, after=True)
    with Store() as store:
        plan = store.conn.execute(
            f"EXPLAIN QUERY PLAN {sql}", {"bound": 1, "k0": 1, "limit": 2}
        ).fetchall()
    details = [row[3] for row in plan]
    assert details == ["SEARCH versions USING PRIMARY KEY (id>?)"]


@pytest.mark.unit
def test_walk_stops_on_an_exact_multiple_of_the_page_size_without_an_empty_page() -> None:
    ledger = Ledger(accounts=8)
    pages: list[int] = []
    served = walk(ledger.store, limit=4, strategy=Strategy.AS_OF, between_pages=pages.append)
    assert (len(served), pages) == (8, [1])


@pytest.mark.unit
def test_store_rejects_bad_limits_and_unknown_ids() -> None:
    store = new_store()
    with pytest.raises(ValueError, match="limit"):
        fetch_page(store, None, limit=0)
    with pytest.raises(KeyError):
        store.update(1, 5)
    with pytest.raises(KeyError):
        store.delete(1)
    assert store.snapshot_bound(Strategy.HIGH_WATER_MARK) == 0
    assert store.snapshot_bound(Strategy.LIVE) is None


@pytest.mark.unit
def test_failed_commit_rolls_back_and_does_not_advance_the_clock() -> None:
    store = new_store()
    store.insert(100)
    pending = PendingInsert(1, 5)  # id 1 already has a version at valid_from 1
    store.update(1, 6)
    clock = store.clock
    store.clock = 0  # force the next commit to reuse valid_from 1 and hit the primary key
    with pytest.raises(Exception, match="UNIQUE"):
        store.commit_insert(pending)
    store.clock = clock
    assert not store.conn.in_transaction
    page = fetch_page(store, None, limit=5)
    assert [(a.id, a.balance) for a in page.items] == [(1, 6)]


class CommitFailsConnection:
    """Delegates to a real connection but fails COMMIT, as SQLITE_BUSY can at commit time."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(
        self, sql: str, parameters: Mapping[str, object] | Sequence[object] = ()
    ) -> sqlite3.Cursor:
        if sql == "COMMIT":
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, parameters)


@pytest.mark.unit
def test_clock_advances_only_after_the_commit_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    store = new_store()
    store.insert(100)
    real = store.conn
    monkeypatch.setattr(store, "conn", CommitFailsConnection(real))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        store.update(1, 5)
    monkeypatch.undo()
    real.execute("ROLLBACK")
    assert store.clock == 1  # a clock bumped before COMMIT would name a snapshot that never existed
    page = fetch_page(store, None, limit=5)
    assert [(a.id, a.balance, a.version) for a in page.items] == [(1, 100, 1)]


# --- A server-held snapshot: SQLite WAL read transaction ---


@pytest.mark.unit
def test_held_wal_read_transaction_is_a_snapshot_and_pins_the_checkpoint(tmp_path: Path) -> None:
    path = str(tmp_path / "export.db")
    writer = open_wal(path)
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)")
    writer.executemany("INSERT INTO t (v) VALUES (?)", [("x" * 100,)] * 100)
    assert checkpoint_truncate(writer) == (0, 0, 0)
    reader = open_wal(path)
    try:
        reader.execute("BEGIN")
        page1 = reader.execute("SELECT id FROM t ORDER BY id LIMIT 5").fetchall()
        writer.executemany("INSERT INTO t (v) VALUES (?)", [("y" * 100,)] * 200)
        busy, frames, done = checkpoint_truncate(writer)
        assert (busy, done) == (1, 0)
        assert frames > 0
        assert Path(path + "-wal").stat().st_size > 0
        rest = reader.execute("SELECT id FROM t WHERE id > ? ORDER BY id", page1[-1]).fetchall()
        assert len(page1) + len(rest) == 100
        reader.execute("COMMIT")
        assert checkpoint_truncate(writer) == (0, 0, 0)
        assert Path(path + "-wal").stat().st_size == 0
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (300,)
    finally:
        reader.close()
        writer.close()


@pytest.mark.unit
def test_autocommit_reads_take_a_new_snapshot_per_statement(tmp_path: Path) -> None:
    path = str(tmp_path / "export.db")
    writer = open_wal(path)
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    writer.executemany("INSERT INTO t (id) VALUES (?)", [(i,) for i in range(1, 11)])
    reader = open_wal(path)
    try:
        page1 = reader.execute("SELECT id FROM t ORDER BY id LIMIT 5").fetchall()
        writer.execute("INSERT INTO t (id) VALUES (11)")
        rest = reader.execute("SELECT id FROM t WHERE id > ? ORDER BY id", page1[-1]).fetchall()
        assert len(page1) + len(rest) == 11
    finally:
        reader.close()
        writer.close()


@pytest.mark.parametrize(
    "cursor",
    [
        Cursor(Strategy.LIVE, Order.ID, None, (3,)),
        Cursor(Strategy.AS_OF, Order.ID, 5, None),
        Cursor(Strategy.HIGH_WATER_MARK, Order.ID, 5, (3,)),
    ],
    ids=["live", "page-one", "hwm"],
)
@pytest.mark.unit
def test_only_an_as_of_continuation_cursor_has_a_token(cursor: Cursor) -> None:
    with pytest.raises(ValueError, match="AS_OF"):
        encode_token(cursor)
    assert decode_token(encode_token(Cursor(Strategy.AS_OF, Order.ID, 5, (3,)))) == Cursor(
        Strategy.AS_OF, Order.ID, 5, (3,)
    )


@pytest.mark.unit
def test_walk_without_a_callback_serves_every_row_once() -> None:
    ledger = Ledger(accounts=9)
    assert ids(walk(ledger.store, limit=2, strategy=Strategy.AS_OF)) == list(range(1, 10))


@pytest.mark.unit
def test_walk_refuses_a_cursor_that_does_not_advance(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger(accounts=3)
    stuck = Page((Account(1, 100, 1),), Cursor(Strategy.AS_OF, Order.ID, 3, (1,)))

    calls: list[None] = []

    def same_page(*_args: object, **_kwargs: object) -> Page:
        calls.append(None)
        if len(calls) > 3:
            raise PageBudgetExceededError  # without the guard the walk would never end
        return stuck

    monkeypatch.setattr(snapshot_pagination, "fetch_page", same_page)
    with pytest.raises(CursorDidNotAdvanceError):
        walk(ledger.store, limit=1, strategy=Strategy.AS_OF)


# --- The HTTP surface ---


def client_for(store: Store) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store)), base_url="http://test"
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_export_keeps_the_total_while_transfers_commit_between_pages() -> None:
    ledger = Ledger(accounts=25)
    total, as_of = sum(ledger.live.values()), ledger.store.clock
    seen: list[dict[str, int]] = []
    tokens: list[str] = []
    rng = random.Random(7)  # noqa: S311 - deterministic test data, not crypto
    async with client_for(ledger.store) as client:
        params = {"page_size": "6"}
        while True:
            response = await client.get("/accounts", params=params)
            assert response.status_code == 200
            body = response.json()
            assert body["as_of"] == as_of
            seen.extend(body["accounts"])
            if not body["next_page_token"]:
                break
            tokens.append(body["next_page_token"])
            src, dst = rng.sample(sorted(ledger.live), 2)
            ledger.transfer(src, dst, 30)
            ledger.insert(999)
            params = {"page_size": "6", "page_token": body["next_page_token"]}
    assert sum(a["balance"] for a in seen) == total
    assert {a["id"]: a["balance"] for a in seen} == ledger.states[as_of]
    assert all(t.startswith(f"{as_of}.") for t in tokens)
    assert sum(ledger.live.values()) == total + 999 * len(tokens)


@pytest.mark.parametrize(
    "token",
    ["10", "10.4.1", "010.4", "10.-4", " 10.4", "1\u0661.4", "10.4' OR 1=1 --", "1" * 20 + ".4"],
)
@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_rejects_malformed_tokens_with_400(token: str) -> None:
    async with client_for(Ledger(accounts=3).store) as client:
        response = await client.get("/accounts", params={"page_token": token})
    assert (response.status_code, response.json()) == (400, {"error": "malformed page_token"})


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_rejects_a_future_snapshot_with_400() -> None:
    ledger = Ledger(accounts=3)
    async with client_for(ledger.store) as client:
        response = await client.get("/accounts", params={"page_token": "4.1"})
    assert response.status_code == 400
    assert response.json() == {"error": "snapshot 4 is in the future"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_expired_snapshot_is_410_with_the_horizon() -> None:
    ledger = Ledger(accounts=10)
    async with client_for(ledger.store) as client:
        first = (await client.get("/accounts", params={"page_size": "4"})).json()
        ledger.update(5, 0)
        ledger.store.gc(ledger.store.clock)
        params = {"page_size": "4", "page_token": first["next_page_token"]}
        response = await client.get("/accounts", params=params)
    assert response.status_code == 410
    assert response.json() == {"error": "snapshot expired", "as_of": 10, "horizon": 11}


@pytest.mark.parametrize(
    ("raw", "status", "count"),
    [
        (None, 200, DEFAULT_PAGE_SIZE),
        ("0", 200, DEFAULT_PAGE_SIZE),
        ("7", 200, 7),
        ("1000", 200, MAX_PAGE_SIZE),
        ("-1", 400, 0),
        ("abc", 400, 0),
        ("9" * 10, 400, 0),
    ],
)
@pytest.mark.asyncio
@pytest.mark.unit
async def test_http_page_size_follows_aip_158(raw: str | None, status: int, count: int) -> None:
    params = {} if raw is None else {"page_size": raw}
    async with client_for(Ledger(accounts=150).store) as client:
        response = await client.get("/accounts", params=params)
    assert response.status_code == status
    if status == 200:
        assert len(response.json()["accounts"]) == count


@pytest.mark.unit
def test_documented_page_size_limits_are_20_and_100() -> None:
    # The note quotes these two numbers; the parametrized test above compares to the constants.
    assert (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE) == (20, 100)
