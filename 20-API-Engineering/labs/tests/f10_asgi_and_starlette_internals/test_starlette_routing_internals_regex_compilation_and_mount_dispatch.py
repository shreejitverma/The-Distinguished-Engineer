"""Starlette 1.10.0 routing: regex compilation, convertors, Mount, Host, and 405 vs 404."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import NoMatchFound, Route, Router, compile_path

from api_labs.f10_asgi_and_starlette_internals.starlette_routing_internals import (
    RejectControlCharactersMiddleware,
    build_demo_app,
    describe_pattern,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Message

ALLOW_PROBE = (
    "from starlette.routing import Route\n"
    "async def e(r): ...\n"
    "r = Route('/u/{name}', e, methods=['GET', 'POST', 'PUT', 'DELETE'])\n"
    "print(', '.join(r.methods))\n"
)
ASSERT_PROBE = (
    "from starlette.routing import Route\n"
    "async def e(r): ...\n"
    "print(Route('/u/{name}', e).url_path_for('e', name='a/b'))\n"
)


def client(app: ASGIApp, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
def test_compile_path_builds_anchored_named_groups_from_convertor_regexes() -> None:
    assert describe_pattern("/users/{uid:int}/files/{rest:path}") == (
        "^/users/(?P<uid>[0-9]+)/files/(?P<rest>.*)$",
        "/users/{uid}/files/{rest}",
        {"uid": "IntegerConvertor", "rest": "PathConvertor"},
    )
    # Literal text between parameters is re.escape()d, so "." matches only a dot.
    assert describe_pattern("/v1.0/{name}")[0] == r"^/v1\.0/(?P<name>[^/]+)$"


@pytest.mark.unit
def test_host_pattern_drops_the_port_and_compiles_case_insensitive() -> None:
    regex, path_format, _ = compile_path("{sub}.example.org:8000")
    assert regex.pattern == r"^(?P<sub>[^/]+)\.example\.org$"
    assert regex.flags & re.IGNORECASE
    assert path_format == "{sub}.example.org:8000"
    assert not compile_path("/{sub}")[0].flags & re.IGNORECASE


@pytest.mark.unit
def test_duplicate_parameter_names_fail_at_route_construction() -> None:
    with pytest.raises(
        ValueError, match=re.escape("Duplicated param name id at path /a/{id}/b/{id}")
    ):
        compile_path("/a/{id}/b/{id}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_convertors_gate_the_match_and_type_the_parameter() -> None:
    async with client(build_demo_app()) as c:
        as_int = (await c.get("/n/42")).json()
        assert as_int["param_types"] == {"value": "int"}
        assert (await c.get("/f/1.5")).json()["param_types"] == {"value": "float"}
        assert (await c.get("/f/1e5")).status_code == 404  # no exponent in the float regex
        uuid_reply = (await c.get("/u/12345678123456781234567812345678")).json()
        assert uuid_reply["path_params"] == {"value": "12345678-1234-5678-1234-567812345678"}
        # scope["path"] is percent-decoded (ASGI), so %2F is a real "/" that [^/]+ refuses.
        assert (await c.get("/s/a%2Fb")).status_code == 404
        assert (await c.get("/p/a%2Fb/c")).json()["path_params"] == {"value": "a/b/c"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_full_match_wins_so_declaration_order_shadows_a_literal_route() -> None:
    async with client(build_demo_app()) as c:
        reply = (await c.get("/users/me")).json()
    assert reply["route"] == "/users/{name}"
    assert reply["path_params"] == {"name": "me"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_405_allow_lists_only_the_first_partial_route_not_every_handler() -> None:
    async with client(build_demo_app()) as c:
        reply = await c.delete("/items")
        assert reply.status_code == 405
        assert set(reply.headers["allow"].split(", ")) == {"GET", "HEAD"}  # POST is missing
        assert (await c.post("/items")).status_code == 201  # yet POST is served


@pytest.mark.unit
@pytest.mark.asyncio
async def test_method_table_route_advertises_every_method_in_sorted_order() -> None:
    async with client(build_demo_app()) as c:
        reply = await c.delete("/things")
        assert reply.status_code == 405
        assert reply.headers["allow"] == "GET, HEAD, POST"
        assert (await c.post("/things")).status_code == 201
        head = await c.head("/things")
        assert (head.status_code, head.content) == (200, b"")


@pytest.mark.unit
def test_allow_header_order_follows_string_hash_randomisation() -> None:
    orders: set[str] = set()
    for seed in range(6):
        result = subprocess.run(  # noqa: S603 - fixed argv, sys.executable, no shell
            [sys.executable, "-c", ALLOW_PROBE],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": str(seed)},
        )
        orders.add(result.stdout.strip())
    expected = {"GET", "HEAD", "POST", "PUT", "DELETE"}
    assert all(set(order.split(", ")) == expected for order in orders)
    assert len(orders) > 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_405_inside_starlette_is_an_httpexception_a_handler_can_rewrite() -> None:
    async def problem(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        return JSONResponse({"title": "method not allowed"}, 405, headers=exc.headers)

    async def only_get(request: Request) -> Response:
        return PlainTextResponse("ok")

    routes = [Route("/r", only_get)]
    app = Starlette(routes=routes, exception_handlers={405: problem})
    async with client(app) as c, client(Router(routes=routes)) as bare:
        wrapped = await c.post("/r")
        plain = await bare.post("/r")
    assert (wrapped.status_code, wrapped.json()) == (405, {"title": "method not allowed"})
    assert set(wrapped.headers["allow"].split(", ")) == {"GET", "HEAD"}
    assert (plain.status_code, plain.text) == (405, "Method Not Allowed")  # no "app" in scope


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mount_moves_the_prefix_into_root_path_and_merges_params() -> None:
    async with client(build_demo_app()) as c:
        api = (await c.get("/api/users/7")).json()
        org = (await c.get("/org/acme/users/3")).json()
    assert api == {
        "path_params": {"uid": "7"},
        "param_types": {"uid": "int"},
        "route": "/users/{uid:int}",
        "root_path": "/api",
        "path": "/api/users/7",
    }
    assert org["path_params"] == {"org": "acme", "uid": "3"}
    assert (org["root_path"], org["route"]) == ("/org/acme", "/users/{uid:int}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bare_mount_prefix_and_toggled_slash_redirect_with_307() -> None:
    async with client(build_demo_app()) as c:
        for method in ("GET", "POST"):
            reply = await c.request(method, "/api")
            assert (reply.status_code, reply.headers["location"]) == (307, "http://test/api/")
        trailing = await c.get("/items/")
        assert (trailing.status_code, trailing.headers["location"]) == (307, "http://test/items")
    async with client(build_demo_app(redirect_slashes=False)) as c:
        assert (await c.get("/items/")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_later_mount_turns_an_earlier_partial_405_into_a_404() -> None:
    async with client(build_demo_app()) as c:
        assert (await c.post("/api/upload")).status_code == 201
        assert (await c.get("/api/upload")).status_code == 404  # Mount is FULL, beats PARTIAL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_host_route_lowercases_ignores_port_and_str_spans_dots() -> None:
    async with client(build_demo_app()) as c:
        reply = await c.get("/", headers={"host": "A.B.Example.ORG:8443"})
        assert reply.json()["path_params"] == {"sub": "a.b"}
        assert (await c.get("/", headers={"host": "bad host"})).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dollar_anchor_matches_before_a_trailing_newline() -> None:
    async with client(build_demo_app()) as c:
        assert (await c.get("/health%0A")).status_code == 200
        newline = (await c.get("/n/5%0A")).json()
        assert (newline["path"], newline["path_params"]) == ("/n/5\n", {"value": "5"})
    async with client(RejectControlCharactersMiddleware(build_demo_app())) as c:
        assert (await c.get("/health%0A")).status_code == 400
        assert (await c.get("/health")).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unbounded_int_convertor_turns_a_long_id_into_a_500() -> None:
    digits = "9" * 5000
    async with client(build_demo_app(), raise_app_exceptions=False) as c:
        assert (await c.get(f"/n/{digits}")).status_code == 500
        assert (await c.get(f"/b/{digits}")).status_code == 404
        assert (await c.get("/b/123")).json()["param_types"] == {"value": "int"}
    with pytest.raises(ValueError, match="4300 digits"):
        async with client(build_demo_app()) as c:
            await c.get(f"/n/{digits}")


@pytest.mark.unit
def test_url_path_for_resolves_mount_names_and_trusts_assert_for_validation() -> None:
    app = build_demo_app()
    assert app.url_path_for("api:user", uid=3) == "/api/users/3"
    assert app.url_path_for("org:user", org="acme", uid=3) == "/org/acme/users/3"
    with pytest.raises(NoMatchFound):
        app.url_path_for("api:user", uid=3, extra=1)
    with pytest.raises(AssertionError, match="May not contain path separators"):
        app.url_path_for("echo", name="a/b")
    optimised = subprocess.run(  # noqa: S603 - fixed argv, sys.executable, no shell
        [sys.executable, "-O", "-c", ASSERT_PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    assert optimised.stdout.strip() == "/u/a/b"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_scope_skips_http_routes_and_closes_with_1000() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "websocket",
        "path": "/health",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "query_string": b"",
        "scheme": "ws",
    }
    await build_demo_app()(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1000, "reason": ""}]
