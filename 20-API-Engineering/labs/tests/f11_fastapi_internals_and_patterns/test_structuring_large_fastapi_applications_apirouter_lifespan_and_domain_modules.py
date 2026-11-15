"""Unit tests for multi-team FastAPI modular application factory and router composition."""

import httpx
import pytest
from fastapi import FastAPI

from api_labs.f11_fastapi_internals_and_patterns.structuring_large_apps import (
    AppSettings,
    DatabaseSettings,
    ExecutionLog,
    create_app,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_composition_and_dependency_execution_order() -> None:
    log = ExecutionLog()
    app = create_app(log=log)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get("/api/v1/users/me")

    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == "usr_current"
    assert data["username"] == "alice"
    assert "db=sqlite+aiosqlite:///:memory:" in data["source"]
    assert "cache=enterprise-api" in data["source"]

    # Verify hierarchical dependency execution order:
    # api_v1_guard (parent router) -> user_domain_guard (child router) -> endpoint
    assert log.events == [
        "lifespan:enter_root",
        "lifespan:startup_db:sqlite+aiosqlite:///:memory:",
        "lifespan:startup_cache:enterprise-api",
        "guard:api_v1",
        "guard:user_domain",
        "endpoint:users_me",
        "lifespan:shutdown_cache",
        "lifespan:shutdown_db",
        "lifespan:exit_root",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_ordering_prevents_parameter_shadowing() -> None:
    log = ExecutionLog()
    app = create_app(log=log)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        res_me = await client.get("/api/v1/users/me")
        res_id = await client.get("/api/v1/users/usr_42")

    assert res_me.status_code == 200
    assert res_me.json()["user_id"] == "usr_current"

    assert res_id.status_code == 200
    assert res_id.json()["user_id"] == "usr_42"
    assert res_id.json()["username"] == "user_usr_42"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_lifo_teardown_and_state_propagation() -> None:
    log = ExecutionLog()
    settings = AppSettings(
        service_name="order-orchestration-service",
        database=DatabaseSettings(url="sqlite+aiosqlite:///custom.db"),
    )
    app = create_app(settings=settings, log=log)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        res = await client.get("/api/v1/orders/ord_999")
        assert res.status_code == 200
        assert res.json() == {
            "order_id": "ord_999",
            "user_id": "usr_current",
            "amount_cents": 4999,
        }
        # The custom settings, not the defaults, reached the lifespan-built resources.
        assert app.state.db_pool.url == "sqlite+aiosqlite:///custom.db"
        assert app.state.domain_cache.domain == "order-orchestration-service"
        assert app.state.db_pool.is_closed is False

    assert "lifespan:startup_db:sqlite+aiosqlite:///custom.db" in log.events
    assert app.state.db_pool.is_closed is True
    assert app.state.domain_cache.is_cleared is True

    # Verify LIFO teardown: domain cache clears BEFORE database pool closes
    assert "lifespan:shutdown_cache" in log.events
    assert "lifespan:shutdown_db" in log.events
    cache_idx = log.events.index("lifespan:shutdown_cache")
    db_idx = log.events.index("lifespan:shutdown_db")
    assert cache_idx < db_idx, "Domain cache must shut down before database pool"


@pytest.mark.unit
def test_application_factory_settings_isolation() -> None:
    settings_a = AppSettings(service_name="service-alpha")
    settings_b = AppSettings(service_name="service-beta")

    app_a = create_app(settings_a)
    app_b = create_app(settings_b)

    assert isinstance(app_a, FastAPI)
    assert isinstance(app_b, FastAPI)
    assert app_a.title == "service-alpha"
    assert app_b.title == "service-beta"
    assert app_a is not app_b


@pytest.mark.unit
def test_router_tag_and_prefix_inheritance_in_openapi() -> None:
    app = create_app()
    schema = app.openapi()

    assert "/api/v1/users/me" in schema["paths"]
    assert "/api/v1/users/{user_id}" in schema["paths"]
    assert "/api/v1/orders/{order_id}" in schema["paths"]

    user_get_tags = schema["paths"]["/api/v1/users/me"]["get"]["tags"]
    assert user_get_tags == ["users"]

    order_get_tags = schema["paths"]["/api/v1/orders/{order_id}"]["get"]["tags"]
    assert order_get_tags == ["orders"]


@pytest.mark.unit
def test_stray_environment_variable_does_not_leak_into_nested_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("URL", "postgresql://leaked/db")
    assert AppSettings().database.url == "sqlite+aiosqlite:///:memory:"
