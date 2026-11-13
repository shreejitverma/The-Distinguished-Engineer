"""Lab: chaining POST -> GET -> PATCH -> DELETE through OpenAPI links in Schemathesis."""

from __future__ import annotations

import copy
from collections import Counter
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from hypothesis import settings
from openapi_spec_validator import validate
from schemathesis.core.errors import InvalidStateMachine, NoLinksFound
from schemathesis.core.failures import FailureGroup
from schemathesis.core.transforms import UNRESOLVABLE
from schemathesis.core.transforms import resolve_pointer as schemathesis_pointer
from schemathesis.generation.stateful.state_machine import DEFAULT_STATE_MACHINE_SETTINGS
from schemathesis.openapi.checks import EnsureResourceAvailability, UseAfterFree

from api_labs.f08_openapi_json_schema_and_contracts.stateful_links import (
    LIFECYCLE_SPEC,
    Defects,
    Exchange,
    ItemStore,
    Unresolved,
    evaluate_expression,
    evaluate_template,
    iter_responses,
    lifecycle_machine,
    load_schema,
    make_app,
    resolve_pointer,
    spec_without_links,
    stateful_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import Receive, Scope, Send

# Schemathesis defaults (generate phase only, no deadline, health checks off), made
# deterministic and small: 60 scenarios of up to 6 calls and no example database.
SCENARIOS = settings(
    DEFAULT_STATE_MACHINE_SETTINGS,
    max_examples=60,
    stateful_step_count=6,
    derandomize=True,
    database=None,
)

EXPECTED_RULES = {
    "RANDOM__POST_items",
    "POST_items___201_GetItem__GET_items_item_id_",
    "POST_items___201_UpdateItem__PATCH_items_item_id_",
    "POST_items___201_DeleteItem__DELETE_items_item_id_",
    "PATCH_items_item_id___200_GetAfterUpdate__GET_items_item_id_",
    "DELETE_items_item_id___204_GetAfterDelete__GET_items_item_id_",
}


class CountingApp:
    """ASGI wrapper that counts (method, status) pairs the state machine produced."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Any]) -> None:
        self.app = app
        self.seen: Counter[tuple[str, int]] = Counter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counting_send(message: Any) -> None:
            if message["type"] == "http.response.start" and scope["path"] != "/openapi.json":
                self.seen[(scope["method"], message["status"])] += 1
            await send(message)

        await self.app(scope, receive, counting_send)


def run_machine(
    defects: Defects,
    *,
    spec: dict[str, Any] | None = None,
    model: bool = False,
    inference: bool = False,
    negative: bool = False,
    first_id: int = 1,
    constants: bool = True,
) -> CountingApp:
    store = ItemStore(first_id=first_id)
    app = CountingApp(make_app(store, defects))
    config = stateful_config(inference=inference, negative=negative, constants=constants)
    schema = load_schema(app, spec, config=config)  # type: ignore[arg-type]
    lifecycle_machine(schema, store, model=model).run(settings=SCENARIOS)
    return app


def rule_names(spec: dict[str, Any], *, inference: bool = False) -> set[str]:
    store = ItemStore()
    schema = load_schema(make_app(store), spec, config=stateful_config(inference=inference))
    machine = schema.as_state_machine()
    return {name for name in vars(machine) if name[0].isupper() and callable(vars(machine)[name])}


def failure_types(exc: FailureGroup) -> set[type]:
    return {type(failure) for failure in exc.exceptions}


# ---- the contract and the generated state machine ---------------------------------------


@pytest.mark.unit
def test_spec_is_valid_openapi_3_1_and_declares_five_links() -> None:
    validate(LIFECYCLE_SPEC)
    links = [
        name
        for _, _, response in iter_responses(LIFECYCLE_SPEC)
        for name in response.get("links", {})
    ]
    assert sorted(links) == [
        "DeleteItem",
        "GetAfterDelete",
        "GetAfterUpdate",
        "GetItem",
        "UpdateItem",
    ]


@pytest.mark.unit
def test_each_link_becomes_one_rule_and_only_post_is_a_root() -> None:
    assert rule_names(LIFECYCLE_SPEC) == EXPECTED_RULES


@pytest.mark.unit
def test_without_links_or_inference_the_machine_cannot_start() -> None:
    assert rule_names(spec_without_links()) == set()  # the class builds, with zero rules
    schema = load_schema(make_app(ItemStore()), spec_without_links(), config=stateful_config())
    with pytest.raises(NoLinksFound, match="no link definitions"):
        schema.as_state_machine()()  # Hypothesis refuses a rule-less machine on instantiation


@pytest.mark.unit
def test_dependency_analysis_infers_links_when_none_are_declared() -> None:
    inferred = rule_names(spec_without_links(), inference=True)
    assert "POST_items___201_GetItem__GET_items_item_id_" in inferred
    assert "RANDOM__POST_items" in inferred
    # Inference reads schemas, not semantics: it never invents DELETE 204 -> GET.
    assert not any(name.startswith("DELETE_items_item_id___204") for name in inferred)


@pytest.mark.unit
def test_dangling_operation_id_fails_when_the_machine_is_built() -> None:
    spec = copy.deepcopy(LIFECYCLE_SPEC)
    spec["paths"]["/items"]["post"]["responses"]["201"]["links"]["GetItem"]["operationId"] = (
        "getItm"
    )
    with pytest.raises(InvalidStateMachine, match="Operation 'getItm' not found"):
        rule_names(spec)


# ---- running the lifecycle ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("model", [False, True], ids=["builtin-checks", "with-model"])
def test_correct_api_survives_and_every_lifecycle_edge_fires(model: bool) -> None:
    seen = run_machine(Defects(), model=model).seen
    for edge in [("POST", 201), ("GET", 200), ("PATCH", 200), ("DELETE", 204), ("GET", 404)]:
        assert seen[edge] > 0, (edge, seen)


@pytest.mark.unit
def test_correct_api_survives_negative_mode_too() -> None:
    seen = run_machine(Defects(), negative=True).seen
    assert seen[("POST", 422)] > 0


@pytest.mark.unit
def test_delete_that_keeps_the_row_is_reported_as_use_after_free() -> None:
    with pytest.raises(FailureGroup) as caught:
        run_machine(Defects(keep_deleted=True))
    assert failure_types(caught.value) == {UseAfterFree}
    assert "ST301" in str(caught.value)


@pytest.mark.unit
def test_create_that_stores_nothing_is_reported_as_unavailable_resource() -> None:
    with pytest.raises(FailureGroup) as caught:
        run_machine(Defects(drop_creates=True))
    assert failure_types(caught.value) == {EnsureResourceAvailability}


@pytest.mark.unit
def test_builtin_checks_miss_a_lost_update() -> None:
    seen = run_machine(Defects(stale_patch=True)).seen
    assert seen[("PATCH", 200)] > 0  # the bug was exercised and nothing complained


@pytest.mark.unit
def test_model_check_catches_the_lost_update() -> None:
    with pytest.raises(AssertionError, match=r"lifecycle invariant broken: GET /items/\d+ -> "):
        run_machine(Defects(stale_patch=True), model=True)


UNGUESSABLE = 700_000_001  # first id far from the small integers Hypothesis tries first


def typo_spec() -> dict[str, Any]:
    spec = copy.deepcopy(LIFECYCLE_SPEC)
    for link in spec["paths"]["/items"]["post"]["responses"]["201"]["links"].values():
        link["parameters"] = {"path.item_id": "$response.body#/identifier"}
    return spec


@pytest.mark.unit
def test_typo_in_link_pointer_silently_hides_use_after_free() -> None:
    seen = run_machine(
        Defects(keep_deleted=True), spec=typo_spec(), first_id=UNGUESSABLE, constants=False
    ).seen
    assert seen[("DELETE", 204)] == 0  # DELETE only ever saw random ids
    assert seen[("DELETE", 404)] > 0


@pytest.mark.unit
def test_constants_harvested_from_app_source_can_stand_in_for_a_broken_link() -> None:
    # The literal UNGUESSABLE sits in this module, which defines the app object (CountingApp).
    with pytest.raises(FailureGroup) as caught:
        run_machine(Defects(keep_deleted=True), spec=typo_spec(), first_id=UNGUESSABLE)
    assert failure_types(caught.value) == {UseAfterFree}


@pytest.mark.unit
def test_with_small_sequential_ids_random_guesses_still_hit_live_rows() -> None:
    seen = run_machine(Defects(), spec=typo_spec(), constants=False).seen
    assert seen[("DELETE", 204)] > 0  # ids 1, 2, ... collide with the fuzzer's favourite values


@pytest.mark.unit
def test_working_links_find_use_after_free_even_with_unguessable_ids() -> None:
    with pytest.raises(FailureGroup) as caught:
        run_machine(Defects(keep_deleted=True), first_id=UNGUESSABLE, constants=False)
    assert failure_types(caught.value) == {UseAfterFree}


# ---- the app itself, driven directly ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("keep_deleted", "after_delete"), [(False, 404), (True, 200)])
async def test_app_lifecycle_over_asgi(keep_deleted: bool, after_delete: int) -> None:
    app = make_app(ItemStore(), Defects(keep_deleted=keep_deleted))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/items", json={"name": "a"})
        assert (created.status_code, created.headers["location"]) == (201, "/items/1")
        item_url = created.headers["location"]
        assert (await client.patch(item_url, json={"name": "b"})).json() == {"id": 1, "name": "b"}
        assert (await client.get(item_url)).json()["name"] == "b"
        assert (await client.delete(item_url)).status_code == 204
        assert (await client.get(item_url)).status_code == after_delete
        assert (await client.post("/items", json={"name": ""})).status_code == 422


# ---- runtime expressions and JSON Pointer --------------------------------------------------

EXCHANGE = Exchange(
    method="POST",
    url="http://test/items?trace=1",
    status=201,
    request_query={"trace": "1"},
    request_headers={"Content-Type": "application/json"},
    request_body={"name": "a"},
    response_headers={"Location": "/items/7"},
    response_body={"id": 7, "tags": ["x", "y"], "a/b": True, "m~n": None},
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("$method", "POST"),
        ("$statusCode", 201),
        ("$url", "http://test/items?trace=1"),
        ("$request.query.trace", "1"),
        ("$request.header.content-type", "application/json"),
        ("$request.body#/name", "a"),
        ("$response.header.LOCATION", "/items/7"),
        ("$response.body#/id", 7),
        ("$response.body#/tags/1", "y"),
        ("$response.body#/a~1b", True),
        ("$response.body#/m~0n", None),
    ],
)
def test_runtime_expressions_resolve_and_preserve_type(expression: str, expected: object) -> None:
    value = evaluate_expression(expression, EXCHANGE)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    "expression",
    [
        "$request.query.Trace",  # names are case-sensitive
        "$response.body#/identifier",
        "$response.body#/tags/2",
        "$response.query.trace",  # a response has no query
        "$request.header.bad header",  # not an RFC 9110 token
        "$response.body.id",  # body needs '#' and a JSON Pointer
    ],
)
def test_runtime_expressions_that_do_not_resolve(expression: str) -> None:
    with pytest.raises(Unresolved):
        evaluate_expression(expression, EXCHANGE)


@pytest.mark.unit
def test_embedded_expressions_always_produce_a_string() -> None:
    assert evaluate_template("/items/{$response.body#/id}", EXCHANGE) == "/items/7"


@pytest.mark.unit
def test_tilde_escapes_decode_in_rfc_6901_order() -> None:
    assert resolve_pointer({"~1": "tilde-one", "/": "slash"}, "/~01") == "tilde-one"
    assert schemathesis_pointer({"~1": "tilde-one", "/": "slash"}, "/~01") == "tilde-one"


@pytest.mark.unit
@pytest.mark.parametrize(("pointer", "lenient"), [("/-1", 30), ("/01", 20), ("/ 1", 20)])
def test_schemathesis_accepts_array_indexes_rfc_6901_forbids(pointer: str, lenient: int) -> None:
    document = [10, 20, 30]
    assert schemathesis_pointer(document, pointer) == lenient
    with pytest.raises(Unresolved):
        resolve_pointer(document, pointer)
    assert schemathesis_pointer(document, "/3") is UNRESOLVABLE
