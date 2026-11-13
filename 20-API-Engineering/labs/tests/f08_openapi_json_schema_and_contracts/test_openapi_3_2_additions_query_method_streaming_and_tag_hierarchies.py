"""Tests for the OpenAPI 3.2.1 additions: QUERY, itemSchema streams, tag trees, OAuth metadata."""

from __future__ import annotations

from collections.abc import AsyncIterator  # noqa: TC003 - FastAPI resolves it at runtime
from typing import TYPE_CHECKING, Any

import h11
import httptools
import httpx
import pytest
from fastapi import FastAPI
from openapi_spec_validator import OpenAPIV31SpecValidator, OpenAPIV32SpecValidator
from openapi_spec_validator.versions.consts import OPENAPIV32
from openapi_spec_validator.versions.shortcuts import get_spec_version
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route

from api_labs.f08_openapi_json_schema_and_contracts.openapi_3_2_additions import (
    DEVICE_GRANT,
    ISSUER,
    build_document,
    content_errors,
    create_app,
    decode_json_seq,
    decode_jsonl,
    decode_sse,
    item_errors,
    lint_oas32,
    metadata_drift,
    negotiate,
    tag_paths,
    well_known_metadata_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

ITEM = "/components/schemas/FeedItem"
EVENT = "/components/schemas/FeedEvent"
MATCHES = [{"id": 1, "title": "alpha"}, {"id": 3, "title": "alphabet"}]
# The text/event-stream document and its JSON Lines equivalent from OpenAPI 3.2.1 Section 4.14.6.3.
SPEC_SSE = (
    b"event: addString\ndata: This data is formatted\ndata: across two lines\nretry: 5\n\n"
    b"event: addInt64\ndata: 1234\nunknownField: this is ignored\n\n"
    b': This is a comment\nevent: addJSON\ndata: {"foo": 42}\n\n'
)
SPEC_JSONL = (
    b'{"event": "addString", "data": "This data is formatted\\nacross two lines", "retry": 5}\n'
    b'{"event": "addInt64", "data": "1234"}\n'
    b'{"event": "addJSON", "data": "{\\"foo\\": 42}"}\n'
)


def spec_errors(document: dict[str, Any], validator: type[Any] = OpenAPIV32SpecValidator) -> str:
    return "\n".join(error.message for error in validator(document).iter_errors())


def client(app: Any, **options: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", **options)


def http_metadata_url(document: dict[str, Any]) -> None:
    scheme = document["components"]["securitySchemes"]["feed_oauth"]
    scheme["oauth2MetadataUrl"] = "http://auth.example.com/.well-known/oauth-authorization-server"


def lowercase_fixed_method(document: dict[str, Any]) -> None:
    operation = {"responses": {"204": {"summary": "done"}}}
    document["paths"]["/feed"]["additionalOperations"]["post"] = operation


def item_schema_on_json(document: dict[str, Any]) -> None:
    content = document["paths"]["/feed"]["query"]["responses"]["200"]["content"]
    content["application/json"] = {"itemSchema": {"$ref": f"#{ITEM}"}}


def path_level_query_parameter(document: dict[str, Any]) -> None:
    parameter = {"name": "tenant", "in": "query", "schema": {"type": "string"}}
    document["paths"]["/feed"]["parameters"] = [parameter]


@pytest.mark.unit
def test_document_validates_as_3_2_and_fails_as_3_1() -> None:
    document = build_document()

    assert spec_errors(document) == ""
    assert get_spec_version(document) == OPENAPIV32

    document["openapi"] = "3.1.2"
    as_3_1 = spec_errors(document, OpenAPIV31SpecValidator)
    for field in ("query", "additionalOperations", "parent", "kind", "querystring"):
        assert f"'{field}'" in as_3_1 or f'"{field}"' in as_3_1
    assert "'deviceAuthorization' was unexpected" in as_3_1
    assert "'oauth2MetadataUrl'" in as_3_1


@pytest.mark.unit
def test_tag_paths_resolve_parent_chain_root_first() -> None:
    paths = tag_paths(build_document()["tags"])

    assert paths["feed-streaming"] == ("catalog", "feed", "feed-streaming")
    assert paths["partner"] == ("partner",)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("parents", "lab_message", "validator_message"),
    [
        ({"feed": "missing"}, "unknown parent 'missing'", "unknown parent tag 'missing'"),
        ({"catalog": "feed-streaming"}, "circular tag hierarchy", "Circular tag hierarchy"),
        ({"partner": "partner"}, "partner -> partner", "partner -> partner"),
    ],
    ids=["unknown-parent", "three-node-cycle", "self-parent"],
)
def test_tag_hierarchy_faults_are_rejected_by_lab_and_pinned_validator(
    parents: dict[str, str], lab_message: str, validator_message: str
) -> None:
    document = build_document()
    for tag in document["tags"]:
        if tag["name"] in parents:
            tag["parent"] = parents[tag["name"]]

    with pytest.raises(ValueError, match=lab_message):
        tag_paths(document["tags"])
    assert validator_message in spec_errors(document)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutate", "finding"),
    [
        (http_metadata_url, "securitySchemes.feed_oauth: oauth2MetadataUrl requires TLS"),
        (lowercase_fixed_method, "/feed: additionalOperations 'post' belongs in the 'post'"),
        (item_schema_on_json, "QUERY /feed: itemSchema on non-sequential application/json"),
        (path_level_query_parameter, "GET /feed: in=query and in=querystring are exclusive"),
    ],
    ids=["http-metadata-url", "lowercase-post", "itemschema-on-json", "cross-level-query"],
)
def test_lint_catches_rules_the_pinned_validator_accepts(
    mutate: Callable[[dict[str, Any]], None], finding: str
) -> None:
    document = build_document()
    assert lint_oas32(document) == []

    mutate(document)

    assert spec_errors(document) == ""
    assert any(line.startswith(finding) for line in lint_oas32(document))


