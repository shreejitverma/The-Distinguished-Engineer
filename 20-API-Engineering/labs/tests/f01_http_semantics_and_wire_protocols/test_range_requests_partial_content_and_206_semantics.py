"""Byte-range requests, 206 Partial Content, 416, and If-Range (RFC 9110)."""

from __future__ import annotations

import re
from email import message_from_bytes, policy
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.range_requests import (
    ByteRange,
    ContentRange,
    IgnoreRange,
    RangeApp,
    Representation,
    Satisfiable,
    Unsatisfiable,
    evaluate_range,
    if_range_holds,
    parse_content_range,
    render_multipart_byteranges,
    respond,
    resume_download,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

DATA = b"0123456789abcdefghijklmnopqrstuvwxyz"
BOUNDARY = "RANGE_LAB_BOUNDARY"
V1 = Representation(
    data=DATA,
    content_type="text/plain",
    etag='"v1"',
    last_modified="Sun, 08 Nov 2026 10:00:00 GMT",
    date="Sun, 08 Nov 2026 10:00:05 GMT",
)
V2 = Representation(
    data=DATA[::-1],
    content_type="text/plain",
    etag='"v2"',
    last_modified="Sun, 08 Nov 2026 11:00:00 GMT",
    date="Sun, 08 Nov 2026 11:00:05 GMT",
)


def _client(store: dict[str, Representation]) -> httpx.AsyncClient:
    app = RangeApp(store, boundary=BOUNDARY)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _get(headers: dict[str, str] | list[tuple[str, str]]) -> httpx.Response:
    async with _client({"/f": V1}) as client:
        return await client.get("/f", headers=headers)


def _multipart_parts(content_type: str, body: bytes) -> list[tuple[str, str, bytes]]:
    """Parse with the stdlib MIME parser, independent of the code under test."""
    message = message_from_bytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body,
        policy=policy.HTTP,
    )
    assert message.is_multipart()
    parts: list[tuple[str, str, bytes]] = []
    for part in message.iter_parts():
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        parts.append((str(part["Content-Type"]), str(part["Content-Range"]), payload))
    return parts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_int_range_returns_206_with_content_range() -> None:
    resp = await _get({"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-4/36"
    assert resp.headers["content-length"] == "5"
    assert resp.headers["etag"] == '"v1"'
    assert resp.content == b"01234"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "content_range", "body"),
    [
        ("bytes=30-", "bytes 30-35/36", b"uvwxyz"),
        ("bytes=-6", "bytes 30-35/36", b"uvwxyz"),
        ("bytes=30-999", "bytes 30-35/36", b"uvwxyz"),
        ("bytes=-999", "bytes 0-35/36", DATA),
        ("BYTES= 5-9 ,", "bytes 5-9/36", b"56789"),
    ],
)
@pytest.mark.asyncio
async def test_open_ended_suffix_and_overlong_last_pos_clamp_to_end(
    spec: str, content_range: str, body: bytes
) -> None:
    resp = await _get({"Range": spec})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == content_range
    assert resp.content == body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_ranges_produce_multipart_byteranges() -> None:
    resp = await _get({"Range": "bytes=0-2,10-12"})
    assert resp.status_code == 206
    assert resp.headers["content-type"] == f"multipart/byteranges; boundary={BOUNDARY}"
    assert "content-range" not in resp.headers
    assert int(resp.headers["content-length"]) == len(resp.content)
    assert _multipart_parts(resp.headers["content-type"], resp.content) == [
        ("text/plain", "bytes 0-2/36", b"012"),
        ("text/plain", "bytes 10-12/36", b"abc"),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "content_range"),
    [
        ("bytes=0-4,2-8", "bytes 0-8/36"),
        ("bytes=5-9,0-4", "bytes 0-9/36"),
        ("bytes=0-0,0-0,0-0", "bytes 0-0/36"),
    ],
)
@pytest.mark.asyncio
async def test_overlapping_or_adjacent_ranges_coalesce_into_one_part(
    spec: str, content_range: str
) -> None:
    resp = await _get({"Range": spec})
    assert resp.status_code == 206
    assert resp.headers["content-type"] == "text/plain"
    assert resp.headers["content-range"] == content_range


