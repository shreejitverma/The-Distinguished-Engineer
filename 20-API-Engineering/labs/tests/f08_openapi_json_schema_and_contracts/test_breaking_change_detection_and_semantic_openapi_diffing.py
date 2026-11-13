"""Lab: a semantic OpenAPI differ judges request changes contravariantly and responses covariantly.

Each breaking verdict is also proven by a witness payload checked with an independent JSON Schema
validator (jsonschema, Draft 2020-12): a request the old contract accepted and the new one
rejects, or a response the new contract allows and the old client's schema rejects.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from api_labs.f08_openapi_json_schema_and_contracts.semantic_openapi_diff import (
    Direction,
    Effect,
    ExternalRefError,
    Level,
    RefCycleError,
    breaking,
    derive_level,
    diff_specs,
    gate,
)

if TYPE_CHECKING:
    from collections.abc import Callable

TODAY = date(2026, 11, 11)


def _validator(spec: dict[str, Any], component: str) -> Callable[[object], bool]:
    """An independent Draft 2020-12 validator for one component, with its siblings as $defs."""
    schema = copy.deepcopy(spec["components"]["schemas"][component])
    schema["$defs"] = spec["components"]["schemas"]
    schema["properties"]["channel"] = {"$ref": "#/$defs/Channel"}
    checker: Any = Draft202012Validator(schema)
    return lambda instance: bool(checker.is_valid(instance))


def _json(schema: dict[str, Any]) -> dict[str, Any]:
    return {"content": {"application/json": {"schema": schema}}}


BASE: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Orders", "version": "1.4.0"},
    "paths": {
        "/orders": {
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": _json({"type": "array", "items": {"$ref": "#/components/schemas/Order"}})
                },
            },
            "post": {
                "requestBody": {
                    "required": True,
                    **_json({"$ref": "#/components/schemas/OrderCreate"}),
                },
                "responses": {"201": _json({"$ref": "#/components/schemas/Order"})},
            },
        },
        "/orders/{orderId}": {
            "parameters": [
                {"name": "orderId", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {
                "responses": {
                    "200": _json({"$ref": "#/components/schemas/Order"}),
                    "404": {"description": "x"},
                }
            },
            "patch": {"responses": {"204": {"description": "updated"}}},
            "delete": {"deprecated": True, "responses": {"204": {"description": "deleted"}}},
        },
    },
    "components": {
        "schemas": {
            "Channel": {"type": "string", "enum": ["web", "store"]},
            "OrderCreate": {
                "type": "object",
                "required": ["sku", "quantity"],
                "properties": {
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "channel": {"$ref": "#/components/schemas/Channel"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                    "note": {"type": "string", "maxLength": 500},
                    "customerRef": {"type": "string"},
                },
            },
            "Order": {
                "type": "object",
                "required": ["id", "status", "currency", "itemCount", "shippedAt"],
                "properties": {
                    "id": {"type": "string", "readOnly": True},
                    "status": {"type": "string", "enum": ["pending", "shipped"]},
                    "channel": {"$ref": "#/components/schemas/Channel"},
                    "currency": {"type": "string"},
                    "itemCount": {"type": "integer"},
                    "shippedAt": {"type": "string"},
                    "tracking": {"type": "string"},
                },
            },
        }
    },
}


def _revision() -> dict[str, Any]:
    rev = copy.deepcopy(BASE)
    rev["info"]["version"] = "1.5.0"
    orders = rev["paths"]["/orders"]
    orders["get"]["parameters"].append(
        {"name": "region", "in": "query", "required": True, "schema": {"type": "string"}}
    )
    orders["post"]["responses"] = {"202": _json({"$ref": "#/components/schemas/Order"})}
    item = rev["paths"].pop("/orders/{orderId}")
    del item["patch"], item["delete"]
    item["parameters"][0]["name"] = "id"
    rev["paths"]["/orders/{id}"] = item
    schemas = rev["components"]["schemas"]
    schemas["Channel"]["enum"].append("marketplace")
    create = schemas["OrderCreate"]
    create["required"] += ["couponCode", "customerRef"]
    create["properties"]["couponCode"] = {"type": "string"}
    create["properties"]["giftWrap"] = {"type": "boolean"}
    create["properties"]["quantity"] = {"type": "number"}
    create["properties"]["priority"]["enum"] = ["normal", "high"]
    create["properties"]["note"]["maxLength"] = 200
    order = schemas["Order"]
    order["required"].remove("currency")
    del order["properties"]["currency"], order["properties"]["tracking"]
    order["properties"]["status"]["enum"].append("cancelled")
    order["properties"]["itemCount"] = {"type": "number"}
    order["properties"]["shippedAt"] = {"type": ["string", "null"]}
    return rev


REVISION = _revision()


def _verdicts(base: dict[str, Any], rev: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    return {
        (c.level.name, c.id, c.method, c.args[0] if c.args else "")
        for c in diff_specs(base, rev, today=TODAY)
    }


@pytest.mark.unit
def test_variance_law_is_asymmetric_across_the_wire() -> None:
    assert derive_level(Effect.NARROWS, Direction.REQUEST) is Level.ERR
    assert derive_level(Effect.WIDENS, Direction.REQUEST) is Level.INFO
    assert derive_level(Effect.WIDENS, Direction.RESPONSE) is Level.ERR
    assert derive_level(Effect.NARROWS, Direction.RESPONSE) is Level.INFO
    assert derive_level(Effect.INCOMPARABLE, Direction.RESPONSE) is Level.ERR
    assert derive_level(Effect.UNKNOWN, Direction.REQUEST) is Level.WARN


@pytest.mark.unit
def test_full_revision_report_matches_expected_verdicts() -> None:
    changes = diff_specs(BASE, REVISION, today=TODAY)
    print("\n" + "\n".join(c.text() for c in changes))
    assert _verdicts(BASE, REVISION) == {
        ("ERR", "new-required-request-parameter", "get", "query:region"),
        ("ERR", "response-required-property-removed", "get", "[]/currency"),
        ("INFO", "response-optional-property-removed", "get", "[]/tracking"),
        ("ERR", "response-property-enum-value-added", "get", "[]/status"),
        ("ERR", "response-property-enum-value-added", "get", "[]/channel"),
        ("ERR", "response-property-type-generalized", "get", "[]/itemCount"),
        ("ERR", "response-property-became-nullable", "get", "[]/shippedAt"),
        ("ERR", "new-required-request-property", "post", "couponCode"),
        ("INFO", "new-optional-request-property", "post", "giftWrap"),
        ("ERR", "request-property-became-required", "post", "customerRef"),
        ("ERR", "request-property-enum-value-removed", "post", "priority"),
        ("INFO", "request-property-enum-value-added", "post", "channel"),
        ("INFO", "request-property-type-generalized", "post", "quantity"),
        ("ERR", "request-property-max-length-decreased", "post", "note"),
        ("ERR", "response-success-status-removed", "post", "201"),
        ("INFO", "response-success-status-added", "post", "202"),
        ("ERR", "response-required-property-removed", "get", "currency"),
        ("INFO", "response-optional-property-removed", "get", "tracking"),
        ("ERR", "response-property-enum-value-added", "get", "status"),
        ("ERR", "response-property-enum-value-added", "get", "channel"),
        ("ERR", "response-property-type-generalized", "get", "itemCount"),
        ("ERR", "response-property-became-nullable", "get", "shippedAt"),
        ("ERR", "api-removed-without-deprecation", "patch", ""),
        ("INFO", "api-removed-with-deprecation", "delete", ""),
    }
    assert gate(changes) == 1


@pytest.mark.unit
def test_shared_enum_widening_is_safe_in_request_and_breaking_in_response() -> None:
    changes = diff_specs(BASE, REVISION, today=TODAY)
    channel = [c for c in changes if c.args and c.args[0].endswith("channel")]
    levels = {(c.direction, c.level) for c in channel}
    assert levels == {(Direction.REQUEST, Level.INFO), (Direction.RESPONSE, Level.ERR)}


@pytest.mark.unit
def test_breaking_request_verdicts_have_a_witness_the_new_contract_rejects() -> None:
    old_server = _validator(BASE, "OrderCreate")
    new_server = _validator(REVISION, "OrderCreate")
    # Each witness is valid under the new contract except for the one rule it names.
    full = {"sku": "a", "quantity": 1, "couponCode": "x", "customerRef": "c"}
    witnesses = {
        "new-required-request-property": {"sku": "a", "quantity": 1, "customerRef": "c"},
        "request-property-became-required": {"sku": "a", "quantity": 1, "couponCode": "x"},
        "request-property-enum-value-removed": {**full, "priority": "low"},
        "request-property-max-length-decreased": {**full, "note": "n" * 300},
    }
    for rule, payload in witnesses.items():
        assert old_server(payload), rule
        assert not new_server(payload), rule
    widened = {**full, "quantity": 1.5, "channel": "marketplace"}
    assert new_server(widened)  # the INFO verdicts only admit more requests
    assert not old_server(widened)


@pytest.mark.unit
def test_breaking_response_verdicts_have_a_witness_the_old_client_rejects() -> None:
    old_client, new_server = _validator(BASE, "Order"), _validator(REVISION, "Order")
    good = {"id": "o1", "status": "pending", "itemCount": 1, "shippedAt": "2026-11-11"}
    witnesses = {
        "response-required-property-removed": good,
        "response-property-enum-value-added": {**good, "status": "cancelled", "currency": "EUR"},
        "response-property-type-generalized": {**good, "itemCount": 1.5, "currency": "EUR"},
        "response-property-became-nullable": {**good, "shippedAt": None, "currency": "EUR"},
    }
    for rule, payload in witnesses.items():
        assert new_server(payload), rule
        assert not old_client(payload), rule


@pytest.mark.unit
def test_reordering_and_nullable_spelling_are_not_changes() -> None:
    base = {
        "paths": {
            "/a": {
                "get": {
                    "responses": {
                        "200": _json(
                            {
                                "type": "object",
                                "properties": {
                                    "kind": {"type": "string", "enum": ["x", "y"]},
                                    "note": {"type": "string", "nullable": True},
                                    "n": {"type": ["integer", "number"]},
                                },
                            }
                        )
                    }
                }
            }
        }
    }
    rev = {
        "paths": {
            "/a": {
                "get": {
                    "responses": {
                        "200": _json(
                            {
                                "type": "object",
                                "properties": {
                                    "n": {"type": "number"},
                                    "note": {"type": ["null", "string"]},
                                    "kind": {"type": "string", "enum": ["y", "x"]},
                                },
                            }
                        )
                    }
                }
            }
        }
    }
    assert diff_specs(base, rev, today=TODAY) == []


@pytest.mark.unit
def test_read_only_and_write_only_properties_are_invisible_on_the_other_side() -> None:
    def spec(enum: list[str], required: list[str]) -> dict[str, Any]:
        body = {
            "type": "object",
            "required": required,
            "properties": {
                "state": {"type": "string", "readOnly": True, "enum": enum},
                "secret": {"type": "string", "writeOnly": True, "enum": enum},
            },
        }
        return {
            "paths": {
                "/a": {"put": {"requestBody": _json(body), "responses": {"200": _json(body)}}}
            }
        }

    verdicts = _verdicts(spec(["a", "b"], []), spec(["a", "c"], ["state", "secret"]))
    request = {v for v in verdicts if v[1].startswith(("request", "new-"))}
    response = {v for v in verdicts if v[1].startswith("response")}
    assert all(v[3] == "secret" for v in request)
    assert all(v[3] == "state" for v in response)
    assert ("ERR", "request-property-enum-value-removed", "put", "secret") in request
    assert ("ERR", "response-property-enum-value-added", "put", "state") in response


@pytest.mark.unit
def test_path_parameter_rename_matches_the_same_endpoint() -> None:
    rule_ids = {c.id for c in diff_specs(BASE, REVISION, today=TODAY) if c.path == "/orders/{id}"}
    assert rule_ids == {
        "response-required-property-removed",
        "response-optional-property-removed",
        "response-property-enum-value-added",
        "response-property-type-generalized",
        "response-property-became-nullable",
    }


@pytest.mark.unit
def test_removal_before_sunset_is_a_violation_and_after_sunset_is_sanctioned() -> None:
    def spec(op: dict[str, Any] | None) -> dict[str, Any]:
        return {"paths": {"/a": {"get": op} if op else {}}}

    op = {"deprecated": True, "x-sunset": "2026-12-31", "responses": {"200": {"description": "x"}}}
    early = diff_specs(spec(op), spec(None), today=TODAY)
    late = diff_specs(spec(op), spec(None), today=date(2027, 1, 1))
    assert [(c.id, c.level) for c in early] == [("api-removed-before-sunset", Level.ERR)]
    assert [(c.id, c.level) for c in late] == [("api-removed-with-deprecation", Level.INFO)]


@pytest.mark.unit
def test_recursive_schema_terminates_and_reports_nested_change_once() -> None:
    def spec(enum: list[str]) -> dict[str, Any]:
        node = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": enum},
                "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
            },
        }
        return {
            "paths": {
                "/t": {"get": {"responses": {"200": _json({"$ref": "#/components/schemas/Node"})}}}
            },
            "components": {"schemas": {"Node": node}},
        }

    changes = diff_specs(spec(["a"]), spec(["a", "b"]), today=TODAY)
    assert [(c.id, c.args[0]) for c in changes] == [("response-property-enum-value-added", "kind")]


@pytest.mark.unit
def test_external_and_cyclic_refs_are_refused() -> None:
    def spec(ref: str) -> dict[str, Any]:
        return {
            "paths": {"/a": {"get": {"responses": {"200": _json({"$ref": ref})}}}},
            "components": {
                "schemas": {"Ok": {"type": "string"}, "Loop": {"$ref": "#/components/schemas/Loop"}}
            },
        }

    with pytest.raises(ExternalRefError, match=r"169\.254\.169\.254"):
        diff_specs(
            spec("#/components/schemas/Ok"),
            spec("http://169.254.169.254/latest/meta-data/"),
            today=TODAY,
        )
    with pytest.raises(RefCycleError):
        diff_specs(spec("#/components/schemas/Ok"), spec("#/components/schemas/Loop"), today=TODAY)


@pytest.mark.unit
def test_gate_honours_fail_level_and_fingerprint_waivers() -> None:
    changes = diff_specs(BASE, REVISION, today=TODAY)
    errors = [c for c in changes if c.level is Level.ERR]
    assert gate(changes, ignore=[c.fingerprint for c in errors[:-1]]) == 1
    assert gate(changes, ignore=[c.fingerprint for c in errors]) == 0
    assert len({c.fingerprint for c in changes}) == len(changes)
    assert all(len(c.fingerprint) == 12 for c in changes)
    assert len(breaking(changes)) == len(errors)  # no WARN-level rule fires on this revision
