"""Lab: Property-Based API Fuzzing with Schemathesis (4.33.1) and Hypothesis (6.170.14)."""

from __future__ import annotations

from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample
from pydantic import BaseModel, Field
from pydantic_core import PydanticSerializationError
from schemathesis.core.failures import FailureGroup

from api_labs.f08_openapi_json_schema_and_contracts.property_fuzzing import (
    API_KEYS,
    NUL,
    Finding,
    ServerErrorBoundary,
    build_app,
    headers_of,
    load_schema,
    minimal_failing_case,
    minimal_failing_text,
    run_engine,
)

FUZZ_PHASES = ("examples", "coverage", "fuzzing")
SURROGATE_BODY = b'{"name": "\\ud800\\ud800"}'
JSON = {"content-type": "application/json"}


class ShortName(BaseModel):
    name: Annotated[str, Field(max_length=1)]


def _client(app: ServerErrorBoundary) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _server_errors(findings: list[Finding]) -> set[str]:
    return {f.operation for f in findings if f.code == "ST101"}


@pytest.mark.unit
def test_starlette_reraises_so_in_process_client_sees_exception_not_500() -> None:
    schema = load_schema(build_app(fixed=False), phases=("fuzzing",), max_examples=1, seed=1)
    case = schema["/ratio"]["GET"].Case(query={"x": 0})
    with pytest.raises(ZeroDivisionError):
        case.call()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_boundary_answers_500_and_records_the_exception() -> None:
    app = ServerErrorBoundary(build_app(fixed=False))
    async with _client(app) as client:
        response = await client.get("/ratio", params={"x": "0"})
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert [type(exc) for exc in app.errors] == [ZeroDivisionError]


@pytest.mark.unit
def test_find_shrinks_division_bug_to_zero() -> None:
    case = minimal_failing_case(ServerErrorBoundary(build_app(fixed=False)), "/ratio", "GET")
    assert case.query == {"x": 0.0}


@pytest.mark.unit
def test_find_shrinks_storage_bug_to_a_single_nul() -> None:
    app = ServerErrorBoundary(build_app(fixed=False))
    case = minimal_failing_case(app, "/users", "POST")
    assert case.body == {"name": NUL}
    assert "0x00" in str(app.errors[-1])


@pytest.mark.unit
def test_find_shrinks_auth_bug_to_an_empty_key() -> None:
    # Security parameters are always generated, so the simplest failing key is "", not absent.
    case = minimal_failing_case(ServerErrorBoundary(build_app(fixed=False)), "/me", "GET")
    assert headers_of(case) == {"X-API-Key": ""}


@pytest.mark.unit
def test_subnormal_input_overflows_to_null_and_breaks_the_response_schema() -> None:
    app = ServerErrorBoundary(build_app(fixed=False))
    schema = load_schema(app, phases=("fuzzing",), max_examples=1, seed=1)
    case = schema["/ratio"]["GET"].Case(query={"x": 5e-324})
    response = case.call()
    assert (response.status_code, response.json()) == (200, {"inverse": None})
    with pytest.raises(FailureGroup) as caught:
        case.validate_response(response)
    assert [failure.code for failure in caught.value.exceptions] == ["ST108"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nan_query_is_accepted_unless_allow_inf_nan_is_false() -> None:
    async with _client(ServerErrorBoundary(build_app(fixed=False))) as client:
        lax = await client.get("/ratio", params={"x": "nan"})
    async with _client(ServerErrorBoundary(build_app(fixed=True))) as client:
        strict = await client.get("/ratio", params={"x": "nan"})
    assert (lax.status_code, lax.json()) == (200, {"inverse": None})
    assert strict.status_code == 422
    assert strict.json()["detail"][0]["type"] == "finite_number"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lone_surrogate_passes_validation_but_fails_serialization() -> None:
    buggy = ServerErrorBoundary(build_app(fixed=False))
    async with _client(buggy) as client:
        crashed = await client.post("/users", content=b'{"name": "\\ud800"}', headers=JSON)
    assert crashed.status_code == 500
    assert isinstance(buggy.errors[-1], PydanticSerializationError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_422_handler_reflects_input_and_crashes_on_a_surrogate() -> None:
    plain = FastAPI()

    @plain.post("/users")
    def create(user: ShortName) -> dict[str, str]:
        return {"name": user.name}

    default = ServerErrorBoundary(plain)
    async with _client(default) as client:
        crashed = await client.post("/users", content=SURROGATE_BODY, headers=JSON)
    async with _client(ServerErrorBoundary(build_app(fixed=True))) as client:
        rejected = await client.post("/users", content=SURROGATE_BODY, headers=JSON)
    assert crashed.status_code == 500
    assert isinstance(default.errors[-1], UnicodeEncodeError)
    assert rejected.status_code == 422
    assert "input" not in rejected.json()["detail"][0]


@pytest.mark.unit
def test_hypothesis_characters_reach_surrogates_and_utf8_codec_never_does() -> None:
    def is_surrogate(char: str) -> bool:
        return 0xD800 <= ord(char) <= 0xDFFF

    assert minimal_failing_text(st.characters(), is_surrogate) == "\ud800"
    with pytest.raises(NoSuchExample):
        minimal_failing_text(st.characters(codec="utf-8"), is_surrogate)


@pytest.mark.unit
def test_shrinking_past_nul_lands_on_the_first_surrogate() -> None:
    app = ServerErrorBoundary(build_app(fixed=False))
    schema = load_schema(app, phases=("fuzzing",), max_examples=1, seed=1)
    operation = schema["/users"]["POST"]

    def crashes(char: str) -> bool:
        return char != NUL and operation.Case(body={"name": char}).call().status_code == 500

    assert minimal_failing_text(st.characters(), crashes) == "\ud800"


@pytest.mark.unit
def test_coverage_phase_is_seed_independent_and_misses_the_nul() -> None:
    runs = [
        run_engine(ServerErrorBoundary(build_app(fixed=False)), phases=("coverage",), seed=seed)
        for seed in (1, 2)
    ]
    assert runs[0] == runs[1]
    assert _server_errors(runs[0]) == {"GET /ratio", "GET /me"}


@pytest.mark.unit
def test_fuzzing_phase_finds_all_three_server_errors() -> None:
    findings = run_engine(
        ServerErrorBoundary(build_app(fixed=False)), phases=("fuzzing",), max_examples=1000
    )
    assert _server_errors(findings) == {"GET /ratio", "GET /me", "POST /users"}


@pytest.mark.unit
def test_undocumented_400_is_a_conformance_failure() -> None:
    app = ServerErrorBoundary(build_app(fixed=True, document_400=False))
    findings = run_engine(app, phases=FUZZ_PHASES)
    assert {(f.operation, f.code) for f in findings} == {("POST /users", "ST103")}


@pytest.mark.unit
def test_fixed_app_passes_every_default_check() -> None:
    app = ServerErrorBoundary(build_app(fixed=True))
    assert run_engine(app, phases=FUZZ_PHASES, max_examples=1000) == []
    assert app.errors == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fixed_app_answers_401_without_a_valid_key_and_200_with_one() -> None:
    (key,) = API_KEYS
    async with _client(ServerErrorBoundary(build_app(fixed=True))) as client:
        missing = await client.get("/me")
        empty = await client.get("/me", headers={"X-API-Key": ""})
        valid = await client.get("/me", headers={"X-API-Key": key})
    assert (missing.status_code, empty.status_code) == (401, 401)
    assert missing.headers["www-authenticate"] == "APIKey"
    assert valid.json() == {"tenant": "tenant-a"}
