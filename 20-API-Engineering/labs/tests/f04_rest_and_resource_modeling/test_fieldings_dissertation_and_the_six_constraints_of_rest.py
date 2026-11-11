"""Each REST constraint from Fielding Chapter 5 as an invariant that a broken mechanism fails."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api_labs.f04_rest_and_resource_modeling.rest_constraints import (
    HitCounter,
    OrderStore,
    RoundRobin,
    SharedCache,
    build_allow_demo,
    build_origin,
    build_session_pager,
    hypermedia_order_ids,
    session_order_ids,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp


class FakeClock:
    """Injected monotonic clock so freshness arithmetic is deterministic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stateless_paging_survives_round_robin_replicas() -> None:
    store = OrderStore.with_orders(5)
    replica_a = HitCounter(build_origin(store))
    replica_b = HitCounter(build_origin(store))
    async with client_for(RoundRobin([replica_a, replica_b])) as client:
        ids = await hypermedia_order_ids(client)

    assert ids == [1, 2, 3, 4, 5]
    assert (replica_a.count, replica_b.count) == (2, 2)  # root + 3 pages, alternating


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_side_session_cursor_breaks_under_round_robin() -> None:
    store = OrderStore.with_orders(5)
    async with client_for(build_session_pager(store, "a")) as client:
        single_replica = await session_order_ids(client, max_requests=6)
    async with client_for(
        RoundRobin([build_session_pager(store, "a"), build_session_pager(store, "b")])
    ) as client:
        balanced = await session_order_ids(client, max_requests=6)

    assert single_replica == [1, 2, 3, 4, 5]
    assert balanced == [1, 2] * 6  # each replica sees an unknown cookie and restarts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_cache_answers_repeat_get_without_origin_and_sets_age() -> None:
    clock = FakeClock()
    origin = HitCounter(build_origin(OrderStore.with_orders(1)))
    cache = SharedCache(origin, clock)
    async with client_for(cache) as client:
        first = await client.get("/orders/1")
        clock.now += 7
        second = await client.get("/orders/1")

    assert origin.count == 1
    assert cache.hits == 1
    assert "age" not in first.headers
    assert second.headers["age"] == "7"
    assert second.content == first.content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_entry_goes_stale_exactly_when_age_reaches_max_age() -> None:
    clock = FakeClock()
    origin = HitCounter(build_origin(OrderStore.with_orders(1), max_age=60))
    async with client_for(SharedCache(origin, clock)) as client:
        await client.get("/orders/1")
        clock.now += 59
        at_59 = await client.get("/orders/1")
        clock.now += 1
        at_60 = await client.get("/orders/1")

    assert at_59.headers["age"] == "59"
    assert "age" not in at_60.headers
    assert origin.count == 2


