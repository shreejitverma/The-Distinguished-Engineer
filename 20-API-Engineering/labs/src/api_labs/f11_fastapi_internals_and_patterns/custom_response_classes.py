import inspect
import json
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any

import msgspec
import orjson
import pydantic
from fastapi import Response
from fastapi.routing import APIRoute
from starlette.background import BackgroundTask


class MsgSpecResponse(Response):
    """High-throughput Starlette Response using msgspec.json.encode.

    Bypasses jsonable_encoder and CPython dictionary allocations by encoding
    msgspec.Struct instances, dataclasses, and primitive structures directly
    into JSON UTF-8 bytes via msgspec's C engine.
    """

    media_type = "application/json"

    def __init__(
        self,
        content: Any,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        super().__init__(
            content=content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
            background=background,
        )

    def render(self, content: Any) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, memoryview):
            return content.tobytes()
        return msgspec.json.encode(content)


def _build_async_msgspec_wrapper(fn: Any, status_code: int | None) -> Callable[..., Any]:
    @wraps(fn)
    async def async_msgspec_wrapper(*a: Any, **kw: Any) -> Response:
        result: Any = await fn(*a, **kw)
        if isinstance(result, Response):
            return result
        return MsgSpecResponse(
            content=result,
            status_code=status_code or 200,
        )

    return async_msgspec_wrapper


def _build_sync_msgspec_wrapper(fn: Any, status_code: int | None) -> Callable[..., Any]:
    @wraps(fn)
    def sync_msgspec_wrapper(*a: Any, **kw: Any) -> Response:
        result: Any = fn(*a, **kw)
        if isinstance(result, Response):
            return result
        return MsgSpecResponse(
            content=result,
            status_code=status_code or 200,
        )

    return sync_msgspec_wrapper


class MsgSpecRoute(APIRoute):
    """Custom APIRoute that automatically serializes endpoint return values via msgspec.

    Intercepts the endpoint invocation at the route level to serialize naked
    msgspec.Struct, dicts, lists, and primitives directly into a MsgSpecResponse,
    preventing FastAPI's serialize_response from calling jsonable_encoder on
    objects that lack a __dict__ attribute.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Overwrite Default(None) placeholder with explicit None to disable
        # automatic Pydantic ModelField construction on return type annotations
        if "response_model" in kwargs:
            kwargs["response_model"] = None
        super().__init__(*args, **kwargs)
        fn: Any = self.endpoint

        if inspect.iscoroutinefunction(fn):
            wrapped_async = _build_async_msgspec_wrapper(fn, self.status_code)
            self.endpoint = wrapped_async
            self.dependant.call = wrapped_async
        else:
            wrapped_sync = _build_sync_msgspec_wrapper(fn, self.status_code)
            self.endpoint = wrapped_sync
            self.dependant.call = wrapped_sync


class BenchmarkItemPydantic(pydantic.BaseModel):
    id: int
    name: str
    active: bool
    score: float


class BenchmarkItemMsgSpec(msgspec.Struct):
    id: int
    name: str
    active: bool
    score: float


def generate_benchmark_datasets(
    count: int = 10_000,
) -> tuple[
    list[BenchmarkItemPydantic],
    list[BenchmarkItemMsgSpec],
    list[dict[str, Any]],
]:
    """Generate aligned datasets for Pydantic, msgspec, and raw dicts."""
    pydantic_data = [
        BenchmarkItemPydantic(
            id=i,
            name=f"item_{i}",
            active=(i % 2 == 0),
            score=i * 1.5,
        )
        for i in range(count)
    ]
    msgspec_data = [
        BenchmarkItemMsgSpec(
            id=i,
            name=f"item_{i}",
            active=(i % 2 == 0),
            score=i * 1.5,
        )
        for i in range(count)
    ]
    dict_data = [
        {
            "id": i,
            "name": f"item_{i}",
            "active": (i % 2 == 0),
            "score": i * 1.5,
        }
        for i in range(count)
    ]
    return pydantic_data, msgspec_data, dict_data


def serialize_stdlib_json(data: list[dict[str, Any]]) -> bytes:
    """Serialize raw dictionaries using CPython stdlib json.dumps."""
    return json.dumps(data).encode("utf-8")


def serialize_orjson_dicts(data: list[dict[str, Any]]) -> bytes:
    """Serialize raw dictionaries using orjson.dumps."""
    return orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY)


def serialize_pydantic_rust_dump_json(
    adapter: pydantic.TypeAdapter[list[BenchmarkItemPydantic]],
    data: list[BenchmarkItemPydantic],
) -> bytes:
    """Serialize Pydantic models directly to JSON bytes via Rust pydantic-core."""
    return adapter.dump_json(data)


def serialize_pydantic_orjson_pipeline(
    adapter: pydantic.TypeAdapter[list[BenchmarkItemPydantic]],
    data: list[BenchmarkItemPydantic],
) -> bytes:
    """Simulate ORJSONResponse with response_model: dump_python then orjson.dumps."""
    python_dict = adapter.dump_python(data, mode="json")
    return orjson.dumps(python_dict, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY)


def serialize_msgspec_encode(data: list[BenchmarkItemMsgSpec]) -> bytes:
    """Serialize msgspec.Struct instances directly via msgspec C engine."""
    return msgspec.json.encode(data)
