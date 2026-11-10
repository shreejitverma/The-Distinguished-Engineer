"""Web cache poisoning via unkeyed inputs and web cache deception, with each defense."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qsl

import httpx
import pytest
import uvicorn
from granian.utils.proxies import (
    wrap_asgi_with_proxy_headers,  # pyright: ignore[reportUnknownVariableType]
)
from hypercorn.middleware import ProxyFixMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_labs.f03_http_caching_and_conditional_requests.cache_poisoning import (
    EdgeCache,
    EdgePolicy,
    OriginConfig,
    StoredResponse,
    build_origin,
    content_type_matches,
    decide_storage,
    keyed_query,
    legacy_parse_qsl,
    static_extension,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

PRODUCT = "/api/products/1"
ALICE_COOKIE = {"cookie": "session=s-alice"}
BEARER = [(b"authorization", b"Bearer x")]
CSS_RULE = EdgePolicy(static_extensions=frozenset({".css", ".js"}))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def self_link(response: httpx.Response) -> str:
    link: str = response.json()["links"]["self"]
    return link


# --- Poisoning through an unkeyed header ----------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unkeyed_forwarded_host_poisons_the_self_link_for_the_next_client() -> None:
    clock = Clock()
    edge = EdgeCache(build_origin(OriginConfig(link_base="forwarded-host")), clock=clock)
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        clock.now = 120.0
        victim = await client.get(PRODUCT)

    assert attack.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert victim.headers["cache-status"] == "edge; hit"
    assert victim.headers["age"] == "120"
    assert self_link(victim) == "https://evil.example/api/products/1"
    assert edge.forwarded == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poisoned_entry_lives_exactly_its_freshness_lifetime() -> None:
    clock = Clock()
    edge = EdgeCache(build_origin(OriginConfig(link_base="forwarded-host")), clock=clock)
    async with client_for(edge) as client:
        await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        clock.now = 299.0
        last_hit = await client.get(PRODUCT)
        clock.now = 300.0  # max-age=300 from the origin
        refetched = await client.get(PRODUCT)

    assert last_hit.headers["cache-status"] == "edge; hit"
    assert self_link(last_hit) == "https://evil.example/api/products/1"
    assert refetched.headers["cache-status"] == "edge; fwd=stale; stored"
    assert self_link(refetched) == "https://api.example.com/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_method_is_written_through_and_never_stored() -> None:
    edge = EdgeCache(build_origin(OriginConfig()), EdgePolicy(heuristic_ttl=60.0))
    async with client_for(edge) as client:
        post = await client.post(PRODUCT, content=b"{}")
        get = await client.get(PRODUCT)

    assert post.status_code == 405
    assert post.headers["cache-status"] == "edge; fwd=method"
    assert (get.status_code, get.headers["cache-status"]) == (200, "edge; fwd=uri-miss; stored")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vary_on_forwarded_host_isolates_the_attackers_variant() -> None:
    origin = build_origin(OriginConfig(link_base="forwarded-host", vary_on_forwarded_host=True))
    edge = EdgeCache(origin)
    async with client_for(edge) as client:
        await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        victim = await client.get(PRODUCT)
        attacker_again = await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})

    assert victim.headers["cache-status"] == "edge; fwd=vary-miss; stored"
    assert self_link(victim) == "https://api.example.com/api/products/1"
    assert attacker_again.headers["cache-status"] == "edge; hit"
    assert self_link(attacker_again) == "https://evil.example/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_that_ignores_vary_serves_the_attackers_variant_to_everyone() -> None:
    origin = build_origin(OriginConfig(link_base="forwarded-host", vary_on_forwarded_host=True))
    edge = EdgeCache(origin, EdgePolicy(honor_vary=False))
    async with client_for(edge) as client:
        await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        victim = await client.get(PRODUCT)

    assert victim.headers["cache-status"] == "edge; hit"
    assert self_link(victim) == "https://evil.example/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forwarded_host_allowlist_falls_back_to_the_canonical_origin() -> None:
    config = OriginConfig(
        link_base="forwarded-host", allowed_forwarded_hosts=frozenset({"api.example.com"})
    )
    edge = EdgeCache(build_origin(config))
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        victim = await client.get(PRODUCT)
        partner = await client.get(
            "/api/products/2", headers={"x-forwarded-host": "API.example.com"}
        )

    assert self_link(attack) == "https://api.example.com/api/products/1"
    assert victim.headers["cache-status"] == "edge; hit"
    assert self_link(victim) == "https://api.example.com/api/products/1"
    assert self_link(partner) == "https://API.example.com/api/products/2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_that_overwrites_x_forwarded_host_removes_the_input() -> None:
    edge = EdgeCache(
        build_origin(OriginConfig(link_base="forwarded-host")),
        EdgePolicy(forwarded_host="overwrite"),
    )
    async with client_for(edge) as client:
        attack = await client.get(
            PRODUCT, headers=[("x-forwarded-host", "evil.example"), ("x-forwarded-host", "b")]
        )
        victim = await client.get(PRODUCT)

    assert self_link(attack) == "https://test/api/products/1"
    assert self_link(victim) == "https://test/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_proxy_fix_makes_url_for_reflect_x_forwarded_host() -> None:
    origin = build_origin(OriginConfig(link_base="request-host"))
    edge = EdgeCache(ProxyFixMiddleware(origin, mode="legacy", trusted_hops=1))  # type: ignore[arg-type]
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        victim = await client.get(PRODUCT)

    assert self_link(attack) == "http://evil.example/api/products/1"
    assert victim.headers["cache-status"] == "edge; hit"
    assert self_link(victim) == "http://evil.example/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hypercorn_proxy_fix_modern_mode_falls_back_to_x_forwarded_host() -> None:
    origin = build_origin(OriginConfig(link_base="request-host"))
    app = cast("ASGIApp", ProxyFixMiddleware(origin, mode="modern", trusted_hops=1))  # type: ignore[arg-type]
    async with client_for(app) as client:
        no_forwarded = await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        with_forwarded = await client.get(
            PRODUCT, headers={"x-forwarded-host": "evil.example", "forwarded": "host=edge.example"}
        )

    # Without a Forwarded field, modern mode reads the X-Forwarded-* fields like legacy mode.
    assert self_link(no_forwarded) == "http://evil.example/api/products/1"
    assert self_link(with_forwarded) == "http://edge.example/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["uvicorn", "granian"])
async def test_uvicorn_and_granian_proxy_helpers_never_rewrite_host(server: str) -> None:
    origin = build_origin(OriginConfig(link_base="request-host"))
    # ASGITransport reports client ("127.0.0.1", 123), which both helpers trust by default.
    if server == "uvicorn":
        wrapped = cast("ASGIApp", ProxyHeadersMiddleware(origin))  # type: ignore[arg-type]
    else:
        wrapped = cast("ASGIApp", wrap_asgi_with_proxy_headers(origin))
    async with client_for(wrapped) as client:
        response = await client.get(
            PRODUCT,
            headers={
                "x-forwarded-host": "evil.example",
                "x-forwarded-proto": "https",
                "x-forwarded-for": "203.0.113.7",  # granian applies the proto only with a For
            },
        )

    # Scheme comes from X-Forwarded-Proto, the authority still from Host.
    assert self_link(response) == "https://test/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forged_host_is_keyed_so_it_cannot_poison_another_host() -> None:
    edge = EdgeCache(build_origin(OriginConfig(link_base="request-host")))
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"host": "evil.example"})
        victim = await client.get(PRODUCT)

    assert self_link(attack) == "http://evil.example/api/products/1"
    assert victim.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert self_link(victim) == "http://test/api/products/1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_url_for_never_reflects_a_syntactically_invalid_host() -> None:
    async with client_for(build_origin(OriginConfig(link_base="request-host"))) as client:
        response = await client.get(PRODUCT, headers={"host": 'a"><script>'})

    # parse_host_header rejects the value, so URL() falls back to scope["server"].
    assert response.status_code == 200
    assert self_link(response).startswith("http://test")
    assert "<script>" not in response.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trusted_host_400_is_not_stored_even_with_heuristic_freshness() -> None:
    origin = build_origin(OriginConfig(link_base="request-host", trusted_hosts=("test",)))
    edge = EdgeCache(origin, EdgePolicy(heuristic_ttl=60.0))
    async with client_for(edge) as client:
        first = await client.get(PRODUCT, headers={"host": "evil.example"})
        second = await client.get(PRODUCT, headers={"host": "evil.example"})
        victim = await client.get(PRODUCT)

    assert (first.status_code, first.text) == (400, "Invalid host header")
    assert first.headers["cache-status"] == "edge; fwd=uri-miss"
    assert second.headers["cache-status"] == "edge; fwd=uri-miss"
    assert victim.status_code == 200


# --- Cache-poisoned denial of service -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_method_override_405_is_heuristically_cached_and_served_to_get_clients() -> None:
    edge = EdgeCache(
        build_origin(OriginConfig(honor_method_override=True)), EdgePolicy(heuristic_ttl=60.0)
    )
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"x-http-method-override": "DELETE"})
        victim = await client.get(PRODUCT)

    assert attack.status_code == 405
    assert "cache-control" not in attack.headers
    assert victim.status_code == 405
    assert victim.headers["cache-status"] == "edge; hit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_heuristic_freshness_the_override_405_is_not_stored() -> None:
    edge = EdgeCache(build_origin(OriginConfig(honor_method_override=True)))
    async with client_for(edge) as client:
        attack = await client.get(PRODUCT, headers={"x-http-method-override": "DELETE"})
        victim = await client.get(PRODUCT)

    assert attack.headers["cache-status"] == "edge; fwd=uri-miss"
    assert victim.status_code == 200


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_h11_rejects_an_unfinished_header_section_over_16_kib_with_400() -> None:
    config = uvicorn.Config(
        build_origin(OriginConfig()), http="h11", lifespan="off", log_level="critical"
    )
    server = uvicorn.Server(config)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()  # the kernel queues connections until uvicorn starts accepting
    host, port = sock.getsockname()
    task = asyncio.create_task(server.serve(sockets=[sock]))

    async def exchange(request: bytes) -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(request)
        await writer.drain()
        raw = await reader.read()  # uvicorn closes after the response
        writer.close()
        await writer.wait_closed()
        return raw

    head = b"GET /api/products/1 HTTP/1.1\r\nHost: test\r\nConnection: close\r\n"
    try:
        async with asyncio.timeout(10):
            small = await exchange(head + b"X-Pad: " + b"a" * 1_000 + b"\r\n\r\n")
            # No blank line: the header section never completes, so h11 keeps buffering
            # until the buffer exceeds DEFAULT_MAX_INCOMPLETE_EVENT_SIZE (16 KiB).
            unfinished = await exchange(head + b"X-Pad: " + b"a" * 20_000 + b"\r\n")
    finally:
        server.should_exit = True
        await task

    assert small.startswith(b"HTTP/1.1 200 OK\r\n")
    response_head = unfinished.split(b"\r\n\r\n", 1)[0].lower()
    # h11 raises with error_status_hint=431; uvicorn's h11 protocol still sends 400.
    assert response_head.startswith(b"http/1.1 400 bad request\r\n")
    assert b"cache-control" not in response_head


# --- Cache parameter cloaking ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_semicolon_splitting_origin_lets_a_cloaked_parameter_poison_the_entry() -> None:
    edge = EdgeCache(
        build_origin(OriginConfig(legacy_semicolon_query=True)),
        EdgePolicy(unkeyed_query_params=frozenset({"utm_content"})),
    )
    async with client_for(edge) as client:
        attack = await client.get("/api/prices?currency=USD&utm_content=x;currency=EUR")
        victim = await client.get("/api/prices?currency=USD")

    assert attack.json()["currency"] == "EUR"
    assert victim.headers["cache-status"] == "edge; hit"
    assert victim.json() == {"currency": "EUR", "price": "9.20"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_query_parsing_does_not_split_on_semicolon() -> None:
    edge = EdgeCache(
        build_origin(OriginConfig()), EdgePolicy(unkeyed_query_params=frozenset({"utm_content"}))
    )
    async with client_for(edge) as client:
        attack = await client.get("/api/prices?currency=USD&utm_content=x;currency=EUR")
        victim = await client.get("/api/prices?currency=USD")

    assert parse_qsl("a=1;b=2") == [("a", "1;b=2")]
    assert legacy_parse_qsl("a=1;b=2") == [("a", "1"), ("b", "2")]
    assert attack.json()["currency"] == "USD"
    assert victim.json() == {"currency": "USD", "price": "10.00"}


@pytest.mark.unit
def test_keyed_query_drops_the_unkeyed_parameter_and_whatever_hides_in_its_value() -> None:
    unkeyed = frozenset({"utm_content"})
    assert keyed_query(b"currency=USD&utm_content=x;currency=EUR", unkeyed) == "currency=USD"
    assert keyed_query(b"currency=USD&utm%5Fcontent=x", unkeyed) == "currency=USD"
    assert keyed_query(b"currency=USD&utm_contents=x", unkeyed) == "currency=USD&utm_contents=x"


# --- Web cache deception --------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_rule_overriding_origin_caches_a_private_profile() -> None:
    policy = EdgePolicy(
        static_extensions=CSS_RULE.static_extensions, extension_rule_overrides_origin=True
    )
    edge = EdgeCache(build_origin(OriginConfig(lenient_suffix_route=True)), policy)
    async with client_for(edge) as client:
        victim = await client.get("/api/me/avatar.css", headers=ALICE_COOKIE)
        attacker = await client.get("/api/me/avatar.css")

    assert victim.headers["cache-control"] == "private, no-store"
    assert victim.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert attacker.headers["cache-status"] == "edge; hit"
    assert attacker.json()["api_key"] == "ak_test_alice"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extension_rule_that_honors_origin_cache_control_stores_nothing() -> None:
    edge = EdgeCache(build_origin(OriginConfig(lenient_suffix_route=True)), CSS_RULE)
    async with client_for(edge) as client:
        victim = await client.get("/api/me/avatar.css", headers=ALICE_COOKIE)
        attacker = await client.get("/api/me/avatar.css")

    assert victim.headers["cache-status"] == "edge; fwd=uri-miss"
    assert attacker.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_type_armor_refuses_json_under_a_css_extension() -> None:
    policy = EdgePolicy(
        static_extensions=CSS_RULE.static_extensions,
        extension_rule_overrides_origin=True,
        verify_content_type=True,
    )
    edge = EdgeCache(build_origin(OriginConfig(lenient_suffix_route=True)), policy)
    async with client_for(edge) as client:
        victim = await client.get("/api/me/avatar.css", headers=ALICE_COOKIE)
        attacker = await client.get("/api/me/avatar.css")

    assert victim.headers["content-type"] == "application/json"
    assert victim.headers["cache-status"] == "edge; fwd=uri-miss"
    assert attacker.status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_origin_routing_turns_the_deception_url_into_a_cached_404() -> None:
    policy = EdgePolicy(
        static_extensions=CSS_RULE.static_extensions, extension_rule_overrides_origin=True
    )
    edge = EdgeCache(build_origin(OriginConfig()), policy)
    async with client_for(edge) as client:
        victim = await client.get("/api/me/avatar.css", headers=ALICE_COOKIE)
        attacker = await client.get("/api/me/avatar.css")

    assert victim.status_code == 404
    assert attacker.headers["cache-status"] == "edge; hit"
    assert attacker.status_code == 404
    assert "alice" not in attacker.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rfc9111_protects_authorization_but_not_cookie_authentication() -> None:
    edge = EdgeCache(build_origin(OriginConfig(me_cache_control="max-age=60")))
    async with client_for(edge) as client:
        bearer = await client.get("/api/me", headers={"authorization": "Bearer t-alice"})
        after_bearer = await client.get("/api/me")
        cookie = await client.get("/api/me", headers=ALICE_COOKIE)
        after_cookie = await client.get("/api/me")

    assert bearer.headers["cache-status"] == "edge; fwd=uri-miss"
    assert after_bearer.status_code == 401
    assert cookie.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert after_cookie.headers["cache-status"] == "edge; hit"
    assert after_cookie.json()["user"] == "alice"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_route_regex_accepts_a_trailing_encoded_newline() -> None:
    async with client_for(build_origin(OriginConfig())) as client:
        newline = await client.get("/api/me%0A", headers=ALICE_COOKIE)
        newline_then_suffix = await client.get("/api/me%0Aa.css", headers=ALICE_COOKIE)

    # compile_path ends the pattern with "$", which also matches before a final "\n".
    assert newline.status_code == 200
    assert newline.json()["user"] == "alice"
    assert newline_then_suffix.status_code == 404


# --- The storage decision table -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("request_headers", "status", "cache_control", "policy", "expected"),
    [
        ([], 200, "public, max-age=60, s-maxage=600", EdgePolicy(), (600.0, "s-maxage")),
        ([], 200, "max-age=60", EdgePolicy(), (60.0, "max-age")),
        ([], 200, "no-store, max-age=60", EdgePolicy(), (None, "no-store")),
        ([], 200, "private, max-age=60", EdgePolicy(), (None, "private")),
        (BEARER, 200, "max-age=60", EdgePolicy(), (None, "authorization")),
        (BEARER, 200, "public, max-age=60", EdgePolicy(), (60.0, "max-age")),
        ([], 400, None, EdgePolicy(heuristic_ttl=60.0), (None, "no explicit freshness")),
        ([], 404, None, EdgePolicy(heuristic_ttl=60.0), (60.0, "heuristic")),
        ([], 200, None, EdgePolicy(), (None, "no explicit freshness")),
        ([], 200, "no-cache, max-age=60", EdgePolicy(), (None, "no-cache")),
        ([], 400, "public", EdgePolicy(heuristic_ttl=60.0), (60.0, "heuristic")),
        ([], 200, "max-age=60, max-age=600", EdgePolicy(), (60.0, "max-age")),
        ([], 200, 'max-age="60"', EdgePolicy(), (60.0, "max-age")),
        ([], 200, "max-age=abc", EdgePolicy(), (None, "no explicit freshness")),
        ([], 200, "max-age=\xb2", EdgePolicy(), (None, "no explicit freshness")),
    ],
    ids=[
        "s-maxage-wins",
        "max-age",
        "no-store",
        "private",
        "authorization",
        "authorization-public",
        "400-never-heuristic",
        "404-heuristic",
        "no-freshness",
        "no-cache-not-stored",
        "public-makes-400-heuristic",
        "first-duplicate-directive-wins",
        "quoted-argument",
        "non-numeric-max-age-is-not-freshness",
        "non-ascii-digit-max-age-is-not-freshness",
    ],
)
def test_decide_storage_follows_rfc9111_section_3_for_a_shared_cache(
    request_headers: list[tuple[bytes, bytes]],
    status: int,
    cache_control: str | None,
    policy: EdgePolicy,
    expected: tuple[float | None, str],
) -> None:
    response_headers = (
        [] if cache_control is None else [(b"cache-control", cache_control.encode("latin-1"))]
    )
    decision = decide_storage(
        policy,
        method="GET",
        raw_path="/api/products/1",
        request_headers=request_headers,
        status=status,
        response_headers=response_headers,
    )
    assert (decision.ttl, decision.reason) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extension", "content_type", "expected"),
    [
        (".css", "text/css; charset=utf-8", True),
        (".css", "application/json", False),
        (".css", None, False),
        (".png", "application/octet-stream", True),
        (".js", "TEXT/JavaScript", True),
    ],
    ids=["css", "json-under-css", "missing", "octet-stream-exception", "case-insensitive"],
)
def test_content_type_armor_table(extension: str, content_type: str | None, expected: bool) -> None:
    assert content_type_matches(extension, content_type) is expected


@pytest.mark.unit
def test_extension_rule_supplies_freshness_only_when_the_origin_gives_none() -> None:
    def decide(cache_control: bytes | None) -> tuple[float | None, str]:
        headers = [] if cache_control is None else [(b"cache-control", cache_control)]
        decision = decide_storage(
            CSS_RULE,
            method="GET",
            raw_path="/assets/site.css",
            request_headers=[],
            status=200,
            response_headers=headers,
        )
        return decision.ttl, decision.reason

    assert decide(None) == (3600.0, "extension rule .css")
    assert decide(b"max-age=60") == (60.0, "max-age")
    assert decide(b"no-store") == (None, "no-store")


@pytest.mark.unit
def test_lookup_prefers_the_most_recent_matching_variant() -> None:
    def stored(
        body: bytes, at: float, vary: tuple[str, ...], selected: tuple[str | None, ...]
    ) -> StoredResponse:
        return StoredResponse(200, (), body, at, 300.0, vary, selected)

    edge = EdgeCache(build_origin(OriginConfig()), clock=lambda: 10.0)
    key = ("GET", "test", PRODUCT, "")
    edge.store[key] = [
        stored(b"old, no Vary", 0.0, (), ()),
        stored(b"new, keyed", 5.0, ("x-forwarded-host",), ("evil.example",)),
    ]
    request = [(b"x-forwarded-host", b" evil.example ")]

    newest, outcome = edge.lookup(key, request)
    fallback, _ = edge.lookup(key, [])
    assert newest is not None
    assert (newest.body, outcome) == (b"new, keyed", "hit")
    assert fallback is not None
    assert fallback.body == b"old, no Vary"


@pytest.mark.unit
def test_vary_star_never_matches_a_stored_response() -> None:
    # RFC 9111 Section 4.1: a stored Vary containing "*" always fails to match.
    edge = EdgeCache(build_origin(OriginConfig()))
    key = ("GET", "test", PRODUCT, "")
    edge.store[key] = [StoredResponse(200, (), b"star", 0.0, 300.0, ("*",), (None,))]

    stored, outcome = edge.lookup(key, [])
    assert (stored, outcome) == (None, "vary-miss")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        ("/assets/site.css", ".css"),
        ("/assets/SITE.CSS", ".css"),
        ("/api/me/avatar.css", ".css"),
        ("/assets/.css", None),
        ("/assets/site", None),
        ("/assets.css/site", None),
        ("/assets/site.png", None),
    ],
    ids=[
        "css",
        "upper-case",
        "deception-url",
        "bare-dotfile",
        "none",
        "dot-in-directory",
        "unlisted",
    ],
)
def test_static_extension_reads_only_the_last_path_segment(
    raw_path: str, expected: str | None
) -> None:
    assert static_extension(raw_path, frozenset({".css", ".js"})) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_host_is_compared_case_insensitively_in_the_cache_key() -> None:
    edge = EdgeCache(build_origin(OriginConfig()))
    async with client_for(edge) as client:
        first = await client.get(PRODUCT)
        second = await client.get(PRODUCT, headers={"host": "TEST"})

    assert first.headers["cache-status"] == "edge; fwd=uri-miss; stored"
    assert second.headers["cache-status"] == "edge; hit"
    assert edge.forwarded == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_refreshing_a_variant_replaces_it_instead_of_accumulating_entries() -> None:
    clock = Clock()
    origin = build_origin(OriginConfig(link_base="forwarded-host", vary_on_forwarded_host=True))
    edge = EdgeCache(origin, clock=clock)
    evil = {"x-forwarded-host": "evil.example"}
    async with client_for(edge) as client:
        await client.get(PRODUCT, headers=evil)
        await client.get(PRODUCT)
        clock.now = 400.0  # both variants are stale now
        refreshed = await client.get(PRODUCT, headers=evil)

    assert refreshed.headers["cache-status"] == "edge; fwd=stale; stored"
    assert len(edge.store[("GET", "test", PRODUCT, "")]) == 2


def with_vary_spelled(app: ASGIApp, spelling: bytes) -> ASGIApp:
    """Rewrite the origin's `Vary` value, since field values may use any letter case."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        async def rewriting_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, spelling if k == b"vary" else v) for k, v in message["headers"]
                ]
            await send(message)

        await app(scope, receive, rewriting_send)

    return wrapped


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vary_field_names_match_case_insensitively() -> None:
    origin = build_origin(OriginConfig(link_base="forwarded-host", vary_on_forwarded_host=True))
    edge = EdgeCache(with_vary_spelled(origin, b"X-Forwarded-Host"))
    async with client_for(edge) as client:
        await client.get(PRODUCT, headers={"x-forwarded-host": "evil.example"})
        victim = await client.get(PRODUCT)

    assert victim.headers["cache-status"] == "edge; fwd=vary-miss; stored"
    assert self_link(victim) == "https://api.example.com/api/products/1"
