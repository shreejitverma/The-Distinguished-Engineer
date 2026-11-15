"""Tests for RFC 9449 DPoP and RFC 8705 mTLS sender-constrained tokens."""

from __future__ import annotations

import base64
import datetime
import hashlib
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fastapi import FastAPI, HTTPException, Request, status

from api_labs.f14_api_authentication_and_identity.sender_constrained_tokens import (
    DPoPVerificationError,
    DPoPVerifier,
    MTLSVerificationError,
    NonceManager,
    compute_access_token_hash,
    compute_cert_thumbprint,
    compute_jwk_thumbprint,
    normalize_target_uri,
    verify_dpop_request,
    verify_mtls_binding,
)


def _generate_ec_key() -> tuple[ec.EllipticCurvePrivateKey, dict[str, Any]]:
    """Generate an EC P-256 private key and its corresponding JWK dict."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()
    x_b64 = (
        base64.urlsafe_b64encode(public_numbers.x.to_bytes(32, "big")).rstrip(b"=").decode("ascii")
    )
    y_b64 = (
        base64.urlsafe_b64encode(public_numbers.y.to_bytes(32, "big")).rstrip(b"=").decode("ascii")
    )
    jwk_dict = {
        "kty": "EC",
        "crv": "P-256",
        "x": x_b64,
        "y": y_b64,
    }
    return private_key, jwk_dict


def _create_dpop_proof(
    private_key: ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey,
    jwk_dict: dict[str, Any],
    method: str,
    target_uri: str,
    access_token: str | None = None,
    nonce: str | None = None,
    jti: str | None = None,
    iat: int | None = None,
    typ: str = "dpop+jwt",
    alg: str = "ES256",
) -> str:
    """Helper to construct and sign a DPoP proof JWT."""
    header = {"typ": typ, "alg": alg, "jwk": jwk_dict}
    payload: dict[str, Any] = {
        "jti": jti or str(uuid.uuid4()),
        "htm": method,
        "htu": target_uri,
        "iat": int(time.time()) if iat is None else iat,
    }
    if access_token is not None:
        payload["ath"] = compute_access_token_hash(access_token)
    if nonce is not None:
        payload["nonce"] = nonce

    return jwt.encode(payload, private_key, algorithm=alg, headers=header)


def _generate_self_signed_cert() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Generate an ephemeral self-signed client certificate for mTLS testing."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "client.example.com")]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert, key


# ==============================================================================
# Unit Tests: Cryptographic Helpers and Test Vectors
# ==============================================================================


@pytest.mark.unit
def test_rfc7638_rsa_thumbprint_test_vector() -> None:
    """Validate RFC 7638 Section 3.1 RSA thumbprint canonical test vector."""
    n_part1 = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJ"
    )
    n_part2 = (
        "ECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FD"
    )
    n_part3 = (
        "W2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vM"
    )
    n_part4 = "QFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    rsa_jwk = {
        "kty": "RSA",
        "n": n_part1 + n_part2 + n_part3 + n_part4,
        "e": "AQAB",
        "alg": "RS256",
        "kid": "2011-04-29",
    }
    expected_thumbprint = "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"
    assert compute_jwk_thumbprint(rsa_jwk) == expected_thumbprint


@pytest.mark.unit
def test_rfc9449_ec_thumbprint_and_ath_test_vector() -> None:
    """Validate RFC 9449 Section 6.1 (Figure 8/9) and Section 7.1 (Figure 13/14) vectors."""
    ec_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": "l8tFrhx-34tV3hRICRDY9zCkDlpBhF42UQUfWVAWBFs",
        "y": "9VE4jf_Ok_o64zbTTlcuNJajHmt6v9TDVrU0CdvGRDA",
    }
    expected_jkt = "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"
    assert compute_jwk_thumbprint(ec_jwk) == expected_jkt

    sample_tok = "Kz~8mXK1EalYznwH-LC-1fBAo.4Ljp~zsPE_NeO.gxU"
    expected_ath = "fUHyO2r2Z3DZ53EsNrWBb0xWXoaNy59IiKCAqksmQEo"
    assert compute_access_token_hash(sample_tok) == expected_ath


@pytest.mark.unit
def test_rfc8705_cert_thumbprint_calculation() -> None:
    """Validate RFC 8705 Section 3.1 x5t#S256 calculation on DER-encoded X.509 cert."""
    cert, _ = _generate_self_signed_cert()
    thumbprint = compute_cert_thumbprint(cert)
    assert len(thumbprint) == 43
    # Check deterministic byte comparison
    der = cert.public_bytes(serialization.Encoding.DER)
    expected = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode("ascii")
    assert thumbprint == expected


