"""Lab: HTTP method safety and idempotency invariants (RFC 9110 Section 9.2)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import h11
import httpx
import pytest
from fastapi import FastAPI
from hypothesis import given
from hypothesis import strategies as st
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from urllib3.util.retry import Retry

from api_labs.f04_rest_and_resource_modeling.method_semantics import (
    INCREMENT_PATCH,
    MERGE_PATCH,
    METHOD_REGISTRY,
    UNKNOWN_METHOD,
    Call,
    Handler,
    MethodSemanticsApp,
    Outcome,
    Store,
    apply,
    check_method_contract,
    properties,
    send_with_retry,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Scope

ITEM_PATHS = ["/items/1", "/items/2", "/items/9"]
docs_strategy = st.dictionaries(
    st.sampled_from(["/items/1", "/items/2", "/items/3"]),
    st.dictionaries(st.sampled_from(["a", "b", "c"]), st.integers(-5, 5), max_size=3),
    max_size=3,
)
stores = st.builds(Store, docs=docs_strategy, next_id=st.just(4))
bodies = st.dictionaries(
    st.sampled_from(["a", "b", "c"]), st.one_of(st.none(), st.integers(-5, 5)), max_size=3
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "safe", "idempotent"),
    [
        ("GET", True, True),
        ("HEAD", True, True),
        ("OPTIONS", True, True),
        ("TRACE", True, True),
        ("PUT", False, True),
        ("DELETE", False, True),
        ("POST", False, False),
        ("CONNECT", False, False),
        ("PATCH", False, False),
        ("QUERY", True, True),
    ],
)
def test_registry_matches_rfc9110_table_7_and_extension_rfcs(
    method: str, safe: bool, idempotent: bool
) -> None:
    assert (properties(method).safe, properties(method).idempotent) == (safe, idempotent)


@pytest.mark.unit
def test_urllib3_default_retry_methods_are_exactly_the_rfc9110_idempotent_methods() -> None:
    rfc9110_idempotent = {
        m for m, p in METHOD_REGISTRY.items() if p.idempotent and p.spec.startswith("RFC 9110")
    }
    assert rfc9110_idempotent == set(Retry.DEFAULT_ALLOWED_METHODS)
    retry = Retry(total=3, status_forcelist=[503])
    assert retry.is_retry("PUT", 503) is True
    assert retry.is_retry("POST", 503) is False
    assert retry.is_retry("PATCH", 503) is False


@pytest.mark.unit
def test_method_tokens_are_case_sensitive_but_httpx_uppercases_them() -> None:
    assert properties("get") is UNKNOWN_METHOD
    assert apply(Store(), Call("get", "/items")).status == 501
    assert httpx.Request("get", "http://test/items").method == "GET"


@pytest.mark.unit
@given(
    store=stores,
    method=st.sampled_from(["GET", "HEAD", "OPTIONS"]),
    path=st.sampled_from(["/items", *ITEM_PATHS]),
)
def test_safe_methods_never_change_server_state(store: Store, method: str, path: str) -> None:
    call = Call(method, path)
    outcome = apply(store, call)
    assert outcome.store == store
    assert check_method_contract(apply, store, call) == ()
    if method == "HEAD":
        # RFC 9110 Section 9.3.2: HEAD is GET without content.
        assert outcome.body is None
        assert outcome.status == apply(store, Call("GET", path)).status


@pytest.mark.unit
@given(
    store=stores,
    method=st.sampled_from(["PUT", "DELETE"]),
    path=st.sampled_from(ITEM_PATHS),
    body=bodies,
)
def test_put_and_delete_are_fixed_points(
    store: Store, method: str, path: str, body: dict[str, int | None]
) -> None:
    call = Call(method, path, body=body)
    once = apply(store, call).store
    assert apply(once, call).store == once
    assert check_method_contract(apply, store, call) == ()


@pytest.mark.unit
@given(store=stores, path=st.sampled_from(ITEM_PATHS), body=bodies)
def test_merge_patch_is_idempotent_even_though_patch_is_not(
    store: Store, path: str, body: dict[str, int | None]
) -> None:
    call = Call("PATCH", path, body=body, content_type=MERGE_PATCH)
    once = apply(store, call).store
    assert apply(once, call).store == once
    if path in store.docs:
        # Merge patch semantics: a null member removes the key, any other value sets it.
        patched = once.docs[path]
        for key, value in body.items():
            assert patched.get(key) == value


@pytest.mark.unit
@given(store=stores, delta=st.integers(-5, 5).filter(lambda d: d != 0))
def test_increment_patch_is_not_idempotent(store: Store, delta: int) -> None:
    store = replace(store, docs={**store.docs, "/items/1": {"a": 0}})
    call = Call("PATCH", "/items/1", body={"a": delta}, content_type=INCREMENT_PATCH)
    once = apply(store, call).store
    twice = apply(once, call).store
    assert twice != once
    assert twice.docs["/items/1"]["a"] == 2 * delta


@pytest.mark.unit
@given(store=stores, body=bodies)
def test_post_without_key_creates_a_new_resource_per_repeat(
    store: Store, body: dict[str, int | None]
) -> None:
    call = Call("POST", "/items", body=body)
    once = apply(store, call)
    twice = apply(once.store, call)
    assert len(twice.store.docs) == len(once.store.docs) + 1
    assert once.headers["location"] != twice.headers["location"]


@pytest.mark.unit
def test_idempotency_key_turns_a_repeated_post_into_a_replay() -> None:
    call = Call("POST", "/items", body={"a": 1}, idempotency_key="k-1")
    once = apply(Store(), call)
    twice = apply(once.store, call)
    assert twice.store == once.store
    assert (twice.status, twice.headers["location"]) == (201, "/items/1")


@pytest.mark.unit
def test_delete_repeats_reach_one_state_through_two_different_responses() -> None:
    store = Store(docs={"/items/1": {"a": 1}})
    first = apply(store, Call("DELETE", "/items/1"))
    second = apply(first.store, Call("DELETE", "/items/1"))
    assert (first.status, second.status) == (204, 404)
    assert first.store == second.store


@pytest.mark.unit
def test_put_reports_201_on_create_and_200_on_replace() -> None:
    call = Call("PUT", "/items/7", body={"a": 1})
    created = apply(Store(), call)
    replaced = apply(created.store, call)
    assert (created.status, replaced.status) == (201, 200)
    empty = apply(Store(docs={"/items/7": {}}), call)
    assert empty.status == 200
    # RFC 9110 Section 9.3.4: PUT replaces the state; keys absent from the body are gone.
    stale = apply(Store(docs={"/items/7": {"b": 9}}), call)
    assert stale.store.docs["/items/7"] == {"a": 1}


def _get_with_view_counter(store: Store, call: Call) -> Outcome:
    outcome = apply(store, call)
    if call.method == "GET":
        return replace(outcome, store=replace(outcome.store, next_id=outcome.store.next_id + 1))
    return outcome


def _put_with_revision_bump(store: Store, call: Call) -> Outcome:
    outcome = apply(store, call)
    if call.method == "PUT":
        doc = dict(outcome.store.docs[call.path])
        doc["rev"] = store.docs.get(call.path, {}).get("rev", 0) + 1
        docs = {**outcome.store.docs, call.path: doc}
        return replace(outcome, store=replace(outcome.store, docs=docs))
    return outcome


@pytest.mark.unit
@pytest.mark.parametrize(
    ("handler", "call", "expected"),
    [
        (apply, Call("GET", "/items/1"), ()),
        (
            _get_with_view_counter,
            Call("GET", "/items/1"),
            ("GET is safe but changed server state", "GET is idempotent but f(f(s)) != f(s)"),
        ),
        (apply, Call("PUT", "/items/1", body={"a": 2}), ()),
        (
            _put_with_revision_bump,
            Call("PUT", "/items/1", body={"a": 2}),
            ("PUT is idempotent but f(f(s)) != f(s)",),
        ),
    ],
    ids=["get-ok", "get-view-counter", "put-ok", "put-revision-bump"],
)
def test_contract_checker_flags_handlers_that_break_the_rfc(
    handler: Handler, call: Call, expected: tuple[str, ...]
) -> None:
    store = Store(docs={"/items/1": {"a": 1}})
    assert check_method_contract(handler, store, call) == expected


@pytest.mark.unit
def test_unknown_method_is_501_and_disallowed_known_method_is_405_with_allow() -> None:
    assert apply(Store(), Call("BREW", "/items")).status == 501
    rejected = apply(Store(), Call("DELETE", "/items"))
    assert rejected.status == 405
    assert rejected.headers["allow"] == "GET, HEAD, OPTIONS, POST"


@pytest.mark.unit
def test_patch_with_an_unknown_media_type_is_415_and_changes_nothing() -> None:
    store = Store(docs={"/items/1": {"a": 1}})
    outcome = apply(store, Call("PATCH", "/items/1", {"a": 2}, content_type="application/json"))
    assert outcome.status == 415
    assert outcome.store == store


@pytest.mark.unit
def test_h11_frames_a_head_response_as_zero_length_content() -> None:
    server = h11.Connection(h11.SERVER)
    server.receive_data(b"HEAD /items/1 HTTP/1.1\r\nHost: test\r\n\r\n")
    assert isinstance(server.next_event(), h11.Request)
    server.next_event()  # EndOfMessage
    head = server.send(h11.Response(status_code=200, headers=[("Content-Length", "7")]))
    assert head is not None
    assert b"content-length: 7" in head.lower()
    with pytest.raises(h11.LocalProtocolError, match="Too much data"):
        server.send(h11.Data(data=b'{"a":1}'))


ALLOW_PROBE = """
import asyncio
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from api_labs.f04_rest_and_resource_modeling.method_semantics import Call, Store, apply

