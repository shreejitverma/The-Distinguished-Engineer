"""Offset/limit pagination: rows read per page, quadratic full walks, and window drift."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns.offset_pagination import (
    COUNTED_KEYSET_SQL,
    COUNTED_OFFSET_SQL,
    MAX_OFFSET,
    OFFSET_SQL,
    SCHEMA,
    ItemStore,
    create_app,
    walk_keyset,
    walk_offset,
)

if TYPE_CHECKING:
    from collections.abc import Callable

N_ROWS = 100_000
LIMIT = 20


def make_store(n: int) -> ItemStore:
    """Rows 1..n with created_at == id, so id n is the newest and sorts first."""
    store = ItemStore()
    store.insert_many((i, i) for i in range(1, n + 1))
    return store


@pytest.fixture(scope="module")
def big_store() -> ItemStore:
    return make_store(N_ROWS)


@pytest.mark.unit
@pytest.mark.parametrize("offset", [0, 1_000, 10_000, 50_000, 99_980])
def test_offset_page_reads_and_discards_every_skipped_row(
    big_store: ItemStore, offset: int
) -> None:
    page = big_store.offset_page(offset, LIMIT)
    assert page.ids == tuple(range(N_ROWS - offset, N_ROWS - offset - LIMIT, -1))
    assert page.rows_visited == offset + LIMIT


@pytest.mark.unit
@pytest.mark.parametrize("offset", [0, 10_000, 50_000, 99_980])
def test_keyset_page_at_the_same_depth_reads_only_the_rows_it_returns(
    big_store: ItemStore, offset: int
) -> None:
    previous = big_store.offset_page(offset - 1, 1).last_key if offset else None
    page = big_store.keyset_page(previous, LIMIT)
    assert page.ids == big_store.offset_page(offset, LIMIT).ids
    assert page.rows_visited == LIMIT


@pytest.mark.unit
def test_vm_instructions_grow_linearly_with_offset(big_store: ItemStore) -> None:
    base = big_store.vm_steps(0, LIMIT)
    per_row = [(big_store.vm_steps(k, LIMIT) - base) / k for k in (1_000, 10_000, 50_000)]
    assert per_row[0] == per_row[1] == per_row[2]
    assert per_row[0] >= 1


@pytest.mark.unit
def test_offset_plan_is_an_index_scan_with_an_offset_counter_in_the_loop(
    big_store: ItemStore,
) -> None:
    scan = ["SCAN items USING COVERING INDEX items_created_at_id"]
    assert big_store.query_plan(OFFSET_SQL, (LIMIT, 50_000)) == scan
    assert big_store.query_plan(COUNTED_OFFSET_SQL, (LIMIT, 50_000)) == scan
    ops = big_store.opcodes(OFFSET_SQL, (LIMIT, 50_000))
    assert "OffsetLimit" in ops
    # IfPos sits between the cursor positioning (Last) and the loop step (Prev).
    assert ops.index("Last") < ops.index("IfPos") < ops.index("Prev")
    keyset_plan = big_store.query_plan(COUNTED_KEYSET_SQL, (50_000, 50_000, LIMIT))
    assert keyset_plan[0].startswith("SEARCH items USING COVERING INDEX items_created_at_id")


@pytest.mark.unit
def test_a_full_offset_walk_reads_quadratically_many_rows_and_keyset_reads_each_once() -> None:
    store = make_store(10_000)
    by_offset = walk_offset(store, 100, lambda _: None)
    by_keyset = walk_keyset(store, 100, lambda _: None)
    assert by_offset.seen == by_keyset.seen == store.all_ids()
    assert by_offset.requests == by_keyset.requests == 101
    # Page k (k = 0..99) reads 100 * (k + 1) rows; the final empty page at offset 10,000 reads all.
    assert by_offset.rows_visited == sum(100 * (k + 1) for k in range(100)) + 10_000 == 515_000
    assert by_keyset.rows_visited == 10_000


@pytest.mark.unit
def test_insert_ahead_of_the_window_repeats_the_last_row_of_the_previous_page() -> None:
    store = make_store(20)
    page1 = store.offset_page(0, 5)
    store.insert_many([(21, 21)])  # newest row: sorts before everything already served
    page2 = store.offset_page(5, 5)
    assert page1.ids == (20, 19, 18, 17, 16)
    assert page2.ids == (16, 15, 14, 13, 12)


@pytest.mark.unit
def test_delete_ahead_of_the_window_skips_the_first_row_of_the_next_page() -> None:
    store = make_store(20)
    page1 = store.offset_page(0, 5)
    store.delete(18)  # a row the client already saw
    page2 = store.offset_page(5, 5)
    assert page1.ids == (20, 19, 18, 17, 16)
    assert page2.ids == (14, 13, 12, 11, 10)  # 15 was never served


@pytest.mark.unit
def test_writes_behind_the_window_do_not_shift_it() -> None:
    store = make_store(20)
    page1 = store.offset_page(0, 5)
    store.insert_many([(100, 0)])  # oldest row: sorts after the next window
    store.delete(1)
    page2 = store.offset_page(5, 5)
    assert page1.ids + page2.ids == tuple(range(20, 10, -1))


@pytest.mark.unit
def test_one_insert_per_page_duplicates_one_row_per_insert_under_offset_but_not_keyset() -> None:
    def walk(walker: str) -> tuple[tuple[int, ...], tuple[int, ...], int]:
        store = make_store(100)
        inserted: list[int] = []

        def insert_newest(n: int) -> None:
            inserted.append(1_000 + n)
            store.insert_many([(1_000 + n, 1_000 + n)])

        fn = walk_offset if walker == "offset" else walk_keyset
        result = fn(store, 10, insert_newest)
        missing = tuple(sorted(set(range(1, 101)) - set(result.seen)))
        return result.duplicates, missing, len(inserted)

    duplicates, missing, inserts = walk("offset")
    assert inserts == 11
    assert len(duplicates) == inserts
    assert missing == ()
    assert walk("keyset") == ((), (), 10)


@pytest.mark.unit
def test_one_delete_per_page_skips_one_surviving_row_per_delete_under_offset_but_not_keyset() -> (
    None
):
    def walk(walker: str) -> tuple[tuple[int, ...], int]:
        store = make_store(100)
        deleted: list[int] = []

        def delete_newest(_: int) -> None:
            newest = store.all_ids()[0]  # always a row already served
            deleted.append(newest)
            store.delete(newest)

        fn = walk_offset if walker == "offset" else walk_keyset
        result = fn(store, 10, delete_newest)
        survivors = set(range(1, 101)) - set(deleted)
        assert result.duplicates == ()
        return tuple(sorted(survivors - set(result.seen))), len(deleted)

    skipped, deletes = walk("offset")
    assert deletes == 9
    assert len(skipped) == deletes
    assert walk("keyset") == ((), 10)


@pytest.mark.unit
def test_ties_without_a_tie_breaker_are_not_a_partition_when_the_plan_changes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL)")
    conn.execute("CREATE INDEX t_created_at ON t (created_at)")
    conn.executemany("INSERT INTO t VALUES (?, 100)", [(i,) for i in range(1, 9)])

    def page(hint: str, order_by: str, offset: int) -> list[int]:
        sql = f"SELECT id FROM t {hint} ORDER BY {order_by} LIMIT 4 OFFSET ?"  # noqa: S608
        return [row[0] for row in conn.execute(sql, (offset,))]

    # Page 1 served by the index, page 2 by a sort after the planner changed its mind.
    loose = page("INDEXED BY t_created_at", "created_at DESC", 0) + page(
        "NOT INDEXED", "created_at DESC", 4
    )
    # Rows 5..8 are served twice and rows 1..4 never (SQLite 3.53.1 and 3.54.1 tie orders).
    assert loose == [8, 7, 6, 5, 5, 6, 7, 8]
    total = "created_at DESC, id DESC"
    strict = page("INDEXED BY t_created_at", total, 0) + page("NOT INDEXED", total, 4)
    assert strict == list(range(8, 0, -1))


@pytest.mark.unit
def test_the_lab_offset_queries_end_in_a_unique_key_so_index_and_sort_plans_agree() -> None:
    rows = [(i, 100) for i in range(1, 9)]  # eight rows tied on created_at
    indexed = make_store(0)
    indexed.insert_many(rows)
    by_index = indexed.offset_page(0, 4).ids + indexed.offset_page(4, 4).ids

    def visit(_item_id: int) -> int:
        return 1

    unindexed = sqlite3.connect(":memory:")
    unindexed.create_function("visit", 1, visit, deterministic=False)
    unindexed.execute(SCHEMA[0])  # the table without its index, so the planner must sort
    unindexed.executemany("INSERT INTO items VALUES (?, ?)", rows)
    for sql in (OFFSET_SQL, COUNTED_OFFSET_SQL):
        plan = [row[3] for row in unindexed.execute(f"EXPLAIN QUERY PLAN {sql}", (4, 0))]
        assert "USE TEMP B-TREE FOR ORDER BY" in plan
        by_sort = tuple(row[0] for off in (0, 4) for row in unindexed.execute(sql, (4, off)))
        assert by_sort == by_index == tuple(range(8, 0, -1))
    unindexed.close()


@pytest.mark.unit
def test_sqlite_treats_negative_limit_as_unbounded_and_negative_offset_as_zero() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, created_at INTEGER)")
    conn.executemany("INSERT INTO items VALUES (?, ?)", ((i, i) for i in range(1, 501)))
    # The query the API runs, with limit=-1 and offset=-5 passed straight through.
    rows: list[tuple[int, int]] = conn.execute(OFFSET_SQL, (-1, -5)).fetchall()
    conn.close()
    assert len(rows) == 500
    assert rows[0] == (500, 500)


def client_for(store: ItemStore) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store)), base_url="http://test"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_returns_a_next_link_and_drifts_under_an_insert_between_requests() -> None:
    store = make_store(50)
    async with client_for(store) as client:
        first = await client.get("/items", params={"limit": 10})
        assert first.status_code == 200
        assert first.headers["link"] == '</items?offset=10&limit=10>; rel="next"'
        assert first.json() == {"items": list(range(50, 40, -1)), "next_offset": 10}
        store.insert_many([(51, 51)])
        second = await client.get("/items", params={"offset": 10, "limit": 10})
    assert second.json()["items"][0] == first.json()["items"][-1] == 41


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [{"limit": -1}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": MAX_OFFSET + 1}],
    ids=["limit-negative", "limit-zero", "limit-over-max", "offset-negative", "offset-over-max"],
)
async def test_api_rejects_out_of_range_parameters_before_they_reach_sql(
    params: dict[str, int],
) -> None:
    store = make_store(200)
    async with client_for(store) as client:
        response = await client.get("/items", params=params)
    assert response.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_stops_advertising_next_at_the_offset_cap_and_at_the_end() -> None:
    store = make_store(MAX_OFFSET + 500)
    async with client_for(store) as client:
        capped = await client.get("/items", params={"offset": MAX_OFFSET - 50, "limit": 100})
        end = await client.get("/items", params={"offset": 0, "limit": 100})
        last = await client.get("/items", params={"offset": MAX_OFFSET, "limit": 100})
        edge = await client.get("/items", params={"offset": MAX_OFFSET - 100, "limit": 100})
    assert edge.json()["next_offset"] == MAX_OFFSET
    assert f"offset={MAX_OFFSET}&" in edge.headers["link"]
    assert len(capped.json()["items"]) == 100
    assert capped.json()["next_offset"] is None
    assert "link" not in capped.headers
    assert end.json()["next_offset"] == 100
    assert last.json()["next_offset"] is None


@pytest.mark.unit
def test_a_writer_inserting_a_page_per_read_livelocks_an_offset_walk_but_not_keyset() -> None:
    def insert_ten_newest(store: ItemStore) -> Callable[[int], None]:
        def hook(n: int) -> None:
            store.insert_many((1_000 * n + i, 1_000 * n + i) for i in range(10))

        return hook

    store = make_store(30)
    with pytest.raises(RuntimeError, match="did not terminate after 50 requests"):
        walk_offset(store, 10, insert_ten_newest(store), max_requests=50)
    store = make_store(30)
    result = walk_keyset(store, 10, insert_ten_newest(store), max_requests=50)
    assert result.seen == tuple(range(30, 0, -1))
    assert result.requests == 4
