"""Unit tests for RFC 9110 proactive negotiation, media type parameters, and charsets."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from litestar.datastructures.headers import Accept
from pydantic import BaseModel
from starlette.responses import Response

from api_labs.f01_http_semantics_and_wire_protocols.content_negotiation import (
    MAX_ACCEPT_MEMBERS,
    MediaRange,
    MediaType,
    UnsupportedContentError,
    app,
    decode_text,
    negotiate,
    parse_accept,
    parse_media_type,
    parse_qvalue,
    quality,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette


def _client(asgi: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://test")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "weight"),
    [("1", 1000), ("1.", 1000), ("1.000", 1000), ("0", 0), ("0.5", 500), ("0.001", 1)],
)
def test_qvalue_is_exact_thousandths(text: str, weight: int) -> None:
    assert parse_qvalue(text) == weight


@pytest.mark.unit
@pytest.mark.parametrize("text", ["1.5", "1.001", "0.0001", ".5", "2", "nan", "inf", "1e0", "-0"])
def test_qvalue_outside_the_grammar_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="invalid qvalue"):
        parse_qvalue(text)


@pytest.mark.unit
def test_rfc9110_table5_with_verified_erratum_7138() -> None:
    ranges = parse_accept(
        "text/*;q=0.3, text/plain;q=0.7, text/plain;format=flowed, "
        "text/plain;format=fixed;q=0.4, */*;q=0.5"
    )
    expected = {
        "text/plain;format=flowed": 1000,
        "text/plain": 700,
        "text/html": 300,
        "image/jpeg": 500,
        "text/plain;format=fixed": 400,
        "text/html;level=3": 300,  # the published table says 0.7; erratum 7138 corrects it
    }
    got = {offer: quality(parse_media_type(offer), ranges) for offer in expected}
    assert got == expected


@pytest.mark.unit
def test_specificity_orders_ranges_as_rfc9110_section_12_5_1() -> None:
    ranges = parse_accept("text/*, text/plain, text/plain;format=flowed, */*")
    ordered = sorted(ranges, key=lambda r: r.specificity, reverse=True)
    assert [str(r.media) for r in ordered] == [
        "text/plain;format=flowed",
        "text/plain",
        "text/*",
        "*/*",
    ]


@pytest.mark.unit
def test_q_zero_on_a_specific_type_beats_a_wildcard() -> None:
    assert negotiate("text/html;q=0, */*", ["text/html"]) is None
    assert negotiate("text/html;q=0, */*", ["text/html", "application/json"]) == "application/json"


@pytest.mark.unit
def test_litestar_2_25_0_best_match_serves_an_explicitly_refused_type() -> None:
    # Litestar sorts ranges by (quality, specificity), so */* is tried before text/html;q=0,
    # and best_match never checks the weight, so a q=0 range alone still selects its type.
    accept = Accept("text/html;q=0, */*")
    assert list(accept) == ["*/*", "text/html;q=0"]
    assert accept.best_match(["text/html"]) == "text/html"
    assert Accept("text/html;q=0").best_match(["text/html"]) == "text/html"


@pytest.mark.unit
def test_absent_accept_is_no_preference_but_empty_accept_accepts_nothing() -> None:
    offers = ["application/json", "text/csv"]
    assert negotiate(None, offers) == "application/json"
    assert negotiate("", offers) is None


@pytest.mark.unit
def test_equal_weights_resolve_in_server_preference_order() -> None:
    assert negotiate("text/csv, application/json", ["application/json", "text/csv"]) == (
        "application/json"
    )
    assert negotiate("text/csv, application/json;q=0.9", ["application/json", "text/csv"]) == (
        "text/csv"
    )


@pytest.mark.unit
def test_q_is_the_weight_in_any_position_and_any_case() -> None:
    (r,) = parse_accept("text/plain;Q=0.25;format=flowed")
    assert r == MediaRange(MediaType("text", "plain", (("format", "flowed"),)), 250)


@pytest.mark.unit
def test_rfc9110_section_8_3_1_equivalent_forms_parse_equal() -> None:
    forms = [
        "text/html;charset=utf-8",
        'Text/HTML;Charset="utf-8"',
        'text/html; charset="utf-8"',
        "text/html;charset=UTF-8",
    ]
    parsed = {parse_media_type(f) for f in forms}
    assert parsed == {MediaType("text", "html", (("charset", "utf-8"),))}


@pytest.mark.unit
def test_non_charset_parameter_values_keep_their_case() -> None:
    assert parse_media_type("text/plain;format=Flowed").param("format") == "Flowed"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("text/plain; charset = utf-8", "no '='"),
        ("text/plain;charset =utf-8", "no '='"),
        ("text/plain;charset= utf-8", "expected token"),
        ("text/plain;charset", "no '='"),
        ("text/plain;charset=", "expected token"),
        ("text", "missing '/'"),
    ],
)
def test_malformed_parameters_are_rejected(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_media_type(text)


@pytest.mark.unit
def test_quoted_string_commas_and_quoted_pairs() -> None:
    ranges = parse_accept('text/plain;x="a,b\\"c", application/json')
    assert [r.media.essence for r in ranges] == ["text/plain", "application/json"]
    assert ranges[0].media.param("x") == 'a,b"c'
    assert str(ranges[0].media) == 'text/plain;x="a,b\\"c"'
    assert parse_media_type(str(ranges[0].media)) == ranges[0].media


@pytest.mark.unit
@pytest.mark.parametrize("text", ['text/plain;x="a\\\x01"', 'text/plain;x="a\\\x7f"'])
def test_quoted_pair_rejects_control_octets(text: str) -> None:
    # quoted-pair = "\" ( HTAB / SP / VCHAR / obs-text ) (RFC 9110 Section 5.6.4).
    with pytest.raises(ValueError, match="invalid character"):
        parse_media_type(text)


@pytest.mark.unit
@pytest.mark.parametrize("text", ["*/json", "te*t/*", "text/js*"])
def test_illegal_media_ranges_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="illegal wildcard"):
        parse_accept(text)


@pytest.mark.unit
def test_wildcards_are_not_media_types() -> None:
    with pytest.raises(ValueError, match="illegal wildcard"):
        parse_media_type("text/*")


@pytest.mark.unit
def test_empty_list_elements_are_ignored_and_member_count_is_bounded() -> None:
    assert [r.media.essence for r in parse_accept(" ,text/html,, \t,")] == ["text/html"]
    assert len(parse_accept(",".join(["a/b"] * MAX_ACCEPT_MEMBERS))) == MAX_ACCEPT_MEMBERS
    with pytest.raises(ValueError, match="limit is 64"):
        parse_accept(",".join(["a/b"] * (MAX_ACCEPT_MEMBERS + 1)))


@pytest.mark.unit
def test_json_charset_rules() -> None:
    assert decode_text(parse_media_type("application/problem+json"), b'{"a":1}') == '{"a":1}'
    assert decode_text(parse_media_type("application/json;charset=UTF-8"), b"[]") == "[]"
    with pytest.raises(UnsupportedContentError, match="must be UTF-8"):
        decode_text(parse_media_type("application/json;charset=utf-16"), b"[]")


@pytest.mark.unit
def test_text_plain_charset_default_is_us_ascii() -> None:
    latin1 = parse_media_type("text/plain;charset=ISO-8859-1")
    assert decode_text(latin1, b"caf\xe9") == "caf\u00e9"
    with pytest.raises(UnicodeDecodeError):
        decode_text(parse_media_type("text/plain"), b"caf\xe9")
    with pytest.raises(UnicodeDecodeError):  # valid UTF-8 is still not US-ASCII
        decode_text(parse_media_type("text/plain"), "caf\u00e9".encode())
    assert decode_text(parse_media_type("text/plain;charset=utf-8"), "caf\u00e9".encode()) == (
        "caf\u00e9"
    )
    with pytest.raises(UnsupportedContentError, match="unknown charset"):
        decode_text(parse_media_type("text/plain;charset=x-bogus"), b"x")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_report_negotiates_and_always_sends_vary() -> None:
    async with _client(app) as client:
        csv = await client.get("/report", headers={"accept": "text/csv"})
        html = await client.get("/report", headers={"accept": "text/*;q=0.5, text/html"})
        default = await client.get("/report")
    assert (csv.status_code, csv.headers["content-type"]) == (200, "text/csv; charset=utf-8")
    assert csv.content == b"id,title,p99_ms\r\n7,Q3 latency,41\r\n"
    assert html.headers["content-type"] == "text/html;charset=utf-8"
    assert default.headers["content-type"] == "application/json"
    assert {csv.headers["vary"], html.headers["vary"], default.headers["vary"]} == {"Accept"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_406_lists_the_available_representations() -> None:
    async with _client(app) as client:
        response = await client.get("/report", headers={"accept": "image/png, */*;q=0"})
    assert response.status_code == 406
    assert response.headers["vary"] == "Accept"
    assert response.json()["available"] == [
        "application/json",
        "text/csv",
        "text/html;charset=utf-8",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_accept_field_lines_are_combined() -> None:
    headers = [("accept", "image/png"), ("accept", "text/csv")]
    async with _client(app) as client:
        response = await client.get("/report", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_accept_is_400() -> None:
    async with _client(app) as client:
        response = await client.get("/report", headers={"accept": "text/html;q=1.5"})
    assert response.status_code == 400
    assert response.headers["vary"] == "Accept"
    assert "invalid qvalue" in response.json()["detail"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_note_415_advertises_accept_and_decodes_charsets() -> None:
    async with _client(app) as client:
        xml = await client.post("/notes", content=b"<a/>", headers={"content-type": "text/xml"})
        missing = await client.post("/notes", content=b"hi")
        doubled = await client.post(
            "/notes",
            content=b"{}",
            headers=[("content-type", "application/json"), ("content-type", "text/plain")],
        )
        patch = await client.post(
            "/notes",
            content=b'"ok"',
            headers={"content-type": "application/merge-patch+json"},
        )
        latin1 = await client.post(
            "/notes",
            content=b"caf\xe9",
            headers={"content-type": "text/plain; charset=iso-8859-1"},
        )
        ascii_only = await client.post(
            "/notes", content=b"caf\xe9", headers={"content-type": "text/plain"}
        )
    assert (xml.status_code, xml.headers["accept"]) == (415, "application/json, text/plain")
    assert (missing.status_code, missing.headers["accept"]) == (415, "application/json, text/plain")
    assert doubled.status_code == 400
    assert patch.status_code == 201
    assert latin1.json() == {"chars": 4, "text": "caf\u00e9"}
    assert ascii_only.status_code == 400


class _Item(BaseModel):
    n: int


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_0_162_0_reports_a_wrong_content_type_as_422_not_415() -> None:
    fastapi_app = FastAPI()

    @fastapi_app.post("/items")
    async def create(item: _Item) -> _Item:  # pyright: ignore[reportUnusedFunction]
        return item

    body = b'{"n": 1}'
    async with _client(fastapi_app) as client:
        ok = await client.post("/items", content=body, headers={"content-type": "application/json"})
        plain = await client.post("/items", content=body, headers={"content-type": "text/plain"})
        utf16 = await client.post(
            "/items", content=body, headers={"content-type": "application/json; charset=utf-16"}
        )
    assert ok.status_code == 200
    assert plain.status_code == 422
    assert plain.json()["detail"][0]["type"] == "model_attributes_type"
    assert utf16.status_code == 200  # the charset parameter is ignored


@pytest.mark.unit
def test_starlette_1_10_0_appends_charset_only_to_lowercase_text_types() -> None:
    def content_type(media_type: str) -> str:
        return Response(b"", media_type=media_type).headers["content-type"]

    assert content_type("text/plain") == "text/plain; charset=utf-8"
    assert content_type("Text/Plain") == "Text/Plain"
    assert content_type("text/plain;Charset=latin-1") == "text/plain;Charset=latin-1"
    assert content_type("application/json") == "application/json"
