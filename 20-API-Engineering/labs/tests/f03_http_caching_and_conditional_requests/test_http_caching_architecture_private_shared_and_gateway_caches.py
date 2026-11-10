"""Unit tests for RFC 9111 cacheability rules in private, shared, and gateway caches."""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from litestar import Litestar, Request, Response, get
from litestar.config.response_cache import ResponseCacheConfig, default_cache_key_builder
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f03_http_caching_and_conditional_requests.cacheability import (
    DELTA_SECONDS_INFINITY,
    CacheKind,
    HTTPCache,
    Reason,
    freshness_lifetime,
    may_store,
    parse_cache_control,
    parse_delta_seconds,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from starlette.requests import Request as StarletteRequest

SHARED = CacheKind.SHARED
PRIVATE = CacheKind.PRIVATE
AUTH = [("authorization", "Bearer alice")]
URI = "http://test/orders/7"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "method", "status", "request_headers", "response_headers", "expected"),
    [
        (SHARED, "GET", 200, AUTH, [("cache-control", "max-age=60")], Reason.AUTHORIZATION),
        (PRIVATE, "GET", 200, AUTH, [("cache-control", "max-age=60")], Reason.STORABLE),
        (SHARED, "GET", 200, AUTH, [("cache-control", "public, max-age=60")], Reason.STORABLE),
        (SHARED, "GET", 200, AUTH, [("cache-control", "s-maxage=60")], Reason.STORABLE),
        (
            SHARED,
            "GET",
            200,
            AUTH,
            [("cache-control", "must-revalidate, max-age=60")],
            Reason.STORABLE,
        ),
        (
            SHARED,
            "GET",
            200,
            AUTH,
            [("cache-control", "proxy-revalidate, max-age=60")],
            Reason.AUTHORIZATION,
        ),
        (SHARED, "GET", 200, [], [("cache-control", "private, max-age=60")], Reason.PRIVATE),
        (PRIVATE, "GET", 302, [], [("cache-control", "private")], Reason.STORABLE),
        (PRIVATE, "GET", 302, [], [], Reason.NO_PERMISSION),
        (PRIVATE, "GET", 302, [], [("cache-control", "max-age=10")], Reason.STORABLE),
        (PRIVATE, "GET", 302, [], [("cache-control", "s-maxage=10")], Reason.NO_PERMISSION),
        (SHARED, "GET", 302, [], [("cache-control", "s-maxage=10")], Reason.STORABLE),
        (SHARED, "GET", 404, [], [], Reason.STORABLE),
        (SHARED, "GET", 302, [], [("cache-control", 'community="UCI"')], Reason.NO_PERMISSION),
        (SHARED, "GET", 200, [], [("expires", "0")], Reason.STORABLE),
        (PRIVATE, "GET", 200, [], [("Cache-Control", "NO-STORE")], Reason.NO_STORE),
        (
            SHARED,
            "GET",
            200,
            [],
            [("cache-control", "must-understand, no-store, max-age=60")],
            Reason.STORABLE,
        ),
        (
            SHARED,
            "GET",
            418,
            [],
            [("cache-control", "must-understand, no-store, max-age=60")],
            Reason.STATUS_NOT_UNDERSTOOD,
        ),
        (SHARED, "GET", 206, [], [("cache-control", "max-age=60")], Reason.STATUS_NOT_UNDERSTOOD),
        (SHARED, "GET", 304, [], [("cache-control", "max-age=60")], Reason.STATUS_NOT_UNDERSTOOD),
        (SHARED, "GET", 103, [], [("cache-control", "max-age=60")], Reason.STATUS_NOT_FINAL),
        (SHARED, "get", 200, [], [("cache-control", "max-age=60")], Reason.METHOD_NOT_UNDERSTOOD),
        (
            SHARED,
            "PROPFIND",
            200,
            [],
            [("cache-control", "max-age=60")],
            Reason.METHOD_NOT_UNDERSTOOD,
        ),
        (
            SHARED,
            "GET",
            200,
            [("cache-control", "no-store")],
            [("cache-control", "max-age=60")],
            Reason.REQUEST_NO_STORE,
        ),
        (
            SHARED,
            "GET",
            200,
            [],
            [("cache-control", "max-age=60"), ("set-cookie", "sid=alice")],
            Reason.STORABLE,
        ),
        (
            SHARED,
            "POST",
            200,
            [],
            [("cache-control", "max-age=60"), ("content-location", "/orders/7")],
            Reason.STORABLE,
        ),
        (SHARED, "POST", 200, [], [("cache-control", "max-age=60")], Reason.POST_NOT_CACHEABLE),
        (
            SHARED,
            "POST",
            200,
            [],
            [("cache-control", "max-age=60"), ("content-location", "/orders/8")],
            Reason.POST_NOT_CACHEABLE,
        ),
        (
            SHARED,
            "POST",
            200,
            [],
            [("cache-control", "public"), ("content-location", "/orders/7")],
            Reason.POST_NOT_CACHEABLE,
        ),
    ],
    ids=[
        "shared-auth-max-age-refused",
        "private-auth-max-age-stored",
        "shared-auth-public-stored",
        "shared-auth-s-maxage-stored",
        "shared-auth-must-revalidate-stored",
        "shared-auth-proxy-revalidate-refused",
        "shared-private-refused",
        "private-cache-private-302-stored",
        "302-no-freshness-refused",
        "302-max-age-stored",
        "private-cache-ignores-s-maxage",
        "shared-cache-302-s-maxage-stored",
        "404-heuristically-cacheable",
        "unknown-extension-ignored",
        "invalid-expires-still-permits-storage",
        "no-store-case-insensitive",
        "must-understand-overrides-no-store",
        "must-understand-unknown-status",
        "206-without-range-support",
        "304-without-validation-support",
        "1xx-not-final",
        "method-token-case-sensitive",
        "unknown-method",
        "request-no-store",
        "set-cookie-does-not-inhibit",
        "post-with-content-location",
        "post-without-content-location",
        "post-content-location-elsewhere",
        "post-public-without-explicit-freshness",
    ],
)
def test_storage_decision_table(
    kind: CacheKind,
    method: str,
    status: int,
    request_headers: list[tuple[str, str]],
    response_headers: list[tuple[str, str]],
    expected: Reason,
) -> None:
    decision = may_store(
        kind=kind,
        method=method,
        target_uri=URI,
        status=status,
        request_headers=request_headers,
        response_headers=response_headers,
    )
    assert decision.reason is expected
    assert decision.storable is (expected is Reason.STORABLE)


