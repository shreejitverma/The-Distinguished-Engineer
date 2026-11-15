"""Tenant-scoped data access that makes BOLA (OWASP API1:2023) structurally hard.

Three layers, each closing a gap the others leave open:

1. ``TenantSession``: a SQLAlchemy ``Session`` subclass that cannot be built without a tenant.
   A ``do_orm_execute`` listener adds ``with_loader_criteria(TenantScoped, ...)`` to every ORM
   SELECT, UPDATE, and DELETE, and a ``before_flush`` listener stamps and pins ``tenant_id``.
2. The schema: per-tenant unique keys and composite ``(tenant_id, id)`` foreign keys, so the
   database refuses a cross-tenant reference even when application code forgets.
3. ``after_begin`` publishes the tenant with ``set_config('app.tenant_id', :t, true)`` so that
   PostgreSQL row-level security policies see it; SQLite gets a recording shim for the lab.

The HTTP layer resolves the principal from the bearer token, never from the URL, and answers
``404`` for both "absent" and "belongs to another tenant".
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    SessionTransaction,
    mapped_column,
    relationship,
    with_loader_criteria,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.elements import ColumnElement

TENANT_GUC = "app.tenant_id"


class TenantScopeError(RuntimeError):
    """A write tried to create or move a row outside the session's tenant."""


class Base(DeclarativeBase):
    pass


class TenantScoped:
    """Mixin: every row carries the tenant that owns it. Not mapped by itself."""

    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str]


class Customer(TenantScoped, Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")


class Invoice(TenantScoped, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        # Per-tenant uniqueness: a global UNIQUE(number) would let tenant A learn that tenant B
        # already issued "INV-1" from the constraint violation (a covert existence channel).
        UniqueConstraint("tenant_id", "number"),
        # Composite FK: an invoice can only point at a customer of the same tenant.
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str]
    amount_cents: Mapped[int]
    customer_id: Mapped[int]
    customer: Mapped[Customer] = relationship(back_populates="invoices")


class TenantSession(Session):
    """A Session bound to exactly one tenant for its whole life (one request)."""

    def __init__(self, bind: Engine, *, tenant_id: str) -> None:
        if not tenant_id:
            raise TenantScopeError("a TenantSession needs a non-empty tenant_id")
        super().__init__(bind=bind, info={"tenant_id": tenant_id})

    @property
    def tenant_id(self) -> str:
        tenant: str = self.info["tenant_id"]
        return tenant


@dataclass(slots=True)
class GuardConfig:
    """Knobs the tests flip to prove each one matters; production keeps the defaults."""

    include_aliases: bool = True
    propagate_to_loaders: bool = True
    publish_guc_local: bool = True


GUARD = GuardConfig()


@event.listens_for(TenantSession, "do_orm_execute")
def _scope_orm_statements(state: ORMExecuteState) -> None:
    """Add ``tenant_id = :tenant`` to every TenantScoped entity in SELECT, UPDATE, DELETE."""
    if state.is_column_load or state.is_relationship_load:
        return  # already carries the option via propagate_to_loaders
    if not (state.is_select or state.is_update or state.is_delete):
        return
    tenant: str = state.session.info["tenant_id"]

    def same_tenant(cls: type[TenantScoped]) -> ColumnElement[bool]:
        return cls.tenant_id == tenant

    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            same_tenant,
            include_aliases=GUARD.include_aliases,
            propagate_to_loaders=GUARD.propagate_to_loaders,
        )
    )


@event.listens_for(TenantSession, "before_flush")
def _stamp_and_pin_tenant(session: Session, flush_context: object, instances: object) -> None:
    """New rows get the session tenant; existing rows may never change tenant."""
    tenant = session.info["tenant_id"]
    for obj in session.new:
        if isinstance(obj, TenantScoped):
            if obj.tenant_id is None:  # pyright: ignore[reportUnnecessaryComparison]
                obj.tenant_id = tenant
            elif obj.tenant_id != tenant:
                raise TenantScopeError(f"insert into tenant {obj.tenant_id!r} from {tenant!r}")
    for obj in session.dirty:
        if isinstance(obj, TenantScoped) and inspect(obj).attrs["tenant_id"].history.deleted:
            raise TenantScopeError("tenant_id is immutable once written")


