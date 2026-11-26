"""Strawberry 0.340.0 with FastAPI: types, context, union mutations, WebSocket subscriptions."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast

import httpx
import pytest
import strawberry
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api_labs.f20_graphql_json_rpc_and_event_apis.strawberry_orders_api import (
    WS_TOKEN,
    InsufficientInventoryError,
    Order,
    OrderBroker,
    OrderCreated,
    QuantityInvalid,
    Store,
    build_app,
    build_leaky_app,
    build_schema,
)

CREATE = """
mutation Create($i: CreateOrderInput!) {
  createOrder(input: $i) {
    __typename
    ... on OrderCreated { order { id sku quantity } }
    ... on InsufficientInventoryError { sku requested available }
    ... on QuantityInvalid { message }
  }
}
"""
SUBPROTOCOL = "graphql-transport-ws"


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def gql(
    client: httpx.AsyncClient,
    query: str,
    variables: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/graphql", json={"query": query, "variables": variables}, headers=headers
    )


async def create(client: httpx.AsyncClient, sku: str, quantity: Any, **kw: Any) -> httpx.Response:
    return await gql(client, CREATE, {"i": {"sku": sku, "quantity": quantity}}, **kw)


def init(ws: Any) -> None:
    ws.send_json({"type": "connection_init", "payload": {"token": WS_TOKEN}})
    assert ws.receive_json() == {"type": "connection_ack"}


def subscribe(ws: Any, op_id: str, query: str) -> None:
    ws.send_json({"id": op_id, "type": "subscribe", "payload": {"query": query}})


def expect_close(ws: Any) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as info:
        ws.receive_json()
    return info.value


def wait_until(predicate: Any, attempts: int = 200) -> bool:
    """Poll from the test thread; the app runs on the TestClient portal's loop."""
    import time

    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


