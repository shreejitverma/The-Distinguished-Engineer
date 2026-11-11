"""Lab: order_by allowlists, NULL placement, tie-breaking, and index-sort alignment."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, cast

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import CompileError
from starlette.datastructures import QueryParams

from api_labs.f05_pagination_filtering_and_query_patterns.sort_allowlist import (
    ASC,
    DESC,
    TASK_FIELDS,
    TASK_INDEXES,
    TASK_POLICY,
    Index,
    Nulls,
    SortError,
    SortField,
    SortKey,
    SortPolicy,
    TaskRow,
    Term,
    create_app,
    create_store,
    fetch_ids,
    list_sql,
    make_tasks,
    needs_sort,
    order_by_clause,
    parse_aip132,
    parse_order_by,
    query_plan,
    vm_steps,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

    type OpenStore = Callable[[list[TaskRow], SortPolicy], sqlite3.Connection]

ROWS = make_tasks(2000)
COLUMN = {"id": 0, "created_at": 1, "priority": 2, "due_at": 3}


def reference_ids(rows: list[TaskRow], keys: tuple[SortKey, ...]) -> list[int]:
    """Pure-Python model of the contract: NULLs last in both directions for due_at."""

    def sort_key(row: TaskRow) -> tuple[int, ...]:
        out: list[int] = []
        for key in keys:
            value = cast("int | None", row[COLUMN[key.field]])  # only integer columns sort
            sign = -1 if key.direction is DESC else 1
            if TASK_FIELDS[key.field].nulls is Nulls.LAST:
                out.append(1 if value is None else 0)
            out.append(0 if value is None else sign * value)
        return tuple(out)

    return [r[0] for r in sorted(rows, key=sort_key)]


@pytest.fixture
def open_store() -> Iterator[OpenStore]:
    """Build stores on demand and close every one of them at teardown."""
    opened: list[sqlite3.Connection] = []

    def factory(rows: list[TaskRow], policy: SortPolicy) -> sqlite3.Connection:
        opened.append(create_store(rows, policy))
        return opened[-1]

    yield factory
    for conn in opened:
        conn.close()


@pytest.fixture
def store(open_store: OpenStore) -> sqlite3.Connection:
    return open_store(ROWS, TASK_POLICY)


# --- Parsing -----------------------------------------------------------------


@pytest.mark.unit
def test_prefix_syntax_gives_each_key_its_own_direction() -> None:
    assert parse_order_by("-created_at,priority") == (
        SortKey("created_at", DESC),
        SortKey("priority", ASC),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["created_at desc,priority", "created_at desc, priority", " created_at  desc , priority "],
)
def test_aip132_syntax_ignores_redundant_spaces_and_matches_prefix_syntax(raw: str) -> None:
    assert parse_aip132(raw) == parse_order_by("-created_at,priority")


@pytest.mark.unit
def test_aip132_blank_means_default_and_only_desc_is_a_direction_word() -> None:
    assert parse_aip132("  ") == ()
    for raw in ("created_at asc", "-created_at", "created_at descending"):
        with pytest.raises(SortError, match="cannot parse"):
            parse_aip132(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "created_at,",
        ",created_at",
        "created_at,,priority",
        "--created_at",
        "+created_at",
        "created_at desc",
        "Created_at",
        "created_at;DROP TABLE tasks",
        "created_at)",
        "owner.name",
        "cr\u00e9ated_at",  # e with acute accent
        chr(0xFF43) + "reated_at",  # fullwidth c
        "created_at\t",
    ],
)
def test_malformed_order_by_is_a_client_error(raw: str) -> None:
    with pytest.raises(SortError) as info:
        parse_order_by(raw)
    assert info.value.code == "malformed"


@pytest.mark.unit
def test_order_by_longer_than_the_cap_is_rejected_before_parsing() -> None:
    raw = ",".join(["id"] * 43)  # 128 characters of valid terms
    assert len(parse_order_by(raw)) == 43
    for parse in (parse_order_by, parse_aip132):
        for too_long in (raw + ",id", ";" * 129, " " * 129):  # valid, malformed, blank
            with pytest.raises(SortError) as info:
                parse(too_long)
            assert info.value.code == "too_long"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("api_key", "unknown_field"),
        ("created_at,-created_at", "duplicate_field"),
        ("priority,created_at,due_at,title", "too_many_keys"),
        ("title", "unsupported_ordering"),
        ("priority,id", "unsupported_ordering"),
        ("-priority,created_at", "unsupported_ordering"),
        ("created_at,priority", "unsupported_ordering"),
        ("-due_at,id", "unsupported_ordering"),
        ("-id,created_at", "keys_after_tie_breaker"),
        ("id,title", "keys_after_tie_breaker"),
    ],
)
def test_policy_rejects_fields_and_orderings_outside_the_allowlist(raw: str, code: str) -> None:
    with pytest.raises(SortError) as info:
        TASK_POLICY.resolve(parse_order_by(raw))
    assert info.value.code == code


# --- Canonical, total orderings ------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "api", "sql", "index"),
    [
        ("", "-created_at,-id", "created_at DESC, id DESC", "ix_tasks_created"),
        ("-created_at", "-created_at,-id", "created_at DESC, id DESC", "ix_tasks_created"),
        ("priority", "priority,created_at,id", "priority ASC, created_at ASC, id ASC",
         "ix_tasks_priority_created"),
        ("-priority", "-priority,-created_at,-id", "priority DESC, created_at DESC, id DESC",
         "ix_tasks_priority_created"),
        ("due_at", "due_at,id", "due_at IS NULL ASC, due_at ASC, id ASC", "ix_tasks_due_asc"),
        ("-due_at", "-due_at,-id", "due_at IS NULL ASC, due_at DESC, id DESC",
         "ix_tasks_due_desc"),
        ("-id", "-id", "id DESC", "pk"),
    ],
)  # fmt: skip
def test_resolved_ordering_is_completed_from_its_index_and_ends_in_the_primary_key(
    raw: str, api: str, sql: str, index: str
) -> None:
    ordering = TASK_POLICY.resolve(parse_order_by(raw))
    assert (ordering.api(), ordering.sql(), ordering.index) == (api, sql, index)
    assert ordering.keys[-1].field == "id"
    assert ordering.requested == (TASK_POLICY.default if raw == "" else parse_order_by(raw))


@pytest.mark.unit
def test_supported_orderings_are_exactly_the_index_prefixes() -> None:
    assert TASK_POLICY.supported() == [
        "id",
        "-id",
        "created_at",
        "created_at,id",
        "-created_at",
        "-created_at,-id",
        "priority",
        "priority,created_at",
        "priority,created_at,id",
        "-priority",
        "-priority,-created_at",
        "-priority,-created_at,-id",
        "due_at",
        "due_at,id",
        "-due_at",
        "-due_at,-id",
    ]


@pytest.mark.unit
def test_policy_refuses_an_index_that_does_not_end_in_the_tie_breaker() -> None:
    with pytest.raises(ValueError, match="must end in the unique"):
        SortPolicy(TASK_FIELDS, (Index("ix_bad", (SortKey("priority"),)),), default=())
    with pytest.raises(ValueError, match="outside the allowlist"):
        SortPolicy(TASK_FIELDS, (Index("ix_bad", (SortKey("api_key"), SortKey("id"))),), ())
    with pytest.raises(ValueError, match="must end in the unique"):
        SortPolicy(TASK_FIELDS, (Index("ix_empty", ()),), ())


@pytest.mark.unit
def test_policy_refuses_a_default_ordering_no_index_serves() -> None:
    with pytest.raises(ValueError, match="no index serves the default"):
        SortPolicy(TASK_FIELDS, TASK_INDEXES, default=(SortKey("title"),))
    with pytest.raises(ValueError, match="default ordering is invalid"):
        SortPolicy(TASK_FIELDS, TASK_INDEXES, default=(SortKey("api_key"),))
    assert TASK_POLICY.naive(()) == TASK_POLICY.expand(TASK_POLICY.default)


# --- The alignment model against the real planner ------------------------------

CANDIDATE_FIELDS = ("id", "created_at", "priority", "due_at", "title")


def candidate_requests() -> list[tuple[SortKey, ...]]:
    out: list[tuple[SortKey, ...]] = []
    for n in (1, 2):
        for fields in itertools.permutations(CANDIDATE_FIELDS, n):
            if "id" in fields[:-1]:
                continue  # refused by check() as keys_after_tie_breaker
            for dirs in itertools.product((ASC, DESC), repeat=n):
                out.append(tuple(SortKey(f, d) for f, d in zip(fields, dirs, strict=True)))
    return out


@pytest.mark.unit
def test_alignment_model_agrees_with_the_sqlite_planner_on_every_candidate(
    store: sqlite3.Connection,
) -> None:
    accepted = rejected = 0
    for keys in candidate_requests():
        ordering = TASK_POLICY.match(TASK_POLICY.check(keys))
        if ordering is not None:
            accepted += 1
            assert not needs_sort(query_plan(store, list_sql(ordering.terms), (21,))), keys
        else:
            rejected += 1
            assert needs_sort(query_plan(store, list_sql(TASK_POLICY.naive(keys)), (21,))), keys
    assert (accepted, rejected) == (14, 60)


@pytest.mark.unit
def test_appending_only_the_primary_key_breaks_alignment_that_index_completion_keeps(
    store: sqlite3.Connection,
) -> None:
    keys = parse_order_by("priority")
    naive = query_plan(store, list_sql(TASK_POLICY.naive(keys)), (21,))
    completed = query_plan(store, list_sql(TASK_POLICY.resolve(keys).terms), (21,))
    assert naive == [
        "SCAN tasks USING INDEX ix_tasks_priority_created",
        "USE TEMP B-TREE FOR LAST TERM OF ORDER BY",
    ]
    assert completed == ["SCAN tasks USING INDEX ix_tasks_priority_created"]
    # The naive form appends `id` in the last key's direction, and only when it is missing.
    assert TASK_POLICY.naive(parse_order_by("-priority")) == (
        Term("priority", DESC),
        Term("id", DESC),
    )
    assert TASK_POLICY.naive(parse_order_by("priority")) == (Term("priority", ASC), Term("id", ASC))
    assert TASK_POLICY.naive(parse_order_by("-id")) == (Term("id", DESC),)


@pytest.mark.unit
def test_mixed_direction_ordering_is_served_once_a_matching_index_exists(
    open_store: OpenStore,
) -> None:
    mixed = Index("ix_tasks_priority_desc_created", (SortKey("priority", DESC),
                  SortKey("created_at"), SortKey("id")))  # fmt: skip
    policy = SortPolicy(TASK_FIELDS, (*TASK_INDEXES, mixed), TASK_POLICY.default)
    conn = open_store(ROWS, policy)
    ordering = policy.resolve(parse_order_by("-priority,created_at"))
    assert ordering.sql() == "priority DESC, created_at ASC, id ASC"
    plan = query_plan(conn, list_sql(ordering.terms), (21,))
    assert plan == ["SCAN tasks USING INDEX ix_tasks_priority_desc_created"]


# --- NULL placement ----------------------------------------------------------------


@pytest.mark.unit
def test_sqlite_sorts_null_first_ascending_and_last_descending(store: sqlite3.Connection) -> None:
    asc = store.execute("SELECT due_at FROM tasks ORDER BY due_at, id").fetchall()
    desc = store.execute("SELECT due_at FROM tasks ORDER BY due_at DESC, id DESC").fetchall()
    nulls = sum(1 for r in ROWS if r[3] is None)
    assert nulls == 500
    assert all(r[0] is None for r in asc[:nulls])
    assert asc[nulls][0] is not None
    assert all(r[0] is None for r in desc[-nulls:])
    assert desc[-nulls - 1][0] is not None


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["due_at", "-due_at"])
def test_nulls_last_contract_holds_in_both_directions(store: sqlite3.Connection, raw: str) -> None:
    ordering = TASK_POLICY.resolve(parse_order_by(raw))
    ids = fetch_ids(store, ordering.terms, len(ROWS))
    assert ids == reference_ids(ROWS, ordering.keys)
    due = {r[0]: r[3] for r in ROWS}
    assert all(due[i] is None for i in ids[-500:])
    assert due[ids[-501]] is not None


@pytest.mark.unit
def test_nulls_last_in_both_directions_needs_two_indexes(open_store: OpenStore) -> None:
    without_desc = tuple(i for i in TASK_INDEXES if i.name != "ix_tasks_due_desc")
    policy = SortPolicy(TASK_FIELDS, without_desc, TASK_POLICY.default)
    with pytest.raises(SortError, match="no index serves"):
        policy.resolve(parse_order_by("-due_at"))
    conn = open_store(ROWS, policy)
    terms = TASK_POLICY.resolve(parse_order_by("-due_at")).terms
    assert needs_sort(query_plan(conn, list_sql(terms), (21,)))


@pytest.mark.unit
def test_sqlite_serves_leading_nulls_last_with_a_two_segment_scan_hidden_from_eqp(
    open_store: OpenStore,
) -> None:
    conn = open_store(ROWS, SortPolicy(TASK_FIELDS, TASK_INDEXES[:1], ()))
    conn.execute("CREATE INDEX ix_due_plain ON tasks (due_at, id)")
    sql = "SELECT id FROM tasks ORDER BY due_at ASC NULLS LAST, id ASC LIMIT ?"
    assert query_plan(conn, sql, (21,)) == ["SCAN tasks USING COVERING INDEX ix_due_plain"]
    opcodes = [row[1] for row in conn.execute(f"EXPLAIN {sql}", (21,))]
    # Seek past the NULLs, serve the values, then rewind to serve the NULLs.
    assert "SeekGT" in opcodes
    assert "Rewind" in opcodes
    ids = [r[0] for r in conn.execute(sql, (len(ROWS),))]
    assert ids == reference_ids(ROWS, (SortKey("due_at"), SortKey("id")))


# --- Determinism ---------------------------------------------------------------------

HINTS = ("INDEXED BY ix_tasks_priority_created", "NOT INDEXED")


def offset_walk(conn: sqlite3.Connection, terms: tuple[Term, ...], size: int) -> list[int]:
    """OFFSET pages whose plan flips every page, as after a new index or fresh statistics."""
    served: list[int] = []
    for page in range(len(ROWS) // size):
        hint = HINTS[page % 2]
        sql = f"SELECT id FROM tasks {hint} ORDER BY {order_by_clause(terms)} LIMIT ? OFFSET ?"  # noqa: S608
        served.extend(r[0] for r in conn.execute(sql, (size, page * size)))
    return served


@pytest.mark.unit
def test_non_unique_sort_returns_plan_dependent_rows(store: sqlite3.Connection) -> None:
    terms = (Term("priority", ASC),)
    pages = [
        [r[0] for r in store.execute(f"SELECT id FROM tasks {h} ORDER BY priority LIMIT 10")]  # noqa: S608
        for h in HINTS
    ]
    assert pages[0] != pages[1]
    served = offset_walk(store, terms, 50)
    assert len(served) == len(ROWS)
    assert len(set(served)) < len(ROWS)  # duplicates, therefore also missing rows


@pytest.mark.unit
def test_completed_ordering_is_plan_independent(store: sqlite3.Connection) -> None:
    terms = TASK_POLICY.resolve(parse_order_by("priority")).terms
    served = offset_walk(store, terms, 50)
    assert served == reference_ids(ROWS, TASK_POLICY.resolve(parse_order_by("priority")).keys)


@pytest.mark.unit
@pytest.mark.parametrize("raw", TASK_POLICY.supported())
def test_every_supported_ordering_matches_the_reference_model(
    store: sqlite3.Connection, raw: str
) -> None:
    ordering = TASK_POLICY.resolve(parse_order_by(raw))
    assert fetch_ids(store, ordering.terms, len(ROWS)) == reference_ids(ROWS, ordering.keys)


# --- Cost: why unindexed orderings are a resource-consumption risk ---------------------


@pytest.mark.unit
def test_unaligned_sort_cost_grows_with_the_table_while_aligned_cost_is_flat(
    open_store: OpenStore,
) -> None:
    aligned = TASK_POLICY.resolve(parse_order_by("-created_at")).terms
    unaligned = TASK_POLICY.naive(parse_order_by("title"))
    small, large = (open_store(make_tasks(n), TASK_POLICY) for n in (1000, 8000))
    a_small, a_large = (vm_steps(c, list_sql(aligned), (21,)) for c in (small, large))
    u_small, u_large = (vm_steps(c, list_sql(unaligned), (21,)) for c in (small, large))
    assert a_small >= 2 * 21  # every returned row costs at least a ResultRow and a Next
    assert a_large <= 1.2 * a_small
    assert u_large >= 6 * u_small
    assert u_large >= 100 * a_large


# --- HTTP contract -----------------------------------------------------------------------


def client_for(conn: sqlite3.Connection) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(conn)), base_url="http://test"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_serves_a_supported_ordering_and_echoes_the_effective_order_by(
    store: sqlite3.Connection,
) -> None:
    async with client_for(store) as client:
        response = await client.get("/tasks", params={"order_by": "-priority"})
    assert response.status_code == 200
    body = response.json()
    assert body["order_by"] == "-priority,-created_at,-id"
    expected = reference_ids(ROWS, TASK_POLICY.resolve(parse_order_by("-priority")).keys)
    assert [item["id"] for item in body["items"]] == expected[:20]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?order_by="])
async def test_http_absent_or_empty_order_by_gets_the_default(
    store: sqlite3.Connection, query: str
) -> None:
    async with client_for(store) as client:
        response = await client.get(f"/tasks{query}")
    assert response.status_code == 200
    assert response.json()["order_by"] == "-created_at,-id"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("order_by=api_key", "unknown_field"),
        ("order_by=title", "unsupported_ordering"),
        ("order_by=created_at%3BDROP%20TABLE%20tasks", "malformed"),
        ("order_by=%2Bcreated_at", "malformed"),
        ("order_by=priority,created_at,due_at,title", "too_many_keys"),
        ("order_by=-created_at&order_by=priority", "repeated_parameter"),
    ],
)
async def test_http_rejects_bad_order_by_with_problem_details(
    store: sqlite3.Connection, query: str, code: str
) -> None:
    statements: list[str] = []
    store.set_trace_callback(statements.append)
    async with client_for(store) as client:
        response = await client.get(f"/tasks?{query}")
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code
    assert statements == []  # rejected before any SQL ran


@pytest.mark.unit
def test_starlette_query_params_keep_the_last_repeat_and_decode_plus_as_space() -> None:
    params = QueryParams("order_by=-created_at&order_by=priority")
    assert params["order_by"] == "priority"
    assert params.getlist("order_by") == ["-created_at", "priority"]
    assert QueryParams("order_by=+created_at")["order_by"] == " created_at"
    assert QueryParams("order_by=created_at+desc")["order_by"] == "created_at desc"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_form_encoded_plus_reaches_the_parser_as_a_space(store: sqlite3.Connection) -> None:
    async with client_for(store) as client:
        response = await client.get("/tasks?order_by=+created_at")
    assert response.status_code == 200
    assert response.json()["order_by"] == "created_at,id"


# --- Security: an ORM is not an allowlist, and sorting is an oracle ------------------------


@pytest.mark.unit
def test_sqlalchemy_string_order_by_blocks_injection_but_resolves_unselected_columns() -> None:
    tasks = sa.Table(
        "tasks", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("api_key", sa.String),
    )  # fmt: skip
    leaked = str(sa.select(tasks.c.id).order_by("api_key")).replace("\n", " ")
    assert leaked == "SELECT tasks.id  FROM tasks ORDER BY tasks.api_key"
    with pytest.raises(CompileError, match="Textual SQL expression"):
        str(sa.select(tasks.c.id).order_by("id; DROP TABLE tasks"))


@pytest.mark.unit
def test_sorting_on_a_hidden_column_is_an_oracle_for_its_value(open_store: OpenStore) -> None:
    fields = {**TASK_FIELDS, "api_key": SortField("api_key")}
    key_index = Index("ix_key", (SortKey("api_key"), SortKey("id")))
    leaky = SortPolicy(fields, (*TASK_INDEXES, key_index), ())
    rows = [*ROWS[:200], (9999, 0, 0, None, "attacker", "key-001000")]
    conn = open_store(rows, leaky)
    ids = fetch_ids(conn, leaky.resolve(parse_order_by("api_key")).terms, len(rows))
    before = set(ids[: ids.index(9999)])
    # The attacker never reads api_key, yet learns which rows hold a smaller one.
    assert before == {r[0] for r in rows if r[5] < "key-001000"}
    with pytest.raises(SortError, match="not a sortable field"):
        TASK_POLICY.resolve(parse_order_by("api_key"))
