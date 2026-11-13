"""Tests for closed-object composition: additionalProperties vs unevaluatedProperties."""

from __future__ import annotations

from typing import Any

import httpx
import jsonschema_rs
import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
    validators,
)
from openapi_schema_validator import OAS30Validator, OAS31Validator, OAS32Validator

from api_labs.f08_openapi_json_schema_and_contracts.closed_composition import (
    DRAFT_07,
    DRAFT_2020_12,
    SAMPLE,
    Outcome,
    Violation,
    accepted_key_sets,
    address_schema,
    build_address_app,
    closure_chain,
    count_keyword_calls,
    evaluate,
    fixed_schema,
    key_subsets,
    redeclared_schema,
    trap_schema,
    violations,
)

FULL = frozenset({"street", "city", "kind"})
WITH_EXTRA = FULL | {"extra"}
BUSINESS = {"street": "1 Main St", "city": "Leeds", "kind": "business"}
JSONSCHEMA: Any = validators

BRANCHES: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "anyOf": [
        {"properties": {"a": {"type": "integer"}}, "required": ["a"]},
        {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
    ],
    "unevaluatedProperties": False,
}
ONE_OF: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "oneOf": [
        {"properties": {"a": {"type": "integer"}}, "required": ["a"]},
        {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
    ],
    "unevaluatedProperties": False,
}
CONDITIONAL: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "type": "object",
    "properties": {"kind": {"enum": ["residential", "business"]}},
    "required": ["kind"],
    "if": {"properties": {"kind": {"const": "business"}}, "required": ["kind"]},
    "then": {"properties": {"department": {"type": "string"}}},
    "unevaluatedProperties": False,
}
COUSINS: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "allOf": [{"properties": {"street": {"type": "string"}}}, {"unevaluatedProperties": False}],
}
BLIND: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "allOf": [{"properties": {"street": {"type": "string"}}}],
    "additionalProperties": False,
}
CLOSED_BASE: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "$defs": {
        "base": {"properties": {"street": {"type": "string"}}, "unevaluatedProperties": False}
    },
    "allOf": [{"$ref": "#/$defs/base"}],
    "properties": {"kind": {"type": "string"}},
}
REF_SIBLING: dict[str, Any] = {
    "definitions": {"a": {"properties": {"x": {}}}},
    "$ref": "#/definitions/a",
    "additionalProperties": False,
}
DOUBLE_NEGATION: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "not": {"not": {"properties": {"street": True}}},
    "unevaluatedProperties": False,
}
OPEN_BASE: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "allOf": [{"properties": {"street": True}, "additionalProperties": True}],
    "unevaluatedProperties": False,
}
NESTED: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "properties": {"child": {"properties": {"x": True}}},
    "unevaluatedProperties": False,
}
DEPENDENT: dict[str, Any] = {
    "$schema": DRAFT_2020_12,
    "dependentSchemas": {"kind": {"properties": {"city": True}}},
    "patternProperties": {"^str": {"type": "string"}},
    "unevaluatedProperties": False,
}


def rust_accepts(schema: dict[str, Any], instance: object) -> bool:
    return jsonschema_rs.validator_for(schema).is_valid(instance)


@pytest.mark.unit
def test_allof_of_two_closed_schemas_accepts_no_object() -> None:
    assert accepted_key_sets(address_schema(closed=True), SAMPLE) == {frozenset({"street", "city"})}
    assert len(list(key_subsets(SAMPLE))) == 16
    assert accepted_key_sets(trap_schema(), SAMPLE) == set()
    assert violations(trap_schema(), BUSINESS) == [
        Violation(
            "additionalProperties",
            ("allOf", 0, "additionalProperties"),
            "Additional properties are not allowed ('kind' was unexpected)",
        ),
        Violation(
            "additionalProperties",
            ("allOf", 1, "additionalProperties"),
            "Additional properties are not allowed ('city', 'street' were unexpected)",
        ),
    ]
    assert not any(rust_accepts(trap_schema(), obj) for obj in key_subsets(SAMPLE))


@pytest.mark.unit
def test_unevaluated_properties_closes_the_composition_once() -> None:
    assert accepted_key_sets(fixed_schema(), SAMPLE) == {FULL}
    assert violations(fixed_schema(), SAMPLE) == [
        Violation(
            "unevaluatedProperties",
            ("unevaluatedProperties",),
            "Unevaluated properties are not allowed ('extra' was unexpected)",
        )
    ]
    assert [obj for obj in key_subsets(SAMPLE) if rust_accepts(fixed_schema(), obj)] == [BUSINESS]

    grown = fixed_schema()
    grown["$defs"]["address"]["properties"]["unit"] = {"type": "string"}
    assert violations(grown, {**BUSINESS, "unit": "4B"}) == []


