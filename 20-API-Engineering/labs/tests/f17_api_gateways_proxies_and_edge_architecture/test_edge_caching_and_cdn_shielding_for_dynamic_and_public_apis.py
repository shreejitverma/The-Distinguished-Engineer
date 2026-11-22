"""Edge caching lab: s-maxage policy, collapse-forwarding, shielding, purge, and signed URLs."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from api_labs.f17_api_gateways_proxies_and_edge_architecture.edge_cache import (
    BrowserView,
    Origin,
    SignedUrlVerifier,
    browser_freshness,
    build_topology,
    parse_surrogate_keys,
    select_policy,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def settle(rounds: int = 60) -> None:
    """Let every runnable task reach its next real suspension point."""
    for _ in range(rounds):
        await asyncio.sleep(0)


def version_of(response: httpx.Response) -> int:
    value = response.json()["version"]
    assert isinstance(value, int)
    return value


def cache_status(response: httpx.Response) -> str:
    return response.headers["cache-status"]


# ---- policy: RFC 9111 s-maxage and RFC 9213 targeted fields ----------------------------------


@pytest.mark.unit
def test_s_maxage_overrides_max_age_for_shared_cache_but_not_browsers() -> None:
    headers = {"Cache-Control": "max-age=0, s-maxage=60"}
    policy = select_policy(headers)
    assert (policy.storable, policy.ttl, policy.source) == (True, 60, "Cache-Control")
    assert browser_freshness(headers["Cache-Control"]) == BrowserView(fresh_for=0, stale_window=0)


@pytest.mark.unit
def test_cdn_cache_control_replaces_cache_control_entirely() -> None:
    both = {"Cache-Control": "max-age=0, s-maxage=60", "CDN-Cache-Control": "max-age=600"}
    assert select_policy(both).ttl == 600
    assert select_policy(both).source == "CDN-Cache-Control"
    # A targeted field with no freshness hides s-maxage from Cache-Control (RFC 9213 Section 2.2).
    swr_only = {"Cache-Control": "s-maxage=60", "CDN-Cache-Control": "stale-while-revalidate=300"}
    assert select_policy(swr_only).storable is False
    # RFC 9213 Section 3.1: CDN-Cache-Control caches while Cache-Control: no-store stops the rest.
    split = {"Cache-Control": "no-store", "CDN-Cache-Control": "max-age=600"}
    assert (select_policy(split).storable, select_policy(split).ttl) == (True, 600)


@pytest.mark.unit
def test_invalid_targeted_field_falls_back_but_invalid_cache_control_is_stale() -> None:
    bad_targeted = {"Cache-Control": "s-maxage=60", "CDN-Cache-Control": "max-age=abc"}
    assert select_policy(bad_targeted).ttl == 60
    assert select_policy(bad_targeted).source == "Cache-Control"
    bad_plain = select_policy({"Cache-Control": "max-age=abc"})
    assert (bad_plain.storable, bad_plain.ttl) == (True, 0)  # RFC 9111 Section 4.2.1


@pytest.mark.unit
def test_surrogate_control_outranks_cdn_cache_control_in_target_order() -> None:
    headers = {"Surrogate-Control": "max-age=5", "CDN-Cache-Control": "max-age=600"}
    assert select_policy(headers).ttl == 5


@pytest.mark.unit
def test_private_and_no_store_are_never_stored_by_a_shared_cache() -> None:
    assert select_policy({"Cache-Control": "private, s-maxage=60"}).storable is False
    assert select_policy({"Cache-Control": "no-store, max-age=60"}).storable is False
    assert select_policy({"Cache-Control": "max-age=60"}).storable is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cache_control", "reusable_with_authorization"),
    [
        ("max-age=60", False),
        ("public, max-age=60", True),
        ("s-maxage=60", True),
        ("max-age=60, must-revalidate", True),
    ],
)
def test_authorization_requests_need_an_explicit_shared_cache_opt_in(
    cache_control: str, reusable_with_authorization: bool
) -> None:
    assert select_policy({"Cache-Control": cache_control}).auth_ok is reusable_with_authorization


@pytest.mark.unit
def test_swr_inside_cache_control_reaches_browsers_but_inside_cdn_field_does_not() -> None:
    leaky = "max-age=0, s-maxage=60, stale-while-revalidate=300"
    assert browser_freshness(leaky) == BrowserView(fresh_for=0, stale_window=300)
    assert browser_freshness("max-age=0") == BrowserView(fresh_for=0, stale_window=0)
    targeted = {
        "Cache-Control": "max-age=0",
        "CDN-Cache-Control": "max-age=60, stale-while-revalidate=300",
    }
    assert select_policy(targeted).swr == 300


@pytest.mark.unit
def test_surrogate_key_parser_drops_the_oversize_token_and_everything_after_it() -> None:
    assert parse_surrogate_keys("a b c") == {"a", "b", "c"}
    assert parse_surrogate_keys("a xxxxx c", max_token=4) == {"a"}
    assert parse_surrogate_keys("aaaa bbbb cccc", max_header=9) == {"aaaa", "bbbb"}
    assert parse_surrogate_keys("a,b  c") == {"a,b", "c"}  # Fastly separates on spaces only


# ---- signed URLs ------------------------------------------------------------------------------


def split_signed(url: str) -> tuple[str, list[tuple[str, str]]]:
    parts = urlsplit(url)
    return parts.path, parse_qsl(parts.query)


@pytest.mark.unit
def test_signature_binds_path_query_expiry_and_key_id() -> None:
    clock = FakeClock(1000)
    verifier = SignedUrlVerifier({"k1": b"secret-1", "k2": b"secret-2"}, clock)
    path, pairs = split_signed(verifier.sign("/products/1", [("color", "red")], exp=1060, kid="k1"))
    assert verifier.check(path, pairs) is None
    assert verifier.check("/products/2", pairs) == "bad-signature"
    assert verifier.check(path, [*pairs, ("x", "1")]) == "bad-signature"
    assert (
        verifier.check(path, [(k, "k2" if k == "kid" else v) for k, v in pairs]) == "bad-signature"
    )
    assert verifier.check(path, [(k, "k9" if k == "kid" else v) for k, v in pairs]) == "unknown-kid"
    assert verifier.check(path, [*pairs, ("sig", "00")]) == "duplicate-parameter"
    assert verifier.check(path, [("color", "red")]) == "missing"
    clock.now = 1060
    assert verifier.check(path, pairs) == "expired"


# ---- one tier: hits, Age, Cache-Status, conditional requests ----------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hit_serves_without_origin_and_reports_age_ttl_and_status() -> None:
    clock, origin = FakeClock(), Origin()
    topo = build_topology(origin, clock, pops=1, shield=False)
    client = topo.client("edge-0")
    first = await client.get("/products/1")
    clock.advance(10)
    second = await client.get("/products/1", headers={"Cache-Control": "no-cache"})
    assert cache_status(first) == "edge-0; fwd=uri-miss; stored"
    assert first.headers["age"] == "0"
    assert cache_status(second) == "edge-0; hit; ttl=50"
    assert second.headers["age"] == "10"
    assert second.json() == first.json()
    assert len(origin.requests) == 1  # the viewer's request Cache-Control never reaches the origin
    assert "surrogate-key" not in second.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_answers_a_browser_revalidation_with_304_and_no_origin_trip() -> None:
    clock, origin = FakeClock(), Origin()
    client = build_topology(origin, clock, pops=1, shield=False).client("edge-0")
    await client.get("/products/1")
    same = await client.get("/products/1", headers={"If-None-Match": 'W/"v1"'})
    other = await client.get("/products/1", headers={"If-None-Match": '"v0"'})
    assert (same.status_code, same.content) == (304, b"")
    assert same.headers["etag"] == '"v1"'
    assert other.status_code == 200
    assert len(origin.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_method_invalidates_the_target_uri() -> None:
    clock, origin = FakeClock(), Origin()
    client = build_topology(origin, clock, pops=1, shield=False).client("edge-0")
    assert version_of(await client.get("/products/1")) == 1
    assert (await client.post("/products/1")).status_code == 200
    after = await client.get("/products/1")
    assert version_of(after) == 2
    assert "fwd=uri-miss" in cache_status(after)


# ---- collapse-forwarding ----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("collapse", [True, False])
async def test_collapse_forwarding_sends_one_origin_request_for_many_misses(collapse: bool) -> None:
    clients, origin = 20, Origin(gated=True)
    topo = build_topology(origin, FakeClock(), pops=1, shield=False, collapse=collapse)
    edge = topo.client("edge-0")
    tasks = [asyncio.create_task(edge.get("/products/1")) for _ in range(clients)]
    await settle()
    expected = 1 if collapse else clients
    assert len(origin.pending) == expected
    origin.release()
    responses = await asyncio.gather(*tasks)
    assert {r.status_code for r in responses} == {200}
    assert {version_of(r) for r in responses} == {1}
    assert len(origin.requests) == expected
    collapsed = [r for r in responses if "collapsed" in cache_status(r)]
    assert len(collapsed) == (clients - 1 if collapse else 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uncacheable_response_serializes_collapsed_requests_without_hit_for_pass() -> None:
    origin = Origin(gated=True)
    topo = build_topology(origin, FakeClock(), pops=1, shield=False, hit_for_pass_ttl=0)
    edge = topo.client("edge-0")
    tasks = [asyncio.create_task(edge.get("/me")) for _ in range(5)]
    await settle()
    assert len(origin.pending) == 1
    for round_number in range(2, 6):
        origin.release()
        await settle()
        assert len(origin.requests) == round_number  # one origin request per round, in series
        assert origin.max_in_flight == 1
    origin.release()
    await asyncio.gather(*tasks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hit_for_pass_releases_the_queue_to_run_concurrently() -> None:
    origin = Origin(gated=True)
    topo = build_topology(origin, FakeClock(), pops=1, shield=False, hit_for_pass_ttl=120)
    edge = topo.client("edge-0")
    tasks = [asyncio.create_task(edge.get("/me")) for _ in range(5)]
    await settle()
    assert len(origin.pending) == 1
    origin.release()
    await settle()
    assert len(origin.pending) == 4  # the four followers are released together
    origin.release()
    responses = await asyncio.gather(*tasks)
    assert origin.max_in_flight == 4
    assert sorted(r.json()["session"] for r in responses) == [1, 2, 3, 4, 5]
    assert len({r.headers["set-cookie"] for r in responses}) == 5  # no cookie shared between users


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_set_cookie_response_is_never_stored_even_with_a_cdn_ttl() -> None:
    clock, origin = FakeClock(), Origin()
    origin.product_headers["Set-Cookie"] = "sid=abc; Path=/"
    client = build_topology(origin, clock, pops=1, shield=False).client("edge-0")
    await client.get("/products/1")
    await client.get("/products/1")
    assert len(origin.requests) == 2


# ---- stale-while-revalidate and stale-if-error ------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_swr_serves_stale_to_every_waiter_and_revalidates_once_in_background() -> None:
    clock, origin = FakeClock(), Origin()
    topo = build_topology(origin, clock, pops=1, shield=False)
    edge, client = topo.pops["edge-0"], topo.client("edge-0")
    await client.get("/products/1")
    clock.advance(61)
    origin.versions["1"] = 2
    responses = await asyncio.gather(*(client.get("/products/1") for _ in range(10)))
    assert {version_of(r) for r in responses} == {1}
    assert {cache_status(r) for r in responses} == {"edge-0; hit; ttl=-1"}
    assert {r.headers["age"] for r in responses} == {"61"}
    await edge.drain()
    assert len(origin.requests) == 2
    assert origin.requests[1].if_none_match == '"v1"'
    fresh = await client.get("/products/1")
    assert (version_of(fresh), cache_status(fresh)) == (2, "edge-0; hit; ttl=60")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_swr_revalidation_that_returns_304_extends_freshness_without_a_body() -> None:
    clock, origin = FakeClock(), Origin()
    topo = build_topology(origin, clock, pops=1, shield=False)
    edge, client = topo.pops["edge-0"], topo.client("edge-0")
    await client.get("/products/1")
    clock.advance(61)
    stale = await client.get("/products/1")
    await edge.drain()
    after = await client.get("/products/1")
    assert (version_of(stale), version_of(after)) == (1, 1)
    assert cache_status(after) == "edge-0; hit; ttl=60"
    assert origin.requests[-1].if_none_match == '"v1"'
    assert edge.stats["revalidate"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_past_the_swr_window_the_request_blocks_on_the_origin() -> None:
    clock, origin = FakeClock(), Origin()
    client = build_topology(origin, clock, pops=1, shield=False).client("edge-0")
    await client.get("/products/1")
    clock.advance(60 + 300 + 1)
    origin.versions["1"] = 2
    late = await client.get("/products/1")
    assert version_of(late) == 2
    assert "fwd=stale" in cache_status(late)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_if_error_masks_a_5xx_until_its_window_ends() -> None:
    clock, origin = FakeClock(), Origin()
    client = build_topology(origin, clock, pops=1, shield=False).client("edge-0")
    await client.get("/products/1")
    clock.advance(400)  # past ttl 60 plus swr 300, inside stale-if-error 3600
    origin.fail_with = 503
    masked = await client.get("/products/1")
    assert (masked.status_code, version_of(masked)) == (200, 1)
    # RFC 9211 Section 2.1: hit and fwd are exclusive; this request went forward.
    assert cache_status(masked) == "edge-0; fwd=stale; fwd-status=503"
    clock.advance(3600)
    assert (await client.get("/products/1")).status_code == 503


# ---- purge: hard, soft, by tag, and across tiers ----------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_surrogate_key_purge_evicts_every_tagged_url_and_only_those() -> None:
    clock, origin = FakeClock(), Origin()
    topo = build_topology(origin, clock, pops=1, shield=False)
    edge, client = topo.pops["edge-0"], topo.client("edge-0")
    for pid in ("1", "2", "3"):
        await client.get(f"/products/{pid}")
    origin.versions.update({"1": 2, "2": 2, "3": 2})
    assert edge.purge_tag("product-1") == 1
    assert edge.purge_tag("no-such-tag") == 0
    assert version_of(await client.get("/products/1")) == 2
    assert version_of(await client.get("/products/2")) == 1  # still the old cached copy
    assert edge.purge_tag("catalog") == 3  # product 1 was refilled and carries the tag again
    assert version_of(await client.get("/products/2")) == 2
    assert version_of(await client.get("/products/3")) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_soft_purge_serves_stale_once_then_refreshes_while_hard_purge_blocks() -> None:
    clock, origin = FakeClock(), Origin()
    topo = build_topology(origin, clock, pops=1, shield=False)
    edge, client = topo.pops["edge-0"], topo.client("edge-0")
    await client.get("/products/1")
    await client.get("/products/2")
    origin.versions.update({"1": 2, "2": 2})
    assert edge.purge_tag("product-1", soft=True) == 1
    soft = await client.get("/products/1")
    assert (version_of(soft), cache_status(soft)) == (1, "edge-0; hit; ttl=0")
    await edge.drain()
    assert version_of(await client.get("/products/1")) == 2
    assert edge.purge_tag("product-2") == 1
    hard = await client.get("/products/2")
    assert (version_of(hard), "fwd=uri-miss" in cache_status(hard)) == (2, True)


# ---- shielding --------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("shield", "expected_origin_requests"), [(True, 3), (False, 12)])
async def test_shield_turns_pops_times_keys_origin_requests_into_keys(
    shield: bool, expected_origin_requests: int
) -> None:
    origin = Origin()
    topo = build_topology(origin, FakeClock(), pops=4, shield=shield)
    for pop in topo.pops:
        client = topo.client(pop)
        for pid in ("1", "2", "3"):
            assert (await client.get(f"/products/{pid}")).status_code == 200
    assert len(origin.requests) == expected_origin_requests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_status_chain_lists_the_tier_nearest_the_origin_first() -> None:
    topo = build_topology(Origin(), FakeClock(), pops=2, shield=True)
    cold = await topo.client("edge-0").get("/products/1")
    warm_shield = await topo.client("edge-1").get("/products/1")
    assert cache_status(cold) == "shield; fwd=uri-miss; stored, edge-0; fwd=uri-miss; stored"
    assert cache_status(warm_shield) == "shield; hit; ttl=60, edge-1; fwd=uri-miss; stored"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_surrogate_headers_survive_between_tiers_and_are_stripped_at_the_viewer_edge() -> (
    None
):
    topo = build_topology(Origin(), FakeClock(), pops=1, shield=True)
    assert topo.shield is not None
    direct = httpx.AsyncClient(transport=httpx.ASGITransport(app=topo.shield), base_url="http://s")
    inner = await direct.get("/products/1")
    viewer = await topo.client("edge-0").get("/products/1")
    assert inner.headers["surrogate-key"] == "product-1 catalog"
    assert "surrogate-key" not in viewer.headers
    assert topo.pops["edge-0"].purge_tag("catalog") == 1  # the edge indexed tags from the shield


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("use_upstream_age", "expected_version"), [(True, 2), (False, 1)])
async def test_age_from_the_shield_stops_the_edge_from_doubling_the_ttl(
    use_upstream_age: bool, expected_version: int
) -> None:
    clock, origin = FakeClock(), Origin()
    origin.product_headers["CDN-Cache-Control"] = "max-age=60"
    topo = build_topology(origin, clock, pops=1, shield=True, use_upstream_age=use_upstream_age)
    assert topo.shield is not None
    direct = httpx.AsyncClient(transport=httpx.ASGITransport(app=topo.shield), base_url="http://s")
    await direct.get("/products/1")  # shield stores at t=0
    clock.advance(50)
    edge_client = topo.client("edge-0")
    await edge_client.get("/products/1")  # edge copies a 50 second old object
    origin.versions["1"] = 2
    clock.advance(11)  # object is 61 seconds old by the origin's clock
    assert version_of(await edge_client.get("/products/1")) == expected_version


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purging_the_shield_first_prevents_edges_from_refilling_stale_copies() -> None:
    results: dict[str, int] = {}
    for order in ("upstream-first", "edge-first"):
        clock, origin = FakeClock(), Origin()
        topo = build_topology(origin, clock, pops=2, shield=True)
        edge0, edge1 = topo.client("edge-0"), topo.client("edge-1")
        await edge0.get("/products/1")
        await edge1.get("/products/1")
        origin.versions["1"] = 2

        async def refill(client: httpx.AsyncClient = edge0) -> None:
            await client.get("/products/1")

        await topo.purge_tag("catalog", order=order, between=refill)
        results[order] = version_of(await edge0.get("/products/1"))
    assert results == {"upstream-first": 2, "edge-first": 1}


# ---- signed URLs at the edge ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signature_is_verified_before_the_cache_lookup_and_excluded_from_the_key() -> None:
    clock, origin = FakeClock(1000), Origin()
    verifier = SignedUrlVerifier({"k1": b"secret-1"}, clock)
    client = build_topology(origin, clock, pops=1, shield=False, verifier=verifier).client("edge-0")
    url_a = verifier.sign("/products/1", [], exp=1060, kid="k1")
    url_b = verifier.sign("/products/1", [], exp=1090, kid="k1")
    short = verifier.sign("/products/1", [], exp=1020, kid="k1")
    assert url_a != url_b
    assert (await client.get(url_a)).status_code == 200
    assert (await client.get(url_b)).status_code == 200
    assert len(origin.requests) == 1  # two signatures, one cache entry
    assert origin.requests[0].target == "/products/1"  # the origin never sees the signature
    clock.advance(30)
    expired = await client.get(short)  # the object is cached and fresh, the URL is not
    assert (expired.status_code, expired.text) == (403, "edge-0: expired")
    assert (await client.get("/products/1")).status_code == 403
    tampered = await client.get(url_a.replace("/products/1", "/products/2"))
    assert (tampered.status_code, tampered.text) == (403, "edge-0: bad-signature")
    assert len(origin.requests) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checking_the_signature_after_the_cache_lookup_leaks_cached_objects() -> None:
    clock, origin = FakeClock(1000), Origin()
    verifier = SignedUrlVerifier({"k1": b"secret-1"}, clock)
    topo = build_topology(
        origin, clock, pops=1, shield=False, verifier=verifier, verify_first=False
    )
    client = topo.client("edge-0")
    assert (
        await client.get(verifier.sign("/products/1", [], exp=1060, kid="k1"))
    ).status_code == 200
    assert (await client.get("/products/1")).status_code == 200  # unsigned, served from cache


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keying_the_cache_on_the_signature_collapses_the_hit_ratio() -> None:
    clock, origin = FakeClock(1000), Origin()
    verifier = SignedUrlVerifier({"k1": b"secret-1"}, clock)
    topo = build_topology(
        origin, clock, pops=1, shield=False, verifier=verifier, cache_signature_params=True
    )
    client = topo.client("edge-0")
    for exp in (1060, 1061, 1062):
        await client.get(verifier.sign("/products/1", [], exp=exp, kid="k1"))
    assert len(origin.requests) == 3


# ---- authenticated and personalized responses -------------------------------------------------


async def two_users(origin: Origin) -> tuple[httpx.Response, httpx.Response, int]:
    client = build_topology(origin, FakeClock(), pops=1, shield=False).client("edge-0")
    alice = await client.get("/orders", headers={"Authorization": "Bearer alice"})
    bob = await client.get("/orders", headers={"Authorization": "Bearer bob"})
    return alice, bob, len(origin.requests)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_age_alone_never_lets_a_shared_cache_reuse_an_authorized_response() -> None:
    origin = Origin(orders_headers={"Cache-Control": "max-age=60"})
    alice, bob, origin_requests = await two_users(origin)
    assert (alice.json()["owner"], bob.json()["owner"]) == ("Bearer alice", "Bearer bob")
    assert origin_requests == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_s_maxage_on_a_personalized_response_leaks_it_to_the_next_user() -> None:
    origin = Origin(orders_headers={"Cache-Control": "s-maxage=60"})
    _, bob, origin_requests = await two_users(origin)
    assert bob.json()["owner"] == "Bearer alice"  # the trap RFC 9111 Section 3.5 warns about
    assert origin_requests == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vary_authorization_keeps_users_apart_even_with_s_maxage() -> None:
    origin = Origin(orders_headers={"Cache-Control": "s-maxage=60", "Vary": "Authorization"})
    _, bob, origin_requests = await two_users(origin)
    assert bob.json()["owner"] == "Bearer bob"
    assert "fwd=vary-miss" in cache_status(bob)
    assert origin_requests == 2
