"""Unit tests for FastAPI Security() scopes, security scheme extraction, and OpenAPI wiring."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.security import SecurityScopes

from api_labs.f11_fastapi_internals_and_patterns.security_primitives import (
    Principal,
    SecurityRecorder,
    build_colliding_schemes_app,
    build_security_app,
)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def operation_security(spec: dict[str, Any], path: str, method: str) -> Any:
    return spec["paths"][path][method].get("security")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_security_scopes_accumulate_parent_first_across_security_chain() -> None:
    recorder = SecurityRecorder()
    async with client_for(build_security_app(recorder)) as client:
        response = await client.post("/orders", headers={"Authorization": "Bearer tok-writer"})

    assert response.status_code == 200
    # Endpoint -> Security(get_active_user, ["orders:write"]) -> Security(get_current_user,
    # ["orders:read"]): the outer requirement comes first, the inner one is appended.
    assert recorder.scope_log == [["orders:write", "orders:read"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insufficient_scope_is_403_with_rfc6750_scope_challenge() -> None:
    recorder = SecurityRecorder()
    async with client_for(build_security_app(recorder)) as client:
        response = await client.post("/orders", headers={"Authorization": "Bearer tok-reader"})

    assert response.status_code == 403
    assert response.headers["www-authenticate"] == (
        'Bearer error="insufficient_scope", scope="orders:write orders:read"'
    )
    assert response.json() == {"detail": "Missing scopes: orders:write"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_authorization_short_circuits_before_user_dependency() -> None:
    recorder = SecurityRecorder()
    async with client_for(build_security_app(recorder)) as client:
        response = await client.get("/orders")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Not authenticated"}
    assert recorder.scope_log == []
    assert recorder.request_id_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oauth2_password_bearer_forwards_empty_token_while_http_bearer_rejects() -> None:
    recorder = SecurityRecorder()
    async with client_for(build_security_app(recorder)) as client:
        oauth2 = await client.get("/orders", headers={"Authorization": "Bearer"})
        bearer = await client.get("/bearer", headers={"Authorization": "Bearer"})

    # OAuth2PasswordBearer only checks the scheme, so "" reaches application code.
    assert recorder.tokens_seen == [""]
    assert oauth2.status_code == 401
    assert oauth2.headers["www-authenticate"] == 'Bearer error="invalid_token", scope="orders:read"'
    # HTTPBearer requires non-empty credentials and fails inside the scheme itself.
    assert bearer.status_code == 401
    assert bearer.headers["www-authenticate"] == "Bearer"
    assert bearer.json() == {"detail": "Not authenticated"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_is_parsed_before_security_but_validated_after_it() -> None:
    recorder = SecurityRecorder()
    async with client_for(build_security_app(recorder)) as client:
        malformed = await client.post(
            "/notes", content=b"{not json", headers={"Content-Type": "application/json"}
        )
        schema_invalid = await client.post("/notes", json={"wrong": 1})

    # get_request_handler decodes JSON before solve_dependencies: no credential needed for 422.
    assert malformed.status_code == 422
    assert malformed.json()["detail"][0]["type"] == "json_invalid"
    # Field validation runs after sub-dependencies, so the scheme's 401 wins.
    assert schema_invalid.status_code == 401
    assert schema_invalid.headers["www-authenticate"] == "Bearer"
    assert recorder.scope_log == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheme_match_is_case_insensitive_and_credentials_are_stripped() -> None:
    async with client_for(build_security_app(SecurityRecorder())) as client:
        response = await client.get("/bearer", headers={"Authorization": "bearer   abc  "})

    assert response.status_code == 200
    assert response.json() == {"scheme": "bearer", "credentials": "abc"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scoped_cache_key_splits_security_calls_but_shares_plain_dependency() -> None:
    recorder = SecurityRecorder()
    async with client_for(build_security_app(recorder)) as client:
        response = await client.get("/report", headers={"Authorization": "Bearer tok-writer"})

    assert response.status_code == 200
    assert response.json() == {"same_reader_object": True, "reader_is_writer": False}
    # Three declarations, two distinct scope sets -> two calls of get_current_user.
    assert recorder.scope_log == [["orders:read"], ["orders:write"]]
    # request_id has no scope-using subtree, so both calls share one cached value.
    assert recorder.request_id_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openapi_security_requirements_mirror_scope_chain() -> None:
    async with client_for(build_security_app(SecurityRecorder())) as client:
        spec = (await client.get("/openapi.json")).json()

    schemes = spec["components"]["securitySchemes"]
    assert schemes["OAuth2PasswordBearer"] == {
        "type": "oauth2",
        "flows": {
            "password": {
                "scopes": {"orders:read": "Read orders", "orders:write": "Create orders"},
                "tokenUrl": "/token",
            }
        },
    }
    assert schemes["HTTPBearer"] == {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"}
    assert schemes["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    assert operation_security(spec, "/orders", "post") == [
        {"OAuth2PasswordBearer": ["orders:write", "orders:read"]}
    ]
    assert operation_security(spec, "/orders", "get") == [{"OAuth2PasswordBearer": ["orders:read"]}]
    assert operation_security(spec, "/report", "get") == [
        {"OAuth2PasswordBearer": ["orders:read", "orders:write"]}
    ]
    assert operation_security(spec, "/token", "post") is None
    assert "security" not in spec


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_schemes_emit_openapi_alternatives_but_runtime_requires_both() -> None:
    async with client_for(build_security_app(SecurityRecorder())) as client:
        spec = (await client.get("/openapi.json")).json()
        key_only = await client.get("/both", headers={"X-API-Key": "k"})
        both = await client.get(
            "/both", headers={"X-API-Key": "k", "Authorization": "Bearer opaque-1"}
        )

    # Two list entries mean "either one" in OpenAPI (Security Requirement Object) ...
    assert operation_security(spec, "/both", "get") == [{"HTTPBearer": []}, {"APIKeyHeader": []}]
    # ... but every dependency runs, so the server demands both.
    assert key_only.status_code == 401
    assert key_only.headers["www-authenticate"] == "Bearer"
    assert both.status_code == 200
    assert both.json() == {"credentials": "opaque-1", "key": "k"}


@pytest.mark.unit
def test_default_scheme_name_collision_keeps_last_definition() -> None:
    spec = build_colliding_schemes_app().openapi()

    schemes = spec["components"]["securitySchemes"]
    assert list(schemes) == ["OAuth2PasswordBearer"]
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/second-token"
    assert operation_security(spec, "/a", "get") == [{"OAuth2PasswordBearer": []}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_password_form_splits_scope_on_any_whitespace_and_pins_grant_type() -> None:
    async with client_for(build_security_app(SecurityRecorder())) as client:
        ok = await client.post(
            "/token",
            data={"username": "u", "password": "p", "scope": "orders:read  orders:write\tadmin"},
        )
        wrong_grant = await client.post(
            "/token",
            data={"username": "u", "password": "p", "grant_type": "client_credentials"},
        )

    assert ok.status_code == 200
    assert ok.json() == {
        "username": "u",
        "scopes": ["orders:read", "orders:write", "admin"],
        "grant_type": None,
    }
    assert wrong_grant.status_code == 422
    assert wrong_grant.json()["detail"][0]["type"] == "string_pattern_mismatch"
    assert wrong_grant.json()["detail"][0]["loc"] == ["body", "grant_type"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_key_header_missing_sends_nonstandard_apikey_challenge() -> None:
    async with client_for(build_security_app(SecurityRecorder())) as client:
        missing = await client.get("/apikey")
        lower = await client.get("/apikey", headers={"x-api-key": "k"})

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "APIKey"
    assert lower.status_code == 200
    assert lower.json() == {"key": "k"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_basic_auto_error_false_still_raises_on_malformed_credentials() -> None:
    async with client_for(build_security_app(SecurityRecorder())) as client:
        absent = await client.get("/basic-optional")
        # base64("nocolon"): decodes, but has no ":" separator.
        malformed = await client.get(
            "/basic-optional", headers={"Authorization": "Basic bm9jb2xvbg=="}
        )

    assert absent.status_code == 200
    assert absent.json() == {"username": None}
    assert malformed.status_code == 401
    assert malformed.headers["www-authenticate"] == "Basic"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_override_inherits_security_scopes() -> None:
    app = build_security_app(SecurityRecorder())
    seen: list[list[str]] = []
    override_marker = "fake"

    async def fake_user(security_scopes: SecurityScopes) -> Principal:
        seen.append(list(security_scopes.scopes))
        return Principal(token=override_marker, granted=frozenset(security_scopes.scopes))

    app.dependency_overrides[app.state.get_current_user] = fake_user
    async with client_for(app) as client:
        response = await client.post("/orders")

    assert response.status_code == 200
    assert response.json() == {"token": "fake"}
    assert seen == [["orders:write", "orders:read"]]
