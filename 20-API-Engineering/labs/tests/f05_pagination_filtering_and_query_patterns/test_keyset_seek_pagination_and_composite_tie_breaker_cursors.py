"""Lab: keyset (seek) pagination with a (created_at, id) row-value cursor."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns.keyset import (
    DEFAULT_PAGE_SIZE,
    INDEX_DDL,
    MAX_PAGE_SIZE,
    ROWID_ALIAS_TABLE_DDL,
    TABLE_DDL,
    CursorDidNotAdvanceError,
    Event,
    Predicate,
    SeekKey,
    bind,
    create_app,
    decode_cursor,
    encode_cursor,
    fetch_page,
    insert_events,
    offset_page,
    open_store,
    page_sql,
    query_plan,
    vm_steps,
    walk,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

BASE_US = 1_794_268_800_000_000  # 2026-11-10T00:00:00Z in microseconds
TIE_SIZE = 7


def tied_events(n: int, *, tie_size: int = TIE_SIZE) -> list[Event]:
    """n events where every run of *tie_size* consecutive ids shares one timestamp."""
    return [Event(i, BASE_US + (i // tie_size) * 1_000_000, f"e{i}") for i in range(1, n + 1)]


def desc_ids(events: list[Event]) -> list[int]:
    return [e.id for e in sorted(events, key=lambda e: (e.created_at, e.id), reverse=True)]


def reference_walk(
    events: list[Event],
    limit: int,
    keep: Callable[[Event, SeekKey], bool],
    cursor_of: Callable[[SeekKey], SeekKey] = lambda key: key,
) -> list[int]:
    """Pure-Python model of a cursor walk: filter the sorted list, cut a page, repeat."""
    ordered = sorted(events, key=lambda e: e.key, reverse=True)
    served: list[int] = []
    after: SeekKey | None = None
    while True:
        rows = [e for e in ordered if after is None or keep(e, after)]
        served.extend(e.id for e in rows[:limit])
        if len(rows) <= limit:
            return served
        after = cursor_of(rows[limit - 1].key)


_OPEN: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_stores() -> Iterator[None]:
    """Close every connection a test opened; CPython 3.13+ warns on unclosed ones."""
    yield
    while _OPEN:
        _OPEN.pop().close()


def db(table_ddl: str = TABLE_DDL, index_ddl: str | None = INDEX_DDL) -> sqlite3.Connection:
    conn = open_store(table_ddl, index_ddl)
    _OPEN.append(conn)
    return conn


def store_with(events: list[Event], table_ddl: str = TABLE_DDL) -> sqlite3.Connection:
    conn = db(table_ddl)
    insert_events(conn, events)
    return conn


# --- correctness ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("limit", [1, 3, 5, TIE_SIZE, 50, 100])
def test_row_value_walk_serves_every_row_exactly_once_across_tie_groups(limit: int) -> None:
    events = tied_events(50)
    assert walk(store_with(events), limit=limit) == desc_ids(events)


@pytest.mark.unit
def test_timestamp_only_lt_cursor_skips_the_rest_of_each_tie_group() -> None:
    events = tied_events(50)
    served = walk(store_with(events), limit=5, predicate=Predicate.TIMESTAMP_LT)
    assert served == reference_walk(events, 5, lambda e, a: e.created_at < a.created_at)
    assert len(served) == len(set(served))
    missing = set(desc_ids(events)) - set(served)
    assert len(missing) == 15
    # Every lost row shares its timestamp with a row that ended a page.
    by_id = {e.id: e for e in events}
    stamps = {by_id[i].created_at for i in served}
    assert all(by_id[i].created_at in stamps for i in missing)


@pytest.mark.unit
def test_timestamp_only_le_cursor_never_advances_inside_a_tie_group() -> None:
    conn = store_with(tied_events(50))
    budget = [1_000_000]

    def abort_when_spent() -> int:
        # Without the advance guard the walk loops forever; abort SQLite instead of hanging.
        budget[0] -= 1
        return int(budget[0] < 0)

    conn.set_progress_handler(abort_when_spent, 1)
    with pytest.raises(CursorDidNotAdvanceError):
        walk(conn, limit=TIE_SIZE - 1, predicate=Predicate.TIMESTAMP_LE)


@pytest.mark.unit
@pytest.mark.parametrize("seed", range(20))
def test_expanded_and_bounded_or_forms_match_the_row_value_walk(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not crypto
    n = rng.randint(1, 120)
    ids = rng.sample(range(1, 10_000), n)
    events = [Event(i, BASE_US + rng.randint(0, 9), f"e{i}") for i in ids]
    conn = store_with(events)
    limit = rng.randint(1, 12)
    expected = desc_ids(events)
    for predicate in (Predicate.ROW_VALUE, Predicate.EXPANDED_OR, Predicate.BOUNDED_OR):
        assert walk(conn, limit=limit, predicate=predicate) == expected, predicate


@pytest.mark.unit
def test_page_reads_one_extra_row_so_the_last_full_page_reports_the_end() -> None:
    conn = store_with(tied_events(14))
    first = fetch_page(conn, after=None, limit=7)
    assert first.next_key == first.items[-1].key
    second = fetch_page(conn, after=first.next_key, limit=7)
    assert [e.id for e in second.items] == [7, 6, 5, 4, 3, 2, 1]
    assert second.next_key is None
    with pytest.raises(ValueError, match="limit"):
        fetch_page(conn, after=None, limit=0)


@pytest.mark.unit
def test_null_sort_key_rows_are_never_served_by_the_row_value_seek() -> None:
    conn = db(TABLE_DDL.replace("created_at INTEGER NOT NULL", "created_at INTEGER"))
    conn.executemany(
        "INSERT INTO events VALUES (?, ?, ?)",
        [(i, None if i in {2, 5} else BASE_US + i, f"e{i}") for i in range(1, 11)],
    )
    served = walk(conn, limit=3)
    # DESC puts NULLs last, and (NULL, id) < (t, id) is NULL, so no page ever reaches them.
    assert served == [10, 9, 8, 7, 6, 4, 3, 1]


@pytest.mark.unit
def test_millisecond_truncated_cursor_skips_rows_inside_the_truncated_interval() -> None:
    # Four rows per millisecond, distinct only at microsecond precision.
    events = [Event(i, BASE_US + i * 250, f"e{i}") for i in range(1, 41)]

    def to_millis(key: SeekKey) -> SeekKey:
        # A client that keeps the cursor timestamp as a JavaScript Date keeps milliseconds.
        return SeekKey(key.created_at // 1000 * 1000, key.id)

    conn = store_with(events)
    served: list[int] = []
    after: SeekKey | None = None
    while True:
        page = fetch_page(conn, after=after, limit=6)
        served.extend(e.id for e in page.items)
        if page.next_key is None:
            break
        after = to_millis(page.next_key)
    assert served == reference_walk(events, 6, lambda e, a: e.key < a, to_millis)
    assert len(served) == len(set(served))
    # Page 1 ends at id 35 (+8750 us); the cursor becomes +8000 us, so ids 33 and 34
    # (+8250, +8500) sort between the cursor and the last row served and are never seen.
    assert sorted(set(desc_ids(events)) - set(served)) == [1, 2, 9, 10, 17, 18, 25, 26, 33, 34]


@pytest.mark.unit
def test_keyset_walk_is_stable_under_head_inserts_while_offset_repeats_rows() -> None:
    original = tied_events(30)
    newer = [Event(1000 + k, BASE_US + 10**9 + k, f"new{k}") for k in range(5)]

    keyset_conn = store_with(original)
    first = fetch_page(keyset_conn, after=None, limit=10)
    insert_events(keyset_conn, newer)
    served = [e.id for e in first.items]
    after = first.next_key
    while after is not None:
        page = fetch_page(keyset_conn, after=after, limit=10)
        served.extend(e.id for e in page.items)
        after = page.next_key
    assert served == desc_ids(original)

    offset_conn = store_with(original)
    offset_served = [e.id for e in offset_page(offset_conn, offset=0, limit=10)]
    insert_events(offset_conn, newer)
    for offset in (10, 20, 30):
        offset_served.extend(e.id for e in offset_page(offset_conn, offset=offset, limit=10))
    assert len(offset_served) - len(set(offset_served)) == 5


# --- plans ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_composite_index_serves_the_row_value_seek_with_no_sort_step() -> None:
    plan = query_plan(db(), page_sql(Predicate.ROW_VALUE), (0, 0, 21))
    assert plan == ("SEARCH events USING INDEX ix_events_created_id ((created_at,id)<(?,?))",)


@pytest.mark.unit
def test_without_the_index_sqlite_scans_the_table_and_sorts_it() -> None:
    plan = query_plan(db(index_ddl=None), page_sql(Predicate.ROW_VALUE), (0, 0, 21))
    assert plan == ("SCAN events", "USE TEMP B-TREE FOR ORDER BY")


@pytest.mark.unit
def test_expanded_or_form_scans_the_index_instead_of_seeking_into_it() -> None:
    plan = query_plan(db(), page_sql(Predicate.EXPANDED_OR), (0, 0, 0, 21))
    assert plan == ("SCAN events USING INDEX ix_events_created_id",)


@pytest.mark.unit
def test_mixed_direction_order_by_cannot_ride_the_index_order() -> None:
    sql = page_sql(Predicate.ROW_VALUE).replace("id DESC", "id ASC")
    plan = query_plan(db(), sql, (0, 0, 21))
    assert plan[-1] == "USE TEMP B-TREE FOR LAST TERM OF ORDER BY"


# --- cost, in SQLite virtual machine instructions -------------------------------------------

N_COST = 8000
TIE_LO, TIE_HI = 2000, 6000  # ids TIE_LO+1 .. TIE_HI share one timestamp


def cost_events() -> list[Event]:
    def ts(i: int) -> int:
        return BASE_US + (TIE_LO + 1 if TIE_LO < i <= TIE_HI else i) * 1_000_000

    return [Event(i, ts(i), f"e{i}") for i in range(1, N_COST + 1)]


def page_cost(conn: sqlite3.Connection, predicate: Predicate, after: SeekKey) -> int:
    return vm_steps(conn, page_sql(predicate), (*bind(predicate, after), 21))


SHALLOW = SeekKey(BASE_US + 7900 * 1_000_000, 7900)  # 100 rows into the DESC walk
DEEP = SeekKey(BASE_US + 100 * 1_000_000, 100)  # 7900 rows into the DESC walk
TIE_T = BASE_US + (TIE_LO + 1) * 1_000_000
TIE_SHALLOW = SeekKey(TIE_T, TIE_HI - 10)  # 10 rows into the tie group
TIE_DEEP = SeekKey(TIE_T, TIE_LO + 10)  # 3990 rows into the tie group


@pytest.mark.unit
def test_row_value_page_cost_is_flat_while_offset_cost_grows_with_depth() -> None:
    conn = store_with(cost_events())
    seek_shallow = page_cost(conn, Predicate.ROW_VALUE, SHALLOW)
    seek_deep = page_cost(conn, Predicate.ROW_VALUE, DEEP)
    assert seek_deep <= seek_shallow * 1.2
    offset_shallow = vm_steps(conn, page_sql(None) + " OFFSET ?", (21, 100))
    offset_deep = vm_steps(conn, page_sql(None) + " OFFSET ?", (21, 7900))
    assert offset_deep >= offset_shallow * 20
    assert offset_deep >= seek_deep * 50


@pytest.mark.unit
def test_expanded_or_page_cost_grows_with_the_rows_already_served() -> None:
    conn = store_with(cost_events())
    shallow = page_cost(conn, Predicate.EXPANDED_OR, SHALLOW)
    deep = page_cost(conn, Predicate.EXPANDED_OR, DEEP)
    # The scan starts at the top of the index and filters every row the client has seen.
    assert deep >= shallow * 20
    assert deep >= page_cost(conn, Predicate.ROW_VALUE, DEEP) * 50


@pytest.mark.unit
def test_bounded_or_pays_for_the_tie_group_rows_ahead_of_the_cursor() -> None:
    conn = store_with(cost_events())
    assert page_cost(conn, Predicate.BOUNDED_OR, TIE_DEEP) >= 20 * page_cost(
        conn, Predicate.BOUNDED_OR, TIE_SHALLOW
    )
    assert page_cost(conn, Predicate.ROW_VALUE, TIE_DEEP) <= 1.2 * page_cost(
        conn, Predicate.ROW_VALUE, TIE_SHALLOW
    )


@pytest.mark.unit
def test_rowid_alias_tie_breaker_loses_the_tight_seek_inside_a_tie_group() -> None:
    conn = store_with(cost_events(), ROWID_ALIAS_TABLE_DDL)  # same (created_at, id) index
    plan = query_plan(conn, page_sql(Predicate.ROW_VALUE), (0, 0, 21))
    assert "(created_at,id)<(?,?)" not in plan[0]
    assert page_cost(conn, Predicate.ROW_VALUE, TIE_DEEP) >= 20 * page_cost(
        conn, Predicate.ROW_VALUE, TIE_SHALLOW
    )
    # Same rows, same index, WITHOUT ROWID table: the seek is tight again.
    tight = store_with(cost_events())
    assert page_cost(tight, Predicate.ROW_VALUE, TIE_DEEP) <= 1.2 * page_cost(
        tight, Predicate.ROW_VALUE, TIE_SHALLOW
    )


# --- cursor codec and HTTP surface ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [SeekKey(0, 0), SeekKey(BASE_US, 42), SeekKey(2**63 - 1, 2**63 - 1)],
    ids=["zero", "typical", "int64-max"],
)
def test_cursor_round_trips(key: SeekKey) -> None:
    assert decode_cursor(encode_cursor(key)) == key


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "1.2.3",
        "01.2",
        "-1.2",
        "1.",
        ".2",
        "1.2 ",
        "9223372036854775808.1",
        "1e3.2",
        "\u0661.2",
        "1\u0661.2",  # int() accepts Arabic-Indic digits, so the regex must not
        "1.2\uff11",  # fullwidth digit one
    ],
)
def test_malformed_cursor_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="cursor"):
        decode_cursor(raw)


def client_for(conn: sqlite3.Connection) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(conn)), base_url="http://test"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_walk_follows_next_page_token_until_it_is_empty() -> None:
    events = tied_events(45)
    served: list[int] = []
    pages = 0
    async with client_for(store_with(events)) as client:
        token = ""
        for _ in range(10):  # 45 rows at 10 per page need 5 pages; never loop unbounded
            resp = await client.get("/events", params={"page_size": "10", "page_token": token})
            assert resp.status_code == 200
            body = resp.json()
            served.extend(e["id"] for e in body["events"])
            pages += 1
            token = body["next_page_token"]
            if token == "":
                break
        else:
            pytest.fail("next_page_token never became empty")
    assert served == desc_ids(events)
    assert pages == 5


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_size", "status", "count"),
    [
        (None, 200, 20),
        ("0", 200, 20),
        ("7", 200, 7),
        ("100", 200, 100),
        ("101", 200, 100),
        ("100000", 200, 100),
        ("-1", 400, None),
        ("ten", 400, None),
    ],
    ids=[
        "absent",
        "zero",
        "explicit",
        "at-max",
        "one-over-max",
        "coerced-down",
        "negative",
        "not-a-number",
    ],
)
async def test_page_size_follows_aip_158_rules(
    page_size: str | None, status: int, count: int | None
) -> None:
    params = {} if page_size is None else {"page_size": page_size}
    async with client_for(store_with(tied_events(150))) as client:
        resp = await client.get("/events", params=params)
    assert resp.status_code == status
    if count is not None:
        assert len(resp.json()["events"]) == count


@pytest.mark.unit
def test_page_size_constants_match_the_values_the_note_documents() -> None:
    assert (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE) == (20, 100)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_page_token_is_a_400_not_a_500() -> None:
    async with client_for(store_with(tied_events(5))) as client:
        resp = await client.get("/events", params={"page_token": "1.2' OR 1=1 --"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "malformed cursor"}
