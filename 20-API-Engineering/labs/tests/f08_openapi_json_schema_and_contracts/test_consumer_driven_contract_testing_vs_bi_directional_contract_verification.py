"""CDCT replay vs BDCT static comparison on one provider with two consumers."""

import copy
from typing import Any

import pytest

from api_labs.f08_openapi_json_schema_and_contracts.contract_verification import (
    BILLING_PACT,
    DASHBOARD_PACT,
    Release,
    UserStore,
    build_provider,
    compare_pact_to_openapi,
    match_body,
    parse_pact,
    parse_rule_path,
    path_weight,
    self_verify,
    state_handlers,
    transform_received_schema,
    verify_pact,
)

BILLING = parse_pact(BILLING_PACT)
DASHBOARD = parse_pact(DASHBOARD_PACT)


def _codes(pact_doc: dict[str, Any], spec: dict[str, Any]) -> list[tuple[str, str]]:
    return [(f.code, f.severity) for f in compare_pact_to_openapi(parse_pact(pact_doc), spec)]


async def _replay(release: Release) -> dict[str, dict[str, list[str]]]:
    store = UserStore()
    app = build_provider(release, store)
    handlers = state_handlers(store)
    outcome: dict[str, dict[str, list[str]]] = {}
    for pact in (BILLING, DASHBOARD):
        outcome[pact.consumer] = (await verify_pact(pact, app, handlers, store.clear)).failures()
    return outcome


@pytest.mark.unit
def test_matcher_weighting_follows_the_v3_product_rule() -> None:
    # The V3 README table prints 16 for the last row, but its own factors
    # $(2).*(1).level(2)[*(1)].id(2) multiply to 8; the product rule is what is asserted.
    item = ("item1", "level", 1, "id")
    table = {
        "$": 2,
        "$.item1": 4,
        "$.item2": 0,
        "$.item1.level": 8,
        "$.item1.level[1]": 16,
        "$.item1.level[1].id": 32,
        "$.item1.level[1].name": 0,
        "$.item1.level[2]": 0,
        "$.item1.level[2].id": 0,
        "$.item1.level[*].id": 16,
        "$.*.level[*].id": 8,
    }
    assert {expr: path_weight(parse_rule_path(expr), item) for expr in table} == table


@pytest.mark.unit
def test_most_specific_rule_wins_so_equality_resets_a_cascaded_type_rule() -> None:
    rules = {
        "$.user": [{"match": "type"}],
        "$.user.role": [{"match": "equality"}],
    }
    expected = {"user": {"name": "Ada", "role": "admin"}}
    assert (
        match_body(
            expected, {"user": {"name": "Bob", "role": "admin"}}, rules, allow_unexpected_keys=True
        )
        == []
    )
    wrong_role = match_body(
        expected, {"user": {"name": "Bob", "role": "viewer"}}, rules, allow_unexpected_keys=True
    )
    assert [m.path for m in wrong_role] == ["$.user.role"]


@pytest.mark.unit
def test_postel_asymmetry_responses_tolerate_extra_keys_requests_do_not() -> None:
    expected = {"name": "Ada"}
    actual = {"name": "Ada", "ssn": "000-00-0000"}
    assert match_body(expected, actual, {}, allow_unexpected_keys=True) == []
    leak = match_body(expected, actual, {}, allow_unexpected_keys=False)
    assert [m.message for m in leak] == ["unexpected key 'ssn'"]


@pytest.mark.unit
def test_arrays_reject_extra_items_unless_a_min_type_rule_is_present() -> None:
    expected = {"ids": [1]}
    actual = {"ids": [7, 8, 9]}
    strict = match_body(expected, actual, {}, allow_unexpected_keys=True)
    assert [m.message for m in strict] == ["expected 1 items but got 3"]
    rules = {"$.ids": [{"match": "type", "min": 1}]}
    assert match_body(expected, actual, rules, allow_unexpected_keys=True) == []
    assert match_body(expected, {"ids": []}, rules, allow_unexpected_keys=True) != []
    assert match_body(expected, {"ids": ["7"]}, rules, allow_unexpected_keys=True) != []