@pytest.mark.unit
def test_pinned_schema_rejects_fixed_method_and_querystring_misuse() -> None:
    operation = {"responses": {"204": {"summary": "done"}}}
    uppercase = build_document()
    uppercase["paths"]["/feed"]["additionalOperations"]["POST"] = operation
    with_schema = build_document()
    with_schema["paths"]["/feed"]["get"]["parameters"][0] = {
        "name": "filter",
        "in": "querystring",
        "schema": {"type": "string"},
    }
    same_array = build_document()
    same_array["paths"]["/feed"]["get"]["parameters"].append(
        {"name": "tenant", "in": "query", "schema": {"type": "string"}}
    )

    assert 'is not allowed for "POST"' in spec_errors(uppercase)
    assert '"content" is a required property' in spec_errors(with_schema)
    assert spec_errors(same_array) != ""
    assert lint_oas32(same_array) == ["GET /feed: in=query and in=querystring are exclusive"]


@pytest.mark.unit
def test_decode_sse_reproduces_the_openapi_specification_example() -> None:
    assert decode_sse(SPEC_SSE) == decode_jsonl(SPEC_JSONL)


@pytest.mark.unit
def test_decode_sse_handles_bom_line_endings_and_unterminated_event() -> None:
    stream = "\ufeffdata: a\r\n\r\ndata: b\rid: 7\r\rid: x\x00y\ndata: c\n\ndata: lost\n".encode()

    assert decode_sse(stream) == [{"data": "a"}, {"data": "b", "id": "7"}, {"data": "c"}]
    assert decode_sse(b"retry: 5s\nevent:\ndata\n\n") == [{"data": ""}]
    assert decode_sse(b"data:x\n\ndata:  y\n\n") == [{"data": "x"}, {"data": " y"}]
    assert decode_sse(b"event: first\nevent:\ndata: z\n\n") == [{"data": "z"}]
    assert decode_sse(b"id: 7\ndata: a\n\ndata: b\n\n") == [{"id": "7", "data": "a"}, {"data": "b"}]
    assert decode_sse(b"data: \xff\n\n") == [{"data": "\ufffd"}]


@pytest.mark.unit
def test_decode_json_seq_drops_truncated_and_malformed_texts() -> None:
    sequence = (
        b'{"lead":0}\n\x1e{"id":1}\n\x1e123\x1e123\n\x1e\x1e{"id":\n\x1e"ok"\x1etrue\x1eNaN\n'
    )

    assert decode_json_seq(sequence) == ([{"id": 1}, 123, "ok"], 5)
    assert decode_json_seq(b"") == ([], 0)


