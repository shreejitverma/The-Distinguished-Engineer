"""Side-by-side proofs of pydantic.v1 (1.10.27) vs Pydantic v2 (2.14.3) behavior changes."""

import warnings
from decimal import Decimal
from typing import Any

import httpx
import pydantic
import pytest
from fastapi import FastAPI
from fastapi.exceptions import PydanticV1NotSupportedError
from pydantic import BaseModel, ValidationError
from pydantic import v1 as pydantic_v1
from pydantic.warnings import PydanticDeprecatedSince20

from api_labs.f09_pydantic_v2_internals_and_validation.v1_to_v2_migration import (
    LegacyLineItem,
    LegacyOrder,
    LineItem,
    Order,
    WireCompatOrder,
    build_edge_app,
    legacy_to_modern,
    shadow_validate,
    unapplied_v1_config_keys,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"sku": "A-1", "quantity": 2, "unit_price": "1.10"}
    item.update(overrides.pop("item", {}))
    payload: dict[str, Any] = {"order_id": 42, "items": [item], "hold_for": 3600}
    payload.update(overrides)
    return payload


@pytest.mark.unit
def test_pinned_versions_of_both_engines() -> None:
    assert pydantic.VERSION == "2.14.3"
    assert pydantic_v1.VERSION == "1.10.27"


@pytest.mark.unit
def test_optional_without_default_is_required_only_in_v2() -> None:
    class NaivePort(BaseModel):
        customer_note: str | None

    assert LegacyOrder.parse_obj(_payload()).customer_note is None
    with pytest.raises(ValidationError) as exc:
        NaivePort.model_validate({})
    assert exc.value.errors()[0]["type"] == "missing"
    assert Order.model_validate(_payload()).customer_note is None


@pytest.mark.unit
def test_shadow_decimal_and_timedelta_are_wire_drift() -> None:
    result = shadow_validate(_payload(), LegacyOrder, Order)
    assert result.verdict == "wire-drift"
    assert result.v1_python == result.v2_python
    assert result.v1_wire["items"][0]["unit_price"] == 1.1
    assert result.v2_wire["items"][0]["unit_price"] == "1.10"
    assert result.v1_wire["hold_for"] == 3600.0
    assert result.v2_wire["hold_for"] == "PT1H"


@pytest.mark.unit
def test_shadow_union_order_is_value_drift() -> None:
    result = shadow_validate(_payload(order_id="42"), LegacyOrder, Order)
    assert result.verdict == "value-drift"
    assert result.v1_python is not None
    assert result.v2_python is not None
    assert result.v1_python["order_id"] == 42
    assert result.v2_python["order_id"] == "42"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("item", "verdict", "v1_errors", "v2_errors"),
    [
        ({"quantity": 1.9}, "v2-stricter", (), ("int_from_float",)),
        ({"sku": 123}, "v2-stricter", (), ("string_type",)),
        ({"quantity": "2.0"}, "v2-looser", ("type_error.integer",), ()),
        ({"quantity": "two"}, "both-rejected", ("type_error.integer",), ("int_parsing",)),
    ],
    ids=["float-truncation", "int-to-str", "decimal-string-int", "garbage"],
)
def test_shadow_classifies_coercion_changes(
    item: dict[str, Any],
    verdict: str,
    v1_errors: tuple[str, ...],
    v2_errors: tuple[str, ...],
) -> None:
    result = shadow_validate(_payload(item=item), LegacyOrder, Order)
    assert result.verdict == verdict
    assert result.v1_error_types == v1_errors
    assert result.v2_error_types == v2_errors


@pytest.mark.unit
def test_v1_silently_truncates_float_to_int() -> None:
    legacy = LegacyOrder.parse_obj(_payload(item={"quantity": 1.9}))
    assert legacy.items[0].quantity == 1


@pytest.mark.unit
@pytest.mark.parametrize("order_id", [42, "42"], ids=["int", "numeric-str"])
def test_wire_compat_port_matches_v1_contract(order_id: int | str) -> None:
    result = shadow_validate(_payload(order_id=order_id), LegacyOrder, WireCompatOrder)
    assert result.verdict == "same"


