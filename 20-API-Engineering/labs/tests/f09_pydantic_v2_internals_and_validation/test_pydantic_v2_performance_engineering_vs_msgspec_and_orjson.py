"""Pydantic v2 performance: Python boundary crossings, fail_fast, and msgspec/orjson parity."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

import msgspec
import orjson
import pytest
from pydantic import BaseModel, FailFast, Field, TypeAdapter, ValidationError, field_validator
from pydantic import model_validator as pyd_model_validator
from pydantic_core import to_json

from api_labs.f09_pydantic_v2_internals_and_validation.validation_cost import (
    PRICE_COUNT,
    DataclassOrder,
    FieldValidatorOrder,
    ItemValidatorOrder,
    RustOnlyOrder,
    StructOrder,
    best_ns_per_call,
    decode_variants,
    fail_fast_probe,
    fail_fast_variants,
    make_payload,
    python_frames_during,
)

PAYLOAD = make_payload()


@pytest.mark.unit
def test_rust_only_validator_starts_no_python_frames() -> None:
    validator = RustOnlyOrder.__pydantic_validator__
    assert python_frames_during(lambda: validator.validate_json(PAYLOAD)) == []


@pytest.mark.unit
def test_model_validate_json_adds_exactly_one_python_frame() -> None:
    frames = python_frames_during(lambda: RustOnlyOrder.model_validate_json(PAYLOAD))
    assert frames == ["BaseModel.model_validate_json"]


@pytest.mark.unit
def test_field_validator_crosses_once_per_field() -> None:
    validator = FieldValidatorOrder.__pydantic_validator__
    frames = python_frames_during(lambda: validator.validate_json(PAYLOAD))
    assert sorted(frames) == [
        "FieldValidatorOrder.check_prices",
        "FieldValidatorOrder.check_prices.<locals>.<genexpr>",
        "FieldValidatorOrder.check_qty",
    ]


@pytest.mark.unit
def test_after_validator_crosses_once_per_item() -> None:
    validator = ItemValidatorOrder.__pydantic_validator__
    frames = python_frames_during(lambda: validator.validate_json(PAYLOAD))
    assert frames == ["_non_negative"] * PRICE_COUNT


@pytest.mark.unit
def test_before_model_validator_receives_materialized_dict() -> None:
    seen: list[str] = []

    class Envelope(BaseModel):
        qty: int

        @pyd_model_validator(mode="before")
        @classmethod
        def record(cls, data: object) -> object:
            seen.append(type(data).__name__)
            return data

    Envelope.model_validate_json(b'{"qty": 1}')
    assert seen == ["dict"]


@pytest.mark.unit
def test_fail_fast_stops_at_first_invalid_item() -> None:
    items: list[object] = [0, 1, "bad", 3, 4, "bad", 6, 7]
    result = fail_fast_probe(items)
    assert result.errors_default == 2
    assert result.errors_fail_fast == 1
    assert result.item_calls_default == 6
    assert result.item_calls_fail_fast == 2


@pytest.mark.unit
def test_fail_fast_reports_only_the_first_location() -> None:
    adapter: TypeAdapter[list[int]] = TypeAdapter(Annotated[list[int], FailFast()])
    with pytest.raises(ValidationError) as caught:
        adapter.validate_json(b'[1, "a", 3, "b"]')
    assert [error["loc"] for error in caught.value.errors()] == [(1,)]
    assert adapter.core_schema == {
        "type": "list",
        "items_schema": {"type": "int"},
        "fail_fast": True,
    }


@pytest.mark.unit
def test_fail_fast_is_rejected_on_scalars_and_sequence_abc() -> None:
    with pytest.raises(RuntimeError, match="'fail_fast' to schema of type 'int'"):

        class Scalar(BaseModel):  # pyright: ignore[reportUnusedClass]
            x: int = Field(fail_fast=True)

    with pytest.raises(RuntimeError, match="'fail_fast' to schema of type 'json-or-python'"):
        TypeAdapter(Annotated[Sequence[int], FailFast()])


@pytest.mark.unit
def test_fail_fast_does_not_stop_sibling_fields() -> None:
    class Batch(BaseModel):
        items: Annotated[list[int], FailFast()]
        a: int
        b: int

    with pytest.raises(ValidationError) as caught:
        Batch.model_validate({"items": ["x", "y"], "a": "q", "b": "r"})
    assert [error["loc"] for error in caught.value.errors()] == [("items", 0), ("a",), ("b",)]


@pytest.mark.unit
def test_type_error_in_validator_escapes_validation_error() -> None:
    class Strictish(BaseModel):
        x: int

        @field_validator("x")
        @classmethod
        def wrong_exception(cls, value: int) -> int:
            raise TypeError("use ValueError instead")

    with pytest.raises(TypeError, match="use ValueError instead"):
        Strictish(x=1)


@pytest.mark.unit
def test_strategies_agree_on_valid_input_but_only_two_reject_bad_input() -> None:
    results = {name: thunk() for name, thunk in decode_variants(PAYLOAD).items()}
    prices = {tuple(getattr(value, "prices")) for value in results.values()}  # noqa: B009
    assert len(prices) == 1
    assert len(next(iter(prices))) == PRICE_COUNT

    bad = b'{"sku": "SKU-1", "qty": -5, "prices": [1.0]}'
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        RustOnlyOrder.model_validate_json(bad)
    with pytest.raises(msgspec.ValidationError, match=r"Expected `int` >= 1 - at `\$\.qty`"):
        msgspec.json.decode(bad, type=StructOrder)
    assert DataclassOrder(**orjson.loads(bad)).qty == -5


@pytest.mark.unit
def test_lax_and_strict_defaults_differ() -> None:
    doc = b'{"sku": "S", "qty": "3", "prices": []}'
    assert RustOnlyOrder.model_validate_json(doc).qty == 3
    with pytest.raises(msgspec.ValidationError, match=r"Expected `int`, got `str` - at `\$\.qty`"):
        msgspec.json.decode(doc, type=StructOrder)
    assert msgspec.json.decode(doc, type=StructOrder, strict=False).qty == 3
    unchecked: object = DataclassOrder(**orjson.loads(doc)).qty
    assert unchecked == "3"


@pytest.mark.unit
def test_wire_output_and_integer_limits_diverge() -> None:
    assert orjson.dumps(1e16) == b"1e+16"
    assert to_json(1e16) == b"1e+16"
    assert msgspec.json.encode(1e16) == b"1e16"

    big = 2**64
    assert to_json(big) == b"18446744073709551616"
    assert msgspec.json.encode(big) == b"18446744073709551616"
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 64-bit range"):
        orjson.dumps(big)
    with pytest.raises(orjson.JSONEncodeError, match="Integer exceeds 53-bit range"):
        orjson.dumps(2**53, option=orjson.OPT_STRICT_INTEGER)


@pytest.mark.unit
def test_non_finite_floats_are_handled_differently() -> None:
    class Reading(BaseModel):
        x: float

    assert str(Reading.model_validate_json(b'{"x": NaN}').x) == "nan"
    assert Reading(x=float("nan")).model_dump_json() == '{"x":null}'
    assert to_json(float("nan")) == b"NaN"
    assert orjson.dumps(float("nan")) == b"null"
    assert msgspec.json.encode(float("nan")) == b"null"
    with pytest.raises(orjson.JSONDecodeError):
        orjson.loads(b"NaN")
    with pytest.raises(msgspec.DecodeError):
        msgspec.json.decode(b"NaN")


@pytest.mark.unit
def test_nesting_depth_limits_differ() -> None:
    def nested(depth: int) -> bytes:
        return b"[" * depth + b"]" * depth

    any_adapter: TypeAdapter[Any] = TypeAdapter(Any)
    any_adapter.validate_json(nested(201))
    with pytest.raises(ValidationError, match="recursion limit exceeded"):
        any_adapter.validate_json(nested(202))
    orjson.loads(nested(1024))
    with pytest.raises(orjson.JSONDecodeError, match="depth limit exceeded"):
        orjson.loads(nested(1025))
    msgspec.json.decode(nested(2000))


@pytest.mark.unit
def test_python_item_validator_is_measurably_slower() -> None:
    variants = decode_variants(PAYLOAD)
    rust = best_ns_per_call(variants["pydantic rust-only validate_json"], number=300, repeat=5)
    per_item = best_ns_per_call(variants["pydantic AfterValidator per item"], number=300, repeat=5)
    assert per_item > 1.5 * rust


@pytest.mark.unit
def test_fail_fast_is_measurably_faster_on_bulk_invalid_input() -> None:
    variants = fail_fast_variants(5_000)
    collect = best_ns_per_call(variants["list[int] collect all"], number=5, repeat=3)
    stop = best_ns_per_call(variants["FailFast list[int]"], number=5, repeat=3)
    assert collect > 5 * stop