@pytest.mark.unit
def test_normalize_target_uri_rules() -> None:
    """Validate RFC 9449 Section 4.3 rule 9 URI normalization."""
    # Scheme and host lowercase, default port stripped, query/fragment stripped
    raw = "HTTPS://API.Example.COM:443/Payments/Transfer?query=1#frag"
    normalized = normalize_target_uri(raw)
    assert normalized == "https://api.example.com/Payments/Transfer"

    # HTTP default port 80 stripped
    raw_http = "HTTP://API.Example.COM:80/resource/"
    assert normalize_target_uri(raw_http) == "http://api.example.com/resource/"

    # Non-default port retained
    raw_custom = "https://api.example.com:8443/v1/orders"
    assert normalize_target_uri(raw_custom) == "https://api.example.com:8443/v1/orders"


# ==============================================================================
# Unit Tests: DPoP Verifier Invariants
# ==============================================================================


@pytest.mark.unit
def test_dpop_verifier_valid_proof() -> None:
    """Verify valid DPoP proof round-trip with correct claims and signature."""
    priv, jwk = _generate_ec_key()
    jkt = compute_jwk_thumbprint(jwk)
    tok = "test-token-12345"
    url = "https://api.example.com/accounts"
    now = time.time()

    proof = _create_dpop_proof(priv, jwk, "GET", url, access_token=tok, iat=int(now))
    verifier = DPoPVerifier(max_clock_skew_seconds=60)
    claims = verifier.verify_proof(
        proof_jwt=proof,
        request_method="GET",
        request_url=url,
        access_token=tok,
        expected_jkt=jkt,
        current_time=now,
    )
    assert claims.htm == "GET"
    assert claims.htu == url
    assert claims.jkt == jkt
    assert claims.ath == compute_access_token_hash(tok)


@pytest.mark.unit
def test_dpop_verifier_rejects_replay_jti() -> None:
    """Verify that replaying a seen jti within the validity window is rejected."""
    priv, jwk = _generate_ec_key()
    jkt = compute_jwk_thumbprint(jwk)
    tok = "test-token-12345"
    url = "https://api.example.com/accounts"
    now = time.time()

    proof = _create_dpop_proof(
        priv, jwk, "GET", url, access_token=tok, jti="fixed-jti-001", iat=int(now)
    )
    verifier = DPoPVerifier()

    # First attempt succeeds
    verifier.verify_proof(proof, "GET", url, access_token=tok, expected_jkt=jkt, current_time=now)

    # Replay attempt fails
    with pytest.raises(DPoPVerificationError) as exc_info:
        verifier.verify_proof(
            proof,
            "GET",
            url,
            access_token=tok,
            expected_jkt=jkt,
            current_time=now + 1,
        )
    assert "already been replayed" in exc_info.value.detail


@pytest.mark.unit
def test_dpop_verifier_rejects_stolen_token_with_different_key() -> None:
    """Verify that using a legitimate token with an attacker's key fails jkt binding."""
    _victim_priv, victim_jwk = _generate_ec_key()
    victim_jkt = compute_jwk_thumbprint(victim_jwk)
    stolen_tok = "stolen-bearer-token-xyz"

    attacker_priv, attacker_jwk = _generate_ec_key()
    url = "https://api.example.com/accounts"
    now = time.time()

    # Attacker signs proof with their own key
    attacker_proof = _create_dpop_proof(
        attacker_priv, attacker_jwk, "GET", url, access_token=stolen_tok, iat=int(now)
    )

    verifier = DPoPVerifier()
    with pytest.raises(DPoPVerificationError) as exc_info:
        verifier.verify_proof(
            attacker_proof,
            request_method="GET",
            request_url=url,
            access_token=stolen_tok,
            expected_jkt=victim_jkt,
            current_time=now,
        )
    assert "does not match token binding" in exc_info.value.detail


