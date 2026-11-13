"""Unit tests: what datamodel-code-generator 0.89.0 keeps, changes, and drops from OpenAPI 3.1.2."""

from __future__ import annotations

import ast
import copy
import itertools
import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

import pytest
from datamodel_code_generator import Error, Formatter, InputFileType, generate

from api_labs.f08_openapi_json_schema_and_contracts.schema_first_codegen import (
    EDGE_OPENAPI,
    LAX,
    ORDERS_OPENAPI,
    STRICT,
    Profile,
    generate_models,
    json_schema_errors,
    load_models,
    model_class,
    mypy_strict_errors,
    rejections,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

_COUNTER = itertools.count()
_PWNED: Final = "DMCG_LAB_PWNED"

GOOD_ORDER: Final[dict[str, Any]] = {
    "id": "0b0e0f7a-52a4-4c55-9a4b-0d5a3a0a9f11",
    "created_at": "2026-11-11T05:00:00Z",
    "items": [
        {"sku": "ABC-1234", "quantity": 2, "unit_price": {"amount_minor": 1999, "currency": "USD"}}
    ],
    "payment": {"kind": "card", "last4": "4242"},
}


def _model(profile: Profile, name: str, spec: dict[str, Any] = ORDERS_OPENAPI) -> type[BaseModel]:
    module = load_models(generate_models(spec, profile), f"generated_{next(_COUNTER)}")
    return model_class(module, name)


def _with_item(**changes: object) -> dict[str, Any]:
    order = copy.deepcopy(GOOD_ORDER)
    order["items"][0].update(changes)
    return order


@pytest.mark.unit
def test_default_profile_emits_call_annotations_that_fail_mypy_strict() -> None:
    source = generate_models(ORDERS_OPENAPI, LAX)
    assert "conint(ge=1, le=1000)" in source
    assert "constr(pattern=r'^[A-Z]{3}-[0-9]{4}$')" in source
    errors = mypy_strict_errors(source)
    assert errors
    assert set(errors) <= {"valid-type", "misc"}


@pytest.mark.unit
def test_annotated_profile_passes_mypy_strict_with_pydantic_plugin() -> None:
    source = generate_models(ORDERS_OPENAPI, STRICT)
    assert "Annotated[StrictInt, Field(ge=1, le=1000)]" in source
    assert "conint(" not in source
    assert mypy_strict_errors(source) == []


@pytest.mark.unit
def test_experimental_schema_validator_helper_is_not_mypy_strict_clean() -> None:
    source = generate_models(ORDERS_OPENAPI, replace(STRICT, schema_validators=True))
    assert "class _JsonSchemaRuntimeValidationBase(BaseModel):" in source
    assert mypy_strict_errors(source) == ["arg-type"]


@pytest.mark.unit
def test_strict_types_reject_the_coercions_lax_mode_accepts() -> None:
    lax, strict = _model(LAX, "Order"), _model(STRICT, "Order")
    as_string = _with_item(quantity="2")
    as_bool = _with_item(unit_price={"amount_minor": True, "currency": "USD"})
    assert rejections(lax, as_string) == []
    assert rejections(lax, as_bool) == []
    assert rejections(strict, as_string) == [("items.0.quantity", "int_type")]
    assert rejections(strict, as_bool) == [("items.0.unit_price.amount_minor", "int_type")]
    assert json_schema_errors(ORDERS_OPENAPI, "Order", as_string) == ["type"]


@pytest.mark.unit
def test_json_integer_2_0_is_valid_json_schema_but_strict_pydantic_rejects_it() -> None:
    whole_float = _with_item(quantity=2.0)
    assert json_schema_errors(ORDERS_OPENAPI, "Order", whole_float) == []
    assert rejections(_model(LAX, "Order"), whole_float) == []
    assert rejections(_model(STRICT, "Order"), whole_float) == [("items.0.quantity", "int_type")]


@pytest.mark.unit
def test_strict_nullable_rejects_null_for_a_defaulted_non_nullable_field() -> None:
    null_priority = {**GOOD_ORDER, "priority": None}
    assert "priority: conint(ge=0, le=9) | None = 5" in generate_models(ORDERS_OPENAPI, LAX)
    assert rejections(_model(LAX, "Order"), null_priority) == []
    assert rejections(_model(STRICT, "Order"), null_priority) == [("priority", "int_type")]
    assert json_schema_errors(ORDERS_OPENAPI, "Order", null_priority) == ["type"]
    assert rejections(_model(STRICT, "Order"), {**GOOD_ORDER, "coupon": None}) == []


@pytest.mark.unit
def test_additional_properties_false_becomes_extra_forbid() -> None:
    assert "extra='forbid'" in generate_models(ORDERS_OPENAPI, LAX)
    unknown = {**GOOD_ORDER, "discount": 10}
    assert rejections(_model(LAX, "Order"), unknown) == [("discount", "extra_forbidden")]
    assert json_schema_errors(ORDERS_OPENAPI, "Order", unknown) == ["additionalProperties"]


@pytest.mark.unit
def test_discriminator_mapping_becomes_a_tagged_union() -> None:
    source = generate_models(ORDERS_OPENAPI, STRICT)
    assert "root: Annotated[CardPayment | WirePayment, Field(discriminator='kind')]" in source
    cash = {**GOOD_ORDER, "payment": {"kind": "cash"}}
    assert rejections(_model(STRICT, "Order"), cash) == [("payment", "union_tag_invalid")]


@pytest.mark.unit
def test_date_time_maps_to_aware_datetime_and_rejects_a_missing_offset() -> None:
    naive = {**GOOD_ORDER, "created_at": "2026-11-11T05:00:00"}
    assert "created_at: AwareDatetime" in generate_models(ORDERS_OPENAPI, LAX)
    assert rejections(_model(LAX, "Order"), naive) == [("created_at", "timezone_aware")]
    assert json_schema_errors(ORDERS_OPENAPI, "Order", naive) == ["format"]
    assert json_schema_errors(ORDERS_OPENAPI, "Order", naive, check_formats=False) == []


@pytest.mark.unit
def test_unique_items_is_dropped_by_default_and_identity_hashed_as_a_set() -> None:
    dupes = {**GOOD_ORDER, "tags": ["vip", "vip"]}
    assert json_schema_errors(ORDERS_OPENAPI, "Order", dupes) == ["uniqueItems"]
    assert rejections(_model(LAX, "Order"), dupes) == []
    as_set = replace(STRICT, unique_items_as_set=True)
    source = generate_models(ORDERS_OPENAPI, as_set)
    assert "__hash__ = object.__hash__" in source
    order = _model(as_set, "Order").model_validate_json(json.dumps(dupes))
    assert len(order.__dict__["tags"]) == 2
    assert order.model_dump()["tags"] == {"vip"}
    assert json.loads(order.model_dump_json())["tags"] == ["vip", "vip"]


@pytest.mark.unit
def test_schema_validators_enforce_unique_items_at_runtime() -> None:
    dupes = {**GOOD_ORDER, "tags": ["vip", "vip"]}
    checked = _model(replace(STRICT, schema_validators=True), "Order")
    assert rejections(checked, dupes) == [("", "value_error")]
    assert rejections(checked, {**GOOD_ORDER, "tags": ["vip", "new"]}) == []


@pytest.mark.unit
def test_collapse_root_models_with_set_drops_the_item_min_length() -> None:
    empty_tag = {**GOOD_ORDER, "tags": [""]}
    as_set = replace(STRICT, unique_items_as_set=True)
    collapsed = replace(as_set, collapse_root_models=True)
    assert rejections(_model(as_set, "Order"), empty_tag) == [("tags.0", "string_too_short")]
    collapsed_source = generate_models(ORDERS_OPENAPI, collapsed)
    assert "tags: Annotated[set[StrictStr] | None, Field(max_length=8)]" in collapsed_source
    assert rejections(_model(collapsed, "Order"), empty_tag) == []
    assert json_schema_errors(ORDERS_OPENAPI, "Order", empty_tag) == ["minLength"]


@pytest.mark.unit
def test_read_only_fields_split_into_request_and_response_models() -> None:
    split = replace(STRICT, request_response=True)
    source = generate_models(ORDERS_OPENAPI, split)
    assert "class OrderRequest(BaseModel):" in source
    assert "class Order(BaseModel):" not in source
    request = {k: v for k, v in GOOD_ORDER.items() if k not in {"id", "created_at"}}
    assert rejections(_model(split, "OrderRequest"), request) == []
    assert rejections(_model(split, "OrderRequest"), GOOD_ORDER) == [
        ("id", "extra_forbidden"),
        ("created_at", "extra_forbidden"),
    ]
    assert rejections(_model(split, "OrderResponse"), request) == [
        ("id", "missing"),
        ("created_at", "missing"),
    ]


@pytest.mark.unit
def test_patterns_stay_unanchored_and_lookahead_selects_python_re() -> None:
    source = generate_models(EDGE_OPENAPI, STRICT)
    assert 'regex_engine="python-re"' in source
    account = _model(STRICT, "Account", EDGE_OPENAPI)
    base = {"user-id": "u1", "class": "gold", "code": "x1234y", "price": 0.5}
    assert rejections(account, base) == []
    assert json_schema_errors(EDGE_OPENAPI, "Account", base) == []
    assert rejections(account, {**base, "password": "abcdefgh"}) == [
        ("password", "string_pattern_mismatch")
    ]


@pytest.mark.unit
def test_float_multiple_of_uses_an_absolute_tolerance_not_exact_division() -> None:
    account = _model(STRICT, "Account", EDGE_OPENAPI)
    base = {"user-id": "u1", "class": "gold", "code": "1234"}
    assert rejections(account, {**base, "price": 19.99}) == []
    assert json_schema_errors(EDGE_OPENAPI, "Account", {**base, "price": 19.99}) == ["multipleOf"]
    assert rejections(account, {**base, "price": 3.0000000001}) == []
    assert rejections(account, {**base, "price": 0.015}) == [("price", "multiple_of")]


@pytest.mark.unit
def test_int64_format_is_not_range_checked() -> None:
    account = _model(STRICT, "Account", EDGE_OPENAPI)
    row = {"user-id": "u1", "class": "gold", "code": "1234", "price": 1.0, "ledger_id": 2**63}
    assert "ledger_id: StrictInt | None = None" in generate_models(EDGE_OPENAPI, STRICT)
    assert rejections(account, row) == []


@pytest.mark.unit
def test_aliases_need_by_alias_for_a_wire_round_trip() -> None:
    account = _model(STRICT, "Account", EDGE_OPENAPI)
    wire = {"user-id": "u1", "class": "gold", "code": "1234", "price": 1.0}
    parsed = account.model_validate_json(json.dumps(wire))
    assert json.loads(parsed.model_dump_json(exclude_none=True, by_alias=True)) == wire
    assert "user_id" in json.loads(parsed.model_dump_json(exclude_none=True))
    by_name = {"user_id": "u1", "class_": "gold", "code": "1234", "price": 1.0}
    assert rejections(account, by_name) == [("user-id", "missing"), ("class", "missing")]


@pytest.mark.unit
def test_output_is_byte_identical_only_without_the_timestamp() -> None:
    assert generate_models(ORDERS_OPENAPI, STRICT) == generate_models(ORDERS_OPENAPI, STRICT)
    stamped = generate(
        json.dumps(ORDERS_OPENAPI),
        input_file_type=InputFileType.OpenAPI,
        formatters=[Formatter.BUILTIN],
        allow_remote_refs=False,
    )
    assert isinstance(stamped, str)
    assert "#   timestamp: " in stamped
    assert "timestamp" not in generate_models(ORDERS_OPENAPI, STRICT)


@pytest.mark.unit
def test_hostile_schema_strings_stay_inert() -> None:
    payload = f"'); __import__('os').environ.__setitem__('{_PWNED}', '1') #"
    hostile = {
        "openapi": "3.1.2",
        "info": {"title": "Hostile", "version": "1.0.0"},
        "paths": {},
        "components": {
            "schemas": {
                "Evil": {
                    "type": "object",
                    "description": f'ends """\nimport os\nos.environ["{_PWNED}"] = "1"\n"""',
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": f"x\rimport os; os.environ['{_PWNED}'] = '1'",
                            "default": payload,
                        },
                        "level": {"type": "string", "enum": [payload, "b"]},
                    },
                }
            }
        },
    }
    source = generate_models(hostile, replace(STRICT, descriptions=True))
    top_level = {type(node).__name__ for node in ast.parse(source).body}
    assert top_level <= {"ImportFrom", "ClassDef"}
    evil = model_class(load_models(source, f"generated_{next(_COUNTER)}"), "Evil")
    assert _PWNED not in os.environ
    assert evil().model_dump()["note"] == payload


@pytest.mark.unit
@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("default_factory", "__import__('os').getcwd", "default_factory must be one of"),
        ("x-python-type", "list[int]; __import__('os').getcwd()", "valid Python type annotation"),
        ("customTypePath", "os.path\nimport sys", "dotted Python identifier path"),
    ],
)
def test_executable_extension_keywords_are_refused(keyword: str, value: str, message: str) -> None:
    spec = copy.deepcopy(ORDERS_OPENAPI)
    spec["components"]["schemas"]["Money"]["properties"]["currency"] = {
        "type": "string",
        keyword: value,
    }
    with pytest.raises(Error, match=message):
        generate_models(spec, STRICT)


@pytest.mark.unit
def test_remote_ref_is_refused_before_any_network_access() -> None:
    spec = copy.deepcopy(ORDERS_OPENAPI)
    remote = "https://schemas.example.invalid/common.json#/Currency"
    spec["components"]["schemas"]["Money"]["properties"]["currency"] = {"$ref": remote}
    with pytest.raises(Error, match=r"Fetching remote \$ref is disabled"):
        generate_models(spec, STRICT)
