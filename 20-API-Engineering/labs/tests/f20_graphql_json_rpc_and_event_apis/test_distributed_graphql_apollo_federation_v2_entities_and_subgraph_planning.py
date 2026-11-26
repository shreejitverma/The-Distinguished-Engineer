"""Lab tests for Apollo Federation v2 entities, composition, query plans, and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio

from api_labs.f20_graphql_json_rpc_and_event_apis.apollo_federation_entities_and_planning import (
    CompositionFailedError,
    OrderRow,
    PlanError,
    Router,
    SubgraphContext,
    build_orders_schema,
    build_stub_schema,
    build_subgraph_app,
    build_users_schema,
    compose,
    parse_subgraph_sdl,
    sample_orders,
    sample_users,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ENTITIES_QUERY = "query($r: [_Any!]!) { _entities(representations: $r) { ... on User { id %s } } }"


@dataclass
class Stack:
    router: Router
    users: SubgraphContext
    orders: SubgraphContext


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[Stack]:
    users = SubgraphContext(users=sample_users())
    orders = SubgraphContext(orders=sample_orders())
    users_app = build_subgraph_app(build_users_schema(), users)
    orders_app = build_subgraph_app(build_orders_schema(), orders)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=users_app), base_url="http://users"
        ) as users_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=orders_app), base_url="http://orders"
        ) as orders_client,
    ):
        router = Router({"users": users_client, "orders": orders_client})
        await router.compose_from_subgraphs()
        yield Stack(router, users, orders)


async def _sdl(schema: Any) -> str:
    result = await schema.execute("{ _service { sdl } }")
    assert result.errors is None
    sdl: str = result.data["_service"]["sdl"]
    return sdl


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orders_subgraph_sdl_declares_key_external_and_requires() -> None:
    sdl = await _sdl(build_orders_schema())
    assert 'type User @key(fields: "id")' in sdl
    assert "region: String @external" in sdl
    assert 'taxNote: String! @requires(fields: "region")' in sdl
    assert "union _Entity = User" in sdl
    assert "_entities(representations: [_Any!]!): [_Entity]!" in sdl
    assert 'import: ["@external", "@key", "@requires"]' in sdl


@pytest.mark.unit
@pytest.mark.asyncio
async def test_entities_keeps_representation_order_and_nulls_missing_entities() -> None:
    context = SubgraphContext(users=sample_users())
    reps = [
        {"__typename": "User", "id": "2"},
        {"__typename": "User", "id": "9"},
        {"__typename": "User", "id": "1"},
    ]
    result = await build_users_schema().execute(
        ENTITIES_QUERY % "name", variable_values={"r": reps}, context_value=context
    )
    assert result.errors is None
    assert result.data == {
        "_entities": [{"id": "2", "name": "Grace"}, None, {"id": "1", "name": "Ada"}]
    }
    assert context.reference_calls == ["2", "9", "1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strawberry_calls_resolve_reference_once_per_representation() -> None:
    context = SubgraphContext(users=sample_users())
    reps = [{"__typename": "User", "id": "1"} for _ in range(3)]
    result = await build_users_schema().execute(
        ENTITIES_QUERY % "name", variable_values={"r": reps}, context_value=context
    )
    assert result.errors is None
    assert context.reference_calls == ["1", "1", "1"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("representation", "fragment"),
    [({"__typename": "Nope", "id": "1"}, "Nope"), ({"id": "1"}, "__typename")],
)
async def test_bad_typename_fails_the_whole_entities_field(
    representation: dict[str, str], fragment: str
) -> None:
    context = SubgraphContext(users=sample_users())
    reps = [{"__typename": "User", "id": "1"}, representation]
    result = await build_users_schema().execute(
        ENTITIES_QUERY % "name", variable_values={"r": reps}, context_value=context
    )
    assert result.data is None
    assert result.errors is not None
    assert fragment in result.errors[0].message
    assert result.errors[0].path == ["_entities"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_reference_error_is_isolated_to_its_item() -> None:
    context = SubgraphContext()
    reps = [{"__typename": "User", "id": "1"}, {"__typename": "User"}]
    result = await build_orders_schema().execute(
        ENTITIES_QUERY % "", variable_values={"r": reps}, context_value=context
    )
    assert result.data == {"_entities": [{"id": "1"}, None]}
    assert result.errors is not None
    assert [e.path for e in result.errors] == [["_entities", 1]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tax_note_fails_when_representation_lacks_the_required_region() -> None:
    context = SubgraphContext()
    without = [{"__typename": "User", "id": "1"}]
    with_region = [{"__typename": "User", "id": "1", "region": "EU"}]
    schema = build_orders_schema()
    bad = await schema.execute(
        ENTITIES_QUERY % "taxNote", variable_values={"r": without}, context_value=context
    )
    good = await schema.execute(
        ENTITIES_QUERY % "taxNote", variable_values={"r": with_region}, context_value=context
    )
    assert bad.errors is not None
    assert "region missing" in bad.errors[0].message
    assert good.data == {"_entities": [{"id": "1", "taxNote": "VAT 20%"}]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strawberry_keeps_resolvable_false_type_in_entity_union() -> None:
    schema = build_stub_schema()
    sdl = await _sdl(schema)
    assert 'type User @key(fields: "id", resolvable: false)' in sdl
    assert "union _Entity = User" in sdl
    result = await schema.execute(
        ENTITIES_QUERY % "", variable_values={"r": [{"__typename": "User", "id": "1"}]}
    )
    assert result.data == {"_entities": [{"id": "1"}]}


def _schema(name: str, sdl: str) -> Any:
    return parse_subgraph_sdl(name, sdl)


@pytest.mark.unit
def test_composition_rejects_unshared_fields_defined_in_two_subgraphs() -> None:
    sdl = "type Position { x: Int! y: Int! }"
    with pytest.raises(CompositionFailedError) as caught:
        compose({"a": _schema("a", sdl), "b": _schema("b", sdl)})
    assert caught.value.codes == ["INVALID_FIELD_SHARING", "INVALID_FIELD_SHARING"]
    shared = "type Position @shareable { x: Int! y: Int! }"
    composed = compose({"a": _schema("a", shared), "b": _schema("b", shared)})
    assert composed.owners("Position", "x") == ["a", "b"]


@pytest.mark.unit
def test_composition_allows_key_fields_in_every_subgraph() -> None:
    a = 'type P @key(fields: "id") { id: ID! name: String }'
    b = 'type P @key(fields: "id") { id: ID! stock: Int }'
    composed = compose({"a": _schema("a", a), "b": _schema("b", b)})
    assert composed.owners("P", "id") == ["a", "b"]
    assert composed.owners("P", "name") == ["a"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sdl_a", "sdl_b", "code"),
    [
        (
            'type P @key(fields: "id") { id: ID! }',
            'type P @key(fields: "id") { id: ID! size: Int @external }',
            "EXTERNAL_MISSING_ON_BASE",
        ),
        (
            'type P @key(fields: "id") { id: ID! }',
            'type P @key(fields: "id") { id: ID! est: Int @requires(fields: "weight") }',
            "REQUIRES_FIELDS_MISSING_EXTERNAL",
        ),
        (
            'type P @key(fields: "id") { id: ID! }',
            'type Q @key(fields: "sku") { id: ID! }',
            "KEY_INVALID_FIELDS",
        ),
    ],
)
def test_composition_reports_the_apollo_error_code(sdl_a: str, sdl_b: str, code: str) -> None:
    with pytest.raises(CompositionFailedError) as caught:
        compose({"a": _schema("a", sdl_a), "b": _schema("b", sdl_b)})
    assert caught.value.codes == [code]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_composes_ownership_from_service_sdl(stack: Stack) -> None:
    supergraph = stack.router.supergraph
    assert supergraph is not None
    assert supergraph.owners("User", "region") == ["users"]
    assert supergraph.owners("User", "orders") == ["orders"]
    assert supergraph.owners("User", "taxNote") == ["orders"]
    assert supergraph.owners("User", "id") == ["orders", "users"]
    assert supergraph.owners("Query", "recentOrders") == ["orders"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_joins_users_to_orders_through_an_entities_flatten(stack: Stack) -> None:
    plan = stack.router.plan("{ users { name orders { totalCents } } }")
    assert plan.render() == "\n".join(
        [
            "QueryPlan {",
            "  Sequence {",
            '    Fetch(service: "users") { { users { __typename name id } } },',
            '    Flatten(path: "users.@") {',
            '      Fetch(service: "orders") {',
            "        { ... on User { __typename id } } =>",
            "        { ... on User { orders { totalCents } } }",
            "      },",
            "    },",
            "  },",
            "}",
        ]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_runs_independent_root_fields_in_parallel(stack: Stack) -> None:
    plan = stack.router.plan("{ users { name } recentOrders { totalCents } }")
    assert [len(level) for level in plan.levels] == [2]
    assert plan.render() == "\n".join(
        [
            "QueryPlan {",
            "  Parallel {",
            '    Fetch(service: "users") { { users { name } } },',
            '    Fetch(service: "orders") { { recentOrders { totalCents } } },',
            "  },",
            "}",
        ]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_for_requires_detours_through_the_owner_of_the_external_field(
    stack: Stack,
) -> None:
    plan = stack.router.plan("{ recentOrders { id buyer { taxNote } } }")
    assert [[g.subgraph for g in level] for level in plan.levels] == [
        ["orders"],
        ["users"],
        ["orders"],
    ]
    assert plan.render() == "\n".join(
        [
            "QueryPlan {",
            "  Sequence {",
            '    Fetch(service: "orders") { { recentOrders { id buyer { __typename id } } } },',
            '    Flatten(path: "recentOrders.@.buyer") {',
            '      Fetch(service: "users") {',
            "        { ... on User { __typename id } } =>",
            "        { ... on User { region } }",
            "      },",
            "    },",
            '    Flatten(path: "recentOrders.@.buyer") {',
            '      Fetch(service: "orders") {',
            "        { ... on User { __typename id region } } =>",
            "        { ... on User { taxNote } }",
            "      },",
            "    },",
            "  },",
            "}",
        ]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plan_rejects_fields_no_subgraph_defines(stack: Stack) -> None:
    with pytest.raises(PlanError, match=r"User\.nickname"):
        stack.router.plan("{ users { nickname } }")
    with pytest.raises(PlanError, match=r"Query\.nope"):
        stack.router.plan("{ nope }")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_stitches_entity_fields_from_two_subgraphs(stack: Stack) -> None:
    result = await stack.router.execute("{ users { id name orders { id totalCents } } }")
    assert result.errors == []
    assert result.data == {
        "users": [
            {
                "id": "1",
                "name": "Ada",
                "orders": [
                    {"id": "o1", "totalCents": 1200},
                    {"id": "o3", "totalCents": 300},
                ],
            },
            {
                "id": "2",
                "name": "Grace",
                "orders": [
                    {"id": "o2", "totalCents": 800},
                    {"id": "o4", "totalCents": 99},
                ],
            },
        ]
    }
    assert [(f.subgraph, f.entity, f.representations) for f in result.fetches] == [
        ("users", False, 0),
        ("orders", True, 2),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_deduplicates_representations_before_calling_the_subgraph(
    stack: Stack,
) -> None:
    result = await stack.router.execute("{ recentOrders { id buyer { name } } }")
    assert result.errors == []
    assert [o["buyer"]["name"] for o in result.data["recentOrders"]] == [
        "Ada",
        "Grace",
        "Ada",
        "Grace",
    ]
    assert [f.representations for f in result.fetches if f.entity] == [2]
    assert stack.users.reference_calls == ["1", "2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_requires_sends_the_external_value_in_the_representation(stack: Stack) -> None:
    result = await stack.router.execute("{ recentOrders { id buyer { taxNote } } }")
    assert result.errors == []
    assert [o["buyer"]["taxNote"] for o in result.data["recentOrders"]] == [
        "VAT 20%",
        "no VAT",
        "VAT 20%",
        "no VAT",
    ]
    assert [(f.subgraph, f.entity, f.representations) for f in result.fetches] == [
        ("orders", False, 0),
        ("users", True, 2),
        ("orders", True, 2),
    ]
    assert stack.orders.reference_calls == ["1", "2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_entity_leaves_its_field_null_without_hiding_the_others(
    stack: Stack,
) -> None:
    stack.orders.orders.append(OrderRow("o9", "9", 5))
    result = await stack.router.execute("{ recentOrders { id buyer { name } } }")
    names = {o["id"]: o["buyer"]["name"] for o in result.data["recentOrders"]}
    assert names == {"o1": "Ada", "o2": "Grace", "o3": "Ada", "o4": "Grace", "o9": None}
    assert result.errors == []
