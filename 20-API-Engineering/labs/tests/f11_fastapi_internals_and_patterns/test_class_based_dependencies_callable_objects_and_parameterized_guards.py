"""Unit tests for parameterized class-based dependencies and callable guards in FastAPI."""

import httpx
import pytest
from fastapi.dependencies.models import (
    _CallIdentity,  # pyright: ignore[reportPrivateUsage]
    _is_coroutine_callable,  # pyright: ignore[reportPrivateUsage]
)

from api_labs.f11_fastapi_internals_and_patterns.class_based_dependencies import (
    AuditLifecycleTracker,
    RequirePermission,
    User,
    build_app,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_guard_allows_authorized_user() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/secure/ledger",
            headers={"Authorization": "Bearer alice|ledger:post,ledger:read"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": "Ledger posted by alice",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_guard_forbids_unauthorized_user() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/secure/ledger",
            headers={"Authorization": "Bearer bob|ledger:read,reports:view"},
        )
    assert response.status_code == 403
    assert response.json() == {
        "detail": "Forbidden: missing required permission 'ledger:post'",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_guard_allows_superuser_bypass() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/secure/ledger",
            headers={"Authorization": "Bearer admin||superuser"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": "Ledger posted by admin",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_guard_rejects_missing_and_malformed_auth() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res_missing = await client.get("/secure/ledger")
        res_malformed = await client.get(
            "/secure/ledger",
            headers={"Authorization": "Bearer notokenformat"},
        )
    assert res_missing.status_code == 401
    assert res_missing.headers.get("www-authenticate") == "Bearer"
    assert res_missing.json() == {"detail": "Missing Authorization header"}
    assert res_malformed.status_code == 401
    assert res_malformed.json() == {"detail": "Malformed bearer token format"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_guard_override_by_value_in_dependency_overrides() -> None:
    app = build_app()
    # RequirePermission is a frozen dataclass; instantiating an equivalent instance
    # in the test overrides the route's dependency by value without leaking global variables.
    mock_user = User(username="test_mock", permissions=frozenset(), is_superuser=False)
    app.dependency_overrides[RequirePermission("ledger:post")] = lambda: mock_user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Request has no Authorization header; mock should bypass authentication completely
        response = await client.get("/secure/ledger")

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": "Ledger posted by test_mock",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limiter_allows_up_to_limit_and_sets_headers() -> None:
    app = build_app(rate_limit_requests=2, rate_limit_window=60.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r1 = await client.get("/rate-limited")
        r2 = await client.get("/rate-limited")

    assert r1.status_code == 200
    assert r1.headers.get("ratelimit-limit") == "2"
    assert r1.headers.get("ratelimit-remaining") == "1"
    assert r1.headers.get("ratelimit-reset") == "60"

    assert r2.status_code == 200
    assert r2.headers.get("ratelimit-limit") == "2"
    assert r2.headers.get("ratelimit-remaining") == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limiter_exceeds_limit_returns_429_with_retry_after() -> None:
    app = build_app(rate_limit_requests=2, rate_limit_window=60.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/rate-limited")
        await client.get("/rate-limited")
        r3 = await client.get("/rate-limited")

    assert r3.status_code == 429
    assert r3.headers.get("ratelimit-limit") == "2"
    assert r3.headers.get("ratelimit-remaining") == "0"
    assert "retry-after" in r3.headers
    assert int(r3.headers["retry-after"]) >= 1
    assert r3.json() == {"detail": "Too Many Requests"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pagination_parser_defaults() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/items")

    assert response.status_code == 200
    assert response.json() == {
        "offset": 0,
        "limit": 10,
        "order_by": "id",
        "descending": False,
        "items": [],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pagination_parser_custom_query_params() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/items?offset=20&limit=30&order_by=amount&descending=true",
        )

    assert response.status_code == 200
    assert response.json() == {
        "offset": 20,
        "limit": 30,
        "order_by": "amount",
        "descending": True,
        "items": [],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pagination_parser_validation_errors() -> None:
    app = build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res_limit = await client.get("/items?limit=999")
        res_order = await client.get("/items?order_by=unsupported_col")

    assert res_limit.status_code == 400
    assert "exceeds maximum allowed limit" in res_limit.json()["detail"]

    assert res_order.status_code == 400
    assert "Invalid order_by field 'unsupported_col'" in res_order.json()["detail"]


@pytest.mark.unit
def test_openapi_schema_contains_inspected_parameters() -> None:
    app = build_app()
    schema = app.openapi()
    items_get = schema["paths"]["/items"]["get"]
    parameters = {p["name"]: p for p in items_get["parameters"]}

    assert "offset" in parameters
    assert parameters["offset"]["in"] == "query"
    assert parameters["offset"]["schema"]["minimum"] == 0
    assert parameters["offset"]["schema"]["default"] == 0

    assert "limit" in parameters
    assert parameters["limit"]["in"] == "query"

    assert "order_by" in parameters
    assert parameters["order_by"]["in"] == "query"
    assert parameters["order_by"]["schema"]["default"] == "id"

    assert "descending" in parameters
    assert parameters["descending"]["in"] == "query"
    assert parameters["descending"]["schema"]["default"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_callable_class_yield_dependency_lifecycle() -> None:
    tracker = AuditLifecycleTracker()
    app = build_app(audit_tracker=tracker)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/audited-action")

    assert response.status_code == 200
    assert response.json() == {
        "action": "ledger_mutation",
        "result": "completed",
    }
    # Teardown must occur after endpoint returns, recording enter -> endpoint -> exit
    assert tracker.events == [
        "enter:ledger_mutation",
        "endpoint_executed:ledger_mutation",
        "exit:ledger_mutation",
    ]


@pytest.mark.unit
def test_coroutine_callable_classification_caching() -> None:
    class SyncGuard:
        def __call__(self) -> str:
            return "sync"

    class AsyncGuard:
        async def __call__(self) -> str:
            return "async"

    sg = SyncGuard()
    ag = AsyncGuard()

    # Classification distinguishes sync vs async instances by inspecting __call__
    assert not _is_coroutine_callable(sg)
    assert _is_coroutine_callable(ag)

    # Call identity hashes by object id and tests identity equality
    id1 = _CallIdentity(sg)
    id2 = _CallIdentity(sg)
    assert id1 == id2
    assert hash(id1) == hash(id2)
