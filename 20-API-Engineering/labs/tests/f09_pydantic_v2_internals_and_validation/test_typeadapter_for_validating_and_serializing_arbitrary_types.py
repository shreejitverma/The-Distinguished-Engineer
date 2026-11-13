"""TypeAdapter lab: cached singletons, non-model shapes, unions, serialization, and rebuilds."""

import warnings
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypedDict, cast

import httpx
import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
)
from pydantic_core import PydanticSerializationError

from api_labs.f09_pydantic_v2_internals_and_validation.type_adapter_registry import (
    ID_OR_SLUG,
    ID_OR_SLUG_LTR,
    ORDER_ITEMS,
    ORDER_LINES,
    QUERY_FILTERS,
    STRICT_IDS,
    OrderItem,
    build_orders_app,
    count_validator_builds,
    order_payload,
    parse_items_cached,
    parse_items_rebuilt,
    parse_lines_cached,
    parse_lines_rebuilt,
)

BODY = order_payload(3)


@pytest.mark.unit
def test_cached_adapter_builds_no_validator_per_call_and_rebuilt_builds_one_each() -> None:
    with count_validator_builds() as spy:
        for _ in range(50):
            parse_items_cached(BODY)
            parse_lines_cached(BODY)
    assert spy.call_count == 0

    with count_validator_builds() as spy:
        for _ in range(50):
            parse_items_rebuilt(BODY)
            parse_lines_rebuilt(BODY)
    assert spy.call_count == 100


@pytest.mark.unit
def test_rebuilt_adapters_are_equal_schemas_but_distinct_rust_validators() -> None:
    first, second = TypeAdapter(list[OrderItem]), TypeAdapter(list[OrderItem])
    assert first.core_schema == second.core_schema
    assert first.validator is not second.validator
    assert first.serializer is not second.serializer


@pytest.mark.unit
def test_model_adapter_borrows_model_validator_and_list_wraps_it_as_prebuilt() -> None:
    model_adapter = TypeAdapter(OrderItem)
    assert model_adapter.validator is OrderItem.__pydantic_validator__
    assert model_adapter.serializer is OrderItem.__pydantic_serializer__
    assert ORDER_ITEMS.core_schema["type"] == "list"
    assert "PrebuiltValidator" in repr(ORDER_ITEMS.validator)
    assert "PrebuiltValidator" not in repr(ORDER_LINES.validator)


@pytest.mark.unit
def test_typeddict_validates_to_plain_dict_coerces_and_drops_extra_keys() -> None:
    lines = ORDER_LINES.validate_json(
        b'[{"sku": "A", "quantity": "2", "unit_price": "1.50", "debug": true}]'
    )
    assert type(lines[0]) is dict
    assert lines == [{"sku": "A", "quantity": 2, "unit_price": Decimal("1.50")}]
    assert "note" not in lines[0]

    with pytest.raises(ValidationError) as exc_info:
        ORDER_LINES.validate_python([{"sku": "A", "unit_price": "1"}])
    assert [(e["type"], e["loc"]) for e in exc_info.value.errors()] == [
        ("missing", (0, "quantity"))
    ]


@pytest.mark.unit
def test_smart_union_returns_exact_type_and_left_to_right_coerces_first_member() -> None:
    assert ID_OR_SLUG.validate_python("1") == "1"
    assert ID_OR_SLUG.validate_python(1) == 1
    assert ID_OR_SLUG_LTR.validate_python("1") == 1

    with pytest.raises(ValidationError) as exc_info:
        ID_OR_SLUG.validate_python([1])
    assert [e["loc"] for e in exc_info.value.errors()] == [("int",), ("str",)]


@pytest.mark.unit
def test_config_is_rejected_for_types_with_their_own_config_and_applied_otherwise() -> None:
    with pytest.raises(PydanticUserError) as exc_info:
        TypeAdapter(OrderItem, config=ConfigDict(strict=True))
    assert exc_info.value.code == "type-adapter-config-unused"

    assert TypeAdapter(list[int]).validate_python(["1", 2.0]) == [1, 2]
    with pytest.raises(ValidationError) as strict_info:
        STRICT_IDS.validate_python(["1"])
    assert strict_info.value.errors()[0]["type"] == "int_type"


@pytest.mark.unit
def test_dump_json_returns_bytes_and_mode_json_matches_wire_types() -> None:
    stamps = TypeAdapter(list[datetime])
    value = [datetime(2026, 11, 12, 10, 0, tzinfo=UTC)]
    assert stamps.dump_json(value) == b'["2026-11-12T10:00:00Z"]'
    assert stamps.dump_python(value) == value
    assert stamps.dump_python(value, mode="json") == ["2026-11-12T10:00:00Z"]
    assert TypeAdapter(Decimal).dump_json(Decimal("1.10")) == b'"1.10"'

    lines = ORDER_LINES.validate_json(BODY)
    assert ORDER_LINES.dump_json(lines).startswith(b'[{"sku":"SKU-0","quantity":1,')