@pytest.mark.unit
def test_sdl_has_input_object_union_and_camel_case_fields() -> None:
    sdl = str(build_schema())
    assert "input CreateOrderInput {\n  sku: String!\n  quantity: Int!\n}" in sdl
    assert (
        "union CreateOrderResult = OrderCreated | InsufficientInventoryError | QuantityInvalid"
        in sdl
    )
    assert "createOrder(input: CreateOrderInput!): CreateOrderResult!" in sdl
    assert "subscriberCount: Int!" in sdl
    assert "orderEvents(sku: String = null): OrderCreated!" in sdl


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_mutation_returns_union_member_and_header_from_context_response() -> None:
    app, store = build_app()
    async with client_for(app) as client:
        resp = await create(client, "WIDGET", 2)
    assert resp.status_code == 200
    assert resp.json() == {
        "data": {
            "createOrder": {
                "__typename": "OrderCreated",
                "order": {"id": "ord-1", "sku": "WIDGET", "quantity": 2},
            }
        }
    }
    assert resp.headers["x-order-id"] == "ord-1"
    assert store.stock["acme"]["WIDGET"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_domain_failure_is_data_with_http_200_and_no_errors_key() -> None:
    app, store = build_app()
    async with client_for(app) as client:
        resp = await create(client, "GADGET", 2)
    body = resp.json()
    assert resp.status_code == 200
    assert "errors" not in body
    assert body["data"]["createOrder"] == {
        "__typename": "InsufficientInventoryError",
        "sku": "GADGET",
        "requested": 2,
        "available": 0,
    }
    assert store.orders == []
    assert "x-order-id" not in resp.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_business_rule_member_is_distinct_from_inventory_member() -> None:
    app, _ = build_app()
    async with client_for(app) as client:
        resp = await create(client, "WIDGET", 0)
    assert resp.json()["data"]["createOrder"] == {
        "__typename": "QuantityInvalid",
        "message": "quantity must be >= 1",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_input_type_violations_are_rejected_before_the_resolver_runs() -> None:
    app, store = build_app()
    async with client_for(app) as client:
        missing = await gql(client, CREATE, {"i": {"sku": "WIDGET"}})
        wrong_type = await create(client, "WIDGET", "two")
    for resp in (missing, wrong_type):
        body = resp.json()
        assert resp.status_code == 200
        assert body["data"] is None
        assert "Variable '$i' has invalid value" in body["errors"][0]["message"]
        assert "path" not in body["errors"][0]
    assert store.stock["acme"]["WIDGET"] == 5
    assert store.orders == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_getter_reads_header_per_request_and_tenants_are_isolated() -> None:
    app, _ = build_app()
    async with client_for(app) as client:
        default = await gql(client, "{ whoami }")
        globex_widget = await gql(
            client, '{ inventory(sku: "WIDGET") }', headers={"x-tenant": "globex"}
        )
        acme_widget = await gql(
            client, '{ inventory(sku: "WIDGET") }', headers={"x-tenant": "acme"}
        )
    assert default.json()["data"] == {"whoami": "acme"}
    assert globex_widget.json()["data"] == {"inventory": 1}
    assert acme_widget.json()["data"] == {"inventory": 5}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_injects_the_http_request_into_the_returned_context() -> None:
    app, _ = build_app()
    async with client_for(app) as client:
        resp = await gql(client, "{ requestPath }")
    assert resp.json()["data"] == {"requestPath": "/graphql"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_context_instance_leaks_the_last_request_into_earlier_ones() -> None:
    results: dict[bool, tuple[str, str]] = {}
    for per_request in (False, True):
        app, shared = build_leaky_app(per_request=per_request)
        async with client_for(app) as client:
            q = {"query": "{ tenantAfterGate }"}
            first = asyncio.create_task(client.post("/graphql", json=q, headers={"x-tenant": "A"}))
            await asyncio.sleep(0.05)
            second = asyncio.create_task(client.post("/graphql", json=q, headers={"x-tenant": "B"}))
            await asyncio.sleep(0.05)
            shared.gate.set()
            a, b = await first, await second
        results[per_request] = (
            a.json()["data"]["tenantAfterGate"],
            b.json()["data"]["tenantAfterGate"],
        )
    assert results[True] == ("A", "B")
    assert results[False] == ("B", "B")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mutations_over_get_and_batches_are_refused_with_plain_400s() -> None:
    app, store = build_app()
    async with client_for(app) as client:
        get_mutation = await client.get(
            "/graphql",
            params={
                "query": 'mutation { createOrder(input: {sku: "W", quantity: 1}) { __typename } }'
            },
        )
        batch = await client.post("/graphql", json=[{"query": "{ whoami }"}])
        get_query = await client.get("/graphql", params={"query": "{ whoami }"})
    assert get_mutation.status_code == 400
    assert get_mutation.text == "mutations are not allowed when using GET"
    assert store.orders == []
    assert batch.status_code == 400
    assert batch.text == "Batching is not enabled"
    assert get_query.json() == {"data": {"whoami": "acme"}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_level_rejections_use_http_status_not_graphql_errors() -> None:
    app, _ = build_app()
    async with client_for(app) as client:
        bad_json = await client.post(
            "/graphql", content="{", headers={"content-type": "application/json"}
        )
        bad_type = await client.post(
            "/graphql", content="x", headers={"content-type": "text/plain"}
        )
        bad_method = await client.put("/graphql", json={})
        validation = await gql(client, "{ nope }")
    assert (bad_json.status_code, bad_json.text) == (400, "Unable to parse request body as JSON")
    assert (bad_type.status_code, bad_type.text) == (400, "Unsupported content type")
    assert bad_method.status_code == 405
    assert validation.status_code == 200
    assert validation.json()["errors"][0]["message"] == "Cannot query field 'nope' on type 'Query'."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unmasked_resolver_exception_leaks_message_and_mask_errors_hides_it() -> None:
    plain_app, _ = build_app()
    masked_app, _ = build_app(mask_errors=True)
    async with client_for(plain_app) as plain, client_for(masked_app) as masked:
        leaked = await gql(plain, "mutation { explode }")
        hidden = await gql(masked, "mutation { explode }")
    assert leaked.status_code == hidden.status_code == 200
    assert leaked.json()["errors"][0]["message"] == "db password=hunter2 refused connection"
    assert hidden.json()["errors"][0]["message"] == "Unexpected error."
    assert hidden.json()["errors"][0]["path"] == ["explode"]
    assert hidden.json()["data"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphiql_is_served_on_html_get_and_404s_when_disabled() -> None:
    on_app, _ = build_app()
    off_app, _ = build_app(graphql_ide=None)
    async with client_for(on_app) as on, client_for(off_app) as off:
        page = await on.get("/graphql", headers={"accept": "text/html"})
        gone = await off.get("/graphql", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert gone.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_union_resolver_returning_a_non_member_type_is_a_field_error() -> None:
    @strawberry.type
    class Q:
        @strawberry.field
        def bad(self) -> Annotated[OrderCreated | QuantityInvalid, strawberry.union("Bad")]:
            return cast(
                "OrderCreated", InsufficientInventoryError(sku="x", requested=1, available=0)
            )

    result = await strawberry.Schema(query=Q).execute("{ bad { __typename } }")
    assert result.data is None
    assert result.errors is not None
    assert "InsufficientInventoryError" in result.errors[0].message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_broker_drops_oldest_event_when_a_subscriber_queue_is_full() -> None:
    broker = OrderBroker(maxsize=2)
    queue = broker.subscribe()
    events = [
        OrderCreated(order=Order(id=strawberry.ID(f"o{i}"), tenant="t", sku="s", quantity=1))
        for i in range(4)
    ]
    for event in events:
        broker.publish(event)
    assert [queue.get_nowait().order.id for _ in range(queue.qsize())] == ["o2", "o3"]
    assert broker.dropped == 2
    broker.unsubscribe(queue)
    broker.unsubscribe(queue)  # idempotent
    assert broker.subscriber_count == 0


@pytest.mark.unit
def test_subscription_receives_mutation_events_and_complete_unsubscribes() -> None:
    app, store = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        init(ws)
        subscribe(ws, "1", "subscription { orderEvents { order { id sku } } }")
        assert wait_until(lambda: store.broker.subscriber_count == 1)
        resp = client.post(
            "/graphql", json={"query": CREATE, "variables": {"i": {"sku": "WIDGET", "quantity": 1}}}
        )
        assert resp.json()["data"]["createOrder"]["__typename"] == "OrderCreated"
        assert ws.receive_json() == {
            "id": "1",
            "type": "next",
            "payload": {"data": {"orderEvents": {"order": {"id": "ord-1", "sku": "WIDGET"}}}},
        }
        ws.send_json({"id": "1", "type": "complete"})
        assert wait_until(lambda: store.broker.subscriber_count == 0)


@pytest.mark.unit
def test_subscription_filters_by_argument_and_by_context_tenant() -> None:
    app, store = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        init(ws)
        subscribe(
            ws, "1", 'subscription { orderEvents(sku: "WIDGET") { order { id tenant sku } } }'
        )
        assert wait_until(lambda: store.broker.subscriber_count == 1)
        mutate = {"query": CREATE}
        client.post(
            "/graphql",
            json={**mutate, "variables": {"i": {"sku": "WIDGET", "quantity": 1}}},
            headers={"x-tenant": "globex"},
        )
        client.post(
            "/graphql", json={**mutate, "variables": {"i": {"sku": "GADGET", "quantity": 1}}}
        )
        stocked = client.post(
            "/graphql", json={**mutate, "variables": {"i": {"sku": "WIDGET", "quantity": 1}}}
        )
        assert stocked.json()["data"]["createOrder"]["order"]["id"] == "ord-2"
        msg = ws.receive_json()
        assert msg["payload"]["data"]["orderEvents"] == {
            "order": {"id": "ord-2", "tenant": "acme", "sku": "WIDGET"}
        }
    assert [o.tenant for o in store.orders] == ["globex", "acme"]


@pytest.mark.unit
def test_disconnect_runs_the_generator_finally_block() -> None:
    app, store = build_app()
    with TestClient(app) as client:
        with client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws:
            init(ws)
            subscribe(ws, "1", "subscription { orderEvents { order { id } } }")
            subscribe(ws, "2", "subscription { orderEvents { order { id } } }")
            assert wait_until(lambda: store.broker.subscriber_count == 2)
        assert store.broker.subscriber_count == 0


@pytest.mark.unit
def test_finite_subscription_ends_with_complete_and_frees_the_operation_id() -> None:
    app, _ = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        init(ws)
        for _ in range(2):  # the id "7" is reusable once complete was sent
            subscribe(ws, "7", "subscription { countdown(start: 2) }")
            frames = [ws.receive_json() for _ in range(3)]
            assert frames == [
                {"id": "7", "type": "next", "payload": {"data": {"countdown": 2}}},
                {"id": "7", "type": "next", "payload": {"data": {"countdown": 1}}},
                {"id": "7", "type": "complete"},
            ]


@pytest.mark.unit
def test_queries_run_over_the_websocket_too_and_ping_gets_pong() -> None:
    app, _ = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        init(ws)
        subscribe(ws, "q", "query { whoami }")
        assert ws.receive_json() == {
            "id": "q",
            "type": "next",
            "payload": {"data": {"whoami": "acme"}},
        }
        assert ws.receive_json() == {"id": "q", "type": "complete"}


@pytest.mark.unit
def test_invalid_subscription_document_yields_error_message_not_a_close() -> None:
    app, _ = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        init(ws)
        subscribe(ws, "1", "subscription { nope }")
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["payload"][0]["message"] == "Cannot query field 'nope' on type 'Subscription'."
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("frames", "subprotocols", "code", "reason"),
    [
        (
            [{"id": "1", "type": "subscribe", "payload": {"query": "subscription { countdown }"}}],
            [SUBPROTOCOL],
            4401,
            "Unauthorized",
        ),
        (
            [{"type": "connection_init", "payload": {"token": "wrong"}}],
            [SUBPROTOCOL],
            4403,
            "Forbidden",
        ),
        (
            [{"type": "connection_init", "payload": {"token": WS_TOKEN}}] * 2,
            [SUBPROTOCOL],
            4429,
            "Too many initialisation requests",
        ),
        ([], ["not-a-graphql-protocol"], 4406, "Subprotocol not acceptable"),
    ],
    ids=["subscribe-before-init", "bad-token", "double-init", "unknown-subprotocol"],
)
def test_protocol_violations_close_with_documented_4xxx_codes(
    frames: list[dict[str, Any]], subprotocols: list[str], code: int, reason: str
) -> None:
    app, _ = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=subprotocols) as ws,
    ):
        for index, frame in enumerate(frames):
            ws.send_json(frame)
            if frame["type"] == "connection_init" and index == 0 and code == 4429:
                assert ws.receive_json() == {"type": "connection_ack"}
        closed = expect_close(ws)
    assert (closed.code, closed.reason) == (code, reason)


@pytest.mark.unit
def test_duplicate_operation_id_closes_4409() -> None:
    app, store = build_app()
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        init(ws)
        for _ in range(2):
            subscribe(ws, "1", "subscription { orderEvents { order { id } } }")
        closed = expect_close(ws)
    assert (closed.code, closed.reason) == (4409, "Subscriber for 1 already exists")
    assert store.broker.subscriber_count == 0


@pytest.mark.unit
def test_max_subscriptions_per_connection_is_enforced_as_an_error_message() -> None:
    app, store = build_app(max_subscriptions_per_connection=1)
    with (
        TestClient(app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL]) as ws,
    ):
        init(ws)
        subscribe(ws, "1", "subscription { orderEvents { order { id } } }")
        subscribe(ws, "2", "subscription { orderEvents { order { id } } }")
        msg = ws.receive_json()
        assert msg == {
            "id": "2",
            "type": "error",
            "payload": [{"message": "Subscription limit reached"}],
        }
        assert wait_until(lambda: store.broker.subscriber_count == 1)


@pytest.mark.unit
def test_cross_origin_handshake_is_rejected_unless_allowed_or_same_origin() -> None:
    evil = {"origin": "https://evil.example"}
    app, _ = build_app()
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL], headers=evil),
        ):
            pass
        with client.websocket_connect(
            "/graphql", subprotocols=[SUBPROTOCOL], headers={"origin": "http://testserver"}
        ) as ws:
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
    assert refused.value.code == 1008
    allowed_app, _ = build_app(allowed_origins=["https://evil.example"])
    with (
        TestClient(allowed_app) as client,
        client.websocket_connect("/graphql", subprotocols=[SUBPROTOCOL], headers=evil) as ws,
    ):
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.unit
def test_store_is_shared_so_http_and_websocket_see_one_inventory() -> None:
    shared = Store()
    app, store = build_app(shared)
    assert store is shared
    with TestClient(app) as client:
        client.post(
            "/graphql", json={"query": CREATE, "variables": {"i": {"sku": "WIDGET", "quantity": 5}}}
        )
        sold_out = client.post(
            "/graphql", json={"query": CREATE, "variables": {"i": {"sku": "WIDGET", "quantity": 1}}}
        )
    assert sold_out.json()["data"]["createOrder"]["available"] == 0
