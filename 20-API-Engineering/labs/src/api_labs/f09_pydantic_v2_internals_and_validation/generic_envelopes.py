"""Generic Pydantic envelopes: PEP 695 type parameters, cached specialization, OpenAPI names."""

import gc
import weakref
from typing import Annotated, Any, Generic

from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing_extensions import TypeVar


class Item(BaseModel):
    """Public catalogue item."""

    sku: str
    price_cents: Annotated[int, Field(ge=0)]


class PromoItem(Item):
    """Subclass carrying a field that the public `Item` contract must not expose."""

    promo_code: str


class PaginatedResponse[T](BaseModel):
    """PEP 695 generic envelope; `PaginatedResponse[X]` is a cached runtime subclass."""

    items: list[T]
    total: Annotated[int, Field(ge=0)]
    next_cursor: str | None = None


class AuditedPage[T](PaginatedResponse[T]):
    """Generic subclass: re-declares `T`, so it stays parametrizable."""

    request_id: str


class ItemPage(PaginatedResponse[Item]):
    """Non-generic specialization: a stable class name, `isinstance` target, and OpenAPI key."""


class BoundedEnvelope[T: Item](BaseModel):
    """Upper bound: unparametrized use validates as `Item` but serializes as `Any`."""

    data: T


class Scalar[T: (int, str)](BaseModel):
    """Constrained type parameter: unparametrized use validates as `int | str`."""

    value: T


# PEP 696 defaults need `class C[T = X]` syntax (CPython >= 3.13); the typing_extensions
# TypeVar keeps this module importable on 3.12 while exposing the same `has_default()` API.
DetailT = TypeVar("DetailT", default=Item)


class DefaultedEnvelope(BaseModel, Generic[DetailT]):
    """PEP 696 default: unparametrized use validates and serializes as the default."""

    data: DetailT


class Pair[K, V](BaseModel):
    """Two-parameter model used to show the partial-specialization cache gap."""

    key: K
    value: V


class Envelope[T](BaseModel):
    """Overrides `model_parametrized_name`, which renames the class but not the `$defs` key."""

    data: T

    @classmethod
    def model_parametrized_name(cls, params: tuple[Any, ...]) -> str:
        names = "".join(getattr(p, "__name__", repr(p)).capitalize() for p in params)
        return f"{names}Envelope"


type NonEmpty[X] = Annotated[list[X], Field(min_length=1)]


class Batch[T](BaseModel):
    """Field typed with a generic PEP 695 `type` alias."""

    rows: NonEmpty[T]


def transient_specialization_is_collected() -> bool:
    """Parametrize with a throwaway model, drop it, and report whether the cache let it go."""

    def build() -> weakref.ref[type[BaseModel]]:
        class Throwaway(BaseModel):
            x: int

        return weakref.ref(PaginatedResponse[Throwaway])

    ref = build()
    gc.collect()
    return ref() is None


def _promo_page() -> PaginatedResponse[PromoItem]:
    promo = PromoItem(sku="sku-1", price_cents=999, promo_code="STAFF-ONLY")
    return PaginatedResponse[PromoItem](items=[promo], total=1)


def build_app() -> FastAPI:
    """Routes whose response models exercise specialization, filtering, and naming."""
    app = FastAPI()

    @app.get("/items", response_model=PaginatedResponse[Item])
    def list_items() -> Any:
        return _promo_page()

    @app.get("/promos")
    def list_promos() -> PaginatedResponse[PromoItem]:
        return _promo_page()

    @app.get("/item-page")
    def item_page() -> ItemPage:
        return ItemPage(items=[], total=0)

    @app.get("/leaky", response_model=BoundedEnvelope)
    def leaky() -> Any:
        return BoundedEnvelope(data=PromoItem(sku="sku-2", price_cents=1, promo_code="LEAK"))

    @app.get("/safe", response_model=BoundedEnvelope[Item])
    def safe() -> Any:
        return BoundedEnvelope(data=PromoItem(sku="sku-3", price_cents=1, promo_code="KEPT"))

    @app.get("/envelope")
    def envelope() -> Envelope[int]:
        return Envelope[int](data=1)

    return app
