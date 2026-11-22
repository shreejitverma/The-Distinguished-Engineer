"""A FastAPI app whose endpoints each spend time in a different way, for the profiler to split."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, Query
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from api_labs.f18_observability_profiling_and_operations.request_profile import (
    ProfilingMiddleware,
    SqlAttribution,
    TaskStepMeter,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp

    from api_labs.f18_observability_profiling_and_operations.request_profile import RequestProfile

Millis = Query(default=20, ge=0, le=2000)
Count = Query(default=5, ge=0, le=100)


def burn_cpu_ns(duration_ns: int) -> int:
    """Spin until this thread has consumed ``duration_ns`` of CPU time; return the spin count."""
    spins = 0
    deadline = time.thread_time_ns() + duration_ns
    while time.thread_time_ns() < deadline:
        spins += 1
    return spins


def _slow_ms(milliseconds: int) -> int:
    """SQLite user function: sleep inside the database worker thread."""
    time.sleep(milliseconds / 1000)
    return milliseconds


def create_engine() -> AsyncEngine:
    """In-memory aiosqlite engine (a StaticPool) with a ``slow_ms(n)`` SQL function."""
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _register(dbapi_connection: object, record: object) -> None:
        create_function = getattr(dbapi_connection, "create_function")  # noqa: B009
        create_function("slow_ms", 1, _slow_ms)

    return engine


async def seed(engine: AsyncEngine, rows: int = 20) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT)"))
        for order_id in range(1, rows + 1):
            await conn.execute(
                text("INSERT INTO orders (id, customer) VALUES (:id, :customer)"),
                {"id": order_id, "customer": f"customer-{order_id}"},
            )


def build_app(
    engine: AsyncEngine, meter: TaskStepMeter, sink: Callable[[RequestProfile], None]
) -> ASGIApp:
    app = FastAPI()

    @app.get("/cpu")
    async def cpu(ms: int = Millis) -> dict[str, int]:
        burn_cpu_ns(ms * 1_000_000)
        return {"ms": ms}

    @app.get("/wait")
    async def wait(ms: int = Millis) -> dict[str, int]:
        await asyncio.sleep(ms / 1000)
        return {"ms": ms}

    @app.get("/block")
    async def block(ms: int = Millis) -> dict[str, int]:
        time.sleep(ms / 1000)  # noqa: ASYNC251 - the bug this endpoint exists to expose
        return {"ms": ms}

    @app.get("/thread")
    async def thread(ms: int = Millis) -> dict[str, int]:
        await asyncio.to_thread(burn_cpu_ns, ms * 1_000_000)
        return {"ms": ms}

    @app.get("/slow-sql")
    async def slow_sql(ms: int = Millis) -> dict[str, int]:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT slow_ms(:ms)"), {"ms": ms})
            return {"ms": int(result.scalar_one())}

    @app.get("/orders")
    async def orders(n: int = Count) -> dict[str, int]:
        """The N+1 shape: one list query, then one lookup per row."""
        async with engine.connect() as conn:
            ids = (await conn.execute(text("SELECT id FROM orders LIMIT :n"), {"n": n})).scalars()
            found = 0
            for order_id in ids.all():
                row = await conn.execute(
                    text("SELECT customer FROM orders WHERE id = :id"), {"id": order_id}
                )
                found += len(row.all())
        return {"found": found}

    return ProfilingMiddleware(app, meter, sink)


def attach(engine: AsyncEngine) -> SqlAttribution:
    attribution = SqlAttribution(engine)
    attribution.install()
    return attribution
