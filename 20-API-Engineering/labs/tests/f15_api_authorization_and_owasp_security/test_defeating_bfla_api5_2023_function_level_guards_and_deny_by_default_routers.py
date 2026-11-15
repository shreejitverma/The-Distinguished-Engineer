"""Lab: OWASP API5:2023 function-level authorization with deny-by-default FastAPI guards."""

import functools
from collections.abc import Awaitable, Callable

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from api_labs.f15_api_authorization_and_owasp_security.function_level_guards import (
    DOCS_PATHS,
    AccessControl,
    Finding,
    RouteAuditError,
    Store,
    audit_routes,
    build_admin_app,
    build_admin_router,
    build_public_app,
    naive_audit_routes,
)


async def call(app: FastAPI, method: str, path: str, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def acl() -> AccessControl:
    return AccessControl()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "token", "expected"),
    [
        ("GET", "/health", None, 200),
        ("GET", "/me", None, 401),
        ("GET", "/me", "tok-alice", 200),
        ("POST", "/invites?role=admin", "tok-alice", 403),
        ("POST", "/invites?role=admin", "tok-ops", 201),
        ("POST", "/users/bob/unlock", "tok-alice", 403),
        ("POST", "/users/bob/unlock", "tok-sam", 200),
        ("POST", "/users/bob/unlock", "tok-forged", 401),
    ],
)
async def test_public_app_role_matrix(
    store: Store, acl: AccessControl, method: str, path: str, token: str | None, expected: int
) -> None:
    r = await call(build_public_app(store, acl), method, path, token)
    assert r.status_code == expected
    if expected == 401:
        assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_rejects_before_route_dependencies_run(
    store: Store, acl: AccessControl
) -> None:
    app = build_public_app(store, acl)
    denied = await call(app, "POST", "/users/bob/unlock", "tok-alice")
    assert (denied.status_code, store.reads, store.locked) == (403, [], {"bob"})
    allowed = await call(app, "POST", "/users/bob/unlock", "tok-sam")
    assert (allowed.status_code, store.reads, store.locked) == (200, ["users"], set())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_without_a_rule_is_denied_to_everyone(store: Store, acl: AccessControl) -> None:
    app = build_public_app(store, acl)

    @app.delete("/users/{name}")
    async def delete_user(name: str) -> dict[str, str]:  # the forgotten declaration
        store.roles.pop(name, None)
        return {"deleted": name}

    r = await call(app, "DELETE", "/users/bob", "tok-ops")
    assert (r.status_code, r.json()) == (403, {"detail": "function has no access rule"})
    assert "bob" in store.roles
    assert audit_routes(app, acl) == [
        Finding("/users/{name}", "DELETE", "endpoint has no access rule")
    ]