@pytest.mark.unit
def test_decode_jsonl_accepts_final_newline_and_rejects_blank_line() -> None:
    assert decode_jsonl(b'{"id":1}\n2\n') == [{"id": 1}, 2]
    with pytest.raises(ValueError, match="Expecting value"):
        decode_jsonl(b'{"id":1}\n\n{"id":2}\n')
    with pytest.raises(ValueError, match="NaN is not JSON"):
        decode_jsonl(b"NaN\n")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_type", "wire"),
    [
        ("application/jsonl", b'{"id":1,"title":"alpha"}\n{"id":3,"title":"alphabet"}\n'),
        (
            "application/json-seq",
            b'\x1e{"id":1,"title":"alpha"}\n\x1e{"id":3,"title":"alphabet"}\n',
        ),
    ],
    ids=["jsonl", "json-seq"],
)
async def test_query_stream_items_validate_against_item_schema(
    media_type: str, wire: bytes
) -> None:
    document = build_document()
    async with client(create_app()) as http:
        response = await http.request(
            "QUERY", "/feed", json={"q": "alpha"}, headers={"accept": media_type}
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["accept-query"] == "application/json"
    assert response.content == wire
    if media_type == "application/jsonl":
        items = decode_jsonl(response.content)
    else:
        items, dropped = decode_json_seq(response.content)
        assert dropped == 0
    assert items == MATCHES
    assert item_errors(document, ITEM, items) == []
    assert item_errors(document, ITEM, [*items, {"id": 0, "title": "zero"}]) == [
        "item[2]: 0 is less than the minimum of 1"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sse_item_schema_needs_explicit_content_schema_assertion() -> None:
    document = build_document()
    async with client(create_app()) as http:
        response = await http.request(
            "QUERY", "/feed", json={"q": "beta"}, headers={"accept": "text/event-stream"}
        )

    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    events = decode_sse(response.content)
    assert events == [{"event": "item", "id": "2", "data": '{"id":2,"title":"beta"}'}]
    assert item_errors(document, EVENT, events) == []
    content = f"{EVENT}/properties/data/contentSchema"
    assert content_errors(document, content, [event["data"] for event in events]) == []

    bad = [{"event": "item", "data": '{"id":0,"title":"zero"}'}, {"event": "item", "data": "{"}]
    assert item_errors(document, EVENT, bad) == []
    assert content_errors(document, content, [event["data"] for event in bad]) == [
        "item[0]: 0 is less than the minimum of 1",
        "item[1]: content is not application/json",
    ]
    assert content_errors(document, content, ["{", '{"id":0,"title":"zero"}']) == [
        "item[0]: content is not application/json",
        "item[1]: 0 is less than the minimum of 1",
    ]
    assert item_errors(document, EVENT, [{"event": "other", "data": "{}"}]) == [
        "item[0]: 'item' was expected"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "headers", "status"),
    [
        (b'{"q":"alpha"}', {}, 400),
        (b"q=alpha", {"content-type": "application/x-www-form-urlencoded"}, 415),
        (b'{"q":', {"content-type": "application/json"}, 400),
        (b'{"q":""}', {"content-type": "application/json"}, 422),
        (b'{"q":"alpha"}', {"content-type": "application/json", "accept": "text/csv"}, 406),
        (b'{"q":"alpha"}', {"content-type": "application/json; charset=utf-8"}, 200),
        (b'{"q":"alpha","limit":NaN}', {"content-type": "application/json"}, 400),
        (b"[" * 100_000, {"content-type": "application/json"}, 400),
        (b'{"q":"alpha","limit":1.0}', {"content-type": "application/json"}, 200),
    ],
    ids=[
        "no-content-type",
        "unsupported-format",
        "not-json",
        "unprocessable",
        "accept",
        "ok",
        "nan-constant",
        "runaway-nesting",
        "float-limit",
    ],
)
async def test_query_failures_use_the_rfc_10008_status_codes(
    content: bytes, headers: dict[str, str], status: int
) -> None:
    async with client(create_app()) as http:
        response = await http.request("QUERY", "/feed", content=content, headers=headers)

    assert response.status_code == status
    assert response.headers["accept-query"] == "application/json"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("accept", "chosen"),
    [
        ("text/csv, application/json", "application/json"),
        ("application/*", "application/json"),
        ("application/json;q=0", None),
        ("application/json;q=0, */*", "application/jsonl"),
        ("text/event-stream;q=0.1, application/jsonl", "application/jsonl"),
        ("*/*;q=0.5, text/event-stream", "text/event-stream"),
        ("application/json;q=bogus, application/json-seq;q=0.2", "application/json-seq"),
        ("text/csv", None),
    ],
)
def test_negotiate_honours_weights_wildcards_and_server_preference(
    accept: str, chosen: str | None
) -> None:
    assert negotiate(accept) == chosen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_querystring_and_query_content_select_the_same_representation() -> None:
    async with client(create_app()) as http:
        by_get = await http.get("/feed?q=alpha&limit=1")
        by_query = await http.request("QUERY", "/feed", json={"q": "alpha", "limit": 1})
        head = await http.head("/feed?q=alpha")
        purge = await http.request("PURGE", "/feed")
        post = await http.post("/feed", json={"q": "alpha"})

    assert by_get.json() == by_query.json() == [{"id": 1, "title": "alpha"}]
    assert head.headers["accept-query"] == "application/json"
    assert purge.status_code == 204
    assert post.status_code == 405
    assert set(post.headers["allow"].split(", ")) == {"GET", "HEAD", "QUERY", "PURGE"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("issuer", "expected"),
    [
        ("https://example.com", "https://example.com/.well-known/oauth-authorization-server"),
        (
            "https://example.com/issuer1",
            "https://example.com/.well-known/oauth-authorization-server/issuer1",
        ),
        (
            "https://example.com/issuer1/",
            "https://example.com/.well-known/oauth-authorization-server/issuer1",
        ),
    ],
    ids=["no-path", "path", "trailing-slash"],
)
def test_well_known_url_inserts_suffix_between_host_and_path(issuer: str, expected: str) -> None:
    assert well_known_metadata_url(issuer) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "issuer",
    [
        "http://example.com",
        "https://example.com?tenant=a",
        "https://example.com?",
        "https://example.com#frag",
        "",
    ],
    ids=["http", "query", "empty-query", "fragment", "empty"],
)
def test_well_known_url_rejects_invalid_issuers(issuer: str) -> None:
    with pytest.raises(ValueError, match="issuer must be an https URL"):
        well_known_metadata_url(issuer)


