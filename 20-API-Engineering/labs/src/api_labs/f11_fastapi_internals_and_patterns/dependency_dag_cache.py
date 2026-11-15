"""FastAPI solve_dependencies(): diamond DAG traversal, the cache key, and use_cache."""

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

import anyio.lowlevel
from fastapi import APIRouter, Depends, FastAPI, Security
from fastapi.dependencies.models import _get_cache_key  # pyright: ignore[reportPrivateUsage]
from fastapi.routing import APIRoute
from fastapi.security import SecurityScopes

CacheKeyView = tuple[str, tuple[str, ...], str]


@dataclass(eq=False)
class CallLog:
    """Ordered record of every dependency call; eq=False keeps identity hash and eq."""

    events: list[str] = field(default_factory=list[str])

    def record(self, event: str) -> None:
        self.events.append(event)

    def count(self, event: str) -> int:
        return self.events.count(event)


@dataclass(frozen=True)
class DagLab:
    """The app under test plus the shared leaf callable, so tests can override it."""

    app: FastAPI
    shared: Callable[[], Awaitable[object]]


def build_dag_app(log: CallLog) -> DagLab:
    """Build routes whose dependency graphs isolate one cache rule each."""
    app = FastAPI()

    # Shared leaf D of the diamond A -> B, A -> C, B -> D, C -> D.
    async def shared() -> object:
        log.record("shared")
        return object()

    async def left(d: Annotated[object, Depends(shared)]) -> object:
        log.record("left")
        return d

    async def right(d: Annotated[object, Depends(shared)]) -> object:
        log.record("right")
        return d

    async def left_fresh(d: Annotated[object, Depends(shared, use_cache=False)]) -> object:
        log.record("left_fresh")
        return d

    async def right_fresh(d: Annotated[object, Depends(shared, use_cache=False)]) -> object:
        log.record("right_fresh")
        return d

    @app.get("/diamond")
    async def diamond(
        b: Annotated[object, Depends(left)], c: Annotated[object, Depends(right)]
    ) -> dict[str, bool]:
        return {"same_instance": b is c}

    @app.get("/fresh-last")
    async def fresh_last(
        b: Annotated[object, Depends(left)], c: Annotated[object, Depends(right_fresh)]
    ) -> dict[str, bool]:
        return {"same_instance": b is c}

    @app.get("/fresh-first")
    async def fresh_first(
        b: Annotated[object, Depends(left_fresh)], c: Annotated[object, Depends(right)]
    ) -> dict[str, bool]:
        return {"same_instance": b is c}

    # Security scopes enter the key only when the dependant (or a sub-dependant) uses them.
    async def scoped_principal(security_scopes: SecurityScopes) -> str:
        log.record(f"scoped_principal:{security_scopes.scope_str}")
        return security_scopes.scope_str

    @app.get("/scopes-split")
    async def scopes_split(
        r: Annotated[str, Security(scoped_principal, scopes=["read"])],
        w: Annotated[str, Security(scoped_principal, scopes=["write"])],
    ) -> dict[str, str]:
        return {"first": r, "second": w}

    @app.get("/scopes-order")
    async def scopes_order(
        ab: Annotated[str, Security(scoped_principal, scopes=["a", "b"])],
        ba: Annotated[str, Security(scoped_principal, scopes=["b", "a"])],
    ) -> dict[str, str]:
        return {"first": ab, "second": ba}

    # Inherited (parent) scopes do not enter the key of a leaf that never reads them.
    async def read_guard(d: Annotated[object, Depends(shared)]) -> object:
        return d

    async def write_guard(d: Annotated[object, Depends(shared)]) -> object:
        return d

    @app.get("/scopes-ignored")
    async def scopes_ignored(
        r: Annotated[object, Security(read_guard, scopes=["read"])],
        w: Annotated[object, Security(write_guard, scopes=["write"])],
    ) -> dict[str, bool]:
        return {"same_instance": r is w}

    # A generator's computed scope ("request" by default) is the third key element.
    async def session() -> AsyncIterator[str]:
        log.record("session:enter")
        yield "session"
        log.record("session:exit")

    @app.get("/generator-scope")
    async def generator_scope(
        req: Annotated[str, Depends(session)],
        fn: Annotated[str, Depends(session, scope="function")],
    ) -> dict[str, str]:
        return {"request": req, "function": fn}

    # A cache hit on D still re-solves D's own sub-dependencies first.
    async def fresh_child() -> str:
        log.record("fresh_child")
        return "child"

    async def parent(c: Annotated[str, Depends(fresh_child, use_cache=False)]) -> str:
        log.record("parent")
        return c

    async def via_left(p: Annotated[str, Depends(parent)]) -> str:
        return p

    async def via_right(p: Annotated[str, Depends(parent)]) -> str:
        return p

    @app.get("/rewalk")
    async def rewalk(
        a: Annotated[str, Depends(via_left)], b: Annotated[str, Depends(via_right)]
    ) -> dict[str, str]:
        return {"left": a, "right": b}

    # Siblings are awaited one after another, never concurrently.
    async def slow_first() -> str:
        log.record("slow_first:start")
        await anyio.lowlevel.checkpoint()
        log.record("slow_first:end")
        return "first"

    async def slow_second() -> str:
        log.record("slow_second:start")
        await anyio.lowlevel.checkpoint()
        log.record("slow_second:end")
        return "second"

    @app.get("/sequential")
    async def sequential(
        a: Annotated[str, Depends(slow_first)], b: Annotated[str, Depends(slow_second)]
    ) -> list[str]:
        return [a, b]

    # A leaf with a required query parameter, reached twice through the diamond.
    async def shared_query(q: int) -> int:
        log.record("shared_query")
        return q

    async def left_q(v: Annotated[int, Depends(shared_query)]) -> int:
        return v

    async def right_q(v: Annotated[int, Depends(shared_query)]) -> int:
        return v

    @app.get("/diamond-query")
    async def diamond_query(
        a: Annotated[int, Depends(left_q)], b: Annotated[int, Depends(right_q)]
    ) -> int:
        return a + b

    # Router, decorator, and parameter declarations of one callable share one cache entry.
    router = APIRouter(dependencies=[Depends(shared)])

    @router.get("/router-level", dependencies=[Depends(shared)])
    async def router_level(d: Annotated[object, Depends(shared)]) -> dict[str, bool]:
        return {"ok": d is not None}

    app.include_router(router)

    # get_parameterless_sub_dependant() drops use_cache: dependencies=[...] entries always cache.
    @app.get("/parameterless-fresh", dependencies=[Depends(shared, use_cache=False)])
    async def parameterless_fresh(d: Annotated[object, Depends(shared)]) -> dict[str, bool]:
        return {"ok": d is not None}

    return DagLab(app=app, shared=shared)


