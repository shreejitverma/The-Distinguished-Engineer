"""Lab: Bidirectional keyset pagination and Relay cursor connections."""

from __future__ import annotations

import base64
import itertools
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns.relay_connection import (
    EXISTS_AT_OR_NEWER,
    EXISTS_AT_OR_OLDER,
    MAX_PAGE_SIZE,
    NAIVE_BEFORE_SQL,
    CountingSource,
    Event,
    EventNode,
    Key,
    PaginationError,
    Window,
    build_offset_schema,
    connect,
    create_app,
    decode_cursor,
    encode_cursor,
    fetch_connection,
    page_sql,
    parse_window,
    query_plan,
    vm_steps,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

    import strawberry


type Db = Callable[[list[Event]], sqlite3.Connection]


@pytest.fixture
def db() -> Iterator[Db]:
    """``connect`` that closes every connection it opened when the test ends."""
    opened: list[sqlite3.Connection] = []

    def make(rows: list[Event]) -> sqlite3.Connection:
        conn = connect(rows)
        opened.append(conn)
        return conn

    yield make
    for conn in opened:
        conn.close()


def events(n: int) -> list[Event]:
    """``n`` rows in tie groups of three: ``created_at = id // 3``."""
    return [Event(created_at=i // 3, id=i, title=f"e{i}") for i in range(1, n + 1)]


def newest_first(rows: list[Event]) -> list[Key]:
    return sorted((e.key for e in rows), reverse=True)


def keys(conn: sqlite3.Connection, window: Window) -> list[Key]:
    return [e.key for e in fetch_connection(conn, window).nodes]


# --- A literal transcription of the GraphQL Cursor Connections algorithm (Sections 4.4, 5.1). ---


def spec_apply_cursors(all_edges: list[Key], *, after: Key | None, before: Key | None) -> list[Key]:
    edges = list(all_edges)
    if after is not None and after in edges:
        edges = edges[edges.index(after) + 1 :]
    if before is not None and before in edges:
        edges = edges[: edges.index(before)]
    return edges


def spec_edges_to_return(all_edges: list[Key], w: Window) -> list[Key]:
    edges = spec_apply_cursors(all_edges, after=w.after, before=w.before)
    if w.first is not None and len(edges) > w.first:
        edges = edges[: w.first]
    if w.last is not None and len(edges) > w.last:
        edges = edges[len(edges) - w.last :]  # not edges[-last:], which is everything for 0
    return edges


# --- Cursor codec ---

URL_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


@pytest.mark.unit
def test_cursor_round_trips_and_uses_only_url_safe_characters() -> None:
    for key in [(0, 0), (1, 23), (2**63 - 1, 2**63 - 1)]:
        cursor = encode_cursor(key)
        assert decode_cursor(cursor) == key
        assert set(cursor) <= set(URL_SAFE)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _same_bytes_other_spelling(cursor: str) -> str:
    """A different final character that decodes to the same bytes (unused low bits set)."""
    pad = "=" * (-len(cursor) % 4)
    want = base64.urlsafe_b64decode(cursor + pad)
    for c in URL_SAFE:
        alt = cursor[:-1] + c
        if c != cursor[-1] and base64.urlsafe_b64decode(alt + pad) == want:
            return alt
    raise AssertionError("no alternate spelling")


@pytest.mark.unit
@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("", id="empty"),
        pytest.param("A" * 65, id="too-long"),
        pytest.param(chr(0xE9) + _b64("v1:1:2"), id="non-ascii"),
        pytest.param(" " + _b64("v1:1:2"), id="leading-space"),
        pytest.param(_b64("v1:1:23") + "==", id="padding"),
        pytest.param(_same_bytes_other_spelling(_b64("v1:1:23")), id="non-canonical-bits"),
        pytest.param(_b64("v1:01:2"), id="leading-zero"),
        pytest.param(_b64("v1:-1:2"), id="negative"),
        pytest.param(_b64("v1:9223372036854775808:1"), id="int64-overflow"),
        pytest.param(_b64("v1:1:2:3"), id="extra-field"),
        pytest.param(_b64("v2:1:2"), id="unknown-version"),
        pytest.param(_b64("v1:1:" + chr(0x661)), id="arabic-indic-digit"),
        pytest.param("YXJyYXljb25uZWN0aW9uOjA", id="arrayconnection-offset"),
    ],
)
def test_cursor_decoder_rejects_everything_it_did_not_mint(cursor: str) -> None:
    with pytest.raises(PaginationError, match="malformed cursor"):
        decode_cursor(cursor)


class _DecodeReachedError(Exception):
    pass


@pytest.mark.unit
def test_oversized_cursor_is_rejected_before_any_base64_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> bytes:
        raise _DecodeReachedError

    monkeypatch.setattr(base64, "b64decode", boom)
    with pytest.raises(PaginationError, match="malformed cursor"):
        decode_cursor("A" * 1_000_000)  # the length cap answers without decoding
    with pytest.raises(PaginationError, match="malformed cursor"):
        decode_cursor("A" * 65)  # one character past the cap is still answered without decoding
    with pytest.raises(_DecodeReachedError):
        decode_cursor("A" * 64)  # 64 characters is still inside the cap


# --- Argument validation ---


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "message"),
    [
        pytest.param({"first": "1", "last": "1"}, "exactly one", id="first-and-last"),
        pytest.param({"first": "-1"}, "non-negative", id="negative-first"),
        pytest.param({"last": "1.5"}, "non-negative", id="fractional-last"),
        pytest.param({"first": "007"}, "non-negative", id="leading-zero"),
        pytest.param({"first": str(MAX_PAGE_SIZE + 1)}, "between 0 and 100", id="over-max"),
        pytest.param({"last": "99999"}, "between 0 and 100", id="five-digits"),
        pytest.param({"first": "100000000"}, "between 0 and 100", id="nine-digits"),
        pytest.param({"first": "1000000000"}, "non-negative", id="ten-digits-fail-the-grammar"),
        pytest.param({"last": "9" * 5000}, "non-negative", id="5000-digits-never-reach-int"),
    ],
)
def test_window_rejects_ambiguous_negative_and_oversized_counts(
    args: dict[str, str], message: str
) -> None:
    with pytest.raises(PaginationError, match=message):
        parse_window(**args)


