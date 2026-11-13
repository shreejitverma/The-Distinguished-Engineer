"""Lab for API-Discovery-and-Well-Known-API-Catalog-RFC-9727.

The unit tests run in process; the socket tests drive Uvicorn, Hypercorn, and Granian on a
loopback port to show which server and HTTP version strips a HEAD body an app leaks.
"""

from __future__ import annotations

import asyncio
import json
import socket
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from granian.constants import HTTPModes, Interfaces
from granian.server.embed import Server as GranianServer
from hypercorn.asyncio import serve  # pyright: ignore[reportUnknownVariableType]
from hypercorn.config import Config
from hypothesis import given
from hypothesis import strategies as st

from api_labs.f07_versioning_evolution_and_api_guidelines.api_catalog import (
    LINKSET_JSON,
    LINKSET_TEXT,
    PROFILE_URI,
    WELL_KNOWN_PATH,
    ApiEntry,
    FetchError,
    Link,
    Redirect,
    Target,
    build_catalog,
    crawl,
    create_app,
    create_redirect_app,
    iter_links,
    negotiate,
    parse_link_header,
    to_link_format,
    to_link_header,
    validate_api_catalog,
    validate_linkset,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

CATALOG_URL = "https://www.example.com/.well-known/api-catalog"
DEV = "https://developer.example.com/apis"

FOO = ApiEntry(
    anchor=f"{DEV}/foo_api",
    service_desc=(Target(f"{DEV}/foo_api/spec", type="application/yaml"),),
    service_doc=(Target(f"{DEV}/foo_api/doc", type="text/html"),),
    status=(Target(f"{DEV}/foo_api/status", type="application/json"),),
)
BAR = ApiEntry(
    anchor=f"{DEV}/bar_api",
    service_desc=(Target(f"{DEV}/bar_api/spec", type="application/vnd.oai.openapi+json"),),
)

# RFC 9727 Appendix A.2, verbatim.
RFC9727_A2: dict[str, object] = {
    "linkset": [
        {
            "anchor": "https://www.example.com/.well-known/api-catalog",
            "item": [
                {"href": "https://developer.example.com/apis/foo_api"},
                {"href": "https://developer.example.com/apis/bar_api"},
                {"href": "https://developer.example.com/apis/cantona_api"},
            ],
        }
    ]
}
# RFC 9727 Appendix A.4, verbatim.
RFC9727_A4: dict[str, object] = {
    "linkset": [
        {
            "anchor": "https://www.example.com/.well-known/api-catalog",
            "api-catalog": [
                {"href": "https://apis.example.com/iot/api-catalog"},
                {"href": "https://ecommerce.example.com/api-catalog"},
                {"href": "https://developer.example.com/gaming/api-catalog"},
            ],
        }
    ]
}
# RFC 9727 Appendix A.1, verbatim: one context per API, no `item` links, a second domain.
RFC9727_A1: dict[str, object] = {
    "linkset": [
        {
            "anchor": "https://developer.example.com/apis/foo_api",
            "service-desc": [
                {
                    "href": "https://developer.example.com/apis/foo_api/spec",
                    "type": "application/yaml",
                }
            ],
            "status": [
                {
                    "href": "https://developer.example.com/apis/foo_api/status",
                    "type": "application/json",
                }
            ],
            "service-doc": [
                {"href": "https://developer.example.com/apis/foo_api/doc", "type": "text/html"}
            ],
            "service-meta": [
                {"href": "https://developer.example.com/apis/foo_api/policies", "type": "text/xml"}
            ],
        },
        {
            "anchor": "https://developer.example.com/apis/bar_api",
            "service-desc": [
                {
                    "href": "https://developer.example.com/apis/bar_api/spec",
                    "type": "application/yaml",
                }
            ],
            "status": [
                {
                    "href": "https://developer.example.com/apis/bar_api/status",
                    "type": "application/json",
                }
            ],
            "service-doc": [
                {"href": "https://developer.example.com/apis/bar_api/doc", "type": "text/plain"}
            ],
        },
        {
            "anchor": "https://apis.example.net/apis/cantona_api",
            "service-desc": [
                {"href": "https://apis.example.net/apis/cantona_api/spec", "type": "text/n3"}
            ],
            "service-doc": [
                {"href": "https://apis.example.net/apis/cantona_api/doc", "type": "text/html"}
            ],
        },
    ]
}
# RFC 9727 Section 5.1, verbatim: "api-catalog" is a bare string (Erratum ID 9009).
RFC9727_SECTION_5_1: dict[str, object] = {
    "linkset": [
        {
            "anchor": "https://www.example.com/.well-known/api-catalog",
            "item": [
                {"href": "https://developer.example.com/apis/foo_api"},
                {"href": "https://developer.example.com/apis/bar_api"},
                {"href": "https://developer.example.com/apis/cantona_api"},
            ],
            "api-catalog": "https://www.example.net/.well-known/api-catalog",
        }
    ]
}


def _client(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="https://www.example.com")


# --- building and validating -------------------------------------------------


@pytest.mark.unit
def test_build_catalog_lists_items_then_one_context_per_api() -> None:
    doc = build_catalog(CATALOG_URL, [FOO, BAR])
    assert doc == {
        "linkset": [
            {
                "anchor": CATALOG_URL,
                "item": [{"href": f"{DEV}/foo_api"}, {"href": f"{DEV}/bar_api"}],
            },
            {
                "anchor": f"{DEV}/foo_api",
                "service-desc": [{"href": f"{DEV}/foo_api/spec", "type": "application/yaml"}],
                "service-doc": [{"href": f"{DEV}/foo_api/doc", "type": "text/html"}],
                "status": [{"href": f"{DEV}/foo_api/status", "type": "application/json"}],
            },
            {
                "anchor": f"{DEV}/bar_api",
                "service-desc": [
                    {"href": f"{DEV}/bar_api/spec", "type": "application/vnd.oai.openapi+json"}
                ],
            },
        ]
    }
    assert validate_api_catalog(doc) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("catalog_url", "apis", "message"),
    [
        ("/.well-known/api-catalog", [FOO], "absolute https"),
        ("http://www.example.com/.well-known/api-catalog", [FOO], "absolute https"),
        (CATALOG_URL, [ApiEntry("https://x.example/a", (Target("/spec"),))], "absolute https"),
        (CATALOG_URL, [FOO, FOO], "duplicate API anchor"),
        (CATALOG_URL, [], "MUST include hyperlinks"),
    ],
    ids=["relative-catalog", "http-catalog", "relative-href", "duplicate", "empty"],
)
def test_build_catalog_rejects(catalog_url: str, apis: list[ApiEntry], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_catalog(catalog_url, apis)


@pytest.mark.unit
@pytest.mark.parametrize(
    "doc",
    [RFC9727_A1, RFC9727_A2, RFC9727_A4],
    ids=["A.1-rfc8631-relations", "A.2-bookmarks", "A.4-nesting"],
)
def test_rfc9727_appendix_examples_validate(doc: dict[str, object]) -> None:
    assert validate_api_catalog(doc) == []


@pytest.mark.unit
def test_rfc9727_section_5_1_example_violates_rfc9264_array_rule() -> None:
    violations = validate_linkset(RFC9727_SECTION_5_1)
    assert [(v.path, v.level) for v in violations] == [("/linkset/0/api-catalog", "MUST")]


@pytest.mark.unit
def test_rfc9264_figure_10_datetime_string_violates_extension_array_rule() -> None:
    figure_10_memento = {
        "linkset": [
            {
                "anchor": "https://example.org/resource1",
                "memento": [
                    {
                        "href": "https://example.org/resource1?version=1",
                        "type": "text/html",
                        "datetime": "Thu, 13 Jun 2019 09:34:33 GMT",
                    }
                ],
            }
        ]
    }
    violations = validate_linkset(figure_10_memento)
    assert [v.path for v in violations] == ["/linkset/0/memento/0/datetime"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("doc", "path", "level"),
    [
        ({"linkset": [], "extra": 1}, "", "MUST"),
        ({"linkset": {"anchor": CATALOG_URL}}, "/linkset", "MUST"),
        ({"linkset": [{"item": [{"title": "x"}]}]}, "/linkset/0/item/0", "MUST"),
        (
            {"linkset": [{"item": [{"href": "https://a.example", "hreflang": "en"}]}]},
            "/linkset/0/item/0/hreflang",
            "MUST",
        ),
        ({"linkset": [{"Item": [{"href": "https://a.example"}]}]}, "/linkset/0/Item", "MUST"),
        (
            {"linkset": [{"anchor": "/cat", "item": [{"href": "https://a.example"}]}]},
            "/linkset/0/anchor",
            "SHOULD",
        ),
        (
            {
                "linkset": [
                    {"item": [{"href": "https://a.example", "title*": [{"language": "de"}]}]}
                ]
            },
            "/linkset/0/item/0/title*/0",
            "MUST",
        ),
        (
            {
                "linkset": [
                    {
                        "item": [
                            {
                                "href": "https://a.example",
                                "title*": [{"value": "Katalog", "language": ["de"]}],
                            }
                        ]
                    }
                ]
            },
            "/linkset/0/item/0/title*/0",
            "MUST",
        ),
    ],
    ids=[
        "extra-member",
        "not-array",
        "no-href",
        "hreflang-string",
        "uppercase-rel",
        "relative-anchor",
        "title-star-no-value",
        "title-star-language-not-string",
    ],
)
def test_validator_reports_each_rfc9264_rule(doc: object, path: str, level: str) -> None:
    violations = validate_linkset(doc)
    assert [(v.path, v.level) for v in violations] == [(path, level)]


@pytest.mark.unit
def test_empty_linkset_is_valid_linkset_but_not_a_valid_catalog() -> None:
    assert validate_linkset({"linkset": []}) == []
    assert [v.rule for v in validate_api_catalog({"linkset": []})] == [
        "catalog MUST include hyperlinks (9727 4.1)"
    ]


# --- serializations ----------------------------------------------------------


@pytest.mark.unit
def test_link_format_round_trips_through_rfc8288_parser() -> None:
    doc = build_catalog(CATALOG_URL, [FOO, BAR], nested_catalogs=["https://apis.example.com/c"])
    text = to_link_format(doc)
    assert "\n" in text
    header = to_link_header(doc)
    assert "\n" not in header
    assert parse_link_header(header, base_url=None) == list(iter_links(doc))
    assert parse_link_header(text, base_url=None) == list(iter_links(doc))


@pytest.mark.unit
@given(st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=40))
def test_quoted_title_round_trips_any_printable_ascii(title: str) -> None:
    doc = {"linkset": [{"anchor": CATALOG_URL, "item": [{"href": f"{DEV}/x", "title": title}]}]}
    (link,) = parse_link_header(to_link_header(doc), base_url=None)
    assert link.attributes == (("title", title),)


