"""Memory-leak probes for a long-running ASGI worker.

Four mechanisms, each with a leaky and a fixed variant:

1. ``functools.lru_cache`` on a method: the cache lives on the class-level
   function, its keys contain ``self``, so every instance it has seen stays
   alive (forever with ``maxsize=None``).
2. Reference cycles: objects that reference counting cannot free and that
   wait for the cyclic collector (httpx ``Response`` <-> ``BoundAsyncStream``,
   and an exception stored on an object its own traceback reaches).
3. Abandoned async generators: ``break`` out of ``async for`` leaves the
   generator suspended; its ``finally`` runs only after the event loop's
   finalizer hook schedules ``aclose()``, and never before the collector
   finds it if a cycle holds it.
4. Arena fragmentation: pymalloc returns a 1 MiB arena to the OS only when
   every pool in it is empty, so a few survivors pin many arenas.

``growth_by_line`` turns two ``tracemalloc`` snapshots into per-line growth.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import gc
import os
import re
import sys
import tempfile
import tracemalloc
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator, Iterable

    from starlette.requests import Request

SCRATCH_BYTES = 4096


# --- 1. lru_cache on a method -------------------------------------------------


class LeakyPricer:
    """Per-request service whose cached method pins every instance it sees."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant
        self.scratch = bytearray(SCRATCH_BYTES)  # per-request working buffer

    @functools.lru_cache(maxsize=None)  # noqa: B019, UP033 - the bug under study
    def quote(self, sku: str) -> int:
        return len(self.tenant) * 100 + len(sku)


@functools.lru_cache(maxsize=1024)
def base_price(tenant: str, sku: str) -> int:
    """Module-level cache keyed only on immutable values, with a bound."""
    return len(tenant) * 100 + len(sku)


class BoundedPricer:
    """Same contract; the cache never sees ``self``."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant
        self.scratch = bytearray(SCRATCH_BYTES)

    def quote(self, sku: str) -> int:
        return base_price(self.tenant, sku)


def build_pricing_app(pricer_cls: type[LeakyPricer] | type[BoundedPricer]) -> Starlette:
    """One pricer per request, as a FastAPI dependency without caching would do."""
    created: weakref.WeakSet[LeakyPricer | BoundedPricer] = weakref.WeakSet()

    async def quote(request: Request) -> JSONResponse:
        pricer = pricer_cls(request.path_params["tenant"])
        created.add(pricer)
        price = pricer.quote(request.path_params["sku"])
        return JSONResponse({"price": price})

    app = Starlette(routes=[Route("/quote/{tenant}/{sku}", quote)])
    app.state.created = created
    return app


async def drive(app: Starlette, paths: Iterable[str]) -> int:
    """Send one in-process GET per path; return how many returned 200."""
    ok = 0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in paths:
            response = await client.get(path)
            ok += response.status_code == 200
    return ok


# --- tracemalloc diffs --------------------------------------------------------


@dataclass(frozen=True)
class LineGrowth:
    location: str
    size_diff: int
    count_diff: int


def growth_by_line(
    before: tracemalloc.Snapshot, after: tracemalloc.Snapshot, *, path: str = __file__
) -> list[LineGrowth]:
    """Lines of ``path`` whose live allocations grew between two snapshots."""
    only = (tracemalloc.Filter(True, path),)
    stats = after.filter_traces(only).compare_to(before.filter_traces(only), "lineno")
    return [
        LineGrowth(
            f"{os.path.basename(s.traceback[0].filename)}:{s.traceback[0].lineno}",
            s.size_diff,
            s.count_diff,
        )
        for s in stats
        if s.size_diff > 0
    ]


# --- 2. reference cycles ------------------------------------------------------


@contextlib.contextmanager
def collector_paused() -> Generator[None]:
    """Disable the automatic cyclic collector so a test decides when it runs."""
    was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@dataclass(frozen=True)
class Survival:
    after_del: bool
    after_collect: bool
    unreachable_found: int


async def httpx_response_survival(app: Starlette, *, break_cycle: bool) -> Survival:
    """Does a dropped httpx 0.28 ``Response`` outlive its last name?

    ``AsyncClient._send_single_request`` sets ``response.stream`` to a
    ``BoundAsyncStream`` that holds ``_response``: a two-object cycle.
    Call under ``collector_paused()``.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/quote/acme/sku-1")
        ref = weakref.ref(response)
        if break_cycle:
            response.stream = httpx.ByteStream(b"")
        del response
    after_del = ref() is not None
    found = gc.collect()
    return Survival(after_del, ref() is not None, found)


