"""Additive schema evolution: tolerant readers, optionality defaults, and fail-closed writers.

Two server builds share one order store, as they do during a rolling deploy or a rollback:
v1 knows `sku`, `quantity`, `status`; v2 additively adds the response and request field
`gift_note`, the request-only flag `dry_run`, and the status value `on_hold`.
"""

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

type ExtraMode = Literal["allow", "ignore", "forbid"]
type Record = dict[str, Any]


@dataclass
class OrderStore:
    """Shared persistence for every server build, keyed by order id."""

    orders: dict[str, Record] = field(default_factory=dict[str, Record])
    next_id: int = 1

    def insert(self, record: Record) -> str:
        order_id = f"ord_{self.next_id}"
        self.next_id += 1
        self.orders[order_id] = {**record, "id": order_id}
        return order_id


# --- server v1 -------------------------------------------------------------------------------


class OrderOutV1(BaseModel):
    """v1 response contract; FastAPI validates every response through it."""

    id: str
    sku: str
    quantity: int
    status: Literal["pending", "shipped"]


def build_v1_app(store: OrderStore, *, request_extra: ExtraMode) -> FastAPI:
    """Old server build whose request model handles unknown fields per `request_extra`."""

    class OrderCreateV1(BaseModel):
        model_config = ConfigDict(extra=request_extra)

        sku: str = Field(min_length=1, max_length=64)
        quantity: int = Field(ge=1, le=1000)

    app = FastAPI()

    @app.post("/orders", status_code=201)
    async def create_order(body: OrderCreateV1) -> OrderOutV1:
        # Spreading the body last is the mass-assignment bug that extra="allow" arms.
        order_id = store.insert({"status": "pending", **body.model_dump()})
        return OrderOutV1.model_validate(store.orders[order_id])

    @app.get("/orders/{order_id}", response_model=OrderOutV1)
    async def get_order(order_id: str) -> Record:
        # FastAPI validates the raw record against OrderOutV1 (serialize_response):
        # unknown members are dropped, unknown enum values fail the response.
        if order_id not in store.orders:
            raise HTTPException(status_code=404)
        return store.orders[order_id]

    return app


# --- server v2: additive only ----------------------------------------------------------------


class OrderCreateV2(BaseModel):
    """v2 request: every new field is optional and its default reproduces v1 behavior."""

    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=1000)
    gift_note: str | None = Field(default=None, max_length=200)
    dry_run: bool = False


class OrderReplaceV2(BaseModel):
    """PUT body: a full replacement of the client-writable state (RFC 9110 Section 9.3.4)."""

    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=1000)
    gift_note: str | None = Field(default=None, max_length=200)


class OrderOutV2(BaseModel):
    """v2 response: a superset of OrderOutV1 plus one new enum value."""

    id: str
    sku: str
    quantity: int
    status: Literal["pending", "shipped", "on_hold"]
    gift_note: str | None = None


def build_v2_app(store: OrderStore) -> FastAPI:
    """New server build: additive fields, fail-closed request models."""
    app = FastAPI()

    @app.post("/orders", status_code=201)
    async def create_order(body: OrderCreateV2, response: Response) -> OrderOutV2:
        record: Record = {**body.model_dump(exclude={"dry_run"}), "status": "pending"}
        if body.dry_run:
            response.status_code = 200
            return OrderOutV2.model_validate({**record, "id": "preview"})
        order_id = store.insert(record)
        return OrderOutV2.model_validate(store.orders[order_id])

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str) -> OrderOutV2:
        if order_id not in store.orders:
            raise HTTPException(status_code=404)
        return OrderOutV2.model_validate(store.orders[order_id])

    @app.put("/orders/{order_id}")
    async def replace_order(order_id: str, body: OrderReplaceV2) -> OrderOutV2:
        if order_id not in store.orders:
            raise HTTPException(status_code=404)
        store.orders[order_id] = {**store.orders[order_id], **body.model_dump()}
        return OrderOutV2.model_validate(store.orders[order_id])

    return app


# --- client readers --------------------------------------------------------------------------

KNOWN_STATUSES = frozenset({"pending", "shipped"})


def _unknown_status_to_fallback(value: object) -> object:
    """Map an unknown status string to "unknown"; leave non-strings to fail validation."""
    if isinstance(value, str) and value not in KNOWN_STATUSES:
        return "unknown"
    return value


type TolerantStatus = Annotated[
    Literal["pending", "shipped", "unknown"], BeforeValidator(_unknown_status_to_fallback)
]


class StrictOrderViewV1(BaseModel):
    """Anti-pattern: a v1-era client that treats every response object as closed."""

    model_config = ConfigDict(extra="forbid")

    id: str
    sku: str
    quantity: int
    status: Literal["pending", "shipped"]


class TolerantOrderViewV1(BaseModel):
    """v1-era tolerant reader: drops unknown members, maps unknown enum values."""

    model_config = ConfigDict(extra="ignore")

    id: str
    sku: str
    quantity: int
    status: TolerantStatus


class PreservingOrderViewV1(TolerantOrderViewV1):
    """v1-era tolerant reader that keeps unknown members for read-modify-write."""

    model_config = ConfigDict(extra="allow")


class TolerantOrderViewV2(TolerantOrderViewV1):
    """v2-era reader: the new field has a default because v1 servers still answer."""

    gift_note: str | None = None


class EagerOrderViewV2(TolerantOrderViewV1):
    """Bug: `str | None` without a default is required in Pydantic v2, not optional."""

    gift_note: str | None


def writable_state(view: TolerantOrderViewV1) -> Record:
    """Build a PUT body from a reader: writable fields plus any preserved unknown members."""
    return view.model_dump(exclude={"id", "status"})


# --- defense in depth ------------------------------------------------------------------------


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a member name (RFC 8259 Section 4)."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate member name {key!r}")
        result[key] = value
    return result


def loads_rejecting_duplicate_keys(raw: bytes) -> Any:
    """Parse JSON, failing closed on duplicate member names instead of keeping the last one."""
    return json.loads(raw, object_pairs_hook=_reject_duplicates)