@pytest.mark.unit
def test_link_format_refuses_non_ascii() -> None:
    doc = {
        "linkset": [{"anchor": CATALOG_URL, "item": [{"href": f"{DEV}/x", "title": "caf\u00e9"}]}]
    }
    with pytest.raises(ValueError, match="ASCII only"):
        to_link_format(doc)


@pytest.mark.unit
def test_parser_applies_rfc8288_appendix_b_rules() -> None:
    value = (
        '</a>; REL="api-catalog Item"; title="one"; title="two"; anchor="/x?v=1", '
        "<https://b.example/c>;rel=status"
    )
    links = parse_link_header(value, base_url="https://www.example.com/dir/page")
    assert links == [
        Link(
            "https://www.example.com/x?v=1",
            "api-catalog",
            "https://www.example.com/a",
            (("title", "one"),),
        ),
        Link(
            "https://www.example.com/x?v=1",
            "item",
            "https://www.example.com/a",
            (("title", "one"),),
        ),
        Link("https://www.example.com/dir/page", "status", "https://b.example/c", ()),
    ]


@pytest.mark.unit
def test_rfc9727_section_3_link_header_resolves_to_the_catalog_location() -> None:
    # RFC 9727 Section 3, verbatim: an unquoted relation and a relative target on the home page.
    value = "</my_api_catalog.json>; rel=api-catalog"
    assert parse_link_header(value, base_url="https://www.example.com/") == [
        Link(
            "https://www.example.com/", "api-catalog", "https://www.example.com/my_api_catalog.json"
        )
    ]


