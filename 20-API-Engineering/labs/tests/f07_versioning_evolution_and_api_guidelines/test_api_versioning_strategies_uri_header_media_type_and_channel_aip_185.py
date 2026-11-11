"""Unit tests for URI, header, Accept media type, query, and AIP-185 channel versioning."""

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

import h2.config
import h2.connection
import h2.errors
import h2.events
import httpx
import hypercorn.asyncio
import hypercorn.config
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from granian.constants import Interfaces
from granian.server.embed import Server as GranianServer
from starlette.datastructures import Headers, QueryParams
from starlette.middleware.gzip import GZipMiddleware

from api_labs.f07_versioning_evolution_and_api_guidelines.versioning_topologies import (
    CATALOG,
    ChannelSurface,
    NegotiationPolicy,
    SharedCache,
    Strategy,
    VersioningError,
    VersionSegment,
    build_app,
    channel_violations,
    edge_accept_normalizer,
    fetch_through_cache,
    parse_accept,
    parse_version_segment,
    promotion_violations,
    quality_of,
    qvalue_to_weight,
    resolve_version,
)

V1_BODY = {"id": 7, "name": "sprocket"}
V2_BODY = {"display_name": "sprocket", "id": 7}


def _client(app: FastAPI) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    del client.headers["accept"]  # httpx sends Accept: */* by default; absence must be testable
    del client.headers["accept-encoding"]
    return client


def _accept_only(value: str, policy: NegotiationPolicy | None = None) -> tuple[int, str, Strategy]:
    decision = resolve_version(
        accept_values=[value],
        header_values=[],
        query_values=[],
        policy=policy or NegotiationPolicy(),
    )
    return decision.version, decision.media_type, decision.source


@pytest.mark.unit
def test_qvalue_grammar_maps_to_integer_thousandths() -> None:
    assert [qvalue_to_weight(q) for q in ("1", "1.000", "0.5", "0.001", "0", "0.")] == [
        1000,
        1000,
        500,
        1,
        0,
        0,
    ]
    for bad in ("0.0001", "1.1", ".5", "2", "-0", "0.5 "):
        with pytest.raises(VersioningError, match="invalid qvalue"):
            qvalue_to_weight(bad)


@pytest.mark.unit
def test_rfc9110_table5_precedence_with_erratum_7138() -> None:
    ranges = parse_accept(
        "text/*;q=0.3, text/plain;q=0.7, text/plain;format=flowed, "
        "text/plain;format=fixed;q=0.4, */*;q=0.5"
    )
    table = {
        "text/plain;format=flowed": 1000,
        "text/plain": 700,
        "text/html": 300,
        "image/jpeg": 500,
        "text/plain;format=fixed": 400,
        "text/html;level=3": 300,  # RFC 9110 prints 0.7; verified erratum 7138 corrects to 0.3
    }
    assert {media: quality_of(ranges, media) for media in table} == table
    assert quality_of(ranges, "TEXT/HTML") == 300  # candidate case folds (RFC 9110 8.3.1)


@pytest.mark.unit
def test_accept_parser_enforces_size_limits_and_grammar() -> None:
    assert len(parse_accept(" , ,application/json,, ")) == 1  # RFC 9110 5.6.1.2 empties
    assert parse_accept('application/json;note="a,b";q=0.5')[0].params == (("note", "a,b"),)
    escaped = parse_accept('application/json;note="a\\",b";q=0.5')  # quoted-pair, RFC 9110 5.6.4
    assert (escaped[0].params, escaped[0].weight) == ((("note", 'a",b'),), 500)
    # The limits are inclusive: exactly 1024 characters and exactly 32 ranges still parse.
    assert len(parse_accept(("application/json;x=" + "a" * 1024)[:1024])) == 1
    assert len(parse_accept(", ".join(["application/json"] * 32))) == 32
    cases = {
        "a/b" * 400: "accept_too_long",
        ("application/json;x=" + "a" * 1024)[:1025]: "accept_too_long",
        ", ".join(["application/json"] * 33): "accept_too_many_ranges",
        "*/json": "malformed_accept",
        'application/json;note="open': "malformed_accept",
        'application/json;note="a"b"': "malformed_accept",  # odd quote count, looks balanced
        "application/json; q = 0.5": "malformed_accept",
        "application/json;x=a b": "malformed_accept",  # a parameter value is a token or quoted
    }
    for value, code in cases.items():
        with pytest.raises(VersioningError) as excinfo:
            parse_accept(value)
        assert (excinfo.value.status_code, excinfo.value.code) == (400, code)


