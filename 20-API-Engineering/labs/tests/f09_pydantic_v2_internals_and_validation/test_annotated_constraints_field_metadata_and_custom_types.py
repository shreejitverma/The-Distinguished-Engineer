"""Lab: composable Annotated constraints lower into pydantic-core leaf schemas."""

from __future__ import annotations

import warnings
from typing import Annotated, Any

import annotated_types as at
import httpx
import pytest
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from pydantic.warnings import UnsupportedFieldAttributeWarning
from pydantic_core import SchemaError

from api_labs.f09_pydantic_v2_internals_and_validation.annotated_domain_types import (
    MAX_CENTS,
    UPPERCASING_CURRENCY_TRAP,
    CurrencyCode,
    Invoice,
    NonEmptyStr,
    PositiveCents,
    PythonReCurrency,
    Quantity,
    build_invoice_app,
    leaf_constraints,
    python_callback_nodes,
)

type DefaultedCount = Annotated[int, Field(default=5, gt=0)]
"""Field-specific `default` inside a PEP 695 alias: warned about and ignored."""


def error_summary(exc: ValidationError) -> list[tuple[str, tuple[int | str, ...]]]:
    return [(err["type"], err["loc"]) for err in exc.errors()]


@pytest.mark.unit
def test_domain_types_lower_into_leaf_core_schemas() -> None:
    assert leaf_constraints(TypeAdapter(NonEmptyStr).core_schema) == {
        "type": "str",
        "min_length": 1,
        "max_length": 200,
        "strip_whitespace": True,
    }
    assert leaf_constraints(TypeAdapter(PositiveCents).core_schema) == {
        "type": "int",
        "strict": True,
        "gt": 0,
        "le": MAX_CENTS,
    }
    assert leaf_constraints(TypeAdapter(CurrencyCode).core_schema) == {
        "type": "str",
        "strict": True,
        "pattern": "^[A-Z]{3}$",
    }
    assert leaf_constraints(TypeAdapter(Quantity).core_schema) == {
        "type": "int",
        "gt": 0,
        "le": 10_000,
    }


@pytest.mark.unit
def test_invoice_schema_has_no_python_callbacks() -> None:
    assert python_callback_nodes(Invoice.__pydantic_core_schema__) == []
    hybrid = TypeAdapter(Annotated[str, AfterValidator(str.strip), Field(max_length=3)])
    assert python_callback_nodes(hybrid.core_schema) == ["function-after", "function-after"]


@pytest.mark.unit
def test_invoice_errors_carry_constraint_types_and_locations() -> None:
    body = (
        '{"currency": "usd", "lines": [{"sku": "AB-1", "description": "   ",'
        ' "unit_price": "5", "quantity": 0}], "tags": ["  "]}'
    )
    with pytest.raises(ValidationError) as caught:
        Invoice.model_validate_json(body)
    assert error_summary(caught.value) == [
        ("string_pattern_mismatch", ("currency",)),
        ("string_too_short", ("lines", 0, "description")),
        ("int_type", ("lines", 0, "unit_price")),
        ("greater_than", ("lines", 0, "quantity")),
        ("string_too_short", ("tags", 0)),
    ]
    with pytest.raises(ValidationError) as empty:
        Invoice.model_validate({"currency": "USD", "lines": []})
    assert error_summary(empty.value) == [("too_short", ("lines",))]


@pytest.mark.unit
def test_rust_string_pipeline_order() -> None:
    assert TypeAdapter(NonEmptyStr).validate_python("  ab  ") == "ab"
    one_char_upper = TypeAdapter(Annotated[str, StringConstraints(max_length=1, to_upper=True)])
    assert one_char_upper.validate_python("\u00df") == "SS"
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(UPPERCASING_CURRENCY_TRAP).validate_python("usd")
    assert error_summary(caught.value) == [("string_pattern_mismatch", ())]
    assert TypeAdapter(UPPERCASING_CURRENCY_TRAP).validate_python("USD") == "USD"


@pytest.mark.unit
def test_length_counts_unicode_code_points() -> None:
    one_char = TypeAdapter(Annotated[str, StringConstraints(max_length=1)])
    assert one_char.validate_python("\u00e9") == "\u00e9"
    assert one_char.validate_python("\U0001f600") == "\U0001f600"
    with pytest.raises(ValidationError) as caught:
        one_char.validate_python("e\u0301")
    assert error_summary(caught.value) == [("string_too_long", ())]