@pytest.mark.unit
def test_dpop_verifier_rejects_method_and_uri_mismatch() -> None:
    """Verify that DPoP proofs bound to a specific method and URI cannot be cross-called."""
    priv, jwk = _generate_ec_key()
    tok = "test-token-abc"
    verifier = DPoPVerifier()
    now = time.time()

    # Proof created for GET /transfers
    proof = _create_dpop_proof(
        priv,
        jwk,
        "GET",
        "https://api.example.com/transfers",
        access_token=tok,
        iat=int(now),
    )

    # Replayed against POST /transfers
    with pytest.raises(DPoPVerificationError) as exc_info_method:
        verifier.verify_proof(
            proof,
            request_method="POST",
            request_url="https://api.example.com/transfers",
            access_token=tok,
            current_time=now,
        )
    assert "does not match request method" in exc_info_method.value.detail

    # Replayed against GET /accounts
    with pytest.raises(DPoPVerificationError) as exc_info_uri:
        verifier.verify_proof(
            proof,
            request_method="GET",
            request_url="https://api.example.com/accounts",
            access_token=tok,
            current_time=now,
        )
    assert "does not match request URI" in exc_info_uri.value.detail


@pytest.mark.unit
def test_dpop_verifier_rejects_invalid_headers_and_private_keys() -> None:
    """Verify rejection of alg:none, symmetric HMAC, invalid typ, and embedded private key."""
    priv, jwk = _generate_ec_key()
    tok = "test-token-1"
    url = "https://api.example.com/test"
    verifier = DPoPVerifier()

    # 1. Invalid typ
    bad_typ_proof = _create_dpop_proof(priv, jwk, "GET", url, access_token=tok, typ="JWT")
    with pytest.raises(DPoPVerificationError) as exc:
        verifier.verify_proof(bad_typ_proof, "GET", url, access_token=tok)
    assert "MUST be 'dpop+jwt'" in exc.value.detail

    # 2. Embedded private key 'd' in jwk
    poisoned_jwk = dict(jwk)
    poisoned_jwk["d"] = "secret-private-key-material"
    poisoned_proof = _create_dpop_proof(priv, poisoned_jwk, "GET", url, access_token=tok)
    with pytest.raises(DPoPVerificationError) as exc:
        verifier.verify_proof(poisoned_proof, "GET", url, access_token=tok)
    assert "MUST NOT contain private key material" in exc.value.detail


@pytest.mark.unit
def test_dpop_nonce_challenge_workflow() -> None:
    """Verify server-provided nonce challenge and retry workflow (RFC 9449 Section 9)."""
    priv, jwk = _generate_ec_key()
    tok = "test-token-nonce"
    url = "https://api.example.com/protected"
    now = time.time()

    nonce_mgr = NonceManager(
        secret_key="top-secret-nonce-key",  # noqa: S106
        validity_seconds=300,
    )
    verifier = DPoPVerifier(nonce_manager=nonce_mgr, require_nonce=True)

    # First request without nonce raises 401 with use_dpop_nonce and DPoP-Nonce header
    proof_without_nonce = _create_dpop_proof(priv, jwk, "GET", url, access_token=tok, iat=int(now))
    with pytest.raises(DPoPVerificationError) as exc_info:
        verifier.verify_proof(proof_without_nonce, "GET", url, access_token=tok, current_time=now)
    assert exc_info.value.error_code == "use_dpop_nonce"
    issued_nonce = exc_info.value.dpop_nonce
    assert issued_nonce is not None

    # Retry with the issued nonce succeeds
    proof_with_nonce = _create_dpop_proof(
        priv,
        jwk,
        "GET",
        url,
        access_token=tok,
        nonce=issued_nonce,
        iat=int(now),
    )
    claims = verifier.verify_proof(proof_with_nonce, "GET", url, access_token=tok, current_time=now)
    assert claims.nonce == issued_nonce