@pytest.mark.unit
def test_no_first_and_no_last_means_first_twenty() -> None:
    assert parse_window() == Window(first=20)
    assert parse_window(last="0") == Window(last=0)


# --- The keyset connection against the spec reference ---


@pytest.mark.unit
def test_every_window_matches_the_spec_algorithm_and_page_info_rules(db: Db) -> None:
    rows = events(9)
    conn = db(rows)
    all_edges = newest_first(rows)
    cursors: list[Key | None] = [None, *all_edges]
    checked = 0
    for after, before in itertools.product(cursors, cursors):
        if (
            after is not None
            and before is not None
            and all_edges.index(after) >= all_edges.index(before)
        ):
            continue  # crossed cursors: see the dedicated test below
        for size in range(len(all_edges) + 2):
            for w in (
                Window(first=size, after=after, before=before),
                Window(last=size, after=after, before=before),
            ):
                got = fetch_connection(conn, w)
                want = spec_edges_to_return(all_edges, w)
                assert [e.key for e in got.nodes] == want, w
                in_range = spec_apply_cursors(all_edges, after=after, before=before)
                info = got.page_info
                if w.backward:  # "must" for last, "may" (here: exact probe) for before
                    assert info.has_previous_page is (len(in_range) > size), w
                    assert info.has_next_page is (before is not None), w
                else:  # "must" for first, "may" (here: exact probe) for after
                    assert info.has_next_page is (len(in_range) > size), w
                    assert info.has_previous_page is (after is not None), w
                assert info.start_cursor == (encode_cursor(want[0]) if want else None)
                assert info.end_cursor == (encode_cursor(want[-1]) if want else None)
                checked += 1
    assert checked == 1210


@pytest.mark.unit
def test_backward_page_keeps_collection_order_with_the_nearest_edge_last(db: Db) -> None:
    conn = db(events(12))
    all_edges = newest_first(events(12))
    page = keys(conn, Window(last=4, before=all_edges[8]))
    assert page == all_edges[4:8]
    assert page[-1] == all_edges[7]  # the edge closest to before comes last (Section 4.3)


@pytest.mark.unit
def test_flipping_only_the_comparison_returns_the_first_page_not_the_previous_one(db: Db) -> None:
    conn = db(events(12))
    all_edges = newest_first(events(12))
    before = all_edges[8]
    naive = [(r[0], r[1]) for r in conn.execute(NAIVE_BEFORE_SQL, (*before, 5)).fetchall()]
    assert naive[:4] == all_edges[0:4]
    assert keys(conn, Window(last=4, before=before)) == all_edges[4:8]


