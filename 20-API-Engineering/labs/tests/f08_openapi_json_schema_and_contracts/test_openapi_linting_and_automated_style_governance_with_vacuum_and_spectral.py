"""Lab: custom OpenAPI style rules with Spectral semantics, checked against captured CLI output."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from api_labs.f08_openapi_json_schema_and_contracts.openapi_style_linter import (
    SPECTRAL_OPERATION_ALIAS,
    Diagnostic,
    Linter,
    RefError,
    Severity,
    exit_code,
    is_identifier,
    lint,
    resolve,
    spectral_owasp_v2_flags,
    vacuum_owasp_flags,
)

PROBLEM = {"$ref": "#/components/responses/Problem"}
COMPONENTS: dict[str, Any] = {
    "parameters": {
        "OrderId": {
            "name": "orderId",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        },
        "Limit": {"name": "limit", "in": "query", "schema": {"type": "integer", "maximum": 100}},
        "Cursor": {"name": "cursor", "in": "query", "schema": {"type": "string"}},
    },
    "responses": {
        "Problem": {
            "description": "Problem details.",
            "content": {"application/problem+json": {"schema": {"type": "object"}}},
        }
    },
}
PAGE = [{"$ref": "#/components/parameters/Limit"}, {"$ref": "#/components/parameters/Cursor"}]

# The same document that was linted with spectral 6.17.0 and vacuum 0.39.0 (good.json).
GOOD: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Orders", "version": "1.0.0"},
    "paths": {
        "/orders": {
            "get": {
                "operationId": "listOrders",
                "parameters": PAGE,
                "responses": {"200": {"description": "A page."}, "400": PROBLEM},
            },
            "post": {
                "operationId": "createOrder",
                "responses": {"201": {"description": "Created."}, "422": PROBLEM},
            },
        },
        "/orders/{orderId}": {
            "parameters": [{"$ref": "#/components/parameters/OrderId"}],
            "get": {
                "operationId": "getOrder",
                "responses": {"200": {"description": "One order."}, "404": PROBLEM},
            },
        },
        "/orders/{orderId}/items": {
            "parameters": [{"$ref": "#/components/parameters/OrderId"}, *PAGE],
            "get": {
                "operationId": "listOrderItems",
                "responses": {"200": {"description": "A page."}, "default": PROBLEM},
            },
        },
    },
    "components": COMPONENTS,
}


def make_bad() -> dict[str, Any]:
    """bad.json: four rule violations planted in GOOD."""
    doc = copy.deepcopy(GOOD)
    listing = doc["paths"]["/orders"]["get"]
    del listing["operationId"], listing["parameters"]
    listing["responses"]["400"] = {
        "description": "Bad request.",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    one = doc["paths"]["/orders/{orderId}"]
    one["parameters"] = [{"name": "orderId", "in": "path", "required": True, "schema": {}}]
    one["parameters"][0]["schema"]["type"] = "integer"
    one["get"]["responses"]["404"] = {"description": "No such order."}
    doc["components"]["parameters"]["OrderId"]["schema"] = {"type": ["integer", "null"]}
    return doc


def codes_and_paths(results: list[Diagnostic]) -> list[tuple[str, str]]:
    return [(d.code, "/".join(map(str, d.path))) for d in results]


@pytest.mark.unit
def test_conforming_spec_is_clean_and_exits_zero() -> None:
    results = lint(GOOD)
    assert results == []
    assert exit_code(results) == 0


@pytest.mark.unit
def test_bad_spec_reports_seven_findings_at_use_sites_like_vacuum() -> None:
    results = lint(make_bad())
    assert codes_and_paths(results) == [
        ("acme-collection-pagination", "paths//orders/get"),
        ("acme-collection-pagination", "paths//orders/get"),
        ("acme-operation-id", "paths//orders/get/operationId"),
        ("acme-error-problem-json", "paths//orders/get/responses/400"),
        ("acme-error-problem-json", "paths//orders/{orderId}/get/responses/404"),
        ("acme-no-numeric-ids", "paths//orders/{orderId}/parameters/0/schema/type"),
        ("acme-no-numeric-ids", "paths//orders/{orderId}/items/parameters/0/schema/type"),
    ]
    assert sum(d.severity is Severity.ERROR for d in results) == 5
    assert sum(d.severity is Severity.WARN for d in results) == 2
    assert exit_code(results) == 1


@pytest.mark.unit
def test_definition_reporting_moves_ref_findings_to_the_component_like_spectral() -> None:
    results = lint(make_bad(), report="definition")
    numeric = [p for c, p in codes_and_paths(results) if c == "acme-no-numeric-ids"]
    assert numeric == [
        "components/parameters/OrderId/schema/type",
        "paths//orders/{orderId}/parameters/0/schema/type",
    ]


@pytest.mark.unit
def test_definition_reporting_dedupes_a_component_used_twice() -> None:
    doc = make_bad()
    doc["paths"]["/orders/{orderId}"]["parameters"] = [{"$ref": "#/components/parameters/OrderId"}]
    use_site = [d for d in lint(doc) if d.code == "acme-no-numeric-ids"]
    definition = [d for d in lint(doc, report="definition") if d.code == "acme-no-numeric-ids"]
    assert len(use_site) == 2
    assert len(definition) == 1


@pytest.mark.unit
def test_fail_severity_threshold_decides_the_exit_code() -> None:
    doc = copy.deepcopy(GOOD)
    del doc["paths"]["/orders"]["get"]["parameters"]  # only the two pagination warnings remain
    results = lint(doc)
    assert {d.severity for d in results} == {Severity.WARN}
    assert exit_code(results, Severity.ERROR) == 0
    assert exit_code(results, Severity.WARN) == 1
    assert exit_code(results, Severity.HINT) == 1


@pytest.mark.unit
def test_operation_id_is_truthy_and_unique_case_sensitively() -> None:
    doc = copy.deepcopy(GOOD)
    doc["paths"]["/orders"]["post"]["operationId"] = ""
    doc["paths"]["/orders/{orderId}/items"]["get"]["operationId"] = "getOrder"
    doc["paths"]["/orders/{orderId}"]["get"]["operationId"] = "GetOrder"
    messages = [d.message for d in lint(doc) if d.code == "acme-operation-id"]
    assert messages == ["operationId is required"]
    doc["paths"]["/orders/{orderId}"]["get"]["operationId"] = "getOrder"
    messages = [d.message for d in lint(doc) if d.code == "acme-operation-id"]
    assert messages == [
        "operationId is required",
        "operationId 'getOrder' already used at paths//orders/{orderId}/get",
    ]


@pytest.mark.unit
def test_openapi_32_query_and_additional_operations_need_operation_ids() -> None:
    doc = copy.deepcopy(GOOD)
    doc["openapi"] = "3.2.0"
    search = {"responses": {"200": {"description": "ok"}}}
    doc["paths"]["/orders/search"] = {"query": search, "additionalOperations": {"LINK": search}}
    flagged = [p for c, p in codes_and_paths(lint(doc)) if c == "acme-operation-id"]
    assert flagged == [
        "paths//orders/search/additionalOperations/LINK/operationId",
        "paths//orders/search/query/operationId",
    ]
    spectral_alias = [
        d for d in lint_with(doc, SPECTRAL_OPERATION_ALIAS) if d.code == "acme-operation-id"
    ]
    assert spectral_alias == []


def lint_with(doc: dict[str, Any], methods: tuple[str, ...]) -> list[Diagnostic]:
    return Linter(doc).run(methods)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "flagged"),
    [
        ("400", True),
        ("4XX", True),
        ("503", True),
        ("5XX", True),
        ("default", True),
        ("200", False),
        ("302", False),
        ("2XX", False),
    ],
)
def test_problem_json_rule_covers_codes_ranges_and_default(status: str, flagged: bool) -> None:
    doc = copy.deepcopy(GOOD)
    doc["paths"]["/orders"]["post"]["responses"] = {status: {"description": "plain"}}
    found = [d for d in lint(doc) if d.code == "acme-error-problem-json"]
    assert bool(found) is flagged


@pytest.mark.unit
def test_operation_parameter_overrides_path_item_parameter_by_name_and_location() -> None:
    doc = make_bad()
    override = {"name": "orderId", "in": "path", "required": True, "schema": {"type": "string"}}
    doc["paths"]["/orders/{orderId}"]["get"]["parameters"] = [override]
    numeric = [p for c, p in codes_and_paths(lint(doc)) if c == "acme-no-numeric-ids"]
    assert numeric == ["paths//orders/{orderId}/items/parameters/0/schema/type"]


@pytest.mark.unit
def test_pagination_parameters_may_come_from_the_path_item() -> None:
    doc = copy.deepcopy(GOOD)
    items = doc["paths"]["/orders/{orderId}/items"]
    assert "parameters" not in items["get"]
    assert lint(doc) == []
    items["parameters"] = items["parameters"][:2]  # drop Cursor
    assert [d.message for d in lint(doc)] == [
        "GET /orders/{orderId}/items is a collection and must declare query parameter 'cursor'"
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("id", True),
        ("ID", True),
        ("orderId", True),
        ("orderID", True),
        ("order_id", True),
        ("order-id", True),
        ("bid", False),
        ("paid", False),
        ("grid", False),
        ("idempotent", False),
    ],
)
def test_identifier_names_use_token_boundaries(name: str, expected: bool) -> None:
    assert is_identifier(name) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "schema", "spectral", "vacuum", "ours"),
    [
        ("bid", {"type": "integer"}, False, True, False),
        ("orderId", {"type": ["integer", "null"]}, False, True, True),
        ("id", {}, True, False, False),
        ("ID", {"type": "integer"}, False, True, True),
    ],
)
def test_published_owasp_numeric_id_rules_disagree_on_edge_cases(
    name: str, schema: dict[str, Any], spectral: bool, vacuum: bool, ours: bool
) -> None:
    # Rows mirror owaspprobe.yaml as captured from spectral 6.17.0 (rule copied from
    # spectral-owasp-ruleset 2.0.1) and vacuum 0.39.0 (`owasp-no-numeric-ids`).
    assert spectral_owasp_v2_flags(name, schema) is spectral
    assert vacuum_owasp_flags(name, schema) is vacuum
    doc = copy.deepcopy(GOOD)
    param = {"name": name, "in": "query", "schema": schema}
    doc["paths"]["/orders/{orderId}"]["get"]["parameters"] = [param]
    assert any(d.code == "acme-no-numeric-ids" for d in lint(doc)) is ours


@pytest.mark.unit
def test_resolver_refuses_remote_dangling_and_circular_refs() -> None:
    loop = {"a": {"$ref": "#/components/b"}, "b": {"$ref": "#/components/a"}}
    doc: dict[str, Any] = {"components": loop}
    with pytest.raises(RefError, match="non-local"):
        resolve(doc, {"$ref": "https://example.com/spec.yaml#/x"})
    with pytest.raises(RefError, match="dangling"):
        resolve(doc, {"$ref": "#/components/missing"})
    with pytest.raises(RefError, match="circular"):
        resolve(doc, {"$ref": "#/components/a"})
    escaped = {"paths": {"/a/{b}": {"ok": True}}}
    assert resolve(escaped, {"$ref": "#/paths/~1a~1{b}"}) == ({"ok": True}, ("paths", "/a/{b}"))
