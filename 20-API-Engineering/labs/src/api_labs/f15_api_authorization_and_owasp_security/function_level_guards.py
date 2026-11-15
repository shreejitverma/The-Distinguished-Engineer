"""Function-level authorization (OWASP API5:2023) in FastAPI: deny-by-default guards.

An `AccessControl` instance is one app-wide dependency.
Every endpoint function declares an `AccessRule` with `@acl.rule(...)`; the dependency
looks up the rule of the matched endpoint (`scope["endpoint"]`) and refuses any function
that has none, so a forgotten declaration fails closed.
`audit_routes` walks the effective route tree with `fastapi.routing.iter_route_contexts`
and reports every route the guard does not cover; tests and the lifespan gate call it.
"""

from collections.abc import AsyncGenerator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, TypeVar, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from starlette.requests import HTTPConnection

SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
DOCS_PATHS: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    roles: frozenset[str]


# Opaque bearer tokens -> principals; a real service verifies a JWT or introspects.
TOKENS: Final[dict[str, Principal]] = {
    "tok-alice": Principal("alice", frozenset({"user"})),
    "tok-sam": Principal("sam", frozenset({"user", "support"})),
    "tok-ops": Principal("ops", frozenset({"user", "admin"})),
}


def authenticate(conn: HTTPConnection) -> Principal | None:
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return TOKENS.get(token)


@dataclass(frozen=True, slots=True)
class AccessRule:
    """`public` needs nothing; otherwise authenticated, plus one of `any_of` if non-empty."""

    public: bool = False
    any_of: frozenset[str] = frozenset()


@dataclass(eq=False)
class AccessControl:
    """The single function-level policy point; `floor` roles are required on every route."""

    floor: frozenset[str] = frozenset()
    _rules: dict[Callable[..., Any], AccessRule] = field(default_factory=dict[Any, AccessRule])

    def rule(self, *, public: bool = False, any_of: Iterable[str] = ()) -> Callable[[F], F]:
        declared = AccessRule(public=public, any_of=frozenset(any_of))
        if public and (declared.any_of or self.floor):
            raise ValueError("a public rule cannot require roles or sit under a role floor")

        def mark(endpoint: F) -> F:
            self._rules[endpoint] = declared
            return endpoint

        return mark

    def rule_for(self, endpoint: object) -> AccessRule | None:
        return self._rules.get(cast("Callable[..., Any]", endpoint))

    async def __call__(self, conn: HTTPConnection) -> Principal | None:
        rule = self.rule_for(conn.scope.get("endpoint"))
        if rule is None:  # deny by default: no declaration, no access, for everyone
            raise HTTPException(status.HTTP_403_FORBIDDEN, "function has no access rule")
        if rule.public:
            return None
        principal = authenticate(conn)
        if principal is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        for required in (self.floor, rule.any_of):
            if required and required.isdisjoint(principal.roles):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        conn.state.principal = principal
        return principal


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    path: str
    methods: str
    problem: str


def dependency_calls(dependant: Dependant) -> Iterator[object]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from dependency_calls(sub)


def audit_routes(
    app: FastAPI, acl: AccessControl, *, allow_unguarded: frozenset[str] = frozenset()
) -> list[Finding]:
    """Every route the guard does not cover, from the effective (post-include) route tree."""
    findings: list[Finding] = []
    if app.dependency_overrides:
        findings.append(Finding("*", "", "dependency_overrides is not empty"))
    for ctx in iter_route_contexts(app.routes):
        path, methods = ctx.path or "?", frozenset(ctx.methods or ())
        label = ",".join(sorted(methods))
        if not isinstance(ctx.original_route, APIRoute | APIWebSocketRoute):
            if path not in allow_unguarded:
                kind = type(ctx.original_route).__name__
                findings.append(Finding(path, label, f"{kind} never runs FastAPI dependencies"))
            continue
        dependant = cast("Dependant", ctx.dependant)
        if not any(call is acl for call in dependency_calls(dependant)):
            findings.append(Finding(path, label, "access control is not in the dependency graph"))
        rule = acl.rule_for(ctx.endpoint)
        if rule is None:
            findings.append(Finding(path, label, "endpoint has no access rule"))
        elif rule.public and methods - SAFE_METHODS:
            findings.append(Finding(path, label, "public rule on an unsafe method"))
        elif methods & SAFE_METHODS and methods - SAFE_METHODS:
            findings.append(Finding(path, label, "one rule covers safe and unsafe methods"))
    return sorted(findings)