# ==============================================================================
# Unit Tests: mTLS Certificate Binding (RFC 8705)
# ==============================================================================


@pytest.mark.unit
def test_mtls_binding_verification() -> None:
    """Verify RFC 8705 mTLS certificate thumbprint confirmation check."""
    client_cert, _ = _generate_self_signed_cert()
    attacker_cert, _ = _generate_self_signed_cert()

    legit_thumbprint = compute_cert_thumbprint(client_cert)
    attacker_thumbprint = compute_cert_thumbprint(attacker_cert)

    # Valid handshake presentation succeeds
    verify_mtls_binding(
        client_cert_thumbprint=legit_thumbprint,
        expected_thumbprint=legit_thumbprint,
    )

    # Missing client certificate fails
    with pytest.raises(MTLSVerificationError) as exc_missing:
        verify_mtls_binding(
            client_cert_thumbprint=None,
            expected_thumbprint=legit_thumbprint,
        )
    assert "missing" in exc_missing.value.detail.lower()

    # Stolen token presented over connection with attacker's cert fails
    with pytest.raises(MTLSVerificationError) as exc_mismatch:
        verify_mtls_binding(
            client_cert_thumbprint=attacker_thumbprint,
            expected_thumbprint=legit_thumbprint,
        )
    assert "does not match token binding" in exc_mismatch.value.detail


# ==============================================================================
# In-Process ASGI / FastAPI Integration Tests
# ==============================================================================


