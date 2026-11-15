"""Executable verification tests for PAR (RFC 9126) and RAR (RFC 9396)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import TYPE_CHECKING

import httpx
import jwt
import pytest

from api_labs.f14_api_authentication_and_identity.par_rar import (
    JWT_SECRET,
    ClientRecord,
    OAuthStateStore,
    create_app,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE S256 code_verifier and code_challenge (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def make_test_context() -> tuple[FastAPI, OAuthStateStore]:
    """Create a test FastAPI instance wired to an initialized OAuth state store."""
    s = OAuthStateStore()
    s.register_client(
        ClientRecord(
            client_id="confidential_fintech_app",
            client_secret="secure_secret_value_12345",  # noqa: S106
            is_public=False,
            redirect_uris=["https://client.example.org/callback"],
            allowed_scopes=["payments", "accounts"],
            require_pushed_authorization_requests=True,
        )
    )
    s.register_client(
        ClientRecord(
            client_id="public_mobile_app",
            client_secret=None,
            is_public=True,
            redirect_uris=["https://mobile.example.org/native-cb"],
            allowed_scopes=["payments"],
            require_pushed_authorization_requests=True,
        )
    )
    s.register_client(
        ClientRecord(
            client_id="attacker_client",
            client_secret="attacker_secret",  # noqa: S106
            is_public=False,
            redirect_uris=["https://attacker.example.org/cb"],
            allowed_scopes=["payments"],
            require_pushed_authorization_requests=True,
        )
    )
    return create_app(s), s


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oauth_server_metadata_advertises_par_and_rar() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        res = await client.get("/.well-known/oauth-authorization-server")
        assert res.status_code == 200
        data = res.json()
        assert data["pushed_authorization_request_endpoint"] == "http://test/as/par"
        assert data["require_pushed_authorization_requests"] is True
        assert "payment_initiation" in data["authorization_details_types_supported"]
        assert "account_information" in data["authorization_details_types_supported"]
        assert "S256" in data["code_challenge_methods_supported"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_par_confidential_client_success() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        rar_payload = [
            {
                "type": "payment_initiation",
                "actions": ["initiate", "status"],
                "locations": ["https://api.example.com/payments"],
                "instructedAmount": {"currency": "EUR", "amount": "150.00"},
                "creditorAccount": {"iban": "DE02100100109307118603"},
            }
        ]

        res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "state": "state_xyz789",
                "authorization_details": json.dumps(rar_payload),
            },
        )
        assert res.status_code == 201
        assert res.headers["cache-control"] == "no-store"
        body = res.json()
        assert "request_uri" in body
        assert body["request_uri"].startswith("urn:ietf:params:oauth:request_uri:")
        assert body["expires_in"] == 90


@pytest.mark.unit
@pytest.mark.asyncio
async def test_par_public_client_pkce_success() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        _, challenge = generate_pkce_pair()
        res = await client.post(
            "/as/par",
            data={
                "response_type": "code",
                "client_id": "public_mobile_app",
                "redirect_uri": "https://mobile.example.org/native-cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "mobile_state_123",
            },
        )
        assert res.status_code == 201
        body = res.json()
        assert "request_uri" in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_par_forbids_request_uri_parameter() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "request_uri": "urn:ietf:params:oauth:request_uri:forbidden_payload",
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "invalid_request"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_par_client_authentication_failure() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        bad_auth = "Basic " + base64.b64encode(b"confidential_fintech_app:wrong_password").decode(
            "ascii"
        )
        res = await client.post(
            "/as/par",
            headers={"Authorization": bad_auth},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
            },
        )
        assert res.status_code == 401
        assert res.json()["detail"]["error"] == "invalid_client"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_par_unregistered_redirect_uri_rejected() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://evil-attacker.example.org/steal-code",
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "invalid_request"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rar_strict_validation_rejects_unknown_type() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        invalid_rar = [{"type": "unregistered_crypto_trade", "asset": "BTC"}]
        res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "authorization_details": json.dumps(invalid_rar),
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "invalid_authorization_details"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rar_strict_validation_rejects_unknown_fields() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        tampered_rar = [
            {
                "type": "payment_initiation",
                "actions": ["initiate"],
                "instructedAmount": {"currency": "EUR", "amount": "50.00"},
                "creditorAccount": {"iban": "DE02100100109307118603"},
                "injected_unrecognized_field": "exploit",
            }
        ]
        res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "authorization_details": json.dumps(tampered_rar),
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "invalid_authorization_details"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rar_strict_validation_rejects_invalid_values() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        invalid_rar = [
            {
                "type": "payment_initiation",
                "actions": ["initiate"],
                "instructedAmount": {"currency": "EUROPE", "amount": "invalid_number"},
                "creditorAccount": {"iban": "DE02100100109307118603"},
            }
        ]
        res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "authorization_details": json.dumps(invalid_rar),
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "invalid_authorization_details"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorize_rejects_direct_call_without_par() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        res = await client.get(
            "/as/authorize",
            params={
                "client_id": "confidential_fintech_app",
                "response_type": "code",
            },
        )
        assert res.status_code == 400
        assert res.json()["error"] == "invalid_request"
        assert "pushed authorization request is required" in res.json()["error_description"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorize_rejects_swapped_client_id() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        par_res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
            },
        )
        req_uri = par_res.json()["request_uri"]

        swap_res = await client.get(
            "/as/authorize",
            params={"client_id": "attacker_client", "request_uri": req_uri},
        )
        assert swap_res.status_code == 400
        assert swap_res.json()["error"] == "invalid_request"
        assert "different client" in swap_res.json()["error_description"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorize_rejects_replayed_request_uri() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        par_res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
            },
        )
        req_uri = par_res.json()["request_uri"]

        first_res = await client.get(
            "/as/authorize",
            params={"client_id": "confidential_fintech_app", "request_uri": req_uri},
            follow_redirects=False,
        )
        assert first_res.status_code == 302
        assert "code=" in first_res.headers["location"]

        replay_res = await client.get(
            "/as/authorize",
            params={"client_id": "confidential_fintech_app", "request_uri": req_uri},
        )
        assert replay_res.status_code == 400
        assert replay_res.json()["error"] == "invalid_request"
        assert "already been used" in replay_res.json()["error_description"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authorize_rejects_expired_request_uri() -> None:
    app, store = make_test_context()
    async with client_for(app) as client:
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        par_res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
            },
        )
        req_uri = par_res.json()["request_uri"]

        record = store.get_pushed_request(req_uri)
        assert record is not None
        record.expires_at = time.time() - 10

        exp_res = await client.get(
            "/as/authorize",
            params={"client_id": "confidential_fintech_app", "request_uri": req_uri},
        )
        assert exp_res.status_code == 400
        assert exp_res.json()["error"] == "invalid_request"
        assert "expired" in exp_res.json()["error_description"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_end_to_end_par_rar_token_exchange_and_rs_enforcement() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        verifier, challenge = generate_pkce_pair()
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        rar_grant = [
            {
                "type": "payment_initiation",
                "actions": ["initiate", "status"],
                "locations": ["https://api.example.com/payments"],
                "instructedAmount": {"currency": "EUR", "amount": "500.00"},
                "creditorAccount": {"iban": "DE02100100109307118603"},
            }
        ]

        # Step 1: Push PAR
        par_res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "state": "state_fintech_42",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "authorization_details": json.dumps(rar_grant),
            },
        )
        assert par_res.status_code == 201
        req_uri = par_res.json()["request_uri"]

        # Step 2: Front-channel authorize
        auth_res = await client.get(
            "/as/authorize",
            params={"client_id": "confidential_fintech_app", "request_uri": req_uri},
            follow_redirects=False,
        )
        assert auth_res.status_code == 302
        location = auth_res.headers["location"]
        code = location.split("code=")[1].split("&")[0]

        # Step 3: Token exchange
        tok_res = await client.post(
            "/as/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://client.example.org/callback",
                "client_id": "confidential_fintech_app",
                "client_secret": "secure_secret_value_12345",
                "code_verifier": verifier,
            },
        )
        assert tok_res.status_code == 200
        tok_data = tok_res.json()
        assert "access_token" in tok_data
        assert tok_data["authorization_details"] == rar_grant

        # Verify JWT payload
        jwt_claims = jwt.decode(
            tok_data["access_token"],
            JWT_SECRET,
            algorithms=["HS256"],
            audience="https://api.example.com",
        )
        assert jwt_claims["authorization_details"] == rar_grant

        # Step 4: Call Resource Server with conforming payment (250 EUR <= 500 EUR limit)
        bearer = f"Bearer {tok_data['access_token']}"
        rs_valid = await client.post(
            "/rs/payments",
            headers={"Authorization": bearer},
            json={
                "amount": "250.00",
                "currency": "EUR",
                "creditor_iban": "DE02100100109307118603",
            },
        )
        assert rs_valid.status_code == 201
        assert rs_valid.json()["status"] == "initiated"

        # Step 5: Call Resource Server exceeding granted amount (750 EUR > 500 EUR limit)
        rs_excess = await client.post(
            "/rs/payments",
            headers={"Authorization": bearer},
            json={
                "amount": "750.00",
                "currency": "EUR",
                "creditor_iban": "DE02100100109307118603",
            },
        )
        assert rs_excess.status_code == 403
        assert rs_excess.json()["error"] == "insufficient_authorization_details"

        # Step 6: Call Resource Server with mismatched creditor IBAN
        rs_wrong_creditor = await client.post(
            "/rs/payments",
            headers={"Authorization": bearer},
            json={
                "amount": "100.00",
                "currency": "EUR",
                "creditor_iban": "DE99999999999999999999",
            },
        )
        assert rs_wrong_creditor.status_code == 403
        assert rs_wrong_creditor.json()["error"] == "insufficient_authorization_details"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_downscoping_success_and_failure() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        verifier, challenge = generate_pkce_pair()
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        initial_rar = [
            {
                "type": "account_information",
                "actions": ["list_accounts", "read_balances"],
                "locations": ["https://api.example.com/accounts"],
                "accounts": ["DE1111", "DE2222"],
            }
        ]

        par_res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "authorization_details": json.dumps(initial_rar),
            },
        )
        req_uri = par_res.json()["request_uri"]

        auth_res = await client.get(
            "/as/authorize",
            params={"client_id": "confidential_fintech_app", "request_uri": req_uri},
            follow_redirects=False,
        )
        code = auth_res.headers["location"].split("code=")[1].split("&")[0]

        # Valid downscope: request only 1 account and 1 action
        downscoped_rar = [
            {
                "type": "account_information",
                "actions": ["list_accounts"],
                "locations": ["https://api.example.com/accounts"],
                "accounts": ["DE1111"],
            }
        ]
        tok_res = await client.post(
            "/as/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://client.example.org/callback",
                "client_id": "confidential_fintech_app",
                "client_secret": "secure_secret_value_12345",
                "code_verifier": verifier,
                "authorization_details": json.dumps(downscoped_rar),
            },
        )
        assert tok_res.status_code == 200
        assert tok_res.json()["authorization_details"] == downscoped_rar


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_downscoping_rejects_privilege_escalation() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        verifier, challenge = generate_pkce_pair()
        auth_header = "Basic " + base64.b64encode(
            b"confidential_fintech_app:secure_secret_value_12345"
        ).decode("ascii")
        initial_rar = [
            {
                "type": "account_information",
                "actions": ["list_accounts"],
                "locations": ["https://api.example.com/accounts"],
                "accounts": ["DE1111"],
            }
        ]

        par_res = await client.post(
            "/as/par",
            headers={"Authorization": auth_header},
            data={
                "response_type": "code",
                "client_id": "confidential_fintech_app",
                "redirect_uri": "https://client.example.org/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "authorization_details": json.dumps(initial_rar),
            },
        )
        req_uri = par_res.json()["request_uri"]

        auth_res = await client.get(
            "/as/authorize",
            params={"client_id": "confidential_fintech_app", "request_uri": req_uri},
            follow_redirects=False,
        )
        code = auth_res.headers["location"].split("code=")[1].split("&")[0]

        # Escalation attempt: requesting unauthorized action and ungranted account
        escalated_rar = [
            {
                "type": "account_information",
                "actions": ["list_accounts", "write_transfer"],
                "locations": ["https://api.example.com/accounts"],
                "accounts": ["DE1111", "DE9999"],
            }
        ]
        tok_res = await client.post(
            "/as/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://client.example.org/callback",
                "client_id": "confidential_fintech_app",
                "client_secret": "secure_secret_value_12345",
                "code_verifier": verifier,
                "authorization_details": json.dumps(escalated_rar),
            },
        )
        assert tok_res.status_code == 400
        assert tok_res.json()["error"] == "invalid_authorization_details"


async def _payment_code(client: httpx.AsyncClient, amount: str) -> tuple[str, str]:
    """Run PAR and authorize for one payment_initiation grant; return (code, verifier)."""
    verifier, challenge = generate_pkce_pair()
    auth_header = "Basic " + base64.b64encode(
        b"confidential_fintech_app:secure_secret_value_12345"
    ).decode("ascii")
    granted = [
        {
            "type": "payment_initiation",
            "actions": ["initiate", "status"],
            "locations": ["https://api.example.com/payments"],
            "instructedAmount": {"currency": "EUR", "amount": amount},
            "creditorAccount": {"iban": "DE02100100109307118603"},
        }
    ]
    par_res = await client.post(
        "/as/par",
        headers={"Authorization": auth_header},
        data={
            "response_type": "code",
            "client_id": "confidential_fintech_app",
            "redirect_uri": "https://client.example.org/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "authorization_details": json.dumps(granted),
        },
    )
    assert par_res.status_code == 201
    auth_res = await client.get(
        "/as/authorize",
        params={
            "client_id": "confidential_fintech_app",
            "request_uri": par_res.json()["request_uri"],
        },
        follow_redirects=False,
    )
    assert auth_res.status_code == 302
    return auth_res.headers["location"].split("code=")[1].split("&")[0], verifier


async def _token_with_amount(
    client: httpx.AsyncClient, code: str, verifier: str, amount: str
) -> httpx.Response:
    requested = [
        {
            "type": "payment_initiation",
            "actions": ["initiate"],
            "instructedAmount": {"currency": "EUR", "amount": amount},
            "creditorAccount": {"iban": "DE02100100109307118603"},
        }
    ]
    return await client.post(
        "/as/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example.org/callback",
            "client_id": "confidential_fintech_app",
            "client_secret": "secure_secret_value_12345",
            "code_verifier": verifier,
            "authorization_details": json.dumps(requested),
        },
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_downscoping_enforces_payment_amount_ceiling() -> None:
    app, _ = make_test_context()
    async with client_for(app) as client:
        code, verifier = await _payment_code(client, "200.00")
        over = await _token_with_amount(client, code, verifier, "300.00")
        assert over.status_code == 400
        assert over.json()["error"] == "invalid_authorization_details"
        assert over.json()["error_description"] == "requested details exceed granted rights"

        code, verifier = await _payment_code(client, "200.00")
        under = await _token_with_amount(client, code, verifier, "150.00")
        assert under.status_code == 200
        assert under.json()["authorization_details"][0]["instructedAmount"]["amount"] == "150.00"
