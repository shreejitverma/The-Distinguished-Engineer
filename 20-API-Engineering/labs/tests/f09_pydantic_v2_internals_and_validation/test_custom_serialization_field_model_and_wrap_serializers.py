"""Unit tests for Pydantic v2 serialization modes, field and model serializers, and when_used."""

import datetime as dt
import json
import math
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import (
    BaseModel,
    FieldSerializationInfo,
    SerializerFunctionWrapHandler,
    field_serializer,
)
from pydantic_core import PydanticSerializationError

from api_labs.f09_pydantic_v2_internals_and_validation.serialization_internals import (
    Account,
    AliasCollision,
    Counter,
    Envelope,
    FlatEnvelope,
    Invoice,
    LineItem,
    Money,
    Nullable,
    PrivilegedAccount,
    Reading,
    Session,
    SignatureTrap,
    build_reading_app,
)

ISSUED = dt.datetime(2026, 11, 13, 9, 30, tzinfo=dt.UTC)
ISSUED_EPOCH = 1794562200


def make_invoice(**overrides: Any) -> Invoice:
    fields: dict[str, Any] = {
        "invoice_id": 42,
        "issued_at": ISSUED,
        "total": Money(cents=123456, currency="EUR"),
        "lines": [LineItem(sku="A-1", qty=2, unit_price=Decimal("9.90"))],
        "customer_email": "alice@example.com",
    }
    fields.update(overrides)
    return Invoice(**fields)


@pytest.mark.unit
def test_python_mode_keeps_rich_types_and_json_mode_lowers_them() -> None:
    invoice = make_invoice(tags={"b", "a"})
    assert invoice.model_dump() == {
        "invoice_id": 42,
        "issued_at": ISSUED,
        "paid_at": None,
        "total": "1234.56 EUR",
        "tags": ["a", "b"],
        "lines": [{"sku": "A-1", "qty": 2, "unit_price": Decimal("9.90")}],
        "customer_email": "a***@example.com",
        "memo": None,
        "line_count": 1,
    }
    as_json_dict = invoice.model_dump(mode="json")
    assert as_json_dict["issued_at"] == ISSUED_EPOCH
    assert as_json_dict["lines"][0]["unit_price"] == "9.90"
    text = invoice.model_dump_json()
    assert isinstance(text, str)
    assert text == json.dumps(as_json_dict, separators=(",", ":"))
    assert isinstance(Invoice.__pydantic_serializer__.to_json(invoice), bytes)


@pytest.mark.unit
def test_when_used_selects_on_exact_mode_and_on_none() -> None:
    invoice = make_invoice()
    assert invoice.model_dump(mode="audit")["issued_at"] == ISSUED
    assert invoice.model_dump(mode="json")["issued_at"] == ISSUED_EPOCH
    paid = make_invoice(paid_at=dt.datetime(2026, 11, 14, tzinfo=dt.UTC))
    assert paid.model_dump()["paid_at"] == 1794614400.0
    assert invoice.model_dump(mode="json")["paid_at"] is None
    names = repr(Invoice.__pydantic_serializer__)
    assert 'name: "plain_function[issued_epoch]"' in names
    assert "when_used: Json" in names
    assert "when_used: UnlessNone" in names
    assert 'name: "wrap_function[redact_email, str]"' in names


@pytest.mark.unit
def test_wrap_handler_returns_mode_specific_python_objects() -> None:
    class Stamp(BaseModel):
        at: dt.datetime

        @field_serializer("at", mode="wrap")
        def probe(
            self,
            value: dt.datetime,
            handler: SerializerFunctionWrapHandler,
            info: FieldSerializationInfo,
        ) -> list[str]:
            inner = handler(value)
            return [info.mode, type(inner).__name__, info.field_name]

    stamp = Stamp(at=ISSUED)
    assert stamp.model_dump() == {"at": ["python", "datetime", "at"]}
    assert stamp.model_dump_json() == '{"at":["json","str","at"]}'