def logged[**P, T](fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await fn(*args, **kwargs)

    return wrapper


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rule_on_a_wrapped_function_fails_closed(store: Store, acl: AccessControl) -> None:
    app = build_public_app(store, acl)

    @app.get("/me/export")
    @logged  # the route registers `wrapper`; the rule was attached to the inner function
    @acl.rule()
    async def export() -> dict[str, str]:
        return {"export": "ok"}

    r = await call(app, "GET", "/me/export", "tok-alice")
    assert r.status_code == 403
    assert audit_routes(app, acl) == [Finding("/me/export", "GET", "endpoint has no access rule")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hardened_apps_audit_clean_and_start(store: Store, acl: AccessControl) -> None:
    public, admin = build_public_app(store, acl), build_admin_app(store)
    assert audit_routes(public, acl) == []
    async with LifespanManager(public), LifespanManager(admin):
        assert (await call(public, "GET", "/health")).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_with_an_uncovered_route(
    store: Store, acl: AccessControl
) -> None:
    app = build_public_app(store, acl)

    @app.get("/debug/env")
    async def env() -> dict[str, str]:
        return {}

    with pytest.raises(RouteAuditError, match="/debug/env endpoint has no access rule"):
        async with LifespanManager(app):
            pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_include_time_guard_is_lost_on_a_second_include(
    store: Store, acl: AccessControl
) -> None:
    app = FastAPI(openapi_url=None)  # no app-wide guard: the guard rides on the include call
    router = build_admin_router(store, acl)
    app.include_router(router, dependencies=[Depends(acl)])
    app.include_router(router, prefix="/v2")  # e.g. a version alias added later
    assert (await call(app, "GET", "/admin/users", "tok-alice")).status_code == 403
    leaked = await call(app, "GET", "/v2/admin/users")  # anonymous
    assert leaked.status_code == 200
    assert leaked.json()["ops"] == ["admin", "user"]
    assert audit_routes(app, acl) == [
        Finding("/v2/admin/users", "GET", "access control is not in the dependency graph"),
        Finding(
            "/v2/admin/users/{name}/roles", "PUT", "access control is not in the dependency graph"
        ),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_wide_guard_covers_every_include(store: Store, acl: AccessControl) -> None:
    app = FastAPI(openapi_url=None, dependencies=[Depends(acl)])
    router = build_admin_router(store, acl)
    app.include_router(router)
    app.include_router(router, prefix="/v2")
    for path in ("/admin/users", "/v2/admin/users"):
        assert (await call(app, "GET", path)).status_code == 401
        assert (await call(app, "GET", path, "tok-alice")).status_code == 403
        assert (await call(app, "GET", path, "tok-ops")).status_code == 200
    assert audit_routes(app, acl) == []


@pytest.mark.unit
def test_flat_walk_of_app_routes_misses_included_routers(store: Store, acl: AccessControl) -> None:
    app = FastAPI(openapi_url=None)
    router = build_admin_router(store, acl)
    app.include_router(router, prefix="/v2")  # completely unguarded
    assert [type(r).__name__ for r in app.routes] == ["_IncludedRouter"]
    assert sum(isinstance(r, APIRoute) for r in app.routes) == 0
    assert [ctx.path for ctx in iter_route_contexts(app.routes)] == [
        "/v2/admin/users",
        "/v2/admin/users/{name}/roles",
    ]
    assert naive_audit_routes(app, acl) == []  # silent pass on FastAPI >= 0.137
    assert len(audit_routes(app, acl)) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mounted_sub_app_skips_the_parent_guard(store: Store, acl: AccessControl) -> None:
    app = build_public_app(store, acl)
    internal = FastAPI(openapi_url=None)

    @internal.put("/users/{name}/roles")
    async def grant(name: str) -> dict[str, str]:
        store.roles[name] = {"admin"}
        return {"granted": name}

    app.mount("/internal", internal)
    r = await call(app, "PUT", "/internal/users/alice/roles")  # anonymous
    assert (r.status_code, store.roles["alice"]) == (200, {"admin"})
    assert audit_routes(app, acl) == [
        Finding("/internal", "", "Mount never runs FastAPI dependencies")
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_override_switches_the_guard_off(store: Store, acl: AccessControl) -> None:
    app = build_public_app(store, acl)
    app.dependency_overrides[acl] = lambda: None
    r = await call(app, "POST", "/invites?role=admin", "tok-alice")
    assert (r.status_code, r.json()) == (201, {"invite": "inv-1", "role": "admin"})
    assert audit_routes(app, acl) == [Finding("*", "", "dependency_overrides is not empty")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_rule_for_get_and_delete_lets_readers_delete(
    store: Store, acl: AccessControl
) -> None:
    app = build_public_app(store, acl)

    @app.api_route("/users/{name}", methods=["GET", "DELETE"])
    @acl.rule()
    async def user(name: str) -> dict[str, str]:
        store.roles.pop(name, None)  # the DELETE branch, reached by any authenticated user
        return {"name": name}

    assert (await call(app, "DELETE", "/users/bob", "tok-alice")).status_code == 200
    assert "bob" not in store.roles
    assert audit_routes(app, acl) == [
        Finding("/users/{name}", "DELETE,GET", "one rule covers safe and unsafe methods")
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrong_method_answers_405_before_authentication(store: Store) -> None:
    admin = build_admin_app(store)
    r = await call(admin, "DELETE", "/admin/users")  # anonymous
    assert (r.status_code, r.headers["allow"]) == (405, "GET")
    head = await call(admin, "HEAD", "/admin/users")  # FastAPI adds no implicit HEAD
    assert (head.status_code, head.headers["allow"]) == (405, "GET")
    assert (await call(admin, "GET", "/admin/users")).status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admin_surface_is_a_separate_app(store: Store, acl: AccessControl) -> None:
    public, admin = build_public_app(store, acl), build_admin_app(store)
    assert not [
        c.path for c in iter_route_contexts(public.routes) if (c.path or "").startswith("/admin")
    ]
    assert (await call(public, "GET", "/admin/users", "tok-ops")).status_code == 404
    assert (await call(admin, "GET", "/admin/users", "tok-ops")).status_code == 200
    assert (await call(admin, "GET", "/admin/users", "tok-sam")).status_code == 403
    with pytest.raises(ValueError, match="role floor"):
        AccessControl(floor=frozenset({"admin"})).rule(public=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_docs_routes_are_unguarded_and_list_privileged_paths(
    store: Store, acl: AccessControl
) -> None:
    app = build_public_app(store, acl, docs=True)
    spec = await call(app, "GET", "/openapi.json")  # anonymous
    assert spec.status_code == 200
    assert {"/invites", "/users/{name}/unlock"} <= spec.json()["paths"].keys()
    assert [f.path for f in audit_routes(app, acl)] == sorted(DOCS_PATHS)
    assert audit_routes(app, acl, allow_unguarded=DOCS_PATHS) == []