@pytest.mark.unit
def test_httpx_links_drops_rel_when_an_earlier_param_contains_equals() -> None:
    value = '<https://www.example.com/c>; anchor="https://www.example.com/?v=1"; rel="api-catalog"'
    response = httpx.Response(200, headers={"link": value})
    # httpx 0.28.1 _parse_header_links: param.split("=") raises on the second "=", then breaks.
    assert "api-catalog" not in response.links
    assert response.links == {"https://www.example.com/c": {"url": "https://www.example.com/c"}}
    assert [link.rel for link in parse_link_header(value, base_url=None)] == ["api-catalog"]


@pytest.mark.unit
def test_httpx_links_keys_by_raw_rel_string_and_collapses_duplicates() -> None:
    value = '<https://a.example/1>; rel="item", <https://a.example/2>; rel="item api-catalog"'
    links = httpx.Response(200, headers={"link": value}).links
    assert set(links) == {"item", "item api-catalog"}
    value_dup = '<https://a.example/1>; rel="item", <https://a.example/2>; rel="item"'
    assert httpx.Response(200, headers={"link": value_dup}).links["item"]["url"] == (
        "https://a.example/2"
    )
    assert len(parse_link_header(value_dup, base_url=None)) == 2


# --- negotiation -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (None, LINKSET_JSON),
        ("*/*", LINKSET_JSON),
        ("application/linkset", LINKSET_TEXT),
        ("application/json", None),
        ("application/linkset+json;q=0, */*", LINKSET_TEXT),
        ("application/linkset+json;q=0", None),
        ("application/*;q=0.9, application/linkset+json;q=0.1", LINKSET_TEXT),
        (f'application/linkset+json; profile="{PROFILE_URI}"', LINKSET_JSON),
        ('application/linkset+json; profile="https://other.example/p"', None),
        (f'application/linkset+json; profile="{PROFILE_URI} https://other.example/p"', None),
        (f'application/linkset; profile="{PROFILE_URI}"', LINKSET_TEXT),
        ("application/linkset+json;q=2", None),
        (", ".join(["text/plain"] * 32 + ["application/linkset+json"]), None),
    ],
    ids=[
        "missing",
        "wildcard",
        "text",
        "plain-json",
        "q0-excludes",
        "only-q0",
        "specific-wins",
        "profile",
        "wrong-profile",
        "two-profiles",
        "text-profile",
        "bad-q",
        "range-cap",
    ],
)
def test_negotiate(accept: str | None, expected: str | None) -> None:
    assert negotiate(accept) == expected


# --- the well-known resource -------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_serves_profiled_linkset_with_self_link() -> None:
    doc = build_catalog(CATALOG_URL, [FOO, BAR])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        response = await client.get(WELL_KNOWN_PATH, headers={"accept": LINKSET_JSON})
    assert response.status_code == 200
    assert response.headers["content-type"] == f'{LINKSET_JSON}; profile="{PROFILE_URI}"'
    assert response.headers["vary"] == "Accept"
    assert response.headers["cache-control"] == "max-age=300"
    assert parse_link_header(response.headers["link"], str(response.url)) == [
        Link(str(response.url), "api-catalog", CATALOG_URL)
    ]
    assert json.loads(response.content) == doc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_sends_get_headers_and_length_but_no_content() -> None:
    doc = build_catalog(CATALOG_URL, [FOO, BAR])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        get = await client.get(WELL_KNOWN_PATH)
        head = await client.head(WELL_KNOWN_PATH)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(get.content))
    for name in ("cache-control", "content-type", "etag", "link", "vary"):
        assert head.headers[name] == get.headers[name]


