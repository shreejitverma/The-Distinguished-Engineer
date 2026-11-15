"""Access and Refresh Token Lifecycles: Refresh Token Rotation, Introspection, and Phantom Tokens.

Implements:
- RFC 9700 Section 4.14.2: Refresh Token Rotation with Token Family tracking and reuse revocation.
- RFC 7662: OAuth 2.0 Token Introspection.
- RFC 9701: JWT Response for OAuth Token Introspection.
- RFC 7009: OAuth 2.0 Token Revocation.
- Phantom Token Pattern: Opaque external reference token translation to RFC 9068 internal JWT.
"""

from __future__ import annotations

import enum
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class TokenStatus(enum.StrEnum):
    """Lifecycle state of a refresh or access token."""

    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"


class TokenError(Exception):
    """Base exception for token lifecycle errors."""

    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


class InvalidGrantError(TokenError):
    """Raised when an authorization grant or refresh token is invalid, expired, or reused."""

    def __init__(self, description: str) -> None:
        super().__init__(error="invalid_grant", description=description, status_code=400)


class InvalidTokenError(TokenError):
    """Raised when an access token is expired, revoked, or untrusted."""

    def __init__(self, description: str) -> None:
        super().__init__(error="invalid_token", description=description, status_code=401)


@dataclass
class RefreshTokenRecord:
    """Represents a refresh token within a rotation family."""

    token_id: str
    family_id: str
    user_id: str
    client_id: str
    generation: int
    parent_token_id: str | None
    status: TokenStatus
    issued_at: float
    expires_at: float
    rotated_at: float | None = None
    scopes: list[str] = field(default_factory=list[str])
    replacement_token_id: str | None = None
    replacement_access_token_id: str | None = None


@dataclass
class AccessTokenRecord:
    """Represents an opaque access token issued to a client."""

    token_id: str
    family_id: str
    user_id: str
    client_id: str
    status: TokenStatus
    issued_at: float
    expires_at: float
    scopes: list[str] = field(default_factory=list[str])


@dataclass
class TokenFamily:
    """Tracks token lineage and grant hierarchy for refresh token rotation (RFC 9700)."""

    family_id: str
    user_id: str
    client_id: str
    active_refresh_token_id: str
    is_revoked: bool = False
    revocation_reason: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class SecurityAlert:
    """Audit record emitted when a security anomaly like token reuse is detected."""

    alert_type: str
    family_id: str
    reused_token_id: str
    user_id: str
    client_id: str
    timestamp: float
    details: dict[str, Any] = field(default_factory=dict[str, Any])