@pytest.mark.unit
def test_adapter_preserves_decimal_where_json_bridge_corrupts_it() -> None:
    exact = Decimal("19.999999999999999999")
    legacy = LegacyOrder.parse_obj(_payload(item={"unit_price": exact}))
    via_python = legacy_to_modern(legacy, Order)
    via_json = Order.model_validate_json(legacy.json())
    assert via_python.items[0].unit_price == exact
    assert via_json.items[0].unit_price == Decimal("20.0")


@pytest.mark.unit
def test_nesting_v1_model_in_v2_model_fails() -> None:
    with pytest.warns(UserWarning, match="Mixing V1 models and V2 models"):

        class Mixed(BaseModel):
            inner: LegacyLineItem

    with pytest.raises(TypeError):
        Mixed.model_validate({"inner": {"sku": "A-1", "quantity": 1, "unit_price": "1"}})


@pytest.mark.unit
def test_nesting_v2_model_in_v1_model_fails() -> None:
    with (
        pytest.warns(UserWarning, match="Mixing V1 and V2 models is not supported"),
        pytest.raises(RuntimeError, match="no validator found"),
    ):

        class Mixed(pydantic_v1.BaseModel):  # pyright: ignore[reportUnusedClass]
            inner: LineItem


@pytest.mark.unit
def test_renamed_config_key_is_kept_but_never_applied() -> None:
    class Row:
        id = 7

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        class Ported(BaseModel):
            class Config:
                orm_mode = True

            id: int

    categories = {w.category for w in caught}
    assert PydanticDeprecatedSince20 in categories
    assert UserWarning in categories
    assert unapplied_v1_config_keys(Ported) == {"orm_mode": "from_attributes"}
    assert unapplied_v1_config_keys(Order) == {}
    with pytest.raises(ValidationError) as exc:
        Ported.model_validate(Row())
    assert exc.value.errors()[0]["type"] == "model_type"


@pytest.mark.unit
def test_deprecated_v1_methods_can_be_made_fatal() -> None:
    order = Order.model_validate(_payload())
    with warnings.catch_warnings():
        warnings.simplefilter("error", PydanticDeprecatedSince20)
        with pytest.raises(PydanticDeprecatedSince20, match="model_dump"):
            order.dict()  # pyright: ignore[reportDeprecated]


@pytest.mark.unit
def test_equality_with_dict_only_in_v1() -> None:
    fields: dict[str, Any] = {"sku": "A-1", "quantity": 1, "unit_price": Decimal(1)}
    plain: object = fields
    assert LegacyLineItem(**fields) == plain
    assert LineItem(**fields) != plain


@pytest.mark.unit
def test_subclass_fields_leak_only_in_v1() -> None:
    class PublicUserV1(pydantic_v1.BaseModel):
        name: str

    class StoredUserV1(PublicUserV1):
        tax_id: str

    class EnvelopeV1(pydantic_v1.BaseModel):
        user: PublicUserV1

    class PublicUser(BaseModel):
        name: str

    class StoredUser(PublicUser):
        tax_id: str

    class Envelope(BaseModel):
        user: PublicUser

    old = EnvelopeV1(user=StoredUserV1(name="ada", tax_id="T-1")).dict()
    new = Envelope(user=StoredUser(name="ada", tax_id="T-1")).model_dump()
    assert old == {"user": {"name": "ada", "tax_id": "T-1"}}
    assert new == {"user": {"name": "ada"}}


@pytest.mark.unit
def test_fastapi_rejects_v1_models_at_route_registration() -> None:
    app = FastAPI()

    async def legacy_endpoint(order: LegacyOrder) -> dict[str, int]:
        return {"items": len(order.items)}

    with pytest.raises(PydanticV1NotSupportedError):
        app.post("/legacy")(legacy_endpoint)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_first_strangler_serves_v1_wire_from_v1_core() -> None:
    transport = httpx.ASGITransport(app=build_edge_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/orders/reprice", json=_payload(order_id="42"))
    assert response.status_code == 200
    assert response.json() == {
        "order_id": 42,
        "customer_note": None,
        "items": [{"sku": "A-1", "quantity": 2, "unit_price": 0.99}],
        "hold_for": 3600.0,
    }
