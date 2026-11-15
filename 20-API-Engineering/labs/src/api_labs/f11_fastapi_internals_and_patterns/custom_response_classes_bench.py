"""Benchmark for the custom response classes note.

Run from labs/ (one command, split here only for line length):
    uv run --frozen python -m \
        api_labs.f11_fastapi_internals_and_patterns.custom_response_classes_bench

Timings are wall-clock medians of repeated calls on one machine; they are
relative evidence for that machine and interpreter, not portable constants.
"""

import asyncio
import json
import platform
import statistics
import sys
import time
import tracemalloc
import warnings
from collections.abc import Callable
from typing import Any

import httpx
import msgspec
import pydantic
from fastapi import FastAPI, Response
from fastapi.responses import ORJSONResponse  # pyright: ignore[reportDeprecated]

from api_labs.f11_fastapi_internals_and_patterns.custom_response_classes import (
    BenchmarkItemMsgSpec,
    BenchmarkItemPydantic,
    MsgSpecResponse,
    generate_benchmark_datasets,
    serialize_msgspec_encode,
    serialize_orjson_dicts,
    serialize_pydantic_orjson_pipeline,
    serialize_pydantic_rust_dump_json,
    serialize_stdlib_json,
)

ITEMS = 10_000
REPEAT = 41
HTTP_REQUESTS = 41


def _median_ms(fn: Callable[[], bytes], repeat: int = REPEAT) -> float:
    fn()  # warm caches and pydantic-core's expected_json_size hint
    samples: list[float] = []
    for _ in range(repeat):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return statistics.median(samples)


def _peak_kib(fn: Callable[[], bytes]) -> float:
    tracemalloc.start()
    tracemalloc.reset_peak()
    base, _ = tracemalloc.get_traced_memory()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return (peak - base) / 1024


def _distinct_objects(tree: Any) -> int:
    seen: set[int] = set()
    stack: list[Any] = [tree]
    while stack:
        obj: object = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, dict):
            mapping: dict[Any, Any] = obj  # pyright: ignore[reportUnknownVariableType]
            stack.extend(mapping.keys())
            stack.extend(mapping.values())
        elif isinstance(obj, list):
            items: list[Any] = obj  # pyright: ignore[reportUnknownVariableType]
            stack.extend(items)
    return len(seen)


def bench_encoders() -> None:
    p_data, m_data, d_data = generate_benchmark_datasets(ITEMS)
    adapter = pydantic.TypeAdapter(list[BenchmarkItemPydantic])
    paths: list[tuple[str, Callable[[], bytes]]] = [
        ("msgspec.json.encode(structs)", lambda: serialize_msgspec_encode(m_data)),
        (
            "TypeAdapter.dump_json(models)",
            lambda: serialize_pydantic_rust_dump_json(adapter, p_data),
        ),
        ("orjson.dumps(dicts)", lambda: serialize_orjson_dicts(d_data)),
        (
            "dump_python(mode=json)+orjson",
            lambda: serialize_pydantic_orjson_pipeline(adapter, p_data),
        ),
        ("json.dumps(dicts) default seps", lambda: serialize_stdlib_json(d_data)),
        (
            "json.dumps(dicts) compact",
            lambda: json.dumps(
                d_data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode(),
        ),
    ]
    print(f"encoders, {ITEMS} items, median of {REPEAT} calls")
    for name, fn in paths:
        size = len(fn())
        ms = _median_ms(fn)
        print(f"  {name:32s} {ms:8.2f} ms  peak {_peak_kib(fn):9.1f} KiB  {size} bytes")
    tree = adapter.dump_python(p_data, mode="json")
    print(f"  distinct objects in dump_python(mode=json) tree: {_distinct_objects(tree)}")


def bench_sizes() -> None:
    s = BenchmarkItemMsgSpec(id=1000, name="item_1000", active=True, score=1.5)
    m = BenchmarkItemPydantic(id=1000, name="item_1000", active=True, score=1.5)
    d = {"id": 1000, "name": "item_1000", "active": True, "score": 1.5}
    print("sys.getsizeof containers (field values excluded)")
    print(f"  msgspec.Struct {sys.getsizeof(s)}  hasattr __dict__ {hasattr(s, '__dict__')}")
    print(f"  dict(4 keys) {sys.getsizeof(d)}")
    print(f"  BaseModel {sys.getsizeof(m)} + __dict__ {sys.getsizeof(m.__dict__)}")


def build_app() -> FastAPI:
    p_data, m_data, d_data = generate_benchmark_datasets(ITEMS)
    adapter = pydantic.TypeAdapter(list[BenchmarkItemPydantic])
    app = FastAPI()

    @app.get("/msgspec-direct")
    def msgspec_direct() -> Response:
        return MsgSpecResponse(m_data)

    @app.get("/pydantic-direct-dump")
    def pydantic_direct_dump() -> Response:
        return Response(adapter.dump_json(p_data), media_type="application/json")

    @app.get("/default-dump-json", response_model=list[BenchmarkItemPydantic])  # noqa: FAST001
    def default_dump_json() -> list[BenchmarkItemPydantic]:
        return p_data

    @app.get(
        "/orjson-with-model",
        response_model=list[BenchmarkItemPydantic],  # noqa: FAST001
        response_class=ORJSONResponse,  # pyright: ignore[reportDeprecated]
    )
    def orjson_with_model() -> list[BenchmarkItemPydantic]:
        return p_data

    @app.get("/stdlib-json-dicts", response_model=None)
    def stdlib_json_dicts() -> Any:
        return d_data

    return app


async def bench_http() -> None:
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    print(f"in-process ASGI round trip via httpx.ASGITransport, {ITEMS} items")
    print(f"  median of {HTTP_REQUESTS} requests after one warm-up request")
    async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
        for path in [
            "/msgspec-direct",
            "/pydantic-direct-dump",
            "/default-dump-json",
            "/orjson-with-model",
            "/stdlib-json-dicts",
        ]:
            first = await client.get(path)
            first.raise_for_status()
            samples: list[float] = []
            for _ in range(HTTP_REQUESTS):
                start = time.perf_counter_ns()
                resp = await client.get(path)
                samples.append((time.perf_counter_ns() - start) / 1e6)
                resp.raise_for_status()
            print(f"  {path:24s} {statistics.median(samples):8.2f} ms  {len(first.content)} bytes")


def main() -> None:
    warnings.simplefilter("ignore")
    py = platform.python_version()
    print(f"CPython {py} {platform.machine()} {platform.system()} {platform.release()}")
    print(f"msgspec {msgspec.__version__} pydantic {pydantic.VERSION}")
    bench_sizes()
    bench_encoders()
    asyncio.run(bench_http())


if __name__ == "__main__":
    main()
