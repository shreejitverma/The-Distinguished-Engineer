"""High-Assurance OAuth Flows: PAR (RFC 9126) and RAR (RFC 9396).

Implements:
1. Pushed Authorization Request (PAR) endpoint with client authentication,
   pre-validation of redirect URIs and PKCE, and generation of high-entropy
   single-use urn:ietf:params:oauth:request_uri references.
2. Rich Authorization Requests (RAR) schema validation, Cartesian product
   semantics, and authorization details downscoping/subsumption comparison.
3. Authorization endpoint enforcing client-id binding, single-use URI replay
   protection, and mandatory PAR policy.
4. Token endpoint with PKCE verification, downscoping evaluation, and issuance
   of JWT access tokens bearing structured authorization_details claims.
5. Resource server enforcement of fine-grained RAR transaction constraints.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

import jwt
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

JWT_SECRET = "par-rar-test-secret-key-32-bytes-minimum!!"  # noqa: S105
JWT_ALGORITHM = "HS256"
ISSUER = "http://test"
DEFAULT_PAR_LIFETIME_SECONDS = 90
DEFAULT_CODE_LIFETIME_SECONDS = 60


class InvalidAuthorizationDetailsError(Exception):
    """Raised when authorization_details fails RFC 9396 Section 5 validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InstructedAmount(BaseModel):
    """Amount and currency specification for payment initiation (RFC 9396 Section 2)."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    amount: str = Field(pattern=r"^\d+(\.\d{1,4})?$")


class CreditorAccount(BaseModel):
    """Creditor bank account specification."""

    model_config = ConfigDict(extra="forbid")

    iban: str = Field(min_length=15, max_length=34, pattern=r"^[A-Z]{2}\d{2}[A-Z0-9]+$")
    bic: str | None = Field(default=None, min_length=8, max_length=11)


class PaymentInitiationDetail(BaseModel):
    """RFC 9396 authorization detail for payment initiation."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["payment_initiation"] = "payment_initiation"
    actions: list[str] = Field(default_factory=lambda: ["initiate", "status"])
    locations: list[str] = Field(default_factory=list)
    instructedAmount: InstructedAmount
    creditorAccount: CreditorAccount
    creditorName: str | None = None
    remittanceInformationUnstructured: str | None = None


