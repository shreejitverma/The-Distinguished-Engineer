"""Unit tests for OAuth Client Credentials vs RFC 8693 Token Exchange.

Tests cover:
- Client Credentials M2M token generation and loss of end-user context.
- RFC 8693 Token Exchange On-Behalf-Of delegation with 'act' actor claim.
- Multi-hop delegation chains with nested 'act' claims (RFC 8693 Section 4.1).
- Scope downscoping and prevention of scope escalation ('invalid_scope').
- Target audience constraint ('invalid_target').
- Impersonation vs delegation semantics (RFC 8693 Section 1.1).
- 'may_act' authorized actor claim enforcement (RFC 8693 Section 4.4).
- 'actor_token' pairing and actor identity (RFC 8693 Section 2.1).
- Token lifetime bounded by subject token expiration.
- Maximum delegation depth limits preventing recursion bombs.
- Error handling for invalid grants, expired tokens, and invalid client credentials.
- In-process ASGI wire-level HTTP tests with httpx.AsyncClient.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import status

from api_labs.f14_api_authentication_and_identity.service_to_service_identity import (
    ERROR_INVALID_CLIENT,
    ERROR_INVALID_REQUEST,
    ERROR_INVALID_SCOPE,
    ERROR_INVALID_TARGET,
    ERROR_UNAUTHORIZED_CLIENT,
    GRANT_TYPE_TOKEN_EXCHANGE,
    TOKEN_TYPE_ACCESS_TOKEN,
    TOKEN_TYPE_JWT,
    ClientRegistration,
    OAuthError,
    SecurityTokenService,
    TokenExchangeRequest,
    create_downstream_service,
    create_sts_app,
)


@pytest.fixture
def sts() -> SecurityTokenService:
    service = SecurityTokenService(issuer="https://as.example.internal")
    # Service A: standard gateway / backend proxy
    service.register_client(
        ClientRegistration(
            client_id="service-a",
            client_secret="secret-service-a",  # noqa: S106 - test fixture
            allowed_audiences=["service-b", "service-c"],
            can_delegate=True,
            can_impersonate=False,
            max_delegation_depth=4,
        )
    )
    # Service B: intermediate microservice
    service.register_client(
        ClientRegistration(
            client_id="service-b",
            client_secret="secret-service-b",  # noqa: S106 - test fixture
            allowed_audiences=["service-c"],
            can_delegate=True,
            can_impersonate=False,
            max_delegation_depth=4,
        )
    )
    # Impersonation-capable admin service
    service.register_client(
        ClientRegistration(
            client_id="admin-service",
            client_secret="secret-admin",  # noqa: S106 - test fixture
            allowed_audiences=["service-b", "service-c"],
            can_delegate=True,
            can_impersonate=True,
        )
    )
    # Trusted delegate service
    service.register_client(
        ClientRegistration(
            client_id="trusted-delegate",
            client_secret="secret-delegate",  # noqa: S106 - test fixture
            allowed_audiences=["service-b"],
            can_delegate=True,
        )
    )
    return service


@pytest.mark.unit
def test_client_credentials_m2m_token_loss_of_user_context(sts: SecurityTokenService) -> None:
    """OAuth 2.0 Client Credentials Grant yields M2M token identifying only the service caller."""
    client = sts.clients["service-a"]
    token_resp = sts.issue_client_credentials(client, audience="service-b", scope="read:records")

    claims = sts.decode_token(token_resp.access_token, audience="service-b")
    # sub is service-a, NOT the end-user
    assert claims["sub"] == "service-a"
    assert claims["client_id"] == "service-a"
    assert claims["aud"] == "service-b"
    assert claims["scope"] == "read:records"
    assert "act" not in claims


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_credentials_fails_downstream_user_access_control(
    sts: SecurityTokenService,
) -> None:
    """Downstream resource server enforcing end-user row level security rejects M2M token."""
    client = sts.clients["service-a"]
    token_resp = sts.issue_client_credentials(client, audience="service-b", scope="read:records")

    app_b = create_downstream_service(
        sts,
        expected_audience="service-b",
        required_scope="read:records",
    )
    transport = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client_http:
        # Request record for user 'alice' with client credentials token (sub='service-a')
        resp = await client_http.get(
            "/records/alice",
            headers={"Authorization": f"Bearer {token_resp.access_token}"},
        )
        assert resp.status_code == status.HTTP_403_FORBIDDEN
        assert "is not authorized to access record 'alice'" in resp.json()["detail"]


@pytest.mark.unit
def test_token_exchange_delegation_preserves_subject_and_actor(sts: SecurityTokenService) -> None:
    """RFC 8693 Token Exchange preserves end-user sub and injects act actor claim."""
    # 1. Inbound user token for alice targeting service-a
    inbound_token = sts.mint_token(
        {
            "sub": "alice",
            "aud": "service-a",
            "scope": "read:records write:records profile",
        },
        lifetime_seconds=3600,
    )

    # 2. Service A exchanges inbound token for service-b token downscoped to read:records
    client_a = sts.clients["service-a"]
    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    exchange_resp = sts.issue_token_exchange(client_a, req)

    # 3. Verify issued token claims
    claims = sts.decode_token(exchange_resp.access_token, audience="service-b")
    assert claims["sub"] == "alice"
    assert claims["aud"] == "service-b"
    assert claims["scope"] == "read:records"
    assert claims["client_id"] == "service-a"
    assert claims["act"] == {"sub": "service-a"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_exchange_succeeds_downstream_with_actor_audit(
    sts: SecurityTokenService,
) -> None:
    """Downstream service validates end-user identity while logging current actor in audit trail."""
    inbound_token = sts.mint_token(
        {
            "sub": "alice",
            "aud": "service-a",
            "scope": "read:records write:records",
        },
        lifetime_seconds=3600,
    )

    client_a = sts.clients["service-a"]
    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    exchange_resp = sts.issue_token_exchange(client_a, req)

    app_b = create_downstream_service(
        sts,
        expected_audience="service-b",
        required_scope="read:records",
    )
    transport = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client_http:
        resp = await client_http.get(
            "/records/alice",
            headers={"Authorization": f"Bearer {exchange_resp.access_token}"},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["owner"] == "alice"
        assert data["caller_subject"] == "alice"
        assert data["current_actor"] == "service-a"
        assert data["delegation_chain"] == {"sub": "service-a"}


@pytest.mark.unit
def test_multi_hop_delegation_chain_nested_act_claim(sts: SecurityTokenService) -> None:
    """RFC 8693 Section 4.1: Multi-hop delegation forms recursively nested act claims."""
    # Hop 0: Inbound token for user 'alice'
    token_hop0 = sts.mint_token(
        {"sub": "alice", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=3600,
    )

    # Hop 1: Service A exchanges token for Service B
    client_a = sts.clients["service-a"]
    req1 = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=token_hop0,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    token_hop1 = sts.issue_token_exchange(client_a, req1).access_token

    # Hop 2: Service B exchanges Hop 1 token for Service C
    client_b = sts.clients["service-b"]
    req2 = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-c",
        scope="read:records",
        subject_token=token_hop1,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    token_hop2 = sts.issue_token_exchange(client_b, req2).access_token

    claims_hop2 = sts.decode_token(token_hop2, audience="service-c")
    assert claims_hop2["sub"] == "alice"
    assert claims_hop2["aud"] == "service-c"
    assert claims_hop2["client_id"] == "service-b"

    # Verify recursive act structure: current actor is service-b, prior actor is service-a
    expected_act = {
        "sub": "service-b",
        "act": {
            "sub": "service-a",
        },
    }
    assert claims_hop2["act"] == expected_act


@pytest.mark.unit
def test_scope_downscoping_and_escalation_rejection(sts: SecurityTokenService) -> None:
    """STS enforces downscoping and rejects scope expansion with invalid_scope."""
    inbound_token = sts.mint_token(
        {"sub": "bob", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=3600,
    )

    client_a = sts.clients["service-a"]

    # 1. Valid downscope (exact or subset)
    req_valid = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    resp = sts.issue_token_exchange(client_a, req_valid)
    assert resp.scope == "read:records"

    # 2. Scope escalation attempt
    req_escalate = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records write:records admin",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, req_escalate)
    assert exc_info.value.error == ERROR_INVALID_SCOPE
    assert "missing" in exc_info.value.error_description


@pytest.mark.unit
def test_target_audience_validation_and_invalid_target(sts: SecurityTokenService) -> None:
    """STS validates requested target audience against client permissions.

    Rejects unpermitted targets with invalid_target.
    """
    inbound_token = sts.mint_token(
        {"sub": "charlie", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=3600,
    )
    client_a = sts.clients["service-a"]

    # Target not in client_a.allowed_audiences (disallowed target)
    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="unauthorized-service-z",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, req)
    assert exc_info.value.error == ERROR_INVALID_TARGET


@pytest.mark.unit
def test_impersonation_mode_omits_act_claim(sts: SecurityTokenService) -> None:
    """RFC 8693 Section 1.1: Impersonation issues token with subject identity but no actor claim."""
    inbound_token = sts.mint_token(
        {"sub": "diana", "aud": "admin-service", "scope": "read:records write:records"},
        lifetime_seconds=3600,
    )
    admin_client = sts.clients["admin-service"]

    # Authorized impersonation
    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
        want_impersonation=True,
    )
    resp = sts.issue_token_exchange(admin_client, req)
    claims = sts.decode_token(resp.access_token, audience="service-b")

    assert claims["sub"] == "diana"
    assert "act" not in claims

    # Unauthorized impersonation by normal service
    client_a = sts.clients["service-a"]
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, req)
    assert exc_info.value.error == ERROR_UNAUTHORIZED_CLIENT


@pytest.mark.unit
def test_may_act_claim_enforces_authorized_actors(sts: SecurityTokenService) -> None:
    """RFC 8693 Section 4.4: Subject token containing may_act.

    Restricts exchange to designated actor.
    """
    # Inbound token designating 'trusted-delegate' as the only authorized actor
    inbound_token = sts.mint_token(
        {
            "sub": "eve",
            "aud": "gateway",
            "scope": "read:records",
            "may_act": {"sub": "trusted-delegate"},
        },
        lifetime_seconds=3600,
    )

    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )

    # 1. Unauthorized service attempts exchange: a policy-rejected subject_token
    # is invalid_request with HTTP 400 (RFC 8693 Section 2.2.2)
    client_a = sts.clients["service-a"]
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, req)
    assert exc_info.value.error == ERROR_INVALID_REQUEST
    assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
    assert "not authorized actor" in exc_info.value.error_description

    # 2. Designated trusted-delegate executes exchange successfully
    delegate_client = sts.clients["trusted-delegate"]
    resp = sts.issue_token_exchange(delegate_client, req)
    claims = sts.decode_token(resp.access_token, audience="service-b")
    assert claims["sub"] == "eve"
    assert claims["act"] == {"sub": "trusted-delegate"}


@pytest.mark.unit
def test_actor_token_sets_current_actor_and_pairing_rules(sts: SecurityTokenService) -> None:
    """RFC 8693 Section 2.1: actor_token names the actor; the type travels with it."""
    subject = sts.mint_token(
        {"sub": "dana", "aud": "gateway", "scope": "read:records", "may_act": {"sub": "service-b"}},
    )
    actor = sts.mint_token({"sub": "service-b", "aud": "sts"})
    client_a = sts.clients["service-a"]

    def request(
        actor_token: str | None = None, actor_token_type: str | None = None
    ) -> TokenExchangeRequest:
        return TokenExchangeRequest(
            grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
            audience="service-b",
            scope="read:records",
            subject_token=subject,
            subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
            actor_token=actor_token,
            actor_token_type=actor_token_type,
        )

    # The actor named by actor_token (not the authenticated client) is the current
    # actor, and may_act is checked against that actor.
    resp = sts.issue_token_exchange(
        client_a, request(actor_token=actor, actor_token_type=TOKEN_TYPE_JWT)
    )
    claims = sts.decode_token(resp.access_token, audience="service-b")
    assert claims["sub"] == "dana"
    assert claims["act"] == {"sub": "service-b"}

    # actor_token_type MUST NOT appear without actor_token.
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, request(actor_token_type=TOKEN_TYPE_JWT))
    assert exc_info.value.error == ERROR_INVALID_REQUEST

    # actor_token requires a supported actor_token_type.
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, request(actor_token=actor))
    assert exc_info.value.error == ERROR_INVALID_REQUEST

    # A tampered actor_token is invalid_request, not a silent fallback to the client.
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(
            client_a,
            request(actor_token=actor[:-4] + "AAAA", actor_token_type=TOKEN_TYPE_JWT),
        )
    assert exc_info.value.error == ERROR_INVALID_REQUEST
    assert "Invalid actor_token" in exc_info.value.error_description

    # may_act names service-b, so a different actor_token subject is rejected.
    other = sts.mint_token({"sub": "service-x", "aud": "sts"})
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(
            client_a, request(actor_token=other, actor_token_type=TOKEN_TYPE_JWT)
        )
    assert "not authorized actor" in exc_info.value.error_description


@pytest.mark.unit
def test_token_lifetime_bounded_by_subject_token_expiration(sts: SecurityTokenService) -> None:
    """Issued token expiration is bounded by the remaining lifetime of the subject token."""
    short_lifetime = 45
    inbound_token = sts.mint_token(
        {"sub": "frank", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=short_lifetime,
    )

    client_a = sts.clients["service-a"]
    req = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=inbound_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    # Default requested lifetime is 3600s, but should be clamped to <= 45s
    resp = sts.issue_token_exchange(client_a, req, lifetime_seconds=3600)
    assert resp.expires_in <= short_lifetime
    assert resp.expires_in >= short_lifetime - 2


@pytest.mark.unit
def test_delegation_depth_limit_prevents_recursion_bomb(sts: SecurityTokenService) -> None:
    """STS prevents unbounded recursion attacks by enforcing max_delegation_depth."""
    token = sts.mint_token(
        {"sub": "grace", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=3600,
    )

    client = sts.clients["service-a"]  # max_delegation_depth = 4

    # Simulate accumulating hops up to the maximum allowed depth (4)
    for _hop in range(1, 5):
        req = TokenExchangeRequest(
            grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
            audience="service-b",
            scope="read:records",
            subject_token=token,
            subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
        )
        token = sts.issue_token_exchange(client, req).access_token

    # 5th exchange attempts depth 5, exceeding max depth of 4
    req_exceed = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        scope="read:records",
        subject_token=token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client, req_exceed)
    assert exc_info.value.error == ERROR_INVALID_REQUEST
    assert "Maximum delegation depth" in exc_info.value.error_description


@pytest.mark.unit
def test_invalid_and_expired_subject_token_rejected(sts: SecurityTokenService) -> None:
    """STS rejects tampered or expired subject tokens with invalid_request (RFC 8693 2.2.2)."""
    client_a = sts.clients["service-a"]

    # 1. Expired subject token
    expired_token = sts.mint_token(
        {"sub": "heidi", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=-10,
    )
    req_expired = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        subject_token=expired_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, req_expired)
    assert exc_info.value.error == ERROR_INVALID_REQUEST
    assert "expired" in exc_info.value.error_description

    # 2. Tampered token
    tampered_token = expired_token[:-4] + "fake"
    req_tampered = TokenExchangeRequest(
        grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
        audience="service-b",
        subject_token=tampered_token,
        subject_token_type=TOKEN_TYPE_ACCESS_TOKEN,
    )
    with pytest.raises(OAuthError) as exc_info:
        sts.issue_token_exchange(client_a, req_tampered)
    assert exc_info.value.error == ERROR_INVALID_REQUEST


@pytest.mark.unit
def test_unauthenticated_client_rejected(sts: SecurityTokenService) -> None:
    """STS rejects requests with missing or invalid client credentials."""
    # Bad basic auth
    with pytest.raises(OAuthError) as exc_info:
        sts.authenticate_client("Basic invalid_base64!", None, None)
    assert exc_info.value.error == ERROR_INVALID_CLIENT
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED

    # Wrong secret
    with pytest.raises(OAuthError) as exc_info:
        sts.authenticate_client(None, "service-a", "wrong-secret")
    assert exc_info.value.error == ERROR_INVALID_CLIENT
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_process_asgi_token_endpoint_wire_flow(sts: SecurityTokenService) -> None:
    """In-process wire HTTP test of RFC 8693 token exchange via httpx.AsyncClient."""
    sts_app = create_sts_app(sts)
    app_b = create_downstream_service(
        sts,
        expected_audience="service-b",
        required_scope="read:records",
    )

    inbound_user_token = sts.mint_token(
        {"sub": "ian", "aud": "service-a", "scope": "read:records write:records"},
        lifetime_seconds=3600,
    )

    transport_sts = httpx.ASGITransport(app=sts_app)
    async with httpx.AsyncClient(transport=transport_sts, base_url="http://sts") as sts_client:
        # Form body request conforming to RFC 8693 Section 2.1
        resp = await sts_client.post(
            "/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
                "client_id": "service-a",
                "client_secret": "secret-service-a",
                "audience": "service-b",
                "scope": "read:records",
                "subject_token": inbound_user_token,
                "subject_token_type": TOKEN_TYPE_ACCESS_TOKEN,
                "requested_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            },
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["token_type"] == "Bearer"  # noqa: S105 - OAuth token_type parameter
        assert data["issued_token_type"] == TOKEN_TYPE_ACCESS_TOKEN
        assert data["scope"] == "read:records"
        exchanged_token = data["access_token"]

    # Use the exchanged token to invoke Service B
    transport_b = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport_b, base_url="http://service-b") as svc_client:
        svc_resp = await svc_client.get(
            "/records/ian",
            headers={"Authorization": f"Bearer {exchanged_token}"},
        )
        assert svc_resp.status_code == status.HTTP_200_OK
        body = svc_resp.json()
        assert body["status"] == "success"
        assert body["owner"] == "ian"
        assert body["caller_subject"] == "ian"
        assert body["current_actor"] == "service-a"
        assert body["delegation_chain"] == {"sub": "service-a"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_process_asgi_token_endpoint_error_responses(sts: SecurityTokenService) -> None:
    """Token endpoint errors are RFC 6749 Section 5.2 JSON bodies, not server errors."""
    sts_app = create_sts_app(sts)
    inbound_user_token = sts.mint_token(
        {"sub": "judy", "aud": "service-a", "scope": "read:records"},
        lifetime_seconds=3600,
    )
    transport_sts = httpx.ASGITransport(app=sts_app)
    async with httpx.AsyncClient(transport=transport_sts, base_url="http://sts") as sts_client:
        # Scope escalation over the wire: 400 invalid_scope
        resp = await sts_client.post(
            "/oauth/token",
            data={
                "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
                "client_id": "service-a",
                "client_secret": "secret-service-a",
                "audience": "service-b",
                "scope": "read:records write:records",
                "subject_token": inbound_user_token,
                "subject_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            },
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"] == ERROR_INVALID_SCOPE

        # Wrong client secret via HTTP Basic: 401 invalid_client with a Basic challenge
        resp = await sts_client.post(
            "/oauth/token",
            auth=("service-a", "wrong-secret"),
            data={"grant_type": GRANT_TYPE_TOKEN_EXCHANGE},
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert resp.json()["error"] == ERROR_INVALID_CLIENT
        assert resp.headers["www-authenticate"].startswith("Basic")

    # Downstream rejects an expired exchanged token with 401 invalid_token (RFC 6750 3.1)
    app_b = create_downstream_service(
        sts, expected_audience="service-b", required_scope="read:records"
    )
    expired = sts.mint_token(
        {"sub": "judy", "aud": "service-b", "scope": "read:records"},
        lifetime_seconds=-10,
    )
    transport_b = httpx.ASGITransport(app=app_b)
    async with httpx.AsyncClient(transport=transport_b, base_url="http://service-b") as svc:
        resp = await svc.get("/records/judy", headers={"Authorization": f"Bearer {expired}"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]
