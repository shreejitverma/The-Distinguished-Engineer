"""Vary secondary cache keys, normalization traps, and hit-ratio collapse (RFC 9111 Section 4.1)."""

import gzip

import h2.config
import h2.connection
import h2.events
import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f03_http_caching_and_conditional_requests.vary_cache import (
    EdgeCache,
    HeaderLines,
    VaryCache,
    accept_language_workload,
    combined_field_value,
    lookup_fallbacks,
    lookup_language,
    normalize_weighted_list,
    parse_vary,
    select_content_coding,
    simulate,
    user_agent_workload,
)

PAYLOAD = b'{"items": [' + b", ".join(b'{"id": %d}' % i for i in range(200)) + b"]}"
ACCEPT_ENCODING_FORMS = (
    "gzip, deflate, br, zstd",
    "gzip, deflate, br",
    "gzip, deflate",
    "gzip,deflate",
    "GZIP, Deflate",
    "deflate, gzip",
    "br;q=1.0, gzip;q=0.8, *;q=0.1",
    "gzip;q=1",
    "identity",
    "",
    "deflate",
    "gzip;q=0, deflate",
)


def gzip_app() -> Starlette:
    async def catalog(request: Request) -> PlainTextResponse:
        return PlainTextResponse(PAYLOAD.decode(), media_type="application/json")

    return Starlette(
        routes=[Route("/catalog", catalog)],
        middleware=[Middleware(GZipMiddleware, minimum_size=500)],
    )


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def raw_get(
    client: httpx.AsyncClient, uri: str, headers: HeaderLines
) -> tuple[int, HeaderLines, bytes]:
    """Send exactly `headers` (no client defaults) and return the undecoded body bytes."""
    request = httpx.Request("GET", f"http://test{uri}", headers=list(headers))
    response = await client.send(request, stream=True)
    body = b"".join([chunk async for chunk in response.aiter_raw()])
    await response.aclose()
    return response.status_code, response.headers.multi_items(), body


def header(headers: HeaderLines, name: str) -> str | None:
    return combined_field_value(headers, name)


@pytest.mark.unit
def test_parse_vary_combines_lines_lowercases_and_dedupes() -> None:
    assert parse_vary(["Accept-Encoding, ,Origin", "accept-encoding", "ORIGIN"]) == (
        "accept-encoding",
        "origin",
    )
    assert parse_vary(["Accept-Language", "*"]) == "*"
    assert parse_vary([]) == ()


@pytest.mark.unit
def test_vary_star_response_is_stored_but_never_matches() -> None:
    cache = VaryCache()
    cache.store("/a", [], 200, [("vary", "*")], b"x")
    assert cache.variant_count("/a") == 1
    assert cache.lookup("/a", []) is None


@pytest.mark.unit
def test_absent_field_matches_only_absent_and_empty_is_not_absent() -> None:
    cache = VaryCache()
    cache.store("/a", [], 200, [("vary", "accept-language")], b"absent")
    cache.store("/a", [("accept-language", "")], 200, [("vary", "accept-language")], b"empty")

    absent = cache.lookup("/a", [])
    empty = cache.lookup("/a", [("Accept-Language", "")])
    assert absent is not None
    assert absent.body == b"absent"
    assert empty is not None
    assert empty.body == b"empty"
    assert cache.lookup("/a", [("accept-language", "fr")]) is None


@pytest.mark.unit
def test_repeated_lines_combine_with_comma_but_cookie_with_semicolon() -> None:
    headers = [
        ("Accept-Language", "fr "),
        ("accept-language", " en;q=0.5"),
        ("cookie", "a=1"),
        ("Cookie", "b=2"),
    ]
    assert combined_field_value(headers, "accept-language") == "fr, en;q=0.5"
    assert combined_field_value(headers, "cookie") == "a=1; b=2"
    assert combined_field_value(headers, "authorization") is None


@pytest.mark.unit
def test_raw_key_misses_on_spelling_variants_that_semantic_normalization_merges() -> None:
    spellings = ["en-US,en;q=0.9", "en-us, en;Q=0.90", " en-US;q=1.0 ,, en;q=0.9"]
    raw = VaryCache()
    normalized = VaryCache(normalizers={"accept-language": normalize_weighted_list})
    for cache in (raw, normalized):
        cache.store(
            "/a", [("accept-language", spellings[0])], 200, [("vary", "accept-language")], b""
        )

    assert [raw.lookup("/a", [("accept-language", s)]) is not None for s in spellings] == [
        True,
        False,
        False,
    ]
    assert all(normalized.lookup("/a", [("accept-language", s)]) for s in spellings)
    assert normalize_weighted_list(spellings[2]) == "en-us, en;q=0.9"
    # Order is preserved: reordering is not known to be semantics-preserving (RFC 9110 12.5.4).
    assert normalize_weighted_list("fr, en") != normalize_weighted_list("en, fr")


