"""Health-probe lab: ASGI probe endpoints and a deterministic kubelet and fleet simulator.

Part A is a small Starlette app with `/healthz` (liveness), `/readyz` (readiness), and
`/startupz` (startup), a single-flight cached dependency check, and a progress heartbeat.
Part B models the kubelet probe worker (Kubernetes v1.36.0 `pkg/kubelet/prober/worker.go`) and a
fleet of pods behind a Service, one simulated second per tick, so that the cascading-restart
failure can be measured without a cluster.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

Clock = Callable[[], float]
HEALTH_JSON = "application/health+json"


# --------------------------------------------------------------------------------------
# Part A: probe endpoints
# --------------------------------------------------------------------------------------


class Phase(StrEnum):
    STARTING = "starting"
    SERVING = "serving"
    DRAINING = "draining"


class HealthState:
    """Process lifecycle as the probes see it: starting, serving, then draining."""

    def __init__(self) -> None:
        self.phase = Phase.STARTING

    def mark_started(self) -> None:
        if self.phase is Phase.STARTING:
            self.phase = Phase.SERVING

    def begin_drain(self) -> None:
        self.phase = Phase.DRAINING


class ReadinessMode(StrEnum):
    LOCAL = "local"  # dependency failure is reported (`warn`) but never removes the pod
    DEPENDENCY = "dependency"  # dependency failure returns 503 and removes the pod


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    at: float
    error: str | None = None


class CachedDependencyCheck:
    """Dependency check with a TTL cache and single-flight: N probes, at most one call per TTL."""

    def __init__(
        self,
        name: str,
        check: Callable[[], Awaitable[None]],
        *,
        timeout_s: float,
        ttl_s: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self.calls = 0
        self._check = check
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._result: CheckResult | None = None

    def _fresh(self) -> CheckResult | None:
        cached = self._result
        if cached is not None and self._clock() - cached.at < self._ttl_s:
            return cached
        return None

    async def status(self) -> CheckResult:
        cached = self._fresh()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._fresh()  # another probe may have refreshed while we waited
            if cached is not None:
                return cached
            self.calls += 1
            try:
                async with asyncio.timeout(self._timeout_s):
                    await self._check()
                result = CheckResult(self.name, True, self._clock())
            except Exception as exc:
                # Only the exception type leaves the process: messages can carry hosts and DSNs.
                result = CheckResult(self.name, False, self._clock(), type(exc).__name__)
            self._result = result
            return result


class Heartbeat:
    """Progress marker for a worker loop; a stale beat means the loop is wedged, not the process."""

    def __init__(self, max_age_s: float, clock: Clock = time.monotonic) -> None:
        self._max_age_s = max_age_s
        self._clock = clock
        self._last = clock()

    def beat(self) -> None:
        self._last = self._clock()

    def fresh(self) -> bool:
        return self._clock() - self._last <= self._max_age_s


def _health(status_code: int, body: dict[str, Any]) -> Response:
    return JSONResponse(
        body, status_code=status_code, media_type=HEALTH_JSON, headers={"Cache-Control": "no-store"}
    )


def build_app(
    state: HealthState,
    dependency: CachedDependencyCheck | None = None,
    *,
    readiness: ReadinessMode = ReadinessMode.LOCAL,
    deep_liveness: bool = False,
    heartbeat: Heartbeat | None = None,
    warmup: Callable[[], Awaitable[None]] | None = None,
) -> Starlette:
    """Probe app. `deep_liveness=True` is the anti-pattern switch, kept to prove the failure."""

    async def liveness(request: Request) -> Response:
        if heartbeat is not None and not heartbeat.fresh():
            return _health(503, {"status": "fail", "reason": "heartbeat-stale"})
        if deep_liveness and dependency is not None:
            result = await dependency.status()
            if not result.ok:
                return _health(503, {"status": "fail", "reason": f"{result.name}-down"})
        return _health(200, {"status": "pass"})

    async def startup(request: Request) -> Response:
        if state.phase is Phase.STARTING:
            return _health(503, {"status": "fail", "reason": "starting"})
        return _health(200, {"status": "pass"})

    async def ready(request: Request) -> Response:
        if state.phase is not Phase.SERVING:
            return _health(503, {"status": "fail", "reason": state.phase.value})
        if dependency is None:
            return _health(200, {"status": "pass"})
        result = await dependency.status()
        checks = {result.name: "pass" if result.ok else "fail"}
        if result.ok:
            return _health(200, {"status": "pass", "checks": checks})
        if readiness is ReadinessMode.DEPENDENCY:
            return _health(503, {"status": "fail", "checks": checks})
        return _health(200, {"status": "warn", "checks": checks})

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        if warmup is not None:
            await warmup()
        state.mark_started()
        try:
            yield
        finally:
            state.begin_drain()

    routes = [
        Route("/healthz", liveness),
        Route("/startupz", startup),
        Route("/readyz", ready),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


# --------------------------------------------------------------------------------------
# Part B: kubelet probe worker and fleet simulation (one tick = one simulated second)
# --------------------------------------------------------------------------------------


class DbMode(StrEnum):
    UP = "up"
    SLOW = "slow"  # answers, but a health ping takes `slow_latency_s`
    DOWN = "down"


class LivenessKind(StrEnum):
    SHALLOW = "shallow"
    DEEP = "deep"  # the liveness (and startup) endpoint pings the shared database


class ReadinessKind(StrEnum):
    LOCAL = "local"
    DEPENDENCY = "dependency"


@dataclass(frozen=True)
class ProbeSpec:
    """Defaults from `SetDefaults_Probe` in pkg/apis/core/v1/defaults.go (Kubernetes v1.36.0)."""

    period_s: int = 10
    timeout_s: int = 1
    success_threshold: int = 1
    failure_threshold: int = 3
    initial_delay_s: int = 0


def restart_window(spec: ProbeSpec) -> tuple[int, int]:
    """Outage lengths (whole ticks) that can, and are guaranteed to, trigger a liveness restart."""
    return (spec.failure_threshold - 1) * spec.period_s + 1, spec.failure_threshold * spec.period_s


class ProbeWorker:
    """Threshold logic of `worker.doProbe`: state flips only after a run of equal results."""

    def __init__(self, spec: ProbeSpec, *, initial: bool, phase_s: int) -> None:
        self.spec = spec
        self.state = initial
        self.phase_s = phase_s
        self._initial = initial
        self._last: bool | None = None
        self._run = 0

    def reset(self) -> None:
        """A new container ID: the result map is re-seeded with the initial value."""
        self.state = self._initial
        self._last = None
        self._run = 0

    def tick_due(self, t: int) -> bool:
        return (t - self.phase_s) % self.spec.period_s == 0

    def record(self, ok: bool) -> bool | None:
        """Return the new state if this result crossed a threshold, else None."""
        if self._last is ok:
            self._run += 1
        else:
            self._last = ok
            self._run = 1
        needed = self.spec.success_threshold if ok else self.spec.failure_threshold
        if self._run < needed:
            return None
        self.state = ok
        return ok


@dataclass(frozen=True)
class DbWindow:
    start_s: int
    end_s: int
    mode: DbMode


@dataclass(frozen=True)
class Fault:
    pod: int
    start_s: int
    end_s: int


@dataclass(frozen=True)
class Scenario:
    pods: int = 20
    duration_s: int = 240
    warmup_s: int = 30  # traffic and `min_ready` are measured from here
    liveness: LivenessKind = LivenessKind.SHALLOW
    readiness: ReadinessKind = ReadinessKind.LOCAL
    liveness_spec: ProbeSpec = field(
        default_factory=lambda: ProbeSpec(period_s=1, initial_delay_s=30)
    )
    readiness_spec: ProbeSpec = field(default_factory=lambda: ProbeSpec(period_s=1))
    startup_spec: ProbeSpec | None = None
    db_windows: tuple[DbWindow, ...] = ()
    slow_latency_s: float = 1.5
    boot_s: int = 20  # imports, config, cache warm before the pool is built
    db_connects_per_s: int = 4  # new pool connections the database admits per second
    rps_db: int = 50  # fleet-wide requests per second that need the database
    rps_other: int = 50  # fleet-wide requests per second that do not
    stalls: tuple[
        Fault, ...
    ] = ()  # event loop of one pod blocked: every probe and request times out
    pool_faults: tuple[Fault, ...] = ()  # one pod's own pool is broken while the database is up


@dataclass(frozen=True)
class Outcome:
    restarts: int
    ok_db: int
    fail_db: int
    ok_other: int
    fail_other: int
    zero_endpoint_s: int
    min_ready: int
    recovery_s: int | None  # seconds from the last database window to a fully ready fleet


@dataclass
class _Pod:
    idx: int
    liveness: ProbeWorker
    readiness: ProbeWorker
    startup: ProbeWorker | None
    container_start: int = 0
    app_up: bool = False
    restarts: int = 0

    def k_started(self) -> bool:
        return self.startup is None or self.startup.state

    def restart(self, t: int) -> None:
        self.container_start = t
        self.app_up = False
        self.restarts += 1
        self.liveness.reset()
        self.readiness.reset()
        if self.startup is not None:
            self.startup.reset()


def _in(faults: tuple[Fault, ...], pod: int, t: int) -> bool:
    return any(f.pod == pod and f.start_s <= t < f.end_s for f in faults)


def _share(total: int, k: int, n: int, t: int) -> int:
    base, rem = divmod(total, n)
    return base + (1 if (k + t) % n < rem else 0)


def simulate(sc: Scenario) -> Outcome:
    """Run the fleet for `sc.duration_s` ticks and count what the Service's clients experienced."""
    pods = [
        _Pod(
            idx=i,
            liveness=ProbeWorker(
                sc.liveness_spec, initial=True, phase_s=i % sc.liveness_spec.period_s
            ),
            readiness=ProbeWorker(
                sc.readiness_spec, initial=False, phase_s=i % sc.readiness_spec.period_s
            ),
            startup=(
                ProbeWorker(sc.startup_spec, initial=False, phase_s=i % sc.startup_spec.period_s)
                if sc.startup_spec
                else None
            ),
        )
        for i in range(sc.pods)
    ]

    def db_mode(t: int) -> DbMode:
        for w in sc.db_windows:
            if w.start_s <= t < w.end_s:
                return w.mode
        return DbMode.UP

    def ping_ok(pod: _Pod, spec: ProbeSpec, t: int) -> bool:
        mode = db_mode(t)
        if mode is DbMode.DOWN or _in(sc.pool_faults, pod.idx, t):
            return False
        return mode is not DbMode.SLOW or sc.slow_latency_s <= spec.timeout_s

    def handler_ok(pod: _Pod, spec: ProbeSpec, deep: bool, t: int) -> bool:
        if not pod.app_up or _in(sc.stalls, pod.idx, t):
            return False  # nothing is listening yet, or the event loop is blocked
        return ping_ok(pod, spec, t) if deep else True

    ok_db = fail_db = ok_other = fail_other = zero_endpoint_s = 0
    min_ready = sc.pods
    ready_series: list[int] = []
    for t in range(sc.duration_s):
        waiting = sorted(
            (p for p in pods if not p.app_up and t >= p.container_start + sc.boot_s),
            key=lambda p: (p.container_start, p.idx),
        )
        for admitted, p in enumerate(waiting):
            if db_mode(t) is DbMode.DOWN or admitted >= sc.db_connects_per_s:
                break
            p.app_up = True
        for p in pods:
            age = t - p.container_start
            deep_live = sc.liveness is LivenessKind.DEEP
            st = p.startup
            if (
                st is not None
                and not st.state
                and st.tick_due(t)
                and age >= st.spec.initial_delay_s
                and st.record(handler_ok(p, st.spec, deep_live, t)) is False
            ):
                p.restart(t)
                continue
            if not p.k_started():
                continue
            if p.liveness.tick_due(t) and age >= p.liveness.spec.initial_delay_s:
                new = p.liveness.record(handler_ok(p, p.liveness.spec, deep_live, t))
                if new is False:
                    p.restart(t)
                    continue
            if p.readiness.tick_due(t) and age >= p.readiness.spec.initial_delay_s:
                dep = sc.readiness is ReadinessKind.DEPENDENCY
                p.readiness.record(handler_ok(p, p.readiness.spec, dep, t))
        ready = [p for p in pods if p.k_started() and p.readiness.state]
        ready_series.append(len(ready))
        measured = t >= sc.warmup_s
        if measured:
            min_ready = min(min_ready, len(ready))
        if not ready:
            if measured:
                zero_endpoint_s += 1
                fail_db += sc.rps_db
                fail_other += sc.rps_other
            continue
        for k, p in enumerate(ready):
            n_db = _share(sc.rps_db, k, len(ready), t)
            n_other = _share(sc.rps_other, k, len(ready), t)
            dead = not p.app_up or _in(sc.stalls, p.idx, t)
            db_bad = dead or db_mode(t) is DbMode.DOWN or _in(sc.pool_faults, p.idx, t)
            if not measured:
                continue
            fail_other += n_other if dead else 0
            ok_other += 0 if dead else n_other
            fail_db += n_db if db_bad else 0
            ok_db += 0 if db_bad else n_db

    recovery: int | None = None
    if sc.db_windows:
        last_end = max(w.end_s for w in sc.db_windows)
        full_at = sc.duration_s
        for t in range(sc.duration_s - 1, -1, -1):
            if ready_series[t] != sc.pods:
                break
            full_at = t
        recovery = max(0, full_at - last_end) if ready_series[-1] == sc.pods else None
    return Outcome(
        restarts=sum(p.restarts for p in pods),
        ok_db=ok_db,
        fail_db=fail_db,
        ok_other=ok_other,
        fail_other=fail_other,
        zero_endpoint_s=zero_endpoint_s,
        min_ready=min_ready,
        recovery_s=recovery,
    )


