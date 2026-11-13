"""Where Pydantic v2 validation time goes: Python boundary crossings, fail_fast, and peers."""

from __future__ import annotations

import gc
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

import msgspec
import orjson
from pydantic import (
    AfterValidator,
    BaseModel,
    FailFast,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType

PRICE_COUNT = 100


def _non_negative(value: float) -> float:
    """Per-item Python check; pydantic-core calls it once per list element."""
    if value < 0:
        raise ValueError("price must be >= 0")
    return value


class RustOnlyOrder(BaseModel):
    """Every constraint lives in the CoreSchema, so validation never enters Python."""

    sku: Annotated[str, Field(min_length=1, max_length=32)]
    qty: Annotated[int, Field(ge=1, le=10_000)]
    prices: list[Annotated[float, Field(ge=0)]]


class FieldValidatorOrder(BaseModel):
    """Same contract, enforced by two @field_validator functions (one call per field)."""

    sku: str
    qty: int
    prices: list[float]

    @field_validator("qty")
    @classmethod
    def check_qty(cls, value: int) -> int:
        if not 1 <= value <= 10_000:
            raise ValueError("qty out of range")
        return value

    @field_validator("prices")
    @classmethod
    def check_prices(cls, value: list[float]) -> list[float]:
        if any(price < 0 for price in value):
            raise ValueError("price must be >= 0")
        return value


class ItemValidatorOrder(BaseModel):
    """Same contract, enforced by an AfterValidator on each list item (one call per item)."""

    sku: Annotated[str, Field(min_length=1, max_length=32)]
    qty: Annotated[int, Field(ge=1, le=10_000)]
    prices: list[Annotated[float, AfterValidator(_non_negative)]]


class StructOrder(msgspec.Struct, frozen=True):
    """msgspec equivalent: constraints in msgspec.Meta, checked in C during decoding."""

    sku: Annotated[str, msgspec.Meta(min_length=1, max_length=32)]
    qty: Annotated[int, msgspec.Meta(ge=1, le=10_000)]
    prices: list[Annotated[float, msgspec.Meta(ge=0)]]


@dataclass(frozen=True, slots=True)
class DataclassOrder:
    """Plain dataclass: orjson.loads plus **kwargs construction validates nothing."""

    sku: str
    qty: int
    prices: list[float]


def make_payload(price_count: int = PRICE_COUNT) -> bytes:
    """Deterministic order document with `price_count` non-negative prices."""
    prices = [round(i * 1.25, 2) for i in range(price_count)]
    return orjson.dumps({"sku": "SKU-1", "qty": 3, "prices": prices})


def python_frames_during(fn: Callable[[], object]) -> list[str]:
    """Return the qualnames of Python functions started while `fn` runs (PEP 669)."""
    monitoring = sys.monitoring
    tool_id = next(i for i in range(6) if monitoring.get_tool(i) is None)
    names: list[str] = []
    thunk = getattr(fn, "__code__", None)

    def on_start(code: CodeType, instruction_offset: int) -> None:
        if code is not thunk:
            names.append(code.co_qualname)

    monitoring.use_tool_id(tool_id, "validation_cost")
    try:
        monitoring.register_callback(tool_id, monitoring.events.PY_START, on_start)
        monitoring.set_events(tool_id, monitoring.events.PY_START)
        fn()
    finally:
        monitoring.set_events(tool_id, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool_id, monitoring.events.PY_START, None)
        monitoring.free_tool_id(tool_id)
    return names


@dataclass(frozen=True, slots=True)
class FailFastReport:
    """Outcome of validating one list with and without fail_fast."""

    errors_default: int
    errors_fail_fast: int
    item_calls_default: int
    item_calls_fail_fast: int


def fail_fast_probe(items: list[object]) -> FailFastReport:
    """Count item-validator calls and reported errors for list[int] with/without FailFast."""
    calls = [0]

    def count(value: int) -> int:
        calls[0] += 1
        return value

    collect_all: TypeAdapter[list[int]] = TypeAdapter(list[Annotated[int, AfterValidator(count)]])
    stop_first: TypeAdapter[list[int]] = TypeAdapter(
        Annotated[list[Annotated[int, AfterValidator(count)]], FailFast()]
    )

    def run(adapter: TypeAdapter[list[int]]) -> tuple[int, int]:
        calls[0] = 0
        try:
            adapter.validate_python(items)
        except ValidationError as exc:
            return exc.error_count(), calls[0]
        return 0, calls[0]

    errors_default, calls_default = run(collect_all)
    errors_ff, calls_ff = run(stop_first)
    return FailFastReport(errors_default, errors_ff, calls_default, calls_ff)


def best_ns_per_call(fn: Callable[[], object], *, number: int, repeat: int) -> float:
    """Minimum over `repeat` runs of the mean ns per call, GC disabled like timeit."""
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        best = float("inf")
        for _ in range(repeat):
            start = time.perf_counter_ns()
            for _ in range(number):
                fn()
            best = min(best, (time.perf_counter_ns() - start) / number)
        return best
    finally:
        if gc_was_enabled:
            gc.enable()


def decode_variants(payload: bytes) -> dict[str, Callable[[], object]]:
    """JSON bytes -> typed object, one thunk per strategy, all reused (no per-call setup)."""
    decoder = msgspec.json.Decoder(StructOrder)

    def dataclass_orjson() -> DataclassOrder:
        return DataclassOrder(**orjson.loads(payload))

    return {
        "pydantic rust-only validate_json": lambda: RustOnlyOrder.model_validate_json(payload),
        "pydantic @field_validator x2": lambda: FieldValidatorOrder.model_validate_json(payload),
        "pydantic AfterValidator per item": lambda: ItemValidatorOrder.model_validate_json(payload),
        "pydantic validate(orjson.loads)": lambda: RustOnlyOrder.model_validate(
            orjson.loads(payload)
        ),
        "msgspec Decoder(Struct)": lambda: decoder.decode(payload),
        "orjson.loads + dataclass (no checks)": dataclass_orjson,
    }


def encode_variants(payload: bytes) -> dict[str, Callable[[], object]]:
    """Typed object -> JSON bytes, one thunk per strategy."""
    model = RustOnlyOrder.model_validate_json(payload)
    struct = msgspec.json.decode(payload, type=StructOrder)
    plain = DataclassOrder(**orjson.loads(payload))
    encoder = msgspec.json.Encoder()
    return {
        "pydantic model_dump_json": model.model_dump_json,
        "orjson.dumps(model_dump())": lambda: orjson.dumps(model.model_dump()),
        "msgspec Encoder.encode(Struct)": lambda: encoder.encode(struct),
        "orjson.dumps(dataclass)": lambda: orjson.dumps(plain),
    }


def fail_fast_variants(bad_count: int) -> dict[str, Callable[[], object]]:
    """Validate `bad_count` invalid items, collecting every error vs stopping at the first."""
    data = b"[" + b",".join([b'"x"'] * bad_count) + b"]"
    collect_all: TypeAdapter[list[int]] = TypeAdapter(list[int])
    stop_first: TypeAdapter[list[int]] = TypeAdapter(Annotated[list[int], FailFast()])

    def attempt(adapter: TypeAdapter[list[int]]) -> Callable[[], object]:
        def run() -> object:
            try:
                return adapter.validate_json(data)
            except ValidationError as exc:
                return exc

        return run

    return {
        "list[int] collect all": attempt(collect_all),
        "FailFast list[int]": attempt(stop_first),
    }


def report(variants: dict[str, Callable[[], object]], *, number: int, repeat: int) -> list[str]:
    """Format a results table relative to the fastest variant."""
    timings = {
        name: best_ns_per_call(fn, number=number, repeat=repeat) for name, fn in variants.items()
    }
    fastest = min(timings.values())
    return [
        f"{name:<38} {ns / 1000:>9.2f} us {ns / fastest:>7.1f}x" for name, ns in timings.items()
    ]


def main() -> None:
    payload = make_payload()
    versions: dict[str, Any] = {
        "python": sys.version.split()[0],
        "msgspec": msgspec.__version__,
        "orjson": orjson.__version__,
    }
    print(versions, f"payload={len(payload)} bytes, {PRICE_COUNT} prices")
    print("decode + validate (min of 7 x 2000):")
    print("\n".join(report(decode_variants(payload), number=2000, repeat=7)))
    print("encode (min of 7 x 2000):")
    print("\n".join(report(encode_variants(payload), number=2000, repeat=7)))
    print("10000 invalid items (min of 5 x 20):")
    print("\n".join(report(fail_fast_variants(10_000), number=20, repeat=5)))


if __name__ == "__main__":
    main()
