"""Tenant scoping for /v1/orgs/{org}/projects/{project}: path vs token claims and region cells."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, quote_from_bytes

import httpx
import jwt
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

ISSUER = "https://issuer.lab.test"
AUDIENCE = "https://api.lab.test"
LAB_HS256_KEY = b"lab-only-hs256-key-EXAMPLE-NOT-A-REAL-KEY"
ACCESS_TOKEN_TYP = "at+jwt"  # noqa: S105 - a JOSE "typ" value (RFC 9068 Section 2.1)
ACCESS_TOKEN_TYPES = frozenset({ACCESS_TOKEN_TYP, "application/at+jwt"})  # RFC 9068 Section 4
REQUIRED_CLAIMS = ["iss", "exp", "aud", "sub", "client_id", "iat", "jti", "org_id"]
PROBLEM_BASE = "https://api.lab.test/problems/"
REGION_HOSTS: Mapping[str, str] = {"us": "us.api.lab.test", "eu": "eu.api.lab.test"}
PUBLIC_PATHS = frozenset({"/healthz"})

# AIP-122 user-specified resource ID: RFC 1034 label, lower case, at most 63 characters.
RESOURCE_ID_PATTERN = r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$"
RESOURCE_ID = re.compile(RESOURCE_ID_PATTERN)
WILDCARD = "-"  # AIP-159 "any parent" collection ID, valid only where a route documents it.

# Matched against route_path(scope): percent-decoded and without the mount prefix, which is
# the string the router matches.
ORG_PATH = re.compile(r"^/v1/orgs/(?P<org>[^/]+)(?:/projects/(?P<project>[^/]+))?(?:/|$)")
WILDCARD_LIST = re.compile(r"^/v1/orgs/[^/]+/projects/-/documents$")

# Bytes left as they are when a raw request target is copied into a Location: RFC 3986
# unreserved characters are always kept by quote_from_bytes, these are sub-delims, ":", "@",
# "/", and "%" (so existing percent-escapes survive); "?" is added for the query.
PATH_SAFE = "/%!$&'()*+,;=:@"
QUERY_SAFE = PATH_SAFE + "?"

PERMISSION_DENIED_DETAIL = "Permission denied on the requested resource (or it might not exist)."


class TokenError(Exception):
    """The bearer token failed validation; the reason is logged, never returned."""


class UntrustedRedirectError(Exception):
    """A region redirect pointed outside the configured regional origins."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Server-verified tenant identity; built only from a validated token."""

    subject: str
    org: str
    projects: frozenset[str]


@dataclass
class Recorder:
    """What ran: handler invocations and tenant-scoped store reads."""

    handler_calls: list[str] = field(default_factory=list[str])
    store_reads: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])


@dataclass
class DocumentStore:
    """In-memory documents keyed by (org, project, document); every read names its tenant."""

    recorder: Recorder
    rows: dict[tuple[str, str, str], str] = field(default_factory=dict[tuple[str, str, str], str])

    def get(self, ctx: TenantContext, project: str, document: str) -> str | None:
        self.recorder.store_reads.append((ctx.org, project, document))
        return self.rows.get((ctx.org, project, document))

    def list_names(self, ctx: TenantContext, projects: frozenset[str]) -> list[str]:
        return sorted(
            resource_name(org, project, document)
            for (org, project, document) in self.rows
            if org == ctx.org and project in projects
        )

    def create(self, ctx: TenantContext, project: str, document: str, body: str) -> bool:
        key = (ctx.org, project, document)
        if key in self.rows:
            return False
        self.rows[key] = body
        return True


def resource_name(org: str, project: str, document: str) -> str:
    """Canonical AIP-122 relative resource name; the only name a response ever returns."""
    return f"orgs/{org}/projects/{project}/documents/{document}"


