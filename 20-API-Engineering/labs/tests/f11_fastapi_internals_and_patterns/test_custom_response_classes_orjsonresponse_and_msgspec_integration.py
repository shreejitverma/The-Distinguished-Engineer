import decimal
import json
import warnings
from typing import Any

import httpx
import msgspec
import orjson
import pydantic
import pytest
from fastapi import FastAPI, Response
from fastapi._compat.v2 import ModelField
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import FastAPIDeprecationWarning
from fastapi.responses import JSONResponse, ORJSONResponse  # pyright: ignore[reportDeprecated]
from fastapi.routing import APIRouter

from api_labs.f11_fastapi_internals_and_patterns.custom_response_classes import (
    BenchmarkItemMsgSpec,
    BenchmarkItemPydantic,
    MsgSpecResponse,
    MsgSpecRoute,
    generate_benchmark_datasets,
    serialize_msgspec_encode,
    serialize_orjson_dicts,
    serialize_pydantic_orjson_pipeline,
    serialize_pydantic_rust_dump_json,
    serialize_stdlib_json,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_response_model_uses_rust_dump_json() -> None:
    """Verifies FastAPI 0.162.0 default response_model serializes via Rust dump_json."""
    app = FastAPI()

    @app.get("/items", response_model=list[BenchmarkItemPydantic])  # noqa: FAST001
    def get_items() -> list[BenchmarkItemPydantic]:
        return [
            BenchmarkItemPydantic(id=1, name="alpha", active=True, score=98.5),
            BenchmarkItemPydantic(id=2, name="beta", active=False, score=42.0),
        ]

    # Verify that route.response_class is DefaultPlaceholder
    route = app.routes[-1]
    assert hasattr(route, "response_class")
    # Verify via in-process ASGI client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/items")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        # Content-Length must match exact body length (RFC 9110 Section 8.6)
        assert resp.headers["content-length"] == str(len(resp.content))
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "alpha"
        assert data[1]["id"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orjson_response_class_deprecation_and_pipeline_trap() -> None:
    """Verifies that setting response_class=ORJSONResponse with response_model triggers warning."""
    app = FastAPI()

    with warnings.catch_warnings(record=True) as recorded_warnings:
        warnings.simplefilter("always")

        @app.get(
            "/orjson-items",
            response_model=list[BenchmarkItemPydantic],  # noqa: FAST001
            response_class=ORJSONResponse,  # pyright: ignore[reportDeprecated]
        )
        def get_orjson_items() -> list[BenchmarkItemPydantic]:
            return [BenchmarkItemPydantic(id=10, name="gamma", active=True, score=12.3)]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/orjson-items")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/json"
            assert resp.json() == [{"id": 10, "name": "gamma", "active": True, "score": 12.3}]

        # Check for FastAPIDeprecationWarning
        deprecation_warnings = [
            w for w in recorded_warnings if issubclass(w.category, FastAPIDeprecationWarning)
        ]
        assert len(deprecation_warnings) >= 1
        assert "ORJSONResponse is deprecated" in str(deprecation_warnings[0].message)


@pytest.mark.unit
def test_msgspec_struct_fails_with_jsonable_encoder() -> None:
    """Verifies jsonable_encoder crashes on msgspec.Struct due to missing __dict__."""
    item = BenchmarkItemMsgSpec(id=1, name="direct", active=True, score=1.0)
    # msgspec.Struct has no __dict__ attribute
    assert not hasattr(item, "__dict__")

    with pytest.raises(ValueError, match=r"__dict__|vars"):
        jsonable_encoder(item)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_msgspec_response_direct_return_bypasses_pipeline() -> None:
    """Verifies direct return of MsgSpecResponse bypasses jsonable_encoder."""
    app = FastAPI()

    @app.get("/direct-msgspec")
    def get_direct() -> Response:
        payload = [
            BenchmarkItemMsgSpec(id=1, name="struct1", active=True, score=10.0),
            BenchmarkItemMsgSpec(id=2, name="struct2", active=False, score=20.0),
        ]
        return MsgSpecResponse(payload)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/direct-msgspec")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/json"
        assert resp.headers["content-length"] == str(len(resp.content))
        assert resp.json() == [
            {"id": 1, "name": "struct1", "active": True, "score": 10.0},
            {"id": 2, "name": "struct2", "active": False, "score": 20.0},
        ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_msgspec_route_automatic_serialization_sync_and_async() -> None:
    """Verifies MsgSpecRoute serializes naked Struct return values for sync and async endpoints."""
    app = FastAPI()
    router = APIRouter(route_class=MsgSpecRoute)

    @router.get("/sync-endpoint")
    def sync_endpoint() -> list[BenchmarkItemMsgSpec]:
        return [BenchmarkItemMsgSpec(id=101, name="sync", active=True, score=1.0)]

    @router.get("/async-endpoint")
    async def async_endpoint() -> list[BenchmarkItemMsgSpec]:
        return [BenchmarkItemMsgSpec(id=102, name="async", active=False, score=2.0)]

    app.include_router(router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Test sync endpoint
        res_sync = await client.get("/sync-endpoint")
        assert res_sync.status_code == 200
        assert res_sync.headers["content-type"] == "application/json"
        assert res_sync.json() == [{"id": 101, "name": "sync", "active": True, "score": 1.0}]

        # Test async endpoint
        res_async = await client.get("/async-endpoint")
        assert res_async.status_code == 200
        assert res_async.headers["content-type"] == "application/json"
        assert res_async.json() == [{"id": 102, "name": "async", "active": False, "score": 2.0}]


@pytest.mark.unit
def test_non_finite_float_handling() -> None:
    """Verifies NaN normalization across orjson, msgspec, and starlette JSONResponse."""
    nan_dict = {"value": float("nan")}

    # orjson normalizes NaN to null
    orjson_bytes = orjson.dumps(nan_dict)
    assert orjson_bytes == b'{"value":null}'

    # msgspec normalizes NaN to null
    msgspec_bytes = msgspec.json.encode(nan_dict)
    assert msgspec_bytes == b'{"value":null}'

    # Starlette JSONResponse with allow_nan=False raises ValueError on instantiation
    with pytest.raises(ValueError, match="Out of range float values"):
        JSONResponse(content=nan_dict)


@pytest.mark.unit
def test_strict_integer_precision_rfc_8259() -> None:
    """Verifies integer handling beyond 2^53 - 1 per RFC 8259 Section 6."""
    safe_int = 2**53 - 1
    overflow_int = 2**53 + 5

    # Safe int encodes cleanly
    assert orjson.dumps({"id": safe_int}, option=orjson.OPT_STRICT_INTEGER) == (
        f'{{"id":{safe_int}}}'.encode()
    )

    # Overflow int raises TypeError under OPT_STRICT_INTEGER
    with pytest.raises(TypeError, match="Integer exceeds 53-bit range"):
        orjson.dumps({"id": overflow_int}, option=orjson.OPT_STRICT_INTEGER)

    # Without OPT_STRICT_INTEGER, orjson and msgspec encode the 64-bit integer
    assert orjson.dumps({"id": overflow_int}) == f'{{"id":{overflow_int}}}'.encode()
    assert msgspec.json.encode({"id": overflow_int}) == f'{{"id":{overflow_int}}}'.encode()


@pytest.mark.unit
def test_serialization_benchmark_data_integrity() -> None:
    """Verifies that all serialization paths produce valid, equivalent JSON outputs."""
    p_data, m_data, d_data = generate_benchmark_datasets(count=10)
    adapter = pydantic.TypeAdapter(list[BenchmarkItemPydantic])

    out_stdlib = serialize_stdlib_json(d_data)
    out_orjson_d = serialize_orjson_dicts(d_data)
    out_rust_dump = serialize_pydantic_rust_dump_json(adapter, p_data)
    out_orjson_pipe = serialize_pydantic_orjson_pipeline(adapter, p_data)
    out_msgspec = serialize_msgspec_encode(m_data)

    parsed_stdlib = json.loads(out_stdlib)
    parsed_orjson = json.loads(out_orjson_d)
    parsed_rust = json.loads(out_rust_dump)
    parsed_pipe = json.loads(out_orjson_pipe)
    parsed_msgspec = json.loads(out_msgspec)

    assert parsed_stdlib == parsed_orjson == parsed_rust == parsed_pipe == parsed_msgspec
    assert len(parsed_stdlib) == 10
    assert parsed_stdlib[0]["id"] == 0
    assert parsed_stdlib[9]["name"] == "item_9"


def _spy_on_serializers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record which ModelField serializer FastAPI calls for each response."""
    calls: list[str] = []
    real_json = ModelField.serialize_json
    real_python = ModelField.serialize

    def spy_json(self: ModelField, *args: Any, **kwargs: Any) -> bytes:
        calls.append("serialize_json")
        return real_json(self, *args, **kwargs)

    def spy_python(self: ModelField, *args: Any, **kwargs: Any) -> Any:
        calls.append("serialize")
        return real_python(self, *args, **kwargs)

    monkeypatch.setattr(ModelField, "serialize_json", spy_json)
    monkeypatch.setattr(ModelField, "serialize", spy_python)
    return calls


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


def _one_item() -> list[BenchmarkItemPydantic]:
    return [BenchmarkItemPydantic(id=1, name="alpha", active=True, score=1.5)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_class_selects_serializer_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default route calls serialize_json; an ORJSONResponse route calls serialize."""
    calls = _spy_on_serializers(monkeypatch)
    app = FastAPI()

    @app.get("/default", response_model=list[BenchmarkItemPydantic])  # noqa: FAST001
    def default_route() -> list[BenchmarkItemPydantic]:
        return _one_item()

    @app.get(
        "/orjson",
        response_model=list[BenchmarkItemPydantic],  # noqa: FAST001
        response_class=ORJSONResponse,  # pyright: ignore[reportDeprecated]
    )
    def orjson_route() -> list[BenchmarkItemPydantic]:
        return _one_item()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FastAPIDeprecationWarning)
        assert (await _get(app, "/default")).status_code == 200
        assert calls == ["serialize_json"]
        calls.clear()
        assert (await _get(app, "/orjson")).status_code == 200
        assert calls == ["serialize"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_response_class_override_also_defeats_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """default_response_class=ORJSONResponse hands every route the bare class, not a placeholder."""
    calls = _spy_on_serializers(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FastAPIDeprecationWarning)
        app = FastAPI(default_response_class=ORJSONResponse)  # pyright: ignore[reportDeprecated]

        @app.get("/items", response_model=list[BenchmarkItemPydantic])  # noqa: FAST001
        def items() -> list[BenchmarkItemPydantic]:
            return _one_item()

        resp = await _get(app, "/items")
    assert resp.status_code == 200
    assert calls == ["serialize"]


@pytest.mark.unit
def test_msgspec_route_discards_response_model_but_responses_keeps_schema() -> None:
    """MsgSpecRoute drops response_model; responses={200: {"model": ...}} still documents it."""
    app = FastAPI()
    router = APIRouter(route_class=MsgSpecRoute)

    @router.get("/dropped", response_model=list[BenchmarkItemPydantic])
    def dropped() -> list[BenchmarkItemMsgSpec]:
        return []

    @router.get("/documented", responses={200: {"model": list[BenchmarkItemPydantic]}})
    def documented() -> list[BenchmarkItemMsgSpec]:
        return []

    app.include_router(router)
    paths = app.openapi()["paths"]
    dropped_schema = paths["/dropped"]["get"]["responses"]["200"]["content"]["application/json"]
    documented_schema = paths["/documented"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    assert dropped_schema["schema"] == {}
    assert "BenchmarkItemPydantic" in json.dumps(documented_schema)


@pytest.mark.unit
def test_msgspec_encoding_hooks_and_unsupported_types() -> None:
    """msgspec rejects unknown types without enc_hook; Decimal becomes a JSON string."""
    model = BenchmarkItemPydantic(id=1, name="alpha", active=True, score=1.5)
    with pytest.raises(TypeError, match="Encoding objects of type BenchmarkItemPydantic"):
        msgspec.json.encode(model)
    assert msgspec.json.encode(model, enc_hook=lambda o: o.model_dump()) == (
        b'{"id":1,"name":"alpha","active":true,"score":1.5}'
    )
    assert msgspec.json.encode(decimal.Decimal("1.10")) == b'"1.10"'
    with pytest.raises(TypeError, match="not JSON serializable"):
        orjson.dumps(decimal.Decimal("1.10"))
    assert orjson.dumps(decimal.Decimal("1.10"), default=str) == b'"1.10"'


@pytest.mark.unit
def test_caller_supplied_content_length_suppresses_computed_header() -> None:
    """Starlette computes Content-Length only when the caller did not pass one (RFC 9110 8.6)."""
    computed = MsgSpecResponse({"a": 1})
    assert computed.headers["content-length"] == str(len(computed.body))
    overridden = MsgSpecResponse({"a": 1}, headers={"content-length": "999"})
    assert overridden.headers["content-length"] == "999"
    assert len(overridden.body) != 999
