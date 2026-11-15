"""Where each authorization decision runs: edge scope gate, fetch-then-check, query-bound.

One invoice API, two tenants, and four placements of the object-level check:

- `EDGE_ONLY`: the gateway-style middleware checks the token and a coarse scope; the
  handler loads by primary key and never looks at the tenant (OWASP API1:2023, BOLA).
- `FETCH_THEN_CHECK_403`: load by primary key, then compare tenants in Python and answer
  `403`; the status code itself says "this id exists in another tenant".
- `FETCH_THEN_CHECK_404`: the same load, answering `404`; the status no longer leaks, but
  the foreign row is still read and its relationships still loaded, so the work differs.
- `QUERY_BOUND`: the tenant predicate is part of the `WHERE` clause of every read and of
  the `UPDATE` itself, so a foreign id and a missing id run identical SQL and return
  identical responses, lists page correctly, and there is no check-then-act window.

The middleware (`EdgeScopeGate`) runs before routing, as a gateway does: it sees the
method, the raw path, and the headers, and nothing the router or the database knows.
Everything is in-process: SQLite through aiosqlite, no network, deterministic ids.
"""

# No `from __future__ import annotations`: FastAPI evaluates endpoint annotations, and the
# dependencies are closures inside `create_app`.
import enum
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, UniqueConstraint, event, func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    mapped_column,
    relationship,
    selectinload,
    with_loader_criteria,
)
from sqlalchemy.pool import StaticPool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

READ_SCOPE = "invoices:read"
WRITE_SCOPE = "invoices:write"
ADMIN_SCOPE = "admin"
REALM = "invoices"


class Placement(enum.Enum):
    EDGE_ONLY = "edge-only"
    FETCH_THEN_CHECK_403 = "fetch-then-check-403"
    FETCH_THEN_CHECK_404 = "fetch-then-check-404"
    QUERY_BOUND = "query-bound"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str]


