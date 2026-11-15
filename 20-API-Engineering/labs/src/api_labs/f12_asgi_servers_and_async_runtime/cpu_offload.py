"""CPU-bound work behind an async API: inline, thread, process, and free-threaded offload.

Every kernel is a module-level function so ProcessPoolExecutor and InterpreterPoolExecutor
can pickle it by reference.
The module imports only the standard library at top level, because every spawned worker
process and every subinterpreter re-imports the module that defines the submitted function.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import pickle
import sys
import sysconfig
import threading
import time
from concurrent.futures import Executor, ProcessPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import anyio
    from starlette.applications import Starlette

type Strategy = Literal["inline", "thread", "process"]
type Work = Callable[[bytes], str]


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    """What the running interpreter can do with threads."""

    version: str
    free_threaded_build: bool
    gil_enabled: bool
    switch_interval_s: float
    default_start_method: str


def runtime_info() -> RuntimeInfo:
    """Report the build flag and the live GIL state, which can differ on a 3.13t+ build."""
    is_gil_enabled = getattr(sys, "_is_gil_enabled", None)
    gil_enabled = True if is_gil_enabled is None else bool(is_gil_enabled())
    return RuntimeInfo(
        version=sys.version.split()[0],
        free_threaded_build=sysconfig.get_config_var("Py_GIL_DISABLED") == 1,
        gil_enabled=gil_enabled,
        switch_interval_s=sys.getswitchinterval(),
        default_start_method=multiprocessing.get_start_method(allow_none=False),
    )


def spin(n: int) -> int:
    """Pure-Python integer loop: the thread holds the GIL for the whole call on a GIL build."""
    acc = 0
    for i in range(n):
        acc = (acc * 31 + i) & 0xFFFFFFFF
    return acc


def make_payload(records: int, *, fields: int = 8) -> bytes:
    """Deterministic JSON array of flat records, the request body of the digest endpoint."""
    rows = [
        {"id": i} | {f"f{j}": (i * 7919 + j) % 1_000_003 for j in range(fields)}
        for i in range(records)
    ]
    return json.dumps(rows).encode()


def transform_and_hash(payload: bytes) -> str:
    """Canonicalize every record and chain SHA-256 over them.

    `json.loads`, `json.dumps`, and hashlib calls on inputs under 2048 bytes all hold the GIL.
    """
    records: list[dict[str, int]] = json.loads(payload)
    digest = hashlib.sha256()
    for record in records:
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        digest.update(hashlib.sha256(canonical).digest())
    return digest.hexdigest()


def hash_buffer(chunk_size: int, total_bytes: int) -> str:
    """SHA-256 over `total_bytes` zero bytes fed in `chunk_size` updates.

    CPython releases the GIL inside an update whose buffer is >= 2048 bytes
    (`HASHLIB_GIL_MINSIZE`), so large chunks run in parallel even on a GIL build.
    """
    chunk = bytes(chunk_size)
    digest = hashlib.sha256()
    for _ in range(total_bytes // chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def nap(seconds: float) -> float:
    """Sleep in a worker; used to force a pool to spawn its workers before timing."""
    time.sleep(seconds)
    return seconds


def worker_pid() -> int:
    """Return the PID of the process that ran the call."""
    return os.getpid()


def echo_len(payload: bytes) -> int:
    """Return the payload size; isolates IPC cost from compute cost."""
    return len(payload)


def crash_worker(code: int = 70) -> NoReturn:
    """Exit the worker process without unwinding, as a segfault or OOM kill would."""
    os._exit(code)


def spawn_pool(workers: int, *, max_tasks_per_child: int | None = None) -> ProcessPoolExecutor:
    """A process pool with an explicit `spawn` context, identical on every OS and version."""
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=max_tasks_per_child,
    )


def pickled_size(obj: object) -> int:
    """Bytes the process pool must copy through its pipe for one argument or result."""
    return len(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))


@dataclass(frozen=True, slots=True)
class ParallelRun:
    """Wall time and process CPU time of `workers` threads running one function each."""

    workers: int
    wall_s: float
    cpu_s: float

    @property
    def cpu_parallelism(self) -> float:
        """Average number of cores busy: about 1.0 under the GIL, up to `workers` without it."""
        return self.cpu_s / self.wall_s if self.wall_s > 0 else 0.0


def run_in_threads(fn: Callable[[], object], workers: int) -> ParallelRun:
    """Start `workers` threads on a barrier, run `fn` in each, and measure wall and CPU time."""
    barrier = threading.Barrier(workers + 1)
    errors: list[Exception] = []

    def body() -> None:
        barrier.wait()
        try:
            fn()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=body, name=f"cpu-{i}") for i in range(workers)]
    for thread in threads:
        thread.start()
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    barrier.wait()
    for thread in threads:
        thread.join()
    run = ParallelRun(workers, time.perf_counter() - wall0, time.process_time() - cpu0)
    if errors:
        raise ExceptionGroup("worker thread failed", errors)
    return run


def best_of(measure: Callable[[], float], *, at_least: float, attempts: int = 5) -> float:
    """Largest of up to `attempts` measurements, stopping once one reaches `at_least`.

    Other load on the host can only lower an observed parallelism or speedup, never raise it
    above what the runtime allows, so a lower-bound claim is tested on the best attempt.
    """
    best = 0.0
    for _ in range(attempts):
        best = max(best, measure())
        if best >= at_least:
            break
    return best


@dataclass(frozen=True, slots=True)
class StallReport:
    """Result of one job plus how late a 2 ms heartbeat on the same loop woke up."""

    result: str
    max_gap_s: float
    ticks: int


async def measure_stall(job: Callable[[], Awaitable[str]], *, tick_s: float = 0.002) -> StallReport:
    """Run `job` while a heartbeat task records its worst wake-up lateness."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(tick_s)
            now = time.perf_counter()
            gaps.append(now - last - tick_s)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(tick_s * 3)
    try:
        result = await job()
    finally:
        stop.set()
        await beat
    return StallReport(result, max(gaps, default=0.0), len(gaps))


