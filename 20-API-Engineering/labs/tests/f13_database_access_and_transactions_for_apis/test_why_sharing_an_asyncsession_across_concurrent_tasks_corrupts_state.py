"""Lab: one AsyncSession shared across asyncio.gather / TaskGroup siblings, and the fix."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IllegalStateChangeError, InvalidRequestError, SAWarning
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_scoped_session,
    async_sessionmaker,
)

from api_labs.f13_database_access_and_transactions_for_apis.shared_async_session import (
    AuditEvent,
    Order,
    count_rows,
    create_app,
    create_schema,
    gather_on_shared_session,
    gather_per_task_sessions,
    make_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

PROVISIONING = "This session is provisioning a new connection; concurrent operations"


@pytest_asyncio.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(str(tmp_path / "lab.db"))
    await create_schema(eng)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def select_one(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT 1"))).scalar_one())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_use_race_raises_isce_provisioning_error(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        results = await gather_on_shared_session(session, select_one, select_one)
    assert results[0] == 1
    error = results[1]
    assert isinstance(error, InvalidRequestError)
    assert PROVISIONING in str(error)
    assert error.code == "isce"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_warm_shared_session_runs_concurrent_selects_without_any_error(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker() as session:
        await select_one(session)  # the connection is provisioned before the fan-out
        results = await gather_on_shared_session(session, select_one, select_one, select_one)
        assert results == [1, 1, 1]  # no guard fires: the tasks now share one transaction
        assert session.in_transaction()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_flush_raises_already_flushing_and_warns_on_add(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async def add_and_flush(session: AsyncSession) -> None:
        session.add(Order(sku="x"))
        await session.flush()

    async with maker() as session:
        await select_one(session)
        with pytest.warns(SAWarning, match="execution stage of the flush process"):
            results = await gather_on_shared_session(session, add_and_flush, add_and_flush)
        assert results[0] is None
        assert isinstance(results[1], InvalidRequestError)
        assert str(results[1]) == "Session is already flushing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_during_provisioning_raises_illegal_state_change(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async def close(session: AsyncSession) -> None:
        await session.close()

    session = maker()
    results: list[object] = await gather_on_shared_session(session, select_one, close)
    await session.close()
    assert results[0] == 1
    error = results[1]
    assert isinstance(error, IllegalStateChangeError)
    assert "method '_connection_for_bind()' is already in progress" in str(error)
    assert "SessionTransactionState.CLOSED" in str(error)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_taskgroup_sibling_failure_surfaces_as_exception_group(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    caught: list[BaseException] = []
    async with maker() as session:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(select_one(session))
                tg.create_task(select_one(session))
        except* InvalidRequestError as group:
            caught.extend(group.exceptions)
    assert len(caught) == 1
    assert PROVISIONING in str(caught[0])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identity_map_is_shared_so_sibling_mutations_leak(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    async with maker.begin() as setup:
        setup.add(Order(id=1, sku="original"))

    async def load(session: AsyncSession) -> Order | None:
        return await session.get(Order, 1)

    async with maker() as session:
        await select_one(session)
        first, second = await gather_on_shared_session(session, load, load)
        assert isinstance(first, Order)
        assert first is second  # one identity map: both tasks hold the same Python object
        first.sku = "mutated-by-task-1"
        assert isinstance(second, Order)
        assert second.sku == "mutated-by-task-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sibling_commit_publishes_another_tasks_unfinished_work(
    engine: AsyncEngine, maker: async_sessionmaker[AsyncSession]
) -> None:
    async def half_done_then_fail(session: AsyncSession) -> None:
        session.add(Order(sku="half-done"))
        await asyncio.sleep(0)  # yields; the sibling commits in this window
        raise RuntimeError("validation failed after the add")

    async def commit(session: AsyncSession) -> None:
        await session.commit()

    async with maker() as session:
        await select_one(session)
        results: list[object] = await gather_on_shared_session(session, half_done_then_fail, commit)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert await count_rows(engine, Order) == 1  # the failed task's row is durable


@pytest.mark.unit
@pytest.mark.asyncio
async def test_shared_session_endpoint_returns_201_but_loses_the_order(
    engine: AsyncEngine,
) -> None:
    app = create_app(engine, audit_fails=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/orders/shared")
    assert response.status_code == 201
    assert response.json() == {"order_id": 1, "audit_ok": False}
    assert await count_rows(engine, Order) == 0  # the audit rollback discarded the order
    assert await count_rows(engine, AuditEvent) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_task_endpoint_persists_order_and_isolates_audit_failure(
    engine: AsyncEngine,
) -> None:
    app = create_app(engine, audit_fails=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/orders/per-task")
    assert response.status_code == 201
    assert response.json() == {"order_id": 1, "audit_ok": False}
    assert await count_rows(engine, Order) == 1
    assert await count_rows(engine, AuditEvent) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_task_sessions_survive_concurrent_first_use(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    results = await gather_per_task_sessions(maker, *([select_one] * 8))
    assert results == [1] * 8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_scoped_session_keyed_by_current_task_isolates_siblings(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    def current_task_key() -> object:
        return asyncio.current_task()

    registry = async_scoped_session(maker, scopefunc=current_task_key)

    async def which() -> tuple[AsyncSession, AsyncSession]:
        first = registry()
        await select_one(first)
        second = registry()
        await registry.remove()
        return first, second  # returning the objects keeps them alive, so ids stay unique

    pairs = await asyncio.gather(which(), which(), which())
    assert all(a is b for a, b in pairs)  # stable within one task
    assert len({id(a) for a, _ in pairs}) == 3  # distinct across sibling tasks
