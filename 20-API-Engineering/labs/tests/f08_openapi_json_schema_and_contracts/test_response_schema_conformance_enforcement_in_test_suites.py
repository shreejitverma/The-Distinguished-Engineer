"""Zero-tolerance contract enforcement: every response a test receives is checked by OpenAPI."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
import schemathesis
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from referencing.exceptions import Unresolvable

from api_labs.f08_openapi_json_schema_and_contracts.response_conformance import (
    ConformanceRecorder,
    ContractViolationError,
    Defect,
    OpenAPIContract,
    build_inventory_app,
    checkable_formats,
    conformance_hook,
)

NEW_ITEM = {"name": "nut", "price": 0.1, "supplier_token": "tok-EXAMPLE-NOT-REAL"}
JSON = httpx.Headers({"content-type": "application/json"})


@dataclass
class Harness:
    client: httpx.AsyncClient
    recorder: ConformanceRecorder
    contract: OpenAPIContract


def make_harness(
    defects: frozenset[Defect] = frozenset(), *, raise_app_exceptions: bool = True
) -> Harness:
    """Code-first: the contract is the app's own generated OpenAPI document."""
    app = build_inventory_app(defects)
    contract = OpenAPIContract(app.openapi())
    recorder = ConformanceRecorder()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        event_hooks={"response": [conformance_hook(contract, recorder)]},
    )
    return Harness(client, recorder, contract)


@pytest_asyncio.fixture
async def conformant() -> AsyncIterator[Harness]:
    harness = make_harness()
    async with harness.client:
        yield harness


async def violations_of(defect: Defect, method: str, url: str, **kwargs: Any) -> list[str]:
    harness = make_harness(frozenset({defect}))
    async with harness.client:
        with pytest.raises(ContractViolationError) as caught:
            await harness.client.request(method, url, **kwargs)
    return [f"{v.rule}: {v.detail}" for v in caught.value.violations]


