"""BOLA (OWASP API1:2023): tenant-scoped sessions, schema guards, and the GUC hand-off to RLS."""

from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import CursorResult, Engine, delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from api_labs.f15_api_authorization_and_owasp_security.tenant_scope import (
    GUARD,
    TENANT_GUC,
    Customer,
    GucLog,
    Invoice,
    Principal,
    Tenant,
    TenantScopeError,
    TenantSession,
    create_app,
    make_engine,
)

ACME, GLOBEX = "t-acme", "t-globex"
TOKENS = {
    "tok-alice": Principal(user_id="alice", tenant_id=ACME),
    "tok-bob": Principal(user_id="bob", tenant_id=GLOBEX),
}
ALICE = {"Authorization": "Bearer tok-alice"}


@pytest.fixture
def db() -> Iterator[tuple[Engine, GucLog]]:
    engine, log = make_engine()
    with Session(engine) as s, s.begin():
        s.add_all([Tenant(id=ACME, name="Acme"), Tenant(id=GLOBEX, name="Globex")])
        s.flush()  # no Tenant relationship, so the unit of work does not order this insert
        s.add_all(
            [
                Customer(id=10, tenant_id=ACME, name="Acme buyer"),
                Customer(id=20, tenant_id=GLOBEX, name="Globex buyer"),
            ]
        )
        s.add_all(
            [
                Invoice(id=1, tenant_id=ACME, customer_id=10, number="INV-1", amount_cents=100),
                Invoice(id=2, tenant_id=GLOBEX, customer_id=20, number="INV-1", amount_cents=999),
                Invoice(id=3, tenant_id=ACME, customer_id=10, number="INV-2", amount_cents=300),
                Invoice(id=4, tenant_id=GLOBEX, customer_id=20, number="INV-7", amount_cents=700),
            ]
        )
    log.calls.clear()
    yield engine, log
    engine.dispose()


def client(engine: Engine, *, scoped: bool) -> httpx.AsyncClient:
    app = create_app(engine, TOKENS, scoped=scoped)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def ids(session: Session, stmt: Any) -> list[int]:
    return sorted(session.scalars(stmt))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unscoped_endpoint_leaks_another_tenants_invoice(db: tuple[Engine, GucLog]) -> None:
    async with client(db[0], scoped=False) as c:
        r = await c.get("/invoices/2", headers=ALICE)
    assert r.status_code == 200
    assert r.json() == {"id": 2, "number": "INV-1", "amount_cents": 999}  # Globex data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scoped_endpoint_answers_foreign_and_missing_ids_identically(
    db: tuple[Engine, GucLog],
) -> None:
    async with client(db[0], scoped=True) as c:
        own = await c.get("/invoices/1", headers=ALICE)
        foreign = await c.get("/invoices/2", headers=ALICE)
        missing = await c.get("/invoices/999", headers=ALICE)
    assert own.status_code == 200
    assert own.json() == {"id": 1, "number": "INV-1", "amount_cents": 100}
    assert foreign.status_code == missing.status_code == 404
    assert foreign.content == missing.content == b'{"detail":"Not Found"}'
    assert foreign.headers.items() == missing.headers.items()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scoped_patch_cannot_touch_another_tenants_invoice(
    db: tuple[Engine, GucLog],
) -> None:
    async with client(db[0], scoped=True) as c:
        r = await c.patch("/invoices/2", headers=ALICE, json={"amount_cents": 0})
        mine = await c.patch("/invoices/3", headers=ALICE, json={"amount_cents": 1})
    assert r.status_code == 404
    assert mine.json() == {"id": 3, "number": "INV-2", "amount_cents": 1}
    with Session(db[0]) as s:
        assert s.get_one(Invoice, 2).amount_cents == 999


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_and_auth_are_tenant_bound(db: tuple[Engine, GucLog]) -> None:
    async with client(db[0], scoped=True) as c:
        alice = await c.get("/invoices", headers=ALICE)
        bob = await c.get("/invoices", headers={"Authorization": "Bearer tok-bob"})
        anon = await c.get("/invoices")
        forged = await c.get("/invoices", headers={"Authorization": "Bearer t-acme"})
    assert [row["id"] for row in alice.json()] == [1, 3]
    assert [row["id"] for row in bob.json()] == [2, 4]
    assert anon.status_code == forged.status_code == 401
    assert anon.headers["www-authenticate"] == "Bearer"


@pytest.mark.unit
def test_tenant_is_a_bind_parameter_not_a_cached_literal(db: tuple[Engine, GucLog]) -> None:
    # The criteria function runs once per class and is cached; ``tenant`` must be re-read from
    # the closure on every execution, or the second tenant would silently see the first's rows.
    for tenant, expected in [(ACME, [1, 3]), (GLOBEX, [2, 4]), (ACME, [1, 3])]:
        with TenantSession(db[0], tenant_id=tenant) as s:
            assert ids(s, select(Invoice.id)) == expected


@pytest.mark.unit
def test_aliases_joins_and_subqueries_are_scoped(db: tuple[Engine, GucLog]) -> None:
    with TenantSession(db[0], tenant_id=ACME) as s:
        inv = aliased(Invoice)
        assert ids(s, select(inv.id)) == [1, 3]
        assert ids(s, select(Invoice.id).join(Invoice.customer).where(Customer.id == 20)) == []
        sub = select(Invoice.customer_id).scalar_subquery()
        assert ids(s, select(Customer.id).where(Customer.id.in_(sub))) == [10]
        assert ids(s, select(Customer.id).where(Customer.name == "Globex buyer")) == []


