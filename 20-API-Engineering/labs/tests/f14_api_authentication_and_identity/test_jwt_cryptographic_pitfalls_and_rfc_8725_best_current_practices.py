"""Unit tests for JWT cryptographic pitfalls and RFC 8725 best current practices.

Covers alg: none bypass, RS256-to-HS256 key confusion, unvalidated kid path traversal,
jku/x5u SSRF, unverified claim decoding, and explicit typing defenses across PyJWT 2.15.1
and joserfc 1.8.0.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc.errors import InvalidKeyTypeError, UnsupportedAlgorithmError
from joserfc.jwk import RSAKey

from api_labs.f14_api_authentication_and_identity.jwt_cryptographic_pitfalls import (
    RFC8725HardenedVerifier,
    VulnerableJwtVerifier,
    b64url_encode,
    forge_alg_none_token,
    forge_jku_token,
    forge_kid_traversal_token,
    forge_rs256_to_hs256_token,
)

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


@pytest.fixture
def rsa_key_pair() -> tuple[RSAPrivateKey, bytes, bytes]:
    """Generate ephemeral RSA-2048 private and public key in PEM format."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, private_pem, public_pem


@pytest.fixture
def key_store_dir(tmp_path: Path) -> Path:
    """Create a temporary key store directory with a legitimate key."""
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    legit_key_file = keys_dir / "key-2026-prod"
    legit_key_file.write_bytes(b"super-secret-hmac-key-material-at-least-32-bytes!")
    return keys_dir


@pytest.mark.unit
def test_alg_none_vulnerable_bypass_and_rfc8725_block(
    rsa_key_pair: tuple[RSAPrivateKey, bytes, bytes],
) -> None:
    """Prove that alg: none bypasses unhardened verifiers but is blocked by RFC 8725."""
    _, _, public_pem = rsa_key_pair
    claims = {"sub": "admin", "role": "superuser", "exp": int(time.time()) + 3600}
    forged_token = forge_alg_none_token(claims)

    # 1. Vulnerable verifier without algorithm enforcement accepts forged token
    vuln_verifier = VulnerableJwtVerifier(rsa_public_pem=public_pem)
    payload = vuln_verifier.verify_dynamic_algorithm(forged_token)
    assert payload["sub"] == "admin"
    assert payload["role"] == "superuser"

    # 2. Hardened RFC 8725 verifier rejects alg: none
    hardened = RFC8725HardenedVerifier(
        allowed_algorithms=["RS256"],
        expected_type="JWT",
    )
    with pytest.raises(ValueError, match=r"algorithm 'none' not in allowlist"):
        hardened.verify_with_pyjwt(forged_token, verification_key=public_pem)

    # 3. Direct PyJWT 2.15.1 enforcement: fails with InvalidAlgorithmError
    with pytest.raises(
        jwt.exceptions.InvalidAlgorithmError,
        match=r"The specified alg value is not allowed",
    ):
        jwt.decode(forged_token, key=public_pem, algorithms=["RS256"])

    # 4. Direct joserfc 1.8.0 enforcement: fails with UnsupportedAlgorithmError
    with pytest.raises(
        UnsupportedAlgorithmError,
        match=r"Algorithm of 'none' is not allowed",
    ):
        hardened.verify_with_joserfc(forged_token, verification_key=RSAKey.import_key(public_pem))