# --- the hook on a code-first FastAPI app --------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conformant_app_exercises_every_documented_response(conformant: Harness) -> None:
    client = conformant.client
    assert (await client.get("/items/1")).status_code == 200
    assert (await client.get("/items/99")).status_code == 404
    assert (await client.get("/items/abc")).status_code == 422
    created = await client.post("/items", json=NEW_ITEM)
    assert created.status_code == 201
    assert created.headers["location"] == "/items/2"
    assert (await client.post("/items", json={"name": "", "price": -1})).status_code == 422
    assert (await client.delete("/items/2")).status_code == 204
    assert (await client.delete("/items/2")).status_code == 404
    assert (await client.delete("/items/abc")).status_code == 422
    assert conformant.recorder.uncovered(conformant.contract) == set()
    assert len(conformant.recorder.observed) == 8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undocumented_status_code_fails_the_request() -> None:
    found = await violations_of("undocumented-404", "GET", "/items/99")
    assert found == ["status: status 404 is not documented; documented: ['200', '422']"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returned_response_object_bypasses_fastapi_validation_but_not_the_contract() -> None:
    found = await violations_of("leaked-field", "GET", "/items/1")
    assert found == [
        "body: <root>: Additional properties are not allowed ('supplier_cost' was unexpected)"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nan_passes_fastapi_validation_then_serializes_to_null_and_fails_schema() -> None:
    harness = make_harness(frozenset({"nan-price"}))
    async with harness.client:
        waived = await harness.client.get(
            "/items/1", extensions={"contract_waiver": "inspect the raw body"}
        )
        with pytest.raises(ContractViolationError) as caught:
            await harness.client.get("/items/1")
    assert waived.content == b'{"id":1,"name":"bolt","price":null}'
    assert harness.recorder.waived == [("GET", "/items/1", "inspect the raw body")]
    assert [v.detail for v in caught.value.violations] == ["price: None is not of type 'number'"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undocumented_media_type_fails_even_when_the_bytes_are_valid_json() -> None:
    found = await violations_of("text-content-type", "GET", "/items/1")
    assert found == [
        "content-type: 'text/plain; charset=utf-8' is not documented; "
        "documented: ['application/json']"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_required_response_header_fails() -> None:
    found = await violations_of("missing-location", "POST", "/items", json=NEW_ITEM)
    assert found == ["header: required header Location is missing"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_on_204_is_a_framing_violation_the_asgi_transport_does_not_catch() -> None:
    found = await violations_of("body-on-204", "DELETE", "/items/1")
    assert found == ["framing: status 204 must not carry content"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_missing_from_the_document_is_an_operation_violation(
    conformant: Harness,
) -> None:
    with pytest.raises(ContractViolationError) as caught:
        await conformant.client.get("/admin/metrics")
    assert [v.rule for v in caught.value.violations] == ["operation"]
    assert conformant.recorder.observed == set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_waiver_without_a_reason_does_not_disable_the_check(conformant: Harness) -> None:
    with pytest.raises(ContractViolationError):
        await conformant.client.get("/admin/metrics", extensions={"contract_waiver": ""})
    assert conformant.recorder.waived == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_exception_reaches_the_test_before_any_response_hook_runs() -> None:
    raising = make_harness(frozenset({"missing-field"}))
    async with raising.client:
        with pytest.raises(ResponseValidationError):
            await raising.client.get("/items/1")
    assert raising.recorder.observed == set()

    observed = make_harness(frozenset({"missing-field"}), raise_app_exceptions=False)
    async with observed.client:
        with pytest.raises(ContractViolationError) as caught:
            await observed.client.get("/items/1")
    assert [v.detail for v in caught.value.violations] == [
        "status 500 is not documented; documented: ['200', '404', '422']"
    ]


# --- what FastAPI documents for you ------------------------------------------------------------


@pytest.mark.unit
def test_fastapi_adds_422_only_to_routes_with_parameters_and_not_beside_4xx() -> None:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/echo/{word}")
    async def echo(word: str) -> dict[str, str]:
        return {"word": word}

    @app.get("/ranged/{word}", responses={"4XX": {"description": "client error"}})
    async def ranged(word: str) -> dict[str, str]:
        return {"word": word}

    paths = app.openapi()["paths"]
    assert sorted(paths["/health"]["get"]["responses"]) == ["200"]
    assert sorted(paths["/echo/{word}"]["get"]["responses"]) == ["200", "422"]
    assert sorted(paths["/ranged/{word}"]["get"]["responses"]) == ["200", "4XX"]
    assert app.openapi()["openapi"] == "3.1.0"


# --- design-first contract rules -----------------------------------------------------------------

USER = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "created"],
    "properties": {
        "id": {"type": "integer"},
        "created": {"type": "string", "format": "date-time"},
        "password": {"type": "string", "writeOnly": True},
    },
}
PROBLEM = {"type": "object", "properties": {"status": {"type": "integer"}}}
DESIGN: dict[str, Any] = {
    "openapi": "3.1.2",
    "info": {"title": "users", "version": "1"},
    "paths": {
        "/users/{user_id}": {
            "get": {
                "responses": {
                    "200": {
                        "description": "one user",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                        },
                    },
                    "2XX": {"description": "export", "content": {"text/*": {}}},
                    "4XX": {
                        "description": "problem",
                        "content": {
                            "application/problem+json": {
                                "schema": {"$ref": "#/components/schemas/Problem"}
                            }
                        },
                    },
                }
            }
        },
        "/users/me": {"get": {"responses": {"default": {"description": "anything"}}}},
        "/remote": {
            "get": {
                "responses": {
                    "200": {
                        "description": "remote schema",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "https://schemas.invalid/thing.json"}
                            }
                        },
                    }
                }
            }
        },
    },
    "components": {"schemas": {"User": USER, "Problem": PROBLEM}},
}
GOOD_USER = b'{"id":7,"created":"2026-11-13T10:00:00Z"}'


def rules(contract: OpenAPIContract, status: int, headers: httpx.Headers, body: bytes) -> list[str]:
    result = contract.check("GET", "/users/7", status, headers, body)
    return [f"{result.status_key} {v.rule}: {v.detail}" for v in result.violations] or [
        f"{result.status_key} ok"
    ]


@pytest.mark.unit
def test_concrete_path_matches_before_a_template_declared_earlier() -> None:
    contract = OpenAPIContract(DESIGN)
    me = contract.find_operation("GET", "/users/me")
    other = contract.find_operation("GET", "/users/7")
    assert me is not None
    assert me.path_template == "/users/me"
    assert other is not None
    assert other.path_template == "/users/{user_id}"
    assert contract.find_operation("GET", "/users/7/extra") is None


@pytest.mark.unit
def test_explicit_code_beats_its_range_and_range_media_keys_match_by_type() -> None:
    contract = OpenAPIContract(DESIGN)
    text = httpx.Headers({"content-type": "text/plain"})
    csv = httpx.Headers({"content-type": "text/csv; charset=utf-8"})
    assert rules(contract, 200, text, b"x") == [
        "200 content-type: 'text/plain' is not documented; documented: ['application/json']"
    ]
    assert rules(contract, 206, csv, b"a,b") == ["2XX ok"]
    assert rules(contract, 206, JSON, b"{}") == [
        "2XX content-type: 'application/json' is not documented; documented: ['text/*']"
    ]


