"""Strangler-fig migration from pydantic.v1 (1.10.27) to Pydantic v2 (2.14.3).

The module pairs one legacy v1 domain model with its v2 ports and provides the three tools a
large migration needs: a shadow validator that runs both engines on the same payload and
classifies the divergence, boundary adapters that convert between the two model families
through Python objects (never JSON), and a config audit that finds v1 keys v2 silently ignores.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field, PlainSerializer, ValidationError
from pydantic import v1 as pydantic_v1
from pydantic._internal._config import V2_REMOVED_KEYS, V2_RENAMED_KEYS

# --- Legacy v1 models, exactly as a 1.x codebase wrote them ---------------------------------


class LegacyLineItem(pydantic_v1.BaseModel):
    sku: str
    quantity: int
    unit_price: Decimal


class LegacyOrder(pydantic_v1.BaseModel):
    order_id: int | str
    customer_note: str | None  # v1: implicitly optional with default None
    items: list[LegacyLineItem]
    hold_for: timedelta = timedelta(0)


# --- Straight v2 port: what bump-pydantic plus BP001 produces ---------------------------------


class LineItem(BaseModel):
    sku: str
    quantity: int
    unit_price: Decimal


class Order(BaseModel):
    order_id: int | str
    customer_note: str | None = None  # BP001: the implicit v1 default made explicit
    items: list[LineItem]
    hold_for: timedelta = timedelta(0)


# --- Contract-preserving v2 port: v2 engine, v1 wire format ---------------------------------


def _seconds(value: timedelta) -> float:
    return value.total_seconds()


V1JsonDecimal = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
V1JsonTimedelta = Annotated[
    timedelta, PlainSerializer(_seconds, return_type=float, when_used="json")
]


class WireCompatLineItem(BaseModel):
    sku: str
    quantity: int
    unit_price: V1JsonDecimal


class WireCompatOrder(BaseModel):
    order_id: int | str = Field(union_mode="left_to_right")
    customer_note: str | None = None
    items: list[WireCompatLineItem]
    hold_for: V1JsonTimedelta = timedelta(0)


# --- Shadow validation: run both engines, classify the difference ---------------------------

Outcome = Literal["accepted", "rejected"]
Verdict = Literal["same", "wire-drift", "value-drift", "v2-stricter", "v2-looser", "both-rejected"]


@dataclass(frozen=True, slots=True)
class ShadowResult:
    """One payload validated by both engines, with the parsed values and wire output of each."""

    v1_outcome: Outcome
    v2_outcome: Outcome
    v1_python: dict[str, Any] | None
    v2_python: dict[str, Any] | None
    v1_wire: Any
    v2_wire: Any
    v1_error_types: tuple[str, ...]
    v2_error_types: tuple[str, ...]

    @property
    def verdict(self) -> Verdict:
        if self.v1_outcome == "rejected" and self.v2_outcome == "rejected":
            return "both-rejected"
        if self.v1_outcome == "accepted" and self.v2_outcome == "rejected":
            return "v2-stricter"
        if self.v1_outcome == "rejected":
            return "v2-looser"
        if self.v1_python != self.v2_python:
            return "value-drift"
        if self.v1_wire != self.v2_wire:
            return "wire-drift"
        return "same"


def shadow_validate(
    payload: Mapping[str, Any],
    legacy: type[pydantic_v1.BaseModel],
    modern: type[BaseModel],
) -> ShadowResult:
    """Validate one payload with the v1 and the v2 model and record both results.

    Wire output is compared after `json.loads`, so separator and key-spacing differences between
    v1 `json()` (`", "`) and v2 `model_dump_json()` (compact) do not count as drift.
    """
    v1_python: dict[str, Any] | None = None
    v1_wire: Any = None
    v1_errors: tuple[str, ...] = ()
    try:
        old = legacy.parse_obj(dict(payload))
    except pydantic_v1.ValidationError as exc:
        v1_errors = tuple(str(err["type"]) for err in exc.errors())
    else:
        v1_python = old.dict()
        v1_wire = json.loads(old.json())

    v2_python: dict[str, Any] | None = None
    v2_wire: Any = None
    v2_errors: tuple[str, ...] = ()
    try:
        new = modern.model_validate(dict(payload))
    except ValidationError as exc:
        v2_errors = tuple(err["type"] for err in exc.errors())
    else:
        v2_python = new.model_dump()
        v2_wire = json.loads(new.model_dump_json())

    return ShadowResult(
        v1_outcome="rejected" if v1_errors else "accepted",
        v2_outcome="rejected" if v2_errors else "accepted",
        v1_python=v1_python,
        v2_python=v2_python,
        v1_wire=v1_wire,
        v2_wire=v2_wire,
        v1_error_types=v1_errors,
        v2_error_types=v2_errors,
    )


# --- Boundary adapters: the only place the two model families meet --------------------------


def legacy_to_modern[M: BaseModel](legacy: pydantic_v1.BaseModel, modern: type[M]) -> M:
    """Re-validate a v1 instance as a v2 model through Python objects, not JSON.

    `legacy.dict()` keeps `Decimal` and `timedelta` as Python objects, so nothing is lost to the
    v1 JSON encoder (which turns `Decimal` into a float).
    """
    return modern.model_validate(legacy.dict())


def modern_to_legacy[L: pydantic_v1.BaseModel](modern: BaseModel, legacy: type[L]) -> L:
    """Re-validate a v2 instance as a v1 model through Python objects."""
    return legacy.parse_obj(modern.model_dump())


# --- Config audit: v1 keys that v2 accepts, warns about once, and then ignores ---------------


def unapplied_v1_config_keys(model: type[BaseModel]) -> dict[str, str | None]:
    """Return v1-only keys left in a v2 model's config, mapped to their v2 name (None: removed).

    Pydantic v2 copies an unknown key such as `orm_mode` into `model_config`, emits one
    `UserWarning` at class creation, and never applies it; this turns that warning into data a
    CI check can fail on.
    """
    keys = set(model.model_config)
    found: dict[str, str | None] = {k: V2_RENAMED_KEYS[k] for k in keys & V2_RENAMED_KEYS.keys()}
    found.update(dict.fromkeys(keys & V2_REMOVED_KEYS))
    return found


# --- Edge-first strangler: FastAPI speaks v2 only, the domain core is still v1 ---------------


def legacy_reprice(order: LegacyOrder) -> LegacyOrder:
    """Stand-in for untouched v1 domain code: applies a 10 percent discount to every line."""
    items = [
        item.copy(update={"unit_price": item.unit_price * Decimal("0.9")}) for item in order.items
    ]
    return order.copy(update={"items": items})


def build_edge_app() -> FastAPI:
    """An API whose request and response models are v2 while the domain core stays v1."""
    app = FastAPI()

    @app.post("/orders/reprice")
    async def reprice(order: Order) -> WireCompatOrder:  # pyright: ignore[reportUnusedFunction]
        repriced = legacy_reprice(modern_to_legacy(order, LegacyOrder))
        return legacy_to_modern(repriced, WireCompatOrder)

    return app