def fixed_origin(headers: dict[str, str]) -> Starlette:
    async def endpoint(request: Request) -> Response:
        return JSONResponse({"n": 1}, headers=headers)

    return Starlette(routes=[Route("/r", endpoint, methods=["GET"])])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_headers", "authorization", "origin_hits"),
    [
        ({"Cache-Control": "max-age=60"}, None, 1),
        ({"Cache-Control": "no-store, max-age=60"}, None, 2),
        ({"Cache-Control": "private, max-age=60"}, None, 2),
        ({"Cache-Control": "public"}, None, 2),
        ({"Cache-Control": "no-cache, max-age=60"}, None, 2),
        ({"Cache-Control": "max-age=abc"}, None, 2),
        ({"Cache-Control": "max-age=\u00b2"}, None, 2),
        ({"Cache-Control": "max-age=60, max-age=0"}, None, 1),
        ({"Cache-Control": "s-maxage=60, max-age=0"}, None, 1),
        ({"Cache-Control": "s-maxage=0, max-age=60"}, None, 2),
        ({"Cache-Control": "max-age=60", "Vary": "Accept"}, None, 2),
        ({"Cache-Control": "max-age=60"}, "Bearer t", 2),
        ({"Cache-Control": "public, max-age=60"}, "Bearer t", 1),
        ({"Cache-Control": "s-maxage=60"}, "Bearer t", 1),
        ({"Cache-Control": "must-revalidate, max-age=60"}, "Bearer t", 1),
    ],
    ids=[
        "max-age",
        "no-store",
        "private",
        "public-without-lifetime",
        "no-cache-not-stored",
        "invalid-max-age-is-stale",
        "non-ascii-digit-max-age-is-stale",
        "first-max-age-wins",
        "s-maxage-beats-max-age",
        "s-maxage-zero-beats-max-age",
        "vary-not-stored",
        "auth",
        "auth-public",
        "auth-s-maxage",
        "auth-must-revalidate",
    ],
)
async def test_control_data_alone_decides_shared_storability(
    response_headers: dict[str, str], authorization: str | None, origin_hits: int
) -> None:
    origin = HitCounter(fixed_origin(response_headers))
    headers = {"Authorization": authorization} if authorization else {}
    async with client_for(SharedCache(origin, FakeClock())) as client:
        await client.get("/r", headers=headers)
        await client.get("/r")  # anonymous follow-up: a hit only if the first was stored

    assert origin.count == origin_hits


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_key_includes_the_query_string() -> None:
    async def echo(request: Request) -> Response:
        return JSONResponse(
            {"n": request.query_params["n"]}, headers={"Cache-Control": "max-age=60"}
        )

    origin = HitCounter(Starlette(routes=[Route("/r", echo, methods=["GET"])]))
    async with client_for(SharedCache(origin, FakeClock())) as client:
        one = await client.get("/r", params={"n": "1"})
        two = await client.get("/r", params={"n": "2"})
        again = await client.get("/r", params={"n": "1"})

    assert (one.json(), two.json(), again.json()) == ({"n": "1"}, {"n": "2"}, {"n": "1"})
    assert "age" in again.headers  # the second request for ?n=1 is the only hit
    assert origin.count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_no_store_forbids_storing_the_response() -> None:
    origin = HitCounter(fixed_origin({"Cache-Control": "max-age=60"}))
    async with client_for(SharedCache(origin, FakeClock())) as client:
        await client.get("/r", headers={"Cache-Control": "no-store"})
        await client.get("/r")

    assert origin.count == 2  # RFC 9111 Section 5.2.1.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_hits_are_served_only_to_requests_without_authorization() -> None:
    origin = HitCounter(fixed_origin({"Cache-Control": "max-age=60"}))
    async with client_for(SharedCache(origin, FakeClock())) as client:
        await client.get("/r")  # anonymous: stored
        authorized = await client.get("/r", headers={"Authorization": "Bearer t"})
        anonymous = await client.get("/r")

    assert "age" not in authorized.headers  # forwarded to the origin, not answered from the cache
    assert "age" in anonymous.headers
    assert origin.count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_method_on_target_uri_invalidates_cached_representation() -> None:
    origin = HitCounter(build_origin(OrderStore.with_orders(1)))
    async with client_for(SharedCache(origin, FakeClock())) as client:
        assert (await client.get("/orders/1")).status_code == 200
        assert (await client.delete("/orders/1")).status_code == 204
        after = await client.get("/orders/1")

    assert after.status_code == 404
    assert origin.count == 3


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_location", "status_seen"),
    [(True, "cancelled"), (False, "pending")],
    ids=["content-location-invalidates", "action-uri-leaves-stale-entry"],
)
async def test_post_action_invalidates_resource_only_through_content_location(
    content_location: bool, status_seen: str
) -> None:
    origin = build_origin(OrderStore.with_orders(1), content_location=content_location)
    async with client_for(SharedCache(origin, FakeClock())) as client:
        order = (await client.get("/orders/1")).json()
        assert (await client.post(order["links"]["cancel"])).status_code == 200
        after = await client.get("/orders/1")

    assert after.json()["status"] == status_seen


def action_origin(status: int, headers: dict[str, str]) -> Starlette:
    """Cacheable ``GET /r`` plus an unsafe ``POST /act`` that answers with a chosen status."""

    async def read(request: Request) -> Response:
        return JSONResponse({"n": 1}, headers={"Cache-Control": "max-age=60"})

    async def act(request: Request) -> Response:
        return Response(status_code=status, headers=headers)

    return Starlette(
        routes=[Route("/r", read, methods=["GET"]), Route("/act", act, methods=["POST"])]
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "invalidated"),
    [
        (303, {"Location": "/r"}, True),
        (201, {"Location": "/r"}, True),
        (201, {}, False),
        (409, {"Location": "/r"}, False),
        (303, {"Location": "http://other.example/r"}, False),
    ],
    ids=[
        "redirect-with-location-invalidates",
        "created-with-location-invalidates",
        "no-header-leaves-entry",
        "error-status-leaves-entry",
        "other-origin-location-leaves-entry",
    ],
)
async def test_non_error_unsafe_response_invalidates_uris_named_in_location(
    status: int, headers: dict[str, str], invalidated: bool
) -> None:
    origin = HitCounter(action_origin(status, headers))
    async with client_for(SharedCache(origin, FakeClock())) as client:
        await client.get("/r")
        assert (await client.post("/act")).status_code == status
        await client.get("/r")

    assert origin.count == (3 if invalidated else 2)  # GET, POST, and a refetch only if purged


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_response_to_unsafe_method_does_not_invalidate() -> None:
    origin = HitCounter(build_origin(OrderStore.with_orders(1)))
    async with client_for(SharedCache(origin, FakeClock())) as client:
        await client.get("/orders/1")
        rejected = await client.put("/orders/1", json={"status": "shipped"})
        after = await client.get("/orders/1")

    assert rejected.status_code == 405  # unsafe method, same target URI, error status
    assert "age" in after.headers  # still served from the cache
    assert origin.count == 2  # first GET and the PUT; the second GET is a hit


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypermedia_client_survives_namespace_move_hardcoded_client_breaks() -> None:
    origin = build_origin(OrderStore.with_orders(3), prefix="/v2/purchase-orders")
    async with client_for(origin) as client:
        ids = await hypermedia_order_ids(client)
        hardcoded = await client.get("/orders?after=0")

    assert ids == [1, 2, 3]
    assert hardcoded.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_links_advertise_only_transitions_valid_in_current_state() -> None:
    async with client_for(build_origin(OrderStore.with_orders(1))) as client:
        before = (await client.get("/orders/1")).json()
        assert (await client.post(before["links"]["cancel"])).status_code == 200
        after = (await client.get("/orders/1")).json()
        replayed = await client.post(before["links"]["cancel"])  # stale link from old state

    assert set(before["links"]) == {"self", "cancel"}
    assert after["status"] == "cancelled"
    assert set(after["links"]) == {"self"}
    assert replayed.status_code == 409


