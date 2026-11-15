"""FastAPI 0.162.0 as a glue layer over Starlette 1.10.0 and Pydantic 2.14.3."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f11_fastapi_internals_and_patterns.glue_layer import (
    RouteAnatomy,
    build_glue_app,
    describe_route,
    find_api_route,
    middleware_chain,
)


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest.mark.unit
def test_fastapi_subclasses_starlette_but_never_runs_its_init() -> None:
    app, _ = build_glue_app()
    assert [c.__name__ for c in type(app).__mro__] == ["FastAPI", "Starlette", "object"]
    # Starlette.__init__ sets max_body_size; FastAPI.__init__ does not call it.
    assert "max_body_size" in vars(Starlette())
    assert "max_body_size" not in vars(app)


@pytest.mark.unit
def test_middleware_stack_inserts_fastapi_layers_around_user_middleware() -> None:
    app, _ = build_glue_app()
    assert middleware_chain(app) == [
        "ServerErrorMiddleware",
        "ExceptionTelemetryMiddleware",
        "ExceptionMiddleware",
        "AsyncExitStackMiddleware",
        "APIRouter",
    ]
    assert middleware_chain(Starlette()) == [
        "ServerErrorMiddleware",
        "ExceptionMiddleware",
        "Router",
    ]


@pytest.mark.unit
def test_apiroute_compiles_signature_into_typeadapters_at_registration() -> None:
    app, _ = build_glue_app()
    route = find_api_route(app, "/items/{item_id}", "GET")
    assert describe_route(route) == RouteAnatomy(
        methods=frozenset({"GET"}),
        handler_qualname="request_response.<locals>.app",
        path_params=("item_id",),
        query_params=("verbose",),
        body_param=None,
        response_adapter_type="TypeAdapter",
        unique_id="read_item_items__item_id__get",
    )
    post = describe_route(find_api_route(app, "/items", "POST"))
    assert post.body_param == "item"
    assert post.response_adapter_type == "TypeAdapter"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_model_filters_fields_and_serializes_in_rust() -> None:
    app, _ = build_glue_app()
    async with client_for(app) as client:
        response = await client.get("/items/3")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    # `secret` dropped by the TypeAdapter; inf -> null by pydantic-core dump_json.
    assert response.content == b'{"id":3,"name":"widget","price":null}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_response_model_json_response_rejects_non_finite_floats() -> None:
    app, _ = build_glue_app()
    async with client_for(app) as client:
        response = await client.get("/unvalidated")
    # jsonable_encoder keeps inf; JSONResponse uses json.dumps(allow_nan=False).
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_route_does_not_imply_head_unlike_starlette_route() -> None:
    app, _ = build_glue_app()

    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    plain = Starlette(routes=[Route("/s", ok)])
    async with client_for(app) as client:
        fastapi_head = await client.head("/items/3")
    async with client_for(plain) as client:
        starlette_head = await client.head("/s")
    assert fastapi_head.status_code == 405
    assert fastapi_head.headers["allow"] == "GET"
    assert starlette_head.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strict_content_type_yields_422_not_415_for_non_json_media_type() -> None:
    app, _ = build_glue_app()
    payload = b'{"id": 1, "name": "a"}'
    async with client_for(app) as client:
        plain = await client.post("/items", content=payload, headers={"content-type": "text/plain"})
        suffix = await client.post(
            "/items", content=payload, headers={"content-type": "application/merge-patch+json"}
        )
    assert plain.status_code == 422
    assert plain.json()["detail"][0]["type"] == "model_attributes_type"
    assert suffix.status_code == 200
    assert suffix.json() == {"id": 1, "name": "a", "price": 1.0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_json_becomes_json_invalid_with_byte_offset() -> None:
    app, _ = build_glue_app()
    async with client_for(app) as client:
        response = await client.post(
            "/items", content=b'{"id":1,', headers={"content-type": "application/json"}
        )
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["type"] == "json_invalid"
    assert error["loc"] == ["body", 8]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lax_mode_coerces_json_string_to_int() -> None:
    app, _ = build_glue_app()
    async with client_for(app) as client:
        response = await client.post("/items", json={"id": "7", "name": "a"})
    assert response.status_code == 200
    assert response.json() == {"id": 7, "name": "a", "price": 1.0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apiroute_cannot_run_inside_plain_starlette() -> None:
    app, _ = build_glue_app()
    route = find_api_route(app, "/items/{item_id}", "GET")
    bare = Starlette(routes=[route])
    transport = httpx.ASGITransport(app=bare, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(AssertionError, match="fastapi_middleware_astack"):
            await client.get("/items/3")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_include_router_stores_router_lazily_and_sees_late_routes() -> None:
    app, router = build_glue_app()
    included = app.routes[-1]
    assert type(included).__name__ == "_IncludedRouter"
    # Effective (prefixed) route contexts are not built until first match.
    assert vars(included)["_effective_candidates_version"] is None
    async with client_for(app) as client:
        response = await client.get("/v1/late")
    assert response.status_code == 200
    assert response.json() == {"late": True}
    assert vars(included)["_effective_candidates_version"] == 1
    assert vars(included)["original_router"] is router


@pytest.mark.unit
def test_openapi_is_3_1_cached_and_invalidated_by_route_changes() -> None:
    app, _ = build_glue_app()
    schema = app.openapi()
    assert schema["openapi"] == "3.1.0"
    body = schema["paths"]["/items"]["post"]["requestBody"]
    assert body["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Item"}
    schema_names = sorted(schema["components"]["schemas"])
    assert schema_names == ["HTTPValidationError", "Item", "ValidationError"]
    assert app.openapi() is schema

    @app.get("/added")
    async def added() -> None:
        return None

    refreshed = app.openapi()
    assert refreshed is not schema
    assert "/added" in refreshed["paths"]


@pytest.mark.unit
def test_fastapi_registers_its_own_default_exception_handlers() -> None:
    app = FastAPI()
    assert list(app.exception_handlers) == [
        HTTPException,
        RequestValidationError,
        WebSocketRequestValidationError,
    ]
    assert Starlette().exception_handlers == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_limit_needs_explicit_starlette_middleware() -> None:
    options: dict[str, Any] = {"max_body_size": 16}
    with pytest.raises(TypeError, match="max_body_size"):
        FastAPI(**options)
    app, _ = build_glue_app()
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=16)
    async with client_for(app) as client:
        response = await client.post("/items", json={"id": 1, "name": "x" * 64})
    assert response.status_code == 413