class RequestContext:
    def __init__(self) -> None:
        self.body = bytearray(SCRATCH_BYTES * 16)
        self.last_error: BaseException | None = None
        self.last_error_text = ""


def handle_with_stored_exception(*, keep_traceback: bool) -> weakref.ref[RequestContext]:
    """Store a caught exception on the request context, with or without its frames."""
    ctx = RequestContext()
    try:
        raise ValueError("upstream returned 502")
    except ValueError as exc:
        if keep_traceback:
            ctx.last_error = exc  # exc -> __traceback__ -> this frame -> ctx -> exc
        else:
            ctx.last_error_text = repr(exc)  # a string holds no frames
    return weakref.ref(ctx)


# --- 3. abandoned async generators --------------------------------------------


class LeasePool:
    """Counts checked-out leases, standing in for pooled DB connections."""

    def __init__(self) -> None:
        self.in_use = 0


async def stream_rows(pool: LeasePool, n: int) -> AsyncGenerator[int]:
    pool.in_use += 1
    try:
        for row in range(n):
            yield row
    finally:
        pool.in_use -= 1


async def first_row_abandoned(pool: LeasePool) -> int:
    async for row in stream_rows(pool, 100):
        return row  # leaves the generator suspended at its yield
    return -1


async def first_row_aclosing(pool: LeasePool) -> int:
    async with contextlib.aclosing(stream_rows(pool, 100)) as rows:
        async for row in rows:
            return row  # aclosing awaits rows.aclose() before returning
    return -1


class Cursor:
    """Holds its own generator; the generator's frame holds the cursor."""

    def __init__(self, pool: LeasePool) -> None:
        self.pool = pool
        self.rows = self._rows()

    async def _rows(self) -> AsyncGenerator[int]:
        self.pool.in_use += 1
        try:
            for row in range(100):
                yield row
        finally:
            self.pool.in_use -= 1


async def first_row_from_cursor(pool: LeasePool) -> int:
    cursor = Cursor(pool)
    async for row in cursor.rows:
        return row
    return -1


async def loop_turns_until_released(pool: LeasePool, *, limit: int) -> int | None:
    """Yield to the loop until no lease is held; ``None`` if ``limit`` turns pass."""
    for turn in range(limit + 1):
        if pool.in_use == 0:
            return turn
        await asyncio.sleep(0)
    return None


# --- 4. arena fragmentation ---------------------------------------------------

_ARENAS = re.compile(r"([\d,]+) arenas \* (\d+) bytes/arena")


def debug_malloc_stats() -> str:
    """Capture ``sys._debugmallocstats()``, which writes to C-level stderr."""
    with tempfile.TemporaryFile() as sink:
        saved = os.dup(2)
        os.dup2(sink.fileno(), 2)
        try:
            sys._debugmallocstats()  # pyright: ignore[reportPrivateUsage]
        finally:
            os.dup2(saved, 2)
            os.close(saved)
        sink.seek(0)
        return sink.read().decode("ascii", "replace")


def pymalloc_arena_bytes() -> int | None:
    """Bytes held in pymalloc arenas; ``None`` when pymalloc is not the allocator
    (free-threaded builds use mimalloc, and tracemalloc's hooks hide the stats)."""
    match = _ARENAS.search(debug_malloc_stats())
    if match is None:
        return None
    return int(match.group(1).replace(",", "")) * int(match.group(2))


@dataclass(frozen=True)
class FragmentationReport:
    """Growth over the baseline at each phase: blocks live, bytes held in arenas."""

    blocks_peak: int
    blocks_sparse: int
    arena_bytes_peak: int
    arena_bytes_sparse: int
    arena_bytes_freed: int


def fragment(n: int = 400_000, keep_every: int = 64) -> FragmentationReport | None:
    """Allocate ``n`` small tuples, keep every ``keep_every``-th, then free all."""
    gc.collect()
    blocks_base = sys.getallocatedblocks()
    base = pymalloc_arena_bytes()
    objs = [(i, i) for i in range(n)]
    blocks_peak = sys.getallocatedblocks() - blocks_base
    peak = pymalloc_arena_bytes()
    keep = objs[::keep_every]
    del objs
    blocks_sparse = sys.getallocatedblocks() - blocks_base
    sparse = pymalloc_arena_bytes()
    del keep
    freed = pymalloc_arena_bytes()
    if base is None or peak is None or sparse is None or freed is None:
        return None
    return FragmentationReport(blocks_peak, blocks_sparse, peak - base, sparse - base, freed - base)
