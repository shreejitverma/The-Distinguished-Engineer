"""Flatten nested legacy JSON with AliasPath and AliasChoices; emit camelCase via AliasGenerator."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from fastapi import FastAPI
from pydantic import AliasChoices, AliasGenerator, AliasPath, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

type WirePath = tuple[str | int, ...]


class OrderView(BaseModel):
    """Flat domain model: legacy nested keys or camelCase keys in, camelCase keys out."""

    model_config = ConfigDict(
        alias_generator=AliasGenerator(serialization_alias=to_camel),
        serialize_by_alias=True,
        extra="forbid",
        frozen=True,
    )

    order_id: int = Field(
        validation_alias=AliasChoices("orderId", AliasPath("order", "header", "id")),
    )
    customer_email: str = Field(
        validation_alias=AliasChoices(
            "customerEmail",
            "cust_email",
            AliasPath("order", "customer", "email"),
        ),
    )
    first_sku: str = Field(
        validation_alias=AliasChoices("firstSku", AliasPath("order", "lines", 0, "sku")),
    )
    last_sku: str = Field(
        validation_alias=AliasChoices("lastSku", AliasPath("order", "lines", -1, "sku")),
    )
    total_cents: int = Field(
        ge=0,
        validation_alias=AliasChoices("totalCents", AliasPath("order", "totals", "grand_cents")),
    )


def parse_legacy_order(raw: bytes) -> OrderView:
    """Validate legacy or camelCase JSON bytes in one pass (jiter -> LookupTree::Nested)."""
    return OrderView.model_validate_json(raw)


def accepted_paths(field_info: FieldInfo) -> list[WirePath]:
    """Return the input paths a field's validation_alias accepts, in priority order."""
    alias = field_info.validation_alias
    if alias is None:
        return []
    choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
    return [
        tuple(choice.convert_to_aliases()) if isinstance(choice, AliasPath) else (choice,)
        for choice in choices
    ]


def output_key(name: str, field_info: FieldInfo) -> str:
    """Return the key a field is serialized under when serializing by alias."""
    return field_info.serialization_alias or name


def one_way_fields(model_cls: type[BaseModel]) -> list[str]:
    """List fields whose by-alias output key is not accepted back on input (no round trip)."""
    by_name = bool(model_cls.model_config.get("validate_by_name", False))
    broken: list[str] = []
    for name, info in model_cls.model_fields.items():
        paths = accepted_paths(info)
        if not paths or by_name:
            paths = [*paths, (name,)]
        if (output_key(name, info),) not in paths:
            broken.append(name)
    return broken


def output_key_collisions(model_cls: type[BaseModel]) -> dict[str, list[str]]:
    """Map each by-alias output key claimed by more than one field to those fields."""
    claims: defaultdict[str, list[str]] = defaultdict(list)
    for name, info in model_cls.model_fields.items():
        claims[output_key(name, info)].append(name)
    return {key: names for key, names in claims.items() if len(names) > 1}


def build_order_import_app() -> FastAPI:
    """ASGI app that ingests legacy order JSON and answers in camelCase."""
    app = FastAPI()

    @app.post("/v2/orders:import")
    async def import_order(order: OrderView) -> OrderView:
        return order

    return app
