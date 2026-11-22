"""Lab: RFC 9111 Section 4.4 invalidation, Surrogate-Key purging, and RFC 9875 cache groups."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api_labs.f03_http_caching_and_conditional_requests.invalidation import (
    InvalidatingCache,
    invalidation_targets,
    normalize_uri,
    origin_of,
    parse_cache_groups,
    parse_surrogate_key,
    target_uri,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

CACHEABLE = ("cache-control", "public, max-age=60")


class ScriptedOrigin:
    """ASGI origin that answers from a (method, path) table and records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, list[tuple[str, str]], bytes]] = {}
        self.calls: list[tuple[str, str]] = []

    def on(
        self,
        method: str,
        path: str,
        status: int = 200,
        *headers: tuple[str, str],
        body: bytes = b"",
    ) -> None:
        self.routes[(method, path)] = (status, list(headers), body or f"{method} {path}".encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        while (await receive()).get("more_body", False):
            pass
        key = (str(scope["method"]), str(scope["path"]))
        self.calls.append(key)
        status, headers, body = self.routes.get(key, (404, [], b"not found"))
        raw = [(k.encode(), v.encode()) for k, v in headers]
        await send({"type": "http.response.start", "status": status, "headers": raw})
        await send({"type": "http.response.body", "body": body})


def client(app: InvalidatingCache, base_url: str = "http://test") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def catalogue_app() -> Starlette:
    """A product API whose category listing embeds product names (a dependent resource)."""
    names = {7: "boot", 8: "sandal"}

    async def product(request: Request) -> Response:
        pid = int(request.path_params["pid"])
        if request.method == "PUT":
            names[pid] = (await request.json())["name"]
            return Response(status_code=204)
        return JSONResponse(
            {"id": pid, "name": names[pid]},
            headers={"Cache-Control": "max-age=60", "Surrogate-Key": f"product-{pid}"},
        )

    async def category(_: Request) -> Response:
        keys = " ".join(f"product-{pid}" for pid in names)
        return JSONResponse(
            sorted(names.values()),
            headers={"Cache-Control": "max-age=60", "Surrogate-Key": f"category-shoes {keys}"},
        )

    return Starlette(
        routes=[
            Route("/products/{pid:int}", product, methods=["GET", "PUT"]),
            Route("/categories/shoes", category),
        ]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_2xx_invalidates_target_uri_so_next_get_refetches() -> None:
    app = InvalidatingCache(catalogue_app())
    async with client(app) as c:
        assert (await c.get("/products/7")).json()["name"] == "boot"
        assert (await c.get("/products/7")).headers["cache-status"] == "lab; hit"
        assert (await c.put("/products/7", json={"name": "clog"})).status_code == 204
        refetched = await c.get("/products/7")
    assert refetched.json()["name"] == "clog"
    assert refetched.headers["cache-status"] == "lab; fwd=uri-miss; stored"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_target_invalidation_misses_dependent_listing_until_surrogate_key_purge() -> None:
    app = InvalidatingCache(catalogue_app())
    async with client(app) as c:
        await c.get("/categories/shoes")
        await c.put("/products/7", json={"name": "clog"})
        stale = await c.get("/categories/shoes")
        assert stale.headers["cache-status"] == "lab; hit"
        assert stale.json() == ["boot", "sandal"]
        assert app.cache.purge_surrogate_key("product-7") == ("http://test/categories/shoes",)
        fresh = await c.get("/categories/shoes")
    assert fresh.json() == ["clog", "sandal"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_201_invalidates_same_origin_location_and_content_location() -> None:
    origin = ScriptedOrigin()
    for path in ("/orders", "/orders/42", "/orders/42/receipt", "/dashboard"):
        origin.on("GET", path, 200, CACHEABLE)
    origin.on(
        "POST",
        "/orders",
        201,
        ("location", "/orders/42"),
        ("content-location", "http://TEST:80/orders/42/receipt"),
    )
    app = InvalidatingCache(origin)
    async with client(app) as c:
        for path in ("/orders", "/orders/42", "/orders/42/receipt", "/dashboard"):
            await c.get(path)
        await c.post("/orders", content=b"{}")
    assert app.cache.uris() == ("http://test/dashboard",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cross_origin_location_is_never_invalidated() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/victim", 200, CACHEABLE)
    origin.on("POST", "/attack", 303, ("location", "https://test/victim"))
    origin.on("POST", "/attack2", 201, ("content-location", "http://evil.example/victim"))
    app = InvalidatingCache(origin)
    async with client(app) as c, client(app, "https://test") as tls:
        await c.get("/victim")
        await tls.get("/victim")
        await c.post("/attack2")
        await c.post("/attack")
    assert "https://test/victim" in app.cache
    assert "http://test/victim" in app.cache


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "invalidated"),
    [
        (200, True),
        (204, True),
        (303, True),
        (308, True),
        (399, True),
        (400, False),
        (409, False),
        (503, False),
    ],
)
async def test_only_non_error_status_invalidates(status: int, invalidated: bool) -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/r", 200, CACHEABLE)
    origin.on("DELETE", "/r", status)
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/r")
        await c.delete("/r")
    assert ("http://test/r" not in app.cache) is invalidated


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "invalidated"),
    [
        ("OPTIONS", False),
        ("HEAD", False),
        ("POST", True),
        ("PUT", True),
        ("PATCH", True),
        ("DELETE", True),
        ("FROB", True),
    ],
)
async def test_safe_methods_keep_and_unknown_methods_invalidate(
    method: str, invalidated: bool
) -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/r", 200, CACHEABLE)
    origin.on(method, "/r", 200)
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/r")
        assert (await c.request(method, "/r")).status_code == 200
    assert ("http://test/r" not in app.cache) is invalidated


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_request_is_written_through_even_when_a_response_is_stored() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/r", 200, CACHEABLE)
    origin.on("PATCH", "/r", 200)
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/r")
        await c.get("/r")
        patched = await c.patch("/r", content=b"[]")
    assert origin.calls == [("GET", "/r"), ("PATCH", "/r")]
    assert patched.headers["cache-status"] == "lab; fwd=method"