async def _raw_asgi(app: ASGIApp, method: str, accept: str) -> tuple[int, dict[str, str], bytes]:
    """Call the app with a bare ASGI scope and return what it sent, with nothing in between."""
    sent: list[dict[str, object]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": WELL_KNOWN_PATH,
        "raw_path": WELL_KNOWN_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"www.example.com"), (b"accept", accept.encode())],
        "server": ("www.example.com", 443),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(scope, receive, send)  # type: ignore[arg-type]
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    status, body = sent[0]["status"], sent[1]["body"]
    assert isinstance(status, int)
    assert isinstance(body, bytes)
    raw_headers = cast("list[tuple[bytes, bytes]]", sent[0]["headers"])
    return status, {name.decode(): value.decode() for name, value in raw_headers}, body


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accept", "status"),
    [(LINKSET_JSON, 200), (LINKSET_TEXT, 200), ("application/json", 406)],
    ids=["json-200", "text-200", "not-acceptable-406"],
)
async def test_head_app_emits_no_body_bytes_at_the_asgi_layer(accept: str, status: int) -> None:
    # httpx 0.28.1 ASGITransport drops HEAD bodies itself, so assert on raw ASGI messages.
    app = create_app(build_catalog(CATALOG_URL, [FOO]), CATALOG_URL)
    get_status, get_headers, get_body = await _raw_asgi(app, "GET", accept)
    head_status, head_headers, head_body = await _raw_asgi(app, "HEAD", accept)
    assert (get_status, head_status) == (status, status)
    assert get_body != b""
    assert head_body == b""
    assert head_headers["content-length"] == get_headers["content-length"] == str(len(get_body))
    assert head_headers == get_headers


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("tag_form", ["{}", "W/{}", '"other", {}', "*"])
async def test_if_none_match_returns_304_with_validators(tag_form: str) -> None:
    doc = build_catalog(CATALOG_URL, [FOO])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        etag = (await client.get(WELL_KNOWN_PATH)).headers["etag"]
        response = await client.get(
            WELL_KNOWN_PATH, headers={"if-none-match": tag_form.format(etag)}
        )
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert response.headers["vary"] == "Accept"
    # RFC 9110 Section 15.4.5: a 304 repeats the Cache-Control the 200 would have carried.
    assert response.headers["cache-control"] == "max-age=300"
    assert "content-type" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_etag_gets_full_200() -> None:
    doc = build_catalog(CATALOG_URL, [FOO])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        response = await client.get(WELL_KNOWN_PATH, headers={"if-none-match": '"stale"'})
    assert response.status_code == 200
    assert json.loads(response.content) == doc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_text_representation_has_its_own_etag_and_same_links() -> None:
    doc = build_catalog(CATALOG_URL, [FOO, BAR])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        as_json = await client.get(WELL_KNOWN_PATH, headers={"accept": LINKSET_JSON})
        as_text = await client.get(WELL_KNOWN_PATH, headers={"accept": LINKSET_TEXT})
    assert as_text.headers["content-type"] == f'{LINKSET_TEXT}; profile="{PROFILE_URI}"'
    assert as_text.headers["etag"] != as_json.headers["etag"]
    assert parse_link_header(as_text.text, base_url=None) == list(iter_links(doc))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unacceptable_accept_gets_406_listing_available_types() -> None:
    doc = build_catalog(CATALOG_URL, [FOO])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        response = await client.get(WELL_KNOWN_PATH, headers={"accept": "application/json"})
    assert response.status_code == 406
    assert response.json() == {"available": [LINKSET_JSON, LINKSET_TEXT]}
    assert response.headers["vary"] == "Accept"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_method_gets_405_with_allow_including_head() -> None:
    doc = build_catalog(CATALOG_URL, [FOO])
    async with _client(create_app(doc, CATALOG_URL)) as client:
        response = await client.post(WELL_KNOWN_PATH, content=b"{}")
    assert response.status_code == 405
    assert set(response.headers["allow"].split(", ")) == {"GET", "HEAD"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_age_sets_the_freshness_lifetime() -> None:
    doc = build_catalog(CATALOG_URL, [FOO])
    async with _client(create_app(doc, CATALOG_URL, max_age=86_400)) as client:
        response = await client.get(WELL_KNOWN_PATH)
    assert response.headers["cache-control"] == "max-age=86400"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_non_canonical_domain_redirects_with_308_and_keeps_the_link(method: str) -> None:
    # RFC 9727 Section 5.1: other instances redirect to the canonical well-known URI.
    transport = httpx.ASGITransport(app=create_redirect_app(CATALOG_URL))
    async with httpx.AsyncClient(transport=transport, base_url="https://www.example.net") as client:
        response = await client.request(method, WELL_KNOWN_PATH)
    assert response.status_code == 308
    assert response.headers["location"] == CATALOG_URL
    assert parse_link_header(response.headers["link"], str(response.url)) == [
        Link(str(response.url), "api-catalog", CATALOG_URL)
    ]
    assert response.content == b""


@pytest.mark.unit
def test_redirect_app_refuses_a_non_https_canonical_url() -> None:
    with pytest.raises(ValueError, match="absolute https"):
        create_redirect_app("http://www.example.com/.well-known/api-catalog")


@pytest.mark.unit
def test_create_app_refuses_non_conformant_catalog() -> None:
    with pytest.raises(ValueError, match="non-conformant"):
        create_app(RFC9727_SECTION_5_1, CATALOG_URL)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_get_route_does_not_answer_head() -> None:
    app = FastAPI()

    @app.get("/get-only")
    def get_only() -> dict[str, str]:
        return {"ok": "yes"}

    @app.api_route("/get-and-head", methods=["GET", "HEAD"])
    def get_and_head() -> dict[str, str]:
        return {"ok": "yes"}

    async with _client(app) as client:
        get_only_head = await client.head("/get-only")
        both_head = await client.head("/get-and-head")
    assert (get_only_head.status_code, get_only_head.headers["allow"]) == (405, "GET")
    assert both_head.status_code == 200


# --- the client: crawling nested catalogs ------------------------------------

ROOT = "https://www.example.com/.well-known/api-catalog"
IOT = "https://apis.example.com/iot/api-catalog"
GAMING = "https://developer.example.com/gaming/api-catalog"


def _nested(anchor: str, children: list[str], items: list[str]) -> dict[str, object]:
    ctx: dict[str, object] = {"anchor": anchor}
    if items:
        ctx["item"] = [{"href": href} for href in items]
    if children:
        ctx["api-catalog"] = [{"href": href} for href in children]
    return {"linkset": [ctx]}


@pytest.mark.unit
def test_crawl_terminates_on_cycles_and_visits_each_catalog_once() -> None:
    calls: list[str] = []
    store = {
        ROOT: _nested(ROOT, [IOT, GAMING], []),
        IOT: _nested(IOT, [ROOT, GAMING], ["https://apis.example.com/iot/v1"]),
        GAMING: _nested(GAMING, [IOT], ["https://developer.example.com/gaming/v2"]),
    }

    def fetch(url: str) -> object:
        calls.append(url)
        return store[url]

    result = crawl(ROOT, fetch)
    assert calls == [ROOT, IOT, GAMING]
    assert result.catalogs == (ROOT, IOT, GAMING)
    assert result.apis == (
        "https://apis.example.com/iot/v1",
        "https://developer.example.com/gaming/v2",
    )
    assert result.rejected == ()


@pytest.mark.unit
def test_crawl_enforces_depth_and_catalog_budgets() -> None:
    chain = [f"https://c{i}.example.com/api-catalog" for i in range(6)]
    store = {url: _nested(url, chain[i + 1 : i + 2], []) for i, url in enumerate(chain)}
    deep = crawl(chain[0], store.__getitem__, max_depth=2)
    assert deep.catalogs == tuple(chain[:3])
    assert deep.rejected == ((chain[3], "depth limit"),)
    narrow = crawl(chain[0], store.__getitem__, max_catalogs=2, max_depth=10)
    assert narrow.catalogs == tuple(chain[:2])
    assert narrow.rejected == ((chain[2], "catalog budget exhausted"),)


@pytest.mark.unit
def test_crawl_rejects_http_third_party_invalid_and_unfetchable() -> None:
    plain = "http://apis.example.com/api-catalog"
    broken = "https://broken.example.com/api-catalog"
    errata = "https://errata.example.com/api-catalog"
    store: dict[str, object] = {
        ROOT: {
            "linkset": [
                {
                    "anchor": ROOT,
                    "api-catalog": [{"href": plain}, {"href": broken}, {"href": errata}],
                    "item": [{"href": "https://www.example.com/a"}],
                },
                {
                    "anchor": "https://evil.example.net/b",
                    "service-desc": [{"href": "https://evil.example.net/b/spec"}],
                },
            ]
        },
        errata: RFC9727_SECTION_5_1,
    }

    def fetch(url: str) -> object:
        if url not in store:
            raise FetchError("404")
        return store[url]

    allowed = frozenset({"www.example.com", "broken.example.com", "errata.example.com"})
    result = crawl(ROOT, fetch, allowed_hosts=allowed)
    assert result.catalogs == (ROOT,)
    assert result.apis == ("https://www.example.com/a",)
    assert result.rejected == (
        ("https://evil.example.net/b", "host not allowed (RFC 9264 Section 9 third-party links)"),
        (plain, "not https (RFC 9727 Section 8)"),
        (broken, "fetch failed: 404"),
        (errata, "invalid linkset"),
    )


# --- sizes (deterministic, so exact) -----------------------------------------


@pytest.mark.unit
def test_catalog_sizes_for_one_hundred_apis() -> None:
    apis = [
        ApiEntry(
            anchor=f"https://api.example.com/v1/svc{i:03d}",
            service_desc=(Target(f"https://api.example.com/v1/svc{i:03d}/openapi.json"),),
            service_doc=(Target(f"https://docs.example.com/svc{i:03d}"),),
            status=(Target(f"https://status.example.com/svc{i:03d}"),),
        )
        for i in range(100)
    ]
    doc = build_catalog(CATALOG_URL, apis)
    body = json.dumps(doc, separators=(",", ":")).encode()
    header = to_link_header(doc).encode()
    assert len(list(iter_links(doc))) == 400
    assert len(body) == 28_283
    assert len(header) == 41_398
    assert len(header) > 16 * 1024
    assert len(zlib.compress(body, 9)) < len(body) // 5


# --- malformed input stays bounded and explicit ------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("doc", "path"),
    [
        ({"linkset": ["not-an-object"]}, "/linkset/0"),
        (
            {"linkset": [{"anchor": 7, "item": [{"href": "https://a.example"}]}]},
            "/linkset/0/anchor",
        ),
        ({"linkset": [{"item": ["https://a.example"]}]}, "/linkset/0/item/0"),
        (
            {"linkset": [{"item": [{"href": "https://a.example", "type": ["a/b"]}]}]},
            "/linkset/0/item/0/type",
        ),
        (
            {"linkset": [{"item": [{"href": "https://a.example", "foo": [1]}]}]},
            "/linkset/0/item/0/foo",
        ),
    ],
    ids=[
        "context-not-object",
        "anchor-not-string",
        "target-not-object",
        "type-array",
        "ext-non-string",
    ],
)
def test_validator_reports_wrong_json_types(doc: object, path: str) -> None:
    assert [v.path for v in validate_linkset(doc)] == [path]