@pytest.mark.unit
def test_vendor_media_type_matching_is_case_insensitive() -> None:
    assert _accept_only("Application/VND.ACME.V2+JSON") == (
        2,
        "application/vnd.acme.v2+json",
        Strategy.MEDIA_TYPE,
    )
    # Parameter names are case-insensitive too (RFC 9110 Section 5.6.6): Q is the weight.
    upper_q = parse_accept('application/json;Q=0.5;Note="Kept"')[0]
    assert (upper_q.weight, upper_q.params) == (500, (("note", "Kept"),))
    assert _accept_only("application/vnd.acme.v2+json;Q=0.4, application/json")[0] == 1


@pytest.mark.unit
def test_wildcard_accept_pins_default_version_not_latest() -> None:
    assert _accept_only("*/*") == (1, "application/vnd.acme.v1+json", Strategy.DEFAULT)
    assert _accept_only("application/*") == (1, "application/vnd.acme.v1+json", Strategy.DEFAULT)
    assert _accept_only("application/json") == (1, "application/json", Strategy.DEFAULT)
    assert _accept_only("application/vnd.acme.v2+json, */*;q=0.1")[0] == 2
    assert _accept_only("application/vnd.acme.v2+json;q=0.4, application/json")[0] == 1


@pytest.mark.unit
def test_equal_weight_ties_break_by_range_specificity_then_default_first() -> None:
    v2 = (2, "application/vnd.acme.v2+json", Strategy.MEDIA_TYPE)
    # Same weight: the candidate matched by the more specific range wins over a wildcard match.
    assert _accept_only("application/vnd.acme.v2+json, */*") == v2
    assert _accept_only("application/*;q=0.5, application/vnd.acme.v2+json;q=0.5") == v2
    # Same weight and same specificity: candidate order decides, and the default is first.
    assert _accept_only("application/json, application/vnd.acme.v2+json") == (
        1,
        "application/json",
        Strategy.DEFAULT,
    )
    assert _accept_only("application/vnd.acme.v2+json, application/vnd.acme.v1+json") == (
        1,
        "application/vnd.acme.v1+json",
        Strategy.MEDIA_TYPE,
    )
    # Two Accept field lines are one list (RFC 9110 Section 5.3): the second line can win.
    two_lines = resolve_version(
        accept_values=["application/json;q=0.1", "application/vnd.acme.v2+json"],
        header_values=[],
        query_values=[],
        policy=NegotiationPolicy(),
    )
    assert (two_lines.version, two_lines.media_type) == v2[:2]