@pytest.mark.unit
def test_forward_and_backward_walks_each_serve_every_row_exactly_once(db: Db) -> None:
    rows = events(14)  # tie groups of three, 14 is not a multiple of the page size
    conn = db(rows)
    forward: list[Key] = []
    window = Window(first=4)
    while True:
        page = fetch_connection(conn, window)
        forward.extend(e.key for e in page.nodes)
        if not page.page_info.has_next_page:
            break
        window = Window(first=4, after=decode_cursor(cast("str", page.page_info.end_cursor)))
    backward: list[Key] = []
    window = Window(last=4)
    while True:
        page = fetch_connection(conn, window)
        backward[:0] = [e.key for e in page.nodes]
        if not page.page_info.has_previous_page:
            break
        window = Window(last=4, before=decode_cursor(cast("str", page.page_info.start_cursor)))
    assert forward == backward == newest_first(rows)


@pytest.mark.unit
def test_walks_stay_correct_when_neither_index_nor_storage_supplies_the_tie_order(db: Db) -> None:
    rows = events(14)
    conn = db(rows)
    # A rowid heap filled in a scrambled id order, with no index: only ORDER BY orders ties.
    conn.executescript(
        "CREATE TABLE heap AS SELECT * FROM events ORDER BY (id * 7) % 15;"
        "DROP TABLE events;"
        "ALTER TABLE heap RENAME TO events;"
    )
    all_edges = newest_first(rows)
    for after in all_edges:
        assert keys(conn, Window(first=4, after=after)) == spec_edges_to_return(
            all_edges, Window(first=4, after=after)
        )
        assert keys(conn, Window(last=4, before=after)) == spec_edges_to_return(
            all_edges, Window(last=4, before=after)
        )


@pytest.mark.unit
def test_one_edge_cursor_works_as_after_and_as_before(db: Db) -> None:
    conn = db(events(12))
    all_edges = newest_first(events(12))
    middle = all_edges[5]
    assert keys(conn, Window(first=3, after=middle)) == all_edges[6:9]
    assert keys(conn, Window(last=3, before=middle)) == all_edges[2:5]


@pytest.mark.unit
def test_cursor_of_a_deleted_row_still_names_its_position(db: Db) -> None:
    rows = events(12)
    conn = db(rows)
    all_edges = newest_first(rows)
    page1 = fetch_connection(conn, Window(first=4))
    after = decode_cursor(cast("str", page1.page_info.end_cursor))
    conn.execute("DELETE FROM events WHERE created_at = ? AND id = ?", after)
    remaining = [k for k in all_edges if k != after]
    assert keys(conn, Window(first=4, after=after)) == all_edges[4:8]
    # A literal ApplyCursorsToEdges finds no afterEdge, keeps every edge, and restarts.
    assert spec_edges_to_return(remaining, Window(first=4, after=after)) == remaining[:4]


@pytest.mark.unit
def test_crossed_cursors_name_an_empty_range_where_the_literal_spec_ignores_before(db: Db) -> None:
    conn = db(events(12))
    all_edges = newest_first(events(12))
    w = Window(first=3, after=all_edges[5], before=all_edges[2])
    assert keys(conn, w) == []
    assert spec_edges_to_return(all_edges, w) == all_edges[6:9]


@pytest.mark.unit
def test_has_previous_page_on_a_forward_page_is_an_existence_probe(db: Db) -> None:
    rows = events(12)
    conn = db(rows)
    page1 = fetch_connection(conn, Window(first=4))
    after = decode_cursor(cast("str", page1.page_info.end_cursor))
    assert fetch_connection(conn, Window(first=4, after=after)).page_info.has_previous_page
    conn.execute("DELETE FROM events WHERE (created_at, id) >= (?, ?)", after)
    page2 = fetch_connection(conn, Window(first=4, after=after))
    assert len(page2.nodes) == 4
    assert page2.page_info.has_previous_page is False  # nothing left at or before the cursor


@pytest.mark.unit
def test_has_next_page_on_a_backward_page_is_an_existence_probe(db: Db) -> None:
    rows = events(12)
    conn = db(rows)
    all_edges = newest_first(rows)
    before = all_edges[8]
    assert fetch_connection(conn, Window(last=4, before=before)).page_info.has_next_page
    conn.execute("DELETE FROM events WHERE (created_at, id) <= (?, ?)", before)
    page = fetch_connection(conn, Window(last=4, before=before))
    assert [e.key for e in page.nodes] == all_edges[4:8]
    assert page.page_info.has_next_page is False


@pytest.mark.unit
def test_first_zero_and_last_zero_return_no_edges_but_still_report_more(db: Db) -> None:
    conn = db(events(5))
    forward = fetch_connection(conn, Window(first=0))
    backward = fetch_connection(conn, Window(last=0))
    assert forward.nodes == backward.nodes == ()
    assert forward.page_info.has_next_page is True
    assert backward.page_info.has_previous_page is True
    assert forward.page_info.start_cursor is None