@pytest.mark.unit
def test_rs256_to_hs256_key_confusion_and_defenses(
    rsa_key_pair: tuple[RSAPrivateKey, bytes, bytes],
) -> None:
    """Prove RS256-to-HS256 key confusion attack and verify RFC 8725 library defenses."""
    _, _, public_pem = rsa_key_pair
    claims = {"sub": "alice", "admin": True, "exp": int(time.time()) + 3600}
    forged_token = forge_rs256_to_hs256_token(claims, rsa_public_key_pem=public_pem)

    # 1. Vulnerable verifier accepting both or dynamic alg falls for key confusion
    vuln_verifier = VulnerableJwtVerifier(rsa_public_pem=public_pem)
    decoded = vuln_verifier.verify_dynamic_algorithm(forged_token)
    assert decoded["sub"] == "alice"
    assert decoded["admin"] is True

    # 2. Hardened verifier blocks because HS256 is not in allowed_algorithms
    hardened = RFC8725HardenedVerifier(
        allowed_algorithms=["RS256"],
        expected_type="JWT",
    )
    with pytest.raises(ValueError, match=r"algorithm 'HS256' not in allowlist"):
        hardened.verify_with_pyjwt(forged_token, verification_key=public_pem)

    # 3. PyJWT 2.15.1 defense: even if misconfigured with both algorithms,
    # HMACAlgorithm.prepare_key rejects asymmetric PEM bytes as HMAC secret
    with pytest.raises(
        jwt.exceptions.InvalidKeyError,
        match=r"The specified key is an asymmetric key or x509 certificate",
    ):
        jwt.decode(forged_token, key=public_pem, algorithms=["RS256", "HS256"])

    # 4. joserfc 1.8.0 defense Layer 1: algorithm allowlist blocks HS256
    rsa_key_obj = RSAKey.import_key(public_pem)
    with pytest.raises(
        UnsupportedAlgorithmError,
        match=r"Algorithm of 'HS256' is not allowed",
    ):
        hardened.verify_with_joserfc(forged_token, verification_key=rsa_key_obj)

    # 5. joserfc 1.8.0 defense Layer 2: even if misconfigured with both algorithms,
    # check_key enforces key.key_type == self.key_type ('oct' vs 'RSA')
    permissive_hardened = RFC8725HardenedVerifier(
        allowed_algorithms=["RS256", "HS256"],
        expected_type="JWT",
    )
    with pytest.raises(
        InvalidKeyTypeError,
        match=r"invalid_key_type: Algorithm 'HS256' requires 'oct' key",
    ):
        permissive_hardened.verify_with_joserfc(forged_token, verification_key=rsa_key_obj)


@pytest.mark.unit
def test_kid_path_traversal_exploit_and_safe_resolution(
    key_store_dir: Path,
    tmp_path: Path,
) -> None:
    """Prove kid path traversal exploit against naive resolvers and verify RFC 8725 path defense."""
    # Create an external arbitrary file that an attacker targets via path traversal
    external_secret_file = tmp_path / "known_system_file.txt"
    known_secret = b"world-readable-public-system-entropy"
    external_secret_file.write_bytes(known_secret)

    # Relative path from keys_dir to external_secret_file
    traversal_kid = "../known_system_file.txt"
    claims = {"sub": "root", "role": "admin"}
    forged_token = forge_kid_traversal_token(claims, traversal_kid, known_secret)

    # 1. Vulnerable path resolver escapes key store directory
    vuln_verifier = VulnerableJwtVerifier(key_store_dir=key_store_dir)
    resolved_secret = vuln_verifier.resolve_key_path_vulnerable(traversal_kid)
    assert resolved_secret == known_secret

    # Verify that the signature succeeds with this resolved secret
    header = jwt.get_unverified_header(forged_token)
    assert header["kid"] == traversal_kid
    verified_payload = jwt.decode(
        forged_token,
        key=resolved_secret,
        algorithms=["HS256"],
        options={"verify_signature": True, "verify_exp": False},
    )
    assert verified_payload["sub"] == "root"

    # 2. Hardened RFC 8725 resolver rejects traversal via regex and containment
    hardened = RFC8725HardenedVerifier(
        allowed_algorithms=["HS256"],
        key_store_dir=key_store_dir,
    )
    with pytest.raises(ValueError, match=r"RFC 8725 Section 3.10: invalid kid format"):
        hardened.safe_resolve_kid_path(traversal_kid)

    with pytest.raises(ValueError, match=r"RFC 8725 Section 3.10: invalid kid format"):
        hardened.safe_resolve_kid_path("../../etc/passwd")

    # A regex-valid kid can still escape through a symlink planted in the key store;
    # only the resolve() + is_relative_to() containment check catches this one
    (key_store_dir / "planted-link").symlink_to(external_secret_file)
    with pytest.raises(ValueError, match=r"RFC 8725 Section 3.10: kid path traversal detected"):
        hardened.safe_resolve_kid_path("planted-link")

    # Legitimate kid resolves properly
    legit_path = hardened.safe_resolve_kid_path("key-2026-prod")
    assert legit_path.name == "key-2026-prod"
    assert legit_path.is_file()


