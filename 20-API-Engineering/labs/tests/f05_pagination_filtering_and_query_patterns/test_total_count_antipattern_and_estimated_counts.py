"""Exact COUNT(*) scales with matching rows; capped and planner counts do not."""

import httpx
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns.total_count import (
    CountBudgetExceededError,
    CountMode,
    CountRelation,
    ItemStore,
    TotalCount,
    build_app,
    parse_count_preference,
    pg_estimate_tuples,
    resolve_total,
)

SIZES = (5_000, 10_000, 20_000)


def _cost(store: ItemStore, label: str) -> int:
    return sum(steps for name, steps in store.cost_log if name == label)


@pytest.mark.unit
def test_exact_count_cost_is_linear_while_keyset_page_cost_is_flat() -> None:
    exact_costs: list[int] = []
    page_costs: list[int] = []
    for size in SIZES:
        store = ItemStore.create({1: size})
        assert store.exact_count(1) == TotalCount(value=size, relation=CountRelation.EQ)
        items, next_after = store.page(1, after=size // 2, limit=50)
        assert [item.id for item in items] == list(range(size // 2 + 1, size // 2 + 51))
        assert next_after == size // 2 + 50
        exact_costs.append(_cost(store, "count:exact"))
        page_costs.append(_cost(store, "page"))

    small, medium, large = exact_costs
    assert large - medium == 2 * (medium - small)  # strictly linear in matching rows
    assert large > 3 * small
    assert len(set(page_costs)) == 1  # O(limit), independent of collection size
    assert exact_costs[-1] > 100 * page_costs[-1]


@pytest.mark.unit
def test_capped_count_reads_at_most_cap_plus_one_rows_and_reports_gte() -> None:
    cap = 1_000
    costs: list[int] = []
    for size in SIZES:
        store = ItemStore.create({1: size})
        assert store.capped_count(1, cap) == TotalCount(value=cap, relation=CountRelation.GTE)
        costs.append(_cost(store, "count:capped"))
    assert len(set(costs)) == 1  # bounded by the cap, not by the collection

    boundary = ItemStore.create({1: cap, 2: cap + 1, 3: 7})
    assert boundary.capped_count(1, cap) == TotalCount(value=cap, relation=CountRelation.EQ)
    assert boundary.capped_count(2, cap) == TotalCount(value=cap, relation=CountRelation.GTE)
    assert boundary.capped_count(3, cap) == TotalCount(value=7, relation=CountRelation.EQ)


@pytest.mark.unit
def test_unfiltered_count_star_is_one_count_opcode_invisible_to_the_step_meter() -> None:
    costs: list[int] = []
    for size in SIZES:
        store = ItemStore.create({1: size})
        assert "Count" in store.explain("SELECT COUNT(*) FROM items")
        rows = store.run("count:table", "SELECT COUNT(*) FROM items", ())
        assert rows == [(size,)]
        costs.append(_cost(store, "count:table"))
    # OP_Count walks every b-tree page inside one opcode, so the step meter stays flat.
    assert len(set(costs)) == 1
    assert "Count" not in ItemStore.create({1: 1}).explain(
        "SELECT COUNT(*) FROM items WHERE tenant_id = ?", (1,)
    )


@pytest.mark.unit
def test_exact_count_budget_aborts_a_runaway_scan() -> None:
    store = ItemStore.create({1: 20_000})
    with pytest.raises(CountBudgetExceededError, match="count:exact: over 5000 steps"):
        store.exact_count(1, budget=5_000)
    assert store.exact_count(1, budget=100_000).value == 20_000


@pytest.mark.unit
def test_budget_abort_leaves_no_progress_handler_on_the_connection() -> None:
    store = ItemStore.create({1: 20_000})
    with pytest.raises(CountBudgetExceededError):
        store.exact_count(1, budget=5_000)
    # insert_items and analyze install no handler; a leaked budget handler would abort them.
    store.insert_items(1, 10)
    store.analyze()
    assert store.exact_count(1).value == 20_010


@pytest.mark.unit
def test_next_after_is_none_when_exactly_a_full_page_remains() -> None:
    store = ItemStore.create({1: 100})
    first, first_next = store.page(1, after=0, limit=50)
    assert [item.id for item in first] == list(range(1, 51))
    assert first_next == 50
    last, last_next = store.page(1, after=50, limit=50)
    assert [item.id for item in last] == list(range(51, 101))
    assert last_next is None  # exactly `limit` rows remain: no further page


@pytest.mark.unit
def test_planner_estimate_is_absent_before_analyze_skewed_after_and_stale_until_reanalyze() -> None:
    store = ItemStore.create({1: 7_500, 2: 2_500})
    assert store.planner_estimate() is None

    store.analyze()
    estimate = TotalCount(value=5_000, relation=CountRelation.APPROX)
    assert store.planner_estimate() == estimate  # tenant 1 has 7500, tenant 2 has 2500

    store.insert_items(1, 10_000)
    assert store.exact_count(1).value == 17_500
    assert store.planner_estimate() == estimate  # statistics do not follow writes

    store.analyze()
    assert store.planner_estimate() == TotalCount(value=10_000, relation=CountRelation.APPROX)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reltuples", "relpages", "curpages", "expected"),
    [
        (1_000_000.0, 10_000, 12_000, 1_200_000.0),  # table grew 20% since ANALYZE
        (1_000_000.0, 10_000, 10_000, 1_000_000.0),
        (5.0, 2, 1, 2.0),  # 2.5 rounds half to even, like C rint()
        (7.0, 2, 1, 4.0),  # 3.5 rounds up to the even 4, so truncation would give 3
        (7.0, 3, 1, 2.0),  # 2.33 rounds down
        (-1.0, 0, 50, None),  # never vacuumed or analyzed: reltuples = -1
        (-1.0, 0, 0, None),  # never vacuumed: 10-page minimum skips the empty exit
        (0.0, 0, 0, 0.0),  # vacuumed and empty: quick exit
        (0.0, 0, 5, None),  # relpages = 0: width-based guess, no density
    ],
)
def test_pg_density_scaling_model(
    reltuples: float, relpages: int, curpages: int, expected: float | None
) -> None:
    assert pg_estimate_tuples(reltuples, relpages, curpages) == expected


@pytest.mark.unit
def test_prefer_parsing_takes_first_count_and_ignores_unknown_values() -> None:
    assert parse_count_preference([]) is None
    assert parse_count_preference(["respond-async, wait=10"]) is None
    assert parse_count_preference(["count=exact"]) is CountMode.EXACT
    assert parse_count_preference(['wait=5, count="planned"; x=1']) is CountMode.PLANNED
    assert parse_count_preference(["count=capped, count=exact"]) is CountMode.CAPPED
    assert parse_count_preference(["return=minimal", "count=estimated"]) is CountMode.ESTIMATED
    assert parse_count_preference(["count=everything"]) is None


@pytest.mark.unit
def test_estimated_total_never_reports_less_than_the_capped_scan_proved() -> None:
    store = ItemStore.create({1: 1_500, 2: 100, 3: 100})
    store.analyze()
    assert store.planner_estimate() == TotalCount(value=567, relation=CountRelation.APPROX)
    total, applied = resolve_total(store, 1, CountMode.ESTIMATED, cap=1_000, allow_exact=False)
    assert applied is CountMode.ESTIMATED
    assert total == TotalCount(value=1_000, relation=CountRelation.APPROX)


async def _get(
    store: ItemStore, path: str, prefer: str | None = None, *, allow_exact: bool = False
) -> httpx.Response:
    app = build_app(store, cap=1_000, allow_exact=allow_exact)
    headers = {"Prefer": prefer} if prefer is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_without_prefer_runs_no_count_query_and_walks_keyset_pages() -> None:
    store = ItemStore.create({1: 7_500, 2: 120})
    seen: list[int] = []
    after = 0
    while True:
        response = await _get(store, f"/tenants/2/items?after={after}&limit=50")
        assert response.status_code == 200
        assert "preference-applied" not in response.headers
        body = response.json()
        assert body["total"] is None
        seen.extend(item["id"] for item in body["items"])
        if body["next_after"] is None:
            break
        after = body["next_after"]
    assert seen == list(range(7_501, 7_621))
    assert [name for name, _ in store.cost_log] == ["page", "page", "page"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prefer_exact_is_downgraded_to_capped_unless_policy_allows_it() -> None:
    store = ItemStore.create({1: 7_500})
    downgraded = await _get(store, "/tenants/1/items?limit=2", "count=exact")
    assert downgraded.status_code == 200
    assert downgraded.headers["preference-applied"] == "count=capped"
    assert downgraded.json()["total"] == {"value": 1_000, "relation": "gte"}

    allowed = await _get(store, "/tenants/1/items?limit=2", "count=exact", allow_exact=True)
    assert allowed.headers["preference-applied"] == "count=exact"
    assert allowed.json()["total"] == {"value": 7_500, "relation": "eq"}
    assert [name for name, _ in store.cost_log] == ["page", "count:capped", "page", "count:exact"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prefer_estimated_is_exact_below_cap_and_planner_estimate_above_it() -> None:
    store = ItemStore.create({1: 7_500, 2: 2_500, 3: 120})
    no_stats = await _get(store, "/tenants/1/items?limit=1", "count=estimated")
    assert no_stats.json()["total"] == {"value": 1_000, "relation": "gte"}

    store.analyze()
    small = await _get(store, "/tenants/3/items?limit=1", "count=estimated")
    assert small.headers["preference-applied"] == "count=estimated"
    assert small.json()["total"] == {"value": 120, "relation": "eq"}

    large = await _get(store, "/tenants/1/items?limit=1", "count=estimated")
    estimate = store.planner_estimate()
    assert estimate is not None
    assert large.json()["total"] == {"value": estimate.value, "relation": "approx"}
    assert estimate.value != 7_500  # an average over tenants, not this tenant's count

    planned = await _get(store, "/tenants/3/items?limit=1", "count=planned")
    assert planned.json()["total"] == {"value": estimate.value, "relation": "approx"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_count_preference_is_found_on_a_second_prefer_field_line() -> None:
    store = ItemStore.create({1: 30})
    app = build_app(store, cap=1_000)
    headers = [("Prefer", "return=minimal"), ("Prefer", "count=capped")]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/tenants/1/items?limit=5", headers=headers)
    assert response.headers["preference-applied"] == "count=capped"
    assert response.json()["total"] == {"value": 30, "relation": "eq"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_above_maximum_is_rejected_before_any_query() -> None:
    store = ItemStore.create({1: 10})
    response = await _get(store, "/tenants/1/items?limit=101", "count=exact")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]
    assert store.cost_log == []