@pytest.mark.unit
def test_plain_model_serializer_drops_filters_wrap_keeps_them() -> None:
    wrapped = Envelope(seq=1, body="x")
    assert wrapped.model_dump(exclude={"body"}) == {"seq": 1, "trace_id": None, "schema_version": 2}
    assert wrapped.model_dump(exclude_unset=True) == {"seq": 1, "body": "x", "schema_version": 2}
    flat = FlatEnvelope(seq=1, body="x")
    everything = {"seq": 1, "body": "x", "trace_id": None, "schema_version": 2}
    assert flat.model_dump(exclude={"body"}) == everything
    assert flat.model_dump(exclude_unset=True) == everything
    money_dump: object = Money(cents=5, currency="USD").model_dump()
    assert money_dump == "0.05 USD"


@pytest.mark.unit
def test_value_filters_inspect_raw_value_not_serializer_output() -> None:
    assert Counter().model_dump(exclude_defaults=True) == {"misses": 0}
    assert Nullable(region="").model_dump(exclude_none=True) == {"region": None}
    assert Nullable(region=None).model_dump(exclude_none=True) == {}


@pytest.mark.unit
def test_exclude_unset_round_trip_and_computed_fields() -> None:
    invoice = make_invoice()
    assert list(invoice.model_dump(exclude_unset=True)) == [
        "invoice_id",
        "issued_at",
        "total",
        "lines",
        "customer_email",
        "line_count",
    ]
    assert "line_count" not in invoice.model_dump(round_trip=True)
    assert "line_count" not in invoice.model_dump(exclude_computed_fields=True)
    assert invoice.model_dump(exclude_none=True)["line_count"] == 1


@pytest.mark.unit
def test_context_reaches_wrap_field_serializer() -> None:
    invoice = make_invoice()
    assert invoice.model_dump()["customer_email"] == "a***@example.com"
    support = invoice.model_dump(context={"role": "support"})
    assert support["customer_email"] == "alice@example.com"


@pytest.mark.unit
def test_subclass_fields_stay_hidden_unless_polymorphic() -> None:
    privileged = PrivilegedAccount(username="u", password_hash="h")  # noqa: S106
    session = Session(account=privileged)
    assert isinstance(session.account, Account)
    assert session.model_dump() == {"account": {"username": "u"}}
    leaked = {"account": {"username": "u", "password_hash": "h"}}
    assert session.model_dump(serialize_as_any=True) == leaked
    assert session.model_dump(polymorphic_serialization=True) == leaked


@pytest.mark.unit
def test_serializer_errors_and_type_mismatch_warnings() -> None:
    class Exploding(BaseModel):
        x: int

        @field_serializer("x")
        def boom(self, value: int) -> int:
            raise ValueError("boom")

    with pytest.raises(PydanticSerializationError) as exc_info:
        Exploding(x=1).model_dump()
    assert isinstance(exc_info.value, ValueError)
    assert "Error calling function `boom`: ValueError: boom" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)

    reading = Reading(sensor="t1", value=1.5)
    object.__setattr__(reading, "value", "hot")
    expected_warning = "Expected `float` - serialized value may not be as expected"
    with pytest.warns(UserWarning, match=expected_warning):
        assert reading.model_dump() == {"sensor": "t1", "value": "hot"}
    with pytest.raises(PydanticSerializationError, match="Expected `float`"):
        reading.model_dump(warnings="error")


@pytest.mark.unit
def test_first_parameter_must_be_named_self() -> None:
    assert SignatureTrap(code=7).model_dump() == {"code": ("int", "SerializationInfo")}


@pytest.mark.unit
def test_nan_and_duplicate_keys_diverge_between_paths() -> None:
    reading = Reading(sensor="t1", value=float("nan"))
    assert reading.model_dump_json() == '{"sensor":"t1","value":null}'
    as_json_dict = reading.model_dump(mode="json")
    assert math.isnan(as_json_dict["value"])
    with pytest.raises(ValueError, match="not JSON compliant"):
        json.dumps(as_json_dict, allow_nan=False)

    collision = AliasCollision(id=1, name="x")
    assert collision.model_dump(by_alias=True) == {"id": "x"}
    assert collision.model_dump_json(by_alias=True) == '{"id":1,"id":"x"}'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_default_response_uses_rust_json_explicit_class_does_not() -> None:
    transport = httpx.ASGITransport(app=build_reading_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        fast = await client.get("/fast")
        assert fast.status_code == 200
        assert fast.content == b'{"sensor":"t1","value":null}'
        with pytest.raises(ValueError, match="not JSON compliant"):
            await client.get("/explicit")