@dataclass
class MutableRoleGuard:
    """eq=True without frozen=True sets __hash__ = None: unusable as a cache key."""

    role: str

    async def __call__(self) -> str:
        return self.role  # pragma: no cover - the cache lookup raises first


@dataclass(frozen=True)
class FrozenRoleGuard:
    """Frozen dataclass: hash and eq by field, so equal instances share one entry."""

    role: str
    log: CallLog

    async def __call__(self) -> str:
        self.log.record(f"guard:{self.role}")
        return self.role


@dataclass(frozen=True)
class TenantGuard:
    """Frozen dataclass whose state field is excluded from eq and hash."""

    tenant: str
    log: CallLog = field(compare=False)

    async def __call__(self) -> str:
        self.log.record(f"tenant:{self.tenant}")
        return self.tenant


def build_tenant_app(log: CallLog, tenant: str) -> FastAPI:
    """One route whose Annotated metadata holds a TenantGuard bound to this log."""
    app = FastAPI()

    @app.get("/tenant")
    async def tenant_route(t: Annotated[str, Depends(TenantGuard(tenant, log))]) -> str:
        return t

    return app


def build_callable_key_app(log: CallLog) -> FastAPI:
    """Routes keyed by callable instances rather than functions."""
    app = FastAPI()

    @app.get("/mutable-guard")
    async def mutable_guard(r: Annotated[str, Depends(MutableRoleGuard("admin"))]) -> str:
        return r  # pragma: no cover - the cache lookup raises first

    @app.get("/frozen-guards")
    async def frozen_guards(
        a: Annotated[str, Depends(FrozenRoleGuard("admin", log))],
        b: Annotated[str, Depends(FrozenRoleGuard("admin", log))],
        c: Annotated[str, Depends(FrozenRoleGuard("user", log))],
    ) -> list[str]:
        return [a, b, c]

    return app


def dependency_cache_keys(app: FastAPI, path: str) -> list[CacheKeyView]:
    """Return (callable name, scopes, computed scope) for every sub-dependant, depth first."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path:
            views: list[CacheKeyView] = []
            pending = list(reversed(route.dependant.dependencies))
            while pending:
                sub = pending.pop()
                call, scopes, scope = _get_cache_key(dependant=sub)
                views.append((getattr(call, "__name__", repr(call)), scopes, scope))
                pending.extend(reversed(sub.dependencies))
            return views
    raise LookupError(path)


def make_self_referential_dependency() -> Callable[[], object]:
    """Return a callable whose signature depends on itself: a cycle in the graph."""

    def loop() -> object:
        return object()  # pragma: no cover - never called; registration fails first

    loop.__dict__["__signature__"] = inspect.Signature(
        [inspect.Parameter("again", inspect.Parameter.KEYWORD_ONLY, default=Depends(loop))]
    )
    return loop
