"""Unit tests for RFC 9211 Cache-Status parsing, serialization, and emission by a tiered cache."""

from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.datastructures import MutableHeaders

from api_labs.f03_http_caching_and_conditional_requests.cache_status import (
    CacheStatusEntry,
    Date,
    DisplayString,
    InnerList,
    SharedCacheLayer,
    StructuredFieldError,
    Token,
    cache_status_or_empty,
    effective_fwd_status,
    lint_cache_status,
    parse_cache_status,
    parse_list,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Every single-line example in RFC 9211 Section 3, with its canonical RFC 9651 serialization.
RFC9211_SECTION3 = [
    ("ExampleCache; hit", "ExampleCache;hit"),
    ("ExampleCache; hit; ttl=376", "ExampleCache;hit;ttl=376"),
    ("ExampleCache; hit; ttl=-412", "ExampleCache;hit;ttl=-412"),
    ("ExampleCache; fwd=uri-miss", "ExampleCache;fwd=uri-miss"),
    ("ExampleCache; fwd=stale; fwd-status=304", "ExampleCache;fwd=stale;fwd-status=304"),
    ("ExampleCache; fwd=uri-miss; collapsed", "ExampleCache;fwd=uri-miss;collapsed"),
    ("ExampleCache; fwd=uri-miss; collapsed=?0", "ExampleCache;fwd=uri-miss;collapsed=?0"),
    ("ExampleCache; hit; detail=MEMORY", "ExampleCache;hit;detail=MEMORY"),
]

# A fresh debug credential per run; the cache compares it with hmac.compare_digest.
DEBUG_TOKEN = secrets.token_urlsafe(16)

# Captured with `curl -sS -D - https://docs.netlify.com/` on 2026-11-09 06:43:50 GMT.
LIVE_NETLIFY_LINES = [
    '"Netlify Edge"; hit; ttl=31489195',
    '"Netlify Edge"; fwd=stale; fwd-status=200; ttl=-10521; stored',
]


class FakeClock:
    """Deterministic clock in whole seconds for freshness arithmetic."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_origin(
    cache_control: str = "max-age=1200",
    *,
    gate: asyncio.Event | None = None,
    fail_first: bool = False,
) -> tuple[ASGIApp, list[str]]:
    """Return an origin ASGI app and the list of requests it has served."""
    calls: list[str] = []

    async def origin(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(f"{scope['method']} {scope['path']}")
        if gate is not None:
            await gate.wait()
        if fail_first and len(calls) == 1:
            raise RuntimeError("origin crashed")
        headers = [
            (b"content-type", b"text/plain"),
            (b"cache-control", cache_control.encode("latin-1")),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": f"v{len(calls)}".encode()})

    return origin, calls


def client(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def fetch(
    app: ASGIApp,
    path: str = "/r",
    *,
    method: str = "GET",
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
) -> httpx.Response:
    async with client(app) as c:
        return await c.request(method, path, headers=headers)


async def wait_for_waiters(layer: SharedCacheLayer, count: int) -> None:
    for _ in range(1000):
        if layer.collapsed_waiters == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} collapsed waiters, saw {layer.collapsed_waiters}")


@pytest.mark.unit
@pytest.mark.parametrize(("wire", "canonical"), RFC9211_SECTION3)
def test_rfc9211_examples_roundtrip_to_canonical_form(wire: str, canonical: str) -> None:
    (entry,) = parse_cache_status([wire])
    assert entry.serialize() == canonical
    assert parse_cache_status([canonical]) == (entry,)


@pytest.mark.unit
def test_rfc9211_example_parameters_map_to_typed_fields() -> None:
    stale_hit, revalidated = parse_cache_status(
        ["ExampleCache; hit; ttl=-412", "ExampleCache; fwd=stale; fwd-status=304"]
    )
    assert stale_hit == CacheStatusEntry(Token("ExampleCache"), hit=True, ttl=-412)
    assert revalidated == CacheStatusEntry(Token("ExampleCache"), fwd="stale", fwd_status=304)
    assert effective_fwd_status(revalidated, response_status=200) == 304
    (plain_miss,) = parse_cache_status(["ExampleCache; fwd=uri-miss"])
    assert effective_fwd_status(plain_miss, response_status=200) == 200
    assert effective_fwd_status(stale_hit, response_status=200) is None


@pytest.mark.unit
def test_three_field_lines_combine_in_order_with_origin_side_first() -> None:
    lines = [
        "ReverseProxyCache; hit",
        "ForwardProxyCache; fwd=uri-miss; collapsed; stored",
        "BrowserCache; fwd=uri-miss",
    ]
    entries = parse_cache_status(lines)
    assert [e.cache for e in entries] == [
        Token("ReverseProxyCache"),
        Token("ForwardProxyCache"),
        Token("BrowserCache"),
    ]
    assert entries[1] == CacheStatusEntry(
        Token("ForwardProxyCache"), fwd="uri-miss", stored=True, collapsed=True
    )
    assert parse_cache_status([", ".join(lines)]) == entries


@pytest.mark.unit
def test_live_netlify_two_member_chain_parses_and_lints_clean() -> None:
    inner, outer = parse_cache_status(LIVE_NETLIFY_LINES)
    assert inner == CacheStatusEntry("Netlify Edge", hit=True, ttl=31489195)
    assert outer == CacheStatusEntry(
        "Netlify Edge", fwd="stale", fwd_status=200, ttl=-10521, stored=True
    )
    assert lint_cache_status((inner, outer)) == []


@pytest.mark.unit
def test_netlify_blog_example_is_not_a_structured_list_so_whole_field_is_ignored() -> None:
    # From the 2024-04-08 Netlify blog post on Cache-Status, verbatim.
    blog_value = '"Netlify Edge"; hit, fwd=stale'
    with pytest.raises(StructuredFieldError, match="expected ','"):
        parse_cache_status([blog_value])
    assert cache_status_or_empty([blog_value]) == ()


@pytest.mark.unit
def test_netlify_docs_examples_hit_with_fwd_is_valid_but_flagged_and_true_token_is_void() -> None:
    # From docs.netlify.com "Debug with Cache-Status", fetched 2026-11-10, verbatim.
    swr_value = '"Netlify Edge"; hit; fwd=stale'
    durable_value = '"Netlify Durable"; fwd=uri-miss; stored=true; ttl=3600'
    entries = parse_cache_status([swr_value])
    assert entries == (CacheStatusEntry("Netlify Edge", hit=True, fwd="stale"),)
    problems = lint_cache_status(entries)
    assert len(problems) == 1
    assert "hit and fwd are exclusive" in problems[0]
    # `true` is a Token, not the Boolean `?1`, so the type constraint voids the whole field.
    with pytest.raises(StructuredFieldError, match="stored has the wrong type"):
        parse_cache_status([durable_value])
    assert cache_status_or_empty(['"Netlify Edge"; fwd=miss', durable_value]) == ()


@pytest.mark.unit
def test_one_bad_member_voids_every_cache_in_the_chain() -> None:
    lines = ["OriginCache; hit; ttl=30", "Edge; hit; ttl=1.5"]
    assert parse_cache_status(lines[:1]) != ()
    with pytest.raises(StructuredFieldError, match="ttl has the wrong type"):
        parse_cache_status(lines)
    assert cache_status_or_empty(lines) == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("C;ttl=1;ttl=2", CacheStatusEntry(Token("C"), ttl=2)),
        ("C; hit", CacheStatusEntry(Token("C"), hit=True)),
        ("C;hit=?0", CacheStatusEntry(Token("C"), hit=False)),
        ("C;ttl=999999999999999", CacheStatusEntry(Token("C"), ttl=999_999_999_999_999)),
        ('"a\\"b\\\\c";hit', CacheStatusEntry('a"b\\c', hit=True)),
        (
            "*cache/v1:edge;detail=MEMORY",
            CacheStatusEntry(Token("*cache/v1:edge"), detail=Token("MEMORY")),
        ),
        (
            'C;detail="ram tier";key="/r?x=1"',
            CacheStatusEntry(Token("C"), detail="ram tier", key="/r?x=1"),
        ),
    ],
    ids=[
        "dup-key-last-wins",
        "sp-after-semicolon",
        "hit-false",
        "15-digits",
        "escapes",
        "token-chars",
        "strings",
    ],
)
def test_valid_grammar_edges(wire: str, expected: CacheStatusEntry) -> None:
    assert parse_cache_status([f"  {wire}"]) == (expected,)


@pytest.mark.unit
def test_tab_ows_between_members_and_extension_parameters_of_every_type() -> None:
    wire = 'A;hit,\tB;hit;x-ratio=0.25;x-blob=:aGk=:;x-at=@1700000000;x-msg=%"caf%c3%a9";x-off=?0'
    a, b = parse_cache_status([wire])
    assert a == CacheStatusEntry(Token("A"), hit=True)
    assert b.extensions == (
        ("x-ratio", Decimal("0.25")),
        ("x-blob", b"hi"),
        ("x-at", Date(1700000000)),
        ("x-msg", DisplayString("caf\u00e9")),
        ("x-off", False),
    )
    assert parse_list("C;x=-1.5;y=:aGk:") == [(Token("C"), {"x": Decimal("-1.5"), "y": b"hi"})]


@pytest.mark.unit
def test_inner_lists_parse_generically_but_are_not_cache_identifiers() -> None:
    assert parse_list('("a" b);lvl=5, ()') == [
        (InnerList((("a", {}), (Token("b"), {}))), {"lvl": 5}),
        (InnerList(()), {}),
    ]
    with pytest.raises(StructuredFieldError, match="String or Token"):
        parse_cache_status(["(A B);hit"])
    assert parse_list("") == []
    assert parse_list("   ") == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "wire",
    [
        "C;HIT",
        "C;ttl=1234567890123456",
        "A;hit,",
        "A B",
        '"a\\x"',
        '"a\\',
        '"unterminated',
        '"a\tb"',
        "C;hit=1",
        'C;fwd="uri-miss"',
        "C;fwd-status=?1",
        "C;ttl=?1",
        "42;hit",
        "caf\u00e9;hit",
        "!",
        "C;x=:a$:",
        "C;x=:abc",
        "C;x=:a===:",
        "C;x=?2",
        "C;x=-",
        "C;x=1.",
        "C;x=1.2345",
        "C;x=1234567890123.5",
        "C;x=123456789012.1234",
        "C;x=@1.5",
        'C;x=%"%C3"',
        'C;x=%"%ff"',
        'C;x=%"a\tb"',
        'C;x=%"abc',
        "C;x=%x",
        "(A",
        "(",
        "(A,B)",
    ],
)
def test_invalid_field_values_fail_strictly(wire: str) -> None:
    with pytest.raises(StructuredFieldError):
        parse_cache_status([wire])


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    [
        CacheStatusEntry("edge\r\nSet-Cookie: x=1", hit=True),
        CacheStatusEntry(Token("1cache"), hit=True),
        CacheStatusEntry(Token(""), hit=True),
        CacheStatusEntry(Token("ca che"), hit=True),
        CacheStatusEntry(Token("C"), ttl=10**15),
        CacheStatusEntry(Token("C"), extensions=(("X-Up", True),)),
        CacheStatusEntry(Token("C"), extensions=(("", True),)),
        CacheStatusEntry(Token("C"), extensions=(("a b", True),)),
        CacheStatusEntry(Token("C"), extensions=(("x", Decimal("1.5")),)),
    ],
    ids=[
        "crlf-injection",
        "digit-token",
        "empty-token",
        "space-token",
        "int-range",
        "upper-key",
        "empty-key",
        "space-key",
        "decimal",
    ],
)
def test_serializer_refuses_values_that_would_corrupt_the_field(entry: CacheStatusEntry) -> None:
    with pytest.raises(StructuredFieldError):
        entry.serialize()


@pytest.mark.unit
def test_serializer_escapes_strings_and_encodes_bytes() -> None:
    entry = CacheStatusEntry('say "hi" \\', fwd="miss", extensions=(("x-b", b"hi"), ("x-f", False)))
    assert entry.serialize() == '"say \\"hi\\" \\\\";fwd=miss;x-b=:aGk=:;x-f=?0'
    assert parse_cache_status([entry.serialize()]) == (entry,)


@pytest.mark.unit
def test_lint_flags_should_level_violations_a_parser_accepts() -> None:
    entries = parse_cache_status(
        [
            "A;hit;fwd=stale",
            "B;stored",
            "C;fwd=cache-hit",
            "D;collapsed=?0",
            "E;fwd=uri-miss;stored",
        ]
    )
    assert lint_cache_status(entries) == [
        "member 0 (A;hit;fwd=stale): hit and fwd are exclusive",
        "member 1 (B;stored): stored is only meaningful with fwd",
        "member 2 (C;fwd=cache-hit): fwd=cache-hit is not an RFC 9211 Section 2.2 reason",
        "member 3 (D;collapsed=?0): collapsed is only meaningful with fwd",
    ]


@pytest.mark.unit
def test_httpx_comma_split_corrupts_a_quoted_identifier_the_parser_keeps() -> None:
    headers = httpx.Headers([("cache-status", '"CDN, Inc";hit'), ("cache-status", "Edge;hit")])
    assert headers["cache-status"] == '"CDN, Inc";hit, Edge;hit'
    assert headers.get_list("cache-status", split_commas=True) == ['"CDN', 'Inc";hit', "Edge;hit"]
    entries = parse_cache_status(headers.get_list("cache-status"))
    assert [e.cache for e in entries] == ["CDN, Inc", Token("Edge")]


@pytest.mark.unit
def test_starlette_setitem_drops_the_upstream_member_and_append_keeps_it() -> None:
    replaced = MutableHeaders(raw=[(b"cache-status", b"OriginCache;hit")])
    replaced["cache-status"] = "Edge;hit"
    assert replaced.getlist("cache-status") == ["Edge;hit"]
    appended = MutableHeaders(raw=[(b"cache-status", b"OriginCache;hit")])
    appended.append("cache-status", "Edge;hit")
    assert appended.getlist("cache-status") == ["OriginCache;hit", "Edge;hit"]
    assert appended["cache-status"] == "OriginCache;hit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cold_miss_appends_one_member_per_tier_origin_side_first() -> None:
    clock = FakeClock()
    origin, calls = make_origin()
    origin_cache = SharedCacheLayer(origin, name=Token("OriginCache"), clock=clock)
    edge = SharedCacheLayer(origin_cache, name="CDN Company Here", clock=clock)
    response = await fetch(edge)
    assert response.headers.get_list("cache-status") == [
        "OriginCache;fwd=uri-miss;ttl=1200;stored",
        '"CDN Company Here";fwd=uri-miss;ttl=1200;stored',
    ]
    assert calls == ["GET /r"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_pops_reproduce_the_rfc9211_two_layer_example() -> None:
    clock = FakeClock()
    origin, calls = make_origin("max-age=1200")
    origin_cache = SharedCacheLayer(origin, name=Token("OriginCache"), clock=clock)
    pop_a = SharedCacheLayer(origin_cache, name="CDN Company Here", clock=clock)
    pop_b = SharedCacheLayer(origin_cache, name="CDN Company Here", clock=clock)
    await fetch(pop_a)
    clock.now = 100
    await fetch(pop_b)
    clock.now = 655
    response = await fetch(pop_b)
    rfc_example = 'OriginCache; hit; ttl=1100, "CDN Company Here"; hit; ttl=545'
    assert (
        response.headers["cache-status"]
        == 'OriginCache;hit;ttl=1100, "CDN Company Here";hit;ttl=545'
    )
    assert response.headers["cache-status"] != rfc_example
    assert parse_cache_status([response.headers["cache-status"]]) == parse_cache_status(
        [rfc_example]
    )
    assert response.headers["age"] == "655"
    assert calls == ["GET /r"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_freshness_boundary_hit_then_stale_forward() -> None:
    clock = FakeClock()
    origin, calls = make_origin("max-age=60")
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=clock)
    await fetch(edge)
    clock.now = 59
    hit = await fetch(edge)
    assert (hit.headers["cache-status"], hit.headers["age"], hit.text) == (
        "Edge;hit;ttl=1",
        "59",
        "v1",
    )
    clock.now = 60
    stale = await fetch(edge)
    assert (stale.headers["cache-status"], stale.text) == ("Edge;fwd=stale;ttl=60;stored", "v2")
    assert len(calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cache_control", "member"),
    [
        ("s-maxage=30, max-age=600", "Edge;fwd=uri-miss;ttl=30;stored"),
        ("max-age=60,", "Edge;fwd=uri-miss;ttl=60;stored"),
        ("max-age=abc", "Edge;fwd=uri-miss;stored=?0"),
        ("no-store, max-age=60", "Edge;fwd=uri-miss;stored=?0"),
        ("private, max-age=60", "Edge;fwd=uri-miss;stored=?0"),
        ("no-cache, max-age=60", "Edge;fwd=uri-miss;stored=?0"),
        ("max-age=\xb2", "Edge;fwd=uri-miss;stored=?0"),
        ("public", "Edge;fwd=uri-miss;stored=?0"),
    ],
)
async def test_stored_and_ttl_follow_shared_cache_freshness(
    cache_control: str, member: str
) -> None:
    origin, _ = make_origin(cache_control)
    response = await fetch(SharedCacheLayer(origin, name=Token("Edge"), clock=FakeClock()))
    assert response.headers["cache-status"] == member


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forward_reasons_for_request_directive_method_and_bypass() -> None:
    clock = FakeClock()
    origin, calls = make_origin("max-age=60")
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=clock, bypass_prefixes=("/admin",))
    await fetch(edge)
    clock.now = 10
    no_cache = await fetch(edge, headers={"cache-control": "no-cache"})
    assert no_cache.headers["cache-status"] == "Edge;fwd=request;ttl=60;stored"
    max_age_0 = await fetch(edge, headers={"cache-control": "max-age=0"})
    assert max_age_0.headers["cache-status"] == "Edge;fwd=request;ttl=60;stored"
    post = await fetch(edge, method="POST")
    assert post.headers["cache-status"] == "Edge;fwd=method;stored=?0"
    head = await fetch(edge, method="HEAD")
    assert head.headers["cache-status"] == "Edge;fwd=bypass;stored=?0"
    admin = await fetch(edge, "/admin/stats")
    assert admin.headers["cache-status"] == "Edge;fwd=bypass;stored=?0"
    assert len(calls) == 6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_misses_collapse_into_one_forward() -> None:
    gate = asyncio.Event()
    origin, calls = make_origin("max-age=60", gate=gate)
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=FakeClock())
    async with client(edge) as c:
        tasks = [asyncio.create_task(c.get("/r")) for _ in range(3)]
        await wait_for_waiters(edge, 2)
        gate.set()
        responses = await asyncio.gather(*tasks)
    assert calls == ["GET /r"]
    assert sorted(r.headers["cache-status"] for r in responses) == [
        "Edge;fwd=uri-miss;ttl=60;collapsed",
        "Edge;fwd=uri-miss;ttl=60;collapsed",
        "Edge;fwd=uri-miss;ttl=60;stored",
    ]
    assert {r.text for r in responses} == {"v1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uncacheable_leader_makes_waiters_forward_with_collapsed_false() -> None:
    gate = asyncio.Event()
    origin, calls = make_origin("private, max-age=60", gate=gate)
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=FakeClock())
    async with client(edge) as c:
        tasks = [asyncio.create_task(c.get("/r")) for _ in range(3)]
        await wait_for_waiters(edge, 2)
        gate.set()
        responses = await asyncio.gather(*tasks)
    assert len(calls) == 3
    assert sorted(r.headers["cache-status"] for r in responses) == [
        "Edge;fwd=uri-miss;stored=?0",
        "Edge;fwd=uri-miss;stored=?0;collapsed=?0",
        "Edge;fwd=uri-miss;stored=?0;collapsed=?0",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_leader_releases_waiters_instead_of_hanging_them() -> None:
    gate = asyncio.Event()
    origin, calls = make_origin("max-age=60", gate=gate, fail_first=True)
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=FakeClock())
    async with client(edge) as c:
        leader = asyncio.create_task(c.get("/r"))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(c.get("/r"))
        await wait_for_waiters(edge, 1)
        gate.set()
        with pytest.raises(RuntimeError, match="origin crashed"):
            await leader
        response = await asyncio.wait_for(waiter, timeout=1.0)
    assert response.headers["cache-status"] == "Edge;fwd=uri-miss;ttl=60;stored;collapsed=?0"
    assert len(calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_parameter_is_emitted_only_for_an_authorized_debug_request() -> None:
    clock = FakeClock()
    origin, _ = make_origin("max-age=60")
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=clock, debug_token=DEBUG_TOKEN)
    anonymous = await fetch(edge, "/r?x=1")
    assert anonymous.headers["cache-status"] == "Edge;fwd=uri-miss;ttl=60;stored"
    wrong = await fetch(edge, "/r?x=1", headers={"cache-debug": "guess"})
    assert wrong.headers["cache-status"] == "Edge;hit;ttl=60"
    authorized = await fetch(edge, "/r?x=1", headers={"cache-debug": DEBUG_TOKEN})
    assert authorized.headers["cache-status"] == 'Edge;hit;ttl=60;key="/r?x=1"'
    duplicated = await fetch(edge, "/r?x=1", headers=[("cache-debug", DEBUG_TOKEN)] * 2)
    assert duplicated.headers["cache-status"] == "Edge;hit;ttl=60"
    no_token = SharedCacheLayer(origin, name=Token("Edge"), clock=clock)
    assert (
        "key="
        not in (await fetch(no_token, headers={"cache-debug": DEBUG_TOKEN})).headers["cache-status"]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_locally_generated_414_carries_no_member() -> None:
    origin, calls = make_origin()
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=FakeClock(), max_target_bytes=64)
    response = await fetch(edge, "/" + "a" * 64)
    assert response.status_code == 414
    assert "cache-status" not in response.headers
    assert calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_path_bytes_are_escaped_before_entering_the_key_string() -> None:
    origin, _ = make_origin()
    edge = SharedCacheLayer(origin, name=Token("Edge"), clock=FakeClock(), debug_token=DEBUG_TOKEN)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/a%20b\u00ff",
        "raw_path": b"/a%20b\xff",
        "query_string": b"",
        "headers": [(b"cache-debug", DEBUG_TOKEN.encode())],
    }
    await edge(scope, receive, send)
    header_lines = [v for n, v in sent[0]["headers"] if n == b"cache-status"]
    assert header_lines == [b'Edge;fwd=uri-miss;ttl=1200;stored;key="/a%2520b%FF"']


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        raise AssertionError("no message expected")

    await SharedCacheLayer(app, name=Token("Edge"), clock=FakeClock())(
        {"type": "lifespan"}, receive, send
    )
    assert seen == ["lifespan"]
