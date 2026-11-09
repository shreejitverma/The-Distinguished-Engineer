"""Unit tests for RFC 3986 resolution and normalization and RFC 6570 template expansion."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f01_http_semantics_and_wire_protocols.uri import (
    TemplateError,
    TemplateValue,
    expand,
    normalize_percent_encoding,
    normalize_uri,
    parse_path_segments,
    parse_reference,
    remove_dot_segments,
    resolve,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.requests import Request

RFC3986_BASE = "http://a/b/c/d;p?q"

# RFC 3986 Section 5.4.1 (normal) and Section 5.4.2 (abnormal), strict parser.
RFC3986_EXAMPLES: dict[str, str] = {
    "g:h": "g:h",
    "g": "http://a/b/c/g",
    "./g": "http://a/b/c/g",
    "g/": "http://a/b/c/g/",
    "/g": "http://a/g",
    "//g": "http://g",
    "?y": "http://a/b/c/d;p?y",
    "g?y": "http://a/b/c/g?y",
    "#s": "http://a/b/c/d;p?q#s",
    "g#s": "http://a/b/c/g#s",
    "g?y#s": "http://a/b/c/g?y#s",
    ";x": "http://a/b/c/;x",
    "g;x": "http://a/b/c/g;x",
    "g;x?y#s": "http://a/b/c/g;x?y#s",
    "": "http://a/b/c/d;p?q",
    ".": "http://a/b/c/",
    "./": "http://a/b/c/",
    "..": "http://a/b/",
    "../": "http://a/b/",
    "../g": "http://a/b/g",
    "../..": "http://a/",
    "../../": "http://a/",
    "../../g": "http://a/g",
    "../../../g": "http://a/g",
    "../../../../g": "http://a/g",
    "/./g": "http://a/g",
    "/../g": "http://a/g",
    "g.": "http://a/b/c/g.",
    ".g": "http://a/b/c/.g",
    "g..": "http://a/b/c/g..",
    "..g": "http://a/b/c/..g",
    "./../g": "http://a/b/g",
    "./g/.": "http://a/b/c/g/",
    "g/./h": "http://a/b/c/g/h",
    "g/../h": "http://a/b/c/h",
    "g;x=1/./y": "http://a/b/c/g;x=1/y",
    "g;x=1/../y": "http://a/b/c/y",
    "g?y/./x": "http://a/b/c/g?y/./x",
    "g?y/../x": "http://a/b/c/g?y/../x",
    "g#s/./x": "http://a/b/c/g#s/./x",
    "g#s/../x": "http://a/b/c/g#s/../x",
    "http:g": "http:g",
}

# RFC 6570 Section 3.2 variable definitions.
RFC6570_VARS: dict[str, TemplateValue] = {
    "count": ("one", "two", "three"),
    "dom": ("example", "com"),
    "dub": "me/too",
    "hello": "Hello World!",
    "half": "50%",
    "var": "value",
    "who": "fred",
    "base": "http://example.com/home/",
    "path": "/foo/bar",
    "list": ("red", "green", "blue"),
    "keys": {"semi": ";", "dot": ".", "comma": ","},
    "v": "6",
    "x": "1024",
    "y": "768",
    "empty": "",
    "empty_keys": {},
    "undef": None,
}

# Every example table in RFC 6570 Sections 3.2.1 through 3.2.9.
RFC6570_EXAMPLES: list[tuple[str, str]] = [
    ("{count}", "one,two,three"),
    ("{count*}", "one,two,three"),
    ("{/count}", "/one,two,three"),
    ("{/count*}", "/one/two/three"),
    ("{;count}", ";count=one,two,three"),
    ("{;count*}", ";count=one;count=two;count=three"),
    ("{?count}", "?count=one,two,three"),
    ("{?count*}", "?count=one&count=two&count=three"),
    ("{&count*}", "&count=one&count=two&count=three"),
    ("{var}", "value"),
    ("{hello}", "Hello%20World%21"),
    ("{half}", "50%25"),
    ("O{empty}X", "OX"),
    ("O{undef}X", "OX"),
    ("{x,y}", "1024,768"),
    ("{x,hello,y}", "1024,Hello%20World%21,768"),
    ("?{x,empty}", "?1024,"),
    ("?{x,undef}", "?1024"),
    ("?{undef,y}", "?768"),
    ("{var:3}", "val"),
    ("{var:30}", "value"),
    ("{list}", "red,green,blue"),
    ("{list*}", "red,green,blue"),
    ("{keys}", "semi,%3B,dot,.,comma,%2C"),
    ("{keys*}", "semi=%3B,dot=.,comma=%2C"),
    ("{+var}", "value"),
    ("{+hello}", "Hello%20World!"),
    ("{+half}", "50%25"),
    ("{base}index", "http%3A%2F%2Fexample.com%2Fhome%2Findex"),
    ("{+base}index", "http://example.com/home/index"),
    ("O{+empty}X", "OX"),
    ("O{+undef}X", "OX"),
    ("{+path}/here", "/foo/bar/here"),
    ("here?ref={+path}", "here?ref=/foo/bar"),
    ("up{+path}{var}/here", "up/foo/barvalue/here"),
    ("{+x,hello,y}", "1024,Hello%20World!,768"),
    ("{+path,x}/here", "/foo/bar,1024/here"),
    ("{+path:6}/here", "/foo/b/here"),
    ("{+list}", "red,green,blue"),
    ("{+list*}", "red,green,blue"),
    ("{+keys}", "semi,;,dot,.,comma,,"),
    ("{+keys*}", "semi=;,dot=.,comma=,"),
    ("{#var}", "#value"),
    ("{#hello}", "#Hello%20World!"),
    ("{#half}", "#50%25"),
    ("foo{#empty}", "foo#"),
    ("foo{#undef}", "foo"),
    ("{#x,hello,y}", "#1024,Hello%20World!,768"),
    ("{#path,x}/here", "#/foo/bar,1024/here"),
    ("{#path:6}/here", "#/foo/b/here"),
    ("{#list}", "#red,green,blue"),
    ("{#list*}", "#red,green,blue"),
    ("{#keys}", "#semi,;,dot,.,comma,,"),
    ("{#keys*}", "#semi=;,dot=.,comma=,"),
    ("{.who}", ".fred"),
    ("{.who,who}", ".fred.fred"),
    ("{.half,who}", ".50%25.fred"),
    ("www{.dom*}", "www.example.com"),
    ("X{.var}", "X.value"),
    ("X{.empty}", "X."),
    ("X{.undef}", "X"),
    ("X{.var:3}", "X.val"),
    ("X{.list}", "X.red,green,blue"),
    ("X{.list*}", "X.red.green.blue"),
    ("X{.keys}", "X.semi,%3B,dot,.,comma,%2C"),
    ("X{.keys*}", "X.semi=%3B.dot=..comma=%2C"),
    ("X{.empty_keys}", "X"),
    ("X{.empty_keys*}", "X"),
    ("{/who}", "/fred"),
    ("{/who,who}", "/fred/fred"),
    ("{/half,who}", "/50%25/fred"),
    ("{/who,dub}", "/fred/me%2Ftoo"),
    ("{/var}", "/value"),
    ("{/var,empty}", "/value/"),
    ("{/var,undef}", "/value"),
    ("{/var,x}/here", "/value/1024/here"),
    ("{/var:1,var}", "/v/value"),
    ("{/list}", "/red,green,blue"),
    ("{/list*}", "/red/green/blue"),
    ("{/list*,path:4}", "/red/green/blue/%2Ffoo"),
    ("{/keys}", "/semi,%3B,dot,.,comma,%2C"),
    ("{/keys*}", "/semi=%3B/dot=./comma=%2C"),
    ("{;who}", ";who=fred"),
    ("{;half}", ";half=50%25"),
    ("{;empty}", ";empty"),
    ("{;v,empty,who}", ";v=6;empty;who=fred"),
    ("{;v,bar,who}", ";v=6;who=fred"),
    ("{;x,y}", ";x=1024;y=768"),
    ("{;x,y,empty}", ";x=1024;y=768;empty"),
    ("{;x,y,undef}", ";x=1024;y=768"),
    ("{;hello:5}", ";hello=Hello"),
    ("{;list}", ";list=red,green,blue"),
    ("{;list*}", ";list=red;list=green;list=blue"),
    ("{;keys}", ";keys=semi,%3B,dot,.,comma,%2C"),
    ("{;keys*}", ";semi=%3B;dot=.;comma=%2C"),
    ("{?who}", "?who=fred"),
    ("{?half}", "?half=50%25"),
    ("{?x,y}", "?x=1024&y=768"),
    ("{?x,y,empty}", "?x=1024&y=768&empty="),
    ("{?x,y,undef}", "?x=1024&y=768"),
    ("{?var:3}", "?var=val"),
    ("{?list}", "?list=red,green,blue"),
    ("{?list*}", "?list=red&list=green&list=blue"),
    ("{?keys}", "?keys=semi,%3B,dot,.,comma,%2C"),
    ("{?keys*}", "?semi=%3B&dot=.&comma=%2C"),
    ("{&who}", "&who=fred"),
    ("{&half}", "&half=50%25"),
    ("?fixed=yes{&x}", "?fixed=yes&x=1024"),
    ("{&x,y,empty}", "&x=1024&y=768&empty="),
    ("{&x,y,undef}", "&x=1024&y=768"),
    ("{&var:3}", "&var=val"),
    ("{&list}", "&list=red,green,blue"),
    ("{&list*}", "&list=red&list=green&list=blue"),
    ("{&keys}", "&keys=semi,%3B,dot,.,comma,%2C"),
    ("{&keys*}", "&semi=%3B&dot=.&comma=%2C"),
]


async def _echo(request: Request) -> JSONResponse:
    raw_path: bytes = request.scope["raw_path"]
    try:
        segments: list[str] | str = list(parse_path_segments(raw_path))
    except ValueError as exc:
        segments = f"rejected: {exc}"
    return JSONResponse(
        {
            "param": request.path_params["name"],
            "scope_path": request.scope["path"],
            "raw_path": raw_path.decode("ascii"),
            "segments": segments,
        }
    )


ECHO_APP = Starlette(
    routes=[
        Route("/seg/{name}", _echo, name="seg"),
        Route("/files/{name:path}", _echo, name="files"),
    ]
)


async def _get_json(path: str) -> tuple[int, dict[str, object]]:
    transport = httpx.ASGITransport(app=ECHO_APP)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    body: dict[str, object] = response.json() if response.status_code == 200 else {}
    return response.status_code, body


@pytest.mark.unit
@pytest.mark.parametrize(("reference", "target"), list(RFC3986_EXAMPLES.items()))
def test_resolution_matches_rfc_3986_section_5_4_tables(reference: str, target: str) -> None:
    assert resolve(RFC3986_BASE, reference) == target


@pytest.mark.unit
def test_non_strict_parser_reopens_the_same_scheme_loophole() -> None:
    assert resolve(RFC3986_BASE, "http:g", strict=True) == "http:g"
    assert resolve(RFC3986_BASE, "http:g", strict=False) == "http://a/b/c/g"
    assert resolve(RFC3986_BASE, "https:g", strict=False) == "https:g"


@pytest.mark.unit
def test_cpython_urljoin_differs_from_strict_rfc_3986_only_on_http_g() -> None:
    mismatches = {
        ref: urljoin(RFC3986_BASE, ref)
        for ref, target in RFC3986_EXAMPLES.items()
        if urljoin(RFC3986_BASE, ref) != target
    }
    assert mismatches == {"http:g": "http://a/b/c/g"}
    assert urljoin(RFC3986_BASE, "http:g") == resolve(RFC3986_BASE, "http:g", strict=False)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("merged", "expected"),
    [
        ("/a/b/c/./../../g", "/a/g"),
        ("mid/content=5/../6", "mid/6"),
        ("/a/b/..", "/a/"),
        ("/a/b/.", "/a/b/"),
        ("/..", "/"),
        ("/../../../etc", "/etc"),
        ("../g", "g"),
        ("./g", "g"),
        ("..", ""),
        (".", ""),
        ("", ""),
    ],
)
def test_remove_dot_segments_follows_section_5_2_4_buffers(merged: str, expected: str) -> None:
    assert remove_dot_segments(merged) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "httpx_raw_path", "rfc_path"),
    [
        ("http://t/a/b/..", b"/a", "/a/"),
        ("http://t/a/b/.", b"/a/b", "/a/b/"),
        ("http://t/x/../../../etc", b"/etc", "/etc"),
    ],
)
def test_httpx_client_dot_removal_drops_the_trailing_slash_rfc_keeps(
    url: str, httpx_raw_path: bytes, rfc_path: str
) -> None:
    assert httpx.URL(url).raw_path == httpx_raw_path
    assert remove_dot_segments(parse_reference(url).path) == rfc_path


@pytest.mark.unit
def test_syntax_based_normalization_rfc_3986_section_6_2_2_example() -> None:
    left = normalize_uri("example://a/b/c/%7Bfoo%7D")
    right = normalize_uri("eXAMPLE://a/./b/../b/%63/%7bfoo%7d")
    assert left == right == "example://a/b/c/%7Bfoo%7D"


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com:80/~smith/home.html",
        "http://EXAMPLE.com/%7Esmith/home.html",
        "http://EXAMPLE.com:/%7esmith/home.html",
    ],
)
def test_rfc_9110_section_4_2_3_equivalent_http_uris(uri: str) -> None:
    assert normalize_uri(uri) == "http://example.com/~smith/home.html"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "normal"),
    [
        ("https://Example.COM:443", "https://example.com/"),
        ("http://h:8080", "http://h:8080/"),
        ("HTTP://[2001:DB8::1]:80/x", "http://[2001:db8::1]/x"),
        ("http://User%7e@H/", "http://User~@h/"),
        ("http://h/a%2fb", "http://h/a%2Fb"),
        ("http://h/?", "http://h/?"),
        ("http://h/#", "http://h/#"),
        ("http://h/p?%7e=%2f#%7Ex%2f", "http://h/p?~=%2F#~x%2F"),
        ("mailto:Joe@Example.COM", "mailto:Joe@Example.COM"),
    ],
)
def test_normalization_keeps_reserved_octets_and_empty_delimiters(uri: str, normal: str) -> None:
    assert normalize_uri(uri) == normal


@pytest.mark.unit
def test_reserved_slash_and_its_encoding_are_different_resources() -> None:
    assert normalize_uri("http://h/a%2Fb") != normalize_uri("http://h/a/b")
    assert normalize_uri("http://h/?") != normalize_uri("http://h/")


@pytest.mark.unit
def test_unreserved_decoding_runs_before_dot_segment_removal() -> None:
    assert normalize_uri("http://h/a/%2E%2E/b") == "http://h/b"
    assert normalize_uri("http://h/a/%2e/b") == "http://h/a/b"


@pytest.mark.unit
def test_host_is_lowercased_after_unreserved_octets_are_decoded() -> None:
    # RFC 3986 Sections 6.2.2.1 and 6.2.2.2: lowercase host, uppercase triplet hex digits.
    assert normalize_uri("http://%41.com/") == normalize_uri("http://a.com/") == "http://a.com/"
    assert normalize_uri("http://%c3%a9X.com/") == "http://%C3%A9x.com/"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "match"),
    [
        ("/relative", "only absolute URIs"),
        ("http://h/%zz", "malformed percent-encoding"),
        ("http://h/%4", "malformed percent-encoding"),
        ("http://h:8o/", "invalid port"),
        ("http://[::1/", "unterminated IP-literal"),
        ("http://[::1]x/", "garbage after IP-literal"),
    ],
)
def test_normalize_rejects_malformed_input(uri: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        normalize_uri(uri)


@pytest.mark.unit
def test_percent_normalization_uppercases_hex_and_decodes_only_unreserved() -> None:
    assert normalize_percent_encoding("%7e%2f%41%c3%a9") == "~%2FA%C3%A9"


@pytest.mark.unit
def test_resolve_requires_an_absolute_base() -> None:
    with pytest.raises(ValueError, match="base URI must be absolute"):
        resolve("/b/c", "g")


@pytest.mark.unit
def test_parse_and_recompose_preserve_undefined_versus_empty_components() -> None:
    assert parse_reference("http://h/p?").query == ""
    assert parse_reference("http://h/p").query is None
    assert parse_reference("http:///p").authority == ""
    for ref in ("http://h/p?", "http://h/p", "http://h/p#", "//h", "?q", "#f", ""):
        assert parse_reference(ref).recompose() == ref
    assert resolve("http://h", "g") == "http://h/g"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_routes_on_decoded_path_so_encoded_slash_is_lost() -> None:
    status, _ = await _get_json("/seg/a%2Fb")
    assert status == 404

    _, encoded = await _get_json("/files/a%2Fb")
    _, literal = await _get_json("/files/a/b")
    assert encoded["param"] == literal["param"] == "a/b"
    assert encoded["scope_path"] == literal["scope_path"] == "/files/a/b"
    assert encoded["raw_path"] == "/files/a%2Fb"
    assert encoded["segments"] == ["files", "a/b"]
    assert literal["segments"] == ["files", "a", "b"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_encoded_dot_segment_survives_client_and_reaches_the_handler() -> None:
    assert httpx.URL("http://test/files/%2E%2E/secret").raw_path == b"/files/%2E%2E/secret"
    _, body = await _get_json("/files/%2E%2E/secret")
    assert body["param"] == "../secret"
    assert body["scope_path"] == "/files/../secret"
    assert body["segments"] == "rejected: dot segment '%2E%2E' after decoding"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_utf8_octets_collapse_to_replacement_character() -> None:
    _, ff = await _get_json("/seg/%FF")
    _, fe = await _get_json("/seg/%FE")
    assert ff["param"] == fe["param"] == "\ufffd"
    assert isinstance(ff["segments"], str)
    assert ff["segments"].startswith("rejected: 'utf-8' codec can't decode byte 0xff")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_path", "match"),
    [
        (b"seg", "must start with '/'"),
        (b"/a/%zz", "malformed percent-encoding"),
        (b"/a/%00", "NUL octet"),
        (b"/a/%2e", "dot segment"),
        (b"/a/..", "dot segment"),
        (b"/a/%C3", "can't decode"),
    ],
)
def test_parse_path_segments_rejects_dangerous_segments(raw_path: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_path_segments(raw_path)


@pytest.mark.unit
def test_parse_path_segments_decodes_after_splitting() -> None:
    assert parse_path_segments(b"/v1/objects/a%2Fb/caf%C3%A9") == (
        "v1",
        "objects",
        "a/b",
        "caf\u00e9",
    )
    assert parse_path_segments(b"/") == ("",)


@pytest.mark.unit
@pytest.mark.parametrize(("template", "expansion"), RFC6570_EXAMPLES)
def test_expansion_matches_every_rfc_6570_section_3_2_example(
    template: str, expansion: str
) -> None:
    assert expand(template, RFC6570_VARS) == expansion


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "variables", "expansion"),
    [
        ("{var}", {"var": "value"}, "value"),
        ("{var:20}", {"var": "value"}, "value"),
        ("{var:3}", {"var": "value"}, "val"),
        ("{semi}", {"semi": ";"}, "%3B"),
        ("{semi:2}", {"semi": ";"}, "%3B"),
        ("find{?year*}", {"year": ("1965", "2000", "2012")}, "find?year=1965&year=2000&year=2012"),
        ("www{.dom*}", {"dom": ("example", "com")}, "www.example.com"),
        (
            "/mapper{?address*}",
            {"address": {"city": "Newport Beach", "state": "CA"}},
            "/mapper?city=Newport%20Beach&state=CA",
        ),
    ],
)
def test_expansion_matches_rfc_6570_section_2_4_examples(
    template: str, variables: Mapping[str, TemplateValue], expansion: str
) -> None:
    assert expand(template, variables) == expansion


@pytest.mark.unit
def test_prefix_counts_code_points_and_keeps_triplets_whole() -> None:
    assert expand("{v:1}", {"v": "\u00e9t\u00e9"}) == "%C3%A9"
    assert expand("{+v:2}", {"v": "%2Fab"}) == "%2Fa"
    assert expand("{v:2}", {"v": "%2Fab"}) == "%252"
    assert expand("caf\u00e9 {v}", {"v": "x"}) == "caf%C3%A9%20x"


@pytest.mark.unit
def test_undefined_composites_and_empty_explode_members() -> None:
    assert expand("/s{?m*}", {"m": {"a": None}}) == "/s"
    assert expand("/s{?l*}", {"l": ()}) == "/s"
    assert expand("{?m*}", {"m": {"a": "", "b": None}}) == "?a="
    assert expand("{;m*}", {"m": {"a": ""}}) == ";a"
    assert expand("{?l*}", {"l": ("", "x")}) == "?l=&l=x"
    assert expand("{;l*}", {"l": ("", "x")}) == ";l;l=x"
    # RFC 6570 Appendix A: unnamed operators always append a pair as "name=value".
    assert expand("{m*}", {"m": {"a": ""}}) == "a="
    assert expand("{/m*}", {"m": {"a": "", "b": "x"}}) == "/a=/b=x"
    assert expand("{#m*}", {"m": {"a": ""}}) == "#a="
    # Named operators encode a pair name like a literal, and its value with the allow set.
    assert expand("{?m*}", {"m": {"a/b": "c/d"}}) == "?a/b=c%2Fd"


@pytest.mark.unit
def test_reserved_expansion_lets_a_value_traverse_but_path_operator_does_not() -> None:
    value = {"name": "../admin/keys"}
    reserved = expand("/v1/files/{+name}", value)
    segment = expand("/v1/files{/name}", value)
    assert reserved == "/v1/files/../admin/keys"
    assert segment == "/v1/files/..%2Fadmin%2Fkeys"
    assert resolve("https://api.test/", reserved) == "https://api.test/v1/admin/keys"
    assert resolve("https://api.test/", segment) == "https://api.test/v1/files/..%2Fadmin%2Fkeys"


@pytest.mark.unit
def test_stdlib_quote_keeps_slash_by_default() -> None:
    assert quote("../admin/keys") == "../admin/keys"
    assert quote("../admin/keys", safe="") == "..%2Fadmin%2Fkeys"
    assert expand("{name}", {"name": "../admin/keys"}) == "..%2Fadmin%2Fkeys"


@pytest.mark.unit
def test_starlette_url_path_for_does_not_percent_encode_parameters() -> None:
    built = ECHO_APP.url_path_for("seg", name="a?b")
    assert built == "/seg/a?b"
    assert parse_reference(built).path == "/seg/a"
    assert parse_reference(built).query == "b"
    assert expand("/seg/{name}", {"name": "a?b"}) == "/seg/a%3Fb"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "match"),
    [
        ("/x{", "unclosed expression"),
        ("/x}", "unmatched"),
        ("{}", "empty expression"),
        ("{=x}", "reserved for future extensions"),
        ("{|x}", "reserved for future extensions"),
        ("{var:0}", "invalid varspec"),
        ("{var:10000}", "invalid varspec"),
        ("{var*:3}", "invalid varspec"),
        ("{a{b}", "invalid varspec"),
        ("{x,}", "invalid varspec"),
        ("{list:2}", "prefix modifier on composite"),
    ],
)
def test_malformed_templates_raise(template: str, match: str) -> None:
    with pytest.raises(TemplateError, match=match):
        expand(template, RFC6570_VARS)
