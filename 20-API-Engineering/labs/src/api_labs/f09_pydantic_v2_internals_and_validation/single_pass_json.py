"""Compare model_validate_json (one Rust pass) with model_validate(json.loads(...)) (two passes)."""

import json
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


class OrderLine(BaseModel):
    """One line of an order; every field is a JSON scalar."""

    sku: str
    qty: int
    unit_price: float


class Order(BaseModel):
    """Lax model: date and UUID arrive as JSON strings and are coerced."""

    order_id: UUID
    placed_on: date
    lines: list[OrderLine]
    tags: list[str]


class StrictOrder(BaseModel):
    """Same shape in strict mode: str -> date and str -> UUID are allowed only from JSON."""

    model_config = ConfigDict(strict=True)

    order_id: UUID
    placed_on: date
    lines: list[OrderLine]


class Price(BaseModel):
    """Decimal field fed from a JSON number."""

    amount: Decimal


class Label(BaseModel):
    """Single str field used to observe the jiter string cache."""

    text: str


class UncachedLabel(BaseModel):
    """Same field with cache_strings disabled."""

    model_config = ConfigDict(cache_strings=False)

    text: str


SEEN_BEFORE_INPUT_TYPES: list[str] = []


class AuditedOrder(BaseModel):
    """A mode='before' model validator: it must receive Python objects, not the Rust tree."""

    order_id: UUID
    lines: list[OrderLine]

    @model_validator(mode="before")
    @classmethod
    def record_input_type(cls, data: Any) -> Any:
        SEEN_BEFORE_INPUT_TYPES.append(type(data).__name__)
        return data


def make_payload(n_lines: int) -> bytes:
    """Deterministic UTF-8 JSON order with n_lines lines and distinct SKU strings."""
    document = {
        "order_id": "6f1c1d3e-8a4b-4c7e-9f00-0123456789ab",
        "placed_on": "2026-11-12",
        "lines": [
            {"sku": f"SKU-{i:08d}", "qty": i % 7 + 1, "unit_price": i * 0.25}
            for i in range(n_lines)
        ],
        "tags": ["priority", "wholesale"],
    }
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def validate_two_pass(raw: bytes) -> Order:
    """CPython json scanner builds dicts/lists/strs, then pydantic-core walks them."""
    return Order.model_validate(json.loads(raw))


def validate_single_pass(raw: bytes) -> Order:
    """jiter parses into a Rust JsonValue tree; only validated outputs become Python objects."""
    return Order.model_validate_json(raw)


def traced_peak_bytes(fn: Callable[[bytes], Order], raw: bytes) -> int:
    """Peak bytes traced by tracemalloc during one call (Python allocator domain only).

    Rust heap allocations made by jiter and pydantic-core go through the system allocator,
    which tracemalloc does not see, so this measures Python object churn, not total RSS.
    """
    fn(raw)  # warm up: import-time caches and the jiter string cache
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline, _ = tracemalloc.get_traced_memory()
        result = fn(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del result
    return peak - baseline


def traced_intermediate_tree_bytes(raw: bytes) -> int:
    """Bytes traced while json.loads alone builds the throwaway dict/list/str tree."""
    json.loads(raw)
    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        tree = json.loads(raw)
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del tree
    return current - baseline


@dataclass(frozen=True, slots=True)
class BenchResult:
    """Best-of-N wall time per call in nanoseconds and the two-pass/single-pass ratio."""

    n_lines: int
    payload_bytes: int
    two_pass_ns: int
    single_pass_ns: int

    @property
    def speedup(self) -> float:
        return self.two_pass_ns / self.single_pass_ns


def _best_ns(fn: Callable[[bytes], Order], raw: bytes, repeats: int, loops: int) -> int:
    best = 0
    for _ in range(repeats):
        start = time.perf_counter_ns()
        for _ in range(loops):
            fn(raw)
        elapsed = (time.perf_counter_ns() - start) // loops
        best = elapsed if best == 0 else min(best, elapsed)
    return best


def benchmark(n_lines: int, repeats: int = 7, loops: int = 20) -> BenchResult:
    """Best-of-repeats mean per call; not asserted in tests because wall time is noisy."""
    raw = make_payload(n_lines)
    return BenchResult(
        n_lines=n_lines,
        payload_bytes=len(raw),
        two_pass_ns=_best_ns(validate_two_pass, raw, repeats, loops),
        single_pass_ns=_best_ns(validate_single_pass, raw, repeats, loops),
    )


def error_types(raw: str | bytes, model: type[BaseModel]) -> list[str]:
    """Run model_validate_json and return the error type of every line error ([] if valid)."""
    try:
        model.model_validate_json(raw)
    except ValidationError as exc:
        return [str(err["type"]) for err in exc.errors()]
    return []


def nested_arrays(depth: int) -> str:
    """A list field holding `depth` nested JSON arrays inside the top-level object."""
    return '{"lines":' + "[" * depth + "]" * depth + "}"


class Nested(BaseModel):
    """Accepts arbitrarily nested lists so only the parser limit can reject input."""

    lines: list[Any]


def build_ingest_app() -> FastAPI:
    """Two routes over StrictOrder: FastAPI's body parsing vs reading raw bytes."""
    app = FastAPI()

    @app.post("/orders/framework")
    async def framework_body(order: StrictOrder) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        # FastAPI 0.162.0: request.json() (json.loads) then TypeAdapter.validate_python.
        return {"placed_on": order.placed_on.isoformat()}

    @app.post("/orders/raw")
    async def raw_body(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        try:
            order = StrictOrder.model_validate_json(await request.body())
        except ValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": exc.errors(include_url=False, include_input=False)},
            )
        return JSONResponse({"placed_on": order.placed_on.isoformat()})

    return app


def main() -> None:
    """Print benchmark and tracemalloc figures quoted in the note."""
    import platform
    import sys

    print(f"python {sys.version.split()[0]} {platform.system()} {platform.machine()}")
    for n_lines in (10, 1_000, 10_000):
        raw = make_payload(n_lines)
        result = benchmark(n_lines)
        two_peak = traced_peak_bytes(validate_two_pass, raw)
        one_peak = traced_peak_bytes(validate_single_pass, raw)
        tree = traced_intermediate_tree_bytes(raw)
        print(
            f"lines={n_lines} bytes={result.payload_bytes} "
            f"two_pass_us={result.two_pass_ns / 1000:.1f} "
            f"single_pass_us={result.single_pass_ns / 1000:.1f} "
            f"speedup={result.speedup:.2f}x "
            f"traced_peak_two={two_peak} traced_peak_single={one_peak} loads_tree={tree}"
        )


if __name__ == "__main__":
    main()
