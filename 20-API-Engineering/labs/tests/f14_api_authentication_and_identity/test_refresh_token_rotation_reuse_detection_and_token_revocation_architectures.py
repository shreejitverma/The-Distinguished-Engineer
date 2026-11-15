"""Tests for Refresh Token Rotation, Reuse Detection, Token Introspection, and Phantom Tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f14_api_authentication_and_identity.token_lifecycle import (
    InvalidGrantError,
    PhantomTokenGatewayMiddleware,
    TokenLifecycleManager,
    TokenStatus,
)

if TYPE_CHECKING:
    from starlette.requests import Request

JWT_SIGNING_KEY = "test-secret-key-32-bytes-long-for-hs256!"
JWT_ISSUER = "https://auth.example.com"
RESOURCE_AUDIENCE = "https://api.example.com/v1"


@pytest.fixture
def manager() -> TokenLifecycleManager:
    return TokenLifecycleManager(
        jwt_signing_key=JWT_SIGNING_KEY,
        jwt_issuer=JWT_ISSUER,
        default_access_ttl=900.0,
        default_refresh_ttl=86400.0,
        grace_period_seconds=10.0,
    )


@pytest.mark.unit
def test_initial_grant_issuance(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    at_id, rt_id = manager.issue_initial_grant(
        user_id="usr_123",
        client_id="client_mobile",
        scopes=["read", "write"],
        now=now,
    )

    assert at_id.startswith("at_")
    assert rt_id.startswith("rt_")

    rt = manager.refresh_tokens[rt_id]
    assert rt.generation == 0
    assert rt.status == TokenStatus.ACTIVE
    assert rt.parent_token_id is None
    assert rt.expires_at == now + 86400.0

    family = manager.families[rt.family_id]
    assert family.user_id == "usr_123"
    assert family.active_refresh_token_id == rt_id
    assert not family.is_revoked


@pytest.mark.unit
def test_refresh_token_rotation_success(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    _, rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)

    # First rotation (gen 0 -> gen 1)
    at1, rt1 = manager.rotate_refresh_token(rt0, "client_mobile", now=now + 60.0)
    assert rt1 != rt0
    assert at1.startswith("at_")

    rt0_rec = manager.refresh_tokens[rt0]
    rt1_rec = manager.refresh_tokens[rt1]
    assert rt0_rec.status == TokenStatus.ROTATED
    assert rt0_rec.rotated_at == now + 60.0
    assert rt1_rec.generation == 1
    assert rt1_rec.parent_token_id == rt0
    assert rt1_rec.status == TokenStatus.ACTIVE

    family = manager.families[rt0_rec.family_id]
    assert family.active_refresh_token_id == rt1

    # Second rotation (gen 1 -> gen 2)
    _at2, rt2 = manager.rotate_refresh_token(rt1, "client_mobile", now=now + 120.0)
    rt2_rec = manager.refresh_tokens[rt2]
    assert rt2_rec.generation == 2
    assert rt2_rec.parent_token_id == rt1
    assert family.active_refresh_token_id == rt2


@pytest.mark.unit
def test_refresh_token_reuse_detection_and_family_revocation(
    manager: TokenLifecycleManager,
) -> None:
    now = 1700000000.0
    _, rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)

    # Legitimate client rotates rt0 -> rt1
    _, rt1 = manager.rotate_refresh_token(rt0, "client_mobile", now=now + 10.0)

    # Attacker replays rt0 AFTER the grace period (15 seconds after rotation)
    with pytest.raises(InvalidGrantError, match="reuse detected"):
        manager.rotate_refresh_token(rt0, "client_mobile", now=now + 25.0)

    # Verify entire family is revoked
    rt0_rec = manager.refresh_tokens[rt0]
    rt1_rec = manager.refresh_tokens[rt1]
    family = manager.families[rt0_rec.family_id]

    assert family.is_revoked
    assert family.revocation_reason == "reuse_detected"
    assert rt0_rec.status == TokenStatus.REVOKED
    assert rt1_rec.status == TokenStatus.REVOKED

    # Verify audit alert was recorded
    assert len(manager.alerts) == 1
    alert = manager.alerts[0]
    assert alert.alert_type == "TOKEN_REUSE_DETECTED"
    assert alert.family_id == family.family_id
    assert alert.reused_token_id == rt0

    # Legitimate client subsequently attempting to use rt1 is also rejected
    with pytest.raises(InvalidGrantError, match="revoked"):
        manager.rotate_refresh_token(rt1, "client_mobile", now=now + 30.0)


@pytest.mark.unit
def test_grace_period_concurrency_tolerance(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    _, rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)

    # First request rotates rt0 -> (at1, rt1)
    at1, rt1 = manager.rotate_refresh_token(rt0, "client_mobile", now=now + 5.0)

    # Concurrent request presenting rt0 2 seconds later (within 10s grace period)
    at1_retry, rt1_retry = manager.rotate_refresh_token(rt0, "client_mobile", now=now + 7.0)

    # Returns the same replacement without revoking the family
    assert at1_retry == at1
    assert rt1_retry == rt1

    rt0_rec = manager.refresh_tokens[rt0]
    family = manager.families[rt0_rec.family_id]
    assert not family.is_revoked
    assert len(manager.alerts) == 0


@pytest.mark.unit
def test_grace_period_rejects_non_immediate_ancestor(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    _, rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)
    _, rt1 = manager.rotate_refresh_token(rt0, "client_mobile", now=now + 1.0)
    _, rt2 = manager.rotate_refresh_token(rt1, "client_mobile", now=now + 2.0)

    # rt0 is inside its own 10 s grace window, but rt1 has already been rotated,
    # so rt0 is a grandparent of the active tip: reuse, not a concurrent retry.
    with pytest.raises(InvalidGrantError, match="reuse detected"):
        manager.rotate_refresh_token(rt0, "client_mobile", now=now + 3.0)

    family = manager.families[manager.refresh_tokens[rt0].family_id]
    assert family.is_revoked
    assert manager.refresh_tokens[rt2].status == TokenStatus.REVOKED


@pytest.mark.unit
def test_rfc7009_hint_miss_extends_search_and_foreign_client_is_refused(
    manager: TokenLifecycleManager,
) -> None:
    now = 1700000000.0
    at0, rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)

    # A wrong hint must not stop the lookup (RFC 7009 Section 2.1).
    manager.revoke_token(at0, "client_mobile", token_type_hint="refresh_token")  # noqa: S106
    assert manager.introspect_token(at0, now=now) == {"active": False}
    assert not manager.families[manager.refresh_tokens[rt0].family_id].is_revoked

    # A token issued to another client is refused and left intact.
    with pytest.raises(InvalidGrantError, match="not issued"):
        manager.revoke_token(rt0, "client_other")
    assert manager.refresh_tokens[rt0].status == TokenStatus.ACTIVE


@pytest.mark.unit
def test_rfc7009_token_revocation(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    _at0, rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)

    # Revoke refresh token
    manager.revoke_token(rt0, "client_mobile", token_type_hint="refresh_token")  # noqa: S106

    rt0_rec = manager.refresh_tokens[rt0]
    family = manager.families[rt0_rec.family_id]
    assert family.is_revoked
    assert rt0_rec.status == TokenStatus.REVOKED

    # Introspecting the revoked token returns active=False
    intro = manager.introspect_token(rt0, now=now)
    assert not intro["active"]

    # Revoking unknown token succeeds quietly (RFC 7009 Section 2.2)
    manager.revoke_token("unknown_token_id", "client_mobile")


@pytest.mark.unit
def test_rfc7662_token_introspection(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    at0, _rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read", "write"], now=now)

    # Active token introspection
    intro = manager.introspect_token(at0, now=now + 100.0)
    assert intro["active"] is True
    assert intro["sub"] == "usr_123"
    assert intro["client_id"] == "client_mobile"
    assert intro["scope"] == "read write"
    assert intro["exp"] == int(now + 900.0)
    assert intro["jti"] == at0

    # Expired token introspection
    intro_expired = manager.introspect_token(at0, now=now + 1000.0)
    assert intro_expired == {"active": False}

    # Unknown token introspection
    assert manager.introspect_token("invalid_token") == {"active": False}


@pytest.mark.unit
def test_rfc9701_jwt_introspection_response(manager: TokenLifecycleManager) -> None:
    now = 1700000000.0
    at0, _ = manager.issue_initial_grant("usr_123", "client_mobile", ["read"], now=now)

    jwt_token = manager.create_rfc9701_jwt_introspection(
        token=at0,
        resource_server_audience="https://rs.example.com",
        now=now,
    )

    unverified_headers = jwt.get_unverified_header(jwt_token)
    assert unverified_headers["typ"] == "token-introspection+jwt"
    assert unverified_headers["alg"] == "HS256"

    payload = jwt.decode(
        jwt_token,
        JWT_SIGNING_KEY,
        algorithms=["HS256"],
        audience="https://rs.example.com",
    )
    assert payload["iss"] == JWT_ISSUER
    assert payload["aud"] == "https://rs.example.com"
    assert payload["token_introspection"]["active"] is True
    assert payload["token_introspection"]["sub"] == "usr_123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_phantom_token_gateway_translation_and_revocation(
    manager: TokenLifecycleManager,
) -> None:
    at0, _rt0 = manager.issue_initial_grant("usr_123", "client_mobile", ["read", "profile"])

    # Downstream microservice that verifies internal RFC 9068 JWT
    async def downstream_endpoint(request: Request) -> JSONResponse:
        auth_header = request.headers.get("authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        decoded = jwt.decode(
            token,
            JWT_SIGNING_KEY,
            algorithms=["HS256"],
            audience=RESOURCE_AUDIENCE,
        )
        token_type = jwt.get_unverified_header(token).get("typ")
        return JSONResponse(
            {
                "sub": decoded["sub"],
                "scope": decoded["scope"],
                "token_typ": token_type,
            }
        )

    base_app = Starlette(routes=[Route("/api/resource", downstream_endpoint)])
    gateway_app = PhantomTokenGatewayMiddleware(
        app=base_app,
        manager=manager,
        resource_audience=RESOURCE_AUDIENCE,
    )

    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Request with active opaque access token
        resp = await client.get("/api/resource", headers={"Authorization": f"Bearer {at0}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["sub"] == "usr_123"
        assert data["scope"] == "read profile"
        assert data["token_typ"] == "at+jwt"  # noqa: S105

        # Revoke the token via RFC 7009
        manager.revoke_token(at0, "client_mobile")

        # Subsequent request with revoked token fails at edge gateway with 401
        revoked_resp = await client.get("/api/resource", headers={"Authorization": f"Bearer {at0}"})
        assert revoked_resp.status_code == 401
        assert revoked_resp.json()["error"] == "invalid_token"
        assert 'Bearer error="invalid_token"' in revoked_resp.headers["WWW-Authenticate"]