@pytest.mark.unit
def test_unanchored_regex_matcher_lets_a_renamed_enum_value_through() -> None:
    loose = {"$.kind": [{"match": "regex", "regex": "(OBJECT|INTEGER)"}]}
    tight = {"$.kind": [{"match": "regex", "regex": "^(OBJECT|INTEGER)$"}]}
    renamed = {"kind": "INTEGER_RESULT"}
    assert match_body({"kind": "INTEGER"}, renamed, loose, allow_unexpected_keys=True) == []
    assert match_body({"kind": "INTEGER"}, renamed, tight, allow_unexpected_keys=True) != []


@pytest.mark.unit
def test_unknown_matcher_fails_closed() -> None:
    rules = {"$.v": [{"match": "semver"}]}
    with pytest.raises(ValueError, match="unsupported matcher"):
        match_body({"v": "1.0.0"}, {"v": "1.0.0"}, rules, allow_unexpected_keys=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cdct_replay_blames_only_the_consumer_that_reads_the_removed_field() -> None:
    assert await _replay("v1") == {"billing": {}, "dashboard": {}}
    missing_email = ["$: expected key 'email' but it was missing"]
    assert await _replay("v2-removed") == {
        "billing": {"get user 1 to address an invoice": missing_email},
        "dashboard": {},
    }


@pytest.mark.unit
def test_bdct_flags_a_field_removed_from_the_spec_for_its_reader_only() -> None:
    v1 = build_provider("v1", UserStore()).openapi()
    removed = build_provider("v2-removed", UserStore()).openapi()
    assert _codes(BILLING_PACT, v1) == []
    assert _codes(DASHBOARD_PACT, v1) == []
    assert _codes(BILLING_PACT, removed) == [("response.body.incompatible", "error")]
    assert _codes(DASHBOARD_PACT, removed) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optional_field_drift_passes_self_verification_and_bdct_but_fails_replay() -> None:
    store = UserStore()
    app = build_provider("v2-optional-unpopulated", store)
    spec = app.openapi()
    store.users[1] = {"name": "Ada", "email": "ada@example.com"}
    assert await self_verify(app, spec, [("GET", "/users/1")]) == []
    assert _codes(BILLING_PACT, spec) == []
    replay = await _replay("v2-optional-unpopulated")
    assert replay["billing"] == {
        "get user 1 to address an invoice": ["$: expected key 'email' but it was missing"]
    }
    assert replay["dashboard"] == {}


@pytest.mark.unit
def test_bdct_drops_required_for_responses_but_keeps_it_for_successful_requests() -> None:
    schema = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    }
    assert transform_received_schema(schema) == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    }
    spec = build_provider("v1", UserStore()).openapi()
    create: dict[str, Any] = {
        "consumer": {"name": "signup"},
        "provider": {"name": "user-service"},
        "interactions": [
            {
                "description": "create without email",
                "request": {"method": "POST", "path": "/users", "body": {"name": "Bob"}},
                "response": {"status": 201, "body": {"id": 2}},
            }
        ],
    }
    assert _codes(create, spec) == [("request.body.incompatible", "error")]
    rejected = copy.deepcopy(create)
    rejected["interactions"][0]["response"] = {"status": 422}
    assert _codes(rejected, spec) == []


@pytest.mark.unit
def test_status_lookup_is_exact_then_range_then_default_with_a_warning() -> None:
    spec = build_provider("v1", UserStore()).openapi()
    gone = copy.deepcopy(DASHBOARD_PACT)
    gone["interactions"] = [gone["interactions"][1]]
    gone["interactions"][0]["response"] = {"status": 410}
    assert _codes(gone, spec) == [("response.status.unknown", "error")]
    responses = spec["paths"]["/users/{user_id}"]["get"]["responses"]
    responses["4XX"] = copy.deepcopy(responses["404"])
    assert _codes(gone, spec) == []
    del responses["4XX"]
    responses["default"] = copy.deepcopy(responses["404"])
    assert _codes(gone, spec) == [("response.status.default", "warning")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_states_need_a_reset_between_interactions_and_a_handler() -> None:
    store = UserStore()
    app = build_provider("v1", store)
    leaky = await verify_pact(DASHBOARD, app, state_handlers(store), reset=lambda: None)
    assert leaky.failures() == {"get a missing user": ["status: expected 404, got 200"]}
    missing = await verify_pact(DASHBOARD, app, {}, reset=store.clear)
    assert missing.failures() == {
        "get user 1 to show a name": ["state: no handler for 'a user exists'"],
        "get a missing user": ["state: no handler for 'no users exist'"],
    }