# --- Plans and cost ---


@pytest.mark.unit
@pytest.mark.parametrize(
    ("window", "detail"),
    [
        pytest.param(
            Window(first=20, after=(5, 5)),
            "SEARCH events USING INDEX ix_events_created_id ((created_at,id)<(?,?))",
            id="first-after",
        ),
        pytest.param(
            Window(last=20, before=(5, 5)),
            "SEARCH events USING INDEX ix_events_created_id ((created_at,id)>(?,?))",
            id="last-before",
        ),
        pytest.param(
            Window(last=20),
            "SCAN events USING INDEX ix_events_created_id",
            id="last-only",
        ),
        pytest.param(
            Window(last=20, after=(9, 9), before=(5, 5)),
            "SEARCH events USING INDEX ix_events_created_id "
            "((created_at,id)>(?,?) AND (created_at,id)<(?,?))",
            id="last-both-cursors",
        ),
    ],
)
def test_both_directions_ride_one_index_with_no_sort_step(
    window: Window, detail: str, db: Db
) -> None:
    conn = db(events(300))
    sql, params = page_sql(window)
    assert query_plan(conn, sql, params) == [detail]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sql", "detail"),
    [
        pytest.param(
            EXISTS_AT_OR_NEWER,
            "SEARCH events USING COVERING INDEX ix_events_created_id ((created_at,id)>(?,?))",
            id="has-previous-probe",
        ),
        pytest.param(
            EXISTS_AT_OR_OLDER,
            "SEARCH events USING COVERING INDEX ix_events_created_id ((created_at,id)<(?,?))",
            id="has-next-probe",
        ),
    ],
)
def test_existence_probes_are_covering_index_searches_whose_cost_ignores_table_size(
    sql: str, detail: str, db: Db
) -> None:
    small = db(events(1000))
    large = db(events(16000))
    assert query_plan(large, sql, (5, 5)) == [detail]
    cost_small = vm_steps(small, lambda: small.execute(sql, (100, 300)).fetchone())
    cost_large = vm_steps(large, lambda: large.execute(sql, (100, 300)).fetchone())
    assert cost_large <= 1.2 * cost_small


@pytest.mark.unit
@pytest.mark.parametrize(
    ("window", "limit"),
    [
        pytest.param(Window(first=20, after=(5, 5)), 21, id="first-after"),
        pytest.param(Window(last=20, before=(5, 5)), 21, id="last-before"),
        pytest.param(Window(last=0), 1, id="last-zero"),
    ],
)
def test_each_page_reads_exactly_one_lookahead_row(window: Window, limit: int) -> None:
    _, params = page_sql(window)
    assert params[-1] == limit  # the LIMIT bind: size rows plus one lookahead row


@pytest.mark.unit
def test_reversing_the_page_in_sql_instead_of_python_adds_a_sort(db: Db) -> None:
    conn = db(events(300))
    sql, params = page_sql(Window(last=20, before=(5, 5)))
    wrapped = f"SELECT * FROM ({sql}) ORDER BY created_at DESC, id DESC"  # noqa: S608
    assert "USE TEMP B-TREE FOR ORDER BY" in query_plan(conn, wrapped, params)


def page_cost(conn: sqlite3.Connection, window: Window) -> int:
    return vm_steps(conn, lambda: fetch_connection(conn, window))


@pytest.mark.unit
def test_backward_page_cost_is_flat_in_cursor_depth(db: Db) -> None:
    rows = events(8000)
    conn = db(rows)
    all_edges = newest_first(rows)
    shallow = page_cost(conn, Window(last=20, before=all_edges[100]))
    deep = page_cost(conn, Window(last=20, before=all_edges[7900]))
    assert deep <= 1.2 * shallow
    assert shallow <= 1.2 * deep


@pytest.mark.unit
def test_last_without_before_is_flat_in_table_size(db: Db) -> None:
    small = db(events(1000))
    large = db(events(16000))
    cost_small = page_cost(small, Window(last=20))
    cost_large = page_cost(large, Window(last=20))
    assert cost_large <= 1.2 * cost_small
    assert keys(large, Window(last=3)) == [(1, 3), (0, 2), (0, 1)]  # the oldest three


# --- Contrast: strawberry's ListConnection, an offset connection behind the same envelope ---

QUERY = """
query ($first: Int, $after: String, $last: Int, $before: String) {
  events(first: $first, after: $after, last: $last, before: $before) {
    edges { cursor node { seq } }
    pageInfo { hasPreviousPage hasNextPage startCursor endCursor }
  }
}
"""