@pytest.mark.unit
def test_validator_accepts_well_formed_attributes_and_extension_rels() -> None:
    target = {
        "href": "",
        "hreflang": ["en", "de"],
        "title*": [{"value": "Katalog", "language": "de"}],
        "foo": ["one", "two"],
    }
    doc = {"linkset": [{"anchor": CATALOG_URL, "https://example.com/rels/x": [target]}]}
    assert validate_linkset(doc) == []
    with pytest.raises(ValueError, match="star attributes"):
        to_link_format(doc)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        ("garbage", []),
        ("<https://a.example/x", []),
        ("<https://a.example/x> junk", []),
        ("<https://a.example/x>", []),
        (
            "<https://a.example/x>; rel=item junk",
            [Link(None, "item", "https://a.example/x"), Link(None, "junk", "https://a.example/x")],
        ),
        (
            '<https://a.example/x>; rel=item; title="ab\\',
            [Link(None, "item", "https://a.example/x", (("title", "ab"),))],
        ),
        (
            '<https://a.example/x>; rel=item; title="open',
            [Link(None, "item", "https://a.example/x", (("title", "open"),))],
        ),
        (
            "<https://a.example/x>; rel=item; flag",
            [Link(None, "item", "https://a.example/x", (("flag", ""),))],
        ),
        ("<https://a.example/x>; title=t", []),
    ],
    ids=[
        "empty",
        "no-angle",
        "unterminated",
        "junk-after-target",
        "no-params",
        "unquoted-token-runs-to-semicolon",
        "dangling-escape",
        "open-quote",
        "valueless",
        "no-rel",
    ],
)
def test_parser_fails_closed_on_malformed_values(value: str, expected: list[Link]) -> None:
    assert parse_link_header(value, base_url=None) == expected