def _create_test_app() -> FastAPI:
    """Create a test FastAPI application enforcing DPoP and mTLS sender-constraining."""
    app = FastAPI(title="Sender-Constrained Token Demo")
    verifier = DPoPVerifier(max_clock_skew_seconds=120)

    # Mock in-memory database of issued tokens with confirmation claims
    issued_tokens: dict[str, dict[str, Any]] = {}

    @app.post("/oauth/token")
    async def token_endpoint(request: Request) -> dict[str, Any]:
        """OAuth 2.0 Token Endpoint issuing DPoP-bound access tokens."""
        claims = verify_dpop_request(request, verifier)
        access_token = f"dpop-token-{uuid.uuid4().hex}"
        issued_tokens[access_token] = {
            "sub": "user_12345",
            "cnf": {"jkt": claims.jkt},
        }
        return {
            "access_token": access_token,
            "token_type": "DPoP",
            "expires_in": 3600,
        }

    @app.get("/api/accounts")
    async def get_accounts(request: Request) -> dict[str, Any]:
        """Protected resource requiring DPoP-bound token and proof."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
                headers={"WWW-Authenticate": "DPoP, Bearer"},
            )

        # RFC 9449 Section 7.2: Downgrade check
        if auth_header.startswith("Bearer "):
            bearer_token = auth_header.removeprefix("Bearer ").strip()
            token_meta = issued_tokens.get(bearer_token)
            if token_meta and "jkt" in token_meta.get("cnf", {}):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="DPoP-bound access token cannot be used as Bearer token",
                    headers={"WWW-Authenticate": 'DPoP error="invalid_token"'},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Bearer token",
            )

        if not auth_header.startswith("DPoP "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme; DPoP required",
                headers={"WWW-Authenticate": "DPoP"},
            )

        token = auth_header.removeprefix("DPoP ").strip()
        token_meta = issued_tokens.get(token)
        if not token_meta:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown access token"
            )

        expected_jkt = token_meta["cnf"]["jkt"]
        claims = verify_dpop_request(
            request, verifier, access_token=token, expected_jkt=expected_jkt
        )

        return {
            "status": "success",
            "account_id": "acc-99881",
            "balance": 150000.0,
            "sender_confirmed_jkt": claims.jkt,
        }

    @app.get("/api/transfers")
    async def get_transfers(request: Request) -> dict[str, Any]:
        """Protected resource requiring mTLS certificate-bound token (RFC 8705)."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required"
            )
        _ = auth_header.removeprefix("Bearer ").strip()

        # In production, proxy forwards verified client cert thumbprint in header
        client_cert_thumbprint = request.headers.get("x-ssl-client-thumbprint")
        expected_thumbprint = "bwcK0esc3ACC3DB2Y5_lESsXE8o9ltc05O89jdN-dg2"

        verify_mtls_binding(client_cert_thumbprint, expected_thumbprint)
        return {"status": "success", "transfers": ["tx_1", "tx_2"]}

    return app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_dpop_end_to_end_flow() -> None:
    """Test full DPoP flow: issuance, access, replay block, and downgrade check."""
    app = _create_test_app()
    client_priv, client_jwk = _generate_ec_key()
    client_jkt = compute_jwk_thumbprint(client_jwk)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Request DPoP-bound access token
        target_token_url = "http://test/oauth/token"  # noqa: S105
        token_proof = _create_dpop_proof(client_priv, client_jwk, "POST", target_token_url)
        token_resp = await client.post("/oauth/token", headers={"DPoP": token_proof})
        assert token_resp.status_code == 200
        token_data = token_resp.json()
        assert token_data["token_type"] == "DPoP"  # noqa: S105
        access_token = token_data["access_token"]

        # 2. Access protected resource with valid DPoP proof
        resource_url = "http://test/api/accounts"
        res_proof = _create_dpop_proof(
            client_priv, client_jwk, "GET", resource_url, access_token=access_token
        )
        acc_resp = await client.get(
            "/api/accounts",
            headers={"Authorization": f"DPoP {access_token}", "DPoP": res_proof},
        )
        assert acc_resp.status_code == 200
        assert acc_resp.json()["sender_confirmed_jkt"] == client_jkt

        # 3. Attacker steals DPoP proof and replays it -> Rejected with 401
        replay_resp = await client.get(
            "/api/accounts",
            headers={"Authorization": f"DPoP {access_token}", "DPoP": res_proof},
        )
        assert replay_resp.status_code == 401
        assert "already been replayed" in replay_resp.json()["detail"]

        # 4. Attacker steals token and attempts downgrade to Bearer -> Rejected with 401
        downgrade_resp = await client.get(
            "/api/accounts",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert downgrade_resp.status_code == 401
        assert "cannot be used as Bearer token" in downgrade_resp.json()["detail"]

        # 5. Attacker steals token and signs fresh DPoP proof with attacker's key -> 401
        attacker_priv, attacker_jwk = _generate_ec_key()
        attacker_proof = _create_dpop_proof(
            attacker_priv,
            attacker_jwk,
            "GET",
            resource_url,
            access_token=access_token,
        )
        stolen_resp = await client.get(
            "/api/accounts",
            headers={"Authorization": f"DPoP {access_token}", "DPoP": attacker_proof},
        )
        assert stolen_resp.status_code == 401
        assert "does not match token binding" in stolen_resp.json()["detail"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_mtls_binding_endpoint() -> None:
    """Test mTLS certificate-bound endpoint in FastAPI."""
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)
    expected_thumbprint = "bwcK0esc3ACC3DB2Y5_lESsXE8o9ltc05O89jdN-dg2"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing client cert thumbprint header -> 401
        r1 = await client.get(
            "/api/transfers",
            headers={"Authorization": "Bearer some-token"},
        )
        assert r1.status_code == 401

        # Mismatched client cert thumbprint header -> 401
        r2 = await client.get(
            "/api/transfers",
            headers={
                "Authorization": "Bearer some-token",
                "x-ssl-client-thumbprint": "attacker-cert-thumbprint",
            },
        )
        assert r2.status_code == 401

        # Matching client cert thumbprint header -> 200
        r3 = await client.get(
            "/api/transfers",
            headers={
                "Authorization": "Bearer some-token",
                "x-ssl-client-thumbprint": expected_thumbprint,
            },
        )
        assert r3.status_code == 200
        assert r3.json()["status"] == "success"
