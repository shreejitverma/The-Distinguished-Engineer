"""Measure Pydantic v2 lax versus strict coercion across Python and JSON input."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, TypedDict
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

ORDER_ID: Final = "12345678-1234-5678-1234-567812345678"


def outcome(adapter: TypeAdapter[Any], value: Any, *, json_input: bool, strict: bool | None) -> str:
    """Return `type:repr` on success or `!error_type` for the first error."""
    try:
        if json_input:
            result = adapter.validate_json(value, strict=strict)
        else:
            result = adapter.validate_python(value, strict=strict)
    except ValidationError as exc:
        return "!" + str(exc.errors(include_url=False)[0]["type"])
    return f"{type(result).__name__}:{result!r}"


@dataclass(frozen=True, slots=True)
class MatrixRow:
    """One target type and input fed through both input channels in both modes."""

    python_lax: str
    python_strict: str
    json_lax: str
    json_strict: str


def coercion_row(target: Any, python_value: Any, json_text: str) -> MatrixRow:
    """Validate the same logical value as a Python object and as a JSON document."""
    adapter: TypeAdapter[Any] = TypeAdapter(target)
    return MatrixRow(
        python_lax=outcome(adapter, python_value, json_input=False, strict=None),
        python_strict=outcome(adapter, python_value, json_input=False, strict=True),
        json_lax=outcome(adapter, json_text, json_input=True, strict=None),
        json_strict=outcome(adapter, json_text, json_input=True, strict=True),
    )


COERCION_CASES: Final[dict[str, tuple[Any, Any, str]]] = {
    "int <- '42'": (int, "42", '"42"'),
    "int <- 42.0": (int, 42.0, "42.0"),
    "int <- 42.5": (int, 42.5, "42.5"),
    "int <- True": (int, True, "true"),
    "float <- 1": (float, 1, "1"),
    "float <- '1.5'": (float, "1.5", '"1.5"'),
    "str <- 42": (str, 42, "42"),
    "bool <- 'yes'": (bool, "yes", '"yes"'),
    "bool <- 2": (bool, 2, "2"),
    "date <- '2026-11-13'": (date, "2026-11-13", '"2026-11-13"'),
    "UUID <- str": (UUID, ORDER_ID, f'"{ORDER_ID}"'),
    "Decimal <- '1.10'": (Decimal, "1.10", '"1.10"'),
    "bytes <- 'abc'": (bytes, "abc", '"abc"'),
    "tuple <- list": (tuple[int, ...], [1, 2], "[1,2]"),
}


def build_matrix() -> dict[str, MatrixRow]:
    """Compute the four-cell coercion row for every case in `COERCION_CASES`."""
    return {name: coercion_row(*case) for name, case in COERCION_CASES.items()}


class Venue(enum.IntEnum):
    """An `int` subclass: strict `int` accepts it because it passes `isinstance(v, int)`."""

    NASDAQ = 1


class Side(enum.StrEnum):
    """A `str` subclass: strict `str` accepts it and returns a plain `str`."""

    BUY = "buy"


class FieldStrictOrder(BaseModel):
    """Field-level strictness via `StrictInt`, `StrictStr`, and `Field(strict=True)`."""

    qty: StrictInt
    symbol: StrictStr
    limit_ticks: int = Field(default=0, strict=True)


def model_outcome(
    model_cls: type[BaseModel], data: Any, *, strict: bool | None
) -> dict[str, Any] | list[tuple[tuple[int | str, ...], str]]:
    """Return `model_dump()` on success, else `(loc, type)` pairs for every error."""
    try:
        return model_cls.model_validate(data, strict=strict).model_dump()
    except ValidationError as exc:
        return [(err["loc"], err["type"]) for err in exc.errors(include_url=False)]


class LegDict(TypedDict):
    """A TypedDict has no config of its own, so it inherits the parent's `strict`."""

    qty: int


class LegModel(BaseModel):
    """A nested BaseModel keeps its own (lax) config."""

    qty: int


@pydantic_dataclass
class LegDataclass:
    """A Pydantic dataclass keeps its own (lax) config."""

    qty: int


class StrictTicket(BaseModel):
    """`ConfigDict(strict=True)` reaches only the fields this model's config governs."""

    model_config = ConfigDict(strict=True)

    td_leg: LegDict
    model_leg: LegModel
    dc_leg: LegDataclass
    override: int = Field(default=0, strict=False)


ParsedQty = Annotated[int, BeforeValidator(int), Strict()]
"""Strict is applied to the inner `int` schema; the before-validator runs first."""


class StrictTrade(BaseModel):
    """A strict body whose `date` field only accepts strings from JSON input."""

    model_config = ConfigDict(strict=True)

    trade_date: date
    qty: int


def build_trade_app() -> FastAPI:
    """Two routes: FastAPI's `json.loads` path and a single-pass `model_validate_json` path."""
    app = FastAPI()

    @app.post("/v1/trades/parsed")
    async def parsed(trade: StrictTrade) -> dict[str, str]:
        return {"trade_date": trade.trade_date.isoformat()}

    @app.post("/v1/trades/raw")
    async def raw(request: Request) -> dict[str, str]:
        try:
            trade = StrictTrade.model_validate_json(await request.body())
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False)) from exc
        return {"trade_date": trade.trade_date.isoformat()}

    return app