@pytest.mark.unit
def test_titles_and_nested_only_catalogs_serialize() -> None:
    titled = ApiEntry(f"{DEV}/t", service_doc=(Target(f"{DEV}/t/doc", title="Docs"),))
    bare = ApiEntry(f"{DEV}/bare")
    doc = build_catalog(CATALOG_URL, [titled, bare])
    assert doc["linkset"] == [
        {"anchor": CATALOG_URL, "item": [{"href": f"{DEV}/t"}, {"href": f"{DEV}/bare"}]},
        {"anchor": f"{DEV}/t", "service-doc": [{"href": f"{DEV}/t/doc", "title": "Docs"}]},
    ]
    nested_only = build_catalog(CATALOG_URL, [], nested_catalogs=[IOT])
    assert nested_only == {"linkset": [{"anchor": CATALOG_URL, "api-catalog": [{"href": IOT}]}]}
    with pytest.raises(ValueError, match="nested catalog"):
        build_catalog(CATALOG_URL, [], nested_catalogs=["http://plain.example/c"])
    no_anchor = {"linkset": [{"item": [{"href": f"{DEV}/x"}]}]}
    assert to_link_format(no_anchor) == f'<{DEV}/x>; rel="item"'


@pytest.mark.unit
@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (" ", LINKSET_JSON),
        (", , application/linkset", LINKSET_TEXT),
        ("application/linkset; level", LINKSET_TEXT),
    ],
    ids=["blank", "empty-members", "valueless-param"],
)
def test_negotiate_tolerates_sloppy_accept(accept: str, expected: str) -> None:
    assert negotiate(accept) == expected


@pytest.mark.unit
def test_crawl_reports_each_rejected_api_once_and_ignores_unrelated_rels() -> None:
    doc = {
        "linkset": [
            {
                "anchor": ROOT,
                "item": [
                    {"href": "http://old.example.com/a"},
                    {"href": "http://old.example.com/a"},
                ],
                "license": [{"href": "https://www.example.com/terms"}],
            },
            {"anchor": "http://old.example.com/a", "status": [{"href": "https://s.example"}]},
        ]
    }
    result = crawl(ROOT, lambda _url: doc)
    assert result.apis == ()
    assert result.rejected == (("http://old.example.com/a", "not https (RFC 9727 Section 8)"),)


@pytest.mark.unit
def test_crawl_resolves_relative_links_and_anchorless_contexts_against_the_catalog_url() -> None:
    # RFC 8288 Section 3.2: without an anchor the context is the URL of the representation,
    # and relative references resolve against it (RFC 3986 Section 5).
    child = "https://www.example.com/catalogs/iot"
    store: dict[str, object] = {
        ROOT: {"linkset": [{"api-catalog": [{"href": "/catalogs/iot"}]}]},
        child: {
            "linkset": [
                {"item": [{"href": "v1"}], "status": [{"href": "https://status.example.com/iot"}]}
            ]
        },
    }
    result = crawl(ROOT, store.__getitem__)
    assert result.catalogs == (ROOT, child)
    assert result.apis == ("https://www.example.com/catalogs/v1", child)
    assert result.rejected == ()


@pytest.mark.unit
def test_extension_relation_uri_is_case_normalised_in_both_serializations() -> None:
    # RFC 8288 Section 2.1.2 compares extension relation types case-insensitively, and
    # Appendix B.2 step 17.1 lowercases them, so the JSON side must fold case the same way.
    doc = {
        "linkset": [{"anchor": CATALOG_URL, "https://Example.com/Rels/X": [{"href": f"{DEV}/x"}]}]
    }
    assert validate_linkset(doc) == []
    assert [link.rel for link in iter_links(doc)] == ["https://example.com/rels/x"]
    assert parse_link_header(to_link_header(doc), base_url=None) == list(iter_links(doc))


@pytest.mark.unit
def test_crawl_resolves_a_relative_anchor_against_the_catalog_url() -> None:
    # RFC 9264 Section 4.2.2 only says an anchor SHOULD NOT be relative, so a client resolves it.
    doc = {
        "linkset": [
            {"anchor": "/apis/foo", "service-desc": [{"href": "/apis/foo/openapi.json"}]},
        ]
    }
    result = crawl(ROOT, lambda _url: doc)
    assert result.apis == ("https://www.example.com/apis/foo",)
    assert result.rejected == ()


@pytest.mark.unit
def test_crawl_ignores_fragments_when_deciding_whether_a_catalog_was_visited() -> None:
    calls: list[str] = []
    store = {
        ROOT: _nested(ROOT, [f"{IOT}#a", f"{IOT}#b", f"{ROOT}#top"], []),
        IOT: _nested(IOT, [], ["https://apis.example.com/iot/v1"]),
    }

    def fetch(url: str) -> object:
        calls.append(url)
        return store[url]

    result = crawl(f"{ROOT}#start", fetch)
    assert calls == [ROOT, IOT]
    assert result.catalogs == (ROOT, IOT)
    assert result.rejected == ()


@pytest.mark.unit
def test_crawl_appendix_a1_needs_a_host_policy_for_the_second_domain() -> None:
    # RFC 9727 Appendix A.1 lists an API on apis.example.net in a catalog served by example.com.
    root = "https://example.com/.well-known/api-catalog"
    open_policy = crawl(root, lambda _url: RFC9727_A1)
    assert open_policy.apis == (
        "https://developer.example.com/apis/foo_api",
        "https://developer.example.com/apis/bar_api",
        "https://apis.example.net/apis/cantona_api",
    )
    strict = crawl(
        root,
        lambda _url: RFC9727_A1,
        allowed_hosts=frozenset({"example.com", "developer.example.com"}),
    )
    assert strict.apis == open_policy.apis[:2]
    assert strict.rejected == (
        (
            "https://apis.example.net/apis/cantona_api",
            "host not allowed (RFC 9264 Section 9 third-party links)",
        ),
    )