@pytest.mark.unit
def test_qualified_private_strips_only_listed_fields_in_shared_cache() -> None:
    headers = [("cache-control", 'private="Set-Cookie, X-Tenant", max-age=60')]
    shared = may_store(
        kind=SHARED,
        method="GET",
        target_uri=URI,
        status=200,
        request_headers=[],
        response_headers=headers,
    )
    private = may_store(
        kind=PRIVATE,
        method="GET",
        target_uri=URI,
        status=200,
        request_headers=[],
        response_headers=headers,
    )
    assert shared.storable
    assert shared.omit_fields == frozenset({"set-cookie", "x-tenant"})
    assert private.storable
    assert private.omit_fields == frozenset()


@pytest.mark.unit
def test_parse_cache_control_quoted_lists_case_and_first_occurrence() -> None:
    parsed = parse_cache_control(['Private="a, b", MAX-AGE=5', "max-age=99, no-cache"])
    assert parsed == {"private": "a, b", "max-age": "5", "no-cache": None}


@pytest.mark.unit
def test_parse_cache_control_escapes_and_malformed_arguments() -> None:
    parsed = parse_cache_control(['no-cache="x\\"y", max-age = 5, public junk'])
    assert parsed == {"no-cache": 'x"y', "max-age": None, "public": None}
    lifetime = freshness_lifetime(SHARED, [("cache-control", "max-age = 5")], received_at=0.0)
    assert lifetime == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_scope_passes_through_the_cache() -> None:
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        events.append("startup")
        yield
        events.append("shutdown")

    cache = HTTPCache(Starlette(lifespan=lifespan), kind=SHARED, clock=FakeClock())
    async with LifespanManager(cache):
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("0", 0),
        ("60", 60),
        ("2147483648", DELTA_SECONDS_INFINITY),
        ("99999999999", DELTA_SECONDS_INFINITY),
        ("9" * 5000, DELTA_SECONDS_INFINITY),
        ("-1", None),
        ("1.5", None),
        ("", None),
        (None, None),
        ("\u0661\u0662", None),
    ],
    ids=[
        "zero",
        "plain",
        "cap",
        "overflow",
        "5000-digits",
        "negative",
        "float",
        "empty",
        "none",
        "non-ascii-digits",
    ],
)
def test_parse_delta_seconds(arg: str | None, expected: int | None) -> None:
    assert parse_delta_seconds(arg) == expected


@pytest.mark.unit
def test_freshness_lifetime_first_match_per_cache_kind() -> None:
    both = [("cache-control", "max-age=60, s-maxage=600")]
    assert freshness_lifetime(SHARED, both, received_at=0.0) == 600
    assert freshness_lifetime(PRIVATE, both, received_at=0.0) == 60
    dated = [
        ("date", "Mon, 09 Nov 2026 00:00:00 GMT"),
        ("expires", "Mon, 09 Nov 2026 00:05:00 GMT"),
    ]
    assert freshness_lifetime(SHARED, dated, received_at=0.0) == 300
    assert freshness_lifetime(SHARED, [("expires", "0")], received_at=0.0) == 0
    received = 1_794_182_400.0  # Mon, 09 Nov 2026 00:00:00 GMT
    undated = [("expires", "Mon, 09 Nov 2026 00:01:00 GMT")]
    assert freshness_lifetime(SHARED, undated, received_at=received) == 60
    assert freshness_lifetime(SHARED, [("cache-control", "public")], received_at=0.0) == 0