def naive_audit_routes(app: FastAPI, acl: AccessControl) -> list[Finding]:
    """The pre-0.137 idiom: a flat walk of `app.routes` looking at `APIRoute.dependant`."""
    findings: list[Finding] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and not any(
            call is acl for call in dependency_calls(route.dependant)
        ):
            findings.append(Finding(route.path, ",".join(sorted(route.methods or ())), "unguarded"))
    return findings


class RouteAuditError(RuntimeError):
    pass


def audited_lifespan(
    acl: AccessControl, *, allow_unguarded: frozenset[str] = frozenset()
) -> Callable[[FastAPI], Any]:
    """Refuse to start (lifespan startup fails) while any route is uncovered."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        findings = audit_routes(app, acl, allow_unguarded=allow_unguarded)
        if findings:
            raise RouteAuditError("; ".join(f"{f.path} {f.problem}" for f in findings))
        yield

    return lifespan


@dataclass
class Store:
    """Records which handlers' data loads ran, to prove denial happens before work."""

    reads: list[str] = field(default_factory=list[str])
    roles: dict[str, set[str]] = field(
        default_factory=lambda: {"alice": {"user"}, "bob": {"user"}, "ops": {"user", "admin"}}
    )
    locked: set[str] = field(default_factory=lambda: {"bob"})


def build_public_app(store: Store, acl: AccessControl, *, docs: bool = False) -> FastAPI:
    """The internet-facing surface: user functions plus two privileged ones under user paths."""
    app = FastAPI(
        dependencies=[Depends(acl)],
        openapi_url="/openapi.json" if docs else None,
        lifespan=audited_lifespan(acl, allow_unguarded=DOCS_PATHS if docs else frozenset()),
    )

    def load_users() -> dict[str, set[str]]:
        store.reads.append("users")
        return store.roles

    @app.get("/health")
    @acl.rule(public=True)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/me")
    @acl.rule()
    async def me(conn: HTTPConnection) -> dict[str, str]:
        return {"subject": conn.state.principal.subject}

    @app.post("/invites", status_code=status.HTTP_201_CREATED)
    @acl.rule(any_of={"admin"})  # OWASP API5 scenario 1: an admin function on a user path
    async def create_invite(role: str = "user") -> dict[str, str]:
        return {"invite": "inv-1", "role": role}

    @app.post("/users/{name}/unlock")
    @acl.rule(any_of={"support", "admin"})
    async def unlock(
        name: str, users: Annotated[dict[str, set[str]], Depends(load_users)]
    ) -> dict[str, str]:
        if name not in users:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        store.locked.discard(name)
        return {"unlocked": name}

    return app


def build_admin_router(store: Store, acl: AccessControl) -> APIRouter:
    router = APIRouter(prefix="/admin")

    def load_users() -> dict[str, set[str]]:
        store.reads.append("users")
        return store.roles

    @router.get("/users")
    @acl.rule(any_of={"admin"})  # OWASP API5 scenario 2: GET /api/admin/v1/users/all
    async def list_users(
        users: Annotated[dict[str, set[str]], Depends(load_users)],
    ) -> dict[str, list[str]]:
        return {name: sorted(roles) for name, roles in users.items()}

    @router.put("/users/{name}/roles")
    @acl.rule(any_of={"admin"})
    async def set_roles(name: str, roles: list[str]) -> dict[str, list[str]]:
        store.roles[name] = set(roles)
        return {name: sorted(store.roles[name])}

    return router


def build_admin_app(store: Store) -> FastAPI:
    """The administrative surface as its own ASGI app, served on a separate listener."""
    acl = AccessControl(floor=frozenset({"admin"}))
    app = FastAPI(
        dependencies=[Depends(acl)],
        openapi_url=None,
        lifespan=audited_lifespan(acl),
    )
    app.include_router(build_admin_router(store, acl))
    return app