def blip(length_s: int, mode: DbMode = DbMode.DOWN, start_s: int = 60) -> tuple[DbWindow, ...]:
    return (DbWindow(start_s, start_s + length_s, mode),)


def main() -> None:
    """Print the comparison table that the note shows."""
    designs = {
        "deep liveness + local readiness": (LivenessKind.DEEP, ReadinessKind.LOCAL),
        "shallow liveness + dependency readiness": (LivenessKind.SHALLOW, ReadinessKind.DEPENDENCY),
        "shallow liveness + local readiness": (LivenessKind.SHALLOW, ReadinessKind.LOCAL),
    }
    print(
        "5 s database outage at t=60 s, 20 pods, probes every 1 s (period_s=1, failureThreshold=3)"
    )
    print(
        f"{'design':44} {'restarts':>8} {'0-ready s':>9} {'db fail':>8} "
        f"{'other fail':>10} {'recovery s':>10}"
    )
    for name, (live, ready) in designs.items():
        out = simulate(Scenario(liveness=live, readiness=ready, db_windows=blip(5)))
        print(
            f"{name:44} {out.restarts:8d} {out.zero_endpoint_s:9d} {out.fail_db:8d} "
            f"{out.fail_other:10d} {out.recovery_s!s:>10}"
        )
    spec = ProbeSpec()
    print(f"\nrestart window for the default probe {spec}: {restart_window(spec)}")
    print("restarts of 20 pods with deep liveness, default probe (period 10, failureThreshold 3):")
    for length in (5, 20, 21, 25, 29, 30, 40):
        out = simulate(
            Scenario(
                liveness=LivenessKind.DEEP,
                liveness_spec=ProbeSpec(initial_delay_s=30),
                readiness_spec=ProbeSpec(),
                db_windows=blip(length),
            )
        )
        print(f"  outage {length:2d} s -> {out.restarts:2d} restarts")


if __name__ == "__main__":
    main()