@pytest.mark.unit
def test_redeclaring_names_works_on_draft_07_but_drifts_from_the_base() -> None:
    schema = redeclared_schema()
    assert JSONSCHEMA.validator_for(schema) is Draft7Validator
    assert accepted_key_sets(schema, SAMPLE) == {FULL}

    schema["allOf"][0]["properties"]["unit"] = {"type": "string"}
    assert (
        violations(schema["allOf"][0], {"street": "1 Main St", "city": "Leeds", "unit": "4B"}) == []
    )
    assert violations(schema, {**BUSINESS, "unit": "4B"}) == [
        Violation(
            "additionalProperties",
            ("additionalProperties",),
            "Additional properties are not allowed ('unit' was unexpected)",
        )
    ]


@pytest.mark.unit
def test_older_dialects_silently_ignore_unevaluated_properties() -> None:
    downgraded = fixed_schema(DRAFT_07)
    assert JSONSCHEMA.validator_for(downgraded) is Draft7Validator
    Draft7Validator.check_schema(downgraded)
    assert "unevaluatedProperties" not in Draft7Validator.VALIDATORS
    assert accepted_key_sets(downgraded, SAMPLE) == {FULL, WITH_EXTRA}

    component = {key: value for key, value in fixed_schema().items() if key != "$schema"}
    accepted = {
        cls: {frozenset(obj) for obj in key_subsets(SAMPLE) if not violations(component, obj, cls)}
        for cls in (
            OAS30Validator,
            OAS31Validator,
            OAS32Validator,
            Draft201909Validator,
            Draft202012Validator,
        )
    }
    assert violations(REF_SIBLING, {"x": 1, "y": 2}, Draft7Validator) == []
    closed_everything = violations(REF_SIBLING, {"x": 1}, Draft202012Validator)
    assert [error.keyword for error in closed_everything] == ["additionalProperties"]

    assert accepted[OAS30Validator] == {FULL, WITH_EXTRA}
    assert "unevaluatedProperties" not in OAS30Validator.VALIDATORS
    assert accepted[OAS31Validator] == {FULL}
    assert accepted[OAS32Validator] == {FULL}
    assert accepted[Draft201909Validator] == {FULL}
    assert accepted[Draft202012Validator] == {FULL}


@pytest.mark.unit
def test_failed_branches_contribute_no_evaluated_names() -> None:
    assert violations(BRANCHES, {"a": 1, "b": 2}) == []
    assert violations(BRANCHES, {"a": "x", "b": 2}) == [
        Violation(
            "unevaluatedProperties",
            ("unevaluatedProperties",),
            "Unevaluated properties are not allowed ('a' was unexpected)",
        )
    ]
    assert evaluate(BRANCHES, {"a": 1, "b": 2}) == Outcome(True, frozenset({"a", "b"}))
    assert evaluate(BRANCHES, {"a": "x", "b": 2}) == Outcome(False)
    assert evaluate(BRANCHES["anyOf"][1], {"a": "x", "b": 2}) == Outcome(True, frozenset({"b"}))

    assert [error.keyword for error in violations(ONE_OF, {"a": 1, "b": 2})] == ["oneOf"]
    assert violations(ONE_OF, {"a": 1}) == []

    assert violations(CONDITIONAL, {"kind": "business", "department": "HR"}) == []
    assert [
        error.message
        for error in violations(CONDITIONAL, {"kind": "residential", "department": "HR"})
    ] == ["Unevaluated properties are not allowed ('department' was unexpected)"]

    assert (
        violations(
            {"properties": {"street": True}, "unevaluatedProperties": False}, {"street": "x"}
        )
        == []
    )
    assert [error.keyword for error in violations(DOUBLE_NEGATION, {"street": "x"})] == [
        "unevaluatedProperties"
    ]


