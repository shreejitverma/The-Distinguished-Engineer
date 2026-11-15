"""Unit tests for FastAPI architectural limits, cold-start cost, and Litestar/Starlette."""

from typing import Any, cast

import httpx
import pytest

from api_labs.f11_fastapi_internals_and_patterns.fastapi_architectural_limits import (
    build_fastapi_app,
    build_fastapi_di_app,
    build_litestar_app,
    build_litestar_di_app,
    build_starlette_app,
    measure_cold_start,
)


@pytest.mark.unit
def test_cold_start_route_compilation_scaling() -> None:
    """Verify that route initialization completes across frameworks and Starlette is lowest cost."""
    metrics = measure_cold_start(route_count=50)

    assert metrics.route_count == 50
    assert metrics.starlette_init_ms > 0.0
    assert metrics.fastapi_init_ms > 0.0
    assert metrics.litestar_init_ms > 0.0
    # Starlette performs zero signature inspection or Pydantic model compilation,
    # making its per-route instantiation substantially faster than FastAPI.
    assert metrics.starlette_init_ms <= metrics.fastapi_init_ms


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_linear_routing_behavior() -> None:
    """Verify raw Starlette routes match correctly at the head and tail of the route list."""
    app = build_starlette_app(route_count=50)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp_head = await client.get("/route_0")
        resp_tail = await client.get("/route_49")
        resp_miss = await client.get("/route_50")

    assert resp_head.status_code == 200
    assert resp_head.json() == {"route": 0, "framework": "starlette"}

    assert resp_tail.status_code == 200
    assert resp_tail.json() == {"route": 49, "framework": "starlette"}

    assert resp_miss.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_dependency_injection_resolution() -> None:
    """Verify FastAPI runtime DI engine resolves async and sync threadpool dependency chains."""
    app = build_fastapi_di_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp_0 = await client.get("/dep-0")
        resp_1 = await client.get("/dep-1")
        resp_5 = await client.get("/dep-5")
        resp_sync_1 = await client.get("/dep-sync-1")
        resp_sync_5 = await client.get("/dep-sync-5")

    assert resp_0.status_code == 200
    assert resp_0.json() == {"result": 0}

    assert resp_1.status_code == 200
    assert resp_1.json() == {"result": 1}

    assert resp_5.status_code == 200
    assert resp_5.json() == {"result": 5}

    assert resp_sync_1.status_code == 200
    assert resp_sync_1.json() == {"result": 1}

    assert resp_sync_5.status_code == 200
    assert resp_sync_5.json() == {"result": 5}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_litestar_dependency_injection_resolution() -> None:
    """Verify Litestar pre-compiled KwargsModel dependency extraction."""
    app = build_litestar_di_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast("Any", app)), base_url="http://test"
    ) as client:
        resp_0 = await client.get("/dep-0")
        resp_1 = await client.get("/dep-1")
        resp_5 = await client.get("/dep-5")

    assert resp_0.status_code == 200
    assert resp_0.json() == {"result": 0}

    assert resp_1.status_code == 200
    assert resp_1.json() == {"result": 1}

    assert resp_5.status_code == 200
    assert resp_5.json() == {"result": 5}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_and_litestar_route_trie_and_linear_routing() -> None:
    """Verify route resolution across FastAPI linear routes and Litestar routing trie."""
    f_app = build_fastapi_app(route_count=30)
    l_app = build_litestar_app(route_count=30)

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=f_app), base_url="http://test"
        ) as f_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=cast("Any", l_app)), base_url="http://test"
        ) as l_client,
    ):
        f_resp = await f_client.get("/route_29")
        l_resp = await l_client.get("/route_29")

    assert f_resp.status_code == 200
    assert f_resp.json() == {"route": 29, "framework": "fastapi"}

    assert l_resp.status_code == 200
    assert l_resp.json() == {"route": 29, "framework": "litestar"}
