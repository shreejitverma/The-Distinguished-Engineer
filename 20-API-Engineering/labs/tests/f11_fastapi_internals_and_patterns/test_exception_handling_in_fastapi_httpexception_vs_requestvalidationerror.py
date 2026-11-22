"""Unit tests for FastAPI exception handling mechanisms."""

from typing import Any

import h11
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import FastAPIError, ResponseValidationError
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.http.httptools_impl import HEADER_VALUE_RE

from api_labs.f11_fastapi_internals_and_patterns.exception_handling import (
    SHARED_NOT_FOUND,
    build_catch_all_app,
    build_custom_handler_app,
    build_exception_handling_app,
    build_starlette_mro_demo_app,
    build_yield_swallow_app,
)


def _client(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://test",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_exception_response_and_headers() -> None:
    async with _client(build_exception_handling_app()) as client:
        response = await client.get("/http-exception")

    assert response.status_code == 400
    assert response.headers["X-Error-Code"] == "ERR_INVALID_PARAM"
    assert response.json() == {"detail": "Client requested invalid query parameters"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_content_http_exception_strips_body() -> None:
    async with _client(build_exception_handling_app()) as client:
        response = await client.get("/no-content-exception")

    assert response.status_code == 204
    assert response.headers["X-Empty"] == "true"
    assert response.content == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_http_exception_handled_by_fastapi_default() -> None:
    async with _client(build_exception_handling_app()) as client:
        response = await client.get("/starlette-http-exception")

    assert response.status_code == 403
    assert response.headers["X-Source"] == "Starlette"
    assert response.json() == {"detail": "Starlette forbidden directly raised"}


@pytest.mark.unit
def test_crlf_in_exception_headers_is_refused_by_the_wire_layer() -> None:
    # CVE-2020-7695 was response splitting through unescaped CRLF in header values.
    injected = "ERR\r\nSet-Cookie: session=attacker"
    with pytest.raises(h11.LocalProtocolError, match="Illegal header value"):
        h11.Response(status_code=400, headers=[("X-Error-Code", injected)])
    assert HEADER_VALUE_RE.search(injected.encode("latin-1")) is not None
    assert HEADER_VALUE_RE.search(b"ERR_INVALID_PARAM") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handled_shared_exception_has_traceback_reset() -> None:
    async with _client(build_exception_handling_app()) as client:
        for _ in range(3):
            response = await client.get("/shared-not-found")
            assert response.status_code == 404
            # wrap_app_handling_exceptions sets exc.__traceback__ = None after the handler.
            assert SHARED_NOT_FOUND.__traceback__ is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_validation_error_on_invalid_fields() -> None:
    async with _client(build_exception_handling_app()) as client:
        res_missing_header = await client.post(
            "/items/10",
            json={"id": 1, "name": "Widget", "price": 9.99},
        )
        assert res_missing_header.status_code == 422
        errors = res_missing_header.json()["detail"]
        assert any(e["loc"] == ["header", "x-tenant"] for e in errors)

        res_bad_path = await client.post(
            "/items/not-an-int",
            headers={"x-tenant": "corp-a"},
            json={"id": 1, "name": "Widget", "price": 9.99},
        )
        assert res_bad_path.status_code == 422
        errors = res_bad_path.json()["detail"]
        assert any(e["loc"] == ["path", "item_id"] for e in errors)

        res_bad_body = await client.post(
            "/items/10",
            headers={"x-tenant": "corp-a"},
            json={"id": 1, "name": "Widget", "price": -5.0},
        )
        assert res_bad_body.status_code == 422
        errors = res_bad_body.json()["detail"]
        assert any(e["loc"] == ["body", "price"] for e in errors)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_json_triggers_request_validation_error() -> None:
    async with _client(build_exception_handling_app()) as client:
        response = await client.post(
            "/items/10",
            headers={"x-tenant": "corp-a", "content-type": "application/json"},
            content=b'{"id": 1, "name": "Broken JSON,',
        )

    assert response.status_code == 422
    (err,) = response.json()["detail"]
    assert err["type"] == "json_invalid"
    assert err["loc"] == ["body", 18]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_text_plain_json_is_not_parsed_as_json() -> None:
    # The CVE-2021-32677 fix: a text/plain body is never decoded as JSON for a model body.
    async with _client(build_exception_handling_app()) as client:
        response = await client.post(
            "/items/10",
            headers={"x-tenant": "corp-a", "content-type": "text/plain"},
            content=b'{"id": 1, "name": "Widget", "price": 9.99}',
        )

    assert response.status_code == 422
    (err,) = response.json()["detail"]
    assert err["loc"] == ["body"]
    assert err["type"] == "model_attributes_type"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_422_echoes_input_despite_hide_input_in_errors() -> None:
    async with _client(build_exception_handling_app()) as client:
        response = await client.post("/credentials", json={"pin": "hunter2"})

    assert response.status_code == 422
    (err,) = response.json()["detail"]
    assert err["loc"] == ["body", "pin"]
    assert err["input"] == "hunter2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nested_json_depth_changes_the_status_code() -> None:
    async with _client(build_exception_handling_app(), raise_app_exceptions=False) as client:
        statuses: dict[int, int] = {}
        for depth in (300, 1_000, 100_000):
            response = await client.post(
                "/items/1",
                headers={"x-tenant": "corp-a", "content-type": "application/json"},
                content=b"[" * depth + b"]" * depth,
            )
            statuses[depth] = response.status_code

    # 422: validated; 500: RecursionError in jsonable_encoder; 400: RecursionError in json.loads.
    assert statuses == {300: 422, 1_000: 500, 100_000: 400}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_validation_error_triggers_500_not_422() -> None:
    async with _client(build_exception_handling_app(), raise_app_exceptions=False) as client:
        response = await client.get("/server-bug-response")

    assert response.status_code == 500
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == "Internal Server Error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jsonl_item_validation_fails_after_200_headers() -> None:
    app = build_exception_handling_app()
    async with _client(app, raise_app_exceptions=False) as client:
        response = await client.get("/jsonl-bug")

    # The status line was already 200; the body is truncated after the first valid record.
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/jsonl"
    assert response.content == b'{"id":1}\n'

    async with _client(app) as client:
        with pytest.raises(ResponseValidationError):
            await client.get("/jsonl-bug")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_custom_handlers_rfc9457_and_response_validation_telemetry() -> None:
    captured_errors: list[dict[str, Any]] = []
    app = build_custom_handler_app(captured_errors)

    async with _client(app) as client:
        res_val = await client.post(
            "/custom-items",
            json={"id": 1, "name": "Item", "price": -2.0},
        )
        assert res_val.status_code == 422
        assert res_val.headers["content-type"] == "application/problem+json"
        body = res_val.json()
        assert body["type"] == "https://errors.example.com/invalid-request"
        assert body["status"] == 422
        assert body["errors"] == [
            {
                "pointer": "#/price",
                "location": "body",
                "detail": "Input should be greater than 0",
                "code": "greater_than",
            }
        ]
        assert "-2.0" not in res_val.text

        res_resp_val = await client.get("/custom-broken")
        assert res_resp_val.status_code == 500
        assert res_resp_val.headers["content-type"] == "application/problem+json"
        resp_body = res_resp_val.json()
        assert resp_body["type"] == "https://errors.example.com/internal-contract-violation"
        assert "Broken Output" not in res_resp_val.text

    assert len(captured_errors) == 1
    assert captured_errors[0]["path"] == "/custom-broken"
    assert captured_errors[0]["body"] == {"id": 99, "name": "Broken Output"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_catch_all_handler_runs_outermost_and_still_reraises() -> None:
    seen: list[str] = []
    app = build_catch_all_app(seen)

    async with _client(app, raise_app_exceptions=False) as client:
        res_boom = await client.get("/boom")
        assert res_boom.status_code == 500
        assert res_boom.json() == {"error": "internal"}

        # Status handlers win over the class table for HTTPException, router 404 included.
        res_missing = await client.get("/missing")
        res_router = await client.get("/no-such-route")
        assert res_missing.json() == res_router.json() == {"error": "no such resource"}

    assert seen == ["ValueError"]

    # ServerErrorMiddleware re-raises after sending the handler's response.
    async with _client(app) as client:
        with pytest.raises(ValueError, match="database row decoder failed"):
            await client.get("/boom")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_vs_fastapi_http_exception_mro_routing_trap() -> None:
    app_fastapi_only, app_starlette_base = build_starlette_mro_demo_app()

    async with _client(app_starlette_base) as client:
        res_starlette = await client.get("/non-existent")
    assert res_starlette.status_code == 404
    assert res_starlette.json() == {
        "custom_handler": "starlette_http_exception",
        "detail": "Not Found",
    }

    async with _client(app_fastapi_only) as client:
        res_fastapi = await client.get("/non-existent")
    assert res_fastapi.status_code == 404
    # The router raised Starlette's class, so FastAPI's default handler answered.
    assert res_fastapi.json() == {"detail": "Not Found"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_exception_capture_order() -> None:
    log_sink: list[str] = []
    app = build_exception_handling_app(log_sink=log_sink)

    async with _client(app, raise_app_exceptions=False) as client:
        res_http = await client.get("/http-exception")
        assert res_http.status_code == 400
        assert log_sink[-1] == "AUDIT: completed with 400"

        res_val = await client.post("/items/invalid-id")
        assert res_val.status_code == 422
        assert log_sink[-1] == "AUDIT: completed with 422"

        res_unhandled = await client.get("/server-bug-response")
        assert res_unhandled.status_code == 500
        assert log_sink[-1] == "AUDIT: unhandled exception ResponseValidationError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exception_after_response_started() -> None:
    async with _client(build_exception_handling_app()) as client:
        # No handler matches RuntimeError: it propagates unchanged past every layer.
        with pytest.raises(RuntimeError, match=r"^Failure after stream start$"):
            await client.get("/streaming-error")

        # A handler matches HTTPException, but http.response.start was already sent.
        with pytest.raises(RuntimeError, match="response already started") as exc_info:
            await client.get("/streaming-http-exception")
        assert isinstance(exc_info.value.__cause__, HTTPException)
        assert exc_info.value.__cause__.status_code == 409


@pytest.mark.unit
@pytest.mark.asyncio
async def test_yield_dependency_that_swallows_errors_breaks_the_response() -> None:
    log: list[str] = []
    app = build_yield_swallow_app(log)

    async with _client(app) as client:
        with pytest.raises(FastAPIError, match=r"^Response not awaited"):
            await client.get("/swallow-request")
        with pytest.raises(UnboundLocalError, match="'response'"):
            await client.get("/swallow-function")
        response = await client.get("/observe")

    assert response.status_code == 409
    assert log == [
        "request-scope swallowed ValueError",
        "function-scope swallowed ValueError",
        "observer saw 409 before the handler ran",
    ]


@pytest.mark.unit
def test_websocket_request_validation_and_policy_violation() -> None:
    client = TestClient(build_exception_handling_app())

    # Missing 'token': WebSocketRequestValidationError -> close(1008) before accept.
    with (
        pytest.raises(WebSocketDisconnect) as exc_info_val,
        client.websocket_connect("/ws-validate"),
    ):
        pass
    assert exc_info_val.value.code == 1008

    with client.websocket_connect("/ws-validate?token=12345") as websocket:
        assert websocket.receive_text() == "Authenticated with token 12345"

    with client.websocket_connect("/ws-policy") as websocket:
        with pytest.raises(WebSocketDisconnect) as exc_info_policy:
            websocket.receive_text()
        assert exc_info_policy.value.code == 1008
        assert exc_info_policy.value.reason == "Policy violation occurred"


@pytest.mark.unit
def test_websocket_denial_responses_carry_http_status_and_body() -> None:
    default_client = TestClient(build_exception_handling_app())
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        default_client.websocket_connect("/ws-deny"),
    ):
        pass
    assert denied.value.status_code == 401
    assert denied.value.headers["www-authenticate"] == "Bearer"
    assert denied.value.json() == {"detail": "Missing session token"}

    custom_client = TestClient(build_custom_handler_app([]))
    with (
        pytest.raises(WebSocketDenialResponse) as invalid,
        custom_client.websocket_connect("/custom-ws?token=abc"),
    ):
        pass
    assert invalid.value.status_code == 422
    assert invalid.value.json() == {"invalid": ["query.token"]}