@pytest.mark.unit
def test_default_response_accepts_any_status_so_it_disables_the_status_gate() -> None:
    contract = OpenAPIContract(DESIGN)
    html = httpx.Headers({"content-type": "text/html"})
    quiet = contract.check("GET", "/users/me", 500, html, b"")
    loud = contract.check("GET", "/users/me", 500, html, b"<h1>Internal Server Error</h1>")
    assert (quiet.status_key, quiet.violations) == ("default", ())
    assert [v.rule for v in loud.violations] == ["content-type"]


@pytest.mark.unit
def test_structured_suffix_json_body_is_parsed_and_validated() -> None:
    contract = OpenAPIContract(DESIGN)
    problem = httpx.Headers({"content-type": "application/problem+json"})
    assert rules(contract, 409, problem, b'{"status":409}') == ["4XX ok"]
    assert rules(contract, 409, problem, b'{"status":"409"}') == [
        "4XX body: status: '409' is not of type 'integer'"
    ]


@pytest.mark.unit
def test_write_only_property_present_in_a_response_is_a_violation() -> None:
    body = b'{"id":7,"created":"2026-11-13T10:00:00Z","password":"hunter2"}'
    assert rules(OpenAPIContract(DESIGN), 200, JSON, body) == [
        "200 body: password: writeOnly value is present in a response"
    ]


@pytest.mark.unit
def test_format_is_an_annotation_unless_assertion_is_enabled() -> None:
    body = b'{"id":7,"created":"yesterday"}'
    assert rules(OpenAPIContract(DESIGN, assert_formats=False), 200, JSON, body) == ["200 ok"]
    assert rules(OpenAPIContract(DESIGN), 200, JSON, body) == [
        "200 body: created: 'yesterday' is not a 'date-time'"
    ]


@pytest.mark.unit
def test_misspelled_or_uncheckable_format_fails_closed_at_load_time() -> None:
    typo = {**DESIGN, "components": {"schemas": {"User": {**USER, "format": "date_time"}}}}
    with pytest.raises(ValueError, match=r"no checker for \['date_time'\]"):
        OpenAPIContract(typo)
    assert OpenAPIContract(typo, assert_formats=False).document is typo
    assert {"date-time", "uuid"} <= checkable_formats()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b'{"id":7,"id":8,"created":"2026-11-13T10:00:00Z"}', "duplicate object member 'id'"),
        (b'{"id":NaN,"created":"2026-11-13T10:00:00Z"}', "NaN is not a JSON number"),
        (b"\xef\xbb\xbf" + GOOD_USER, "Unexpected UTF-8 BOM"),
        (b'{"id":7,"created":"\xe9"}', "'utf-8' codec can't decode byte 0xe9"),
    ],
    ids=["duplicate-key", "nan-token", "byte-order-mark", "latin-1"],
)
def test_strict_json_rejects_what_python_json_accepts(body: bytes, reason: str) -> None:
    (found,) = rules(OpenAPIContract(DESIGN), 200, JSON, body)
    assert found.startswith("200 json: body is not strict UTF-8 JSON: ")
    assert reason in found


@pytest.mark.unit
def test_charset_parameter_does_not_change_media_type_selection() -> None:
    headers = httpx.Headers({"content-type": "Application/JSON; charset=utf-8"})
    assert rules(OpenAPIContract(DESIGN), 200, headers, GOOD_USER) == ["200 ok"]


@pytest.mark.unit
def test_remote_ref_is_unresolvable_instead_of_fetched() -> None:
    contract = OpenAPIContract(DESIGN)
    with pytest.raises(Unresolvable, match=r"https://schemas\.invalid/thing\.json"):
        contract.check("GET", "/remote", 200, JSON, b"{}")


# --- comparison with Schemathesis -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("defect", "rule"),
    [("undocumented-404", "status"), ("text-content-type", "content-type")],
)
async def test_schemathesis_validate_response_checks_only_the_body(
    defect: Defect, rule: str
) -> None:
    app = build_inventory_app(frozenset({defect}))
    url = "/items/99" if defect == "undocumented-404" else "/items/1"
    loaded: Any = schemathesis.openapi.from_dict(app.openapi())
    operation: Any = loaded["/items/{item_id}"]["GET"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(url)
    assert operation.validate_response(response) is None
    result = OpenAPIContract(app.openapi()).check(
        "GET", url, response.status_code, response.headers, response.content
    )
    assert [v.rule for v in result.violations] == [rule]
