"""AIP-136 custom methods, AIP-216 transitions, AIP-155 replay, and `:verb` routing traps."""

from collections.abc import AsyncIterator
from urllib.parse import urljoin

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from api_labs.f04_rest_and_resource_modeling.order_state_machine import (
    VERBS,
    InvalidTransitionError,
    OrderStore,
    State,
    Verb,
    build_app,
    next_state,
)

RID_1 = "6f1c1b7e-2f0a-4c52-9a35-0d6c4a1e9b01"
RID_2 = "0b9e7d3c-5a41-4e8f-8c2d-7a6b5c4d3e02"

# Every (state, verb) pair: the target state, or None when the transition is illegal.
EXPECTED: dict[tuple[State, Verb], State | None] = {
    (State.PENDING_PAYMENT, "pay"): State.PAID,
    (State.PENDING_PAYMENT, "ship"): None,
    (State.PENDING_PAYMENT, "deliver"): None,
    (State.PENDING_PAYMENT, "cancel"): State.CANCELLED,
    (State.PAID, "pay"): None,
    (State.PAID, "ship"): State.SHIPPED,
    (State.PAID, "deliver"): None,
    (State.PAID, "cancel"): State.CANCELLED,
    (State.SHIPPED, "pay"): None,
    (State.SHIPPED, "ship"): None,
    (State.SHIPPED, "deliver"): State.DELIVERED,
    (State.SHIPPED, "cancel"): None,
    (State.DELIVERED, "pay"): None,
    (State.DELIVERED, "ship"): None,
    (State.DELIVERED, "deliver"): None,
    (State.DELIVERED, "cancel"): None,
    (State.CANCELLED, "pay"): None,
    (State.CANCELLED, "ship"): None,
    (State.CANCELLED, "deliver"): None,
    (State.CANCELLED, "cancel"): None,
}


@pytest_asyncio.fixture
async def store_and_client() -> AsyncIterator[tuple[OrderStore, httpx.AsyncClient]]:
    store = OrderStore()
    app = build_app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/v1/orders", params={"order_id": "ord-1"})
        assert created.status_code == 201
        yield store, client


@pytest.mark.unit
def test_transition_table_covers_every_state_and_verb() -> None:
    assert len(EXPECTED) == len(State) * len(VERBS)
    for (state, verb), target in EXPECTED.items():
        if target is None:
            with pytest.raises(InvalidTransitionError):
                next_state(state, verb)
        else:
            assert next_state(state, verb) is target