@pytest.mark.unit
@pytest.mark.parametrize("spec", ["bytes=36-", "bytes=100-200", "bytes=-0", "bytes=36-40,-0"])
@pytest.mark.asyncio
async def test_unsatisfiable_range_returns_416_with_complete_length(spec: str) -> None:
    resp = await _get({"Range": spec})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */36"
    assert resp.content == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partially_satisfiable_set_serves_only_satisfiable_ranges() -> None:
    resp = await _get({"Range": "bytes=100-200,0-1"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-1/36"
    assert resp.content == b"01"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ("items=0-1", "unsupported range unit"),
        ("bytes=5-2", "invalid ranges-specifier"),
        ("bytes=abc-def", "invalid ranges-specifier"),
        ("bytes=7", "invalid ranges-specifier"),
        ("bytes=-", "invalid ranges-specifier"),
        ("bytes=-x", "invalid ranges-specifier"),
        ("bytes=,", "invalid ranges-specifier"),
        ("bytes 0-1", "invalid ranges-specifier"),
        (",".join(f"{i}-{i}" for i in range(0, 18, 2)).join(["bytes=", ""]), "too many ranges"),
    ],
)
@pytest.mark.asyncio
async def test_ignored_range_falls_back_to_full_200(spec: str, reason: str) -> None:
    assert evaluate_range(spec, len(DATA)) == IgnoreRange(reason)
    resp = await _get({"Range": spec})
    assert resp.status_code == 200
    assert "content-range" not in resp.headers
    assert resp.content == DATA


@pytest.mark.unit
def test_exactly_max_ranges_is_still_served() -> None:
    eight = "bytes=" + ",".join(f"{i}-{i}" for i in range(0, 16, 2))
    decision = evaluate_range(eight, len(DATA))
    assert isinstance(decision, Satisfiable)
    assert len(decision.ranges) == 8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_range_fields_combine_into_an_invalid_specifier() -> None:
    resp = await _get([("Range", "bytes=0-1"), ("Range", "bytes=5-6")])
    assert resp.status_code == 200
    assert resp.content == DATA


@pytest.mark.unit
def test_range_is_ignored_for_methods_other_than_get_and_for_empty_data() -> None:
    resp = respond("POST", {"range": "bytes=0-1"}, V1, boundary=BOUNDARY)
    assert resp.status == 200
    assert resp.body == DATA
    assert evaluate_range("bytes=-5", 0) == IgnoreRange("zero-length representation")
    with pytest.raises(ValueError, match="non-negative"):
        evaluate_range("bytes=0-1", -1)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("if_range", "status"),
    [('"v1"', 206), ('"v0"', 200), ('W/"v1"', 200), ("Sun, 08 Nov 2026 10:00:00 GMT", 206)],
)
@pytest.mark.asyncio
async def test_if_range_gates_the_range_on_a_strong_validator(if_range: str, status: int) -> None:
    resp = await _get({"Range": "bytes=0-4", "If-Range": if_range})
    assert resp.status_code == status
    assert resp.content == (b"01234" if status == 206 else DATA)


@pytest.mark.unit
def test_if_range_entity_tag_uses_strong_comparison() -> None:
    assert if_range_holds('W/"v1"', etag='W/"v1"', last_modified=None, date=None) is False
    assert if_range_holds('"v1"', etag='W/"v1"', last_modified=None, date=None) is False
    assert if_range_holds('"v1"', etag=None, last_modified=None, date=None) is False
    assert if_range_holds(' "v1" ', etag='"v1"', last_modified=None, date=None) is True


