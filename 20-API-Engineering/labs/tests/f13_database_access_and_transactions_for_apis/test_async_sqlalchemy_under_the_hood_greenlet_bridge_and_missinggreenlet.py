"""Lab: greenlet_spawn bridging and why implicit lazy loading raises MissingGreenlet."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import importlib
import inspect
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from aiosqlite.context import Result
from fastapi.exceptions import ResponseValidationError
from sqlalchemy import exc
from sqlalchemy.util.concurrency import await_, greenlet_spawn, in_greenlet

from api_labs.f13_database_access_and_transactions_for_apis.async_sqlalchemy_greenlet_bridge import (  # noqa: E501
    LOADERS,
    MiniMissingGreenletError,
    SqlCounter,
    build_app,
    create_schema_and_seed,
    is_missing_greenlet,
    make_engine,
    mini_await,
    mini_spawn,
    name_after_commit,
    pool_class_name,
    sessions,
    titles_via_implicit_lazy_load,
    titles_via_raiseload,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine

# aiosqlite wraps each cursor() coroutine in a Result; when await_() refuses to run, SQLAlchemy
# closes only the outer __aenter__ coroutine, so the inner one warns when garbage collected.
# test_await_outside_greenlet_leaves_aiosqlite_inner_coroutine_unstarted pins that mechanism.
pytestmark = pytest.mark.filterwarnings(
    "ignore:coroutine 'Connection.cursor' was never awaited:RuntimeWarning"
)


@pytest.fixture(autouse=True)
def _collect_garbage_inside_each_test() -> Iterator[None]:
    yield
    gc.collect()  # emit any pending "never awaited" warning under this module's filter


request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unset")


async def _value(v: int) -> int:
    await asyncio.sleep(0)
    return v


async def _fail() -> int:
    await asyncio.sleep(0)
    raise ValueError("driver error")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine()
    await create_schema_and_seed(eng)
    yield eng
    await eng.dispose()


# --- Part 1: the bridge itself ------------------------------------------------------------


@pytest.mark.unit
def test_mini_await_outside_spawn_raises_and_closes_the_coroutine() -> None:
    coro = _value(1)
    with pytest.raises(MiniMissingGreenletError):
        mini_await(coro)
    # Closed, so no "coroutine was never awaited" warning.
    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mini_spawn_round_trips_each_await_through_the_driver() -> None:
    trace: list[str] = []

    def sync_code() -> int:
        return mini_await(_value(20), trace) + mini_await(_value(22), trace)

    assert await mini_spawn(sync_code, trace=trace) == 42
    one_round = [
        "child: switch to parent with awaitable",
        "driver: awaiting on the event loop",
        "driver: switch result back into child",
    ]
    assert trace == one_round * 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mini_spawn_throws_driver_errors_into_the_sync_frame() -> None:
    events: list[str] = []

    def sync_code() -> str:
        try:
            mini_await(_fail())
        except ValueError as error:
            events.append(f"caught {error}")
            return "recovered"
        finally:
            events.append("finally ran")
        return "unreachable"

    assert await mini_spawn(sync_code) == "recovered"
    assert events == ["caught driver error", "finally ran"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bridge_copies_the_task_context_into_the_child_greenlet() -> None:
    request_id.set("req-42")
    greenlet_module: Any = importlib.import_module("greenlet")
    plain = greenlet_module.greenlet(request_id.get)
    assert plain.switch() == "unset"  # a bare greenlet starts with an empty Context
    assert await mini_spawn(request_id.get) == "req-42"
    assert await greenlet_spawn(request_id.get) == "req-42"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlalchemy_await_outside_greenlet_spawn_raises_missing_greenlet() -> None:
    assert in_greenlet() is False
    assert await greenlet_spawn(in_greenlet) is True
    coro = _value(1)
    with pytest.raises(exc.MissingGreenlet, match="greenlet_spawn has not been called") as info:
        await_(coro)
    assert info.value.code == "xd2s"
    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED"
    assert await greenlet_spawn(lambda: await_(_value(7))) == 7


@pytest.mark.unit
def test_await_outside_greenlet_leaves_aiosqlite_inner_coroutine_unstarted() -> None:
    inner = _value(1)
    outer = Result(inner).__aenter__()
    with pytest.raises(exc.MissingGreenlet):
        await_(outer)
    # _safe_cancel_awaitable closed the coroutine it was given, not the one inside Result.
    assert inspect.getcoroutinestate(outer) == "CORO_CLOSED"
    assert inspect.getcoroutinestate(inner) == "CORO_CREATED"
    inner.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_require_await_flags_work_that_never_reached_the_loop() -> None:
    assert await greenlet_spawn(lambda: 1) == 1
    with pytest.raises(exc.AwaitRequired) as info:
        await greenlet_spawn(lambda: 1, _require_await=True)
    assert info.value.code == "xd1r"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_cancellation_is_thrown_into_the_sync_frame() -> None:
    seen: list[str] = []

    def sync_code() -> None:
        try:
            await_(asyncio.sleep(10))
        except asyncio.CancelledError:
            seen.append("sync frame saw CancelledError")
            raise

    task = asyncio.create_task(greenlet_spawn(sync_code))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == ["sync frame saw CancelledError"]


# --- Part 2: the real AsyncSession ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lazy_load_on_held_connection_raises_statement_error_wrapping_missing_greenlet(
    engine: AsyncEngine,
) -> None:
    assert pool_class_name(engine) == "StaticPool"
    async with sessions(engine)() as session:
        with pytest.raises(exc.StatementError) as info:
            await titles_via_implicit_lazy_load(session, 1)
    error = info.value
    assert not isinstance(error, exc.MissingGreenlet)
    assert isinstance(error.orig, exc.MissingGreenlet)
    assert error.code == "xd2s"
    assert is_missing_greenlet(error)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lazy_load_that_must_connect_raises_bare_missing_greenlet(tmp_path: Path) -> None:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'lab.db'}", null_pool=True)
    try:
        await create_schema_and_seed(eng)
        assert pool_class_name(eng) == "NullPool"
        async with sessions(eng, expire_on_commit=True)() as session:
            with pytest.raises(exc.MissingGreenlet) as info:
                await name_after_commit(session, 1)
        assert not isinstance(info.value, exc.StatementError)
        assert is_missing_greenlet(info.value)
    finally:
        await eng.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_file_database_defaults_to_async_adapted_queue_pool(tmp_path: Path) -> None:
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'pool.db'}")
    try:
        assert pool_class_name(eng) == "AsyncAdaptedQueuePool"
    finally:
        await eng.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expire_on_commit_turns_a_column_read_into_io(engine: AsyncEngine) -> None:
    counter = SqlCounter(engine)
    async with sessions(engine, expire_on_commit=True)() as session:
        with pytest.raises(exc.StatementError) as info:
            await name_after_commit(session, 1)
    assert is_missing_greenlet(info.value)
    async with sessions(engine, expire_on_commit=False)() as session:
        before = counter.selects
        assert await name_after_commit(session, 1) == "Ada"
        assert counter.selects == before + 1  # only the get_author SELECT


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "expected_selects"),
    [("awaitable", 2), ("selectin", 2), ("run_sync", 2), ("refresh", 3)],
)
async def test_explicit_strategies_load_the_collection(
    engine: AsyncEngine, strategy: str, expected_selects: int
) -> None:
    counter = SqlCounter(engine)
    async with sessions(engine)() as session:
        assert await LOADERS[strategy](session, 1) == ["Notes", "G"]
    assert counter.selects == expected_selects


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raiseload_fails_fast_without_emitting_sql(engine: AsyncEngine) -> None:
    counter = SqlCounter(engine)
    async with sessions(engine)() as session:
        with pytest.raises(exc.InvalidRequestError, match="lazy='raise'") as info:
            await titles_via_raiseload(session, 1)
    assert not is_missing_greenlet(info.value)
    assert counter.selects == 1  # the Author SELECT only; no lazy-load attempt reached the driver


# --- Part 3: the API surface ----------------------------------------------------------------


def _client(engine: AsyncEngine) -> httpx.AsyncClient:
    app = build_app(sessions(engine))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_implicit_lazy_load_is_classified_as_implicit_io_500(
    engine: AsyncEngine,
) -> None:
    async with _client(engine) as client:
        response = await client.get("/authors/1/titles/lazy")
    assert response.status_code == 500
    assert response.json() == {"error": "implicit_io", "code": "xd2s"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["awaitable", "selectin", "run_sync", "refresh"])
async def test_api_explicit_strategies_return_200(engine: AsyncEngine, strategy: str) -> None:
    async with _client(engine) as client:
        response = await client.get(f"/authors/2/titles/{strategy}")
    assert response.status_code == 200
    assert response.json() == {"titles": ["Unix"]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_response_model_hides_missing_greenlet_inside_response_validation(
    engine: AsyncEngine,
) -> None:
    # pydantic-core stringifies the StatementError into a get_attribute_error line error, so the
    # app's SQLAlchemyError handler never runs and ASGITransport re-raises the app exception.
    async with _client(engine) as client:
        with pytest.raises(ResponseValidationError) as info:
            await client.get("/authors/1")
        eager = await client.get("/authors/1/eager")
    (line,) = info.value.errors()
    assert line["type"] == "get_attribute_error"
    assert line["loc"] == ("response", "books")
    assert "(sqlalchemy.exc.MissingGreenlet)" in line["msg"]
    assert not is_missing_greenlet(info.value)
    assert eager.status_code == 200
    assert eager.json() == {
        "id": 1,
        "name": "Ada",
        "books": [{"id": 1, "title": "Notes"}, {"id": 2, "title": "G"}],
    }
