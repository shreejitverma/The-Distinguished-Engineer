"""Unit tests for FastAPI parameter sources, parameter models, and body embedding."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from starlette.types import Message

from api_labs.f11_fastapi_internals_and_patterns.param_validation import (
    Item,
    build_param_app,
    param_sources,
)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = build_param_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as http_client:
        yield http_client


def _errors(response: httpx.Response) -> list[tuple[str, list[Any]]]:
    return [(error["type"], error["loc"]) for error in response.json()["detail"]]


@pytest.mark.unit
def test_signature_inference_routes_scalars_to_query_and_lists_to_body() -> None:
    async def infer(item_id: int, q: str, ids: list[int], item: Item, flag: bool = False) -> None:
        return None

    assert param_sources(infer, "/infer/{item_id}") == {
        "item_id": "path",
        "q": "query",
        "flag": "query",
        "ids": "body",
        "item": "body",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_query_keys_build_a_list_and_bool_is_lax(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/items/7?tags=a&tags=b&verbose=yes")

    assert response.status_code == 200
    assert response.json() == {"item_id": 7, "tags": ["a", "b"], "verbose": True}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_errors_from_every_source_are_collected_in_one_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/items/0?verbose=maybe")

    assert response.status_code == 422
    assert _errors(response) == [
        ("greater_than_equal", ["path", "item_id"]),
        ("bool_parsing", ["query", "verbose"]),
    ]
    assert response.json()["detail"][0]["input"] == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_dependency_runs_even_when_endpoint_params_fail() -> None:
    calls: list[str] = []
    app = build_param_app(dependency_calls=calls)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as http_client:
        response = await http_client.get("/audited?n=abc", headers={"x-tenant": "acme"})

    assert response.status_code == 422
    assert _errors(response) == [("int_parsing", ["query", "n"])]
    assert calls == ["acme"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_percent_encoded_slash_in_path_param_is_a_404_not_a_422(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/items/a%2Fb")

    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_underscores_become_hyphens_and_values_are_not_comma_split(
    client: httpx.AsyncClient,
) -> None:
    ok = await client.get("/headers", headers={"X-Request-ID": "r1", "x-hop": "a, b"})
    underscored = await client.get("/headers", headers={"x_request_id": "r1", "x-hop": "a"})

    assert ok.status_code == 200
    assert ok.json() == {"x_request_id": "r1", "x_hop": ["a, b"], "legacy_key": None}
    assert underscored.status_code == 422
    assert _errors(underscored) == [("missing", ["header", "x-request-id"])]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_header_lines_become_list_items(client: httpx.AsyncClient) -> None:
    headers = [("x-request-id", "r1"), ("x-hop", "edge"), ("x-hop", "mesh")]

    response = await client.get("/headers", headers=headers)

    assert response.json()["x_hop"] == ["edge", "mesh"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_convert_underscores_false_reads_the_literal_underscore_header(
    client: httpx.AsyncClient,
) -> None:
    headers = {"x-request-id": "r1", "x-hop": "a", "legacy_key": "k1", "legacy-key": "k2"}

    response = await client.get("/headers", headers=headers)

    assert response.json()["legacy_key"] == "k1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_lookup_relies_on_the_server_lowercasing_names() -> None:
    app = build_param_app()
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    async def call(raw_headers: list[tuple[bytes, bytes]]) -> int:
        sent.clear()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/headers",
            "raw_path": b"/headers",
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "server": ("test", 80),
            "client": ("127.0.0.1", 1),
        }
        await app(scope, receive, send)
        return int(sent[0]["status"])

    assert await call([(b"x-request-id", b"r1"), (b"x-hop", b"a")]) == 200
    assert await call([(b"X-Request-ID", b"r1"), (b"X-Hop", b"a")]) == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_model_collects_lists_and_forbids_unknown_keys(
    client: httpx.AsyncClient,
) -> None:
    ok = await client.get("/search?limit=5&tags=x&tags=y")
    extra = await client.get("/search?limit=5&tool=plumbus")
    too_big = await client.get("/search?limit=500")

    assert ok.json() == {"limit": 5, "order_by": "created_at", "tags": ["x", "y"]}
    assert extra.status_code == 422
    assert extra.json()["detail"] == [
        {
            "type": "extra_forbidden",
            "loc": ["query", "tool"],
            "msg": "Extra inputs are not permitted",
            "input": "plumbus",
        }
    ]
    assert _errors(too_big) == [("less_than_equal", ["query", "limit"])]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_optional_header_model_is_none_unless_one_of_its_headers_arrives(
    client: httpx.AsyncClient,
) -> None:
    absent = await client.get("/trace")
    hyphen = await client.get("/trace", headers=[("x-trace-id", "t1"), ("x-hop", "a")])
    underscore = await client.get("/trace", headers={"x_trace_id": "t1"})

    assert absent.json() == {"headers": None}
    assert hyphen.json() == {"headers": {"x_trace_id": "t1", "x_hop": ["a"]}}
    assert underscore.json() == {"headers": None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cookie_model_takes_last_duplicate_and_forbids_unknown_cookies(
    client: httpx.AsyncClient,
) -> None:
    duplicate = await client.get("/session", headers={"cookie": "session_id=s1; session_id=s2"})
    extra = await client.get("/session", headers={"cookie": "session_id=s1; tracker=z"})

    assert duplicate.json() == {"session_id": "s2", "theme": "light"}
    assert _errors(extra) == [("extra_forbidden", ["cookie", "tracker"])]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_query_int_rejects_every_wire_value(client: httpx.AsyncClient) -> None:
    response = await client.get("/strict?n=5")

    assert response.status_code == 422
    assert _errors(response) == [("int_type", ["query", "n"])]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_model_body_is_bare_but_embed_requires_a_wrapping_key(
    client: httpx.AsyncClient,
) -> None:
    item = {"name": "a", "price": 1}

    bare = await client.post("/single", json=item)
    embedded_bare = await client.post("/embedded", json=item)
    embedded_wrapped = await client.post("/embedded", json={"item": item})

    assert bare.json() == {"name": "a", "price": 1.0}
    assert _errors(embedded_bare) == [("missing", ["body", "item"])]
    assert embedded_wrapped.json() == {"name": "a", "price": 1.0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_body_params_embed_by_parameter_name(client: httpx.AsyncClient) -> None:
    body = {"item": {"name": "a", "price": 1}, "user": {"username": "u"}, "importance": 3}

    ok = await client.post("/multi", json=body)
    bad = await client.post("/multi", json={**body, "importance": 0})

    assert ok.json() == {"item": "a", "user": "u", "importance": 3}
    assert _errors(bad) == [("greater_than", ["body", "importance"])]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_scalar_body_param_is_the_whole_json_document(
    client: httpx.AsyncClient,
) -> None:
    bare = await client.post("/scalar", json=5)
    wrapped = await client.post("/scalar", json={"count": 5})

    assert bare.json() == {"count": 5}
    assert _errors(wrapped) == [("int_type", ["body"])]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_json_is_parsed_only_for_json_media_types(client: httpx.AsyncClient) -> None:
    payload = json.dumps({"name": "a", "price": 1}).encode()

    missing_type = await client.post("/single", content=payload)
    vendor_json = await client.post(
        "/single",
        content=payload,
        headers={"content-type": "application/vnd.api+json; charset=utf-8"},
    )
    broken = await client.post(
        "/single", content=b'{"name":', headers={"content-type": "application/json"}
    )

    assert _errors(missing_type) == [("model_attributes_type", ["body"])]
    assert vendor_json.json() == {"name": "a", "price": 1.0}
    assert _errors(broken) == [("json_invalid", ["body", 8])]
