"""FastAPI security primitives: Security() scope propagation, scheme extraction, OpenAPI wiring."""

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import (
    APIKeyHeader,
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    SecurityScopes,
)
from pydantic import BaseModel

# Opaque in-memory tokens; a real resource server verifies a JWT or introspects (RFC 7662).
TOKEN_GRANTS: dict[str, frozenset[str]] = {
    "tok-reader": frozenset({"orders:read"}),
    "tok-writer": frozenset({"orders:read", "orders:write"}),
}

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/token",
    scopes={"orders:read": "Read orders", "orders:write": "Create orders"},
)
http_bearer = HTTPBearer(bearerFormat="opaque")
api_key_header = APIKeyHeader(name="X-API-Key")
basic_optional = HTTPBasic(auto_error=False)


@dataclass
class SecurityRecorder:
    """Deterministic log of what each security dependency saw during a request."""

    scope_log: list[list[str]] = field(default_factory=list[list[str]])
    tokens_seen: list[str] = field(default_factory=list[str])
    request_id_calls: int = 0


@dataclass(frozen=True)
class Principal:
    token: str
    granted: frozenset[str]


class NoteIn(BaseModel):
    text: str


def bearer_challenge(security_scopes: SecurityScopes, error: str) -> str:
    """Build an RFC 6750 Section 3 challenge; scope is listed only when the chain requires one."""
    params = [f'error="{error}"']
    if security_scopes.scopes:
        params.append(f'scope="{security_scopes.scope_str}"')
    return "Bearer " + ", ".join(params)


def build_security_app(recorder: SecurityRecorder) -> FastAPI:
    app = FastAPI()

    def request_id() -> str:
        # Plain dependency: no scopes in its subtree, so its cache key ignores scopes.
        recorder.request_id_calls += 1
        return f"req-{recorder.request_id_calls}"

    async def get_current_user(
        security_scopes: SecurityScopes,
        token: Annotated[str, Depends(oauth2_scheme)],
        rid: Annotated[str, Depends(request_id)],
    ) -> Principal:
        recorder.scope_log.append(list(security_scopes.scopes))
        recorder.tokens_seen.append(token)
        granted = TOKEN_GRANTS.get(token)
        if granted is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": bearer_challenge(security_scopes, "invalid_token")},
            )
        missing = [s for s in security_scopes.scopes if s not in granted]
        if missing:
            # RFC 6750 Section 3.1: insufficient_scope SHOULD be 403, not 401.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing scopes: {' '.join(missing)}",
                headers={
                    "WWW-Authenticate": bearer_challenge(security_scopes, "insufficient_scope")
                },
            )
        return Principal(token=token, granted=granted)

    async def get_active_user(
        user: Annotated[Principal, Security(get_current_user, scopes=["orders:read"])],
    ) -> Principal:
        return user

    @app.post("/token")
    async def issue_token(
        form: Annotated[OAuth2PasswordRequestForm, Depends()],
    ) -> dict[str, object]:
        # Echo only: shows how the form parses scope. RFC 9700 Section 2.4 forbids this grant.
        return {"username": form.username, "scopes": form.scopes, "grant_type": form.grant_type}

    @app.get("/orders")
    async def list_orders(
        user: Annotated[Principal, Security(get_current_user, scopes=["orders:read"])],
    ) -> dict[str, str]:
        return {"token": user.token}

    @app.post("/orders")
    async def create_order(
        user: Annotated[Principal, Security(get_active_user, scopes=["orders:write"])],
    ) -> dict[str, str]:
        return {"token": user.token}

    @app.post("/notes")
    async def create_note(
        user: Annotated[Principal, Security(get_current_user, scopes=["orders:write"])],
        note: NoteIn,
    ) -> dict[str, str]:
        # The JSON body is parsed before solve_dependencies runs the security chain.
        return {"token": user.token, "text": note.text}

    @app.get("/report")
    async def report(
        reader: Annotated[Principal, Security(get_current_user, scopes=["orders:read"])],
        writer: Annotated[Principal, Security(get_current_user, scopes=["orders:write"])],
        again: Annotated[Principal, Security(get_current_user, scopes=["orders:read"])],
    ) -> dict[str, bool]:
        return {"same_reader_object": reader is again, "reader_is_writer": reader is writer}

    @app.get("/bearer")
    async def bearer_only(
        creds: Annotated[HTTPAuthorizationCredentials, Depends(http_bearer)],
    ) -> dict[str, str]:
        return {"scheme": creds.scheme, "credentials": creds.credentials}

    @app.get("/apikey")
    async def api_key_only(key: Annotated[str, Depends(api_key_header)]) -> dict[str, str]:
        return {"key": key}

    @app.get("/both")
    async def both_schemes(
        creds: Annotated[HTTPAuthorizationCredentials, Depends(http_bearer)],
        key: Annotated[str, Depends(api_key_header)],
    ) -> dict[str, str]:
        return {"credentials": creds.credentials, "key": key}

    @app.get("/basic-optional")
    async def basic_optional_route(
        creds: Annotated[HTTPBasicCredentials | None, Depends(basic_optional)],
    ) -> dict[str, str | None]:
        return {"username": creds.username if creds else None}

    # Exposed so tests can register dependency_overrides against the exact callable.
    app.state.get_current_user = get_current_user
    return app


def build_colliding_schemes_app() -> FastAPI:
    """Two OAuth2PasswordBearer instances share the default scheme_name (the class name)."""
    app = FastAPI()
    first = OAuth2PasswordBearer(tokenUrl="/first-token")
    second = OAuth2PasswordBearer(tokenUrl="/second-token")

    @app.get("/a")
    async def route_a(token: Annotated[str, Depends(first)]) -> dict[str, str]:
        return {"token": token}

    @app.get("/b")
    async def route_b(token: Annotated[str, Depends(second)]) -> dict[str, str]:
        return {"token": token}

    return app