@pytest.mark.unit
def test_dollar_anchor_differs_between_regex_engines() -> None:
    with pytest.raises(ValidationError) as caught:
        TypeAdapter(CurrencyCode).validate_python("USD\n")
    assert error_summary(caught.value) == [("string_pattern_mismatch", ())]
    assert PythonReCurrency(currency="USD\n").currency == "USD\n"
    unanchored = TypeAdapter(Annotated[str, StringConstraints(pattern=r"[A-Z]{3}")])
    assert unanchored.validate_python("xxUSDxx") == "xxUSDxx"


@pytest.mark.unit
def test_later_metadata_overwrites_earlier_bound() -> None:
    widened = TypeAdapter(Annotated[PositiveInt, Field(gt=-5)])
    assert leaf_constraints(widened.core_schema) == {"type": "int", "gt": -5}
    assert widened.validate_python(-1) == -1
    tightened = TypeAdapter(Annotated[int, at.Ge(0), at.Ge(5)])
    assert leaf_constraints(tightened.core_schema) == {"type": "int", "ge": 5}


@pytest.mark.unit
def test_constraint_after_function_validator_falls_back_to_python() -> None:
    rust_first = TypeAdapter(Annotated[str, Field(max_length=3), AfterValidator(str.strip)])
    python_after = TypeAdapter(Annotated[str, AfterValidator(str.strip), Field(max_length=3)])
    with pytest.raises(ValidationError) as rust_err:
        rust_first.validate_python("abcd")
    with pytest.raises(ValidationError) as python_err:
        python_after.validate_python("abcd")
    assert error_summary(rust_err.value) == [("string_too_long", ())]
    assert error_summary(python_err.value) == [("too_long", ())]
    gt_after = TypeAdapter(Annotated[int, AfterValidator(lambda v: v), Field(gt=0)])
    gt_first = TypeAdapter(Annotated[int, Field(gt=0), AfterValidator(lambda v: v)])
    assert gt_after.json_schema() == {"type": "integer", "gt": 0}
    assert gt_first.json_schema() == {"type": "integer", "exclusiveMinimum": 0}


@pytest.mark.unit
def test_bound_on_union_raises_type_error_not_validation_error() -> None:
    adapter = TypeAdapter(Annotated[int | str, at.Gt(0)])
    with pytest.raises(ValidationError):
        adapter.validate_python(0)
    with pytest.raises(TypeError, match="Unable to apply constraint 'gt'"):
        adapter.validate_python("abc")


@pytest.mark.unit
def test_invalid_bound_fails_at_schema_build() -> None:
    with pytest.raises(SchemaError, match="'gt' must be coercible to an integer"):
        TypeAdapter(Annotated[int, at.Gt(0.5)])
    with pytest.raises(SchemaError, match="look-around"):
        TypeAdapter(Annotated[str, StringConstraints(pattern=r"(?<=a)b")])


@pytest.mark.unit
def test_pep695_alias_is_shared_definition_and_drops_field_metadata() -> None:
    schema = Invoice.model_json_schema()
    assert schema["$defs"]["Sku"] == {"pattern": "^[A-Z0-9][A-Z0-9-]{3,31}$", "type": "string"}
    assert schema["$defs"]["LineItem"]["properties"]["sku"] == {"$ref": "#/$defs/Sku"}
    assert schema["properties"]["replaces_sku"]["anyOf"][0] == {"$ref": "#/$defs/Sku"}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        class Counter(BaseModel):
            count: DefaultedCount

    assert [w.category for w in caught] == [UnsupportedFieldAttributeWarning]
    assert Counter.model_fields["count"].is_required()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_maps_constraint_failures_to_422_and_type_error_to_500() -> None:
    app = build_invoice_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    good: dict[str, Any] = {
        "currency": "EUR",
        "lines": [{"sku": "AB-12", "description": " widget ", "unit_price": 250, "quantity": 4}],
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post("/v1/invoices", json=good)
        bad = await client.post("/v1/invoices", json={**good, "currency": "eur"})
        crash = await client.post("/v1/union-bound", json={"value": "abc"})
    assert (ok.status_code, ok.json()) == (200, {"currency": "EUR", "total_cents": 1000})
    assert bad.status_code == 422
    assert [(e["type"], e["loc"]) for e in bad.json()["detail"]] == [
        ("string_pattern_mismatch", ["body", "currency"])
    ]
    assert crash.status_code == 500