@pytest.mark.unit
def test_jku_ssrf_exploit_and_domain_allowlist_defense(
    rsa_key_pair: tuple[RSAPrivateKey, bytes, bytes],
) -> None:
    """Prove jku SSRF exploit vector and verify RFC 8725 domain allowlist and IP block defenses."""
    _, private_pem, _ = rsa_key_pair
    claims = {"sub": "attacker", "exp": int(time.time()) + 3600}
    ssrf_target = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    token = forge_jku_token(claims, ssrf_target, private_pem)

    header = jwt.get_unverified_header(token)
    assert header["jku"] == ssrf_target

    hardened = RFC8725HardenedVerifier(
        allowed_algorithms=["RS256"],
        allowed_jku_domains=["auth.example.com", "idp.internal.net"],
    )

    # 1. Non-HTTPS scheme blocked
    with pytest.raises(ValueError, match=r"RFC 8725 Section 3.10: jku must use https scheme"):
        hardened.safe_validate_jku(ssrf_target)

    # 2. Link-local cloud metadata IP blocked even over HTTPS
    with pytest.raises(ValueError, match=r"RFC 8725 Section 3.10: jku SSRF target blocked"):
        hardened.safe_validate_jku("https://169.254.169.254/jwks.json")

    # 3. Loopback, RFC 1918 172.16.0.0/12, and IPv6 loopback literals blocked
    for blocked in (
        "https://127.0.0.1/jwks.json",
        "https://172.16.0.5/jwks.json",
        "https://[::1]/jwks.json",
    ):
        with pytest.raises(ValueError, match=r"RFC 8725 Section 3.10: jku SSRF target blocked"):
            hardened.safe_validate_jku(blocked)

    # 4. Untrusted public domain blocked
    with pytest.raises(ValueError, match=r"not in allowlist"):
        hardened.safe_validate_jku("https://evil-attacker.com/jwks.json")

    # 5. Allowlisted trusted domain accepted
    trusted_url = "https://auth.example.com/.well-known/jwks.json"
    validated = hardened.safe_validate_jku(trusted_url)
    assert validated == trusted_url


@pytest.mark.unit
def test_explicit_typing_and_cross_jwt_confusion_rfc8725(
    rsa_key_pair: tuple[RSAPrivateKey, bytes, bytes],
) -> None:
    """Prove RFC 8725 Section 3.11 explicit typing defense against cross-JWT confusion."""
    _, private_pem, public_pem = rsa_key_pair
    now = int(time.time())
    claims = {
        "sub": "user_42",
        "iss": "https://auth.example.com",
        "aud": "https://api.example.com",
        "exp": now + 3600,
    }

    # Access token properly typed per RFC 9068
    access_token = jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"typ": "at+jwt", "kid": "key-1"},
    )

    # ID token with standard typ: JWT
    id_token = jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"typ": "JWT", "kid": "key-1"},
    )

    # Security event token with typ: secevent+jwt
    set_token = jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"typ": "secevent+jwt", "kid": "key-1"},
    )

    # Hardened API verifier expecting OAuth 2.0 access token (typ: at+jwt)
    verifier = RFC8725HardenedVerifier(
        allowed_algorithms=["RS256"],
        allowed_issuers=["https://auth.example.com"],
        allowed_audiences=["https://api.example.com"],
        expected_type="at+jwt",
    )

    # Access token passes verification
    decoded = verifier.verify_with_pyjwt(access_token, verification_key=public_pem)
    assert decoded["sub"] == "user_42"

    # ID token rejected due to mismatched type
    with pytest.raises(
        ValueError,
        match=r"token type 'JWT' does not match expected 'at\+jwt'",
    ):
        verifier.verify_with_pyjwt(id_token, verification_key=public_pem)

    # SET token rejected due to mismatched type
    with pytest.raises(
        ValueError,
        match=r"token type 'secevent\+jwt' does not match expected 'at\+jwt'",
    ):
        verifier.verify_with_pyjwt(set_token, verification_key=public_pem)


@pytest.mark.unit
def test_unverified_claim_decoding_pitfall(
    rsa_key_pair: tuple[RSAPrivateKey, bytes, bytes],
) -> None:
    """Prove danger of trusting claims prior to signature verification (RFC 8725 Section 3.10)."""
    _, private_pem, public_pem = rsa_key_pair
    claims = {"sub": "legit_user", "tenant_id": "tenant_a", "exp": int(time.time()) + 3600}
    token = jwt.encode(claims, private_pem, algorithm="RS256")

    # Attacker modifies unverified payload in transit to change tenant_id
    parts = token.split(".")
    tampered_claims = {"sub": "legit_user", "tenant_id": "tenant_b_attacker", "exp": claims["exp"]}
    tampered_part1 = b64url_encode(str(tampered_claims).replace("'", '"').encode("utf-8"))
    tampered_token = f"{parts[0]}.{tampered_part1}.{parts[2]}"

    # Reading claims without verification reveals tampered tenant
    unverified = jwt.decode(tampered_token, options={"verify_signature": False})
    assert unverified["tenant_id"] == "tenant_b_attacker"

    # Verifying signature immediately catches the forgery
    with pytest.raises(
        jwt.exceptions.InvalidSignatureError,
        match=r"Signature verification failed",
    ):
        jwt.decode(tampered_token, key=public_pem, algorithms=["RS256"])