@pytest.mark.unit
def test_lookup_prefers_most_recent_match_that_carried_vary() -> None:
    cache = VaryCache()
    cache.store("/doc", [("accept-language", "fr")], 200, [("vary", "accept-language")], b"fr")
    cache.store("/doc", [], 200, [], b"default without vary")  # newer, matches everything

    french = cache.lookup("/doc", [("accept-language", "fr")])
    german = cache.lookup("/doc", [("accept-language", "de")])
    assert french is not None
    assert french.body == b"fr"
    assert german is not None
    assert german.body == b"default without vary"


@pytest.mark.unit
def test_each_stored_response_is_matched_against_its_own_vary() -> None:
    cache = VaryCache()
    cache.store("/x", [("accept-encoding", "gzip")], 200, [("vary", "accept-encoding")], b"v1")
    cache.store(
        "/x",
        [("accept-encoding", "gzip"), ("origin", "https://a.example")],
        200,
        [("vary", "accept-encoding, origin")],
        b"v2",
    )
    same_encoding_other_origin = cache.lookup(
        "/x", [("accept-encoding", "gzip"), ("origin", "https://b.example")]
    )
    assert same_encoding_other_origin is not None
    assert same_encoding_other_origin.body == b"v1"
    both_match = cache.lookup("/x", [("accept-encoding", "gzip"), ("origin", "https://a.example")])
    assert both_match is not None
    assert both_match.body == b"v2"  # the most recent of several matches
    assert cache.variant_count("/x") == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("accept_encoding", "expected"),
    [
        (None, "identity"),
        ("", "identity"),
        ("gzip, deflate, br, zstd", "br"),
        ("GZIP", "gzip"),
        ("gzip;q=0", "identity"),
        ("br;q=0.5, gzip;q=0.8", "gzip"),
        ("*;q=0.1", "br"),
        ("*, br;q=0", "gzip"),
        ("gzip;q=2", "identity"),
    ],
)
def test_select_content_coding_follows_rfc_9110_section_12_5_3(
    accept_encoding: str | None, expected: str
) -> None:
    assert select_content_coding(accept_encoding, ("br", "gzip")) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("accept_language", "expected"),
    [
        ("de-CH-1996", "de"),
        ("fr-CA, en;q=0.9", "fr"),
        ("ja, fr;q=0.2", "fr"),
        ("de;q=0.5, fr", "fr"),
        ("fr;q=0, en-GB;q=0.5", "en"),
        ("ja, fr;q=0", "en"),
        ("*, de;q=0.5", "de"),
        ("pt-BR-x-private", "en"),
        (None, "en"),
    ],
)
def test_lookup_language_truncates_ranges_per_rfc_4647_section_3_4(
    accept_language: str | None, expected: str
) -> None:
    assert lookup_language(accept_language, ("en", "fr", "de"), default="en") == expected


@pytest.mark.unit
def test_lookup_fallbacks_reproduce_the_rfc_4647_example() -> None:
    assert lookup_fallbacks("zh-Hant-CN-x-private1-private2") == [
        "zh-Hant-CN-x-private1-private2",
        "zh-Hant-CN-x-private1",
        "zh-Hant-CN",
        "zh-Hant",
        "zh",
    ]


@pytest.mark.unit
def test_vary_user_agent_collapses_hit_ratio_where_a_client_hint_does_not() -> None:
    workload = user_agent_workload(distinct=8000, requests=100_000, seed=2026)
    results = {
        (vary, lifetime): simulate(workload, vary, lifetime=lifetime)
        for vary in ("user-agent", "sec-ch-ua-mobile")
        for lifetime in (None, 1000, 100)
    }
    ua_forever = results[("user-agent", None)]
    assert ua_forever.variants == 5713
    assert ua_forever.hits == ua_forever.requests - ua_forever.variants
    assert results[("user-agent", 1000)].hits == 56467
    assert results[("user-agent", 100)].hits == 31417
    # Two variants refill once per lifetime window each: 100 windows x 2 = 200 misses.
    assert results[("sec-ch-ua-mobile", 1000)].hits == 100_000 - 200
    assert results[("sec-ch-ua-mobile", 100)].hits == 98020