@pytest.mark.unit
def test_if_range_http_date_requires_exact_match_and_strong_date() -> None:
    lm = "Sun, 08 Nov 2026 10:00:00 GMT"
    assert if_range_holds(lm, etag=None, last_modified=lm, date="Sun, 08 Nov 2026 10:00:01 GMT")
    assert not if_range_holds(lm, etag=None, last_modified=lm, date=lm)
    assert not if_range_holds(
        "Sun, 08 Nov 2026 09:00:00 GMT",
        etag=None,
        last_modified=lm,
        date="Sun, 08 Nov 2026 10:00:05 GMT",
    )
    assert not if_range_holds(
        "Sunday, 08-Nov-26 10:00:00 GMT",
        etag=None,
        last_modified=lm,
        date="Sun, 08 Nov 2026 10:00:05 GMT",
    )
    assert not if_range_holds("garbage", etag=None, last_modified="garbage", date="garbage")
    assert not if_range_holds(lm, etag=None, last_modified=None, date=None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_with_if_range_never_splices_two_versions() -> None:
    store = {"/f": V1}
    async with _client(store) as client:
        prefix = (await client.get("/f", headers={"Range": "bytes=0-9"})).content
        same = await resume_download(client, "/f", partial=prefix, etag=V1.etag)
        assert same == type(same)(body=V1.data, etag='"v1"', resumed=True)

        store["/f"] = V2
        safe = await resume_download(client, "/f", partial=prefix, etag=V1.etag)
        assert safe == type(safe)(body=V2.data, etag='"v2"', resumed=False)

        naive = await resume_download(
            client, "/f", partial=prefix, etag=V1.etag, use_if_range=False
        )
        assert naive.resumed is True
        assert naive.body == V1.data[:10] + V2.data[10:]
        assert naive.body not in (V1.data, V2.data)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resume_rejects_416_and_responses_it_cannot_combine() -> None:
    async with _client({"/f": V1}) as client:
        with pytest.raises(ValueError, match="cannot resume: status 416"):
            await resume_download(client, "/f", partial=DATA, etag=V1.etag)
        missing = await client.get("/missing")
        assert missing.status_code == 404

    def lying_app(content_range: str, etag: str) -> RangeApp:
        class Lying(RangeApp):
            async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                headers = [(b"content-range", content_range.encode()), (b"etag", etag.encode())]
                await send({"type": "http.response.start", "status": 206, "headers": headers})
                await send({"type": "http.response.body", "body": DATA[10:]})

        return Lying({}, boundary=BOUNDARY)

    cases = [
        ("bytes 0-35/36", '"v1"', "unexpected Content-Range"),
        ("bytes 10-35/*", '"v1"', "unexpected Content-Range"),
        ("bytes 10-20/36", '"v1"', "unexpected Content-Range"),
        ("bytes 10-35/36", '"v2"', "different strong validator"),
    ]
    for content_range, etag, match in cases:
        transport = httpx.ASGITransport(app=lying_app(content_range, etag))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(ValueError, match=match):
                await resume_download(client, "/f", partial=DATA[:10], etag=V1.etag)


@pytest.mark.unit
def test_parse_content_range_enforces_rfc_9110_validity() -> None:
    assert parse_content_range("bytes 0-9/10") == ContentRange(0, 9, 10)
    assert parse_content_range("bytes 42-1233/*") == ContentRange(42, 1233, None)
    assert parse_content_range("bytes */1234") == ContentRange(None, None, 1234)
    for bad in ("bytes 5-2/10", "bytes 0-10/10"):
        with pytest.raises(ValueError, match="invalid Content-Range"):
            parse_content_range(bad)
    for malformed in ("items 0-1/2", "bytes 0-1", "bytes=0-1/2"):
        with pytest.raises(ValueError, match="malformed Content-Range"):
            parse_content_range(malformed)


@pytest.mark.unit
def test_huge_numerals_are_compared_without_int_conversion() -> None:
    with pytest.raises(ValueError, match="Exceeds the limit"):
        int("9" * 5000)
    assert evaluate_range("bytes=0-" + "9" * 5000, 36) == Satisfiable((ByteRange(0, 35),))
    assert evaluate_range("bytes=" + "9" * 5000 + "-", 36) == Unsatisfiable(36)
    assert evaluate_range("bytes=-" + "9" * 5000, 36) == Satisfiable((ByteRange(0, 35),))
    assert evaluate_range("bytes=" + "9" * 30 + "-" + "9" * 29, 36) == IgnoreRange(
        "invalid ranges-specifier"
    )
    assert evaluate_range("bytes=0005-0009", 36) == Satisfiable((ByteRange(5, 9),))


@pytest.mark.unit
def test_multipart_rendering_rejects_bad_boundaries_and_collisions() -> None:
    ranges = [ByteRange(0, 1), ByteRange(4, 5)]
    for bad in ("", "x" * 71, "has space"):
        with pytest.raises(ValueError, match="bchars"):
            render_multipart_byteranges(DATA, ranges, content_type="text/plain", boundary=bad)
    colliding = b"--abc" + DATA
    with pytest.raises(ValueError, match="boundary occurs"):
        render_multipart_byteranges(
            colliding, [ByteRange(0, 6)], content_type="text/plain", boundary="abc"
        )
    with pytest.raises(ValueError, match="invalid byte range"):
        ByteRange(5, 4)


def _starlette_client(path: Path) -> httpx.AsyncClient:
    async def endpoint(request: Request) -> FileResponse:
        return FileResponse(path, media_type="application/octet-stream")

    app = Starlette(routes=[Route("/file", endpoint)])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def blob(tmp_path: Path) -> Path:
    path = tmp_path / "blob.bin"
    path.write_bytes(bytes(range(256)) * 4)
    return path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_file_response_206_and_416(blob: Path) -> None:
    async with _starlette_client(blob) as client:
        whole = await client.get("/file")
        assert whole.status_code == 200
        assert whole.headers["accept-ranges"] == "bytes"
        part = await client.get("/file", headers={"Range": "bytes=1020-"})
        assert part.status_code == 206
        assert part.headers["content-range"] == "bytes 1020-1023/1024"
        assert part.content == bytes([252, 253, 254, 255])
        bad = await client.get("/file", headers={"Range": "bytes=1024-"})
        assert bad.status_code == 416
        assert bad.headers["content-range"] == "bytes */1024"


@pytest.mark.unit
@pytest.mark.parametrize("spec", ["items=0-1", "bytes=5-2"])
@pytest.mark.asyncio
async def test_starlette_answers_400_where_this_lab_ignores_the_range(
    blob: Path, spec: str
) -> None:
    async with _starlette_client(blob) as client:
        resp = await client.get("/file", headers={"Range": spec})
    assert resp.status_code == 400
    assert evaluate_range(spec, 1024) in (
        IgnoreRange("unsupported range unit"),
        IgnoreRange("invalid ranges-specifier"),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_if_range_matches_its_own_etag_only(blob: Path) -> None:
    async with _starlette_client(blob) as client:
        etag = (await client.get("/file")).headers["etag"]
        hit = await client.get("/file", headers={"Range": "bytes=0-0", "If-Range": etag})
        weak = await client.get("/file", headers={"Range": "bytes=0-0", "If-Range": "W/" + etag})
        stale = await client.get("/file", headers={"Range": "bytes=0-0", "If-Range": '"stale"'})
    assert (hit.status_code, weak.status_code, stale.status_code) == (206, 200, 200)
    assert len(stale.content) == 1024


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_ignores_more_than_100_ranges_and_frames_multipart(blob: Path) -> None:
    def spec(n: int) -> str:
        return "bytes=" + ",".join(f"{4 * i}-{4 * i}" for i in range(n))

    async with _starlette_client(blob) as client:
        hundred = await client.get("/file", headers={"Range": spec(100)})
        over = await client.get("/file", headers={"Range": spec(101)})
    assert over.status_code == 200
    assert len(over.content) == 1024
    assert hundred.status_code == 206
    content_type = hundred.headers["content-type"]
    assert re.fullmatch(r"multipart/byteranges; boundary=[0-9a-f]{26}", content_type)
    assert int(hundred.headers["content-length"]) == len(hundred.content)
    parts = _multipart_parts(content_type, hundred.content)
    assert len(parts) == 100
    assert parts[1] == ("application/octet-stream", "bytes 4-4/1024", bytes([4]))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_gzip_200_and_identity_206_share_one_strong_etag(tmp_path: Path) -> None:
    path = tmp_path / "log.txt"
    path.write_bytes(b"range request line\n" * 600)

    async def endpoint(request: Request) -> FileResponse:
        return FileResponse(path, media_type="text/plain")

    app = Starlette(
        routes=[Route("/file", endpoint)],
        middleware=[Middleware(GZipMiddleware, minimum_size=500)],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        full = await client.get("/file", headers={"Accept-Encoding": "gzip"})
        etag = full.headers["etag"]
        part = await client.get(
            "/file",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=100-", "If-Range": etag},
        )
    assert full.headers["content-encoding"] == "gzip"
    assert int(full.headers["content-length"]) < 600 * 19
    assert part.status_code == 206
    assert "content-encoding" not in part.headers
    assert part.headers["etag"] == etag
    assert part.headers["content-range"] == f"bytes 100-{600 * 19 - 1}/{600 * 19}"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "content_range"),
    [
        ("bytes=0-1_0", "bytes 0-10/1024"),
        ("bytes=0- 10", "bytes 0-10/1024"),
        ("bytes=+0-+5", "bytes 0-5/1024"),
    ],
)
@pytest.mark.asyncio
async def test_starlette_int_parsing_accepts_non_abnf_digits(
    blob: Path, spec: str, content_range: str
) -> None:
    async with _starlette_client(blob) as client:
        resp = await client.get("/file", headers={"Range": spec})
    assert evaluate_range(spec, 1024) == IgnoreRange("invalid ranges-specifier")
    assert resp.status_code == 206
    assert resp.headers["content-range"] == content_range


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_answers_416_when_any_one_range_is_unsatisfiable(blob: Path) -> None:
    async with _starlette_client(blob) as client:
        resp = await client.get("/file", headers={"Range": "bytes=0-1,2000-3000"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */1024"
    assert evaluate_range("bytes=0-1,2000-3000", 1024) == Satisfiable((ByteRange(0, 1),))