@event.listens_for(TenantSession, "after_begin")
def _publish_tenant_guc(session: Session, transaction: SessionTransaction, connection: Any) -> None:
    """Make the tenant visible to PostgreSQL RLS for this transaction only.

    ``SET LOCAL`` cannot take a bind parameter, so ``set_config(name, value, true)`` is the
    injection-safe spelling of ``SET LOCAL app.tenant_id = ...``.
    """
    connection.execute(
        select(func.set_config(TENANT_GUC, session.info["tenant_id"], GUARD.publish_guc_local))
    )


@dataclass(slots=True)
class GucLog:
    """What the SQLite ``set_config`` shim was asked to do, in call order."""

    calls: list[tuple[str, str, bool]] = field(default_factory=lambda: [])


def make_engine() -> tuple[Engine, GucLog]:
    """One shared in-memory SQLite connection with FK enforcement and a ``set_config`` shim."""
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    log = GucLog()

    def set_config(name: str, value: str, is_local: int) -> str:
        log.calls.append((name, value, bool(is_local)))
        return value

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: sqlite3.Connection, record: object) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys = ON")
        dbapi_conn.create_function("set_config", 3, set_config)

    Base.metadata.create_all(engine)
    return engine, log


# --- HTTP layer -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    tenant_id: str


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    number: str
    amount_cents: int


class InvoicePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_cents: int = Field(ge=0)


class InvoiceRepository:
    """The only way handlers reach invoices; it cannot be constructed without a tenant."""

    def __init__(self, session: TenantSession) -> None:
        self._session = session

    def get(self, invoice_id: int) -> "Invoice | None":
        stmt = select(Invoice).where(Invoice.id == invoice_id)
        return self._session.scalars(stmt).one_or_none()

    def list(self) -> list[Invoice]:
        return list(self._session.scalars(select(Invoice).order_by(Invoice.id)))


def create_app(engine: Engine, tokens: dict[str, Principal], *, scoped: bool = True) -> FastAPI:
    """``scoped=False`` builds the classic BOLA bug: a plain Session and ``session.get``."""
    app = FastAPI()

    def principal(authorization: Annotated[str, Header()] = "") -> Principal:
        scheme, _, token = authorization.partition(" ")
        found = tokens.get(token) if scheme.lower() == "bearer" else None
        if found is None:
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return found

    def tenant_session(who: Annotated[Principal, Depends(principal)]) -> Iterator[Session]:
        session = TenantSession(engine, tenant_id=who.tenant_id) if scoped else Session(engine)
        with session, session.begin():
            yield session

    def load(db: Session, invoice_id: int) -> Invoice:
        if isinstance(db, TenantSession):
            found = InvoiceRepository(db).get(invoice_id)
        else:
            found = db.get(Invoice, invoice_id)  # BOLA: trusts the client-supplied id
        if found is None:
            # Same status and body as "no such id": a 403 here would confirm the id exists.
            raise HTTPException(status_code=404, detail="Not Found")
        return found

    @app.get("/invoices/{invoice_id}")
    def read_invoice(
        invoice_id: int, db: Annotated[Session, Depends(tenant_session)]
    ) -> InvoiceOut:
        return InvoiceOut.model_validate(load(db, invoice_id))

    @app.patch("/invoices/{invoice_id}")
    def patch_invoice(
        invoice_id: int, patch: InvoicePatch, db: Annotated[Session, Depends(tenant_session)]
    ) -> InvoiceOut:
        invoice = load(db, invoice_id)
        invoice.amount_cents = patch.amount_cents
        db.flush()
        return InvoiceOut.model_validate(invoice)

    @app.get("/invoices")
    def list_invoices(db: Annotated[Session, Depends(tenant_session)]) -> list[InvoiceOut]:
        rows = db.scalars(select(Invoice).order_by(Invoice.id))
        return [InvoiceOut.model_validate(row) for row in rows]

    return app
