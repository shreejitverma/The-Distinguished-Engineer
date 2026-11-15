"""Sender-constrained tokens: DPoP (RFC 9449) and mTLS binding (RFC 8705).

Provides cryptographic proof-of-possession verifiers, JWK thumbprint (RFC 7638)
and certificate thumbprint calculations, URI normalizers, and replay protection
for FastAPI and Starlette applications.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException, Request, status

ALLOWED_DPOP_ALGORITHMS: frozenset[str] = frozenset(
    {"ES256", "ES384", "ES512", "EdDSA", "PS256", "PS384", "PS512", "RS256"}
)

JWK_PRIVATE_KEY_FIELDS: frozenset[str] = frozenset({"d", "p", "q", "dp", "dq", "qi"})


class DPoPVerificationError(HTTPException):
    """Exception raised when DPoP proof validation fails (RFC 9449 Section 4.3)."""

    def __init__(
        self,
        detail: str,
        error_code: str = "invalid_dpop_proof",
        status_code: int = status.HTTP_401_UNAUTHORIZED,
        dpop_nonce: str | None = None,
    ) -> None:
        headers: dict[str, str] = {
            "WWW-Authenticate": f'DPoP error="{error_code}", error_description="{detail}"'
        }
        if dpop_nonce:
            headers["DPoP-Nonce"] = dpop_nonce
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
        self.dpop_nonce = dpop_nonce


class MTLSVerificationError(HTTPException):
    """Exception raised when mTLS certificate binding fails (RFC 8705 Section 3)."""

    def __init__(
        self,
        detail: str,
        error_code: str = "invalid_token",
        status_code: int = status.HTTP_401_UNAUTHORIZED,
    ) -> None:
        headers: dict[str, str] = {
            "WWW-Authenticate": f'Bearer error="{error_code}", error_description="{detail}"'
        }
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code


@dataclass(frozen=True)
class DPoPProofClaims:
    """Validated claims and metadata from an RFC 9449 DPoP proof."""

    jti: str
    htm: str
    htu: str
    iat: int
    ath: str | None
    nonce: str | None
    jwk: dict[str, Any]
    jkt: str


def compute_jwk_thumbprint(jwk_dict: dict[str, Any]) -> str:
    """Compute the SHA-256 JWK Thumbprint per RFC 7638 Section 3.

    Lexicographically orders required members and hashes canonical UTF-8 JSON.
    """
    kty = jwk_dict.get("kty")
    if kty == "EC":
        # RFC 7638 Section 3.2: crv, kty, x, y
        canonical = {
            "crv": jwk_dict["crv"],
            "kty": "EC",
            "x": jwk_dict["x"],
            "y": jwk_dict["y"],
        }
    elif kty == "RSA":
        # RFC 7638 Section 3.2: e, kty, n
        canonical = {
            "e": jwk_dict["e"],
            "kty": "RSA",
            "n": jwk_dict["n"],
        }
    elif kty == "OKP":
        # RFC 8037 Section 2: crv, kty, x
        canonical = {
            "crv": jwk_dict["crv"],
            "kty": "OKP",
            "x": jwk_dict["x"],
        }
    else:
        raise ValueError(f"Unsupported JWK key type (kty): {kty}")

    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical_json).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def compute_access_token_hash(access_token: str) -> str:
    """Compute the base64url-encoded SHA-256 hash of an access token (RFC 9449 Section 4.2)."""
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def compute_cert_thumbprint(cert: x509.Certificate | bytes) -> str:
    """Compute the base64url-encoded SHA-256 thumbprint of an X.509 cert (RFC 8705 Section 3.1)."""
    if isinstance(cert, x509.Certificate):
        der_bytes = cert.public_bytes(serialization.Encoding.DER)
    else:
        der_bytes = cert
    digest = hashlib.sha256(der_bytes).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def normalize_target_uri(target_url: str) -> str:
    """Normalize an HTTP target URI before the RFC 9449 Section 4.3 htu comparison.

    Applies a subset of RFC 3986 Sections 6.2.2 and 6.2.3: lowercases scheme and
    host, removes default ports (80/443), maps an empty path to "/", and strips
    query and fragment parts. The path is otherwise compared verbatim.
    """
    parsed = urlparse(target_url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    if ":" in netloc:
        host, _, port_str = netloc.partition(":")
        if (scheme == "http" and port_str == "80") or (scheme == "https" and port_str == "443"):
            netloc = host

    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", "", ""))


class InMemoryJtiReplayCache:
    """Thread-safe in-memory cache for DPoP proof replay detection (RFC 9449 Section 11.1).

    Tracks seen (jti, htu) tuples and purges entries older than max_age_seconds.
    Stores SHA-256 hashes of jti values to prevent memory exhaustion attacks.
    """

    def __init__(self, max_age_seconds: int = 120) -> None:
        self.max_age_seconds = max_age_seconds
        self._entries: dict[str, float] = {}

    def _hash_key(self, jti: str, htu: str) -> str:
        data = f"{jti}|{htu}".encode()
        return hashlib.sha256(data).hexdigest()

    def check_and_record(self, jti: str, htu: str, current_time: float) -> bool:
        """Check if jti has been seen before for htu. Returns True if fresh, False if replay."""
        self._purge(current_time)
        key = self._hash_key(jti, htu)
        if key in self._entries:
            return False
        self._entries[key] = current_time
        return True

    def _purge(self, current_time: float) -> None:
        threshold = current_time - self.max_age_seconds
        expired = [k for k, ts in self._entries.items() if ts < threshold]
        for k in expired:
            del self._entries[k]


class NonceManager:
    """Server-provided nonce manager (RFC 9449 Sections 8 and 9)."""

    def __init__(self, secret_key: str, validity_seconds: int = 300) -> None:
        self.secret_key = secret_key.encode("utf-8")
        self.validity_seconds = validity_seconds

    def generate_nonce(self, current_time: float) -> str:
        """Generate an unpredictable nonce: timestamp plus truncated HMAC-SHA256 tag."""
        ts_bytes = str(int(current_time)).encode("ascii")
        sig = hmac.new(self.secret_key, ts_bytes, hashlib.sha256).digest()[:16]
        raw = ts_bytes + b"." + sig
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def validate_nonce(self, nonce_str: str, current_time: float) -> bool:
        """Verify nonce signature and expiration."""
        try:
            raw = base64.urlsafe_b64decode(nonce_str.encode("ascii"))
            ts_bytes, sep, sig = raw.partition(b".")
            if not sep:
                return False
            expected_sig = hmac.new(self.secret_key, ts_bytes, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(sig, expected_sig):
                return False
            ts = int(ts_bytes.decode("ascii"))
            return abs(current_time - ts) <= self.validity_seconds
        except Exception:
            return False


@dataclass
class DPoPVerifier:
    """Full implementation of RFC 9449 Section 4.3 DPoP proof verification checklist."""

    replay_cache: InMemoryJtiReplayCache = field(
        default_factory=lambda: InMemoryJtiReplayCache(max_age_seconds=120)
    )
    nonce_manager: NonceManager | None = None
    require_nonce: bool = False
    max_clock_skew_seconds: int = 120

    def verify_proof(
        self,
        proof_jwt: str,
        request_method: str,
        request_url: str,
        access_token: str | None = None,
        expected_jkt: str | None = None,
        current_time: float | None = None,
    ) -> DPoPProofClaims:
        """Verify a DPoP proof against request context and access token binding."""
        now = time.time() if current_time is None else current_time

        # Step 2: Well-formed JWT
        try:
            unverified_header = jwt.get_unverified_header(proof_jwt)
        except Exception as exc:
            raise DPoPVerificationError("DPoP proof is not a well-formed JWT") from exc

        # Step 4: Header typ must be dpop+jwt
        typ = unverified_header.get("typ")
        if typ != "dpop+jwt":
            raise DPoPVerificationError(f"Invalid JOSE header typ '{typ}'; MUST be 'dpop+jwt'")

        # Step 5: alg is asymmetric, not none, not symmetric
        alg = unverified_header.get("alg")
        if not alg or alg not in ALLOWED_DPOP_ALGORITHMS:
            raise DPoPVerificationError(f"Unsupported or disallowed signature algorithm '{alg}'")

        # Step 6 & 7: Extract jwk, verify no private key
        raw_jwk = unverified_header.get("jwk")
        if not isinstance(raw_jwk, dict):
            raise DPoPVerificationError("Missing or invalid 'jwk' header parameter")

        jwk_dict: dict[str, Any] = cast("dict[str, Any]", raw_jwk)

        if any(private_field in jwk_dict for private_field in JWK_PRIVATE_KEY_FIELDS):
            raise DPoPVerificationError(
                "DPoP proof 'jwk' header parameter MUST NOT contain private key material"
            )

        # Compute JWK thumbprint per RFC 7638
        try:
            computed_jkt = compute_jwk_thumbprint(jwk_dict)
            public_key = jwt.PyJWK.from_dict(jwk_dict).key
        except Exception as exc:
            raise DPoPVerificationError(
                f"Failed to parse public key from 'jwk' header: {exc}"
            ) from exc

        # Step 6: Verify signature
        try:
            payload = jwt.decode(
                proof_jwt,
                public_key,
                algorithms=[alg],
                options={"verify_signature": True, "verify_exp": False},
            )
        except Exception as exc:
            raise DPoPVerificationError(f"DPoP proof signature verification failed: {exc}") from exc

        # Step 3: Required claims
        for required_claim in ("jti", "htm", "htu", "iat"):
            if required_claim not in payload:
                raise DPoPVerificationError(f"DPoP proof missing required claim '{required_claim}'")

        jti = str(payload["jti"])
        htm = str(payload["htm"])
        htu = str(payload["htu"])
        try:
            iat = int(payload["iat"])
        except (ValueError, TypeError) as exc:
            raise DPoPVerificationError(
                "DPoP proof 'iat' claim must be an integer timestamp"
            ) from exc

        # Step 8: htm matches HTTP method (case-sensitive, RFC 9110 Section 9.1)
        if htm != request_method:
            raise DPoPVerificationError(
                f"DPoP proof htm '{htm}' does not match request method '{request_method}'"
            )

        # Step 9: htu matches normalized target URI
        normalized_req_url = normalize_target_uri(request_url)
        normalized_htu = normalize_target_uri(htu)
        if normalized_htu != normalized_req_url:
            msg = (
                f"DPoP proof htu '{normalized_htu}' does not match "
                f"request URI '{normalized_req_url}'"
            )
            raise DPoPVerificationError(msg)

        # Step 10: Nonce validation
        nonce = payload.get("nonce")
        if self.require_nonce:
            if not nonce:
                new_nonce = (
                    self.nonce_manager.generate_nonce(now)
                    if self.nonce_manager
                    else "challenge-nonce"
                )
                raise DPoPVerificationError(
                    "Resource server requires nonce in DPoP proof",
                    error_code="use_dpop_nonce",
                    dpop_nonce=new_nonce,
                )
            if self.nonce_manager and not self.nonce_manager.validate_nonce(nonce, now):
                new_nonce = self.nonce_manager.generate_nonce(now)
                raise DPoPVerificationError(
                    "Stale or invalid DPoP nonce",
                    error_code="use_dpop_nonce",
                    dpop_nonce=new_nonce,
                )

        # Step 11: Creation time iat within acceptable window
        if abs(now - iat) > self.max_clock_skew_seconds:
            msg = (
                f"DPoP proof iat '{iat}' is outside the validity window "
                f"of +/-{self.max_clock_skew_seconds}s"
            )
            raise DPoPVerificationError(msg)

        # Step 12: Access token hash and public key binding
        ath = payload.get("ath")
        if access_token is not None:
            if not ath:
                raise DPoPVerificationError(
                    "DPoP proof missing mandatory 'ath' claim for protected resource access"
                )
            expected_ath = compute_access_token_hash(access_token)
            if ath != expected_ath:
                raise DPoPVerificationError(
                    f"DPoP proof ath '{ath}' does not match access token hash '{expected_ath}'"
                )
            if expected_jkt is not None and computed_jkt != expected_jkt:
                msg = (
                    f"DPoP proof key thumbprint '{computed_jkt}' does not "
                    f"match token binding '{expected_jkt}'"
                )
                raise DPoPVerificationError(msg)

        # Replay prevention check
        if not self.replay_cache.check_and_record(jti, normalized_req_url, now):
            raise DPoPVerificationError(
                f"DPoP proof jti '{jti}' has already been replayed for this URI"
            )

        return DPoPProofClaims(
            jti=jti,
            htm=htm,
            htu=htu,
            iat=iat,
            ath=ath,
            nonce=nonce,
            jwk=jwk_dict,
            jkt=computed_jkt,
        )


def verify_dpop_request(
    request: Request,
    verifier: DPoPVerifier,
    access_token: str | None = None,
    expected_jkt: str | None = None,
    current_time: float | None = None,
) -> DPoPProofClaims:
    """Extract and verify DPoP proof header from FastAPI/Starlette request."""
    # Step 1: Exactly one DPoP header
    dpop_headers = request.headers.getlist("dpop")
    if not dpop_headers:
        raise DPoPVerificationError(
            "Missing required DPoP header field",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if len(dpop_headers) > 1:
        raise DPoPVerificationError(
            "Multiple DPoP header fields received",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    proof_jwt = dpop_headers[0]
    return verifier.verify_proof(
        proof_jwt=proof_jwt,
        request_method=request.method,
        request_url=str(request.url),
        access_token=access_token,
        expected_jkt=expected_jkt,
        current_time=current_time,
    )


def verify_mtls_binding(
    client_cert_thumbprint: str | None,
    expected_thumbprint: str,
) -> None:
    """Verify that client certificate thumbprint matches access token cnf['x5t#S256'].

    Follows RFC 8705 Section 3.1.
    """
    if not client_cert_thumbprint:
        raise MTLSVerificationError(
            "Client certificate missing or not presented in mutual TLS connection"
        )
    if client_cert_thumbprint != expected_thumbprint:
        msg = (
            f"Client certificate thumbprint '{client_cert_thumbprint}' does not "
            f"match token binding '{expected_thumbprint}'"
        )
        raise MTLSVerificationError(msg)