@dataclass
class FakeClock:
    now: float = 1_794_182_400.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Origin:
    """A Starlette origin whose per-path response headers the test controls."""

    cache_control: str = "max-age=60"
    extra: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    calls: list[str] = field(default_factory=list[str])

    def app(self) -> Starlette:
        async def order(request: StarletteRequest) -> PlainTextResponse:
            self.calls.append(request.method)
            if request.method == "DELETE":
                return PlainTextResponse("", status_code=204)
            user = request.headers.get("authorization") or request.headers.get("x-user-id")
            headers = {"cache-control": self.cache_control, **dict(self.extra)}
            if request.method == "POST":
                headers["content-location"] = "/orders/7"
            return PlainTextResponse(f"order 7 for {user}", headers=headers)

        return Starlette(routes=[Route("/orders/7", order, methods=["GET", "POST", "DELETE"])])


def _client(app: Callable[..., Awaitable[None]]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_cache_refuses_authenticated_max_age_response() -> None:
    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        alice = await client.get("/orders/7", headers={"authorization": "Bearer alice"})
        bob = await client.get("/orders/7", headers={"authorization": "Bearer bob"})
    assert alice.text == "order 7 for Bearer alice"
    assert bob.text == "order 7 for Bearer bob"
    assert origin.calls == ["GET", "GET"]
    assert cache.entries == {}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache_control",
    ["public, max-age=60", "s-maxage=60", "must-revalidate, max-age=60"],
)
async def test_directives_that_unlock_shared_storage_leak_across_users(cache_control: str) -> None:
    origin = Origin(cache_control=cache_control)
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7", headers={"authorization": "Bearer alice"})
        bob = await client.get("/orders/7", headers={"authorization": "Bearer bob"})
    assert bob.text == "order 7 for Bearer alice"
    assert bob.headers["age"] == "0"
    assert origin.calls == ["GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_that_rewrites_authorization_disables_the_section_3_5_guard() -> None:
    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7", headers={"x-user-id": "alice"})
        leaked = await client.get("/orders/7", headers={"x-user-id": "bob"})
    assert leaked.text == "order 7 for alice"

    origin = Origin(cache_control="private, max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7", headers={"x-user-id": "alice"})
        safe = await client.get("/orders/7", headers={"x-user-id": "bob"})
    assert safe.text == "order 7 for bob"
    assert origin.calls == ["GET", "GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_private_cache_reuses_authenticated_response_until_it_is_stale() -> None:
    clock = FakeClock()
    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=PRIVATE, clock=clock)
    async with _client(cache) as client:
        first = await client.get("/orders/7", headers=dict(AUTH))
        clock.now += 59
        hit = await client.get("/orders/7", headers=dict(AUTH))
        clock.now += 1
        refetched = await client.get("/orders/7", headers=dict(AUTH))
    assert "age" not in first.headers
    assert hit.headers["age"] == "59"
    assert hit.text == first.text
    assert "age" not in refetched.headers
    assert origin.calls == ["GET", "GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qualified_private_field_is_never_replayed_by_shared_cache() -> None:
    origin = Origin(cache_control='private="set-cookie", max-age=60')
    origin.extra = [("set-cookie", "sid=alice-session")]
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        first = await client.get("/orders/7")
        hit = await client.get("/orders/7")
    assert first.headers["set-cookie"] == "sid=alice-session"
    assert hit.headers["age"] == "0"
    assert "set-cookie" not in hit.headers
    assert origin.calls == ["GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_connection_specific_fields_are_not_stored() -> None:
    origin = Origin(cache_control="max-age=60")
    origin.extra = [("connection", "x-hop"), ("x-hop", "1"), ("keep-alive", "timeout=5")]
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        first = await client.get("/orders/7")
        hit = await client.get("/orders/7")
    assert first.headers["x-hop"] == "1"
    assert hit.headers["age"] == "0"
    assert {"connection", "x-hop", "keep-alive"}.isdisjoint(hit.headers.keys())
    assert hit.headers["cache-control"] == "max-age=60"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_writes_through_and_its_response_can_answer_a_later_get() -> None:
    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.post("/orders/7", headers={"x-user-id": "writer"})
        await client.post("/orders/7", headers={"x-user-id": "writer"})
        read = await client.get("/orders/7", headers={"x-user-id": "reader"})
    assert origin.calls == ["POST", "POST"]
    assert read.text == "order 7 for writer"
    assert read.headers["content-location"] == "/orders/7"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_method_invalidates_target_uri() -> None:
    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7")
        await client.get("/orders/7")
        deleted = await client.delete("/orders/7")
        await client.get("/orders/7")
    assert deleted.status_code == 204
    assert origin.calls == ["GET", "DELETE", "GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vary_and_request_no_cache_bypass_storage_and_reuse() -> None:
    origin = Origin(cache_control="max-age=60")
    origin.extra = [("vary", "accept-language")]
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7")
        await client.get("/orders/7")
    assert origin.calls == ["GET", "GET"]

    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7")
        await client.get("/orders/7", headers={"cache-control": "no-cache"})
        await client.get("/orders/7")
    assert origin.calls == ["GET", "GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_received_age_counts_toward_staleness_and_is_replaced_on_replay() -> None:
    clock = FakeClock()
    origin = Origin(cache_control="max-age=60")
    origin.extra = [("age", "30")]
    cache = HTTPCache(origin.app(), kind=SHARED, clock=clock)
    async with _client(cache) as client:
        await client.get("/orders/7")
        clock.now += 29
        hit = await client.get("/orders/7")
        clock.now += 1
        await client.get("/orders/7")
    assert hit.headers.get_list("age") == ["59"]
    assert origin.calls == ["GET", "GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_no_cache_is_stored_but_never_reused_without_validation() -> None:
    origin = Origin(cache_control="no-cache, max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7")
        await client.get("/orders/7")
    assert list(cache.entries) == ["http://test/orders/7"]
    assert origin.calls == ["GET", "GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_key_includes_the_query() -> None:
    origin = Origin(cache_control="max-age=60")
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        await client.get("/orders/7?view=full")
        await client.get("/orders/7?view=summary")
        await client.get("/orders/7?view=full")
    assert sorted(cache.entries) == [
        "http://test/orders/7?view=full",
        "http://test/orders/7?view=summary",
    ]
    assert origin.calls == ["GET", "GET"]


def _litestar_app(key_builder: Any = None) -> Litestar:
    @get("/me", cache=60, cache_key_builder=key_builder)
    async def me(request: Request[Any, Any, Any]) -> Response[str]:
        return Response(
            f"profile of {request.headers.get('authorization')}",
            media_type="text/plain",
            headers={"cache-control": "private, no-store"},
        )

    return Litestar([me], response_cache_config=ResponseCacheConfig())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_litestar_response_cache_ignores_authorization_and_no_store() -> None:
    async with _client(_litestar_app()) as client:
        alice = await client.get("/me", headers={"authorization": "Bearer alice"})
        bob = await client.get("/me", headers={"authorization": "Bearer bob"})
    assert alice.headers["cache-control"] == "private, no-store"
    assert bob.text == "profile of Bearer alice"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_litestar_default_key_is_method_path_and_sorted_query_only() -> None:
    seen: list[str] = []

    def spy(request: Request[Any, Any, Any]) -> str:
        key = default_cache_key_builder(request)
        seen.append(key)
        return key

    async with _client(_litestar_app(spy)) as client:
        await client.get("/me?b=2&a=1", headers={"authorization": "Bearer alice"})
        await client.get("/me?a=1&b=2", headers={"authorization": "Bearer bob"})
    assert seen[0] == "GET/mea=1&b=2"
    assert set(seen) == {"GET/mea=1&b=2"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_litestar_per_credential_key_turns_the_app_cache_private() -> None:
    def per_user(request: Request[Any, Any, Any]) -> str:
        credential = request.headers.get("authorization", "")
        digest = hashlib.sha256(credential.encode()).hexdigest()
        return f"{default_cache_key_builder(request)}|{digest}"

    async with _client(_litestar_app(per_user)) as client:
        await client.get("/me", headers={"authorization": "Bearer alice"})
        bob = await client.get("/me", headers={"authorization": "Bearer bob"})
    assert bob.text == "profile of Bearer bob"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_name",
    [
        "proxy-connection",
        "keep-alive",
        "te",
        "transfer-encoding",
        "upgrade",
        "proxy-authenticate",
        "proxy-authentication-info",
        "proxy-authorization",
    ],
)
async def test_each_never_stored_field_is_dropped_from_the_replay(field_name: str) -> None:
    origin = Origin(cache_control="max-age=60")
    origin.extra = [(field_name, "x")]
    cache = HTTPCache(origin.app(), kind=SHARED, clock=FakeClock())
    async with _client(cache) as client:
        first = await client.get("/orders/7")
        hit = await client.get("/orders/7")
    assert first.headers[field_name] == "x"
    assert hit.headers["age"] == "0"
    assert field_name not in hit.headers