@pytest.mark.unit
def test_version_value_grammar_rejects_leading_zero_sign_and_non_ascii_digits() -> None:
    def header(value: str) -> int:
        return resolve_version(
            accept_values=[], header_values=[value], query_values=[], policy=NegotiationPolicy()
        ).version

    assert [header("1"), header("2"), header(" 2 ")] == [1, 2, 2]
    # int() would accept "01", "+2", and the Arabic-Indic digit two; the grammar must not.
    for bad in ("01", "0", "1000", "+2", "2.0", "v2", "\u0662", ""):
        with pytest.raises(VersioningError) as excinfo:
            header(bad)
        assert (excinfo.value.status_code, excinfo.value.code) == (400, "invalid_version"), bad


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unacceptable_accept_returns_406_listing_available_types() -> None:
    async with _client(build_app()) as client:
        unknown = await client.get("/widgets/7", headers={"Accept": "application/vnd.acme.v9+json"})
        refused = await client.get(
            "/widgets/7",
            headers={"Accept": "application/json;q=0, application/vnd.acme.v1+json;q=0"},
        )
        bad_q = await client.get("/widgets/7", headers={"Accept": "application/json;q=1.5"})
        # A range parameter must match the representation; the lab's types carry none.
        with_param = await client.get(
            "/widgets/7", headers={"Accept": "application/vnd.acme.v2+json;charset=utf-8"}
        )
        pinned = await client.get("/widgets/7", headers={"Accept": "text/html", "API-Version": "2"})

    assert unknown.status_code == refused.status_code == with_param.status_code == 406
    assert pinned.status_code == 406
    assert pinned.json()["available"] == ["application/vnd.acme.v2+json", "application/json"]
    assert unknown.json()["available"] == [
        "application/vnd.acme.v1+json",
        "application/json",
        "application/vnd.acme.v2+json",
    ]
    for error in (unknown, bad_q):  # version-selection errors are uncacheable and still vary
        assert error.headers["cache-control"] == "no-store"
        assert error.headers["vary"] == "Accept, API-Version"
    assert bad_q.status_code == 400
    assert bad_q.json()["error"] == "malformed_accept"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_topology_routes_each_major_version_without_vary() -> None:
    async with _client(build_app()) as client:
        v1 = await client.get("/v1/widgets/7")
        v2 = await client.get("/v2/widgets/7")

    assert (v1.status_code, v1.json()) == (200, V1_BODY)
    assert (v2.status_code, v2.json()) == (200, V2_BODY)
    assert "vary" not in v1.headers
    assert "vary" not in v2.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_widget_is_404_without_vary_or_cache_control_on_both_routes() -> None:
    async with _client(build_app()) as client:
        negotiated = await client.get("/widgets/8", headers={"API-Version": "2"})
        by_path = await client.get("/v1/widgets/8")
        # Version resolution runs first, so a bad signal is still a 400 for an unknown widget.
        bad_signal = await client.get("/widgets/8", headers={"API-Version": "9"})

    for missing in (negotiated, by_path):
        assert (missing.status_code, missing.json()) == (404, {"error": "not_found"})
        assert "vary" not in missing.headers
        assert "cache-control" not in missing.headers
    assert (bad_signal.status_code, bad_signal.json()["error"]) == (400, "unsupported_version")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aip185_channels_are_nested_supersets_of_stable() -> None:
    async with _client(build_app()) as client:
        bodies = {
            c: (await client.get(f"/{c}/widgets/7")).json() for c in ("v1", "v1beta", "v1alpha")
        }

    stable, beta, alpha = (set(bodies[c]) for c in ("v1", "v1beta", "v1alpha"))
    assert stable < beta < alpha
    assert bodies["v1alpha"] == {
        "color": "#0000ff",
        "id": 7,
        "labels": ["blue"],
        "name": "sprocket",
    }
    assert channel_violations(CATALOG) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_version_segment_grammar_rejects_minor_versions_and_stable_suffix() -> None:
    assert parse_version_segment("v1") == VersionSegment(1, "stable", None)
    assert parse_version_segment("v1beta") == VersionSegment(1, "beta", None)
    assert parse_version_segment("v1beta2") == VersionSegment(1, "beta", 2)
    for bad in ("v1.1", "v1.0", "v1stable", "V1", "v01", "v0", "v1beta0", "1"):
        assert parse_version_segment(bad) is None, bad

    async with _client(build_app()) as client:
        statuses = [(await client.get(f"/{s}/widgets/7")).status_code for s in ("v1.1", "v1beta2")]
    assert statuses == [404, 404]  # v1beta2 parses (release-based) but is not published here