def run(schema: strawberry.Schema, **variables: object) -> dict[str, Any]:
    result = schema.execute_sync(QUERY, variable_values=dict(variables))
    assert result.errors is None, result.errors
    assert result.data is not None
    return cast("dict[str, Any]", result.data["events"])


def node_ids(connection: dict[str, Any]) -> list[int]:
    return [edge["node"]["seq"] for edge in connection["edges"]]


def nodes(n: int) -> list[EventNode]:
    return [EventNode(event_id=i, seq=i, created_at=n - i) for i in range(n)]


@pytest.mark.unit
def test_list_connection_cursor_is_base64_of_an_array_offset() -> None:
    page = run(build_offset_schema(lambda: nodes(10)), first=3)
    cursors = [base64.b64decode(edge["cursor"]) for edge in page["edges"]]
    assert cursors == [b"arrayconnection:0", b"arrayconnection:1", b"arrayconnection:2"]


@pytest.mark.unit
def test_offset_cursor_repeats_an_edge_after_a_head_insert_while_keyset_does_not(db: Db) -> None:
    source = nodes(10)
    schema = build_offset_schema(lambda: source)
    page1 = run(schema, first=3)
    source.insert(0, EventNode(event_id=100, seq=100, created_at=99))  # a newer row arrives
    page2 = run(schema, first=3, after=page1["pageInfo"]["endCursor"])
    assert node_ids(page2)[0] == node_ids(page1)[-1]

    rows = events(10)
    conn = db(rows)
    kpage1 = fetch_connection(conn, Window(first=3))
    conn.execute("INSERT INTO events VALUES (100, 99, 'new')")
    after = decode_cursor(cast("str", kpage1.page_info.end_cursor))
    kpage2 = fetch_connection(conn, Window(first=3, after=after))
    assert not {e.key for e in kpage1.nodes} & {e.key for e in kpage2.nodes}
    assert [e.key for e in kpage2.nodes] == newest_first(rows)[3:6]


@pytest.mark.unit
@pytest.mark.parametrize("size", [1000, 4000])
def test_list_connection_last_without_before_pulls_every_node(size: int) -> None:
    source = CountingSource(nodes(size))
    page = run(build_offset_schema(source), last=3)
    assert node_ids(page) == [size - 3, size - 2, size - 1]
    assert source.pulled == size


@pytest.mark.unit
def test_list_connection_last_zero_returns_everything_past_max_results(db: Db) -> None:
    page = run(build_offset_schema(lambda: nodes(500)), last=0)
    assert len(page["edges"]) == 500  # strawberry 0.340.0: edges[-0:] is edges[0:]
    assert len(page["edges"]) > 100  # StrawberryConfig.relay_max_results default
    assert keys(db(events(500)), Window(last=0)) == []


# --- HTTP envelope ---


async def get(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    return await client.get("/events", params=params)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_envelope_walks_forward_then_back_to_the_first_page(db: Db) -> None:
    rows = events(11)
    app = create_app(db(rows))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = (await get(client, first="4")).json()
        assert set(first) == {"edges", "pageInfo"}
        assert set(first["pageInfo"]) == {
            "hasPreviousPage",
            "hasNextPage",
            "startCursor",
            "endCursor",
        }
        assert set(first["edges"][0]) == {"cursor", "node"}
        second = (await get(client, first="4", after=first["pageInfo"]["endCursor"])).json()
        assert second["pageInfo"]["hasPreviousPage"] is True
        back = (await get(client, last="4", before=second["pageInfo"]["startCursor"])).json()
        assert back["edges"] == first["edges"]
        assert back["pageInfo"]["hasPreviousPage"] is False
        assert back["pageInfo"]["hasNextPage"] is True
        ids = [e["node"]["id"] for e in first["edges"] + second["edges"]]
        assert ids == [k[1] for k in newest_first(rows)[:8]]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"first": "1", "last": "1"}, id="first-and-last"),
        pytest.param({"first": "-1"}, id="negative"),
        pytest.param({"last": "101"}, id="over-max"),
        pytest.param({"after": "' OR 1=1 --"}, id="injection"),
        pytest.param({"before": "YXJyYXljb25uZWN0aW9uOjA"}, id="foreign-cursor"),
    ],
)
async def test_http_maps_bad_pagination_arguments_to_400(params: dict[str, str], db: Db) -> None:
    app = create_app(db(events(5)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await get(client, **params)
    assert response.status_code == 400
    assert set(response.json()) == {"error"}
