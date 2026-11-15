"""Lab: coarse scopes at the edge, object-level checks bound to the database query."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api_labs.f15_api_authorization_and_owasp_security.authz_placement import (
    Invoice,
    Placement,
    Store,
    create_app,
    install_tenant_criteria,
    open_store,
)

TRANSACTION_CONTROL = ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")


@pytest_asyncio.fixture
async def store() -> AsyncIterator[Store]:
    db = await open_store()
    yield db
    await db.engine.dispose()


async def call(
    app: FastAPI, method: str, url: str, token: str | None = None, body: Any = None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, headers=headers, json=body)


def sql(db: Store) -> list[str]:
    return [s for s in db.statements if not s.startswith(TRANSACTION_CONTROL)]


async def row_state(db: Store, invoice_id: int) -> tuple[str, str]:
    async with db.sessions() as session:
        row = (
            await session.execute(
                select(Invoice.tenant_id, Invoice.status).where(Invoice.id == invoice_id)
            )
        ).one()
        return row.tenant_id, row.status


# --- The edge: authentication and one coarse scope, before routing and before SQL ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_token_is_401_with_bare_challenge_and_no_sql(store: Store) -> None:
    r = await call(create_app(store, Placement.QUERY_BOUND), "GET", "/invoices/1")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="invoices"'
    assert sql(store) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_token_is_401_invalid_token(store: Store) -> None:
    r = await call(create_app(store, Placement.QUERY_BOUND), "GET", "/invoices/1", "tok-x")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="invoices", error="invalid_token"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_rejects_missing_write_scope_before_any_sql(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND)
    r = await call(app, "POST", "/invoices/1/void", "tok-carol")
    assert r.status_code == 403
    assert r.headers["www-authenticate"] == (
        'Bearer realm="invoices", error="insufficient_scope", scope="invoices:write"'
    )
    assert sql(store) == []
    assert await row_state(store, 1) == ("acme", "draft")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_scope_cannot_see_tenants_so_edge_only_serves_bola(store: Store) -> None:
    r = await call(create_app(store, Placement.EDGE_ONLY), "GET", "/invoices/2", "tok-alice")
    # Alice (acme) holds a valid invoices:read token, so the edge passes her; the row is globex.
    assert (r.status_code, r.json()["tenant_id"]) == (200, "globex")


# --- Fetch, then check in Python ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_then_check_403_is_an_existence_oracle(store: Store) -> None:
    app = create_app(store, Placement.FETCH_THEN_CHECK_403)
    foreign = await call(app, "GET", "/invoices/2", "tok-alice")
    missing = await call(app, "GET", "/invoices/999", "tok-alice")
    assert (foreign.status_code, missing.status_code) == (403, 404)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_then_check_404_still_reads_the_foreign_row(store: Store) -> None:
    app = create_app(store, Placement.FETCH_THEN_CHECK_404)
    foreign = await call(app, "GET", "/invoices/2", "tok-alice")
    foreign_sql, foreign_rows = sql(store), list(store.materialized)
    store.reset()
    missing = await call(app, "GET", "/invoices/999", "tok-alice")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    # Same response, different work: the foreign row and its lines were loaded.
    assert (len(foreign_sql), foreign_rows) == (2, [2])
    assert (len(sql(store)), store.materialized) == (1, [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_bound_foreign_and_missing_ids_run_identical_sql(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND)
    foreign = await call(app, "GET", "/invoices/2", "tok-alice")
    foreign_sql, foreign_rows = sql(store), list(store.materialized)
    store.reset()
    missing = await call(app, "GET", "/invoices/999", "tok-alice")
    assert foreign.status_code == missing.status_code == 404
    assert foreign_sql == sql(store)
    assert len(foreign_sql) == 1
    assert "invoices.id = ? AND invoices.tenant_id = ?" in foreign_sql[0]
    assert foreign_rows == store.materialized == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_bound_serves_the_callers_own_invoice(store: Store) -> None:
    r = await call(create_app(store, Placement.QUERY_BOUND), "GET", "/invoices/1", "tok-alice")
    assert r.status_code == 200
    assert (r.json()["number"], r.json()["lines"]) == ("A-1", ["design", "build"])
    assert len(sql(store)) == 2


# --- Lists: post-filtering versus a WHERE clause ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edge_only_list_returns_every_tenant(store: Store) -> None:
    app = create_app(store, Placement.EDGE_ONLY)
    r = await call(app, "GET", "/invoices?limit=3", "tok-alice")
    assert [item["id"] for item in r.json()["items"]] == [1, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_filtered_page_is_short_and_reads_foreign_rows(store: Store) -> None:
    app = create_app(store, Placement.FETCH_THEN_CHECK_404)
    r = await call(app, "GET", "/invoices?limit=3", "tok-alice")
    page = r.json()
    # Alice owns four invoices, yet a limit=3 page holds two, and globex's row was read.
    assert ([item["id"] for item in page["items"]], page["next_after"]) == ([1, 3], 3)
    assert store.materialized == [1, 2, 3]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_bound_pages_are_full_and_tenant_only(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND)
    first = (await call(app, "GET", "/invoices?limit=3", "tok-alice")).json()
    assert ([item["id"] for item in first["items"]], first["next_after"]) == ([1, 3, 5], 5)
    assert store.materialized == [1, 3, 5]
    second = (await call(app, "GET", "/invoices?limit=3&after=5", "tok-alice")).json()
    assert ([item["id"] for item in second["items"]], second["next_after"]) == ([6], None)


# --- Mutations: check-then-act versus a predicate inside the UPDATE ---


async def transfer_to_globex(session: AsyncSession, invoice_id: int) -> None:
    """A concurrent transaction that commits between the check and the act."""
    await session.execute(
        update(Invoice).where(Invoice.id == invoice_id).values(tenant_id="globex")
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_then_act_voids_an_invoice_transferred_mid_request(store: Store) -> None:
    app = create_app(
        store, Placement.FETCH_THEN_CHECK_404, between_check_and_act=transfer_to_globex
    )
    r = await call(app, "POST", "/invoices/1/void", "tok-alice")
    assert r.status_code == 200
    # Alice voided an invoice that globex owned by the time the UPDATE ran.
    assert await row_state(store, 1) == ("globex", "void")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_bound_update_refuses_a_transferred_invoice(store: Store) -> None:
    async with store.sessions() as session:
        await transfer_to_globex(session, 1)
        await session.commit()
    r = await call(
        create_app(store, Placement.QUERY_BOUND), "POST", "/invoices/1/void", "tok-alice"
    )
    assert r.status_code == 404
    assert await row_state(store, 1) == ("globex", "draft")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_bound_conflict_is_explained_only_inside_the_tenant(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND)
    assert (await call(app, "POST", "/invoices/1/void", "tok-alice")).status_code == 200
    again = await call(app, "POST", "/invoices/1/void", "tok-alice")
    assert (again.status_code, again.json()["detail"]) == (409, "invoice is void")
    # Bob must not learn that invoice 1 exists and is void.
    assert (await call(app, "POST", "/invoices/1/void", "tok-bob")).status_code == 404


# --- Unique constraints bypass row filters ---


def new_invoice(number: str) -> dict[str, Any]:
    return {"number": number, "amount_cents": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_unique_number_reveals_other_tenants_numbers() -> None:
    db = await open_store(global_unique_numbers=True)
    try:
        app = create_app(db, Placement.QUERY_BOUND)
        taken = await call(app, "POST", "/invoices", "tok-alice", new_invoice("G-1"))
        free = await call(app, "POST", "/invoices", "tok-alice", new_invoice("G-9"))
        assert (taken.status_code, free.status_code) == (409, 201)
    finally:
        await db.engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tenant_scoped_unique_number_does_not_leak(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND)
    theirs = await call(app, "POST", "/invoices", "tok-alice", new_invoice("G-1"))
    ours = await call(app, "POST", "/invoices", "tok-alice", new_invoice("A-1"))
    assert (theirs.status_code, theirs.json()["tenant_id"]) == (201, "acme")
    assert ours.status_code == 409


# --- Path rules at the edge versus route-bound guards ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_path_edge_rule_is_bypassed_by_percent_encoding(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND, route_guard=False)
    plain = await call(app, "GET", "/admin/stats", "tok-alice")
    encoded = await call(app, "GET", "/%61dmin/stats", "tok-alice")
    assert plain.status_code == 403
    # The gate matched raw_path b"/%61dmin/stats"; the router matched path "/admin/stats".
    assert (encoded.status_code, encoded.json()) == (200, {"acme": 4, "globex": 2})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_level_guard_closes_the_encoding_gap(store: Store) -> None:
    app = create_app(store, Placement.QUERY_BOUND, route_guard=True)
    encoded = await call(app, "GET", "/%61dmin/stats", "tok-alice")
    assert encoded.status_code == 403
    assert 'scope="admin"' in encoded.headers["www-authenticate"]
    assert (await call(app, "GET", "/admin/stats", "tok-ops")).status_code == 200


# --- Session.get answers from the identity map without running the query ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_get_identity_map_hit_skips_tenant_criteria(store: Store) -> None:
    async with store.sessions() as session:
        install_tenant_criteria(session, "acme")
        assert await session.get(Invoice, 2) is None  # cold: the criteria reach the SQL
        unscoped = select(Invoice).where(Invoice.id == 2).execution_options(skip_tenant=True)
        # An admin path in the same session loads it and keeps a reference; the identity
        # map holds objects weakly, so without one the next get() would query again.
        admin_view = (await session.scalars(unscoped)).all()
        assert [invoice.id for invoice in admin_view] == [2]
        store.reset()
        cached = await session.get(Invoice, 2)
        assert cached is not None
        assert cached.tenant_id == "globex"
        assert sql(store) == []
        bound = (await session.scalars(select(Invoice).where(Invoice.id == 2))).one_or_none()
        assert bound is None
        assert len(sql(store)) == 1