# Authentication is out of scope here: opaque tokens map straight to verified principals.
TOKENS: Mapping[str, Principal] = {
    "tok-alice": Principal("alice", "acme", frozenset({READ_SCOPE, WRITE_SCOPE})),
    "tok-bob": Principal("bob", "globex", frozenset({READ_SCOPE, WRITE_SCOPE})),
    "tok-carol": Principal("carol", "acme", frozenset({READ_SCOPE})),
    "tok-ops": Principal("ops", "acme", frozenset({READ_SCOPE, ADMIN_SCOPE})),
}


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"
    # Uniqueness is per tenant; a global UNIQUE(number) is an existence oracle (see open_store).
    __table_args__ = (UniqueConstraint("tenant_id", "number", name="uq_invoices_tenant_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    number: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="draft")
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", order_by="InvoiceLine.id"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    description: Mapped[str] = mapped_column(String(64))
    invoice: Mapped[Invoice] = relationship(back_populates="lines")


@dataclass
class Store:
    """The database plus two observers: SQL statements sent and invoices built from rows."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    statements: list[str] = field(default_factory=lambda: [])
    materialized: list[int] = field(default_factory=lambda: [])

    def reset(self) -> None:
        self.statements.clear()
        self.materialized.clear()


async def open_store(*, global_unique_numbers: bool = False) -> Store:
    """In-memory SQLite with the lab's seed data; one shared connection (StaticPool)."""
    engine = _create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if global_unique_numbers:
            await conn.execute(
                text("CREATE UNIQUE INDEX ix_invoices_number_global ON invoices (number)")
            )

    class ObservedSession(Session):
        pass

    store = Store(
        engine,
        async_sessionmaker(engine, sync_session_class=ObservedSession, expire_on_commit=False),
    )

    def on_cursor(
        conn: Connection, cursor: Any, statement: str, params: Any, ctx: Any, many: bool
    ) -> None:
        store.statements.append(" ".join(statement.split()))

    def on_load(session: Session, instance: object) -> None:
        if isinstance(instance, Invoice):
            store.materialized.append(instance.id)

    event.listen(engine.sync_engine, "before_cursor_execute", on_cursor)
    event.listen(ObservedSession, "loaded_as_persistent", on_load)
    await _seed(store)
    store.reset()
    return store


SEED: tuple[tuple[int, str, str, int, tuple[str, ...]], ...] = (
    (1, "acme", "A-1", 1200, ("design", "build")),
    (2, "globex", "G-1", 9900, ("audit",)),
    (3, "acme", "A-2", 500, ()),
    (4, "globex", "G-2", 7000, ()),
    (5, "acme", "A-3", 800, ()),
    (6, "acme", "A-4", 300, ()),
)


async def _seed(store: Store) -> None:
    async with store.sessions() as session:
        for invoice_id, tenant, number, cents, lines in SEED:
            invoice = Invoice(id=invoice_id, tenant_id=tenant, number=number, amount_cents=cents)
            invoice.lines = [InvoiceLine(description=d) for d in lines]
            session.add(invoice)
        await session.commit()


def install_tenant_criteria(session: AsyncSession, tenant_id: str) -> None:
    """Add `invoices.tenant_id = :tenant` to every ORM SELECT this session executes.

    `execution_options(skip_tenant=True)` opts a statement out, as admin jobs often do.
    """

    def add_criteria(state: ORMExecuteState) -> None:
        if state.is_select and not state.execution_options.get("skip_tenant", False):
            state.statement = state.statement.options(
                with_loader_criteria(Invoice, Invoice.tenant_id == tenant_id)
            )

    event.listen(session.sync_session, "do_orm_execute", add_criteria)


def bearer_challenge(error: str | None = None, scope: str | None = None) -> str:
    """RFC 6750 Section 3 challenge; no error code when the request had no credentials."""
    params = [f'realm="{REALM}"']
    if error is not None:
        params.append(f'error="{error}"')
    if scope is not None:
        params.append(f'scope="{scope}"')
    return "Bearer " + ", ".join(params)


def _deny(status_code: int, error: str | None, scope: str | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": error or "unauthenticated"},
        status_code=status_code,
        headers={"WWW-Authenticate": bearer_challenge(error, scope)},
    )


class EdgeScopeGate:
    """A gateway-style policy enforcement point: authenticate, then one coarse scope.

    It runs before the router, so it decides from the method, the raw request target, and
    the headers only: no route object, no path parameters, no database rows.
    """

    def __init__(self, app: ASGIApp, *, tokens: Mapping[str, Principal] = TOKENS) -> None:
        self.app = app
        self.tokens = tokens

    def required_scope(self, method: str, raw_path: bytes) -> str:
        # Path rules on the raw target, as many gateways match; the router sees `path`.
        if raw_path.startswith(b"/admin"):
            return ADMIN_SCOPE
        return READ_SCOPE if method in {"GET", "HEAD"} else WRITE_SCOPE

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = b""
        for name, value in scope["headers"]:
            if name == b"authorization":
                authorization = value
                break
        if not authorization:
            await _deny(401, None)(scope, receive, send)
            return
        kind, _, token = authorization.decode("latin-1").partition(" ")
        principal = self.tokens.get(token.strip()) if kind.lower() == "bearer" else None
        if principal is None:
            await _deny(401, "invalid_token")(scope, receive, send)
            return
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode()
        needed = self.required_scope(scope["method"], raw_path)
        if needed not in principal.scopes:
            await _deny(403, "insufficient_scope", needed)(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(min_length=1, max_length=32)
    amount_cents: int = Field(ge=0)


class InvoiceOut(BaseModel):
    id: int
    tenant_id: str
    number: str
    amount_cents: int
    status: str
    lines: list[str]


class InvoiceSummary(BaseModel):
    id: int
    number: str


class PageOut(BaseModel):
    items: list[InvoiceSummary]
    next_after: int | None


def _out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=invoice.id,
        tenant_id=invoice.tenant_id,
        number=invoice.number,
        amount_cents=invoice.amount_cents,
        status=invoice.status,
        lines=[line.description for line in invoice.lines],
    )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")


def _forbidden() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, "invoice belongs to another tenant")


async def load_invoice(
    session: AsyncSession, principal: Principal, invoice_id: int, placement: Placement
) -> Invoice:
    """The object-level check, placed four ways; every path loads `lines` for the response."""
    if placement is Placement.QUERY_BOUND:
        stmt = (
            select(Invoice)
            .where(Invoice.id == invoice_id, Invoice.tenant_id == principal.tenant_id)
            .options(selectinload(Invoice.lines))
        )
        invoice = (await session.scalars(stmt)).one_or_none()
        if invoice is None:
            raise _not_found()
        return invoice
    loaded = await session.get(Invoice, invoice_id, options=[selectinload(Invoice.lines)])
    if loaded is None:
        raise _not_found()
    if placement is Placement.EDGE_ONLY or loaded.tenant_id == principal.tenant_id:
        return loaded
    raise _forbidden() if placement is Placement.FETCH_THEN_CHECK_403 else _not_found()


Hook = Callable[[AsyncSession, int], Awaitable[None]]


def create_app(
    store: Store,
    placement: Placement,
    *,
    route_guard: bool = True,
    between_check_and_act: Hook | None = None,
) -> FastAPI:
    """The invoice API with the object-level check at `placement`.

    `route_guard` adds a route-level `admin` dependency that runs after routing;
    `between_check_and_act` runs between the ownership check and the `UPDATE` in the
    non-query-bound void path, standing in for a concurrent committed transaction.
    """

    def principal_dep(request: Request) -> Principal:
        principal = request.scope.get("state", {}).get("principal")
        if not isinstance(principal, Principal):
            # Fail closed: the edge layer is missing or misordered.
            raise HTTPException(401, "no principal", {"WWW-Authenticate": bearer_challenge()})
        return principal

    async def session_dep() -> AsyncIterator[AsyncSession]:
        async with store.sessions() as session:
            yield session

    def require_scope(scope: str) -> Callable[[Principal], None]:
        def check(principal: Annotated[Principal, Depends(principal_dep)]) -> None:
            if scope not in principal.scopes:
                raise HTTPException(
                    403,
                    "insufficient_scope",
                    {"WWW-Authenticate": bearer_challenge("insufficient_scope", scope)},
                )

        return check

    app = FastAPI()
    app.add_middleware(EdgeScopeGate)

    @app.get("/invoices/{invoice_id}")
    async def read_invoice(
        invoice_id: Annotated[int, Path(ge=1)],
        principal: Annotated[Principal, Depends(principal_dep)],
        session: Annotated[AsyncSession, Depends(session_dep)],
    ) -> InvoiceOut:
        return _out(await load_invoice(session, principal, invoice_id, placement))

    @app.get("/invoices")
    async def list_invoices(
        principal: Annotated[Principal, Depends(principal_dep)],
        session: Annotated[AsyncSession, Depends(session_dep)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> PageOut:
        stmt = select(Invoice).where(Invoice.id > after).order_by(Invoice.id).limit(limit)
        if placement is Placement.QUERY_BOUND:
            stmt = stmt.where(Invoice.tenant_id == principal.tenant_id)
        rows = list((await session.scalars(stmt)).all())
        visible = rows
        if placement in (Placement.FETCH_THEN_CHECK_403, Placement.FETCH_THEN_CHECK_404):
            visible = [row for row in rows if row.tenant_id == principal.tenant_id]
        return PageOut(
            items=[InvoiceSummary(id=row.id, number=row.number) for row in visible],
            next_after=rows[-1].id if len(rows) == limit else None,
        )

    @app.post("/invoices/{invoice_id}/void")
    async def void_invoice(
        invoice_id: Annotated[int, Path(ge=1)],
        principal: Annotated[Principal, Depends(principal_dep)],
        session: Annotated[AsyncSession, Depends(session_dep)],
    ) -> dict[str, int | str]:
        if placement is Placement.QUERY_BOUND:
            voided = await session.execute(
                update(Invoice)
                .where(
                    Invoice.id == invoice_id,
                    Invoice.tenant_id == principal.tenant_id,
                    Invoice.status == "draft",
                )
                .values(status="void")
                .returning(Invoice.id)
            )
            if voided.first() is None:
                # Explain the miss with a query bound to the same tenant, so 409 never
                # reveals another tenant's invoice.
                current = await session.scalar(
                    select(Invoice.status).where(
                        Invoice.id == invoice_id, Invoice.tenant_id == principal.tenant_id
                    )
                )
                await session.rollback()
                if current is None:
                    raise _not_found()
                raise HTTPException(status.HTTP_409_CONFLICT, f"invoice is {current}")
        else:
            invoice = await load_invoice(session, principal, invoice_id, placement)
            if invoice.status != "draft":
                raise HTTPException(status.HTTP_409_CONFLICT, f"invoice is {invoice.status}")
            if between_check_and_act is not None:
                await between_check_and_act(session, invoice_id)
            await session.execute(
                update(Invoice).where(Invoice.id == invoice_id).values(status="void")
            )
        await session.commit()
        return {"id": invoice_id, "status": "void"}

    @app.post("/invoices", status_code=status.HTTP_201_CREATED)
    async def create_invoice(
        body: InvoiceIn,
        principal: Annotated[Principal, Depends(principal_dep)],
        session: Annotated[AsyncSession, Depends(session_dep)],
    ) -> InvoiceOut:
        # tenant_id comes from the verified principal, never from the body.
        invoice = Invoice(
            tenant_id=principal.tenant_id, number=body.number, amount_cents=body.amount_cents
        )
        invoice.lines = []
        session.add(invoice)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "invoice number in use") from None
        return _out(invoice)

    admin_guard = [Depends(require_scope(ADMIN_SCOPE))] if route_guard else []

    @app.get("/admin/stats", dependencies=admin_guard)
    async def admin_stats(session: Annotated[AsyncSession, Depends(session_dep)]) -> dict[str, int]:
        rows = await session.execute(
            select(Invoice.tenant_id, func.count()).group_by(Invoice.tenant_id)
        )
        return {tenant: count for tenant, count in rows}

    return app
