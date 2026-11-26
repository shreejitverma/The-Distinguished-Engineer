"""Lab for GraphQL-Security-Query-Depth-Complexity-Cost-Analysis-and-Batch-Limits."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from graphql import (
    execute_sync,  # pyright: ignore[reportUnknownVariableType]
    get_introspection_query,
    parse,
    validate,
)
from strawberry.extensions import query_depth_limiter

from api_labs.f20_graphql_json_rpc_and_event_apis import (
    graphql_security_query_depth_complexity_cost_analysis_and_batch_limits as lab,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette


@dataclass
class Rig:
    app: Starlette
    backend: lab.Backend
    budget: lab.CostBudget


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_rig(
    limits: lab.Limits | None = None,
    *,
    capacity: float = 100_000,
    refill: float = 1.0,
    clock: FakeClock | None = None,
) -> Rig:
    backend = lab.Backend()
    budget = lab.CostBudget(capacity, refill, clock or FakeClock())
    app = lab.build_app(lab.build_lab_schema(), backend, limits or lab.Limits(), budget)
    return Rig(app, backend, budget)


async def post(
    rig: Rig, payload: Any = None, *, content: bytes | None = None, client: str = "203.0.113.7"
) -> httpx.Response:
    transport = httpx.ASGITransport(app=rig.app, client=(client, 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        if content is not None:
            return await http.post("/graphql", content=content)
        return await http.post("/graphql", json=payload)


def friends_chain(levels: int, first: int) -> str:
    return "{ me " + f"{{ friends(first: {first}) " * levels + "{ id }" + " }" * (levels + 1)


def login_aliases(count: int) -> str:
    fields = " ".join(
        f'a{i}: login(username: "victim", password: "guess{i}") {{ ok }}' for i in range(count)
    )
    return f"mutation {{ {fields} }}"


def code_of(response: httpx.Response) -> str:
    return str(response.json()["errors"][0]["extensions"]["code"])


def profile_of(query: str, operation: str | None = None) -> lab.Profile:
    document = parse(query)
    return lab.analyze(document, lab.select_operation(document, operation))


def cost_of(query: str, variables: dict[str, Any] | None = None) -> int:
    schema = lab.build_lab_schema()
    document = parse(query)
    operation = lab.select_operation(document, None)
    return lab.estimate_cost(schema, lab.CostModel(), document, operation, variables or {})


# --- depth -----------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cyclic_15_level_query_is_rejected_before_any_resolver_runs() -> None:
    rig = make_rig()
    response = await post(rig, {"query": friends_chain(15, 2)})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/graphql-response+json")
    assert code_of(response) == "DEPTH_LIMIT"
    assert rig.backend.resolver_calls == 0


@pytest.mark.unit
def test_unprotected_cyclic_query_costs_an_exponential_number_of_resolver_calls() -> None:
    levels, first = 8, 2
    backend = lab.Backend()
    schema = lab.build_lab_schema()
    document = parse(friends_chain(levels, first))
    result = execute_sync(schema, document, field_resolver=backend.resolve)
    assert result.errors is None
    expected = 1 + (first**levels - 1) // (first - 1) + first**levels
    assert backend.resolver_calls == expected == 512
    assert profile_of(friends_chain(levels, first)).depth == levels + 2


@pytest.mark.unit
def test_depth_counts_levels_hidden_behind_fragments() -> None:
    hidden = """
    { me { ...A } }
    fragment A on User { friends { ...B } }
    fragment B on User { friends { id } }
    """
    assert profile_of(hidden).depth == 4
    assert profile_of("{ me { friends { friends { id } } } }").depth == 4


@pytest.mark.unit
def test_depth_is_unchanged_by_inline_fragments() -> None:
    assert profile_of("{ me { ... on User { ... on User { id } } } }").depth == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fragment_cycle_is_a_clean_rejection_not_a_crash() -> None:
    query = "{ me { ...A } } fragment A on User { ...B } fragment B on User { ...A }"
    assert profile_of(query).depth == 1
    rig = make_rig()
    response = await post(rig, {"query": query})
    assert response.status_code == 422
    assert code_of(response) == "VALIDATION_FAILED"
    assert "within itself" in response.json()["errors"][0]["message"]
    assert rig.backend.resolver_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undefined_fragment_is_a_clean_rejection() -> None:
    rig = make_rig()
    response = await post(rig, {"query": "{ me { ...Missing } }"})
    assert response.status_code == 422
    assert "Unknown fragment" in response.json()["errors"][0]["message"]
    assert rig.backend.resolver_calls == 0


@pytest.mark.unit
def test_fragment_dag_analysis_is_linear_not_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    levels = 40
    parts = [f"fragment F{i} on User {{ ...F{i + 1} ...F{i + 1} }}" for i in range(levels)]
    parts.append(f"fragment F{levels} on User {{ name }}")
    query = "{ me { ...F0 } } " + " ".join(parts)
    calls = 0
    original = lab.scan_selection_set

    def counting(selection_set: Any) -> lab.Scan:
        nonlocal calls
        calls += 1
        if calls > 200:
            raise AssertionError("analysis revisits fragments")
        return original(selection_set)

    monkeypatch.setattr(lab, "scan_selection_set", counting)
    profile = profile_of(query)
    assert calls == levels + 2
    assert profile.names["name"] == 2**levels
    assert profile.depth == 2


@pytest.mark.unit
def test_strawberry_depth_limiter_revisits_shared_fragments_exponentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = lab.build_lab_schema()

    def dag(levels: int) -> str:
        parts = [f"fragment F{i} on User {{ ...F{i + 1} ...F{i + 1} }}" for i in range(levels)]
        parts.append(f"fragment F{levels} on User {{ name }}")
        return "{ me { ...F0 } } " + " ".join(parts)

    counts: list[int] = []
    original = query_depth_limiter.determine_depth
    tally = 0

    def counting(*args: Any, **kwargs: Any) -> int:
        nonlocal tally
        tally += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(query_depth_limiter, "determine_depth", counting)
    for levels in (8, 10):
        tally = 0
        validate(schema, parse(dag(levels)), [query_depth_limiter.create_validator(50, None)])
        counts.append(tally)
    assert counts[1] > 3.5 * counts[0]
    assert counts[1] > 2**10


# --- breadth, aliases, sensitive fields ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hundred_alias_login_brute_force_is_rejected_before_any_login_runs() -> None:
    rig = make_rig()
    response = await post(rig, {"query": login_aliases(100)})
    assert response.status_code == 422
    assert code_of(response) == "ALIAS_LIMIT"
    assert rig.backend.login_attempts == []
    assert rig.backend.resolver_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_caps_all_hundred_guesses_run_serially_in_order() -> None:
    loose = lab.Limits(
        max_aliases=10_000, max_root_fields=10_000, sensitive_field_limits={}, max_cost=100_000
    )
    rig = make_rig(loose)
    response = await post(rig, {"query": login_aliases(100)})
    assert response.status_code == 200
    assert len(rig.backend.login_attempts) == 100
    assert list(response.json()["data"]) == [f"a{i}" for i in range(100)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sensitive_field_cap_stops_brute_force_even_with_loose_alias_limits() -> None:
    loose = lab.Limits(max_aliases=10_000, max_root_fields=10_000, max_cost=100_000)
    rig = make_rig(loose)
    response = await post(rig, {"query": login_aliases(100)})
    assert response.status_code == 422
    assert code_of(response) == "FIELD_REPEAT_LIMIT"
    assert rig.backend.login_attempts == []


@pytest.mark.unit
def test_aliases_in_a_reused_fragment_are_counted_once_per_spread() -> None:
    inner = ", ".join(f"a{i}: name" for i in range(10))
    spreads = " ".join(f"u{i}: me {{ ...P }}" for i in range(10))
    query = f"fragment P on User {{ {inner} }} query Q {{ {spreads} }}"
    assert profile_of(query).aliases == 10 + 10 * 10
    with pytest.raises(lab.Rejected) as caught:
        lab.prepare(lab.build_lab_schema(), lab.Limits(max_aliases=20), lab.CostModel(), query)
    assert caught.value.code == "ALIAS_LIMIT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_the_selected_operation_is_analyzed_and_executed() -> None:
    heavy = " ".join(f"a{i}: me {{ id }}" for i in range(50))
    query = f"query Heavy {{ {heavy} }} query Light {{ me {{ id }} }}"
    rig = make_rig()
    response = await post(rig, {"query": query, "operationName": "Light"})
    assert response.status_code == 200
    assert response.json() == {"data": {"me": {"id": "id-value"}}}
    assert rig.backend.resolver_calls == 2
    rejected = await post(rig, {"query": query, "operationName": "Heavy"})
    assert code_of(rejected) == "ALIAS_LIMIT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_root_field_count_is_capped_through_fragments() -> None:
    query = (
        "{ ...R } fragment R on Query { " + " ".join(f"f{i}: me {{ id }}" for i in range(4)) + " }"
    )
    rig = make_rig(replace(lab.Limits(), max_root_fields=3, max_aliases=100))
    assert profile_of(query).top_fields == 4
    response = await post(rig, {"query": query})
    assert code_of(response) == "ROOT_FIELD_LIMIT"


# --- weighted cost ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cost_multiplies_list_sizes_through_nesting() -> None:
    assert cost_of("{ users(first: 10) { friends(first: 5) { id } } }") == 10 * (1 + 5 * 1)


@pytest.mark.unit
def test_cost_reads_variables_and_assumes_the_worst_when_unknown() -> None:
    query = "query Q($n: Int) { users(first: $n) { id } }"
    assert cost_of(query, {"n": 3}) == 3
    assert cost_of(query, {}) == lab.CostModel().assumed_list_size
    assert cost_of(query, {"n": True}) == lab.CostModel().assumed_list_size


@pytest.mark.unit
def test_cost_uses_the_schema_default_when_the_argument_is_omitted() -> None:
    assert cost_of("{ users { id } }") == 10


@pytest.mark.unit
def test_a_weighted_mutation_costs_its_weight() -> None:
    assert cost_of('mutation { login(username: "u", password: "p") { ok } }') == 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shallow_wide_query_is_rejected_by_cost_not_by_depth() -> None:
    query = "{ users(first: 100) { friends(first: 100) { posts(first: 100) { id } } } }"
    assert profile_of(query).depth == 4
    assert cost_of(query) == 100 * (1 + 100 * (1 + 100 * 1))
    rig = make_rig()
    response = await post(rig, {"query": query})
    assert code_of(response) == "COST_LIMIT"
    assert rig.backend.resolver_calls == 0
    huge = await post(rig, {"query": "{ users(first: 99999999) { id } }"})
    assert code_of(huge) == "COST_LIMIT"


@pytest.mark.unit
def test_fragment_cost_is_charged_at_every_spreading_field() -> None:
    query = "{ users(first: 10) { ...P } } fragment P on User { posts(first: 4) { id } }"
    assert cost_of(query) == 10 * (1 + 4 * 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_charges_cost_and_answers_429_with_retry_after() -> None:
    clock = FakeClock()
    rig = make_rig(lab.Limits(max_cost=100), capacity=100, refill=10, clock=clock)
    query = {"query": "{ users(first: 60) { id } }"}
    assert (await post(rig, query)).status_code == 200
    blocked = await post(rig, query)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "2"
    assert code_of(blocked) == "RATE_LIMITED"
    calls = rig.backend.resolver_calls
    clock.now += 2
    assert (await post(rig, query)).status_code == 200
    assert rig.backend.resolver_calls > calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_budget_is_tracked_per_client() -> None:
    rig = make_rig(lab.Limits(max_cost=100), capacity=100, refill=1)
    query = {"query": "{ users(first: 90) { id } }"}
    assert (await post(rig, query, client="198.51.100.1")).status_code == 200
    assert (await post(rig, query, client="198.51.100.1")).status_code == 429
    assert (await post(rig, query, client="198.51.100.2")).status_code == 200


@pytest.mark.unit
def test_a_max_cost_above_the_bucket_capacity_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="never be paid"):
        make_rig(lab.Limits(max_cost=500), capacity=100)


# --- batching --------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batching_is_off_by_default() -> None:
    rig = make_rig()
    response = await post(rig, [{"query": "{ me { id } }"}])
    assert response.status_code == 400
    assert code_of(response) == "BATCHING_DISABLED"
    assert rig.backend.resolver_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_size_is_capped_and_an_empty_batch_is_refused() -> None:
    rig = make_rig(lab.Limits(batching=True, max_batch_size=5))
    entry = {"query": "{ me { id } }"}
    ok = await post(rig, [entry] * 5)
    assert ok.status_code == 200
    assert len(ok.json()) == 5
    over = await post(rig, [entry] * 6)
    assert (over.status_code, code_of(over)) == (400, "BATCH_LIMIT")
    empty = await post(rig, [])
    assert (empty.status_code, code_of(empty)) == (400, "BATCH_LIMIT")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_login_operations_still_hit_the_per_request_sensitive_cap() -> None:
    rig = make_rig(lab.Limits(batching=True, max_batch_size=5))
    login = {"query": 'mutation { login(username: "victim", password: "x") { ok } }'}
    one = await post(rig, [login])
    assert one.status_code == 200
    assert rig.backend.login_attempts == ["victim"]
    many = await post(rig, [login] * 3)
    assert many.status_code == 422
    assert code_of(many) == "FIELD_REPEAT_LIMIT"
    assert rig.backend.login_attempts == ["victim"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_cost_is_summed_against_the_budget() -> None:
    rig = make_rig(lab.Limits(batching=True, max_cost=100), capacity=100, refill=1)
    entry = {"query": "{ users(first: 60) { id } }"}
    alone = await post(rig, [entry], client="198.51.100.9")
    assert alone.status_code == 200
    rig2 = make_rig(lab.Limits(batching=True, max_cost=100), capacity=100, refill=1)
    together = await post(rig2, [entry, entry])
    assert together.status_code == 429
    assert rig2.backend.resolver_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_rejected_batch_entry_does_not_block_its_neighbours() -> None:
    rig = make_rig(lab.Limits(batching=True))
    good = {"query": "{ me { id } }"}
    bad = {"query": friends_chain(15, 2)}
    response = await post(rig, [good, bad])
    assert response.status_code == 200
    first, second = response.json()
    assert first == {"data": {"me": {"id": "id-value"}}}
    assert second["errors"][0]["extensions"]["code"] == "DEPTH_LIMIT"


# --- introspection and suggestions -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_introspection_is_blocked_but_typename_is_not() -> None:
    rig = make_rig()
    blocked = await post(rig, {"query": "{ __schema { queryType { name } } }"})
    assert blocked.status_code == 422
    assert "introspection has been disabled" in blocked.json()["errors"][0]["message"]
    typed = await post(rig, {"query": '{ __type(name: "User") { name } }'})
    assert typed.status_code == 422
    typename = await post(rig, {"query": "{ __typename }"})
    assert typename.json() == {"data": {"__typename": "Query"}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_introspection_works_when_explicitly_allowed() -> None:
    rig = make_rig(lab.Limits(allow_introspection=True))
    response = await post(rig, {"query": "{ __schema { queryType { name } } }"})
    assert response.json() == {"data": {"__schema": {"queryType": {"name": "Query"}}}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_did_you_mean_suggestions_leak_field_names_until_hidden() -> None:
    leaky = make_rig(lab.Limits(hide_suggestions=False))
    leaked = await post(leaky, {"query": "{ mee { id } }"})
    assert "Did you mean 'me'?" in leaked.json()["errors"][0]["message"]
    hidden = await post(make_rig(), {"query": "{ mee { id } }"})
    assert "Did you mean" not in hidden.json()["errors"][0]["message"]


@pytest.mark.unit
def test_the_standard_introspection_query_is_deeper_than_a_typical_depth_limit() -> None:
    query = get_introspection_query()
    depth = profile_of(query).depth
    assert depth > lab.Limits().max_depth
    with pytest.raises(lab.Rejected) as caught:
        lab.prepare(
            lab.build_lab_schema(),
            lab.Limits(allow_introspection=True),
            lab.CostModel(),
            query,
        )
    assert caught.value.code == "DEPTH_LIMIT"


# --- parser and transport limits -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_documents_and_token_floods_fail_at_the_parser() -> None:
    rig = make_rig()
    big = await post(rig, {"query": "{ me { id } }" + " " * 9_000})
    assert (big.status_code, code_of(big)) == (400, "PARSE_FAILED")
    assert "code points" in big.json()["errors"][0]["message"]
    wide = make_rig(replace(lab.Limits(), max_source_length=100_000, max_root_fields=10_000))
    flood = await post(wide, {"query": "{ " + "me " * 5_000 + "}"})
    assert (flood.status_code, code_of(flood)) == (400, "PARSE_FAILED")
    assert "tokens" in flood.json()["errors"][0]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_comment_padding_is_bounded_separately_from_tokens() -> None:
    rig = make_rig(replace(lab.Limits(), max_source_length=1_000_000))
    response = await post(rig, {"query": "# c\n" * 500 + "{ me { id } }"})
    assert (response.status_code, code_of(response)) == (400, "PARSE_FAILED")
    assert "comments" in response.json()["errors"][0]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_parser_refuses_nesting_beyond_200_levels_whatever_the_depth_limit() -> None:
    rig = make_rig(replace(lab.Limits(), max_depth=10_000, max_source_length=1_000_000))
    deep = "{" + "me{" * 200 + "id" + "}" * 201
    response = await post(rig, {"query": deep})
    assert (response.status_code, code_of(response)) == (400, "PARSE_FAILED")
    assert "nested too deeply" in response.json()["errors"][0]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validation_errors_are_capped() -> None:
    rig = make_rig(replace(lab.Limits(), max_lexical_tokens=10_000, max_source_length=100_000))
    response = await post(
        rig, {"query": "{ me { " + " ".join(f"x{i}" for i in range(150)) + " } }"}
    )
    message = response.json()["errors"][0]["message"]
    assert code_of(response) == "VALIDATION_FAILED"
    assert message.count("Cannot query field") == 10
    assert "Too many validation errors" in message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_body_is_413_before_json_parsing() -> None:
    rig = make_rig()
    response = await post(rig, content=b"[" * 20_000 + b"]" * 20_000)
    assert (response.status_code, code_of(response)) == (413, "BODY_TOO_LARGE")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_without_content_length_is_capped_while_streaming() -> None:
    rig = make_rig()
    transport = httpx.ASGITransport(app=rig.app, client=("203.0.113.7", 5000))

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b" " * 4_000

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/graphql", content=chunks())
    assert (response.status_code, code_of(response)) == (413, "BODY_TOO_LARGE")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_json_nesting_bomb_is_a_400_not_a_500() -> None:
    rig = make_rig(replace(lab.Limits(), max_body_bytes=2_000_000))
    bomb = b"[" * 500_000 + b"]" * 500_000
    response = await post(rig, content=bomb)
    assert (response.status_code, code_of(response)) == (400, "BAD_JSON")


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"query": friends_chain(15, 2)},
        {"query": login_aliases(100)},
        {"query": "{ users(first: 100) { friends(first: 100) { posts(first: 100) { id } } } }"},
        {"query": "{ __schema { types { name } } }"},
        {"query": "{ ...A } fragment A on Query { ...A }"},
        {"query": "{ me { id } ", "operationName": None},
        {"query": "query A { me { id } } query B { me { id } }"},
    ],
)
@pytest.mark.asyncio
async def test_every_rejection_path_leaves_the_backend_untouched(payload: dict[str, Any]) -> None:
    rig = make_rig()
    response = await post(rig, payload)
    assert 400 <= response.status_code < 500
    assert json.loads(response.content)["errors"]
    assert rig.backend.resolver_calls == 0
    assert rig.backend.login_attempts == []