# --- the client: redirects (RFC 9727 Section 5.1) ------------------------------

NET = "https://www.example.net/.well-known/api-catalog"
ORG = "https://www.example.org/.well-known/api-catalog"


@pytest.mark.unit
def test_crawl_follows_a_redirect_and_resolves_links_against_the_final_url() -> None:
    calls: list[str] = []
    store: dict[str, object] = {
        NET: Redirect(ROOT),
        ROOT: {
            "linkset": [
                {"item": [{"href": "/apis/a"}], "api-catalog": [{"href": NET}, {"href": ROOT}]}
            ]
        },
    }

    def fetch(url: str) -> object:
        calls.append(url)
        return store[url]

    result = crawl(NET, fetch)
    # Both the redirecting URL and the URL it led to count as visited, so neither nested
    # link (back to NET, or the catalog's own self link) causes a second fetch.
    assert calls == [NET, ROOT]
    assert result.catalogs == (ROOT,)
    assert result.apis == ("https://www.example.com/apis/a",)
    assert result.rejected == ()


@pytest.mark.unit
def test_crawl_fetches_a_canonical_catalog_once_when_two_domains_redirect_to_it() -> None:
    calls: list[str] = []
    store: dict[str, object] = {
        ORG: _nested(ORG, [NET, ROOT], []),
        NET: Redirect("https://www.example.com/.well-known/api-catalog#latest"),
        ROOT: _nested(ROOT, [], ["https://www.example.com/a"]),
    }

    def fetch(url: str) -> object:
        calls.append(url)
        return store[url]

    result = crawl(ORG, fetch)
    assert calls == [ORG, NET, ROOT]
    assert result.catalogs == (ORG, ROOT)
    assert result.apis == ("https://www.example.com/a",)
    assert result.rejected == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("location", "allowed", "rejected"),
    [
        (
            "http://www.example.com/.well-known/api-catalog",
            None,
            ("http://www.example.com/.well-known/api-catalog", "not https (RFC 9727 Section 8)"),
        ),
        (
            "https://169.254.169.254/latest/meta-data/",
            frozenset({"www.example.net", "www.example.com"}),
            (
                "https://169.254.169.254/latest/meta-data/",
                "host not allowed (RFC 9264 Section 9 third-party links)",
            ),
        ),
    ],
    ids=["downgrade-to-http", "host-outside-policy"],
)
def test_crawl_applies_the_link_policy_to_every_redirect_hop(
    location: str, allowed: frozenset[str] | None, rejected: tuple[str, str]
) -> None:
    calls: list[str] = []

    def fetch(url: str) -> object:
        calls.append(url)
        return Redirect(location)

    result = crawl(NET, fetch, allowed_hosts=allowed)
    assert calls == [NET]
    assert result.catalogs == ()
    assert result.rejected == (rejected,)


@pytest.mark.unit
def test_crawl_bounds_redirect_chains_and_survives_a_redirect_loop() -> None:
    hops = [f"https://r{i}.example.com/api-catalog" for i in range(5)]
    chain: dict[str, object] = {url: Redirect(hops[i + 1]) for i, url in enumerate(hops[:-1])}
    chain[hops[-1]] = _nested(hops[-1], [], ["https://r4.example.com/a"])
    short = crawl(hops[0], chain.__getitem__, max_redirects=3)
    assert short.catalogs == ()
    assert short.rejected == ((hops[4], "redirect limit"),)
    enough = crawl(hops[0], chain.__getitem__, max_redirects=4)
    assert enough.catalogs == (hops[4],)

    calls: list[str] = []
    loop: dict[str, object] = {NET: Redirect(ORG), ORG: Redirect(NET)}

    def fetch(url: str) -> object:
        calls.append(url)
        return loop[url]

    looped = crawl(NET, fetch, max_redirects=10)
    assert calls == [NET, ORG]
    assert (looped.catalogs, looped.rejected) == ((), ())


@pytest.mark.unit
def test_crawl_over_asgi_follows_the_section_5_1_redirect_to_the_canonical_catalog() -> None:
    apps: dict[str, ASGIApp] = {
        "www.example.net": create_redirect_app(CATALOG_URL),
        "www.example.com": create_app(build_catalog(CATALOG_URL, [FOO, BAR]), CATALOG_URL),
    }

    async def get(url: str) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        transport = httpx.ASGITransport(app=apps[host])
        async with httpx.AsyncClient(transport=transport) as client:
            return await client.get(url, headers={"accept": LINKSET_JSON})

    def fetch(url: str) -> object:
        response = asyncio.run(get(url))  # httpx does not follow redirects unless asked to
        if response.is_redirect:
            return Redirect(response.headers["location"])
        if response.status_code != 200:
            raise FetchError(str(response.status_code))
        return response.json()

    result = crawl(NET, fetch, allowed_hosts=frozenset({"www.example.net", "www.example.com"}))
    assert result.catalogs == (CATALOG_URL,)
    assert result.apis == ()
    assert result.rejected == (
        (f"{DEV}/foo_api", "host not allowed (RFC 9264 Section 9 third-party links)"),
        (f"{DEV}/bar_api", "host not allowed (RFC 9264 Section 9 third-party links)"),
    )
    wider = frozenset({"www.example.net", "www.example.com", "developer.example.com"})
    assert crawl(NET, fetch, allowed_hosts=wider).apis == (f"{DEV}/foo_api", f"{DEV}/bar_api")


# --- real servers on loopback: who strips a HEAD body the app leaks ------------

LEAK = b"hello world"