@pytest.mark.unit
def test_dump_of_unexpected_value_warns_by_default_and_raises_on_error_mode() -> None:
    ids = TypeAdapter(list[int])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert ids.dump_json(cast("list[int]", ["x", 1])) == b'["x",1]'
    assert len(caught) == 1
    assert "PydanticSerializationUnexpectedValue" in str(caught[0].message)

    with pytest.raises(PydanticSerializationError):
        ids.dump_json(cast("list[int]", ["x"]), warnings="error")


@pytest.mark.unit
def test_dump_of_list_of_base_class_drops_subclass_fields_unless_serialize_as_any() -> None:
    class Base(BaseModel):
        a: int

    class Sub(Base):
        internal_cost: int

    adapter = TypeAdapter(list[Base])
    assert adapter.dump_json([Sub(a=1, internal_cost=7)]) == b'[{"a":1}]'
    assert adapter.dump_json([Sub(a=1, internal_cost=7)], serialize_as_any=True) == (
        b'[{"a":1,"internal_cost":7}]'
    )


@pytest.mark.unit
def test_validate_strings_parses_string_leaves_but_rejects_non_string_containers() -> None:
    assert QUERY_FILTERS.validate_strings({"limit": "10"}) == {"limit": 10}
    with pytest.raises(ValidationError) as exc_info:
        TypeAdapter(list[int]).validate_strings(["1"])
    assert exc_info.value.errors()[0]["type"] == "string_type"


@pytest.mark.unit
def test_forward_reference_adapter_is_incomplete_until_rebuild() -> None:
    adapter = TypeAdapter(list["LateLine"])
    assert adapter.pydantic_complete is False
    with pytest.raises(PydanticUserError) as exc_info:
        adapter.validate_python([])
    assert exc_info.value.code == "class-not-fully-defined"

    class LateLine(TypedDict):
        x: int

    assert adapter.rebuild() is True
    assert adapter.validate_python([{"x": "1"}]) == [{"x": 1}]
    assert adapter.rebuild() is None


@pytest.mark.unit
def test_defer_build_installs_mocks_until_first_use() -> None:
    adapter = TypeAdapter(list[int], config=ConfigDict(defer_build=True))
    assert adapter.pydantic_complete is False
    assert type(adapter.validator).__name__ == "MockValSer"
    assert adapter.validate_python(["1"]) == [1]
    assert adapter.pydantic_complete is True
    assert type(adapter.validator).__name__ == "SchemaValidator"


@pytest.mark.unit
def test_json_schemas_shares_defs_and_splits_a_model_whose_modes_differ() -> None:
    one_item = TypeAdapter(OrderItem)
    by_key, top = TypeAdapter.json_schemas(
        [("in", "validation", ORDER_ITEMS), ("one", "validation", one_item)]
    )
    assert by_key[("in", "validation")]["items"] == {"$ref": "#/$defs/OrderItem"}
    assert by_key[("one", "validation")] == {"$ref": "#/$defs/OrderItem"}
    assert list(top["$defs"]) == ["OrderItem"]

    # Decimal validates from number or string but serializes to string, so the modes diverge.
    by_key, top = TypeAdapter.json_schemas(
        [("in", "validation", ORDER_ITEMS), ("one", "serialization", one_item)]
    )
    assert by_key[("in", "validation")]["items"] == {"$ref": "#/$defs/OrderItem-Input"}
    assert by_key[("one", "serialization")] == {"$ref": "#/$defs/OrderItem-Output"}
    assert sorted(top["$defs"]) == ["OrderItem-Input", "OrderItem-Output"]


@pytest.mark.unit
def test_partial_validation_keeps_complete_items_and_silently_drops_a_bad_last_one() -> None:
    ids = TypeAdapter(list[int])
    assert ids.validate_json("[1, 2, 3", experimental_allow_partial=True) == [1, 2, 3]
    assert ids.validate_json('[1, 2, "x"', experimental_allow_partial=True) == [1, 2]
    with pytest.raises(ValidationError):
        ids.validate_json("[1, 2, 3")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_validates_and_reserializes_with_zero_validator_builds() -> None:
    app = build_orders_app()
    transport = httpx.ASGITransport(app=app)
    with count_validator_builds() as spy:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ok = await client.post(
                "/v1/order-lines",
                content=b'[{"sku": "A", "quantity": "2", "unit_price": "1.5", "x": 0}]',
            )
            bad = await client.post("/v1/order-lines", content=b'[{"sku": "A"}]')
    assert ok.status_code == 200
    assert ok.content == b'[{"sku":"A","quantity":2,"unit_price":"1.5"}]'
    assert bad.status_code == 422
    assert [tuple(e["loc"]) for e in bad.json()["detail"]] == [
        (0, "quantity"),
        (0, "unit_price"),
    ]
    assert spy.call_count == 0
