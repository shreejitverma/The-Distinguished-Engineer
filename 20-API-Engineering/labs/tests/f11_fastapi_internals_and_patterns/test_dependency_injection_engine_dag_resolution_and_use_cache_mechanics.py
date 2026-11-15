"""Unit tests for FastAPI solve_dependencies() DAG traversal and the per-request cache."""

from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from api_labs.f11_fastapi_internals_and_patterns.dependency_dag_cache import (
    CallLog,
    build_callable_key_app,
    build_dag_app,
    build_tenant_app,
    dependency_cache_keys,
    make_self_referential_dependency,
)


async def get_json(app: FastAPI, path: str) -> tuple[int, Any]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path)
    return response.status_code, response.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_diamond_leaf_runs_once_and_both_parents_get_the_same_object() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/diamond")
    assert status == 200
    assert body == {"same_instance": True}
    assert log.events == ["shared", "left", "right"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_is_per_request_not_per_process() -> None:
    log = CallLog()
    app = build_dag_app(log).app
    await get_json(app, "/diamond")
    await get_json(app, "/diamond")
    assert log.count("shared") == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_use_cache_false_on_the_second_edge_runs_the_leaf_twice() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/fresh-last")
    assert status == 200
    assert body == {"same_instance": False}
    assert log.events == ["shared", "left", "shared", "right_fresh"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_use_cache_false_on_the_first_edge_still_seeds_the_cache() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/fresh-first")
    assert status == 200
    assert body == {"same_instance": True}
    assert log.events == ["shared", "left_fresh", "right"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_different_scopes_split_the_cache_when_the_dependency_reads_them() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/scopes-split")
    assert status == 200
    assert body == {"first": "read", "second": "write"}
    assert log.events == ["scoped_principal:read", "scoped_principal:write"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_order_is_normalized_so_the_second_caller_sees_the_first_order() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/scopes-order")
    assert status == 200
    assert body == {"first": "a b", "second": "a b"}
    assert log.events == ["scoped_principal:a b"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_inherited_scopes_do_not_split_a_leaf_that_ignores_them() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/scopes-ignored")
    assert status == 200
    assert body == {"same_instance": True}
    assert log.events == ["shared"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_scope_is_part_of_the_key() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/generator-scope")
    assert status == 200
    assert body == {"request": "session", "function": "session"}
    assert log.count("session:enter") == 2
    assert log.count("session:exit") == 2


@pytest.mark.unit
def test_cache_key_tuple_is_call_sorted_scopes_and_computed_scope() -> None:
    app = build_dag_app(CallLog()).app
    assert dependency_cache_keys(app, "/scopes-split") == [
        ("scoped_principal", ("read",), ""),
        ("scoped_principal", ("write",), ""),
    ]
    assert dependency_cache_keys(app, "/scopes-order") == [
        ("scoped_principal", ("a", "b"), ""),
        ("scoped_principal", ("a", "b"), ""),
    ]
    assert dependency_cache_keys(app, "/scopes-ignored") == [
        ("read_guard", ("read",), ""),
        ("shared", (), ""),
        ("write_guard", ("write",), ""),
        ("shared", (), ""),
    ]
    assert dependency_cache_keys(app, "/generator-scope") == [
        ("session", (), "request"),
        ("session", (), "function"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_hit_still_resolves_the_subtree_of_the_cached_node() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/rewalk")
    assert status == 200
    assert body == {"left": "child", "right": "child"}
    assert log.events == ["fresh_child", "parent", "fresh_child"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sibling_dependencies_are_awaited_sequentially() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/sequential")
    assert status == 200
    assert body == ["first", "second"]
    assert log.events == [
        "slow_first:start",
        "slow_first:end",
        "slow_second:start",
        "slow_second:end",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_decorator_and_parameter_declarations_share_one_entry() -> None:
    log = CallLog()
    status, body = await get_json(build_dag_app(log).app, "/router-level")
    assert status == 200
    assert body == {"ok": True}
    assert log.events == ["shared"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_use_cache_false_is_dropped_on_a_parameterless_dependency() -> None:
    log = CallLog()
    app = build_dag_app(log).app
    route = next(
        r for r in app.routes if isinstance(r, APIRoute) and r.path == "/parameterless-fresh"
    )
    assert [sub.use_cache for sub in route.dependant.dependencies] == [True, True]
    status, body = await get_json(app, "/parameterless-fresh")
    assert status == 200
    assert body == {"ok": True}
    assert log.events == ["shared"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_override_is_cached_under_the_original_callable() -> None:
    log = CallLog()
    lab = build_dag_app(log)

    async def fake_shared() -> object:
        log.record("fake_shared")
        return object()

    lab.app.dependency_overrides[lab.shared] = fake_shared
    status, body = await get_json(lab.app, "/diamond")
    assert status == 200
    assert body == {"same_instance": True}
    assert log.events == ["fake_shared", "left", "right"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_leaf_is_not_cached_so_a_diamond_reports_the_error_twice() -> None:
    log = CallLog()
    app = build_dag_app(log).app
    status, body = await get_json(app, "/diamond-query")
    assert status == 422
    assert [err["loc"] for err in body["detail"]] == [["query", "q"], ["query", "q"]]
    assert log.events == []

    status, body = await get_json(app, "/diamond-query?q=21")
    assert status == 200
    assert body == 42
    assert log.events == ["shared_query"]


@pytest.mark.unit
def test_openapi_lists_a_diamond_leaf_parameter_once() -> None:
    app = build_dag_app(CallLog()).app
    params = app.openapi()["paths"]["/diamond-query"]["get"]["parameters"]
    assert [p["name"] for p in params] == ["q"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhashable_callable_instance_registers_but_fails_every_request() -> None:
    app = build_callable_key_app(CallLog())
    with pytest.raises(TypeError, match="unhashable type: 'MutableRoleGuard'"):
        await get_json(app, "/mutable-guard")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_equal_frozen_callable_instances_share_one_cache_entry() -> None:
    log = CallLog()
    status, body = await get_json(build_callable_key_app(log), "/frozen-guards")
    assert status == 200
    assert body == ["admin", "admin", "user"]
    assert log.events == ["guard:admin", "guard:user"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_typing_cache_hands_a_second_app_the_first_apps_equal_instance() -> None:
    first_log, second_log = CallLog(), CallLog()
    tenant = f"acme-{id(first_log)}"
    build_tenant_app(first_log, tenant)
    second_app = build_tenant_app(second_log, tenant)
    status, body = await get_json(second_app, "/tenant")
    assert status == 200
    assert body == tenant
    assert second_log.events == []
    assert first_log.events == [f"tenant:{tenant}"]


@pytest.mark.unit
def test_dependency_cycle_fails_at_route_registration() -> None:
    app = FastAPI()
    loop = make_self_referential_dependency()

    async def endpoint(v: Annotated[object, Depends(loop)]) -> None:
        del v  # pragma: no cover - registration fails first

    with pytest.raises(RecursionError):
        app.get("/cycle")(endpoint)