@pytest.mark.unit
def test_accept_language_normalization_tiers_recover_the_hit_ratio() -> None:
    workload = accept_language_workload(requests=100_000, seed=2026)

    def canonical(value: str | None) -> str:
        return lookup_language(value, ("en", "fr", "de"), default="en")

    raw = simulate(workload, "accept-language", lifetime=1000)
    semantic = simulate(
        workload,
        "accept-language",
        lifetime=1000,
        normalizers={"accept-language": normalize_weighted_list},
    )
    canonicalized = simulate(
        workload,
        "accept-language",
        lifetime=1000,
        canonicalizers={"accept-language": canonical},
    )
    assert (raw.variants, raw.hits) == (4062, 48259)
    assert (semantic.variants, semantic.hits) == (520, 74779)
    assert (canonicalized.variants, canonicalized.hits) == (3, 99700)


@pytest.mark.unit
def test_vary_authorization_with_per_request_tokens_never_hits() -> None:
    per_request: list[HeaderLines] = [[("authorization", f"Bearer t{i}")] for i in range(5000)]
    per_user: list[HeaderLines] = [[("authorization", f"Bearer u{i % 50}")] for i in range(5000)]
    assert simulate(per_request, "authorization").hits == 0
    assert simulate(per_user, "authorization").hits == 5000 - 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_gzip_compresses_for_gzip_q0_and_reads_only_first_line() -> None:
    async with client_for(gzip_app()) as client:
        _, refused, _ = await raw_get(client, "/catalog", [("accept-encoding", "gzip;q=0")])
        _, split, _ = await raw_get(
            client, "/catalog", [("accept-encoding", "br"), ("accept-encoding", "gzip")]
        )

    # starlette 1.10.0 GZipMiddleware tests `"gzip" in headers.get("Accept-Encoding", "")`.
    assert header(refused, "content-encoding") == "gzip"
    assert select_content_coding("gzip;q=0", ("gzip",)) == "identity"
    # Headers.get returns the first line only, so the combined "br, gzip" is never seen.
    assert header(split, "content-encoding") is None
    assert header(split, "vary") == "Accept-Encoding"
    assert select_content_coding("br, gzip", ("gzip",)) == "gzip"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_only_canonicalization_serves_gzip_to_a_client_that_refused_it() -> None:
    def canonical(value: str | None) -> str:
        return select_content_coding(value, ("gzip",))

    async with client_for(gzip_app()) as client:

        async def upstream(uri: str, headers: HeaderLines) -> tuple[int, HeaderLines, bytes]:
            return await raw_get(client, uri, headers)

        broken = EdgeCache(upstream, {"accept-encoding": canonical}, forward_canonical=False)
        fixed = EdgeCache(upstream, {"accept-encoding": canonical}, forward_canonical=True)
        for edge in (broken, fixed):
            await edge.get("/catalog", [("accept-encoding", "gzip;q=0")])
        served_broken = await broken.get("/catalog", [("accept-encoding", "identity")])
        served_fixed = await fixed.get("/catalog", [("accept-encoding", "identity")])

    assert (broken.hits, fixed.hits) == (1, 1)
    assert header(served_broken.headers, "content-encoding") == "gzip"
    assert served_broken.body[:2] == b"\x1f\x8b"
    assert header(served_fixed.headers, "content-encoding") is None
    assert served_fixed.body == PAYLOAD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_canonicalize_and_forward_collapses_accept_encoding_to_two_variants() -> None:
    def canonical(value: str | None) -> str:
        return select_content_coding(value, ("gzip",))

    async with client_for(gzip_app()) as client:

        async def upstream(uri: str, headers: HeaderLines) -> tuple[int, HeaderLines, bytes]:
            return await raw_get(client, uri, headers)

        raw_edge = EdgeCache(upstream)
        semantic_edge = EdgeCache(
            upstream, cache=VaryCache(normalizers={"accept-encoding": normalize_weighted_list})
        )
        canonical_edge = EdgeCache(upstream, {"accept-encoding": canonical})
        for _ in range(2):
            for form in ACCEPT_ENCODING_FORMS:
                for edge in (raw_edge, semantic_edge):
                    await edge.get("/catalog", [("accept-encoding", form)])
                served = await canonical_edge.get("/catalog", [("accept-encoding", form)])
                encoding = header(served.headers, "content-encoding")
                assert encoding == (None if canonical(form) == "identity" else "gzip")
                assert (gzip.decompress(served.body) if encoding else served.body) == PAYLOAD

    assert raw_edge.cache.variant_count("/catalog") == 12
    assert semantic_edge.cache.variant_count("/catalog") == 10
    assert canonical_edge.cache.variant_count("/catalog") == 2
    assert (canonical_edge.hits, canonical_edge.misses) == (22, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_cors_wildcard_still_varies_on_origin_and_fragments_the_cache() -> None:
    async def ping(request: Request) -> PlainTextResponse:
        return PlainTextResponse("pong")

    app = Starlette(
        routes=[Route("/ping", ping)],
        middleware=[Middleware(CORSMiddleware, allow_origins=["*"])],
    )
    async with client_for(app) as client:

        async def upstream(uri: str, headers: HeaderLines) -> tuple[int, HeaderLines, bytes]:
            return await raw_get(client, uri, headers)

        edge = EdgeCache(upstream)
        served = [
            await edge.get("/ping", [("origin", f"https://site{i}.example")]) for i in range(20)
        ]
        _, no_origin, _ = await raw_get(client, "/ping", [])

    assert {header(s.headers, "access-control-allow-origin") for s in served} == {"*"}
    assert {header(s.headers, "vary") for s in served} == {"Origin"}
    # CORSMiddleware.send adds Origin to Vary even when the request carried no Origin.
    assert header(no_origin, "vary") == "Origin"
    assert header(no_origin, "access-control-allow-origin") is None
    assert {s.body for s in served} == {b"pong"}
    assert edge.cache.variant_count("/ping") == 20
    assert edge.hits == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_session_membership_test_adds_vary_cookie() -> None:
    async def probe(request: Request) -> PlainTextResponse:
        return PlainTextResponse("member" if "user" in request.session else "anonymous")

    async def public(request: Request) -> PlainTextResponse:
        return PlainTextResponse("public")

    app = Starlette(
        routes=[Route("/probe", probe), Route("/public", public)],
        middleware=[
            Middleware(SessionMiddleware, secret_key="lab-only-secret"),  # noqa: S106 - test key
        ],
    )
    async with client_for(app) as client:
        _, probed, _ = await raw_get(client, "/probe", [])
        _, untouched, _ = await raw_get(client, "/public", [])

    assert header(probed, "vary") == "Cookie"
    assert header(untouched, "vary") is None


def _h2_request_headers(*, normalize_inbound: bool) -> list[tuple[str, str]]:
    client = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=True, split_outbound_cookies=True)
    )
    server = h2.connection.H2Connection(
        h2.config.H2Configuration(
            client_side=False, header_encoding="utf-8", normalize_inbound_headers=normalize_inbound
        )
    )
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.send_headers(
        1,
        [
            (":method", "GET"),
            (":scheme", "https"),
            (":authority", "example.com"),
            (":path", "/catalog"),
            ("cookie", "theme=dark; lang=fr"),
        ],
        end_stream=True,
    )
    events = server.receive_data(client.data_to_send())
    received = [event for event in events if isinstance(event, h2.events.RequestReceived)]
    assert len(received) == 1
    assert received[0].headers is not None
    return [(str(name), str(value)) for name, value in received[0].headers]


@pytest.mark.unit
def test_h2_cookie_crumbs_rejoin_to_the_http_1_1_secondary_key() -> None:
    joined = _h2_request_headers(normalize_inbound=True)
    crumbs = _h2_request_headers(normalize_inbound=False)
    http11 = [("cookie", "theme=dark; lang=fr")]

    assert [value for name, value in crumbs if name == "cookie"] == ["theme=dark", "lang=fr"]
    assert [value for name, value in joined if name == "cookie"] == ["theme=dark; lang=fr"]

    cache = VaryCache()
    cache.store("/catalog", http11, 200, [("vary", "cookie")], b"dark-fr")
    assert cache.lookup("/catalog", joined) is not None
    assert cache.lookup("/catalog", crumbs) is not None
    # A cache that joined crumbs with the generic list comma would key them differently.
    assert ", ".join(v for n, v in crumbs if n == "cookie") != "theme=dark; lang=fr"
