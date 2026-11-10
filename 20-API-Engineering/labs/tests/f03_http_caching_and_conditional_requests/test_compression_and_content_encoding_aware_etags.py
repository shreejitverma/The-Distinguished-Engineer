"""Content-Encoding must change the strong ETag: reproduce the collision, prove the fixes."""

from __future__ import annotations

import gzip
import os
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.datastructures import Headers

from api_labs.f03_http_caching_and_conditional_requests.content_encoding_etags import (
    PATHSEND,
    EncodingAwareGZipMiddleware,
    EntityTag,
    Policy,
    VariantCache,
    accepts_gzip,
    build_static_app,
    gzip_bytes,
    parse_etag_list,
    resume_download,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    EncodingPolicy = Literal["suffix", "weaken"]

BODY = b"".join(f"row {i:04d}: status=ok latency_ms={i % 97}\n".encode() for i in range(400))
MTIME = 1_700_000_000  # fixed mtime -> deterministic Starlette FileResponse ETag
URL = "/static/report.txt"
CUT = 1000  # bytes of the first response kept before the simulated disconnect


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    path = tmp_path / "report.txt"
    path.write_bytes(BODY)
    os.utime(path, (MTIME, MTIME))
    return tmp_path


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def raw_get(client: httpx.AsyncClient, headers: dict[str, str]) -> httpx.Response:
    async with client.stream("GET", URL, headers=headers) as response:
        response.extensions["raw"] = b"".join([c async for c in response.aiter_raw()])
    return response


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_gzip_shares_one_strong_etag_across_two_byte_sequences(
    static_dir: Path,
) -> None:
    async with client_for(build_static_app(static_dir, "starlette")) as client:
        gz = await raw_get(client, {"Accept-Encoding": "gzip"})
        ident = await raw_get(client, {"Accept-Encoding": "identity"})
    assert gz.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in ident.headers
    assert gz.extensions["raw"] != ident.extensions["raw"] == BODY
    assert gzip.decompress(gz.extensions["raw"]) == BODY
    assert gz.headers["etag"] == ident.headers["etag"]
    assert not gz.headers["etag"].startswith("W/")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_range_resume_across_shared_strong_etag_corrupts_the_download(
    static_dir: Path,
) -> None:
    async with client_for(build_static_app(static_dir, "starlette")) as client:
        outcome = await resume_download(client, URL, cut=CUT)
    assert outcome.statuses == (200, 206)  # If-Range matched: identity bytes after gzip bytes
    assert outcome.assembled[:2] == b"\x1f\x8b"  # gzip magic from the first response
    assert outcome.assembled[CUT:] == BODY[CUT:]  # identity tail from the 206
    assert outcome.decoded != BODY
    assert outcome.error is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suffix_policy_makes_if_range_fail_closed_to_a_full_200(static_dir: Path) -> None:
    async with client_for(build_static_app(static_dir, "suffix")) as client:
        outcome = await resume_download(client, URL, cut=CUT)
    assert outcome.first_etag.endswith('-gzip"')
    assert outcome.statuses == (200, 200)
    assert outcome.decoded == BODY
    assert outcome.error is None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("naive_if_range", [False, True])
async def test_weaken_policy_never_yields_a_range_of_gzip_bytes(
    static_dir: Path, naive_if_range: bool
) -> None:
    async with client_for(build_static_app(static_dir, "weaken")) as client:
        outcome = await resume_download(client, URL, cut=CUT, naive_if_range=naive_if_range)
    assert outcome.first_etag.startswith('W/"')
    assert outcome.statuses == (200, 200)
    assert outcome.decoded == BODY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suffix_policy_keeps_304_for_a_stored_gzip_copy(static_dir: Path) -> None:
    async with client_for(build_static_app(static_dir, "suffix")) as client:
        first = await raw_get(client, {"Accept-Encoding": "gzip"})
        etag = first.headers["etag"]
        again = await raw_get(client, {"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert etag.endswith('-gzip"')
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert "accept-encoding" in again.headers["vary"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_suffix_policy_identity_tag_cannot_revalidate_the_gzip_form(
    static_dir: Path,
) -> None:
    async with client_for(build_static_app(static_dir, "suffix")) as client:
        ident = await raw_get(client, {"Accept-Encoding": "identity"})
        etag = ident.headers["etag"]
        again = await raw_get(client, {"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert "accept-encoding" in ident.headers["vary"].lower()
    assert again.status_code == 200
    assert again.headers["content-encoding"] == "gzip"
    assert again.headers["etag"] == etag[:-1] + '-gzip"'


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition",
    [{"If-None-Match": "*"}, {"If-Modified-Since": "Tue, 14 Nov 2023 22:13:20 GMT"}],
)
async def test_suffix_policy_tags_every_gzip_path_304_with_the_gzip_tag(
    static_dir: Path, condition: dict[str, str]
) -> None:
    async with client_for(build_static_app(static_dir, "suffix")) as client:
        first = await raw_get(client, {"Accept-Encoding": "gzip"})
        again = await raw_get(client, {"Accept-Encoding": "gzip", **condition})
    assert first.headers["etag"].endswith('-gzip"')
    assert again.status_code == 304
    assert again.headers["etag"] == first.headers["etag"]  # RFC 9110 Section 15.4.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weaken_policy_revalidates_through_weak_comparison(static_dir: Path) -> None:
    async with client_for(build_static_app(static_dir, "weaken")) as client:
        first = await raw_get(client, {"Accept-Encoding": "gzip"})
        etag = first.headers["etag"]
        again = await raw_get(client, {"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert etag.startswith('W/"')
    assert again.status_code == 304
    assert again.headers["etag"] == etag


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rfc2616_cache_hands_gzip_bytes_to_identity_client_under_shared_etag(
    static_dir: Path,
) -> None:
    async with client_for(build_static_app(static_dir, "starlette")) as client:
        cache = VariantCache(client, URL, mode="rfc2616")
        await cache.get("gzip")
        served = await cache.get("identity")
    assert served.headers["content-encoding"] == "gzip"  # identity client gets gzip bytes
    assert served.body != BODY
    assert cache.origin_requests == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rfc9111_cache_refuses_cross_variant_update_and_refetches(
    static_dir: Path,
) -> None:
    async with client_for(build_static_app(static_dir, "starlette")) as client:
        cache = VariantCache(client, URL, mode="rfc9111")
        await cache.get("gzip")
        served = await cache.get("identity")
    assert "content-encoding" not in served.headers
    assert served.body == BODY
    assert cache.origin_requests == 3  # wasted 304, then the full identity fetch


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "origin_requests"), [("suffix", 2), ("weaken", 3)])
async def test_rfc2616_cache_is_safe_when_the_etag_changes_with_the_coding(
    static_dir: Path, policy: Policy, origin_requests: int
) -> None:
    async with client_for(build_static_app(static_dir, policy)) as client:
        cache = VariantCache(client, URL, mode="rfc2616")
        await cache.get("gzip")
        served = await cache.get("identity")
    assert "content-encoding" not in served.headers
    assert served.body == BODY
    assert cache.origin_requests == origin_requests


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("gzip", True),
        ("GZIP; Q=0.5", True),
        ("x-gzip", True),
        ("br, gzip;q=0.001", True),
        ("*", True),
        ("gzip;q=0", False),
        ("gzip;q=0, *", False),
        ("*;q=0", False),
        ("identity", False),
        ("", False),
        (None, False),
    ],
)
def test_accepts_gzip_honours_qvalues(header: str | None, expected: bool) -> None:
    assert accepts_gzip(header) is expected


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "encoded"), [("starlette", True), ("suffix", False)])
async def test_gzip_q0_refusal_is_ignored_by_substring_matching(
    static_dir: Path, policy: Policy, encoded: bool
) -> None:
    async with client_for(build_static_app(static_dir, policy)) as client:
        response = await raw_get(client, {"Accept-Encoding": "gzip;q=0"})
    assert (response.headers.get("content-encoding") == "gzip") is encoded


@pytest.mark.unit
def test_gzip_bytes_are_deterministic_per_level_but_differ_across_levels() -> None:
    assert gzip_bytes(BODY, 6) == gzip_bytes(BODY, 6)
    assert gzip_bytes(BODY, 6)[4:8] == bytes(4)  # gzip header MTIME is pinned to 0, not the clock
    assert gzip_bytes(BODY, 1) != gzip_bytes(BODY, 9)
    assert gzip.decompress(gzip_bytes(BODY, 1)) == gzip.decompress(gzip_bytes(BODY, 9)) == BODY


@pytest.mark.unit
def test_parse_etag_list_keeps_commas_inside_quotes_and_rejects_garbage() -> None:
    assert parse_etag_list('"a,b", W/"c"') == [EntityTag("a,b"), EntityTag("c", weak=True)]
    assert str(EntityTag("c", weak=True)) == 'W/"c"'
    assert parse_etag_list('"a", junk') == []


def chunked_origin(parts: list[bytes], extra_headers: list[tuple[bytes, bytes]]) -> ASGIApp:
    """A raw ASGI origin that streams `parts` as separate body messages under a strong ETag."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        assert scope["type"] == "http"
        headers = [(b"content-type", b"text/plain"), (b"etag", b'"origin"'), *extra_headers]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for index, part in enumerate(parts):
            more = index < len(parts) - 1
            await send({"type": "http.response.body", "body": part, "more_body": more})

    return app


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["suffix", "weaken"])
async def test_compressed_200_declares_the_length_of_the_gzip_body(
    static_dir: Path, policy: Policy
) -> None:
    async with client_for(build_static_app(static_dir, policy)) as client:
        response = await raw_get(client, {"Accept-Encoding": "gzip"})
    raw = response.extensions["raw"]
    assert response.headers["content-encoding"] == "gzip"
    assert int(response.headers["content-length"]) == len(raw) != len(BODY)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["suffix", "weaken"])
async def test_multi_chunk_origin_body_is_compressed_whole(policy: EncodingPolicy) -> None:
    parts = [BODY[:5000], BODY[5000:9000], BODY[9000:]]
    app = EncodingAwareGZipMiddleware(chunked_origin(parts, []), policy=policy)
    async with client_for(app) as client:
        response = await raw_get(client, {"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(response.extensions["raw"]) == BODY


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["suffix", "weaken"])
async def test_origin_response_that_is_already_encoded_passes_through_untouched(
    policy: EncodingPolicy,
) -> None:
    pre = gzip_bytes(BODY, 9)
    origin = chunked_origin([pre], [(b"content-encoding", b"gzip")])
    app = EncodingAwareGZipMiddleware(origin, policy=policy)
    async with client_for(app) as client:
        response = await raw_get(client, {"Accept-Encoding": "gzip"})
    assert response.extensions["raw"] == pre  # not compressed a second time
    assert response.headers["etag"] == '"origin"'  # the origin owns the tag of its own coding


class WeakTagOrigin(httpx.AsyncBaseTransport):
    """Answers every request with a 2000-byte body under a weak ETag and records the requests."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, headers={"ETag": 'W/"t"'}, stream=httpx.ByteStream(b"x" * 2000))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("naive_if_range", "resumes"),
    [(False, False), (True, True)],
)
async def test_compliant_client_never_sends_a_weak_if_range(
    naive_if_range: bool, resumes: bool
) -> None:
    transport = WeakTagOrigin()
    async with httpx.AsyncClient(transport=transport) as client:
        await resume_download(client, "http://test/f", cut=10, naive_if_range=naive_if_range)
    assert len(transport.seen) == 2
    assert ("if-range" in transport.seen[1].headers) is resumes
    assert ("range" in transport.seen[1].headers) is resumes


async def call_with_pathsend(app: ASGIApp, accept_encoding: str) -> list[Message]:
    """Drive `app` as a server that advertises `http.response.pathsend` (Granian does)."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"accept-encoding", accept_encoding.encode())],
        "extensions": {PATHSEND: {}},
    }
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["suffix", "weaken"])
async def test_gzip_path_withdraws_pathsend_so_the_body_is_compressed(
    static_dir: Path, policy: EncodingPolicy
) -> None:
    sent = await call_with_pathsend(build_static_app(static_dir, policy), "gzip")
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    headers = Headers(raw=sent[0]["headers"])
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(sent[1]["body"]) == BODY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identity_path_keeps_pathsend_and_starlette_gzip_skips_it(static_dir: Path) -> None:
    ours = await call_with_pathsend(build_static_app(static_dir, "suffix"), "identity")
    theirs = await call_with_pathsend(build_static_app(static_dir, "starlette"), "gzip")
    for sent in (ours, theirs):
        assert [m["type"] for m in sent] == ["http.response.start", PATHSEND]
        assert "content-encoding" not in Headers(raw=sent[0]["headers"])