async def offload(
    strategy: Strategy,
    work: Work,
    payload: bytes,
    *,
    executor: Executor | None = None,
    limiter: anyio.CapacityLimiter | None = None,
) -> str:
    """Run `work(payload)` inline on the loop, in a worker thread, or in a worker process."""
    if strategy == "inline":
        return work(payload)
    if strategy == "thread":
        import anyio.to_thread

        return await anyio.to_thread.run_sync(work, payload, limiter=limiter)
    if executor is None:
        raise ValueError("the process strategy needs an executor")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, work, payload)


@dataclass(slots=True)
class AdmissionGate:
    """Counts in-flight CPU jobs; safe without a lock because it only runs on the loop thread."""

    capacity: int
    in_flight: int = 0
    rejected: int = 0

    def try_enter(self) -> bool:
        if self.in_flight >= self.capacity:
            self.rejected += 1
            return False
        self.in_flight += 1
        return True

    def leave(self) -> None:
        self.in_flight -= 1


@dataclass(frozen=True, slots=True)
class DigestService:
    """The ASGI app and its admission gate, returned together so tests can observe the gate."""

    app: Starlette
    gate: AdmissionGate


def build_app(
    *,
    strategy: Strategy,
    work: Work,
    max_in_flight: int,
    max_body_bytes: int = 1 << 20,
    executor: Executor | None = None,
    limiter: anyio.CapacityLimiter | None = None,
) -> DigestService:
    """`POST /digest` that sheds load with `503` before reading the body and caps body size."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if TYPE_CHECKING:
        from starlette.requests import Request
        from starlette.responses import Response

    gate = AdmissionGate(max_in_flight)

    async def digest(request: Request) -> Response:
        if not gate.try_enter():
            return JSONResponse(
                {"detail": "cpu workers saturated"}, status_code=503, headers={"Retry-After": "1"}
            )
        try:
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > max_body_bytes:
                    return JSONResponse({"detail": "body too large"}, status_code=413)
            result = await offload(strategy, work, bytes(body), executor=executor, limiter=limiter)
        finally:
            gate.leave()
        return JSONResponse({"sha256": result, "strategy": strategy})

    app = Starlette(routes=[Route("/digest", digest, methods=["POST"])])
    return DigestService(app=app, gate=gate)


@dataclass(frozen=True, slots=True)
class BenchRow:
    """Latency of `requests` offloaded jobs at a fixed client concurrency."""

    strategy: str
    requests: int
    concurrency: int
    wall_s: float
    p50_ms: float
    max_ms: float
    max_loop_gap_ms: float


async def bench_strategy(
    label: str,
    strategy: Strategy,
    work: Work,
    payload: bytes,
    *,
    requests: int,
    concurrency: int,
    executor: Executor | None = None,
    limiter: anyio.CapacityLimiter | None = None,
) -> BenchRow:
    """Fire `requests` jobs through `offload` with at most `concurrency` outstanding."""
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []

    async def one() -> str:
        async with sem:
            t0 = time.perf_counter()
            out = await offload(strategy, work, payload, executor=executor, limiter=limiter)
            latencies.append(time.perf_counter() - t0)
            return out

    async def all_requests() -> str:
        t0 = time.perf_counter()
        async with asyncio.TaskGroup() as tg:
            for _ in range(requests):
                tg.create_task(one())
        return f"{time.perf_counter() - t0:.6f}"

    report = await measure_stall(all_requests)
    latencies.sort()
    return BenchRow(
        strategy=label,
        requests=requests,
        concurrency=concurrency,
        wall_s=float(report.result),
        p50_ms=latencies[len(latencies) // 2] * 1000,
        max_ms=latencies[-1] * 1000,
        max_loop_gap_ms=report.max_gap_s * 1000,
    )


def run_benchmark(
    *, records: int = 2000, requests: int = 32, concurrency: int = 4, workers: int = 4
) -> list[BenchRow]:
    """Compare inline, thread, and process offload of `transform_and_hash` on this interpreter."""
    import anyio

    payload = make_payload(records)
    pool = spawn_pool(workers)
    try:
        list(pool.map(nap, [0.2] * workers))

        async def main() -> list[BenchRow]:
            limiter = anyio.CapacityLimiter(workers)
            rows: list[BenchRow] = []
            pairs: tuple[tuple[str, Strategy], ...] = (("inline", "inline"), ("thread", "thread"))
            for label, strategy in pairs:
                rows.append(
                    await bench_strategy(
                        label,
                        strategy,
                        transform_and_hash,
                        payload,
                        requests=requests,
                        concurrency=concurrency,
                        limiter=limiter,
                    )
                )
            rows.append(
                await bench_strategy(
                    "process",
                    "process",
                    transform_and_hash,
                    payload,
                    requests=requests,
                    concurrency=concurrency,
                    executor=pool,
                )
            )
            return rows

        return asyncio.run(main())
    finally:
        pool.shutdown(wait=True)


def format_rows(rows: list[BenchRow]) -> str:
    """Render benchmark rows as a fixed-width table."""
    lines = [
        f"{'strategy':<8} {'reqs':>4} {'conc':>4} {'wall_s':>7} {'p50_ms':>8} "
        f"{'max_ms':>8} {'loop_gap_ms':>11}"
    ]
    lines.extend(
        f"{r.strategy:<8} {r.requests:>4} {r.concurrency:>4} {r.wall_s:>7.3f} "
        f"{r.p50_ms:>8.1f} {r.max_ms:>8.1f} {r.max_loop_gap_ms:>11.1f}"
        for r in rows
    )
    return "\n".join(lines)