@pytest.mark.unit
def test_closure_reaches_adjacent_subschemas_only() -> None:
    assert violations(BLIND, {"street": "x"}) == [
        Violation(
            "additionalProperties",
            ("additionalProperties",),
            "Additional properties are not allowed ('street' was unexpected)",
        )
    ]
    assert violations(COUSINS, {"street": "x"}) == [
        Violation(
            "unevaluatedProperties",
            ("allOf", 1, "unevaluatedProperties"),
            "Unevaluated properties are not allowed ('street' was unexpected)",
        )
    ]
    assert rust_accepts(COUSINS, {"street": "x"}) is False
    assert violations(CLOSED_BASE, {"street": "x", "kind": "business"}) == [
        Violation(
            "unevaluatedProperties",
            ("allOf", 0, "unevaluatedProperties"),
            "Unevaluated properties are not allowed ('kind' was unexpected)",
        )
    ]

    assert violations(OPEN_BASE, {"street": "x", "anything": 1}) == []
    assert rust_accepts(OPEN_BASE, {"street": "x", "anything": 1}) is True
    assert evaluate(OPEN_BASE, {"street": "x", "anything": 1}).evaluated == {"street", "anything"}

    assert violations(NESTED, {"child": {"x": 1, "y": 2}}) == []
    assert [error.message for error in violations(NESTED, {"child": {"x": 1}, "y": 2})] == [
        "Unevaluated properties are not allowed ('y' was unexpected)"
    ]


@pytest.mark.unit
def test_reference_evaluator_agrees_with_python_and_rust_validators() -> None:
    corpus: list[dict[str, Any]] = [
        trap_schema(),
        fixed_schema(),
        BRANCHES,
        ONE_OF,
        CONDITIONAL,
        COUSINS,
        CLOSED_BASE,
        BLIND,
        DOUBLE_NEGATION,
        OPEN_BASE,
        DEPENDENT,
        {"$schema": DRAFT_2020_12, **closure_chain(3)},
    ]
    samples: list[dict[str, Any]] = [
        SAMPLE,
        {"a": 1, "b": 2, "street": "x"},
        {"a": "x", "b": 2},
        {"kind": "business", "department": "HR", "city": "Leeds"},
        {"kind": "residential", "department": "HR"},
    ]
    compared = 0
    verdicts: set[bool] = set()
    for schema in corpus:
        for sample in samples:
            for obj in key_subsets(sample):
                expected = not violations(schema, obj)
                assert evaluate(schema, obj).valid is expected, (schema, obj)
                assert rust_accepts(schema, obj) is expected, (schema, obj)
                compared += 1
                verdicts.add(expected)
    assert compared == 12 * (16 + 8 + 4 + 8 + 4)
    assert verdicts == {True, False}

    assert evaluate(fixed_schema(), BUSINESS) == Outcome(True, FULL)
    annotated: dict[str, list[str]] = {}
    for entry in jsonschema_rs.validator_for(fixed_schema()).evaluate(BUSINESS).annotations():
        names = entry["annotations"]
        if entry["instanceLocation"] == "" and isinstance(names, list):
            annotated[entry["schemaLocation"]] = sorted(str(name) for name in names)
    assert annotated == {"/properties": ["kind"], "/$defs/address/properties": ["city", "street"]}
    assert evaluate(address_schema(closed=True), BUSINESS) == Outcome(False)


@pytest.mark.unit
def test_nested_closures_cost_odd_fibonacci_keyword_calls() -> None:
    calls = [count_keyword_calls(closure_chain(depth), {"a": 1}) for depth in range(8)]
    assert calls == [1, 2, 5, 13, 34, 89, 233, 610]
    assert all(calls[d] == 3 * calls[d - 1] - calls[d - 2] for d in range(2, 8))

    flat = {"properties": {"a": {"type": "integer"}}, "additionalProperties": False}
    assert count_keyword_calls(flat, {"a": 1}) == 1

    wide = {
        "allOf": [{"properties": {f"p{index}": {"type": "integer"}}} for index in range(8)],
        "unevaluatedProperties": False,
    }
    instance = {f"p{index}": index for index in range(8)}
    assert violations(wide, instance) == []
    assert count_keyword_calls(wide, instance) == 16


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pydantic_inheritance_publishes_one_flat_closed_object() -> None:
    app = build_address_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        document = (await client.get("/openapi.json")).json()
        assert document["openapi"] == "3.1.0"
        component = document["components"]["schemas"]["BusinessAddress"]
        assert "allOf" not in component
        assert component["additionalProperties"] is False
        assert set(component["properties"]) == FULL
        assert accepted_key_sets(component, SAMPLE) == {FULL}

        created = await client.post("/addresses", json=BUSINESS)
        assert created.status_code == 200
        assert created.json() == BUSINESS

        rejected = await client.post("/addresses", json=SAMPLE)
        assert rejected.status_code == 422
        detail = rejected.json()["detail"]
        assert [(item["type"], item["loc"]) for item in detail] == [
            ("extra_forbidden", ["body", "extra"])
        ]