@pytest.mark.unit
@pytest.mark.asyncio
async def test_last_full_page_has_no_next_link_and_no_trailing_request() -> None:
    origin = HitCounter(build_origin(OrderStore.with_orders(4)))
    async with client_for(origin) as client:
        ids = await hypermedia_order_ids(client)
        last = await client.get("/orders?after=2")

    assert ids == [1, 2, 3, 4]
    assert last.json()["links"] == {}  # exactly one page remains, so no `next`
    assert origin.count == 4  # root, two pages, and the explicit fetch of the last page


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_cursor_is_rejected_with_400() -> None:
    async with client_for(build_origin(OrderStore.with_orders(1))) as client:
        response = await client.get("/orders?after=-1")
        superscript = await client.get("/orders", params={"after": "\u00b2"})  # isdigit() is True
        missing = await client.post("/orders/9/cancel")
        missing_item = await client.get("/orders/9")

    assert (response.status_code, superscript.status_code) == (400, 400)
    assert (missing.status_code, missing_item.status_code) == (404, 404)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("split_routes", "allow"),
    [(False, {"GET", "HEAD", "DELETE"}), (True, {"GET", "HEAD"})],
    ids=["one-route", "split-routes-drop-delete"],
)
async def test_405_allow_header_depends_on_route_layout(
    split_routes: bool, allow: set[str]
) -> None:
    async with client_for(build_allow_demo(split_routes=split_routes)) as client:
        response = await client.put("/items/1")
        read = await client.get("/items/1")
        deleted = await client.delete("/items/1")

    assert response.status_code == 405
    assert set(response.headers["allow"].split(", ")) == allow
    assert (read.text, deleted.status_code) == ("item", 204)  # both work; only Allow lies


@pytest.mark.unit
@pytest.mark.asyncio
async def test_layered_system_is_invisible_to_the_client() -> None:
    store = OrderStore.with_orders(2)
    replicas = [HitCounter(build_origin(store)), HitCounter(build_origin(store))]
    layered = SharedCache(RoundRobin(replicas), FakeClock())
    async with LifespanManager(layered), client_for(layered) as via_layers:
        layered_responses = [await via_layers.get(path) for path in ("/", "/orders/1", "/orders/1")]
    async with client_for(build_origin(store)) as direct:
        direct_responses = [await direct.get(path) for path in ("/", "/orders/1", "/orders/1")]

    for layered_response, direct_response in zip(layered_responses, direct_responses, strict=True):
        assert layered_response.status_code == direct_response.status_code
        assert layered_response.content == direct_response.content
        layered_headers = {k: v for k, v in layered_response.headers.items() if k != "age"}
        assert layered_headers == dict(direct_response.headers)
    assert layered_responses[2].headers["age"] == "0"  # the only trace of the cache layer
    assert [replica.count for replica in replicas] == [1, 1]  # lifespan is not counted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypermedia_client_refuses_a_next_link_cycle() -> None:
    async def root(request: Request) -> Response:
        return JSONResponse({"links": {"orders": "/loop"}})

    async def loop(request: Request) -> Response:
        return JSONResponse({"items": [], "links": {"next": "/loop"}})

    app = Starlette(routes=[Route("/", root), Route("/loop", loop)])
    async with client_for(app) as client:
        with pytest.raises(RuntimeError, match="did not terminate"):
            await hypermedia_order_ids(client)