@pytest.mark.unit
def test_promotion_gate_rejects_pre_deprecated_arrival_and_broken_superset() -> None:
    pre_deprecated = {
        **CATALOG,
        "v1": ChannelSurface(frozenset({"id", "name", "labels"}), frozenset({"labels"})),
    }
    assert promotion_violations(CATALOG, pre_deprecated) == [
        "v1 gains already-deprecated ['labels']"
    ]

    beta_dropped_name = {**CATALOG, "v1beta": ChannelSurface(frozenset({"id", "labels"}))}
    assert promotion_violations(CATALOG, beta_dropped_name) == [
        "v1beta lacks ['name'] present in v1"
    ]

    # A channel that did not exist before has no prior surface: everything in it arrives.
    new_channel = {
        **CATALOG,
        "v3": ChannelSurface(frozenset({"id", "legacy"}), frozenset({"legacy"})),
    }
    assert promotion_violations(CATALOG, new_channel) == ["v3 gains already-deprecated ['legacy']"]

    # Deprecating an element that already shipped is allowed; only arrival is checked.
    deprecated_in_place = {
        **CATALOG,
        "v1": ChannelSurface(frozenset({"id", "name"}), frozenset({"name"})),
    }
    assert promotion_violations(CATALOG, deprecated_in_place) == []

    # With no beta channel, alpha must still be a superset of stable.
    no_beta = {
        "v1": ChannelSurface(frozenset({"id", "name"})),
        "v1alpha": ChannelSurface(frozenset({"id", "color"})),
    }
    assert channel_violations(no_beta) == ["v1alpha lacks ['name'] present in v1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_topology_selects_version_and_declares_vary() -> None:
    async with _client(build_app()) as client:
        response = await client.get("/widgets/7", headers={"API-Version": "2"})

    assert response.status_code == 200
    assert response.json() == V2_BODY
    assert response.headers["content-type"] == "application/vnd.acme.v2+json"
    assert response.headers["vary"] == "Accept, API-Version"
    assert response.headers["x-version-source"] == "header"

    async with _client(build_app()) as client:
        with_accept = await client.get(
            "/widgets/7", headers={"API-Version": "2", "Accept": "application/json"}
        )
    # The header pins the version; Accept then only chooses between that version's two types.
    assert (with_accept.status_code, with_accept.json()) == (200, V2_BODY)
    assert with_accept.headers["content-type"] == "application/json"
    assert with_accept.headers["x-version-source"] == "header"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_topology_selects_version_inside_the_target_uri() -> None:
    async with _client(build_app()) as client:
        response = await client.get("/widgets/7?api-version=2")
        default = await client.get("/widgets/7")

    assert (response.json(), response.headers["x-version-source"]) == (V2_BODY, "query")
    assert (default.json(), default.headers["x-version-source"]) == (V1_BODY, "default")
    assert default.headers["content-type"] == "application/vnd.acme.v1+json"

    async with _client(build_app()) as client:
        with_accept = await client.get("/widgets/7?api-version=2", headers={"Accept": "*/*"})
    assert (with_accept.json(), with_accept.headers["x-version-source"]) == (V2_BODY, "query")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_switches_ignore_disabled_signals_and_drop_them_from_vary() -> None:
    media_only = NegotiationPolicy(use_header=False, use_query=False)
    async with _client(build_app(media_only)) as client:
        # Neither signal is read, so even duplicates and garbage are not errors here.
        header = await client.get(
            "/widgets/7", headers=[("API-Version", "2"), ("API-Version", "junk")]
        )
        query = await client.get("/widgets/7?api-version=2&api-version=junk")
        accept = await client.get("/widgets/7", headers={"Accept": "application/vnd.acme.v2+json"})
        refused = await client.get("/widgets/7", headers={"Accept": "text/html"})

    for ignored in (header, query):
        assert (ignored.status_code, ignored.json()) == (200, V1_BODY)
        assert ignored.headers["x-version-source"] == "default"
    assert (accept.json(), accept.headers["x-version-source"]) == (V2_BODY, "media_type")
    # Vary names only the request fields this policy reads.
    for response in (header, query, accept, refused):
        assert response.headers["vary"] == "Accept"
    assert refused.status_code == 406


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_version_signals_are_rejected_not_first_or_last_wins() -> None:
    # Starlette's own lookups disagree: header __getitem__ is first-wins, query is last-wins.
    raw = [(b"api-version", b"1"), (b"api-version", b"2")]
    assert Headers(raw=raw)["api-version"] == "1"
    assert QueryParams("api-version=1&api-version=2")["api-version"] == "2"

    async with _client(build_app()) as client:
        twice_header = await client.get(
            "/widgets/7", headers=[("API-Version", "1"), ("API-Version", "2")]
        )
        twice_query = await client.get("/widgets/7?api-version=1&api-version=2")
        comma_header = await client.get("/widgets/7", headers={"API-Version": "1, 2"})
        # CVE-2021-23336 fix: parse_qsl no longer splits on ";", so this is one cloaked value.
        cloaked = await client.get("/widgets/7?api-version=1;api-version=2")

    responses = (twice_header, twice_query, comma_header, cloaked)
    assert [r.status_code for r in responses] == [400, 400, 400, 400]
    assert [r.json()["error"] for r in responses] == [
        "ambiguous_version",
        "ambiguous_version",
        "invalid_version",
        "invalid_version",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conflicting_header_and_query_signals_are_400() -> None:
    async with _client(build_app()) as client:
        conflict = await client.get("/widgets/7?api-version=1", headers={"API-Version": "2"})
        agree = await client.get("/widgets/7?api-version=2", headers={"API-Version": "2"})
        unknown = await client.get("/widgets/7", headers={"API-Version": "3"})

    assert (conflict.status_code, conflict.json()["error"]) == (400, "conflicting_version_signals")
    assert (agree.status_code, agree.json()) == (200, V2_BODY)
    assert (unknown.status_code, unknown.json()["error"]) == (400, "unsupported_version")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_cache_serves_wrong_version_when_default_response_omits_vary() -> None:
    results: dict[bool, tuple[bytes, bool, int]] = {}
    for emit_vary in (False, True):
        cache = SharedCache()
        async with _client(build_app(NegotiationPolicy(emit_vary=emit_vary))) as client:
            await fetch_through_cache(client, cache, "/widgets/7", [])  # default v1, stored
            _, body, from_cache = await fetch_through_cache(
                client, cache, "/widgets/7", [("API-Version", "2")]
            )
        results[emit_vary] = (body, from_cache, cache.origin_hits)

    assert results[False] == (b'{"id":7,"name":"sprocket"}', True, 1)  # v2 client got v1
    assert results[True] == (b'{"display_name":"sprocket","id":7}', False, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_and_query_versions_are_primary_cache_keys_even_without_vary() -> None:
    urls = [
        "/v1/widgets/7",
        "/v2/widgets/7",
        "/widgets/7?api-version=1",
        "/widgets/7?api-version=2",
    ]
    cache = SharedCache()
    # emit_vary=False: the target URI alone keeps these versions apart (RFC 9110 12.5.5).
    async with _client(build_app(NegotiationPolicy(emit_vary=False))) as client:
        first = [await fetch_through_cache(client, cache, url, []) for url in urls]
        again = [await fetch_through_cache(client, cache, url, []) for url in urls]

    v1, v2 = b'{"id":7,"name":"sprocket"}', b'{"display_name":"sprocket","id":7}'
    assert first == [(200, v1, False), (200, v2, False), (200, v1, False), (200, v2, False)]
    assert again == [(200, v1, True), (200, v2, True), (200, v1, True), (200, v2, True)]
    assert (cache.origin_hits, cache.entry_count(), sorted(cache.entries)) == (4, 4, sorted(urls))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_equivalent_accept_strings_fragment_cache_until_edge_normalizes() -> None:
    media_only = NegotiationPolicy(use_header=False, use_query=False)
    variants = [
        "application/vnd.acme.v2+json",
        "application/vnd.acme.v2+json;q=1",
        "Application/vnd.acme.v2+json",
        "application/vnd.acme.v2+json, */*;q=0.1",
    ]
    hits: dict[str, int] = {}
    for label, cache in (
        ("raw", SharedCache()),
        ("normalized", SharedCache(normalizer=edge_accept_normalizer(media_only))),
    ):
        async with _client(build_app(media_only)) as client:
            for accept in variants:
                _, body, _ = await fetch_through_cache(
                    client, cache, "/widgets/7", [("Accept", f"  {accept} ")]
                )
                assert body == b'{"display_name":"sprocket","id":7}'
        hits[label] = cache.origin_hits
        assert cache.entry_count() == cache.origin_hits

    header_cache = SharedCache()
    async with _client(build_app()) as client:
        for _ in variants:
            await fetch_through_cache(client, header_cache, "/widgets/7", [("API-Version", "2")])
    hits["header"] = header_cache.origin_hits

    assert hits == {"raw": 4, "normalized": 1, "header": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_normalizer_rewrites_only_accept_and_keeps_versions_apart() -> None:
    normalize = edge_accept_normalizer(NegotiationPolicy())
    assert normalize("accept", "Application/VND.ACME.V2+JSON;q=1") == "application/vnd.acme.v2+json"
    assert normalize("accept", "*/*") == "application/vnd.acme.v1+json"
    assert normalize("accept", None) is None  # absence stays absence (RFC 9111 Section 4.1)
    assert normalize("api-version", "2") == "2"  # other nominated fields pass through
    # Values that cannot be negotiated get distinct keys per error, never a shared one.
    assert normalize("accept", "text/html") == "error:not_acceptable"
    assert normalize("accept", "a/b;q=2") == "error:malformed_accept"

    cache = SharedCache(normalizer=normalize)
    async with _client(build_app()) as client:
        results = [
            await fetch_through_cache(client, cache, "/widgets/7", [("API-Version", version)])
            for version in ("2", "1", "2")
        ]
    assert results == [
        (200, b'{"display_name":"sprocket","id":7}', False),
        (200, b'{"id":7,"name":"sprocket"}', False),
        (200, b'{"display_name":"sprocket","id":7}', True),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_cache_matches_after_whitespace_trim_and_line_combining_only() -> None:
    media_only = NegotiationPolicy(use_header=False, use_query=False)
    cache = SharedCache()
    requests: list[list[tuple[str, str]]] = [
        [("Accept", "application/json;q=0.1, application/vnd.acme.v2+json")],  # stored
        [("Accept", "  application/json;q=0.1, application/vnd.acme.v2+json ")],  # whitespace
        [("accept", "application/json;q=0.1"), ("ACCEPT", "application/vnd.acme.v2+json")],  # lines
        [("Accept", "application/vnd.acme.v2+json, application/json;q=0.1")],  # reordered: a miss
    ]
    async with _client(build_app(media_only)) as client:
        served = [
            await fetch_through_cache(client, cache, "/widgets/7", headers) for headers in requests
        ]

    assert [body for _, body, _ in served] == [b'{"display_name":"sprocket","id":7}'] * 4
    assert [from_cache for _, _, from_cache in served] == [False, True, True, False]
    assert (cache.origin_hits, cache.entry_count()) == (2, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_cache_never_stores_errors_or_reuses_vary_star() -> None:
    cache = SharedCache()
    async with _client(build_app()) as client:
        for _ in range(2):
            status, _, from_cache = await fetch_through_cache(
                client, cache, "/widgets/7", [("Accept", "text/html")]
            )
            assert (status, from_cache) == (406, False)
    assert (cache.origin_hits, cache.entry_count()) == (2, 0)

    # The lab cache stores only a 200 that carries max-age; each condition is needed alone.
    unstorable = FastAPI()

    @unstorable.get("/gone")
    async def gone_with_max_age() -> JSONResponse:
        return JSONResponse({}, status_code=410, headers={"Cache-Control": "public, max-age=60"})

    @unstorable.get("/private")
    async def ok_without_max_age() -> JSONResponse:
        return JSONResponse({}, headers={"Cache-Control": "no-store"})

    unstorable_cache = SharedCache()
    async with _client(unstorable) as client:
        for url, expected in (("/gone", 410), ("/private", 200)):
            for _ in range(2):
                status, _, from_cache = await fetch_through_cache(client, unstorable_cache, url, [])
                assert (status, from_cache) == (expected, False), url
    assert (unstorable_cache.origin_hits, unstorable_cache.entry_count()) == (4, 0)

    star = FastAPI()

    @star.get("/widgets/7")
    async def vary_star() -> JSONResponse:
        return JSONResponse({}, headers={"Cache-Control": "public, max-age=60", "Vary": "*"})

    star_cache = SharedCache()
    async with _client(star) as client:
        for _ in range(2):
            await fetch_through_cache(client, star_cache, "/widgets/7", [])
    # RFC 9111 4.1: a stored response whose Vary contains "*" always fails to match.
    assert (star_cache.origin_hits, star_cache.entry_count()) == (2, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_version_vary_composes_with_gzip_middleware_add_vary_header() -> None:
    app = build_app()
    app.add_middleware(GZipMiddleware, minimum_size=0)
    async with _client(app) as client:
        response = await client.get(
            "/widgets/7", headers={"Accept-Encoding": "gzip", "API-Version": "2"}
        )

        identity = await client.get("/widgets/7", headers={"API-Version": "2"})

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["vary"] == "Accept, API-Version, Accept-Encoding"
    assert response.json() == V2_BODY
    # The middleware adds Accept-Encoding to Vary on the uncompressed variant too.
    assert "content-encoding" not in identity.headers
    assert identity.headers["vary"] == "Accept, API-Version, Accept-Encoding"


ServerKind = Literal["uvicorn-httptools", "uvicorn-h11", "hypercorn-h11", "granian"]


async def _wait_until_listening(port: int) -> None:
    for _ in range(500):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.01)
            continue
        writer.close()
        await writer.wait_closed()
        return
    pytest.fail(f"no server accepted connections on 127.0.0.1:{port} within 5 s")


@asynccontextmanager
async def _live_server(kind: ServerKind, app: FastAPI) -> AsyncGenerator[int]:
    """Serve the app with a real ASGI server on an ephemeral loopback port; yield the port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    if kind == "granian":
        granian = GranianServer(
            app,
            address="127.0.0.1",
            port=port,
            interface=Interfaces.ASGINL,
            websockets=False,
            log_enabled=False,
        )
        task = asyncio.create_task(granian.serve())
        try:
            await _wait_until_listening(port)
            yield port
        finally:
            granian.stop()  # type: ignore[no-untyped-call]  # unannotated in granian 2.8.7
            await asyncio.wait_for(task, timeout=10)
    elif kind == "hypercorn-h11":
        config = hypercorn.config.Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.loglevel = "WARNING"
        stop = asyncio.Event()
        task = asyncio.create_task(
            hypercorn.asyncio.serve(app, config, shutdown_trigger=stop.wait, mode="asgi")  # type: ignore[arg-type]
        )
        try:
            await _wait_until_listening(port)
            yield port
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=10)
    else:
        http_impl: Literal["httptools", "h11"] = (
            "httptools" if kind.endswith("httptools") else "h11"
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                http=http_impl,
                lifespan="off",
                log_level="warning",
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            await _wait_until_listening(port)
            yield port
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


async def _raw_get(
    port: int, target: str, field_lines: list[str]
) -> tuple[int, dict[str, str], bytes]:
    """Send one HTTP/1.1 request as literal bytes so duplicate and mixed-case lines survive."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"GET {target} HTTP/1.1", "Host: test", *field_lines, "Connection: close", "", ""]
    writer.write("\r\n".join(lines).encode("ascii"))
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line, *response_lines = head.decode("ascii").split("\r\n")
    fields = {
        name.lower(): value.strip()
        for name, _, value in (line.partition(":") for line in response_lines)
    }
    return int(status_line.split(" ")[1]), fields, body


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["uvicorn-httptools", "uvicorn-h11", "hypercorn-h11", "granian"])
async def test_real_servers_pass_duplicate_and_mixed_case_version_fields_to_the_app(
    kind: ServerKind,
) -> None:
    async with _live_server(kind, build_app()) as port:
        duplicate = await _raw_get(port, "/widgets/7", ["API-Version: 1", "api-version: 2"])
        upper = await _raw_get(port, "/widgets/7", ["API-VERSION: 2"])
        absent = await _raw_get(port, "/widgets/7", [])
        two_accept_lines = await _raw_get(
            port,
            "/widgets/7",
            ["Accept: application/json;q=0.1", "Accept: Application/VND.ACME.V2+JSON"],
        )
        cloaked = await _raw_get(port, "/widgets/7?api-version=1;api-version=2", [])
        channel = await _raw_get(port, "/v1beta/widgets/7", [])

    # No server merges, drops, or reorders the two field lines: the app sees both and refuses.
    assert (duplicate[0], json.loads(duplicate[2])["error"]) == (400, "ambiguous_version")
    assert (upper[0], json.loads(upper[2])) == (200, V2_BODY)
    assert (absent[0], json.loads(absent[2])) == (200, V1_BODY)
    for negotiated in (duplicate, upper, absent):
        assert negotiated[1]["vary"] == "Accept, API-Version"
    assert two_accept_lines[0] == 200
    assert two_accept_lines[1]["content-type"] == "application/vnd.acme.v2+json"
    assert (cloaked[0], json.loads(cloaked[2])["error"]) == (400, "invalid_version")
    assert (channel[0], "vary" in channel[1]) == (200, False)
    assert json.loads(channel[2]) == {"id": 7, "labels": ["blue"], "name": "sprocket"}


async def _h2_get(
    port: int, target: str, fields: list[tuple[str, str]], *, lowercase_names: bool = True
) -> tuple[int | None, dict[str, str], bytes, int | None]:
    """One HTTP/2 request over cleartext with prior knowledge (RFC 9113 Section 3.3).

    Returns (status, fields, body, error_code); error_code is set only when the peer reset
    the stream or closed the connection instead of answering.
    """
    config = h2.config.H2Configuration(
        client_side=True,
        header_encoding="ascii",
        validate_outbound_headers=lowercase_names,
        normalize_outbound_headers=lowercase_names,
    )
    connection = h2.connection.H2Connection(config)
    connection.initiate_connection()
    pseudo = [(":method", "GET"), (":scheme", "http"), (":authority", "test"), (":path", target)]
    connection.send_headers(1, [*pseudo, *fields], end_stream=True)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(connection.data_to_send())
    await writer.drain()

    status: int | None = None
    response_fields: dict[str, str] = {}
    body = b""
    error_code: int | None = None
    finished = False
    while not finished:
        data = await asyncio.wait_for(reader.read(65535), timeout=5)
        if not data:
            break
        for event in connection.receive_data(data):
            if isinstance(event, h2.events.ResponseReceived):
                for raw_name, raw_value in event.headers or []:
                    name = raw_name if isinstance(raw_name, str) else raw_name.decode("ascii")
                    value = raw_value if isinstance(raw_value, str) else raw_value.decode("ascii")
                    if name == ":status":
                        status = int(value)
                    else:
                        response_fields[name] = value
            elif isinstance(event, h2.events.DataReceived):
                body += event.data or b""
            elif isinstance(event, h2.events.StreamEnded):
                finished = True
            elif isinstance(event, (h2.events.StreamReset, h2.events.ConnectionTerminated)):
                error_code = None if event.error_code is None else int(event.error_code)
                finished = True
        writer.write(connection.data_to_send())
        await writer.drain()
    writer.close()
    await writer.wait_closed()
    return status, response_fields, body, error_code


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hypercorn-h11", "granian"], ids=["hypercorn-h2", "granian-h2"])
async def test_http2_servers_pass_duplicate_fields_and_refuse_uppercase_field_names(
    kind: ServerKind,
) -> None:
    async with _live_server(kind, build_app()) as port:
        pinned = await _h2_get(port, "/widgets/7", [("api-version", "2")])
        # The client library lowercases this name before encoding; the control for `upper`.
        normalized = await _h2_get(port, "/widgets/7", [("API-Version", "2")])
        duplicate = await _h2_get(port, "/widgets/7", [("api-version", "1"), ("api-version", "2")])
        two_accept_lines = await _h2_get(
            port,
            "/widgets/7",
            [("accept", "application/json;q=0.1"), ("accept", "Application/VND.ACME.V2+JSON")],
        )
        channel = await _h2_get(port, "/v1beta/widgets/7", [])
        upper = await _h2_get(port, "/widgets/7", [("API-Version", "2")], lowercase_names=False)

    for answered in (pinned, normalized):
        assert (answered[0], json.loads(answered[2]), answered[3]) == (200, V2_BODY, None)
        assert answered[1]["vary"] == "Accept, API-Version"
    # HTTP/2 has no field lines to merge, yet both fields still reach the app as two entries.
    assert (duplicate[0], json.loads(duplicate[2])["error"]) == (400, "ambiguous_version")
    assert duplicate[1]["cache-control"] == "no-store"
    assert (two_accept_lines[0], two_accept_lines[1]["content-type"]) == (
        200,
        "application/vnd.acme.v2+json",
    )
    assert (channel[0], "vary" in channel[1]) == (200, False)
    # RFC 9113 Section 8.2.1: an uppercase field name makes the request malformed, so the
    # server answers with PROTOCOL_ERROR and the request never reaches the application.
    assert upper == (None, {}, b"", h2.errors.ErrorCodes.PROTOCOL_ERROR)