class AccountInformationDetail(BaseModel):
    """RFC 9396 authorization detail for account information access."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["account_information"] = "account_information"
    actions: list[str] = Field(default_factory=lambda: ["list_accounts", "read_balances"])
    locations: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)


KNOWN_RAR_SCHEMAS: dict[str, type[BaseModel]] = {
    "payment_initiation": PaymentInitiationDetail,
    "account_information": AccountInformationDetail,
}


def parse_and_validate_rar(raw_details: object) -> list[dict[str, Any]]:
    """Parse and strictly validate authorization_details according to RFC 9396 Section 5.

    The AS MUST abort processing and return invalid_authorization_details if:
    - contains an unknown authorization details type value,
    - is an object of known type but containing unknown fields,
    - contains fields of the wrong type,
    - contains fields with invalid values,
    - is missing required fields.
    """
    items_raw: object
    if isinstance(raw_details, str):
        try:
            items_raw = cast("object", json.loads(raw_details))
        except json.JSONDecodeError as exc:
            raise InvalidAuthorizationDetailsError("JSON syntax error") from exc
    elif isinstance(raw_details, list):
        items_raw = cast("object", raw_details)
    else:
        raise InvalidAuthorizationDetailsError("authorization_details must be a JSON array")

    if not isinstance(items_raw, list):
        raise InvalidAuthorizationDetailsError("authorization_details must be a JSON array")

    items_list = cast("list[object]", items_raw)
    validated: list[dict[str, Any]] = []
    for elem in items_list:
        if not isinstance(elem, dict):
            raise InvalidAuthorizationDetailsError("elements must be JSON objects")
        item_dict = cast("dict[str, object]", elem)
        item_type = item_dict.get("type")
        if not item_type or not isinstance(item_type, str):
            raise InvalidAuthorizationDetailsError("missing or invalid type identifier")
        schema_cls = KNOWN_RAR_SCHEMAS.get(item_type)
        if schema_cls is None:
            raise InvalidAuthorizationDetailsError(
                f"unknown authorization details type: {item_type}"
            )
        try:
            model = schema_cls.model_validate(item_dict)
            validated.append(model.model_dump(exclude_none=True))
        except ValidationError as exc:
            raise InvalidAuthorizationDetailsError(f"schema validation failed: {exc}") from exc

    return validated


def is_detail_subsumed(requested: dict[str, Any], granted: dict[str, Any]) -> bool:
    """Compare requested vs granted detail under RFC 9396 Section 6.1 rules."""
    req_type = requested.get("type")
    grant_type = granted.get("type")
    if req_type != grant_type:
        return False

    req_actions = set(requested.get("actions", []))
    grant_actions = set(granted.get("actions", []))
    if not req_actions.issubset(grant_actions):
        return False

    req_locs = set(requested.get("locations", []))
    grant_locs = set(granted.get("locations", []))
    if grant_locs and not req_locs.issubset(grant_locs):
        return False

    if req_type == "payment_initiation":
        req_amt = requested.get("instructedAmount", {})
        grant_amt = granted.get("instructedAmount", {})
        if req_amt.get("currency") != grant_amt.get("currency"):
            return False
        if Decimal(str(req_amt.get("amount", "0"))) > Decimal(str(grant_amt.get("amount", "0"))):
            return False
        req_creditor = requested.get("creditorAccount", {}).get("iban")
        grant_creditor = granted.get("creditorAccount", {}).get("iban")
        if req_creditor != grant_creditor:
            return False

    if req_type == "account_information":
        req_acc = set(requested.get("accounts", []))
        grant_acc = set(granted.get("accounts", []))
        if grant_acc and not req_acc.issubset(grant_acc):
            return False

    return True


def compare_authorization_details(
    requested: list[dict[str, Any]], granted: list[dict[str, Any]]
) -> bool:
    """Check if all requested authorization details are covered by granted permissions."""
    return all(any(is_detail_subsumed(req, grant) for grant in granted) for req in requested)


def verify_pkce(verifier: str, challenge: str, method: str) -> bool:
    """Validate PKCE code_verifier against code_challenge (RFC 7636)."""
    if method == "S256":
        hashed = hashlib.sha256(verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(hashed).decode("ascii").rstrip("=")
        return hmac.compare_digest(computed, challenge)
    if method == "plain":
        return hmac.compare_digest(verifier, challenge)
    return False


class ClientRecord(BaseModel):
    client_id: str
    client_secret: str | None = None
    is_public: bool = False
    redirect_uris: list[str]
    allowed_scopes: list[str]
    require_pushed_authorization_requests: bool = True


class PushedRequestRecord(BaseModel):
    request_uri: str
    client_id: str
    redirect_uri: str
    response_type: str
    scope: str | None = None
    state: str | None = None
    code_challenge: str | None = None
    code_challenge_method: str | None = None
    authorization_details: list[dict[str, Any]] | None = None
    expires_at: float
    used: bool = False


class AuthCodeRecord(BaseModel):
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str | None = None
    code_challenge_method: str | None = None
    authorization_details: list[dict[str, Any]] | None = None
    scope: str | None = None
    expires_at: float
    used: bool = False


class OAuthStateStore:
    """In-memory store for registered clients, pushed requests, and authorization codes."""

    def __init__(self) -> None:
        self.clients: dict[str, ClientRecord] = {}
        self.pushed_requests: dict[str, PushedRequestRecord] = {}
        self.auth_codes: dict[str, AuthCodeRecord] = {}

    def register_client(self, client: ClientRecord) -> None:
        self.clients[client.client_id] = client

    def get_client(self, client_id: str) -> ClientRecord | None:
        return self.clients.get(client_id)

    def store_pushed_request(self, record: PushedRequestRecord) -> None:
        self.pushed_requests[record.request_uri] = record

    def get_pushed_request(self, request_uri: str) -> PushedRequestRecord | None:
        return self.pushed_requests.get(request_uri)

    def store_auth_code(self, record: AuthCodeRecord) -> None:
        self.auth_codes[record.code] = record

    def get_auth_code(self, code: str) -> AuthCodeRecord | None:
        return self.auth_codes.get(code)


def authenticate_client_dependency(
    store: OAuthStateStore,
    authorization: str | None,
    client_id_form: str | None,
    client_secret_form: str | None,
) -> ClientRecord:
    """Validate client authentication via Basic Auth or form parameters."""
    cid: str | None = None
    csecret: str | None = None

    if authorization and authorization.startswith("Basic "):
        try:
            raw_b64 = authorization.split(" ", 1)[1]
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            cid, csecret = decoded.split(":", 1)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_client", "error_description": "malformed basic auth"},
                headers={"WWW-Authenticate": "Basic"},
            ) from exc
    elif client_id_form:
        cid = client_id_form
        csecret = client_secret_form

    if not cid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_client", "error_description": "client missing"},
            headers={"WWW-Authenticate": "Basic"},
        )

    client = store.get_client(cid)
    if not client:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_client", "error_description": "unknown client"},
            headers={"WWW-Authenticate": "Basic"},
        )

    if client.is_public:
        return client

    if (
        not csecret
        or not client.client_secret
        or not hmac.compare_digest(csecret, client.client_secret)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_client", "error_description": "client auth failed"},
            headers={"WWW-Authenticate": "Basic"},
        )

    return client


async def get_client_record(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    client_id_form: Annotated[str | None, Form(alias="client_id")] = None,
    client_secret_form: Annotated[str | None, Form(alias="client_secret")] = None,
) -> ClientRecord:
    """Module-level dependency resolving ClientRecord from app state."""
    state_store = cast("OAuthStateStore", request.app.state.oauth_store)
    return authenticate_client_dependency(
        state_store, authorization, client_id_form, client_secret_form
    )


def create_app(store: OAuthStateStore | None = None) -> FastAPI:
    """Instantiate a FastAPI application configured for PAR, RAR, and RS endpoints."""
    app = FastAPI(title="High-Assurance OAuth AS & RS")
    state_store = store if store is not None else OAuthStateStore()
    app.state.oauth_store = state_store

    @app.get("/.well-known/oauth-authorization-server")
    async def metadata() -> dict[str, Any]:
        """Authorization Server Metadata (RFC 8414, RFC 9126 Section 5, RFC 9396 Section 10)."""
        return {
            "issuer": ISSUER,
            "pushed_authorization_request_endpoint": f"{ISSUER}/as/par",
            "require_pushed_authorization_requests": True,
            "authorization_endpoint": f"{ISSUER}/as/authorize",
            "token_endpoint": f"{ISSUER}/as/token",
            "authorization_details_types_supported": list(KNOWN_RAR_SCHEMAS.keys()),
            "code_challenge_methods_supported": ["S256"],
            "response_types_supported": ["code"],
        }

    @app.post("/as/par", status_code=status.HTTP_201_CREATED)
    async def pushed_authorization_request(
        client: Annotated[ClientRecord, Depends(get_client_record)],
        response_type: Annotated[str, Form()],
        client_id: Annotated[str, Form()],
        redirect_uri: Annotated[str, Form()],
        scope: Annotated[str | None, Form()] = None,
        state: Annotated[str | None, Form()] = None,
        code_challenge: Annotated[str | None, Form()] = None,
        code_challenge_method: Annotated[str | None, Form()] = None,
        authorization_details: Annotated[str | None, Form()] = None,
        request_uri: Annotated[str | None, Form()] = None,
    ) -> Response:
        """Pushed Authorization Request endpoint (RFC 9126 Section 2)."""
        # RFC 9126 Section 2.1: request_uri MUST NOT be provided in the PAR request.
        if request_uri is not None:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "request_uri is forbidden in PAR payload",
                },
            )

        if client_id != client.client_id:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "client_id does not match authenticated client",
                },
            )

        if redirect_uri not in client.redirect_uris:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "redirect_uri is not registered for this client",
                },
            )

        if response_type != "code":
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "unsupported_response_type",
                    "error_description": "only response_type=code is supported",
                },
            )

        # Public clients MUST provide PKCE S256
        if client.is_public and (not code_challenge or code_challenge_method != "S256"):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "PKCE S256 code_challenge is required for public clients",
                },
            )

        validated_rar: list[dict[str, Any]] | None = None
        if authorization_details is not None:
            try:
                validated_rar = parse_and_validate_rar(authorization_details)
            except InvalidAuthorizationDetailsError as exc:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "error": "invalid_authorization_details",
                        "error_description": exc.message,
                    },
                )

        token = secrets.token_urlsafe(32)
        issued_uri = f"urn:ietf:params:oauth:request_uri:{token}"
        expires_at = time.time() + DEFAULT_PAR_LIFETIME_SECONDS

        pushed_record = PushedRequestRecord(
            request_uri=issued_uri,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            authorization_details=validated_rar,
            expires_at=expires_at,
            used=False,
        )
        state_store.store_pushed_request(pushed_record)

        payload = {
            "request_uri": issued_uri,
            "expires_in": DEFAULT_PAR_LIFETIME_SECONDS,
        }
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=payload,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.get("/as/authorize")
    async def authorize(
        client_id: str,
        request_uri: str | None = None,
    ) -> Response:
        """Authorization endpoint (RFC 9126 Section 4)."""
        client = state_store.get_client(client_id)
        if not client:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "unknown client_id"},
            )

        if client.require_pushed_authorization_requests and not request_uri:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "pushed authorization request is required for this client",
                },
            )

        if not request_uri:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "missing request_uri"},
            )

        record = state_store.get_pushed_request(request_uri)
        if not record:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "unknown request_uri"},
            )

        # RFC 9126 Section 4 & 7.5: Client binding check against Request URI Swapping
        if record.client_id != client_id:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "request_uri was issued to a different client",
                },
            )

        # RFC 9126 Section 4 & 7.3: Single-use replay check
        if record.used:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": "invalid_request",
                    "error_description": "request_uri has already been used",
                },
            )

        if time.time() > record.expires_at:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_request", "error_description": "request_uri expired"},
            )

        # Mark request_uri as consumed
        record.used = True

        # Simulate user consent and issue authorization code
        code_val = secrets.token_urlsafe(32)
        code_record = AuthCodeRecord(
            code=code_val,
            client_id=record.client_id,
            redirect_uri=record.redirect_uri,
            code_challenge=record.code_challenge,
            code_challenge_method=record.code_challenge_method,
            authorization_details=record.authorization_details,
            scope=record.scope,
            expires_at=time.time() + DEFAULT_CODE_LIFETIME_SECONDS,
            used=False,
        )
        state_store.store_auth_code(code_record)

        sep = "&" if "?" in record.redirect_uri else "?"
        redirect_target = f"{record.redirect_uri}{sep}code={code_val}"
        if record.state:
            redirect_target += f"&state={record.state}"

        return RedirectResponse(url=redirect_target, status_code=status.HTTP_302_FOUND)

    @app.post("/as/token")
    async def token(
        grant_type: Annotated[str, Form()],
        code: Annotated[str, Form()],
        redirect_uri: Annotated[str, Form()],
        client_id: Annotated[str, Form()],
        code_verifier: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
        authorization_details: Annotated[str | None, Form()] = None,
    ) -> Response:
        """Token endpoint (RFC 6749 Section 4.1.3 & RFC 9396 Section 6)."""
        if grant_type != "authorization_code":
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "unsupported_grant_type"},
            )

        client = state_store.get_client(client_id)
        if not client:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "invalid_client"},
            )

        if not client.is_public and (
            not client_secret
            or not client.client_secret
            or not hmac.compare_digest(client_secret, client.client_secret)
        ):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "invalid_client"},
            )

        code_record = state_store.get_auth_code(code)
        if not code_record or code_record.used or time.time() > code_record.expires_at:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_grant"},
            )

        if code_record.client_id != client_id or code_record.redirect_uri != redirect_uri:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": "invalid_grant"},
            )

        if code_record.code_challenge:
            if not code_verifier or not code_record.code_challenge_method:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"error": "invalid_grant", "error_description": "missing verifier"},
                )
            if not verify_pkce(
                code_verifier, code_record.code_challenge, code_record.code_challenge_method
            ):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"error": "invalid_grant", "error_description": "PKCE failed"},
                )

        code_record.used = True

        final_rar = code_record.authorization_details
        if authorization_details is not None:
            try:
                requested_rar = parse_and_validate_rar(authorization_details)
            except InvalidAuthorizationDetailsError as exc:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "error": "invalid_authorization_details",
                        "error_description": exc.message,
                    },
                )
            if code_record.authorization_details is None or not compare_authorization_details(
                requested_rar, code_record.authorization_details
            ):
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "error": "invalid_authorization_details",
                        "error_description": "requested details exceed granted rights",
                    },
                )
            final_rar = requested_rar

        # Mint JWT access token bearing top-level authorization_details (RFC 9396 Section 9.1)
        now = int(time.time())
        token_claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": "user_sandbox_9941",
            "aud": "https://api.example.com",
            "client_id": client_id,
            "iat": now,
            "exp": now + 3600,
            "jti": secrets.token_hex(16),
        }
        if final_rar is not None:
            token_claims["authorization_details"] = final_rar

        access_token_jwt = jwt.encode(token_claims, JWT_SECRET, algorithm=JWT_ALGORITHM)

        response_body: dict[str, Any] = {
            "access_token": access_token_jwt,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        if final_rar is not None:
            response_body["authorization_details"] = final_rar

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=response_body,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    @app.post("/rs/payments", status_code=status.HTTP_201_CREATED)
    async def create_payment(
        request: Request,
        authorization: Annotated[str, Header()],
    ) -> Response:
        """Resource Server endpoint enforcing fine-grained RAR details (RFC 9396 Section 9)."""
        if not authorization.startswith("Bearer "):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "invalid_token"},
            )

        raw_jwt = authorization.split(" ", 1)[1]
        try:
            claims = jwt.decode(
                raw_jwt,
                JWT_SECRET,
                algorithms=[JWT_ALGORITHM],
                audience="https://api.example.com",
                issuer=ISSUER,
            )
        except Exception:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"error": "invalid_token"},
            )

        body = await request.json()
        req_amount = Decimal(str(body.get("amount", "0")))
        req_currency = body.get("currency")
        req_creditor = body.get("creditor_iban")

        rar_claims = claims.get("authorization_details", [])
        authorized = False

        for detail in rar_claims:
            if detail.get("type") != "payment_initiation":
                continue
            if "initiate" not in detail.get("actions", []):
                continue
            amt_spec = detail.get("instructedAmount", {})
            if amt_spec.get("currency") != req_currency:
                continue
            limit_amount = Decimal(str(amt_spec.get("amount", "0")))
            if req_amount > limit_amount:
                continue
            creditor_spec = detail.get("creditorAccount", {})
            if creditor_spec.get("iban") != req_creditor:
                continue
            authorized = True
            break

        if not authorized:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "insufficient_authorization_details",
                    "error_description": "parameters exceed granted authorization_details",
                },
            )

        payment_id = f"pmt_{secrets.token_hex(8)}"
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"payment_id": payment_id, "status": "initiated", "amount": str(req_amount)},
        )

    return app
