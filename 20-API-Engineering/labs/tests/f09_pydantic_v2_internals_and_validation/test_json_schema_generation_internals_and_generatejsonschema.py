"""Unit tests for mode-aware JSON Schema generation and GenerateJsonSchema subclassing."""

from __future__ import annotations

import warnings

import httpx
import pytest
from pydantic import BaseModel, Field, PydanticUserError
from pydantic.json_schema import GenerateJsonSchema, PydanticJsonSchemaWarning

from api_labs.f09_pydantic_v2_internals_and_validation.json_schema_generation import (
    COMPONENTS_REF_TEMPLATE,
    ContractGenerateJsonSchema,
    OpenAPI30GenerateJsonSchema,
    Order,
    OutputDefaultsRequired,
    PinnedOrder,
    SchemaContractError,
    build_order_app,
    component_schemas,
    contract_errors,
    dangling_refs,
    schema_pair,
)

INBOUND = {"orderId": 7, "price": "101.25", "qty": 3}


class Unencodable(BaseModel):
    marker: object = Field(default=object())


@pytest.mark.unit
def test_modes_pick_different_aliases_and_computed_fields() -> None:
    pair = schema_pair(Order)
    assert pair.property_names("validation") == ("orderId", "price", "qty", "placed_ms", "fill")
    assert pair.property_names("serialization") == (
        "order_ref",
        "price",
        "qty",
        "placed_ms",
        "fill",
        "notional",
    )
    assert pair.required("validation") == ("orderId", "price")
    assert pair.required("serialization") == ("order_ref", "price", "notional")
    assert pair.serialization["properties"]["notional"]["readOnly"] is True


@pytest.mark.unit
def test_types_and_defaults_follow_the_serializer_only_in_serialization_mode() -> None:
    pair = schema_pair(Order)
    assert pair.validation["properties"]["price"]["anyOf"] == [
        {"type": "number"},
        {"type": "string"},
    ]
    assert pair.serialization["properties"]["price"]["type"] == "string"
    placed_in = pair.validation["properties"]["placed_ms"]
    placed_out = pair.serialization["properties"]["placed_ms"]
    assert (placed_in["type"], placed_in["default"]) == ("integer", 0)
    assert (placed_out["type"], placed_out["default"]) == ("string", "0")


@pytest.mark.unit
def test_each_schema_accepts_only_its_own_direction() -> None:
    pair = schema_pair(Order)
    dumped = Order.model_validate(INBOUND).model_dump(mode="json", by_alias=True)
    assert contract_errors(pair.validation, INBOUND) == []
    assert contract_errors(pair.serialization, dumped) == []
    assert contract_errors(pair.serialization, INBOUND) == [
        "'notional' is a required property",
        "'order_ref' is a required property",
    ]
    assert contract_errors(pair.validation, dumped) == [
        "'0' is not of type 'integer'",
        "'orderId' is a required property",
    ]


@pytest.mark.unit
def test_dump_without_by_alias_breaks_the_serialization_schema() -> None:
    pair = schema_pair(Order)
    dumped = Order.model_validate(INBOUND).model_dump(mode="json")
    assert "order_id" in dumped
    assert contract_errors(pair.serialization, dumped) == ["'order_ref' is a required property"]


@pytest.mark.unit
def test_defaults_are_required_in_output_only_when_configured() -> None:
    assert schema_pair(Order).required("serialization") == ("order_ref", "price", "notional")
    configured = schema_pair(OutputDefaultsRequired)
    assert configured.required("validation") == ()
    assert configured.required("serialization") == ("qty",)


@pytest.mark.unit
def test_mode_override_pins_the_validation_shape() -> None:
    pinned = schema_pair(PinnedOrder)
    assert pinned.serialization == pinned.validation
    assert "notional" not in pinned.property_names("serialization")


@pytest.mark.unit
def test_models_json_schema_suffixes_only_divergent_definitions() -> None:
    refs, defs = component_schemas([Order])
    assert refs == {
        "Order:validation": "#/components/schemas/Order-Input",
        "Order:serialization": "#/components/schemas/Order-Output",
    }
    assert sorted(defs) == ["Fill", "Order-Input", "Order-Output"]
    assert defs["Order-Input"]["title"] == defs["Order-Output"]["title"] == "Order"


@pytest.mark.unit
def test_generator_instance_is_single_use() -> None:
    generator = GenerateJsonSchema()
    generator.generate(Order.__pydantic_core_schema__)
    with pytest.raises(PydanticUserError) as excinfo:
        generator.generate(Order.__pydantic_core_schema__)
    assert excinfo.value.code == "json-schema-already-used"


@pytest.mark.unit
def test_contract_generator_declares_dialect_and_pins_decimal_syntax() -> None:
    pair = schema_pair(Order, ContractGenerateJsonSchema)
    assert pair.validation["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "$schema" not in schema_pair(Order).validation
    out_pattern = pair.serialization["properties"]["price"]["pattern"]
    in_pattern = pair.validation["properties"]["price"]["anyOf"][1]["pattern"]
    assert out_pattern != in_pattern
    assert contract_errors(pair.validation, {"orderId": 1, "price": "1.2345"}) == []
    assert contract_errors(pair.validation, {"orderId": 1, "price": "1.23456"}) == [
        "'1.23456' is not valid under any of the given schemas"
    ]


@pytest.mark.unit
def test_contract_generator_fails_closed_where_default_generator_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        schema = Unencodable.model_json_schema()
    assert "default" not in schema["properties"]["marker"]
    assert [type(w.message) for w in caught] == [PydanticJsonSchemaWarning]
    with pytest.raises(SchemaContractError, match=r"\[non-serializable-default\]$"):
        Unencodable.model_json_schema(schema_generator=ContractGenerateJsonSchema)


@pytest.mark.unit
def test_openapi30_generator_rewrites_null_branches() -> None:
    default_fill = schema_pair(Order).validation["properties"]["fill"]
    assert default_fill["anyOf"] == [{"$ref": "#/$defs/Fill"}, {"type": "null"}]
    fill = schema_pair(Order, OpenAPI30GenerateJsonSchema).validation["properties"]["fill"]
    assert fill == {"allOf": [{"$ref": "#/$defs/Fill"}], "default": None, "nullable": True}


@pytest.mark.unit
def test_ref_template_alone_leaves_refs_dangling() -> None:
    assert dangling_refs(Order.model_json_schema()) == set()
    relocated = Order.model_json_schema(ref_template=COMPONENTS_REF_TEMPLATE)
    assert sorted(relocated["$defs"]) == ["Fill"]
    assert dangling_refs(relocated) == {"#/components/schemas/Fill"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_openapi_points_request_and_response_at_split_components() -> None:
    app = build_order_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    operation = document["paths"]["/v1/orders"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert request_ref == "#/components/schemas/Order-Input"
    assert response_ref == "#/components/schemas/Order-Output"
    components = document["components"]["schemas"]
    assert {"Fill", "Order-Input", "Order-Output"} <= set(components)
    assert dangling_refs(document) == set()
