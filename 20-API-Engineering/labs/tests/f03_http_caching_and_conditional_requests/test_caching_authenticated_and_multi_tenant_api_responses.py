"""Lab: how a shared cache leaks authenticated and multi-tenant API responses, and what stops it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f03_http_caching_and_conditional_requests.multi_tenant_cache import (
    MAX_AGE_ONLY,
    PRIVATE,
    PUBLIC,
    TENANT_SHARED,
    CachePolicy,
    Origin,
    SharedCache,
    TenantEdge,
    combined_headers,
    delta_seconds,
    parse_cache_control,
    refusal_reason,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp


SIGNING_KEY = "lab-only-signing-key"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def get(
    app: ASGIApp, path: str, *, user: str | None = None, **headers: str
) -> httpx.Response:
    """Send one GET as a fresh client; `user` sends that user's bearer token from `TOKENS`."""
    if user is not None:
        headers["Authorization"] = f"Bearer tok-{user}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers)


def stack(policy: CachePolicy) -> tuple[Origin, SharedCache]:
    """Client -> SharedCache -> Origin, with no gateway in between."""
    origin = Origin(policy)
    return origin, SharedCache(origin.app(), clock=FakeClock())


@pytest.mark.unit
def test_parse_cache_control_lowercases_unquotes_and_keeps_quoted_commas() -> None:
    parsed = parse_cache_control(
        'Private="Set-Cookie, X-Token", S-MAXAGE=60, max-age=1, max-age=9, ext="a\\",b"'
    )

    assert parsed == {
        "private": "Set-Cookie, X-Token",
        "s-maxage": "60",
        "max-age": "1",
        "ext": 'a\\",b',
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cache_control", "authorization", "reason"),
    [
        ("max-age=60", False, None),
        ("max-age=60", True, "authorization"),
        ("public, max-age=60", True, None),
        ("s-maxage=60", True, None),
        ("max-age=60, must-revalidate", True, None),
        ("private, max-age=60", False, "private"),
        ("public, private, max-age=60", True, "private"),
        ("no-store, public, max-age=60", True, "no-store"),
        ("public", True, "no-freshness"),
        ("max-age=0, s-maxage=60", True, None),
        ('s-maxage="60"', True, None),
        ("s-maxage=-1", True, "no-freshness"),
    ],
    ids=[
        "anonymous-max-age",
        "authorization-max-age",
        "public-unlocks",
        "s-maxage-unlocks",
        "must-revalidate-unlocks",
        "private",
        "private-beats-public",
        "no-store-beats-public",
        "public-without-lifetime",
        "s-maxage-overrides-max-age-0",
        "quoted-s-maxage-accepted",
        "invalid-s-maxage-only",
    ],
)
def test_shared_cache_storage_rules_for_authorization_requests(
    cache_control: str, authorization: bool, reason: str | None
) -> None:
    got = refusal_reason(
        status=200,
        request_has_authorization=authorization,
        directives=parse_cache_control(cache_control),
    )

    assert got == reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_without_a_tenant_key_serves_acme_invoices_to_globex() -> None:
    origin, cache = stack(PUBLIC)

    first = await get(cache, "/v1/invoices", user="alice")
    leaked = await get(cache, "/v1/invoices", user="mallory")

    assert first.headers["cache-status"] == "lab; fwd=uri-miss; stored"
    assert leaked.headers["cache-status"] == "lab; hit"
    assert leaked.json() == {"tenant": "acme", "invoices": ["INV-A1", "INV-A2"]}
    assert origin.calls == [("/v1/invoices", "alice")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_age_alone_is_not_shared_across_authorization_requests() -> None:
    origin, cache = stack(MAX_AGE_ONLY)

    first = await get(cache, "/v1/invoices", user="alice")
    second = await get(cache, "/v1/invoices", user="mallory")

    assert first.headers["cache-status"] == "lab; fwd=uri-miss; detail=authorization"
    assert second.headers["cache-status"] == "lab; fwd=uri-miss; detail=authorization"
    assert second.json() == {"tenant": "globex", "invoices": ["INV-G1"]}
    assert origin.calls == [("/v1/invoices", "alice"), ("/v1/invoices", "mallory")]
    assert cache.entries == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_private_is_never_stored_by_the_shared_cache() -> None:
    origin, cache = stack(PRIVATE)

    first = await get(cache, "/v1/me", user="alice")
    second = await get(cache, "/v1/me", user="alice")

    assert first.headers["cache-status"] == "lab; fwd=uri-miss; detail=private"
    assert second.headers["cache-status"] == "lab; fwd=uri-miss; detail=private"
    assert cache.entries == {}
    assert len(origin.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cookie_authenticated_max_age_response_leaks_to_another_session() -> None:
    origin, cache = stack(MAX_AGE_ONLY)

    await get(cache, "/v1/me", Cookie="sid=sid-alice")
    leaked = await get(cache, "/v1/me", Cookie="sid=sid-mallory")

    assert leaked.headers["cache-status"] == "lab; hit"
    assert leaked.json() == {"tenant": "acme", "user": "alice"}
    assert origin.calls == [("/v1/me", "alice")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_session_middleware_vary_cookie_gives_each_session_its_own_entry() -> None:
    async def login(request: Request) -> JSONResponse:
        request.session["user"] = request.query_params["user"]
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    async def profile(request: Request) -> JSONResponse:
        return JSONResponse(
            {"user": request.session["user"]}, headers={"Cache-Control": "max-age=60"}
        )

    app = Starlette(
        routes=[Route("/login", login), Route("/profile", profile)],
        middleware=[Middleware(SessionMiddleware, secret_key=SIGNING_KEY)],
    )
    cache = SharedCache(app, clock=FakeClock())
    transport = httpx.ASGITransport(app=cache)
    seen: dict[str, httpx.Response] = {}
    for user in ("alice", "mallory"):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/login", params={"user": user})
            seen[user] = await client.get("/profile")

    assert seen["alice"].headers["vary"] == "Cookie"
    assert seen["mallory"].headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert seen["mallory"].json() == {"user": "mallory"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cached_set_cookie_is_replayed_to_the_next_requester() -> None:
    origin = Origin(PUBLIC, extra_headers={"Set-Cookie": "sid=sid-alice; HttpOnly"})
    cache = SharedCache(origin.app(), clock=FakeClock())

    await get(cache, "/v1/invoices", user="alice")
    replayed = await get(cache, "/v1/invoices", user="mallory")

    assert replayed.headers["cache-status"] == "lab; hit"
    assert replayed.headers["set-cookie"] == "sid=sid-alice; HttpOnly"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_verified_tenant_header_shares_within_a_tenant_and_isolates_tenants() -> None:
    origin, cache = stack(TENANT_SHARED)
    edge = TenantEdge(cache)

    alice = await get(edge, "/v1/invoices", user="alice")
    bob = await get(edge, "/v1/invoices", user="bob")
    mallory = await get(edge, "/v1/invoices", user="mallory")

    assert alice.headers["cache-status"] == "lab; fwd=uri-miss; stored"
    assert bob.headers["cache-status"] == "lab; hit"
    assert mallory.headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert mallory.json() == {"tenant": "globex", "invoices": ["INV-G1"]}
    assert origin.calls == [("/v1/invoices", "alice"), ("/v1/invoices", "mallory")]
    assert [e.selecting for e in cache.entries[("/v1/invoices", b"")]] == [("acme",), ("globex",)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_shared_policy_on_a_per_user_resource_leaks_within_the_tenant() -> None:
    _, cache = stack(TENANT_SHARED)
    edge = TenantEdge(cache)

    await get(edge, "/v1/me", user="alice")
    bob = await get(edge, "/v1/me", user="bob")

    assert bob.headers["cache-status"] == "lab; hit"
    assert bob.json() == {"tenant": "acme", "user": "alice"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_in_front_of_the_gateway_keys_on_an_absent_header_and_leaks() -> None:
    origin = Origin(TENANT_SHARED)
    cache = SharedCache(TenantEdge(origin.app()), clock=FakeClock())

    await get(cache, "/v1/invoices", user="alice")
    leaked = await get(cache, "/v1/invoices", user="mallory")

    assert leaked.headers["cache-status"] == "lab; hit"
    assert leaked.json()["tenant"] == "acme"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spoofed_tenant_header_is_replaced_by_the_edge_before_the_cache_sees_it() -> None:
    origin, cache = stack(TENANT_SHARED)
    edge = TenantEdge(cache)

    await get(edge, "/v1/invoices", user="alice")
    spoofed = await get(edge, "/v1/invoices", user="mallory", **{"X-Tenant-ID": "acme"})

    assert spoofed.headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert spoofed.json()["tenant"] == "globex"
    assert origin.calls[-1] == ("/v1/invoices", "mallory")
    assert [e.selecting for e in cache.entries[("/v1/invoices", b"")]] == [("acme",), ("globex",)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_surrogate_key_purge_evicts_only_that_tenants_entries() -> None:
    origin, cache = stack(TENANT_SHARED)
    edge = TenantEdge(cache)
    await get(edge, "/v1/invoices", user="alice")
    await get(edge, "/v1/invoices", user="mallory")

    evicted = cache.purge_tag("tenant-acme")
    bob = await get(edge, "/v1/invoices", user="bob")
    mallory = await get(edge, "/v1/invoices", user="mallory")

    assert evicted == 1
    assert bob.headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert mallory.headers["cache-status"] == "lab; hit"
    assert [user for _, user in origin.calls] == ["alice", "mallory", "bob"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cdn_cache_control_replaces_private_for_a_cache_that_targets_it() -> None:
    policy = CachePolicy("private, max-age=60")
    plain_origin = Origin(policy, extra_headers={"CDN-Cache-Control": "max-age=60"})
    plain = SharedCache(plain_origin.app(), clock=FakeClock())
    cdn_origin = Origin(policy, extra_headers={"CDN-Cache-Control": "max-age=60"})
    cdn = SharedCache(cdn_origin.app(), clock=FakeClock(), target_fields=("cdn-cache-control",))

    for cache in (plain, cdn):
        await get(cache, "/v1/me", Cookie="sid=sid-alice")
    plain_second = await get(plain, "/v1/me", Cookie="sid=sid-mallory")
    cdn_second = await get(cdn, "/v1/me", Cookie="sid=sid-mallory")

    assert plain_second.json()["user"] == "mallory"
    assert cdn_second.headers["cache-status"] == "lab; hit"
    assert cdn_second.json()["user"] == "alice"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vary_star_is_stored_but_never_matches() -> None:
    origin, cache = stack(CachePolicy("public, max-age=60", vary="*"))

    await get(cache, "/v1/invoices", user="alice")
    second = await get(cache, "/v1/invoices", user="alice")

    assert second.headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert len(origin.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_s_maxage_sets_the_shared_lifetime_and_age_counts_up() -> None:
    clock = FakeClock()
    origin = Origin(TENANT_SHARED)
    edge = TenantEdge(SharedCache(origin.app(), clock=clock))

    await get(edge, "/v1/invoices", user="alice")
    clock.now += 59
    fresh = await get(edge, "/v1/invoices", user="bob")
    clock.now += 1
    expired = await get(edge, "/v1/invoices", user="bob")

    assert (fresh.headers["cache-status"], fresh.headers["age"]) == ("lab; hit", "59")
    assert expired.headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert [user for _, user in origin.calls] == ["alice", "bob"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unauthenticated_401_and_unsafe_methods_are_never_stored() -> None:
    origin, cache = stack(PUBLIC)
    edge = TenantEdge(cache)

    anonymous = await get(edge, "/v1/invoices")
    no_session = await get(cache, "/v1/me")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="http://test"
    ) as client:
        post = await client.post("/v1/invoices", headers={"Authorization": "Bearer tok-alice"})

    assert (anonymous.status_code, no_session.status_code) == (401, 401)
    assert anonymous.headers["cache-status"] == "lab; fwd=uri-miss; detail=status"
    assert post.status_code == 405
    assert "cache-status" not in post.headers
    assert cache.entries == {}
    assert origin.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_expired_entry_is_replaced_rather_than_kept_beside_its_successor() -> None:
    clock = FakeClock()
    origin = Origin(TENANT_SHARED)
    cache = SharedCache(origin.app(), clock=clock)
    edge = TenantEdge(cache)

    await get(edge, "/v1/invoices", user="alice")
    clock.now += 60
    await get(edge, "/v1/invoices", user="bob")

    stored = cache.entries[("/v1/invoices", b"")]
    assert [(e.selecting, e.stored_at) for e in stored] == [(("acme",), 1_060.0)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_request_without_the_vary_field_does_not_match_an_entry_that_had_it() -> None:
    origin, cache = stack(TENANT_SHARED)

    await get(cache, "/v1/invoices", user="alice", **{"X-Tenant-ID": "acme"})
    without = await get(cache, "/v1/invoices", user="mallory")
    with_acme = await get(cache, "/v1/invoices", user="mallory", **{"X-Tenant-ID": "acme"})

    assert without.headers["cache-status"] == "lab; fwd=vary-miss; stored"
    assert without.json()["tenant"] == "globex"
    assert with_acme.headers["cache-status"] == "lab; hit"
    assert with_acme.json()["tenant"] == "acme"
    assert [user for _, user in origin.calls] == ["alice", "mallory"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_request_field_lines_are_combined_with_comma_space_before_matching() -> None:
    assert combined_headers([(b"X-Tenant-ID", b"acme"), (b"x-tenant-id", b" globex ")]) == {
        "x-tenant-id": "acme, globex"
    }
    _, cache = stack(TENANT_SHARED)

    await get(cache, "/v1/invoices", user="alice", **{"X-Tenant-ID": "acme"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cache), base_url="http://test"
    ) as client:
        await client.get(
            "/v1/invoices",
            headers=[
                ("Authorization", "Bearer tok-alice"),
                ("X-Tenant-ID", "a"),
                ("X-Tenant-ID", "b"),
            ],
        )

    assert [e.selecting for e in cache.entries[("/v1/invoices", b"")]] == [("acme",), ("a, b",)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argument", "seconds"),
    [
        ("0", 0),
        ("60", 60),
        ("2147483648", 2**31),
        ("99999999999999999999", 2**31),
        ("-1", None),
        ("1.5", None),
        ("", None),
        ("\u0661", None),
        (None, None),
    ],
    ids=[
        "zero",
        "plain",
        "at-cap",
        "overflow-uses-cap",
        "negative",
        "fraction",
        "empty",
        "non-ascii-digit",
        "bare-directive",
    ],
)
def test_delta_seconds_is_digits_only_and_overflow_uses_two_to_the_31(
    argument: str | None, seconds: int | None
) -> None:
    assert delta_seconds(argument) == seconds
