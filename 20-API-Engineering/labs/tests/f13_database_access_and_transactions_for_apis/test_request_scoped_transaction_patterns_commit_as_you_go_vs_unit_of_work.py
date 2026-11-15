"""Lab: transaction boundaries in FastAPI (Unit-of-Work dependency vs service-layer commit)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api_labs.f13_database_access_and_transactions_for_apis.transaction_boundaries import (
    AuditEntry,
    DependencyScope,
    Order,
    OrderIn,
    OrderService,
    TxnLog,
    close_after_failed_commit,
    create_app,
    create_schema,
    make_primary_engine,
    make_replica_engine,
    replicate,
)

GOOD = {"customer_id": 1, "sku": "widget", "quantity": 2}
ORPHAN = {"customer_id": 999, "sku": "widget", "quantity": 2}  # passes flush, fails COMMIT


@dataclass
class Lab:
    primary: AsyncEngine
    replica: AsyncEngine
    log: TxnLog
    primary_path: str
    replica_path: str

    def client(
        self, *, scope: DependencyScope = "request", raise_app_exceptions: bool = False
    ) -> httpx.AsyncClient:
        app = create_app(self.primary, self.replica, uow_scope=scope)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    async def count(self, model: type[Order] | type[AuditEntry]) -> int:
        async with self.primary.connect() as conn:
            total = (await conn.execute(select(func.count()).select_from(model))).scalar_one()
        self.log.take()
        return total


@pytest_asyncio.fixture
async def lab(tmp_path: Path) -> AsyncIterator[Lab]:
    primary_path, replica_path = str(tmp_path / "primary.db"), str(tmp_path / "replica.db")
    log = TxnLog()
    primary = make_primary_engine(primary_path)
    log.attach(primary, "primary")
    await create_schema(primary)
    replicate(primary_path, replica_path)
    replica = make_replica_engine(replica_path)
    log.attach(replica, "replica")
    log.take()
    yield Lab(primary, replica, log, primary_path, replica_path)
    await primary.dispose()
    await replica.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uow_dependency_commits_in_teardown_on_clean_exit(lab: Lab) -> None:
    async with lab.client() as client:
        response = await client.post("/uow/orders", json=GOOD)
    assert response.status_code == 201
    events, engine_sql = lab.log.take()
    assert events == ["primary:checkout", "primary:BEGIN", "primary:INSERT", "primary:COMMIT"]
    assert engine_sql == ["primary:BEGIN", "primary:INSERT", "primary:COMMIT"]
    assert await lab.count(Order) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uow_dependency_rolls_back_when_handler_raises(lab: Lab) -> None:
    async with lab.client() as client:
        response = await client.post("/uow/orders", json={**GOOD, "quantity": 101})
    assert response.status_code == 409
    events, _ = lab.log.take()
    assert "primary:INSERT" in events
    assert events[-1] == "primary:ROLLBACK"
    assert "primary:COMMIT" not in events
    assert await lab.count(Order) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uow_read_only_request_still_pays_begin_and_commit(lab: Lab) -> None:
    async with lab.client() as client:
        assert (await client.post("/uow/orders", json=GOOD)).status_code == 201
        lab.log.take()
        response = await client.get("/uow/orders/1")
    assert response.status_code == 200
    _, engine_sql = lab.log.take()
    assert engine_sql == ["primary:BEGIN", "primary:SELECT", "primary:COMMIT"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replica_autocommit_read_executes_only_the_select(lab: Lab) -> None:
    async with lab.client() as client:
        assert (await client.post("/svc/orders", json=GOOD)).status_code == 201
        replicate(lab.primary_path, lab.replica_path)
        lab.log.take()
        response = await client.get("/svc/orders/1")
    assert response.status_code == 200
    assert response.headers["X-Served-By"] == "replica"
    events, engine_sql = lab.log.take()
    assert engine_sql == ["replica:SELECT"]
    assert not [e for e in events if e.startswith("primary:")]
    # SQLAlchemy still dispatches its rollback-on-close; SQLite has nothing to roll back.
    assert events == ["replica:checkout", "replica:SELECT", "replica:ROLLBACK"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unused_uow_session_never_checks_out_a_connection(lab: Lab) -> None:
    async with lab.client() as client:
        response = await client.get("/uow/health")
    assert response.json() == {"in_transaction": True}
    assert lab.log.take() == ([], [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_scoped_teardown_commit_failure_is_invisible_to_client(lab: Lab) -> None:
    async with lab.client(scope="request") as client:
        response = await client.post("/uow/orders", json=ORPHAN)
    assert response.status_code == 201
    assert response.json()["id"] == 1
    _, engine_sql = lab.log.take()
    assert engine_sql == [
        "primary:BEGIN",
        "primary:INSERT",
        "primary:COMMIT",
        "primary:ROLLBACK",
    ]
    assert await lab.count(Order) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_scoped_teardown_failure_surfaces_only_server_side(lab: Lab) -> None:
    async with lab.client(scope="request", raise_app_exceptions=True) as client:
        with pytest.raises(RuntimeError, match="response already started") as info:
            await client.post("/uow/orders", json=ORPHAN)
    assert "FOREIGN KEY constraint failed" in str(info.value.__cause__)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_function_scoped_teardown_commit_failure_becomes_409(lab: Lab) -> None:
    async with lab.client(scope="function") as client:
        response = await client.post("/uow/orders", json=ORPHAN)
    assert response.status_code == 409
    assert response.json() == {"detail": "constraint violated"}
    assert await lab.count(Order) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_layer_commit_failure_maps_to_409_before_response(lab: Lab) -> None:
    async with lab.client(scope="request") as client:
        response = await client.post("/svc/orders", json=ORPHAN)
    assert response.status_code == 409
    assert response.json() == {"detail": "unknown customer"}
    assert "X-Read-Token" not in response.headers
    assert await lab.count(Order) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_as_you_go_leaves_first_commit_durable(lab: Lab) -> None:
    async with lab.client() as client:
        response = await client.post("/cayg/orders", json=ORPHAN)
    assert response.status_code == 409
    events, _ = lab.log.take()
    assert events.count("primary:COMMIT") == 2
    assert await lab.count(AuditEntry) == 1
    assert await lab.count(Order) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_after_failed_commit_gets_a_clean_connection(lab: Lab) -> None:
    async with lab.client() as client:
        assert (await client.post("/cayg/orders", json=ORPHAN)).status_code == 409
        response = await client.post("/svc/orders", json=GOOD)
    assert response.status_code == 201
    assert await lab.count(Order) == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rollback", "outcome"),
    [(False, "cannot start a transaction within a transaction"), (True, "ok")],
)
async def test_close_without_rollback_after_failed_commit_poisons_pool(
    lab: Lab, *, rollback: bool, outcome: str
) -> None:
    assert await close_after_failed_commit(lab.primary, rollback=rollback) == outcome


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_token_routes_lagging_reads_to_primary(lab: Lab) -> None:
    async with lab.client() as client:
        created = await client.post("/svc/orders", json=GOOD)
        assert created.status_code == 201
        token = created.headers["X-Read-Token"]
        assert int(token) == 1
        order_id = created.json()["id"]

        stale = await client.get(f"/svc/orders/{order_id}")
        assert stale.status_code == 404  # replica has not replayed the write

        pinned = await client.get(f"/svc/orders/{order_id}", headers={"X-Read-Token": token})
        assert pinned.status_code == 200
        assert pinned.headers["X-Served-By"] == "primary"

        replicate(lab.primary_path, lab.replica_path)
        caught_up = await client.get(f"/svc/orders/{order_id}", headers={"X-Read-Token": token})
    assert caught_up.status_code == 200
    assert caught_up.headers["X-Served-By"] == "replica"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replica_engine_refuses_writes(lab: Lab) -> None:
    async with lab.replica.connect() as conn:
        with pytest.raises(OperationalError, match="attempt to write a readonly database"):
            await conn.execute(text("INSERT INTO audit_log (action) VALUES ('sneaky')"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_begin_after_autobegin_raises(lab: Lab) -> None:
    sessions = async_sessionmaker(lab.primary, expire_on_commit=False)
    async with sessions() as session:
        await session.execute(select(Order.id))  # autobegin
        with pytest.raises(InvalidRequestError, match="already begun"):
            await session.begin()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_begin_inside_outer_unit_of_work_raises(lab: Lab) -> None:
    sessions = async_sessionmaker(lab.primary, expire_on_commit=False)
    async with sessions.begin() as session:
        with pytest.raises(InvalidRequestError, match="already begun"):
            await OrderService().place_order(session, OrderIn.model_validate(GOOD))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_cache_shares_one_session_per_request(lab: Lab) -> None:
    async with lab.client() as client:
        response = await client.get("/shared")
    assert response.json() == {"first_is_second": True, "first_is_third": False}
