"""Module-level TypeAdapter singletons for non-BaseModel payloads and the cost of rebuilds."""

# No `from __future__ import annotations`: pydantic resolves Decimal and NotRequired at runtime.
import json
import statistics
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Final, NotRequired, TypedDict
from unittest import mock

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.plugin._schema_validator import create_schema_validator


class OrderItem(BaseModel):
    """A model: TypeAdapter(list[OrderItem]) reuses its prebuilt SchemaValidator."""

    sku: str = Field(min_length=1, max_length=32)
    quantity: int = Field(gt=0)
    unit_price: Decimal
    currency: str = "USD"


class OrderLine(TypedDict):
    """A TypedDict: every TypeAdapter that mentions it recompiles its schema."""

    sku: str
    quantity: int
    unit_price: Decimal
    note: NotRequired[str]


# Built once at import time; every request reuses the same Rust validator and serializer.
ORDER_ITEMS: Final = TypeAdapter(list[OrderItem])
ORDER_LINES: Final = TypeAdapter(list[OrderLine])
ID_OR_SLUG: Final = TypeAdapter(int | str)
ID_OR_SLUG_LTR: Final = TypeAdapter(Annotated[int | str, Field(union_mode="left_to_right")])
STRICT_IDS: Final = TypeAdapter(list[int], config=ConfigDict(strict=True))
QUERY_FILTERS: Final = TypeAdapter(dict[str, int])


def parse_items_cached(body: bytes) -> list[OrderItem]:
    """Validate with the import-time singleton: zero schema builds per call."""
    return ORDER_ITEMS.validate_json(body)


def parse_items_rebuilt(body: bytes) -> list[OrderItem]:
    """Anti-pattern: build a fresh TypeAdapter (and Rust validator) on every call."""
    return TypeAdapter(list[OrderItem]).validate_json(body)


def parse_lines_cached(body: bytes) -> list[OrderLine]:
    """Validate a TypedDict list with the import-time singleton."""
    return ORDER_LINES.validate_json(body)


def parse_lines_rebuilt(body: bytes) -> list[OrderLine]:
    """Anti-pattern for a TypedDict payload, where the rebuild also recompiles the TypedDict."""
    return TypeAdapter(list[OrderLine]).validate_json(body)


@contextmanager
def count_validator_builds() -> Generator[mock.MagicMock]:
    """Count SchemaValidator constructions made by TypeAdapter while the block runs."""
    # TypeAdapter._init_core_attrs looks this name up in pydantic.type_adapter's globals.
    with mock.patch(
        "pydantic.type_adapter.create_schema_validator", wraps=create_schema_validator
    ) as spy:
        yield spy


@dataclass(frozen=True, slots=True)
class CostRow:
    """Median wall time of one strategy, in nanoseconds."""

    label: str
    median_ns: int


def median_ns(fn: Callable[[], object], rounds: int) -> int:
    """Median of `rounds` perf_counter_ns samples of fn(), after a 10% warm-up."""
    for _ in range(max(1, rounds // 10)):
        fn()
    samples: list[int] = []
    for _ in range(rounds):
        start = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - start)
    return int(statistics.median(samples))


def order_payload(n: int) -> bytes:
    """A JSON array of n order lines, valid for both OrderItem and OrderLine."""
    rows = [{"sku": f"SKU-{i}", "quantity": i + 1, "unit_price": "9.99"} for i in range(n)]
    return json.dumps(rows).encode()


def measure_costs(n_items: int = 10, rounds: int = 2000) -> list[CostRow]:
    """Compare construction cost with validation cost for model and TypedDict payloads."""
    body = order_payload(n_items)
    strategies: list[tuple[str, Callable[[], object]]] = [
        ("construct TypeAdapter(list[OrderItem])", lambda: TypeAdapter(list[OrderItem])),
        ("construct TypeAdapter(list[OrderLine])", lambda: TypeAdapter(list[OrderLine])),
        ("validate_json cached list[OrderItem]", lambda: parse_items_cached(body)),
        ("validate_json rebuilt list[OrderItem]", lambda: parse_items_rebuilt(body)),
        ("validate_json cached list[OrderLine]", lambda: parse_lines_cached(body)),
        ("validate_json rebuilt list[OrderLine]", lambda: parse_lines_rebuilt(body)),
    ]
    return [CostRow(label, median_ns(fn, rounds)) for label, fn in strategies]


def build_orders_app() -> FastAPI:
    """An endpoint that validates and re-serializes a raw body with the cached TypedDict adapter."""
    app = FastAPI()

    @app.post("/v1/order-lines")
    async def ingest_lines(request: Request) -> Response:
        try:
            lines = ORDER_LINES.validate_json(await request.body())
        except ValidationError as exc:
            detail = exc.json(include_url=False, include_input=False)
            return Response(f'{{"detail":{detail}}}', 422, media_type="application/json")
        return Response(ORDER_LINES.dump_json(lines), 200, media_type="application/json")

    return app


def main() -> None:
    """Print the cost table used in the note."""
    for row in measure_costs():
        print(f"{row.label:<42} {row.median_ns:>9} ns")


if __name__ == "__main__":
    main()
