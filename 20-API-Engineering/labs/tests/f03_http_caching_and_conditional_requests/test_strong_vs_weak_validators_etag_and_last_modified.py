"""Lab: strong vs weak validators, ETag grammar, and Last-Modified granularity (RFC 9110)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from api_labs.f03_http_caching_and_conditional_requests.validator_strength import (
    COMPARISON_FOR_FIELD,
    Document,
    EntityTag,
    build_document_app,
    format_http_date,
    if_match_holds,
    if_none_match_holds,
    if_range_holds,
    last_modified_for_response,
    last_modified_is_strong,
    parse_entity_tag,
    parse_entity_tag_list,
    parse_http_date,
    strong_compare,
    strong_etag,
    weak_compare,
    weak_json_etag,
)

if TYPE_CHECKING:
    from pathlib import Path

T0 = datetime(2026, 11, 9, 12, 0, 0, 200_000, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- RFC 9110 Section 8.8.3.2, Table 3 -------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("left", "right", "strong", "weak"),
    [
        ('W/"1"', 'W/"1"', False, True),
        ('W/"1"', 'W/"2"', False, False),
        ('W/"1"', '"1"', False, True),
        ('"1"', '"1"', True, True),
    ],
    ids=["weak-weak-same", "weak-weak-diff", "weak-strong-same", "strong-strong-same"],
)
def test_comparison_functions_reproduce_rfc_9110_table_3(
    left: str, right: str, strong: bool, weak: bool
) -> None:
    a, b = parse_entity_tag(left), parse_entity_tag(right)
    assert strong_compare(a, b) is strong
    assert weak_compare(a, b) is weak


@pytest.mark.unit
def test_each_precondition_field_uses_its_rfc_comparison() -> None:
    assert COMPARISON_FOR_FIELD == {
        "if-match": "strong",
        "if-none-match": "weak",
        "if-range": "strong",
    }


# --- Grammar (RFC 9110 Section 8.8.3) --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"xyzzy"', EntityTag("xyzzy")),
        ('W/"xyzzy"', EntityTag("xyzzy", weak=True)),
        ('""', EntityTag("")),
        ('  "a,b"\t', EntityTag("a,b")),
    ],
    ids=["strong", "weak", "empty-opaque", "comma-is-etagc"],
)
def test_parse_entity_tag_accepts_the_grammar(raw: str, expected: EntityTag) -> None:
    tag = parse_entity_tag(raw)
    assert tag == expected
    assert str(tag) == raw.strip()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('w/"xyzzy"', "entity-tag must start with W/ or DQUOTE"),
        ("xyzzy", "entity-tag must start with W/ or DQUOTE"),
        ('"a b"', "non-etagc character"),
        ('"a"b"', "trailing data after entity-tag"),
        ('"unterminated', "unterminated opaque-tag"),
        ('W/ "x"', "entity-tag must start with W/ or DQUOTE"),
    ],
    ids=["lowercase-w", "unquoted", "space", "inner-dquote", "unterminated", "space-after-W"],
)
def test_parse_entity_tag_rejects_non_conforming_values(raw: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_entity_tag(raw)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("opaque", "accepted"),
    [
        ("!#~", True),
        ("caf\xe9", True),
        ("\xff", True),
        ("\u0100", False),
        ('a"b', False),
        ("a b", False),
        ("a\x7fb", False),
    ],
    ids=["ascii-bounds", "obs-text", "obs-text-top", "above-obs-text", "dquote", "sp", "del"],
)
def test_entity_tag_constructor_enforces_etagc(opaque: str, accepted: bool) -> None:
    if accepted:
        assert EntityTag(opaque).opaque == opaque
    else:
        with pytest.raises(ValueError, match="non-etagc character"):
            EntityTag(opaque)


@pytest.mark.unit
def test_list_parser_keeps_commas_inside_opaque_tags() -> None:
    assert parse_entity_tag_list('"a,b", W/"c",, "d"') == (
        EntityTag("a,b"),
        EntityTag("c", weak=True),
        EntityTag("d"),
    )


@pytest.mark.unit
def test_list_parser_rejects_wildcard_mixed_with_tags() -> None:
    assert parse_entity_tag_list(" * ") == "*"
    with pytest.raises(ValueError, match="'\\*' cannot be combined"):
        parse_entity_tag_list('"a", *')


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty entity-tag list"),
        (" , ,", "empty entity-tag list"),
        ('"a" "b"', "expected ','"),
        ('"a"x', "expected ','"),
    ],
    ids=["empty", "only-empty-elements", "missing-comma", "garbage-after-tag"],
)
def test_list_parser_rejects_empty_and_malformed_lists(raw: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_entity_tag_list(raw)


@pytest.mark.unit
def test_starlette_naive_split_misses_an_etag_that_contains_a_comma(tmp_path: Path) -> None:
    static = StaticFiles(directory=tmp_path)
    response_headers = Headers({"etag": '"a,b"'})
    request_headers = Headers({"if-none-match": '"a,b"'})
    # starlette 1.10.0 StaticFiles.is_not_modified splits the field on ","
    assert static.is_not_modified(response_headers, request_headers) is False
    assert if_none_match_holds('"a,b"', EntityTag("a,b")) is False


# --- Preconditions: If-Match strong, If-None-Match weak ---------------------------------


@pytest.mark.unit
def test_if_match_rejects_a_weak_tag_even_when_opaque_matches() -> None:
    weak_current = EntityTag("v7", weak=True)
    assert if_match_holds('W/"v7"', weak_current) is False
    assert if_match_holds('"v7"', weak_current) is False
    assert if_match_holds('"v7"', EntityTag("v7")) is True
    assert if_match_holds("*", weak_current) is True
    assert if_match_holds("*", None) is False


@pytest.mark.unit
def test_if_none_match_matches_weak_and_strong_forms() -> None:
    current = EntityTag("v7")
    assert if_none_match_holds('W/"v7"', current) is False
    assert if_none_match_holds('"v6", "v7"', current) is False
    assert if_none_match_holds('W/"v6"', current) is True
    assert if_none_match_holds("*", None) is True


# --- Generation: strong over bytes, weak over semantics ---------------------------------


@pytest.mark.unit
def test_strong_tag_tracks_bytes_and_weak_tag_tracks_semantics() -> None:
    one = {"id": 1, "name": "ada"}
    reordered = {"name": "ada", "id": 1}
    assert strong_etag(b'{"id":1,"name":"ada"}') != strong_etag(b'{"name":"ada","id":1}')
    assert weak_json_etag(one) == weak_json_etag(reordered)
    assert weak_json_etag(one).weak is True
    assert weak_json_etag(one) != weak_json_etag({"id": 2, "name": "ada"})
    assert len(str(strong_etag(b""))) == 34


# --- Last-Modified granularity (RFC 9110 Sections 5.6.7, 8.8.2) -------------------------


@pytest.mark.unit
def test_two_writes_in_one_second_share_last_modified_but_not_strong_etag() -> None:
    first, second = T0, T0 + timedelta(milliseconds=600)
    assert format_http_date(first) == format_http_date(second) == "Mon, 09 Nov 2026 12:00:00 GMT"
    assert len(format_http_date(first)) == 29
    assert strong_etag(b"version-a") != strong_etag(b"version-b")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("date_offset", "strong"),
    [
        (timedelta(0), False),
        (timedelta(milliseconds=799), False),
        (timedelta(milliseconds=800), True),
        (timedelta(seconds=1), True),
    ],
    ids=["date-equals-lm", "same-wire-second", "next-wire-second", "one-second-later"],
)
def test_last_modified_strength_needs_date_one_second_later(
    date_offset: timedelta, strong: bool
) -> None:
    # T0 is 12:00:00.200, so +800 ms puts Date on the wire as 12:00:01.
    assert last_modified_is_strong(T0, T0 + date_offset) is strong


@pytest.mark.unit
def test_future_last_modified_is_clamped_to_date() -> None:
    date = T0
    assert last_modified_for_response(T0 + timedelta(hours=1), date) == date
    assert last_modified_for_response(T0 - timedelta(hours=1), date) == T0 - timedelta(hours=1)


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    ["Sun, 06 Nov 1994 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT", "Sun Nov  6 08:49:37 1994"],
    ids=["imf-fixdate", "rfc850", "asctime"],
)
def test_parse_http_date_accepts_all_three_formats(raw: str) -> None:
    assert parse_http_date(raw) == datetime(1994, 11, 6, 8, 49, 37, tzinfo=UTC)


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["garbage", "", "Sun, 06 Nov 1994 08:49:37 +0500"])
def test_parse_http_date_rejects_invalid_and_non_gmt(raw: str) -> None:
    assert parse_http_date(raw) is None


# --- If-Range (RFC 9110 Section 13.1.5) --------------------------------------------------


@pytest.mark.unit
def test_if_range_never_passes_with_a_weak_tag() -> None:
    assert if_range_holds('W/"v7"', EntityTag("v7", weak=True), None, date_is_strong=True) is False
    assert if_range_holds('W/"v7"', EntityTag("v7"), None, date_is_strong=True) is False
    assert if_range_holds('"v7"', EntityTag("v7"), None, date_is_strong=True) is True


@pytest.mark.unit
def test_if_range_date_needs_strength_and_exact_match() -> None:
    lm = T0
    wire = format_http_date(lm)
    assert if_range_holds(wire, None, lm, date_is_strong=True) is True
    assert if_range_holds(wire, None, lm, date_is_strong=False) is False
    later = format_http_date(lm + timedelta(seconds=5))
    # If-Range is exact match, not the "earlier than or equal" of If-Unmodified-Since.
    assert if_range_holds(later, None, lm, date_is_strong=True) is False
    earlier = format_http_date(lm - timedelta(seconds=5))
    assert if_range_holds(earlier, None, lm, date_is_strong=True) is False


# --- In-process ASGI resource -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weak_tag_revalidates_with_304_but_cannot_guard_a_put() -> None:
    clock = FakeClock(T0)
    document = Document(body={"id": 1, "name": "ada"}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=True, clock=clock)) as client:
        first = await client.get("/doc")
        tag = first.headers["etag"]
        assert tag.startswith('W/"')
        revalidated = await client.get("/doc", headers={"if-none-match": tag})
        assert revalidated.status_code == 304
        assert revalidated.content == b""
        write = await client.put("/doc", json={"id": 1, "name": "grace"}, headers={"if-match": tag})
        assert write.status_code == 412
        assert document.body == {"id": 1, "name": "ada"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strong_tag_guards_a_put_and_rejects_the_stale_writer() -> None:
    clock = FakeClock(T0)
    document = Document(body={"id": 1, "name": "ada"}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        tag = (await client.get("/doc")).headers["etag"]
        clock.now = T0 + timedelta(seconds=3)
        ok = await client.put("/doc", json={"id": 1, "name": "grace"}, headers={"if-match": tag})
        assert ok.status_code == 204
        assert ok.headers["etag"] != tag
        stale = await client.put("/doc", json={"id": 1, "name": "eve"}, headers={"if-match": tag})
        assert stale.status_code == 412
        assert document.body == {"id": 1, "name": "grace"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_reorder_changes_strong_tag_but_keeps_weak_tag() -> None:
    clock = FakeClock(T0)
    for weak_tags, expect_304 in [(True, True), (False, False)]:
        document = Document(body={"id": 1, "name": "ada"}, modified_at=T0)
        app = build_document_app(document, weak_tags=weak_tags, clock=clock)
        async with client_for(app) as client:
            tag = (await client.get("/doc")).headers["etag"]
            put = await client.put("/doc", json={"name": "ada", "id": 1})
            assert put.status_code == 204
            again = await client.get("/doc", headers={"if-none-match": tag})
            assert (again.status_code == 304) is expect_304


@pytest.mark.unit
@pytest.mark.asyncio
async def test_range_with_weak_if_range_returns_full_200() -> None:
    clock = FakeClock(T0 + timedelta(seconds=10))
    document = Document(body={"id": 1, "name": "ada"}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=True, clock=clock)) as client:
        full = await client.get("/doc")
        tag = full.headers["etag"]
        partial = await client.get("/doc", headers={"range": "bytes=0-4", "if-range": tag})
        assert partial.status_code == 200
        assert partial.content == full.content
        assert "content-range" not in partial.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_range_with_strong_if_range_returns_206() -> None:
    clock = FakeClock(T0 + timedelta(seconds=10))
    document = Document(body={"id": 1, "name": "ada"}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        full = await client.get("/doc")
        partial = await client.get(
            "/doc", headers={"range": "bytes=0-4", "if-range": full.headers["etag"]}
        )
        assert partial.status_code == 206
        assert partial.content == full.content[:5]
        assert partial.headers["content-range"] == f"bytes 0-4/{len(full.content)}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_refuses_if_range_date_after_two_writes_in_one_second() -> None:
    clock = FakeClock(T0)
    document = Document(body={"v": "aaaa"}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        lm = (await client.get("/doc")).headers["last-modified"]
        clock.now = T0 + timedelta(milliseconds=500)
        assert (await client.put("/doc", json={"v": "bbbb"})).status_code == 204
        clock.now = T0 + timedelta(seconds=10)
        resumed = await client.get("/doc", headers={"range": "bytes=0-3", "if-range": lm})
        assert resumed.headers["last-modified"] == lm
        assert resumed.status_code == 200
        assert resumed.content == b'{"v":"bbbb"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_304_and_412_carry_the_current_validators_and_date() -> None:
    clock = FakeClock(T0 + timedelta(seconds=10))
    document = Document(body={"id": 1}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        first = await client.get("/doc")
        assert first.headers["date"] == "Mon, 09 Nov 2026 12:00:10 GMT"
        not_modified = await client.get("/doc", headers={"if-none-match": first.headers["etag"]})
        refused = await client.put("/doc", json={"id": 2}, headers={"if-match": '"stale"'})
    assert not_modified.status_code == 304
    assert not_modified.headers["etag"] == first.headers["etag"]
    assert not_modified.headers["last-modified"] == first.headers["last-modified"]
    assert not_modified.headers["date"] == first.headers["date"]
    assert refused.status_code == 412
    assert refused.headers["etag"] == first.headers["etag"]
    assert document.body == {"id": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_clamps_a_future_modification_time_to_the_date_header() -> None:
    clock = FakeClock(T0)
    document = Document(body={"id": 1}, modified_at=T0 + timedelta(hours=1))
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        response = await client.get("/doc")
    assert response.headers["last-modified"] == response.headers["date"]
    assert response.headers["last-modified"] == "Mon, 09 Nov 2026 12:00:00 GMT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_range_without_if_range_is_served_and_the_end_is_clamped() -> None:
    clock = FakeClock(T0 + timedelta(seconds=10))
    document = Document(body={"id": 1, "name": "ada"}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        full = await client.get("/doc")
        unconditional = await client.get("/doc", headers={"range": "bytes=2-4"})
        past_end = await client.get("/doc", headers={"range": "bytes=2-9999"})
    assert unconditional.status_code == 206
    assert unconditional.content == full.content[2:5]
    assert unconditional.headers["content-range"] == f"bytes 2-4/{len(full.content)}"
    assert past_end.status_code == 206
    assert past_end.content == full.content[2:]
    assert (
        past_end.headers["content-range"] == f"bytes 2-{len(full.content) - 1}/{len(full.content)}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_rejects_a_non_object_body_without_changing_the_document() -> None:
    clock = FakeClock(T0)
    document = Document(body={"id": 1}, modified_at=T0)
    async with client_for(build_document_app(document, weak_tags=False, clock=clock)) as client:
        response = await client.put("/doc", json=[1, 2, 3])
    assert response.status_code == 422
    assert document.body == {"id": 1}


# --- Characterization of starlette 1.10.0 FileResponse and StaticFiles ------------------


def _write(path: Path, data: bytes, mtime_ns: int) -> None:
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))


MTIME_NS = 1_793_000_000 * 1_000_000_000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_if_range_date_splices_bytes_from_a_same_second_rewrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "blob.bin"
    _write(target, b"AAAAAAAA", MTIME_NS + 100_000_000)
    app = Starlette(routes=[Mount("/static", StaticFiles(directory=tmp_path))])
    async with client_for(app) as client:
        first = await client.get("/static/blob.bin")
        _write(target, b"BBBBBBBB", MTIME_NS + 700_000_000)
        resumed = await client.get(
            "/static/blob.bin",
            headers={"range": "bytes=4-7", "if-range": first.headers["last-modified"]},
        )
        by_etag = await client.get(
            "/static/blob.bin",
            headers={"range": "bytes=4-7", "if-range": first.headers["etag"]},
        )
    assert resumed.headers["last-modified"] == first.headers["last-modified"]
    assert resumed.headers["etag"] != first.headers["etag"]
    assert resumed.status_code == 206
    assert first.content[:4] + resumed.content == b"AAAABBBB"
    assert by_etag.status_code == 200
    assert by_etag.content == b"BBBBBBBB"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_if_range_weak_tag_ignores_range(tmp_path: Path) -> None:
    _write(tmp_path / "blob.bin", b"AAAAAAAA", MTIME_NS)
    app = Starlette(routes=[Mount("/static", StaticFiles(directory=tmp_path))])
    async with client_for(app) as client:
        etag = (await client.get("/static/blob.bin")).headers["etag"]
        strong = await client.get(
            "/static/blob.bin", headers={"range": "bytes=0-1", "if-range": etag}
        )
        weak = await client.get(
            "/static/blob.bin", headers={"range": "bytes=0-1", "if-range": f"W/{etag}"}
        )
    assert strong.status_code == 206
    assert weak.status_code == 200
    assert weak.content == b"AAAAAAAA"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mtime_delta_ns", "status"),
    [(0, 304), (1, 304), (100, 304), (1_000, 200)],
    ids=["mtime-restored", "plus-1ns", "plus-100ns", "plus-1us"],
)
async def test_starlette_metadata_etag_returns_304_for_changed_bytes_of_equal_size(
    tmp_path: Path, mtime_delta_ns: int, status: int
) -> None:
    target = tmp_path / "config.json"
    _write(target, b'{"flag":true}', MTIME_NS)
    app = Starlette(routes=[Mount("/static", StaticFiles(directory=tmp_path))])
    async with client_for(app) as client:
        first = await client.get("/static/config.json")
        # Same size; the ETag hashes str(st_mtime), a float whose ulp near 1.79e9 s is 238 ns.
        _write(target, b'{"flag":fals}', MTIME_NS + mtime_delta_ns)
        weak_form = f"W/{first.headers['etag']}"
        again = await client.get("/static/config.json", headers={"if-none-match": weak_form})
    assert again.status_code == status
    assert strong_etag(target.read_bytes()) != strong_etag(first.content)