class TokenLifecycleManager:
    """Coordinates token issuance, rotation, introspection, and revocation."""

    def __init__(
        self,
        jwt_signing_key: str,
        jwt_issuer: str = "https://auth.example.com",
        default_access_ttl: float = 900.0,
        default_refresh_ttl: float = 86400.0,
        grace_period_seconds: float = 10.0,
    ) -> None:
        self.jwt_signing_key = jwt_signing_key
        self.jwt_issuer = jwt_issuer
        self.default_access_ttl = default_access_ttl
        self.default_refresh_ttl = default_refresh_ttl
        self.grace_period_seconds = grace_period_seconds

        self.families: dict[str, TokenFamily] = {}
        self.refresh_tokens: dict[str, RefreshTokenRecord] = {}
        self.access_tokens: dict[str, AccessTokenRecord] = {}
        self.alerts: list[SecurityAlert] = []

    def issue_initial_grant(
        self,
        user_id: str,
        client_id: str,
        scopes: list[str],
        now: float | None = None,
    ) -> tuple[str, str]:
        """Issue initial access and refresh tokens, starting a new token family."""
        current_time = time.time() if now is None else now
        family_id = f"fam_{secrets.token_urlsafe(16)}"
        rt_id = f"rt_{secrets.token_urlsafe(32)}"
        at_id = f"at_{secrets.token_urlsafe(32)}"

        family = TokenFamily(
            family_id=family_id,
            user_id=user_id,
            client_id=client_id,
            active_refresh_token_id=rt_id,
            created_at=current_time,
        )
        self.families[family_id] = family

        rt_record = RefreshTokenRecord(
            token_id=rt_id,
            family_id=family_id,
            user_id=user_id,
            client_id=client_id,
            generation=0,
            parent_token_id=None,
            status=TokenStatus.ACTIVE,
            issued_at=current_time,
            expires_at=current_time + self.default_refresh_ttl,
            scopes=list(scopes),
        )
        self.refresh_tokens[rt_id] = rt_record

        at_record = AccessTokenRecord(
            token_id=at_id,
            family_id=family_id,
            user_id=user_id,
            client_id=client_id,
            status=TokenStatus.ACTIVE,
            issued_at=current_time,
            expires_at=current_time + self.default_access_ttl,
            scopes=list(scopes),
        )
        self.access_tokens[at_id] = at_record

        return at_id, rt_id

    def rotate_refresh_token(
        self,
        presented_token_id: str,
        client_id: str,
        now: float | None = None,
    ) -> tuple[str, str]:
        """Rotate a refresh token per RFC 9700 Section 4.14.2 with reuse detection."""
        current_time = time.time() if now is None else now
        record = self.refresh_tokens.get(presented_token_id)
        if record is None:
            raise InvalidGrantError("Refresh token does not exist.")

        if record.client_id != client_id:
            raise InvalidGrantError("Client mismatch for presented refresh token.")

        family = self.families.get(record.family_id)
        if family is None or family.is_revoked:
            raise InvalidGrantError("Token family has been revoked.")

        if current_time >= record.expires_at:
            raise InvalidGrantError("Refresh token has expired.")

        # Replay / reuse detection
        if record.status == TokenStatus.ROTATED:
            # Check concurrency grace period
            elapsed_since_rotation = (
                current_time - record.rotated_at if record.rotated_at is not None else float("inf")
            )
            if (
                elapsed_since_rotation <= self.grace_period_seconds
                and record.replacement_token_id is not None
                and record.replacement_access_token_id is not None
                and family.active_refresh_token_id == record.replacement_token_id
            ):
                # Immediate predecessor of the active tip, inside the grace window:
                # return the existing replacement pair without advancing the lineage.
                return record.replacement_access_token_id, record.replacement_token_id

            # Outside grace period or non-immediate ancestor: REUSE DETECTED!
            self.revoke_family(
                family_id=record.family_id,
                reason="reuse_detected",
                trigger_token_id=presented_token_id,
            )
            raise InvalidGrantError(
                "Refresh token reuse detected; all family tokens have been revoked."
            )

        if record.status != TokenStatus.ACTIVE:
            raise InvalidGrantError("Refresh token is not active.")

        # Active token rotation
        new_rt_id = f"rt_{secrets.token_urlsafe(32)}"
        new_at_id = f"at_{secrets.token_urlsafe(32)}"

        record.status = TokenStatus.ROTATED
        record.rotated_at = current_time
        record.replacement_token_id = new_rt_id
        record.replacement_access_token_id = new_at_id

        new_rt = RefreshTokenRecord(
            token_id=new_rt_id,
            family_id=record.family_id,
            user_id=record.user_id,
            client_id=record.client_id,
            generation=record.generation + 1,
            parent_token_id=record.token_id,
            status=TokenStatus.ACTIVE,
            issued_at=current_time,
            expires_at=current_time + self.default_refresh_ttl,
            scopes=list(record.scopes),
        )
        self.refresh_tokens[new_rt_id] = new_rt

        new_at = AccessTokenRecord(
            token_id=new_at_id,
            family_id=record.family_id,
            user_id=record.user_id,
            client_id=record.client_id,
            status=TokenStatus.ACTIVE,
            issued_at=current_time,
            expires_at=current_time + self.default_access_ttl,
            scopes=list(record.scopes),
        )
        self.access_tokens[new_at_id] = new_at

        family.active_refresh_token_id = new_rt_id
        return new_at_id, new_rt_id

    def revoke_family(
        self,
        family_id: str,
        reason: str,
        trigger_token_id: str | None = None,
    ) -> None:
        """Revoke all tokens in a family immediately (RFC 9700 Section 4.14.2)."""
        family = self.families.get(family_id)
        if family is None:
            return

        family.is_revoked = True
        family.revocation_reason = reason

        for rt in self.refresh_tokens.values():
            if rt.family_id == family_id:
                rt.status = TokenStatus.REVOKED

        for at in self.access_tokens.values():
            if at.family_id == family_id:
                at.status = TokenStatus.REVOKED

        if reason == "reuse_detected" and trigger_token_id is not None:
            self.alerts.append(
                SecurityAlert(
                    alert_type="TOKEN_REUSE_DETECTED",
                    family_id=family_id,
                    reused_token_id=trigger_token_id,
                    user_id=family.user_id,
                    client_id=family.client_id,
                    timestamp=time.time(),
                    details={"revocation_reason": reason},
                )
            )

    def revoke_token(
        self,
        token: str,
        client_id: str,
        token_type_hint: str | None = None,
    ) -> None:
        """Revoke a token according to RFC 7009 Section 2.1.

        The hint only orders the lookup; a miss extends the search to the other token type.
        An unknown token returns quietly (the endpoint answers 200, Section 2.2), but a token
        issued to another client is refused, as Authlib 1.9.5 does with ``invalid_grant``.
        """
        record: RefreshTokenRecord | AccessTokenRecord | None
        if token_type_hint == "access_token":  # noqa: S105
            record = self.access_tokens.get(token) or self.refresh_tokens.get(token)
        else:
            record = self.refresh_tokens.get(token) or self.access_tokens.get(token)

        if record is None:
            return
        if record.client_id != client_id:
            raise InvalidGrantError("Token was not issued to the requesting client.")
        if isinstance(record, RefreshTokenRecord):
            # Section 2.1: revoking a refresh token SHOULD invalidate its access tokens too.
            self.revoke_family(record.family_id, reason="client_revocation")
        else:
            record.status = TokenStatus.REVOKED

    def introspect_token(
        self,
        token: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Introspect an access or refresh token according to RFC 7662."""
        current_time = time.time() if now is None else now

        if token.startswith("at_") or token in self.access_tokens:
            at = self.access_tokens.get(token)
            if at is None:
                return {"active": False}
            family = self.families.get(at.family_id)
            if (
                family is None
                or family.is_revoked
                or at.status != TokenStatus.ACTIVE
                or current_time >= at.expires_at
            ):
                return {"active": False}
            return {
                "active": True,
                "scope": " ".join(at.scopes),
                "client_id": at.client_id,
                "sub": at.user_id,
                "exp": int(at.expires_at),
                "iat": int(at.issued_at),
                "iss": self.jwt_issuer,
                "jti": at.token_id,
                "token_type": "Bearer",
            }

        if token.startswith("rt_") or token in self.refresh_tokens:
            rt = self.refresh_tokens.get(token)
            if rt is None:
                return {"active": False}
            family = self.families.get(rt.family_id)
            if (
                family is None
                or family.is_revoked
                or rt.status != TokenStatus.ACTIVE
                or current_time >= rt.expires_at
            ):
                return {"active": False}
            return {
                "active": True,
                "scope": " ".join(rt.scopes),
                "client_id": rt.client_id,
                "sub": rt.user_id,
                "exp": int(rt.expires_at),
                "iat": int(rt.issued_at),
                "iss": self.jwt_issuer,
                "jti": rt.token_id,
                "token_type": "refresh_token",
            }

        return {"active": False}

    def mint_phantom_jwt(
        self,
        opaque_token: str,
        resource_audience: str,
        now: float | None = None,
    ) -> str:
        """Mint an RFC 9068 Access Token JWT for an active opaque token (Phantom Token)."""
        current_time = time.time() if now is None else now
        introspection = self.introspect_token(opaque_token, now=current_time)
        if not introspection.get("active", False):
            raise InvalidTokenError("Opaque token is inactive or revoked.")

        payload = {
            "iss": self.jwt_issuer,
            "sub": introspection["sub"],
            "aud": resource_audience,
            "exp": introspection["exp"],
            "iat": int(current_time),
            "jti": introspection["jti"],
            "client_id": introspection["client_id"],
            "scope": introspection["scope"],
        }
        headers = {
            "typ": "at+jwt",
            "alg": "HS256",
        }
        return jwt.encode(payload, self.jwt_signing_key, algorithm="HS256", headers=headers)

    def create_rfc9701_jwt_introspection(
        self,
        token: str,
        resource_server_audience: str,
        now: float | None = None,
    ) -> str:
        """Create an RFC 9701 JWT Response for Token Introspection."""
        current_time = time.time() if now is None else now
        raw_introspection = self.introspect_token(token, now=current_time)

        payload = {
            "iss": self.jwt_issuer,
            "aud": resource_server_audience,
            "iat": int(current_time),
            "token_introspection": raw_introspection,
        }
        headers = {
            "typ": "token-introspection+jwt",
            "alg": "HS256",
        }
        return jwt.encode(payload, self.jwt_signing_key, algorithm="HS256", headers=headers)


class PhantomTokenGatewayMiddleware:
    """ASGI Middleware implementing the Phantom Token translation pattern.

    Intercepts 'Authorization: Bearer <opaque_token>', verifies it via introspection,
    mints/translates it to an internal RFC 9068 signed JWT, and forwards it to downstream APIs.
    """

    def __init__(
        self,
        app: ASGIApp,
        manager: TokenLifecycleManager,
        resource_audience: str,
    ) -> None:
        self.app = app
        self.manager = manager
        self.resource_audience = resource_audience

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        auth_header = headers.get("authorization")

        if not auth_header or not auth_header.startswith("Bearer "):
            response = JSONResponse(
                {"error": "invalid_request", "error_description": "Missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_request"'},
            )
            await response(scope, receive, send)
            return

        opaque_token = auth_header[len("Bearer ") :].strip()

        try:
            phantom_jwt = self.manager.mint_phantom_jwt(
                opaque_token=opaque_token,
                resource_audience=self.resource_audience,
            )
        except InvalidTokenError as exc:
            response = JSONResponse(
                {"error": exc.error, "error_description": exc.description},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer error="{exc.error}"'},
            )
            await response(scope, receive, send)
            return

        # Replace external opaque token with internal phantom JWT
        new_headers = [
            (name, value) for name, value in scope["headers"] if name.lower() != b"authorization"
        ]
        new_headers.append((b"authorization", f"Bearer {phantom_jwt}".encode("latin-1")))
        scope["headers"] = new_headers

        await self.app(scope, receive, send)
