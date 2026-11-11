"""Lab: the HTTP QUERY method (RFC 10008) from the wire parser up to the OpenAPI export."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import http
import json
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

import h2.config
import h2.connection
import h2.events
import h11
import httptools
import httpx
import hypercorn.asyncio
import hypercorn.config
import pytest
import uvicorn
from openapi_spec_validator import OpenAPIV31SpecValidator, OpenAPIV32SpecValidator
from starlette.middleware.cors import CORSMiddleware
from urllib3.util.retry import Retry

from api_labs.f04_rest_and_resource_modeling.query_method import (
    ContactQuery,
    ContactStore,
    accept_query_value,
    build_app,
    build_fastapi_app,
    openapi_32_with_query_body,
    query_cache_key,
    query_retry_policy,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import ASGIApp

CONTACTS = [
    {"surname": "Smith", "givenname": "John", "email": "smith@example.org"},
    {"surname": "Jones", "givenname": "Sally", "email": "sally.jones@example.com"},
    {"surname": "Dubois", "givenname": "Camille", "email": "camille.dubois@example.net"},
]
JSON = {"Content-Type": "application/json"}


def make_store() -> ContactStore:
    return ContactStore(contacts=[dict(c) for c in CONTACTS], secret=b"lab-secret")


def client_for(store: ContactStore) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=build_app(store))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_runs_body_and_names_equivalent_resource() -> None:
    async with client_for(make_store()) as client:
        resp = await client.request(
            "QUERY", "/contacts", content=b'{"surname_prefix": "D"}', headers=JSON
        )
    assert resp.status_code == 200
    assert resp.json() == [{"surname": "Dubois", "email": "camille.dubois@example.net"}]
    assert resp.headers["location"].startswith("/contacts/stored-queries/")
    assert resp.headers["accept-query"] == "application/json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retried_query_is_safe_and_lands_on_the_same_stored_query() -> None:
    store = make_store()
    async with client_for(store) as client:
        first = await client.request(
            "QUERY", "/contacts", content=b'{"limit": 2, "surname_prefix": ""}', headers=JSON
        )
        retry = await client.request(
            "QUERY", "/contacts", content=b'{ "surname_prefix" : "",\n "limit" : 2 }', headers=JSON
        )
    assert first.json() == retry.json()
    assert first.headers["location"] == retry.headers["location"]
    assert len(store.stored_queries) == 1
    assert store.contacts == CONTACTS
    assert store.version == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_location_get_reruns_query_and_revalidates_with_etag() -> None:
    store = make_store()
    async with client_for(store) as client:
        queried = await client.request(
            "QUERY", "/contacts", content=b'{"surname_prefix": "S"}', headers=JSON
        )
        location, etag = queried.headers["location"], queried.headers["etag"]
        unchanged = await client.get(location, headers={"If-None-Match": etag})
        store.contacts.append({"surname": "Sato", "email": "sato@example.jp"})
        store.version += 1
        changed = await client.get(location, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert [c["surname"] for c in changed.json()] == ["Smith", "Sato"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conditional_query_returns_304_without_content() -> None:
    async with client_for(make_store()) as client:
        body = b'{"surname_prefix": "J"}'
        first = await client.request("QUERY", "/contacts", content=body, headers=JSON)
        again = await client.request(
            "QUERY",
            "/contacts",
            content=body,
            headers={**JSON, "If-None-Match": first.headers["etag"]},
        )
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["location"] == first.headers["location"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({}, b'{"surname_prefix": "D"}', 400),
        ({"Content-Type": "text/plain"}, b"surname_prefix=D", 415),
        (JSON, b"surname_prefix=D", 400),
        (JSON, b'{"surname_prefix": "D", "table": "users"}', 422),
        ({**JSON, "Accept": "text/csv"}, b"{}", 406),
        ({**JSON, "Accept": "application/json;q=0, text/csv"}, b"{}", 406),
        ({**JSON, "Accept": "application/json;q=0, */*"}, b"{}", 406),
        ({**JSON, "Accept": "application/*;q=0, */*"}, b"{}", 406),
    ],
    ids=[
        "no-content-type",
        "unsupported-type",
        "type-mismatch",
        "bad-query",
        "accept",
        "q-zero",
        "q-zero-beats-wildcard",
        "q-zero-subtype-wildcard-beats-wildcard",
    ],
)
async def test_rfc_10008_section_2_1_error_statuses(
    headers: dict[str, str], body: bytes, status: int
) -> None:
    async with client_for(make_store()) as client:
        resp = await client.request("QUERY", "/contacts", content=body, headers=headers)
    assert resp.status_code == status
    if status == 415:
        assert resp.headers["accept-query"] == "application/json"
        assert resp.headers["accept"] == "application/json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stored_query_uri_does_not_leak_query_content() -> None:
    sensitive_prefix = "Dubois-SSN-078-05-1120"
    async with client_for(make_store()) as client:
        resp = await client.request(
            "QUERY",
            "/contacts",
            content=json.dumps({"surname_prefix": sensitive_prefix}),
            headers=JSON,
        )
    query_id = resp.headers["location"].rsplit("/", 1)[1]
    assert sensitive_prefix not in resp.headers["location"]
    assert len(query_id) == 16
    assert int(query_id, 16) >= 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_methods_get_405_with_allow_query() -> None:
    async with client_for(make_store()) as client:
        resp = await client.request("POST", "/contacts", content=b"{}", headers=JSON)
    assert resp.status_code == 405
    assert {m.strip() for m in resp.headers["allow"].split(",")} == {"QUERY"}


@pytest.mark.unit
def test_cache_key_incorporates_content_and_normalizes_insignificant_differences() -> None:
    def key(body: bytes, content_type: str = "application/json", coding: str | None = None) -> str:
        return query_cache_key(
            target="/contacts", content_type=content_type, content_encoding=coding, body=body
        )

    base = key(b'{"limit": 2, "surname_prefix": "D"}')
    assert key(b'{"surname_prefix":"D","limit":2}') == base
    assert key(gzip.compress(b'{"surname_prefix":"D","limit":2}'), coding="gzip") == base
    assert key(gzip.compress(b'{"surname_prefix":"D","limit":2}'), coding="GZIP") == base
    assert key(b'{"surname_prefix": "S", "limit": 2}') != base
    assert key(b'{"limit": 2, "surname_prefix": "D"}', "application/merge-patch+json") != base
    assert (
        query_cache_key(
            target="/archive",
            content_type="application/json",
            content_encoding=None,
            body=b'{"limit": 2, "surname_prefix": "D"}',
        )
        != base
    )


@pytest.mark.unit
def test_cache_key_parts_are_length_prefixed() -> None:
    def key(target: str, content_type: str) -> str:
        return query_cache_key(
            target=target, content_type=content_type, content_encoding=None, body=b""
        )

    assert key("/a", "b/c") != key("/ab", "/c")


@pytest.mark.unit
def test_stored_query_id_is_keyed_by_the_server_secret() -> None:
    cache_key = query_cache_key(
        target="/contacts", content_type="application/json", content_encoding=None, body=b"{}"
    )
    ids = {
        ContactStore(contacts=[], secret=secret).remember(cache_key, ContactQuery())
        for secret in (b"one", b"two")
    }
    assert len(ids) == 2
    assert hashlib.sha256(cache_key.encode()).hexdigest()[:16] not in ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_stored_query_is_404_so_the_client_resends_the_query() -> None:
    async with client_for(make_store()) as client:
        resp = await client.get("/contacts/stored-queries/0000000000000000")
    assert resp.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accept",
    ["*/*", "application/*", "text/csv, application/json;q=0.5", "*/*;q=0, application/json"],
    ids=["any", "application-wildcard", "weighted", "specific-beats-refused-wildcard"],
)
async def test_accept_ranges_that_include_json_succeed(accept: str) -> None:
    async with client_for(make_store()) as client:
        resp = await client.request(
            "QUERY", "/contacts", content=b"{}", headers={**JSON, "Accept": accept}
        )
    assert resp.status_code == 200


@pytest.mark.unit
def test_json_normalization_collapses_duplicate_keys_a_false_positive_risk() -> None:
    def key(body: bytes) -> str:
        return query_cache_key(
            target="/contacts", content_type="application/json", content_encoding=None, body=body
        )

    assert key(b'{"surname_prefix": "A", "surname_prefix": "D"}') == key(b'{"surname_prefix": "D"}')


@pytest.mark.unit
@pytest.mark.parametrize(
    ("media_types", "field_value"),
    [
        (["application/json"], "application/json"),
        (["application/sql", "*/*"], "application/sql, */*"),
        (["9x/y"], '"9x/y"'),
        (['text/x "q"'], '"text/x \\"q\\""'),
    ],
    ids=["token", "list-and-wildcard", "leading-digit-string", "escaped-string"],
)
def test_accept_query_is_a_structured_field_list(media_types: list[str], field_value: str) -> None:
    assert accept_query_value(media_types) == field_value


@pytest.mark.unit
def test_accept_query_rejects_non_ascii() -> None:
    with pytest.raises(ValueError, match="not serializable"):
        accept_query_value(["application/caf\u00e9"])


@pytest.mark.unit
def test_urllib3_default_retry_allowlist_omits_query() -> None:
    default = Retry(total=3, status_forcelist=(503,))
    assert not default.is_retry("QUERY", 503)
    assert query_retry_policy().is_retry("QUERY", 503)
    assert not query_retry_policy().is_retry("POST", 503)


@pytest.mark.unit
def test_stdlib_httpmethod_enum_has_no_query_member() -> None:
    assert "QUERY" not in http.HTTPMethod.__members__


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_runs_query_body_but_openapi_31_export_is_invalid() -> None:
    app = build_fastapi_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.request("QUERY", "/contacts", json={"surname_prefix": "D"})
    assert resp.status_code == 200
    assert resp.json() == [{"surname": "D"}]

    document = app.openapi()
    operation = document["paths"]["/contacts"]["query"]
    assert document["openapi"] == "3.1.0"
    assert "requestBody" not in operation
    errors = [e.message for e in OpenAPIV31SpecValidator(document).iter_errors()]
    assert any("'query' was unexpected" in message for message in errors)


@pytest.mark.unit
def test_openapi_32_document_with_query_request_body_validates() -> None:
    document = openapi_32_with_query_body(build_fastapi_app())
    assert document["paths"]["/contacts"]["query"]["requestBody"]["required"] is True
    assert list(OpenAPIV32SpecValidator(document).iter_errors()) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cors_preflight_must_list_query_explicitly() -> None:
    preflight = {
        "Origin": "https://app.example",
        "Access-Control-Request-Method": "QUERY",
    }
    results: dict[str, httpx.Response] = {}
    for label, methods in (("get-only", ["GET"]), ("with-query", ["GET", "QUERY"])):
        app = CORSMiddleware(
            build_app(make_store()), allow_origins=["https://app.example"], allow_methods=methods
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            results[label] = await client.options("/contacts", headers=preflight)
    assert results["get-only"].status_code == 400
    assert results["with-query"].status_code == 200
    assert "QUERY" in results["with-query"].headers["access-control-allow-methods"]


@pytest.mark.unit
def test_h11_frames_query_request_with_content_length_body() -> None:
    server = h11.Connection(h11.SERVER)
    server.receive_data(
        b"QUERY /contacts HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2\r\n\r\n{}"
    )
    request = server.next_event()
    data = server.next_event()
    assert isinstance(request, h11.Request)
    assert request.method == b"QUERY"
    assert isinstance(data, h11.Data)
    assert bytes(data.data) == b"{}"


class _Sink:
    def __init__(self) -> None:
        self.body = b""

    def on_body(self, body: bytes) -> None:
        self.body += body


@pytest.mark.unit
def test_httptools_llhttp_knows_query_but_rejects_unregistered_methods() -> None:
    sink = _Sink()
    parser = httptools.HttpRequestParser(sink)
    parser.feed_data(b"QUERY /contacts HTTP/1.1\r\nHost: test\r\nContent-Length: 2\r\n\r\n{}")
    assert parser.get_method() == b"QUERY"
    assert sink.body == b"{}"
    with pytest.raises(httptools.HttpParserInvalidMethodError):
        httptools.HttpRequestParser(_Sink()).feed_data(
            b"FROBNICATE /contacts HTTP/1.1\r\nHost: test\r\nContent-Length: 0\r\n\r\n"
        )


@pytest.mark.unit
def test_h2_carries_query_as_an_ordinary_method_pseudo_header() -> None:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    client.send_headers(
        1,
        [
            (":method", "QUERY"),
            (":scheme", "https"),
            (":authority", "test"),
            (":path", "/contacts"),
            ("content-type", "application/json"),
        ],
    )
    client.send_data(1, b"{}", end_stream=True)
    events = server.receive_data(client.data_to_send())
    request = next(e for e in events if isinstance(e, h2.events.RequestReceived))
    data = next(e for e in events if isinstance(e, h2.events.DataReceived))
    assert request.headers is not None
    assert (b":method", b"QUERY") in request.headers
    assert data.data == b"{}"


ServerKind = Literal["uvicorn-httptools", "uvicorn-h11", "hypercorn-h11"]


@asynccontextmanager
async def live_server(kind: ServerKind, app: ASGIApp) -> AsyncGenerator[str]:
    """Serve ``app`` on an ephemeral loopback port with a real ASGI server, yield its base URL."""
    if kind.startswith("uvicorn"):
        http_impl: Literal["httptools", "h11"] = (
            "httptools" if kind.endswith("httptools") else "h11"
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                http=http_impl,
                lifespan="off",
                log_level="warning",
            )
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            assert not task.done(), "uvicorn exited before it started"
            await asyncio.sleep(0.01)
        port: int = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task
        return
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = hypercorn.config.Config()
    config.bind = [f"127.0.0.1:{port}"]
    config.loglevel = "WARNING"
    stop = asyncio.Event()
    task = asyncio.create_task(
        hypercorn.asyncio.serve(app, config, shutdown_trigger=stop.wait, mode="asgi")  # type: ignore[arg-type]
    )
    for _ in range(500):
        assert not task.done(), "hypercorn exited before it started"
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.01)
            continue
        writer.close()
        await writer.wait_closed()
        break
    else:
        raise AssertionError("hypercorn did not accept connections")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        await task


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["uvicorn-httptools", "uvicorn-h11", "hypercorn-h11"])
async def test_live_servers_pass_query_through_and_only_llhttp_rejects_unknown_methods(
    kind: ServerKind,
) -> None:
    async with (
        live_server(kind, build_app(make_store())) as base,
        httpx.AsyncClient(base_url=base) as client,
    ):
        ok = await client.request(
            "QUERY", "/contacts", content=b'{"surname_prefix": "D"}', headers=JSON
        )
        unknown = await client.request("FROBNICATE", "/contacts", content=b"{}", headers=JSON)
    assert ok.status_code == 200
    assert ok.json() == [{"surname": "Dubois", "email": "camille.dubois@example.net"}]
    assert unknown.status_code == (400 if kind == "uvicorn-httptools" else 405)