@pytest.mark.unit
def test_orm_bulk_update_and_delete_are_scoped(db: tuple[Engine, GucLog]) -> None:
    s = TenantSession(db[0], tenant_id=ACME)
    with s, s.begin():
        changed = cast("CursorResult[Any]", s.execute(update(Invoice).values(amount_cents=0)))
        removed = cast("CursorResult[Any]", s.execute(delete(Invoice).where(Invoice.id == 2)))
        assert (changed.rowcount, removed.rowcount) == (2, 0)
    with Session(db[0]) as admin:
        assert admin.execute(
            select(Invoice.id, Invoice.amount_cents).order_by(Invoice.id)
        ).all() == [
            (1, 0),
            (2, 999),
            (3, 0),
            (4, 700),
        ]


@pytest.mark.unit
def test_mixin_criteria_without_include_aliases_scope_nothing(
    db: tuple[Engine, GucLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    # with_loader_criteria(<unmapped mixin>, ...) has entity=None, and the compiler applies an
    # option only if include_aliases is set or option.entity is the queried entity.
    monkeypatch.setattr(GUARD, "include_aliases", False)
    with TenantSession(db[0], tenant_id=ACME) as s:
        assert ids(s, select(Invoice.id)) == [1, 2, 3, 4]


@pytest.mark.unit
def test_lazy_load_carries_the_tenant_criteria(db: tuple[Engine, GucLog]) -> None:
    seen: list[str] = []

    @event.listens_for(db[0], "before_cursor_execute")
    def capture(*args: Any) -> None:
        seen.append(args[2])

    with TenantSession(db[0], tenant_id=ACME) as s:
        customer = s.scalars(select(Customer)).one()
        seen.clear()
        assert sorted(i.id for i in customer.invoices) == [1, 3]
    lazy = [sql for sql in seen if "FROM invoices" in sql]
    assert len(lazy) == 1
    assert "invoices.tenant_id = ?" in lazy[0]


@pytest.mark.unit
def test_inserts_are_stamped_and_cross_tenant_writes_rejected(db: tuple[Engine, GucLog]) -> None:
    with TenantSession(db[0], tenant_id=ACME) as s, s.begin():
        new = Invoice(customer_id=10, number="INV-3", amount_cents=5)
        s.add(new)
        s.flush()
        assert new.tenant_id == ACME
    with TenantSession(db[0], tenant_id=ACME) as s:
        s.add(Invoice(tenant_id=GLOBEX, customer_id=20, number="X", amount_cents=1))
        with pytest.raises(TenantScopeError, match="insert into tenant"):
            s.flush()
    with TenantSession(db[0], tenant_id=ACME) as s:
        s.get_one(Invoice, 1).tenant_id = GLOBEX
        with pytest.raises(TenantScopeError, match="immutable"):
            s.flush()


@pytest.mark.unit
def test_composite_fk_rejects_a_reference_to_another_tenants_row(
    db: tuple[Engine, GucLog],
) -> None:
    with TenantSession(db[0], tenant_id=ACME) as s:
        s.add(Invoice(customer_id=20, number="INV-9", amount_cents=1))  # Globex's customer
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            s.flush()


@pytest.mark.unit
def test_per_tenant_unique_key_gives_no_existence_oracle(db: tuple[Engine, GucLog]) -> None:
    with TenantSession(db[0], tenant_id=ACME) as s, s.begin():
        s.add(Invoice(customer_id=10, number="INV-7", amount_cents=1))  # Globex has INV-7
    with TenantSession(db[0], tenant_id=ACME) as s:
        s.add(Invoice(customer_id=10, number="INV-7", amount_cents=2))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            s.flush()


@pytest.mark.unit
def test_tenant_is_published_as_a_transaction_local_setting(db: tuple[Engine, GucLog]) -> None:
    engine, log = db
    for tenant in (ACME, GLOBEX):
        with TenantSession(engine, tenant_id=tenant) as s, s.begin():
            s.scalars(select(Invoice.id)).all()
            s.scalars(select(Customer.id)).all()
    assert log.calls == [(TENANT_GUC, ACME, True), (TENANT_GUC, GLOBEX, True)]


@pytest.mark.unit
def test_raw_sql_and_identity_map_bypass_the_orm_guard(db: tuple[Engine, GucLog]) -> None:
    # The ORM guard is a convenience layer, not a security boundary: this is why RLS exists.
    seen: list[str] = []
    with TenantSession(db[0], tenant_id=ACME) as s:
        raw = s.execute(text("SELECT number FROM invoices WHERE id = 2")).scalar_one()
        assert raw == "INV-1"  # Globex's row, unfiltered
        stmt = select(Invoice).from_statement(text("SELECT * FROM invoices WHERE id = 2"))
        leaked = s.scalars(stmt).one()
        assert leaked.tenant_id == GLOBEX

        @event.listens_for(db[0], "before_cursor_execute")
        def capture(*args: Any) -> None:
            seen.append(args[2])

        assert s.get(Invoice, 2) is leaked  # served from the identity map
    assert seen == []  # no SQL, so no criteria could have been applied


@pytest.mark.unit
def test_session_without_a_tenant_cannot_exist(db: tuple[Engine, GucLog]) -> None:
    with pytest.raises(TenantScopeError):
        TenantSession(db[0], tenant_id="")