@pytest.mark.unit
def test_invalidation_targets_resolve_relative_refs_and_normalize_origin() -> None:
    targets = invalidation_targets(
        "POST",
        "HTTP://Api.Example.COM:80/v1/carts/9/items?x=1",
        201,
        {"location": "../../orders/5#frag", "content-location": "https://api.example.com/v1/x"},
    )
    assert targets == (
        "http://api.example.com/v1/carts/9/items?x=1",
        "http://api.example.com/v1/orders/5",
    )
    assert invalidation_targets("PUT", "http://a/r", 200, {"location": "urn:isbn:1"}) == (
        "http://a/r",
    )
    assert origin_of("https://[::1]/a") == ("https", "::1", 443)
    assert normalize_uri("https://[::1]:8443/a#b") == "https://[::1]:8443/a"


@pytest.mark.unit
def test_method_tokens_are_case_sensitive_so_lowercase_get_is_unsafe() -> None:
    assert invalidation_targets("GET", "http://a/r", 200, {}) == ()
    assert invalidation_targets("get", "http://a/r", 200, {}) == ("http://a/r",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_soft_purge_serves_nothing_stale_and_refills_on_next_get() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/a", 200, CACHEABLE, ("surrogate-key", "k"))
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/a")
        assert app.cache.purge_surrogate_key("k", soft=True) == ("http://test/a",)
        assert "http://test/a" in app.cache
        again = await c.get("/a")
        assert again.headers["cache-status"] == "lab; fwd=stale; stored"
        assert (await c.get("/a")).headers["cache-status"] == "lab; hit"
    assert origin.calls == [("GET", "/a"), ("GET", "/a")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hard_purge_fans_out_and_leaves_no_dangling_index_entries() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/p1", 200, CACHEABLE, ("surrogate-key", "sku-1 home"))
    origin.on("GET", "/p2", 200, CACHEABLE, ("surrogate-key", "sku-1"))
    origin.on("GET", "/p3", 200, CACHEABLE, ("surrogate-key", "sku-3"))
    app = InvalidatingCache(origin)
    async with client(app) as c:
        for path in ("/p1", "/p2", "/p3"):
            await c.get(path)
    assert app.cache.index_sizes() == (3, 0)
    assert app.cache.purge_surrogate_key("sku-1") == ("http://test/p1", "http://test/p2")
    assert app.cache.index_sizes() == (1, 0)
    assert app.cache.purge_surrogate_key("sku-3") == ("http://test/p3",)
    assert (len(app.cache), app.cache.index_sizes()) == (0, (0, 0))


@pytest.mark.unit
def test_surrogate_key_parsing_applies_fastly_limits() -> None:
    assert parse_surrogate_key("a  b\tA a") == ("a", "b", "A")
    k1024, k1025 = "k" * 1024, "k" * 1025
    assert parse_surrogate_key(f"x {k1024}") == ("x", k1024)
    assert parse_surrogate_key(f"x {k1025} y") == ("x",)
    many = " ".join(f"key-{i:05d}" for i in range(2000))  # 9-byte keys, 1-byte separators
    parsed = parse_surrogate_key(many)
    assert len(parsed) == 1638  # key n ends at byte 10n - 1: 16379 <= 16384 < 16389
    assert parsed[-1] == "key-01637"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_surrogate_key_is_stripped_and_cache_status_added_for_clients() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/a", 200, CACHEABLE, ("surrogate-key", "secret-tenant-42"))
    app = InvalidatingCache(origin)
    async with client(app) as c:
        miss, hit = await c.get("/a"), await c.get("/a")
    for response in (miss, hit):
        assert "surrogate-key" not in response.headers
        assert response.headers.get_list("cache-status") == [response.headers["cache-status"]]


@pytest.mark.unit
def test_cache_groups_parse_as_rfc_9651_list_of_strings() -> None:
    assert parse_cache_groups('"scripts"') == ("scripts",)
    assert parse_cache_groups(' "a\\"b";x=1;y="z", "c\\\\";flag ,\t"a\\"b"') == ('a"b', "c\\")
    assert parse_cache_groups('"Eurovision", "eurovision"') == ("Eurovision", "eurovision")
    assert parse_cache_groups('"a";b=?1;c=tok/x;d=-1.5, "e"') == ("a", "e")
    bad_values = ('"open', "token", '"a",', '"a" "b"', '"caf\\u"', '"a";X=1', '"a" ;x=1')
    for bad in (*bad_values, '"a";b=?2', '"tab\there"', '"a";=1'):
        assert parse_cache_groups(bad) == (), bad


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_group_invalidation_on_unsafe_response_is_scoped_to_origin() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/p/1", 200, CACHEABLE, ("cache-groups", '"products"'))
    origin.on("GET", "/p/2", 200, CACHEABLE, ("cache-groups", '"products", "home"'))
    origin.on("GET", "/about", 200, CACHEABLE, ("cache-groups", '"static"'))
    origin.on("POST", "/admin/reprice", 200, ("cache-group-invalidation", '"products"'))
    app = InvalidatingCache(origin)
    async with client(app) as c, client(app, "http://other") as other:
        for path in ("/p/1", "/p/2", "/about"):
            await c.get(path)
        await other.get("/p/1")
        await c.post("/admin/reprice")
    assert app.cache.uris() == ("http://other/p/1", "http://test/about")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_cache_group_invalidation_is_ignored_on_safe_method_responses(method: str) -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/p/1", 200, CACHEABLE, ("cache-groups", '"products"'))
    origin.on(method, "/evil", 200, ("cache-group-invalidation", '"products"'))
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/p/1")
        assert (await c.request(method, "/evil")).status_code == 200
    assert "http://test/p/1" in app.cache


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("fanout", "left"), [(True, ("/c",)), (False, ("/b", "/c"))])
async def test_group_fanout_from_uri_invalidation_does_not_cascade(
    fanout: bool, left: tuple[str, ...]
) -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/a", 200, CACHEABLE, ("cache-groups", '"x"'))
    origin.on("GET", "/b", 200, CACHEABLE, ("cache-groups", '"x", "y"'))
    origin.on("GET", "/c", 200, CACHEABLE, ("cache-groups", '"y"'))
    origin.on("PUT", "/a", 204)
    app = InvalidatingCache(origin, group_fanout=fanout)
    async with client(app) as c:
        for path in ("/a", "/b", "/c"):
            await c.get(path)
        await c.put("/a", content=b"{}")
    assert app.cache.uris() == tuple(f"http://test{path}" for path in left)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalidation_is_local_to_the_cache_the_write_traverses() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/r", 200, CACHEABLE, ("surrogate-key", "r"))
    origin.on("PUT", "/r", 204)
    edge_a, edge_b = InvalidatingCache(origin, name="a"), InvalidatingCache(origin, name="b")
    async with client(edge_a) as a, client(edge_b) as b:
        await a.get("/r")
        await b.get("/r")
        await a.put("/r", content=b"{}")
        assert (await b.get("/r")).headers["cache-status"] == "b; hit"
        for edge in (edge_a, edge_b):
            edge.cache.purge_surrogate_key("r")
        assert (await b.get("/r")).headers["cache-status"] == "b; fwd=uri-miss; stored"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalidation_completes_before_the_response_reaches_the_client() -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/r", 200, CACHEABLE)
    origin.on("PUT", "/r", 204)
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/r")
    seen_at_start: list[bool] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            seen_at_start.append("http://test/r" in app.cache)

    scope: Scope = {
        "type": "http",
        "method": "PUT",
        "scheme": "http",
        "path": "/r",
        "raw_path": b"/r",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    assert seen_at_start == [False]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("guard", "served"), [(True, "v2"), (False, "v1")])
async def test_refill_that_started_before_a_write_is_discarded(guard: bool, served: str) -> None:
    state = {"version": "v1"}
    read_started, release = asyncio.Event(), asyncio.Event()

    async def resource(request: Request) -> Response:
        if request.method == "PUT":
            state["version"] = "v2"
            return Response(status_code=204)
        snapshot = state["version"]
        read_started.set()
        await release.wait()
        return Response(snapshot, headers={"Cache-Control": "max-age=60"})

    app = InvalidatingCache(
        Starlette(routes=[Route("/r", resource, methods=["GET", "PUT"])]), guard_refill=guard
    )
    async with asyncio.timeout(5), client(app) as c:
        slow_read = asyncio.create_task(c.get("/r"))
        await read_started.wait()
        await c.put("/r", content=b"v2")
        release.set()
        assert (await slow_read).text == "v1"
        assert (await c.get("/r")).text == served


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cache_control", "stored"),
    [
        ("max-age=60", True),
        ("s-maxage=60", True),
        ("no-store, max-age=60", False),
        ("private, max-age=60", False),
        ("no-cache", False),
    ],
)
async def test_only_shared_cacheable_get_responses_are_stored(
    cache_control: str, stored: bool
) -> None:
    origin = ScriptedOrigin()
    origin.on("GET", "/r", 200, ("cache-control", cache_control))
    app = InvalidatingCache(origin)
    async with client(app) as c:
        await c.get("/r")
    assert ("http://test/r" in app.cache) is stored


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_and_a_silent_origin_is_an_error() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(str(scope["type"]))

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        raise AssertionError(message)

    app = InvalidatingCache(inner)
    await app({"type": "lifespan"}, receive, send)
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/r",
        "query_string": b"q=1",
        "headers": [],
        "server": ("10.0.0.1", 8080),
    }
    assert target_uri(scope) == "http://10.0.0.1:8080/r?q=1"
    with pytest.raises(RuntimeError, match=r"http\.response\.start"):
        await app(scope, receive, send)
    assert seen == ["lifespan", "http"]