@pytest.mark.unit
def test_metadata_drift_compares_flows_with_authorization_server_metadata() -> None:
    scheme = build_document()["components"]["securitySchemes"]["feed_oauth"]
    metadata: dict[str, Any] = {
        "issuer": ISSUER,
        "token_endpoint": f"{ISSUER}/token",
        "device_authorization_endpoint": f"{ISSUER}/device_authorization",
        "grant_types_supported": [DEVICE_GRANT, "refresh_token"],
    }
    assert metadata_drift(scheme, metadata) == []

    impostor = {**metadata, "issuer": "https://auth.example.com/tenants/evil"}
    moved = {**metadata, "token_endpoint": f"{ISSUER}/v2/token"}
    defaults = {key: value for key, value in metadata.items() if key != "grant_types_supported"}

    assert metadata_drift(scheme, impostor) == [
        "issuer does not match the URL the metadata was retrieved from"
    ]
    assert metadata_drift(scheme, moved) == [
        "deviceAuthorization.tokenUrl differs from token_endpoint"
    ]
    assert metadata_drift(scheme, defaults) == [
        "deviceAuthorization flow is not in grant_types_supported"
    ]
    for unusable in ({**metadata, "issuer": "http://auth.example.com/tenants/acme"}, {}):
        assert metadata_drift(scheme, unusable)[0] == (
            "issuer does not match the URL the metadata was retrieved from"
        )


class Search(BaseModel):
    q: str


class Hit(BaseModel):
    id: int


