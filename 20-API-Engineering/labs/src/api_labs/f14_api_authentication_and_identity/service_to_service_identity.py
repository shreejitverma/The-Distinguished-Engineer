"""East-West Service Authentication: OAuth Client Credentials vs RFC 8693 Token Exchange.

Provides an RFC 8693 Token Exchange engine and OAuth 2.0 Client Credentials handler
for service-to-service east-west identity propagation, supporting on-behalf-of
delegation, recursive actor chains (RFC 8693 Section 4.1), downscoping, target
audience constraint (RFC 8707), and authorization evaluation.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, cast

import jwt
import jwt.types
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from pydantic import BaseModel

# --- RFC 8693 and OAuth 2.0 Constants ---

GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105 - standard URN
GRANT_TYPE_CLIENT_CREDENTIALS = "client_credentials"

TOKEN_TYPE_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - standard URN
TOKEN_TYPE_REFRESH_TOKEN = "urn:ietf:params:oauth:token-type:refresh_token"  # noqa: S105 - standard URN
TOKEN_TYPE_ID_TOKEN = "urn:ietf:params:oauth:token-type:id_token"  # noqa: S105 - standard URN
TOKEN_TYPE_SAML1 = "urn:ietf:params:oauth:token-type:saml1"  # noqa: S105 - standard URN
TOKEN_TYPE_SAML2 = "urn:ietf:params:oauth:token-type:saml2"  # noqa: S105 - standard URN
TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"  # noqa: S105 - standard URN

ERROR_INVALID_REQUEST = "invalid_request"
ERROR_INVALID_CLIENT = "invalid_client"
ERROR_INVALID_GRANT = "invalid_grant"
ERROR_UNAUTHORIZED_CLIENT = "unauthorized_client"
ERROR_UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"
ERROR_INVALID_SCOPE = "invalid_scope"
ERROR_INVALID_TARGET = "invalid_target"


class OAuthError(Exception):
    """OAuth 2.0 / RFC 8693 structured protocol error."""

    def __init__(
        self,
        error: str,
        error_description: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        super().__init__(error_description)
        self.error = error
        self.error_description = error_description
        self.status_code = status_code


class TokenExchangeRequest(BaseModel):
    """Request parameters for RFC 8693 Token Exchange."""

    grant_type: str
    resource: str | None = None
    audience: str | None = None
    scope: str | None = None
    requested_token_type: str = TOKEN_TYPE_ACCESS_TOKEN
    subject_token: str
    subject_token_type: str
    actor_token: str | None = None
    actor_token_type: str | None = None
    want_impersonation: bool = False


class TokenResponse(BaseModel):
    """Successful token response (RFC 6749 Section 5.1 & RFC 8693 Section 2.2.1)."""

    access_token: str
    issued_token_type: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth parameter
    expires_in: int
    scope: str | None = None
    refresh_token: str | None = None


class OAuthErrorResponse(BaseModel):
    """Token endpoint error body (RFC 6749 Section 5.2)."""

    error: str
    error_description: str | None = None


class ActorClaim(BaseModel):
    """Actor claim (RFC 8693 Section 4.1) representing delegation chains."""

    sub: str
    iss: str | None = None
    act: dict[str, Any] | None = None


@dataclass
class ClientRegistration:
    """Registered confidential client for service-to-service authentication."""

    client_id: str
    client_secret: str
    allowed_grant_types: list[str] = field(
        default_factory=lambda: [GRANT_TYPE_CLIENT_CREDENTIALS, GRANT_TYPE_TOKEN_EXCHANGE]
    )
    allowed_audiences: list[str] = field(default_factory=lambda: list[str]())
    can_delegate: bool = True
    can_impersonate: bool = False
    max_delegation_depth: int = 5


class SecurityTokenService:
    """In-memory RFC 8693 Security Token Service (STS) and OAuth AS."""

    def __init__(self, issuer: str = "https://as.example.internal") -> None:
        self.issuer = issuer
        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self._public_key = self._private_key.public_key()
        # Sign and verify with the parsed key objects: passing PEM bytes makes
        # PyJWT re-parse (and re-validate) the RSA key on every call.
        self._public_pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.clients: dict[str, ClientRegistration] = {}

    @property
    def public_pem(self) -> bytes:
        """Exported RSA public key in PEM format."""
        return self._public_pem

    def register_client(self, client: ClientRegistration) -> None:
        """Register a confidential service client."""
        self.clients[client.client_id] = client

    def authenticate_client(
        self,
        authorization_header: str | None,
        body_client_id: str | None,
        body_client_secret: str | None,
    ) -> ClientRegistration:
        """Authenticate caller via HTTP Basic (RFC 6749 Section 2.3.1) or post body."""
        client_id: str | None = None
        client_secret: str | None = None

        if authorization_header and authorization_header.startswith("Basic "):
            try:
                b64_creds = authorization_header[6:].strip()
                decoded = base64.b64decode(b64_creds).decode("utf-8")
                client_id, client_secret = decoded.split(":", 1)
            except Exception as exc:
                raise OAuthError(
                    ERROR_INVALID_CLIENT,
                    "Malformed HTTP Basic Authorization header",
                    status.HTTP_401_UNAUTHORIZED,
                ) from exc
        elif body_client_id and body_client_secret:
            client_id = body_client_id
            client_secret = body_client_secret

        if not client_id or not client_secret:
            raise OAuthError(
                ERROR_INVALID_CLIENT,
                "Client credentials missing",
                status.HTTP_401_UNAUTHORIZED,
            )

        client = self.clients.get(client_id)
        if not client or client.client_secret != client_secret:
            raise OAuthError(
                ERROR_INVALID_CLIENT,
                "Client authentication failed",
                status.HTTP_401_UNAUTHORIZED,
            )

        return client

    def mint_token(
        self,
        claims: dict[str, Any],
        lifetime_seconds: int = 3600,
    ) -> str:
        """Mint a signed JWT access token conforming to RFC 9068."""
        now = int(time.time())
        token_claims = {
            "iss": self.issuer,
            "iat": now,
            "nbf": now,
            "exp": now + lifetime_seconds,
            "jti": str(uuid.uuid4()),
            **claims,
        }
        encoded = jwt.encode(
            payload=token_claims,
            key=self._private_key,
            algorithm="RS256",
            headers={"typ": "at+jwt", "alg": "RS256"},
        )
        return encoded

    def decode_token(
        self,
        token: str,
        audience: str | None = None,
        options: jwt.types.Options | None = None,
    ) -> dict[str, Any]:
        """Decode and verify a JWT issued by this STS."""
        opts: jwt.types.Options = {"verify_aud": audience is not None}
        if options:
            opts.update(options)
        try:
            return jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=audience,
                options=opts,
            )
        except jwt.ExpiredSignatureError as exc:
            raise OAuthError(ERROR_INVALID_GRANT, "Security token has expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise OAuthError(ERROR_INVALID_GRANT, "Invalid token issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise OAuthError(ERROR_INVALID_GRANT, "Audience validation failed") from exc
        except jwt.PyJWTError as exc:
            raise OAuthError(ERROR_INVALID_GRANT, f"Invalid token: {exc}") from exc

    def issue_client_credentials(
        self,
        client: ClientRegistration,
        audience: str,
        scope: str | None = None,
        lifetime_seconds: int = 3600,
    ) -> TokenResponse:
        """Execute OAuth 2.0 Client Credentials Grant (RFC 6749 Section 4.4)."""
        if GRANT_TYPE_CLIENT_CREDENTIALS not in client.allowed_grant_types:
            raise OAuthError(
                ERROR_UNAUTHORIZED_CLIENT,
                "Client not authorized for client_credentials grant",
            )

        if client.allowed_audiences and audience not in client.allowed_audiences:
            raise OAuthError(
                ERROR_INVALID_TARGET,
                f"Client is not permitted to target audience: {audience}",
                status.HTTP_400_BAD_REQUEST,
            )

        # M2M token: subject is client identity; zero user context preserved.
        claims: dict[str, Any] = {
            "sub": client.client_id,
            "client_id": client.client_id,
            "aud": audience,
        }
        if scope:
            claims["scope"] = scope

        token_str = self.mint_token(claims, lifetime_seconds=lifetime_seconds)
        return TokenResponse(
            access_token=token_str,
            issued_token_type=TOKEN_TYPE_ACCESS_TOKEN,
            token_type="Bearer",  # noqa: S106 - OAuth parameter
            expires_in=lifetime_seconds,
            scope=scope,
        )

    def issue_token_exchange(
        self,
        client: ClientRegistration,
        request: TokenExchangeRequest,
        lifetime_seconds: int = 3600,
    ) -> TokenResponse:
        """Execute RFC 8693 Token Exchange."""
        if GRANT_TYPE_TOKEN_EXCHANGE not in client.allowed_grant_types:
            raise OAuthError(
                ERROR_UNAUTHORIZED_CLIENT,
                "Client not authorized for token exchange grant",
            )

        # 1. Validate subject_token_type (RFC 8693 Section 2.1)
        valid_subject_types = {TOKEN_TYPE_ACCESS_TOKEN, TOKEN_TYPE_JWT}
        if request.subject_token_type not in valid_subject_types:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                f"Unsupported subject_token_type: {request.subject_token_type}",
            )

        # 2. Decode and validate subject_token; an invalid or policy-rejected
        # subject_token MUST yield invalid_request (RFC 8693 Section 2.2.2)
        try:
            subject_claims = self.decode_token(request.subject_token, audience=None)
        except OAuthError as exc:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                f"Invalid subject_token: {exc.error_description}",
            ) from exc
        subject_id = subject_claims.get("sub")
        if not subject_id:
            raise OAuthError(ERROR_INVALID_REQUEST, "subject_token missing 'sub' claim")

        # actor_token and actor_token_type travel together (RFC 8693 Section 2.1):
        # the type is REQUIRED with a token and MUST NOT appear without one.
        actor_id = client.client_id
        if request.actor_token is None and request.actor_token_type is not None:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                "actor_token_type MUST NOT be included without actor_token",
            )
        if request.actor_token is not None:
            if request.actor_token_type not in {TOKEN_TYPE_ACCESS_TOKEN, TOKEN_TYPE_JWT}:
                raise OAuthError(
                    ERROR_INVALID_REQUEST,
                    "actor_token requires a supported actor_token_type",
                )
            try:
                actor_claims = self.decode_token(request.actor_token, audience=None)
            except OAuthError as exc:
                raise OAuthError(
                    ERROR_INVALID_REQUEST,
                    f"Invalid actor_token: {exc.error_description}",
                ) from exc
            actor_sub = actor_claims.get("sub")
            if not actor_sub:
                raise OAuthError(ERROR_INVALID_REQUEST, "actor_token missing 'sub' claim")
            actor_id = str(actor_sub)

        # 3. Determine and validate target (audience / resource, RFC 8693 Section 2.1)
        target = request.audience or request.resource
        if not target:
            raise OAuthError(
                ERROR_INVALID_REQUEST,
                "Token exchange requires 'audience' or 'resource' parameter",
            )

        if client.allowed_audiences and target not in client.allowed_audiences:
            raise OAuthError(
                ERROR_INVALID_TARGET,
                f"Client not authorized to request tokens for target: {target}",
            )

        # 4. Check 'may_act' claim in subject token if present (RFC 8693 Section 4.4)
        if "may_act" in subject_claims:
            may_act = subject_claims["may_act"]
            if isinstance(may_act, dict):
                may_act_dict = cast("dict[str, Any]", may_act)
                authorized_actor: str | None = cast("str | None", may_act_dict.get("sub"))
                if authorized_actor and authorized_actor != actor_id:
                    # Policy-rejected subject_token: invalid_request (RFC 8693 Section 2.2.2)
                    raise OAuthError(
                        ERROR_INVALID_REQUEST,
                        f"Actor '{actor_id}' is not authorized actor '{authorized_actor}'",
                    )

        # 5. Scope downscoping: subset rule is STS policy, not mandated by RFC 8693
        # (Section 5 only suggests scope to limit delegated rights)
        subject_scope_raw = subject_claims.get("scope", "")
        subject_scope_str = str(subject_scope_raw) if subject_scope_raw else ""
        subject_scopes: set[str] = set(subject_scope_str.split()) if subject_scope_str else set()

        if request.scope:
            requested_scopes = set(request.scope.split())
            if not requested_scopes.issubset(subject_scopes):
                missing = requested_scopes - subject_scopes
                raise OAuthError(
                    ERROR_INVALID_SCOPE,
                    f"Requested scope exceeds subject token scopes; missing: {sorted(missing)}",
                )
            effective_scope = request.scope
        else:
            effective_scope = subject_scope_str

        # 6. Delegation vs Impersonation handling (RFC 8693 Section 1.1 & 4.1)
        new_claims: dict[str, Any] = {
            "sub": subject_id,
            "client_id": client.client_id,
            "aud": target,
        }
        if effective_scope:
            new_claims["scope"] = effective_scope

        if request.want_impersonation:
            if not client.can_impersonate:
                raise OAuthError(
                    ERROR_UNAUTHORIZED_CLIENT,
                    f"Client '{client.client_id}' is not authorized for impersonation",
                )
            # Impersonation: omit 'act' claim entirely per RFC 8693 Section 1.1
        else:
            if not client.can_delegate:
                raise OAuthError(
                    ERROR_UNAUTHORIZED_CLIENT,
                    f"Client '{client.client_id}' is not authorized for delegation",
                )

            # Build recursive delegation actor chain (RFC 8693 Section 4.1)
            prior_act = subject_claims.get("act")
            depth = 1  # Count the new actor being appended
            curr: Any = prior_act
            while isinstance(curr, dict):
                depth += 1
                curr = cast("dict[str, Any]", curr).get("act")

            if depth > client.max_delegation_depth:
                raise OAuthError(
                    ERROR_INVALID_REQUEST,
                    f"Maximum delegation depth of {client.max_delegation_depth} "
                    f"exceeded (depth: {depth})",
                )

            current_actor_claim: dict[str, Any] = {"sub": actor_id}
            if prior_act and isinstance(prior_act, dict):
                current_actor_claim["act"] = prior_act

            new_claims["act"] = current_actor_claim

        # Bound token lifetime by the remaining lifetime of the subject token
        subject_exp = subject_claims.get("exp")
        now = int(time.time())
        token_lifetime = lifetime_seconds
        if subject_exp:
            remaining_lifetime = subject_exp - now
            if remaining_lifetime < token_lifetime:
                token_lifetime = max(1, remaining_lifetime)

        token_str = self.mint_token(new_claims, lifetime_seconds=token_lifetime)
        return TokenResponse(
            access_token=token_str,
            issued_token_type=TOKEN_TYPE_ACCESS_TOKEN,
            token_type="Bearer",  # noqa: S106 - OAuth parameter
            expires_in=token_lifetime,
            scope=effective_scope or None,
        )


def create_sts_app(sts: SecurityTokenService) -> FastAPI:
    """Create ASGI application implementing OAuth token endpoint."""
    app = FastAPI(title="RFC 8693 Security Token Service")

    @app.post("/oauth/token")
    async def token_endpoint(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" not in content_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": ERROR_INVALID_REQUEST, "error_description": "Must be urlencoded"},
            )

        form_data = await request.form()
        auth_header = request.headers.get("authorization")
        body_client_id = cast("str | None", form_data.get("client_id"))
        body_client_secret = cast("str | None", form_data.get("client_secret"))

        try:
            client = sts.authenticate_client(auth_header, body_client_id, body_client_secret)
            grant_type = form_data.get("grant_type")

            if grant_type == GRANT_TYPE_CLIENT_CREDENTIALS:
                raw_target = form_data.get("audience") or form_data.get("resource")
                audience = cast("str | None", raw_target)
                if not audience:
                    raise OAuthError(
                        ERROR_INVALID_REQUEST,
                        "audience or resource is required for client_credentials",
                    )
                scope = cast("str | None", form_data.get("scope"))
                resp = sts.issue_client_credentials(client, audience=audience, scope=scope)
                return Response(
                    content=resp.model_dump_json(),
                    media_type="application/json",
                    status_code=status.HTTP_200_OK,
                )

            if grant_type == GRANT_TYPE_TOKEN_EXCHANGE:
                subject_token = cast("str | None", form_data.get("subject_token"))
                subject_token_type = cast("str | None", form_data.get("subject_token_type"))
                if not subject_token or not subject_token_type:
                    raise OAuthError(
                        ERROR_INVALID_REQUEST,
                        "subject_token and subject_token_type are required for token exchange",
                    )
                req_obj = TokenExchangeRequest(
                    grant_type=GRANT_TYPE_TOKEN_EXCHANGE,
                    resource=cast("str | None", form_data.get("resource")),
                    audience=cast("str | None", form_data.get("audience")),
                    scope=cast("str | None", form_data.get("scope")),
                    requested_token_type=cast(
                        "str",
                        form_data.get("requested_token_type") or TOKEN_TYPE_ACCESS_TOKEN,
                    ),
                    subject_token=subject_token,
                    subject_token_type=subject_token_type,
                    actor_token=cast("str | None", form_data.get("actor_token")),
                    actor_token_type=cast("str | None", form_data.get("actor_token_type")),
                    want_impersonation=form_data.get("want_impersonation") == "true",
                )
                resp = sts.issue_token_exchange(client, req_obj)
                return Response(
                    content=resp.model_dump_json(),
                    media_type="application/json",
                    status_code=status.HTTP_200_OK,
                )

            raise OAuthError(
                ERROR_UNSUPPORTED_GRANT_TYPE,
                f"Unsupported grant_type: {grant_type}",
            )
        except OAuthError as exc:
            headers: dict[str, str] = {}
            if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                # RFC 6749 Section 5.2: the challenge matches the client's auth scheme
                headers["WWW-Authenticate"] = 'Basic realm="token"'
            return Response(
                content=OAuthErrorResponse(
                    error=exc.error, error_description=exc.error_description
                ).model_dump_json(),
                media_type="application/json",
                status_code=exc.status_code,
                headers=headers,
            )

    return app


def create_downstream_service(
    sts: SecurityTokenService,
    expected_audience: str,
    required_scope: str,
) -> FastAPI:
    """Create downstream resource server verifying tokens and evaluating delegation."""
    app = FastAPI(title="Downstream Resource Server")

    @app.get("/records/{user_id}")
    async def get_record(
        user_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or malformed Bearer token",
            )
        raw_token = authorization[7:].strip()
        try:
            claims = sts.decode_token(raw_token, audience=expected_audience)
        except OAuthError as exc:
            # RFC 6750 Section 3.1: an invalid or expired token is 401 invalid_token
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=exc.error_description,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            ) from exc

        # 1. Scope verification
        token_scopes = set(claims.get("scope", "").split())
        if required_scope not in token_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient scope. Required: {required_scope}",
            )

        # 2. Subject identity verification (Access Control)
        # In Token Exchange, claims['sub'] is the end user!
        # In Client Credentials, claims['sub'] is the service ID, failing user record check!
        token_sub = claims.get("sub")
        if token_sub != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Subject '{token_sub}' is not authorized to access record '{user_id}'",
            )

        # 3. Actor evaluation (RFC 8693 Section 4.1 normative rule)
        # Consumer checks top-level claims and the current actor (act.sub).
        act_claim = claims.get("act")
        current_actor: str | None = None
        if isinstance(act_claim, dict):
            act_dict = cast("dict[str, Any]", act_claim)
            current_actor = cast("str | None", act_dict.get("sub"))

        return {
            "status": "success",
            "record_id": f"rec-{user_id}-999",
            "owner": user_id,
            "caller_subject": token_sub,
            "current_actor": current_actor,
            "delegation_chain": act_claim,
        }

    return app