def mint_token(
    *,
    org: object,
    projects: object,
    now: int,
    subject: str = "user-1",
    ttl: int = 300,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    key: bytes = LAB_HS256_KEY,
    typ: str = ACCESS_TOKEN_TYP,
    issued_at: object | None = None,
    not_before: object | None = None,
) -> str:
    """Mint an HS256 access token; claim values are untyped so tests can forge shapes."""
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "client_id": "lab-client",
        "iat": now if issued_at is None else issued_at,
        "jti": f"{subject}-{now}",
        "exp": now + ttl,
        "org_id": org,
        "projects": projects,
    }
    if not_before is not None:
        claims["nbf"] = not_before
    return jwt.encode(claims, key, algorithm="HS256", headers={"typ": typ})


def verify_access_token(token: str, *, now: int) -> TenantContext:
    """Validate signature, typ, iss, aud, time claims, and tenant claims, or raise TokenError."""
    try:
        decoded = jwt.decode_complete(
            token,
            LAB_HS256_KEY,
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            # PyJWT judges exp, nbf, and iat by the wall clock; this verifier owns the clock.
            options={
                "require": REQUIRED_CLAIMS,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise TokenError(str(exc)) from exc
    header: dict[str, Any] = decoded["header"]
    payload: dict[str, Any] = decoded["payload"]
    if header.get("typ") not in ACCESS_TOKEN_TYPES:
        raise TokenError("typ is not at+jwt")
    exp: object = payload["exp"]
    if type(exp) is not int or exp <= now:
        raise TokenError("expired or malformed exp")
    for claim in ("iat", "nbf"):
        instant: object = payload.get(claim)
        if instant is not None and (type(instant) is not int or instant > now):
            raise TokenError(f"{claim} is malformed or in the future")
    subject: object = payload["sub"]
    org: object = payload["org_id"]
    projects: object = payload.get("projects", [])
    if not isinstance(subject, str) or not isinstance(org, str) or not RESOURCE_ID.fullmatch(org):
        raise TokenError("malformed sub or org_id")
    if not isinstance(projects, list):
        raise TokenError("malformed projects")
    names: list[str] = []
    for item in cast("list[object]", projects):
        if not isinstance(item, str) or not RESOURCE_ID.fullmatch(item):
            raise TokenError("malformed projects")
        names.append(item)
    return TenantContext(subject=subject, org=org, projects=frozenset(names))


def problem(
    status: int,
    slug: str,
    title: str,
    *,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    **extensions: str,
) -> JSONResponse:
    """RFC 9457 problem details; `extensions` become extension members (Section 3.2)."""
    body: dict[str, object] = {"type": PROBLEM_BASE + slug, "title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    body.update(extensions)
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


def permission_denied() -> JSONResponse:
    """One byte-identical 403 for every tenant denial, so the body is not an existence oracle."""
    return problem(403, "permission-denied", "Permission denied", detail=PERMISSION_DENIED_DETAIL)


def _header_values(scope: Scope, name: bytes) -> list[str]:
    headers: list[tuple[bytes, bytes]] = scope["headers"]
    return [value.decode("latin-1") for key, value in headers if key == name]


def route_path(scope: Scope) -> str:
    """The path the router matches: scope["path"] without the root_path mount prefix."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and (path == root_path or path.startswith(root_path + "/")):
        return path[len(root_path) :]
    return path


def _bearer_token(scope: Scope) -> str | None:
    values = _header_values(scope, b"authorization")
    if len(values) != 1:
        return None
    scheme, _, credentials = values[0].partition(" ")
    if scheme.lower() != "bearer" or not credentials or " " in credentials:
        return None
    return credentials


@dataclass(frozen=True)
class TenantGuard:
    """The single decision procedure both enforcement points share."""

    region: str
    directory: Mapping[str, str]
    clock: Callable[[], int]

    def evaluate(self, scope: Scope) -> TenantContext | Response | None:
        """Return a context to proceed, a Response to reject, or None for a public path."""
        hosts = _header_values(scope, b"host")
        if len(hosts) != 1 or hosts[0].lower().removesuffix(":443") != REGION_HOSTS[self.region]:
            # RFC 9110 Section 7.4: this origin is not the one the request names.
            return problem(421, "misdirected-request", "Misdirected request")
        path = route_path(scope)
        if path in PUBLIC_PATHS:
            return None
        token = _bearer_token(scope)
        if token is None:
            # RFC 6750 Section 3.1: no error code when no credentials were presented.
            return problem(
                401, "unauthenticated", "Unauthenticated", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            ctx = verify_access_token(token, now=self.clock())
        except TokenError:
            return problem(
                401,
                "unauthenticated",
                "Unauthenticated",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        match = ORG_PATH.match(path)
        if match is not None and match["org"] != ctx.org:
            return permission_denied()
        home = self.directory.get(ctx.org)
        if home is None or home not in REGION_HOSTS:
            return permission_denied()
        if home != self.region:
            # Only a verified member of the org reaches this line, so naming its region is safe.
            # raw_path keeps the client's percent-encoding; ASGI servers may omit it.
            raw_path: bytes | None = scope.get("raw_path")
            full_path: str = scope["path"]
            target = quote_from_bytes(raw_path, safe=PATH_SAFE) if raw_path else quote(full_path)
            if not target.startswith("/"):
                # "https://host" + "@evil.test/x" would name another authority.
                return problem(400, "bad-request-target", "Bad request target")
            location = f"https://{REGION_HOSTS[home]}{target}"
            query: bytes = scope["query_string"]
            if query:
                location += "?" + quote_from_bytes(query, safe=QUERY_SAFE)
            return problem(
                307,
                "wrong-region",
                "Organization is homed in another region",
                headers={"Location": location, "Cache-Control": "no-store"},
                home_region=home,
            )
        if match is not None and match["project"] is not None:
            project = match["project"]
            listing = scope["method"] == "GET" and WILDCARD_LIST.fullmatch(path) is not None
            if project not in ctx.projects and not (project == WILDCARD and listing):
                return permission_denied()
        return ctx


class TenantGuardMiddleware:
    """Pure ASGI enforcement: rejects before routing, body read, and validation."""

    def __init__(self, app: ASGIApp, guard: TenantGuard) -> None:
        self.app = app
        self.guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        decision = self.guard.evaluate(scope)
        if isinstance(decision, Response):
            await decision(scope, receive, send)
            return
        if decision is not None:
            scope.setdefault("state", {})["tenant"] = decision
        await self.app(scope, receive, send)


class GuardRejectedError(Exception):
    """Carries the guard's response out of a FastAPI dependency."""

    def __init__(self, response: Response) -> None:
        super().__init__("tenant guard rejected the request")
        self.response = response


def current_tenant(request: Request) -> TenantContext:
    """Hand the verified context to a handler; fail closed if the guard and router disagree."""
    ctx: object = getattr(request.state, "tenant", None)
    if not isinstance(ctx, TenantContext):
        raise RuntimeError("tenant guard did not run for this route")
    # Backstop: the guard parsed the path with a regex, the router with its own; re-check the
    # router's parameters so a mismatch between the two denies instead of crossing tenants.
    org: object = request.path_params.get("org", ctx.org)
    project: object = request.path_params.get("project")
    if org != ctx.org or (project is not None and project not in ctx.projects):
        raise GuardRejectedError(permission_denied())
    return ctx


Tenant = Annotated[TenantContext, Depends(current_tenant)]


class DocumentIn(BaseModel):
    """Create request; the caller picks the ID (AIP-122 user-specified resource ID)."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(pattern=RESOURCE_ID_PATTERN)
    body: str = Field(max_length=4096)


def build_cell_app(
    *,
    region: str,
    directory: Mapping[str, str],
    store: DocumentStore,
    clock: Callable[[], int],
    enforcement: Literal["middleware", "dependency"] = "middleware",
) -> FastAPI:
    """One regional cell of the API, guarded at the ASGI edge or by a router dependency."""
    guard = TenantGuard(region=region, directory=directory, clock=clock)
    recorder = store.recorder
    app = FastAPI()

    def dependency_guard(request: Request) -> None:
        decision = guard.evaluate(request.scope)
        if isinstance(decision, Response):
            raise GuardRejectedError(decision)
        request.state.tenant = decision

    if enforcement == "middleware":
        app.add_middleware(TenantGuardMiddleware, guard=guard)
        router = APIRouter(prefix="/v1")
    else:
        router = APIRouter(prefix="/v1", dependencies=[Depends(dependency_guard)])

    @app.exception_handler(GuardRejectedError)
    async def guard_rejected(_request: Request, exc: GuardRejectedError) -> Response:
        return exc.response

    @router.get("/orgs/{org}/projects/-/documents")
    async def list_across_projects(org: str, ctx: Tenant) -> dict[str, list[str]]:
        recorder.handler_calls.append("list_across_projects")
        # AIP-159: the wildcard spans the caller's projects, never the org's.
        return {"documents": store.list_names(ctx, ctx.projects)}

    @router.get("/orgs/{org}/projects/{project}/documents/{document}")
    async def get_document(org: str, project: str, document: str, ctx: Tenant) -> Response:
        recorder.handler_calls.append("get_document")
        body = store.get(ctx, project, document)
        if body is None:
            return problem(404, "not-found", "Document not found")
        return JSONResponse({"name": resource_name(ctx.org, project, document), "body": body})

    @router.post("/orgs/{org}/projects/{project}/documents", status_code=201)
    async def create_document(org: str, project: str, payload: DocumentIn, ctx: Tenant) -> Response:
        recorder.handler_calls.append("create_document")
        if not store.create(ctx, project, payload.document_id, payload.body):
            return problem(409, "already-exists", "Document already exists")
        name = resource_name(ctx.org, project, payload.document_id)
        return JSONResponse({"name": name}, status_code=201)

    @router.get("/documents/{document}")
    async def get_document_by_selector(
        document: str, ctx: Tenant, lab_project: Annotated[str | None, Header()] = None
    ) -> Response:
        recorder.handler_calls.append("get_document_by_selector")
        # The header selects among the token's projects; it never grants one.
        if lab_project is None:
            if len(ctx.projects) != 1:
                return problem(400, "ambiguous-project", "Lab-Project header is required")
            (project,) = ctx.projects
        elif lab_project in ctx.projects:
            project = lab_project
        else:
            return permission_denied()
        body = store.get(ctx, project, document)
        if body is None:
            return problem(404, "not-found", "Document not found")
        return JSONResponse({"name": resource_name(ctx.org, project, document), "body": body})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"region": region}

    app.include_router(router)
    return app


async def send_region_aware(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    token: str,
    payload: object | None = None,
) -> httpx.Response:
    """Send once, and re-send once with credentials if a known regional origin redirects us."""
    headers = {"Authorization": f"Bearer {token}"}
    content = None if payload is None else json.dumps(payload).encode()
    if content is not None:
        headers["Content-Type"] = "application/json"
    response = await client.request(
        method, url, headers=headers, content=content, follow_redirects=False
    )
    if response.status_code != 307:
        return response
    location = response.headers.get("Location")
    if location is None:
        raise UntrustedRedirectError("307 without Location")
    target = httpx.URL(location)
    # An origin is scheme, host, and port: httpx reports port None for the scheme default.
    trusted_origin = (
        target.scheme == "https"
        and target.host in REGION_HOSTS.values()
        and target.port is None
        and not target.userinfo
    )
    if not trusted_origin:
        raise UntrustedRedirectError(location)
    return await client.request(
        method, target, headers=headers, content=content, follow_redirects=False
    )