def code_first_app(method: str) -> FastAPI:
    app = FastAPI(title="code-first", version="1")

    @app.api_route("/search", methods=[method])
    async def search(body: Search) -> AsyncIterator[Hit]:  # pyright: ignore[reportUnusedFunction]
        yield Hit(id=len(body.q))

    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_query_route_drops_request_body_and_mislabels_version() -> None:
    app = code_first_app("QUERY")
    document = app.openapi()
    operation = document["paths"]["/search"]["query"]
    posted = code_first_app("POST").openapi()["paths"]["/search"]["post"]

    assert document["openapi"] == "3.1.0"
    assert "requestBody" not in operation
    assert posted["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Search"
    }
    assert operation["responses"]["200"]["content"] == {
        "application/jsonl": {"itemSchema": {"$ref": "#/components/schemas/Hit"}}
    }
    assert "'query' was unexpected" in spec_errors(document, OpenAPIV31SpecValidator)
    assert "'itemSchema' was unexpected" in spec_errors(
        code_first_app("GET").openapi(), OpenAPIV31SpecValidator
    )
    document["openapi"] = "3.2.1"
    assert spec_errors(document) == ""
    extension = code_first_app("PROPFIND").openapi()
    extension["openapi"] = "3.2.1"
    assert list(extension["paths"]["/search"]) == ["propfind"]
    assert "'propfind' was unexpected" in spec_errors(extension)

    async with client(app) as http:
        accepted = await http.request("QUERY", "/search", json={"q": "abc"})
        rejected = await http.request("QUERY", "/search", json={})
    assert accepted.text == '{"id":3}\n'
    assert rejected.status_code == 422


class UrlRecorder:
    def __init__(self) -> None:
        self.url = b""

    def on_url(self, url: bytes) -> None:
        self.url += url


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "httptools_accepts"),
    [("QUERY", True), ("PURGE", True), ("FROBNICATE", False)],
)
def test_http_parsers_differ_on_extension_methods(method: str, httptools_accepts: bool) -> None:
    request = f"{method} /feed HTTP/1.1\r\nHost: example.org\r\n\r\n".encode()

    connection = h11.Connection(h11.SERVER)
    connection.receive_data(request)
    event = connection.next_event()
    assert isinstance(event, h11.Request)
    assert event.method == method.encode()

    recorder = UrlRecorder()
    parser = httptools.HttpRequestParser(recorder)
    if httptools_accepts:
        parser.feed_data(request)
        assert parser.get_method() == method.encode()
        assert recorder.url == b"/feed"
    else:
        with pytest.raises(httptools.HttpParserInvalidMethodError):
            parser.feed_data(request)
        assert issubclass(httptools.HttpParserInvalidMethodError, httptools.HttpParserError)


async def echo(request: Request) -> PlainTextResponse:
    return PlainTextResponse(f"{request.method}:{(await request.body()).decode()}")


async def redirect(request: Request) -> RedirectResponse:
    return RedirectResponse("/echo", status_code=int(request.path_params["status"]))


def redirect_app(*middleware: Middleware) -> Starlette:
    routes = [
        Route("/echo", echo, methods=["GET", "QUERY"]),
        Route("/redirect/{status}", redirect, methods=["QUERY"]),
    ]
    return Starlette(routes=routes, middleware=list(middleware))


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "replayed"),
    [(301, "QUERY:q=1"), (302, "GET:"), (303, "GET:"), (307, "QUERY:q=1"), (308, "QUERY:q=1")],
)
async def test_httpx_redirect_rewrites_query_to_get_on_302(status: int, replayed: str) -> None:
    async with client(redirect_app(), follow_redirects=True) as http:
        response = await http.request(
            "QUERY", f"/redirect/{status}", content=b"q=1", headers={"content-type": "text/plain"}
        )

    assert response.text == replayed


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_methods", "status", "body"),
    [(("GET",), 400, "Disallowed CORS method"), (("GET", "QUERY"), 200, "OK")],
    ids=["default-allow-methods", "query-allowed"],
)
async def test_cors_preflight_for_query_requires_explicit_allow_methods(
    allow_methods: tuple[str, ...], status: int, body: str
) -> None:
    cors = Middleware(
        CORSMiddleware, allow_origins=["https://app.example"], allow_methods=allow_methods
    )
    preflight = {"origin": "https://app.example", "access-control-request-method": "QUERY"}
    async with client(redirect_app(cors)) as http:
        response = await http.options("/echo", headers=preflight)

    assert response.status_code == status
    assert response.text == body
    assert response.headers["access-control-allow-methods"] == ", ".join(allow_methods)