async def _leaky_head_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Negative control: sends its 11-byte body for every method, HEAD included."""
    del receive
    if scope["type"] != "http":
        return
    headers = [(b"content-type", b"text/plain"), (b"content-length", b"11")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": LEAK})


@dataclass(frozen=True, slots=True)
class _HeadResult:
    status: int
    content_length: str | None
    link: str | None
    body_bytes: int


def _listening_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)  # connects queue in the backlog before the server starts accepting
    return sock


async def _head_http11(port: int) -> _HeadResult:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        f"HEAD {WELL_KNOWN_PATH} HTTP/1.1\r\nhost: www.example.com\r\n"
        f"accept: {LINKSET_JSON}\r\nconnection: close\r\n\r\n"
    )
    writer.write(request.encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, rest = data.partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    fields = {name.lower(): value.strip() for name, _, value in (ln.partition(":") for ln in lines)}
    status = int(status_line.split()[1])
    return _HeadResult(status, fields.get("content-length"), fields.get("link"), len(rest))


async def _head_h2c(port: int) -> _HeadResult:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    request = [
        (":method", "HEAD"),
        (":scheme", "http"),
        (":authority", "www.example.com"),
        (":path", WELL_KNOWN_PATH),
        ("accept", LINKSET_JSON),
    ]
    conn.send_headers(1, request, end_stream=True)
    writer.write(conn.data_to_send())
    fields: dict[str, str] = {}
    body_bytes = 0
    buffer = b""
    ended = False
    while not ended:
        data = await reader.read(65535)
        if not data:
            break
        buffer += data
        # Feed one frame at a time so the HEADERS event is seen before a DATA frame is refused.
        while len(buffer) >= 9 and len(buffer) >= 9 + int.from_bytes(buffer[:3]):
            size = 9 + int.from_bytes(buffer[:3])
            frame, buffer = buffer[:size], buffer[size:]
            try:
                events = conn.receive_data(frame)
            except h2.exceptions.InvalidBodyLengthError as exc:
                # h2 4.4.1 expects 0 content bytes in a HEAD response and raises on the DATA frame.
                body_bytes, ended = exc.actual_length, True
                break
            for event in events:
                if isinstance(event, h2.events.ResponseReceived):
                    fields = {bytes(k).decode(): bytes(v).decode() for k, v in event.headers}
                ended = ended or isinstance(event, h2.events.StreamEnded)
        writer.write(conn.data_to_send())
    writer.close()
    await writer.wait_closed()
    status = int(fields[":status"])
    return _HeadResult(status, fields.get("content-length"), fields.get("link"), body_bytes)


async def _with_hypercorn[T](app: ASGIApp, client: Callable[[int], Awaitable[T]]) -> T:
    sock = _listening_socket()
    port = sock.getsockname()[1]
    config = Config()
    config.bind = [f"fd://{sock.detach()}"]  # Hypercorn now owns and closes the descriptor
    config.accesslog = None
    config.errorlog = None
    stop = asyncio.Event()
    server = asyncio.create_task(serve(app, config, shutdown_trigger=stop.wait))  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(10):
            return await client(port)
    finally:
        stop.set()
        await server


async def _with_uvicorn[T](
    app: ASGIApp, client: Callable[[int], Awaitable[T]], http: str = "h11"
) -> T:
    sock = _listening_socket()
    config = uvicorn.Config(app, http=http, lifespan="off", log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            return await client(sock.getsockname()[1])
    finally:
        server.should_exit = True
        await task
        sock.close()


async def _with_granian[T](
    app: ASGIApp, client: Callable[[int], Awaitable[T]], http: HTTPModes = HTTPModes.http1
) -> T:
    # Granian binds in Rust, so pick a free loopback port and retry until it listens.
    probe = _listening_socket()
    port = probe.getsockname()[1]
    probe.close()
    server = GranianServer(
        app,
        address="127.0.0.1",
        port=port,
        interface=Interfaces.ASGINL,
        http=http,
        websockets=False,
        log_enabled=False,
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    return await client(port)
                except ConnectionRefusedError:
                    await asyncio.sleep(0.01)
    finally:
        server.stop()  # type: ignore[no-untyped-call]
        await task


async def _head_through(server: str, app: ASGIApp) -> _HeadResult:
    match server:
        case "uvicorn-h11":
            return await _with_uvicorn(app, _head_http11, "h11")
        case "uvicorn-httptools":
            return await _with_uvicorn(app, _head_http11, "httptools")
        case "hypercorn-http11":
            return await _with_hypercorn(app, _head_http11)
        case "hypercorn-h2c":
            return await _with_hypercorn(app, _head_h2c)
        case "granian-http11":
            return await _with_granian(app, _head_http11, HTTPModes.http1)
        case "granian-h2c":
            return await _with_granian(app, _head_h2c, HTTPModes.http2)
        case _:
            raise AssertionError(server)


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "leaked_bytes"),
    [
        ("uvicorn-h11", 0),
        ("uvicorn-httptools", 0),
        ("hypercorn-http11", 0),
        ("hypercorn-h2c", 0),
        ("granian-http11", 0),
        ("granian-h2c", len(LEAK)),
    ],
)
async def test_only_granian_over_http2_puts_a_leaked_head_body_on_the_wire(
    server: str, leaked_bytes: int
) -> None:
    result = await _head_through(server, _leaky_head_app)
    assert result == _HeadResult(200, "11", None, leaked_bytes)


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server",
    [
        "uvicorn-h11",
        "uvicorn-httptools",
        "hypercorn-http11",
        "hypercorn-h2c",
        "granian-http11",
        "granian-h2c",
    ],
)
async def test_lab_app_head_is_clean_on_every_pinned_server_and_http_version(server: str) -> None:
    doc = build_catalog(CATALOG_URL, [FOO, BAR])
    length = len(json.dumps(doc, separators=(",", ":")).encode())
    result = await _head_through(server, create_app(doc, CATALOG_URL))
    assert result == _HeadResult(200, str(length), f'<{CATALOG_URL}>; rel="api-catalog"', 0)