def ep(request):
    return PlainTextResponse("")

app = Starlette(routes=[Route("/items", ep, methods=["GET", "POST", "OPTIONS"])])
sent = []

async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}

async def send(message):
    sent.append(message)

scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "DELETE",
         "scheme": "http", "path": "/items", "raw_path": b"/items", "root_path": "",
         "query_string": b"", "headers": [], "server": ("test", 80), "client": ("c", 1)}
asyncio.run(app(scope, receive, send))
print(dict(sent[0]["headers"])[b"allow"].decode())
print(apply(Store(), Call("DELETE", "/items")).headers["allow"])
"""


@pytest.mark.unit
def test_starlette_allow_order_varies_with_hash_seed_and_sorted_allow_does_not() -> None:
    starlette_values: set[str] = set()
    sorted_values: set[str] = set()
    for seed in range(12):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a constant script
            [sys.executable, "-c", ALLOW_PROBE], env=env, capture_output=True, text=True, check=True
        )
        starlette_line, sorted_line = result.stdout.splitlines()
        assert set(starlette_line.split(", ")) == {"GET", "HEAD", "OPTIONS", "POST"}
        starlette_values.add(starlette_line)
        sorted_values.add(sorted_line)
    assert len(starlette_values) > 1
    assert sorted_values == {"GET, HEAD, OPTIONS, POST"}


async def _item_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"a": 1})


def _starlette_app() -> Starlette:
    return Starlette(routes=[Route("/items/1", _item_endpoint, methods=["GET", "PUT"])])


def _fastapi_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/1")
    async def read_item() -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return {"a": 1}

    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_adds_head_to_get_routes_but_fastapi_does_not() -> None:
    transport = httpx.ASGITransport(app=_starlette_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        get, head = await client.get("/items/1"), await client.head("/items/1")
    assert (get.status_code, head.status_code) == (200, 200)
    assert head.headers["content-length"] == get.headers["content-length"] == "7"
    assert head.content == b""

    transport = httpx.ASGITransport(app=_fastapi_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        head = await client.head("/items/1")
    assert head.status_code == 405
    assert head.headers["allow"] == "GET"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_frameworks_answer_unknown_methods_with_405_where_the_rfc_suggests_501() -> None:
    results: dict[str, int] = {}
    apps: list[tuple[str, ASGIApp]] = [
        ("starlette", _starlette_app()),
        ("fastapi", _fastapi_app()),
        ("lab", MethodSemanticsApp(Store(docs={"/items/1": {"a": 1}}))),
    ]
    for name, app in apps:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.request("BREW", "/items/1")
        results[name] = response.status_code
        if response.status_code == 405:
            assert "allow" in response.headers
    assert results == {"starlette": 405, "fastapi": 405, "lab": 501}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_compares_the_raw_method_token_case_sensitively() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "get",
        "scheme": "http",
        "path": "/items/1",
        "raw_path": b"/items/1",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("c", 1),
    }
    await _starlette_app()(scope, receive, send)
    assert sent[0]["status"] == 405


class LossyTransport(httpx.AsyncBaseTransport):
    """Delivers the request to the app, then loses the first ``losses`` responses."""

    def __init__(self, app: MethodSemanticsApp, losses: int = 1) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self._losses = losses
        self.attempts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        response = await self._inner.handle_async_request(request)
        await response.aread()
        if self._losses > 0:
            self._losses -= 1
            raise httpx.ReadError("connection reset after the server applied it", request=request)
        return response


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url", "key", "retry_any", "attempts", "status", "items"),
    [
        ("PUT", "/items/1", None, False, 2, 200, 1),
        ("POST", "/items", None, False, 1, None, 1),
        ("POST", "/items", None, True, 2, 201, 2),
        ("POST", "/items", "k-1", False, 2, 201, 1),
    ],
    ids=["put-retried", "post-not-retried", "post-blind-retry-duplicates", "post-key-replayed"],
)
async def test_lost_response_retry_matrix(
    method: str,
    url: str,
    key: str | None,
    retry_any: bool,
    attempts: int,
    status: int | None,
    items: int,
) -> None:
    app = MethodSemanticsApp()
    transport = LossyTransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if status is None:
            with pytest.raises(httpx.ReadError):
                await send_with_retry(client, method, url, json={"a": 1}, idempotency_key=key)
        else:
            response = await send_with_retry(
                client, method, url, json={"a": 1}, idempotency_key=key, retry_any_method=retry_any
            )
            assert response.status_code == status
    assert transport.attempts == attempts
    assert len(app.store.docs) == items


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_stops_after_the_attempt_cap_even_for_an_idempotent_method() -> None:
    app = MethodSemanticsApp()
    transport = LossyTransport(app, losses=10)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(httpx.ReadError):
            await send_with_retry(client, "PUT", "/items/1", json={"a": 1}, attempts=3)
    assert transport.attempts == 3
