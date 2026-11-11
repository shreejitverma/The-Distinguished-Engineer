"""Unit tests mapping FastAPI RequestValidationError lists to RFC 9457 problem details."""

import json
from typing import Annotated, Any, Literal

import httpx
import pytest
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from api_labs.f06_bulk_long_running_and_error_contracts.validation_problem_details import (
    MALFORMED_JSON_TYPE,
    PROBLEM_RESPONSES,
    VALIDATION_TYPE,
    build_validation_problem,
    evaluate_fragment,
    make_validation_handler,
    to_fragment,
)


class Cat(BaseModel):
    kind: Literal["cat"]
    meows: int


class Dog(BaseModel):
    kind: Literal["dog"]
    barks: int


class Item(BaseModel):
    sku: str
    price: Annotated[int, Field(gt=0)]


class Order(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    items: list[Item]
    pet: Annotated[Cat | Dog, Field(discriminator="kind")] | None = None
    quantity: int | bool | None = None
    password: str = Field(default="", min_length=12)
    labels: dict[str, int] = Field(default_factory=dict[str, int])


class Account(BaseModel):
    email: str
    name: str
    region: str


def build_app(*, problem_handler: bool, max_errors: int = 50) -> FastAPI:
    handlers: dict[Any, Any] = {}
    responses: dict[int | str, dict[str, Any]] = {}
    if problem_handler:
        handlers[RequestValidationError] = make_validation_handler(max_errors=max_errors)
        responses = dict(PROBLEM_RESPONSES)
    app = FastAPI(exception_handlers=handlers, responses=responses)

    @app.post("/orders")
    async def create_order(order: Order, limit: Annotated[int, Query(le=10)] = 1) -> dict[str, int]:
        return {"items": len(order.items), "limit": limit}

    @app.post("/accounts")
    async def create_account(account: Account) -> dict[str, str]:
        return {"email": account.email}

    return app


async def post(app: FastAPI, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, **kwargs)


BAD_ORDER: dict[str, Any] = {
    "items": [{"sku": "a-1", "price": 0}, {"price": 3}],
    "pet": {"kind": "cat", "meows": "loud"},
    "quantity": "many",
    "password": "hunter2",
    "labels": {"unit/price~eur": "x"},
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_handler_echoes_input_and_ignores_hide_input_in_errors() -> None:
    response = await post(build_app(problem_handler=False), "/orders", json=BAD_ORDER)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    detail = response.json()["detail"]
    password_error = next(e for e in detail if e["loc"] == ["body", "password"])
    assert password_error["input"] == "hunter2"
    assert [e["loc"] for e in detail if e["loc"][1] == "quantity"] == [
        ["body", "quantity", "int"],
        ["body", "quantity", "bool"],
    ]
    assert ["body", "pet", "cat", "meows"] in [e["loc"] for e in detail]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_problem_handler_emits_problem_json_with_resolvable_pointers() -> None:
    response = await post(build_app(problem_handler=True), "/orders?limit=99", json=BAD_ORDER)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["type"] == VALIDATION_TYPE
    assert problem["status"] == response.status_code
    assert problem["error_count"] == 8
    located = [
        (e["source"], e.get("pointer", e.get("parameter")), e["code"]) for e in problem["errors"]
    ]
    assert located == [
        ("query", "limit", "less_than_equal"),
        ("body", "#/items/0/price", "greater_than"),
        ("body", "#/items/1/sku", "missing"),
        ("body", "#/pet/meows", "int_parsing"),
        ("body", "#/quantity", "int_parsing"),
        ("body", "#/quantity", "bool_parsing"),
        ("body", "#/password", "string_too_short"),
        ("body", "#/labels/unit~1price~0eur", "int_parsing"),
    ]
    assert "hunter2" not in response.text
    for error in problem["errors"]:
        assert set(error) <= {"detail", "code", "source", "pointer", "parameter"}
        if error["source"] == "body" and error["code"] != "missing":
            evaluate_fragment(BAD_ORDER, error["pointer"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_member_pointer_names_absent_member_and_does_not_resolve() -> None:
    response = await post(build_app(problem_handler=True), "/orders", json=BAD_ORDER)

    missing = [e for e in response.json()["errors"] if e["code"] == "missing"]
    assert missing == [
        {
            "detail": "Field required",
            "code": "missing",
            "source": "body",
            "pointer": "#/items/1/sku",
        }
    ]
    assert evaluate_fragment(BAD_ORDER, "#/items/1") == {"price": 3}
    with pytest.raises(KeyError):
        evaluate_fragment(BAD_ORDER, missing[0]["pointer"])


@pytest.mark.unit
def test_rfc_6901_section_6_fragment_examples_round_trip() -> None:
    document: dict[str, Any] = {
        "foo": ["bar", "baz"],
        "": 0,
        "a/b": 1,
        "c%d": 2,
        "e^f": 3,
        "g|h": 4,
        "i\\j": 5,
        'k"l': 6,
        " ": 7,
        "m~n": 8,
    }
    expected = {
        "": "#/",
        "a/b": "#/a~1b",
        "c%d": "#/c%25d",
        "e^f": "#/e%5Ef",
        "g|h": "#/g%7Ch",
        "i\\j": "#/i%5Cj",
        'k"l': "#/k%22l",
        " ": "#/%20",
        "m~n": "#/m~0n",
    }
    for key, fragment in expected.items():
        assert to_fragment([key]) == fragment
        assert evaluate_fragment(document, fragment) == document[key]
    assert to_fragment([]) == "#"
    assert evaluate_fragment(document, "#") is document
    assert to_fragment(["foo", 0]) == "#/foo/0"
    assert evaluate_fragment(document, "#/foo/0") == "bar"
    assert to_fragment(["prix\u00e9"]) == "#/prix%C3%A9"
    assert to_fragment(["a:b@c=d,e"]) == "#/a:b@c=d,e"


@pytest.mark.unit
def test_escape_order_tilde_before_slash() -> None:
    assert to_fragment(["~1"]) == "#/~01"
    assert evaluate_fragment({"~1": "tilde-one", "/": "slash"}, "#/~01") == "tilde-one"


@pytest.mark.unit
def test_array_index_rejects_leading_zero_and_non_ascii_digits() -> None:
    document = {"items": ["a", "b"]}
    assert evaluate_fragment(document, "#/items/1") == "b"
    for bad in ("#/items/01", "#/items/-", "#/items/%C2%B2"):
        with pytest.raises(KeyError):
            evaluate_fragment(document, bad)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_json_is_400_without_pointer() -> None:
    raw = b'{"items": ['
    default = await post(
        build_app(problem_handler=False),
        "/orders",
        content=raw,
        headers={"content-type": "application/json"},
    )
    problem = await post(
        build_app(problem_handler=True),
        "/orders",
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert default.status_code == 422
    assert default.json()["detail"][0]["loc"] == ["body", 11]
    assert problem.status_code == 400
    assert problem.headers["content-type"] == "application/problem+json"
    assert problem.json() == {
        "type": MALFORMED_JSON_TYPE,
        "title": "Malformed JSON request body",
        "status": 400,
        "detail": "The request body is not valid JSON: Expecting value.",
        "offset": 11,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_model_level_error_points_at_document_root() -> None:
    response = await post(build_app(problem_handler=True), "/accounts", json=[1, 2])

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {
            "detail": "Input should be a valid dictionary or object to extract fields from",
            "code": "model_attributes_type",
            "source": "body",
            "pointer": "#",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_field_input_echo_amplifies_and_problem_handler_does_not() -> None:
    blob = {"padding": "x" * 65_536}
    request_bytes = len(json.dumps(blob).encode())
    default = await post(build_app(problem_handler=False), "/accounts", json=blob)
    problem = await post(build_app(problem_handler=True), "/accounts", json=blob)

    assert [e["type"] for e in default.json()["detail"]] == ["missing"] * 3
    assert len(default.content) > 3 * request_bytes
    assert len(problem.content) < 1024
    assert problem.json()["error_count"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_list_is_capped_but_true_count_reported() -> None:
    body = {"items": [{"sku": f"s{i}", "price": -1} for i in range(1000)]}
    default = await post(build_app(problem_handler=False), "/orders", json=body)
    problem = await post(build_app(problem_handler=True, max_errors=50), "/orders", json=body)

    assert len(default.json()["detail"]) == 1000
    assert problem.json()["error_count"] == 1000
    assert len(problem.json()["errors"]) == 50
    assert problem.json()["errors"][-1]["pointer"] == "#/items/49/price"
    assert len(problem.content) * 5 < len(default.content)


@pytest.mark.unit
def test_build_validation_problem_drops_ctx_input_and_url() -> None:
    errors = [
        {
            "type": "value_error",
            "loc": ("body", "code"),
            "msg": "Value error, code is bad",
            "input": "secret-input",
            "ctx": {"error": ValueError("code is bad")},
            "url": "https://errors.pydantic.dev/2.14/v/value_error",
        }
    ]
    problem = build_validation_problem(errors, {"code": "secret-input"})

    assert problem["errors"] == [
        {
            "detail": "Value error, code is bad",
            "code": "value_error",
            "source": "body",
            "pointer": "#/code",
        }
    ]
    assert problem["detail"] == "The request has 1 validation error."
    json.dumps(problem)


@pytest.mark.unit
def test_custom_handler_leaves_openapi_422_stale_unless_responses_declared() -> None:
    stale = FastAPI(exception_handlers={RequestValidationError: make_validation_handler()})

    @stale.post("/accounts")
    async def create(account: Account) -> dict[str, str]:
        return {"email": account.email}

    stale_422 = stale.openapi()["paths"]["/accounts"]["post"]["responses"]["422"]
    fixed = build_app(problem_handler=True).openapi()
    fixed_422 = fixed["paths"]["/accounts"]["post"]["responses"]["422"]

    assert list(stale_422["content"]) == ["application/json"]
    assert "HTTPValidationError" in stale.openapi()["components"]["schemas"]
    assert list(fixed_422["content"]) == ["application/problem+json"]
    assert "HTTPValidationError" not in fixed.get("components", {}).get("schemas", {})


@pytest.mark.unit
def test_evaluate_fragment_rejects_non_fragment_and_relative_syntax() -> None:
    cases = {"/items": "not a URI fragment pointer", "#items": "pointer must start with '/'"}
    for bad, message in cases.items():
        with pytest.raises(ValueError, match=message):
            evaluate_fragment({"items": []}, bad)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_refuses_foreign_exception_types() -> None:
    handler = make_validation_handler()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    with pytest.raises(TypeError, match="expected RequestValidationError, got ValueError"):
        await handler(request, ValueError("not a validation error"))
