"""Reusable Annotated domain types whose constraints execute inside pydantic-core.

Every constraint below is lowered by GenerateSchema into keys on a leaf CoreSchema
(`str`, `int`), so the Rust validators StrConstrainedValidator and
ConstrainedIntValidator enforce them without calling back into Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, cast

import annotated_types as at
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

MAX_CENTS = 10**12
"""Upper bound for a single monetary amount: 10 billion units of a 2-decimal currency."""

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
"""Trimmed text; whitespace-only input fails `string_too_short` because trimming runs first."""

PositiveCents = Annotated[int, Field(strict=True, gt=0, le=MAX_CENTS)]
"""Integer minor units; strict mode rejects `"5"`, `5.0`, and `True`."""

CurrencyCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{3}$")]
"""ISO 4217 alphabetic code shape; case-sensitive on purpose (see UPPERCASING_CURRENCY_TRAP)."""

UPPERCASING_CURRENCY_TRAP = Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Z]{3}$")]
"""Looks like it accepts `usd`, but pydantic-core checks `pattern` before applying `to_upper`."""

type Sku = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{3,31}$")]
"""PEP 695 alias: one `definitions` entry in CoreSchema and one `$defs` entry in JSON Schema."""

Quantity = Annotated[int, at.Interval(gt=0, le=10_000)]
"""annotated-types GroupedMetadata, expanded into Gt and Le before lowering."""


class LineItem(BaseModel):
    """One invoice line; every field is a reusable domain type."""

    sku: Sku
    description: NonEmptyStr
    unit_price: PositiveCents
    quantity: Quantity


class Invoice(BaseModel):
    """Invoice whose list-level bounds also lower into the Rust `list` validator."""

    currency: CurrencyCode
    lines: Annotated[list[LineItem], Field(min_length=1, max_length=100)]
    tags: list[NonEmptyStr] = Field(default_factory=list)
    replaces_sku: Sku | None = None


class PythonReCurrency(BaseModel):
    """Same CurrencyCode type, but compiled with the Python `re` engine."""

    model_config = ConfigDict(regex_engine="python-re")

    currency: CurrencyCode


class UnionBoundPayload(BaseModel):
    """A bound on a union cannot lower into a member, so it becomes a Python after-validator."""

    value: Annotated[int | str, at.Gt(0)]


def iter_schema_nodes(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every CoreSchema dict reachable from `node`, depth first."""
    if isinstance(node, dict):
        mapping = cast("dict[str, Any]", node)
        if isinstance(mapping.get("type"), str):
            yield mapping
        for key, value in mapping.items():
            if key != "metadata":
                yield from iter_schema_nodes(value)
    elif isinstance(node, list | tuple):
        for item in cast("Iterable[Any]", node):
            yield from iter_schema_nodes(item)


def python_callback_nodes(schema: Any) -> list[str]:
    """Return the CoreSchema node types that call a Python function during validation."""
    return [
        node["type"]
        for node in iter_schema_nodes(schema)
        if node["type"].startswith("function-") or node["type"] == "chain"
    ]


def leaf_constraints(schema: Any) -> dict[str, Any]:
    """Return a CoreSchema node without its `metadata` key, for readable comparisons."""
    return {key: value for key, value in schema.items() if key != "metadata"}


def invoice_total_cents(invoice: Invoice) -> int:
    """Sum the line totals; inputs are already bounded, so the product cannot be unbounded."""
    return sum(line.unit_price * line.quantity for line in invoice.lines)


def build_invoice_app() -> FastAPI:
    """FastAPI app: domain types yield `422` on bad input; the union bound yields `500`."""
    app = FastAPI()

    @app.post("/v1/invoices")
    async def create_invoice(invoice: Invoice) -> dict[str, int | str]:
        return {"currency": invoice.currency, "total_cents": invoice_total_cents(invoice)}

    @app.post("/v1/union-bound")
    async def union_bound(payload: UnionBoundPayload) -> dict[str, int | str]:
        return {"value": payload.value}

    return app
