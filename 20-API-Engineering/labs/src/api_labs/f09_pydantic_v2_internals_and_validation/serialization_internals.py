"""Pydantic v2 serialization internals: modes, field and model serializers, and when_used."""

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    Field,
    PlainSerializer,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    computed_field,
    field_serializer,
    model_serializer,
)

# Reusable annotated serializer: compiled once, applied wherever the alias is used.
# `return_type=float` makes pydantic-core build a `FloatSerializer` for the result
# instead of inferring the type of whatever the lambda returns.
EpochSeconds = Annotated[
    dt.datetime,
    PlainSerializer(lambda v: v.timestamp(), return_type=float, when_used="unless-none"),
]


class Money(BaseModel):
    """A plain `@model_serializer` replaces the whole fields serializer with one Python call."""

    cents: int
    currency: str

    @model_serializer(mode="plain")
    def as_text(self) -> str:
        return f"{self.cents // 100}.{self.cents % 100:02d} {self.currency}"


class LineItem(BaseModel):
    sku: str
    qty: int
    unit_price: Decimal


class Invoice(BaseModel):
    """Exercises every serializer hook the note describes."""

    invoice_id: int
    issued_at: dt.datetime
    paid_at: EpochSeconds | None = None
    total: Money
    tags: set[str] = Field(default_factory=set[str])
    lines: list[LineItem]
    customer_email: str
    memo: str | None = None

    @field_serializer("issued_at", when_used="json")
    def issued_epoch(self, value: dt.datetime) -> int:
        # Runs only when the serializer mode is exactly "json".
        return int(value.timestamp())

    @field_serializer("tags")
    def sorted_tags(self, value: set[str]) -> list[str]:
        # Plain mode: the default `set` serializer is never called.
        return sorted(value)

    @field_serializer("customer_email", mode="wrap")
    def redact_email(
        self, value: str, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> str:
        # Wrap mode: delegate to the inner `str` serializer, then post-process.
        rendered: str = handler(value)
        context: dict[str, Any] = info.context or {}
        if context.get("role") == "support":
            return rendered
        user, _, domain = rendered.partition("@")
        return f"{user[:1]}***@{domain}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def line_count(self) -> int:
        return len(self.lines)


class Envelope(BaseModel):
    """A wrap `@model_serializer` keeps include/exclude and exclude_* filtering working."""

    seq: int
    body: str
    trace_id: str | None = None

    @model_serializer(mode="wrap")
    def with_schema_version(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        out: dict[str, Any] = handler(self)
        out["schema_version"] = 2
        return out


class FlatEnvelope(BaseModel):
    """The same shape with a plain `@model_serializer`: filters are silently dropped."""

    seq: int
    body: str
    trace_id: str | None = None

    @model_serializer(mode="plain")
    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "body": self.body, "trace_id": self.trace_id, "schema_version": 2}


class Counter(BaseModel):
    """`exclude_defaults` cannot see the default of a field that has a field serializer."""

    hits: int = 0
    misses: int = 0

    @field_serializer("misses")
    def passthrough(self, value: int) -> int:
        return value


class Nullable(BaseModel):
    """`exclude_none` tests the raw attribute, not the serializer's return value."""

    region: str | None = "eu-west-1"

    @field_serializer("region")
    def blank_to_none(self, value: str | None) -> str | None:
        return None if value == "" else value


class Account(BaseModel):
    username: str


class PrivilegedAccount(Account):
    password_hash: str


class Session(BaseModel):
    account: Account


class SignatureTrap(BaseModel):
    """First parameter not named `self`: pydantic treats the method as a free function."""

    code: int

    @field_serializer("code")
    def describe(this, value: object) -> tuple[str, str]:  # pyright: ignore[reportSelfClsParameterName]
        return (type(this).__name__, type(value).__name__)


class AliasCollision(BaseModel):
    """A serialization alias equal to another field's name: dict and JSON outputs disagree."""

    id: int
    name: str = Field(serialization_alias="id")


class Reading(BaseModel):
    sensor: str
    value: float


def build_reading_app() -> FastAPI:
    """Two routes returning the same model: the default response class and an explicit one."""
    app = FastAPI()

    @app.get("/fast")
    async def fast() -> Reading:
        return Reading(sensor="t1", value=float("nan"))

    @app.get("/explicit", response_class=JSONResponse)
    async def explicit() -> Reading:
        return Reading(sensor="t1", value=float("nan"))

    return app