@pytest.mark.unit
@pytest.mark.asyncio
async def test_colon_verb_routes_to_custom_method_and_returns_resource(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    paid = await client.post("/v1/orders/ord-1:pay", json={})
    assert paid.status_code == 200
    assert paid.json() == {"name": "orders/ord-1", "state": "PAID", "etag": '"2"'}
    fetched = await client.get("/v1/orders/ord-1")
    assert fetched.json()["state"] == "PAID"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_custom_method_accepts_an_empty_body(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    response = await client.post("/v1/orders/ord-1:cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "CANCELLED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_illegal_transition_is_failed_precondition_400(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    await client.post("/v1/orders/ord-1:pay")
    await client.post("/v1/orders/ord-1:ship")
    response = await client.post("/v1/orders/ord-1:cancel")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["status"] == "FAILED_PRECONDITION"
    assert error["code"] == 400
    assert error["details"][0]["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
    assert error["details"][0]["domain"] == "orders.example.com"
    assert error["details"][0]["reason"] == "INVALID_STATE_TRANSITION"
    assert error["details"][0]["metadata"] == {
        "state": "SHIPPED",
        "verb": "cancel",
        "allowedFrom": "PAID,PENDING_PAYMENT",
    }
    assert (await client.get("/v1/orders/ord-1")).json()["state"] == "SHIPPED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_with_same_request_id_replays_instead_of_failing(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    store, client = store_and_client
    first = await client.post("/v1/orders/ord-1:cancel", json={"request_id": RID_1})
    retry = await client.post("/v1/orders/ord-1:cancel", json={"request_id": RID_1})
    assert first.status_code == retry.status_code == 200
    assert retry.json() == first.json()
    assert store.transitions_applied == 1
    # The same retry without a request_id is a fresh call and hits the state machine.
    blind_retry = await client.post("/v1/orders/ord-1:cancel")
    assert blind_retry.status_code == 400
    assert blind_retry.json()["error"]["status"] == "FAILED_PRECONDITION"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_returns_the_historical_response_not_the_current_state(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    await client.post("/v1/orders/ord-1:pay", json={"request_id": RID_1})
    await client.post("/v1/orders/ord-1:ship")
    replay = await client.post("/v1/orders/ord-1:pay", json={"request_id": RID_1})
    assert replay.status_code == 200
    assert replay.json() == {"name": "orders/ord-1", "state": "PAID", "etag": '"2"'}
    assert (await client.get("/v1/orders/ord-1")).json()["state"] == "SHIPPED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_id_reused_for_another_request_is_rejected(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    await client.post("/v1/orders", params={"order_id": "ord-2"})
    await client.post("/v1/orders/ord-1:cancel", json={"request_id": RID_1})
    reused = await client.post("/v1/orders/ord-2:cancel", json={"request_id": RID_1})
    assert reused.status_code == 400
    assert reused.json()["error"]["details"][0]["reason"] == "REQUEST_ID_REUSED"
    assert (await client.get("/v1/orders/ord-2")).json()["state"] == "PENDING_PAYMENT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_request_id_is_rejected_instead_of_disabling_idempotency(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    store, client = store_and_client
    typo = await client.post("/v1/orders/ord-1:cancel", json={"requestId": RID_1})
    not_uuid = await client.post("/v1/orders/ord-1:cancel", json={"request_id": "not-a-uuid"})
    assert (typo.status_code, typo.json()["detail"][0]["type"]) == (422, "extra_forbidden")
    assert (not_uuid.status_code, not_uuid.json()["detail"][0]["type"]) == (422, "uuid_parsing")
    assert store.orders["ord-1"].state is State.PENDING_PAYMENT
    assert store.transitions_applied == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_etag_is_aborted_409_and_changes_nothing(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    seen = (await client.get("/v1/orders/ord-1")).json()["etag"]
    await client.post("/v1/orders/ord-1:pay")  # someone else moves the order on
    stale = await client.post("/v1/orders/ord-1:cancel", json={"etag": seen})
    assert stale.status_code == 409
    assert stale.json()["error"]["status"] == "ABORTED"
    assert (await client.get("/v1/orders/ord-1")).json()["state"] == "PAID"
    fresh = await client.post("/v1/orders/ord-1:cancel", json={"etag": '"2"', "request_id": RID_2})
    assert fresh.json()["state"] == "CANCELLED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_state_is_output_only_on_create_and_errors_use_google_rpc_codes() -> None:
    app = build_app(OrderStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/orders", params={"order_id": "ord-9"}, json={"state": "DELIVERED"}
        )
        duplicate = await client.post("/v1/orders", params={"order_id": "ord-9"})
        missing = await client.post("/v1/orders/ord-404:pay")
        missing_get = await client.get("/v1/orders/ord-404")
    assert created.status_code == 201
    assert created.headers["location"] == "/v1/orders/ord-9"
    assert created.json()["state"] == "PENDING_PAYMENT"
    assert (duplicate.status_code, duplicate.json()["error"]["status"]) == (409, "ALREADY_EXISTS")
    assert (missing.status_code, missing.json()["error"]["status"]) == (404, "NOT_FOUND")
    assert missing_get.json() == missing.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_on_a_custom_method_uri_is_captured_by_the_item_route() -> None:
    naive = FastAPI()

    @naive.get("/v1/orders/{order_id}")
    async def get_order(order_id: str) -> dict[str, str]:
        return {"order_id": order_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=naive), base_url="http://test"
    ) as client:
        captured = await client.get("/v1/orders/ord-1:cancel")
    assert captured.status_code == 200
    assert captured.json() == {"order_id": "ord-1:cancel"}

    store = OrderStore()
    store.create("ord-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(store)), base_url="http://test"
    ) as client:
        guarded = await client.get("/v1/orders/ord-1:cancel")
    assert guarded.status_code == 422  # the id pattern rejects ':' instead of a misleading 404
    assert store.orders["ord-1"].state is State.PENDING_PAYMENT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_misspelled_verb_is_405_with_allow_from_the_item_route(
    store_and_client: tuple[OrderStore, httpx.AsyncClient],
) -> None:
    _, client = store_and_client
    response = await client.post("/v1/orders/ord-1:cancle")
    assert response.status_code == 405
    # FastAPI APIRoute does not add HEAD to a GET route (Starlette Route does).
    assert response.headers["allow"] == "GET"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generic_post_route_registered_first_shadows_the_custom_method() -> None:
    def make(generic_first: bool) -> FastAPI:
        app = FastAPI()

        async def generic(order_id: str) -> dict[str, str]:
            return {"handler": "generic", "order_id": order_id}

        async def cancel(order_id: str) -> dict[str, str]:
            return {"handler": "cancel", "order_id": order_id}

        routes = [("/v1/orders/{order_id}", generic), ("/v1/orders/{order_id}:cancel", cancel)]
        for path, endpoint in routes if generic_first else reversed(routes):
            app.add_api_route(path, endpoint, methods=["POST"])
        return app

    results: list[dict[str, str]] = []
    for generic_first in (True, False):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make(generic_first)), base_url="http://test"
        ) as client:
            results.append((await client.post("/v1/orders/ord-1:cancel")).json())
    assert results == [
        {"handler": "generic", "order_id": "ord-1:cancel"},
        {"handler": "cancel", "order_id": "ord-1"},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_percent_encoded_colon_is_decoded_before_routing() -> None:
    store = OrderStore()
    store.create("ord-1")
    inner = build_app(store)
    seen: list[tuple[str, bytes]] = []

    async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            seen.append((scope["path"], scope["raw_path"]))
        await inner(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=recorder), base_url="http://test"
    ) as client:
        response = await client.post("/v1/orders/ord-1%3Acancel")
    assert seen == [("/v1/orders/ord-1:cancel", b"/v1/orders/ord-1%3Acancel")]
    assert response.status_code == 200
    assert store.orders["ord-1"].state is State.CANCELLED


@pytest.mark.unit
def test_relative_reference_with_a_colon_parses_as_a_uri_scheme() -> None:
    base = "https://api.test/v1/orders/"
    assert urljoin(base, "ord-1:cancel") == "ord-1:cancel"
    assert str(httpx.URL(base).join("ord-1:cancel")) == "ord-1:cancel"
    assert urljoin(base, "./ord-1:cancel") == "https://api.test/v1/orders/ord-1:cancel"
