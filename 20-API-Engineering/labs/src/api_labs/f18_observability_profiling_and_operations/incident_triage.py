"""Sixty-second API incident triage: a deterministic decision tree over one telemetry window.

The tree encodes the order in which a responder rules causes in or out: what changed, did demand
change, is the process itself stuck, which shared pool is full, which dependency is slow.
Each node reads only fields that one dashboard row or one query returns.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class Cause(StrEnum):
    """The verdicts the tree can reach."""

    HEALTHY = "healthy"
    BAD_DEPLOY = "bad_deploy"
    TRAFFIC_SPIKE = "traffic_spike"
    EVENT_LOOP_BLOCKING = "event_loop_blocking"
    DB_LOCK_CONVOY = "db_lock_convoy"
    DOWNSTREAM_STALL = "downstream_stall"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class Baseline:
    """What the same service looked like at the same hour of a normal week."""

    rps: float
    p99_latency_s: float
    downstream_p99_s: float


@dataclass(frozen=True, slots=True)
class Canary:
    """The same signals split by build, available only while two builds serve traffic."""

    new_ratio_5xx: float
    old_ratio_5xx: float
    new_p99_s: float
    old_p99_s: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    """One window (about 60 s) of telemetry for one service.

    ``in_flight`` is the time-averaged number of requests inside the service, so
    ``rps * mean_latency_s`` should reproduce it (Little's law).
    ``cpu_util`` is the busy fraction of the event-loop thread, not of the whole host.
    """

    rps: float
    mean_latency_s: float
    p99_latency_s: float
    in_flight: float
    ratio_5xx: float
    ratio_429: float
    retry_fraction: float
    cpu_util: float
    loop_lag_p99_s: float
    db_pool_size: int
    db_pool_in_use: int
    db_lock_waiters: int
    downstream_p99_s: float
    downstream_timeout_ratio: float
    threadpool_limit: int
    threadpool_in_use: int
    change_age_s: float | None = None
    onset_age_s: float | None = None
    canary: Canary | None = None

    def __post_init__(self) -> None:
        ratios = (
            self.ratio_5xx,
            self.ratio_429,
            self.retry_fraction,
            self.cpu_util,
            self.downstream_timeout_ratio,
        )
        if any(not 0.0 <= value <= 1.0 for value in ratios):
            raise ValueError("ratios and cpu_util must lie in [0, 1]")
        durations = (
            self.rps,
            self.mean_latency_s,
            self.p99_latency_s,
            self.in_flight,
            self.loop_lag_p99_s,
            self.downstream_p99_s,
        )
        if any(value < 0.0 for value in durations):
            raise ValueError("rates, latencies, and counts must be non-negative")
        if not 0 <= self.db_pool_in_use <= self.db_pool_size:
            raise ValueError("db_pool_in_use must lie in [0, db_pool_size]")
        if not 0 <= self.threadpool_in_use <= self.threadpool_limit:
            raise ValueError("threadpool_in_use must lie in [0, threadpool_limit]")


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Decision thresholds; each default is a starting point to tune per service."""

    latency_factor: float = 2.0
    error_ratio: float = 0.01
    traffic_factor: float = 1.5
    limiter_ratio: float = 0.05
    retry_fraction: float = 0.2
    loop_lag_s: float = 0.1
    cpu_bound_util: float = 0.8
    pool_saturation: float = 0.9
    lock_waiters: int = 2
    downstream_factor: float = 3.0
    downstream_timeout_ratio: float = 0.05
    canary_error_factor: float = 3.0
    deploy_lead_s: float = 300.0
    little_tolerance: float = 0.5


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """A verdict, the nodes visited to reach it, and what to do in the next minute."""

    cause: Cause
    detail: str
    path: tuple[str, ...]
    evidence: tuple[str, ...]
    action: str
    warnings: tuple[str, ...]


DEFAULT_THRESHOLDS = Thresholds()
BASELINE = Baseline(rps=200.0, p99_latency_s=0.25, downstream_p99_s=0.05)


def littles_law_gap(snap: Snapshot) -> float:
    """Relative disagreement between ``in_flight`` and ``rps * mean_latency_s`` (0 is perfect)."""
    expected = snap.rps * snap.mean_latency_s
    scale = max(expected, snap.in_flight)
    if scale == 0.0:
        return 0.0
    return abs(snap.in_flight - expected) / scale


def offered_load_multiplier(attempts_per_layer: int, layers: int) -> int:
    """Worst-case load on the deepest dependency when every layer retries independently."""
    if attempts_per_layer < 1 or layers < 0:
        raise ValueError("need attempts_per_layer >= 1 and layers >= 0")
    multiplier: int = attempts_per_layer**layers
    return multiplier


def _canary_is_worse(canary: Canary, th: Thresholds) -> bool:
    error_floor = max(th.error_ratio, th.canary_error_factor * canary.old_ratio_5xx)
    errors = canary.new_ratio_5xx >= error_floor
    slower = canary.new_p99_s >= th.latency_factor * canary.old_p99_s
    return errors or slower


def diagnose(
    snap: Snapshot, base: Baseline = BASELINE, th: Thresholds = DEFAULT_THRESHOLDS
) -> Diagnosis:
    """Walk the tree and return the first cause that the evidence supports."""
    path: list[str] = ["sanity"]
    warnings: list[str] = []
    gap = littles_law_gap(snap)
    if gap > th.little_tolerance:
        warnings.append(
            f"in_flight {snap.in_flight:.0f} vs rps x mean latency "
            f"{snap.rps * snap.mean_latency_s:.0f}: the windows disagree, re-query before trusting"
        )

    def done(cause: Cause, detail: str, evidence: list[str], action: str) -> Diagnosis:
        return Diagnosis(cause, detail, tuple(path), tuple(evidence), action, tuple(warnings))

    path.append("degraded")
    latency_factor = snap.p99_latency_s / base.p99_latency_s
    degraded = (
        latency_factor >= th.latency_factor
        or snap.ratio_5xx >= th.error_ratio
        or snap.ratio_429 >= th.limiter_ratio
    )
    if not degraded:
        return done(
            Cause.HEALTHY,
            "no signal beyond thresholds",
            [f"p99 {snap.p99_latency_s:.2f}s is {latency_factor:.1f}x baseline"],
            "no action",
        )

    retries_dominate = snap.retry_fraction >= th.retry_fraction
    traffic_elevated = snap.rps >= th.traffic_factor * base.rps
    organic_traffic = traffic_elevated and not retries_dominate
    limiter_engaged = snap.ratio_429 >= th.limiter_ratio
    if retries_dominate:
        warnings.append(
            f"{snap.retry_fraction:.0%} of requests are retries: the extra traffic is a symptom, "
            "keep looking for the first failure"
        )

    path.append("what_changed")
    if snap.canary is not None:
        if _canary_is_worse(snap.canary, th):
            return done(
                Cause.BAD_DEPLOY,
                "canary split",
                [
                    f"new build 5xx {snap.canary.new_ratio_5xx:.1%} vs old "
                    f"{snap.canary.old_ratio_5xx:.1%}",
                    f"new build p99 {snap.canary.new_p99_s:.2f}s vs old "
                    f"{snap.canary.old_p99_s:.2f}s",
                ],
                "roll back or flip the kill switch first, then root-cause",
            )
        warnings.append("a canary split exists and the new build is not worse: deploy exonerated")
    elif snap.change_age_s is not None and snap.onset_age_s is not None:
        lead = snap.change_age_s - snap.onset_age_s
        if 0.0 <= lead <= th.deploy_lead_s:
            if not organic_traffic:
                return done(
                    Cause.BAD_DEPLOY,
                    "time correlation only",
                    [f"degradation began {lead:.0f}s after the last deploy or flag change"],
                    "roll back or flip the kill switch; a clean recovery is the confirmation",
                )
            warnings.append("a change preceded the onset, but organic traffic is also elevated")

    path.append("traffic")
    if (organic_traffic or limiter_engaged) and not retries_dominate:
        return done(
            Cause.TRAFFIC_SPIKE,
            "limiter engaged" if limiter_engaged else "demand above capacity",
            [
                f"rps {snap.rps:.0f} is {snap.rps / base.rps:.1f}x baseline",
                f"429 ratio {snap.ratio_429:.1%}, cpu {snap.cpu_util:.0%}",
            ],
            "find the top clients; shed low-priority traffic; add capacity only if broad",
        )

    path.append("event_loop")
    if snap.loop_lag_p99_s >= th.loop_lag_s:
        cpu_bound = snap.cpu_util >= th.cpu_bound_util
        return done(
            Cause.EVENT_LOOP_BLOCKING,
            "cpu-bound code on the loop" if cpu_bound else "blocking io on the loop",
            [
                f"loop lag p99 {snap.loop_lag_p99_s * 1000:.0f} ms (threshold "
                f"{th.loop_lag_s * 1000:.0f} ms)",
                f"cpu {snap.cpu_util:.0%} with rps {snap.rps:.0f} near baseline",
                "downstream and pool numbers measured on the loop are inflated by the stall",
            ],
            "sample the loop thread (py-spy dump) and move the blocking call off the loop",
        )

    path.append("thread_pool")
    threads_full = th.pool_saturation * snap.threadpool_limit
    if snap.threadpool_limit and snap.threadpool_in_use >= threads_full:
        return done(
            Cause.DOWNSTREAM_STALL,
            "sync handlers parked on blocking calls",
            [f"threadpool {snap.threadpool_in_use}/{snap.threadpool_limit} tokens borrowed"],
            "dump thread stacks, find the blocking call, give it a timeout",
        )

    path.append("db_pool")
    pool_full = snap.db_pool_in_use >= th.pool_saturation * snap.db_pool_size
    downstream_slow = (
        snap.downstream_p99_s >= th.downstream_factor * base.downstream_p99_s
        or snap.downstream_timeout_ratio >= th.downstream_timeout_ratio
    )
    if pool_full and snap.db_lock_waiters >= th.lock_waiters:
        return done(
            Cause.DB_LOCK_CONVOY,
            "sessions queued on a lock",
            [
                f"pool {snap.db_pool_in_use}/{snap.db_pool_size} in use",
                f"{snap.db_lock_waiters} sessions waiting on a lock, cpu {snap.cpu_util:.0%}",
            ],
            "find the head of the lock queue with pg_blocking_pids and end that session",
        )
    if pool_full:
        detail = (
            "connections held across a slow outbound call"
            if downstream_slow
            else "slow queries holding connections"
        )
        return done(
            Cause.DOWNSTREAM_STALL,
            detail,
            [f"pool {snap.db_pool_in_use}/{snap.db_pool_size} in use, no lock waiters"],
            "check the dependency or the slowest queries; release connections before outbound I/O",
        )

    path.append("downstream")
    if downstream_slow:
        return done(
            Cause.DOWNSTREAM_STALL,
            "slow dependency",
            [
                f"downstream p99 {snap.downstream_p99_s:.2f}s vs baseline "
                f"{base.downstream_p99_s:.2f}s, timeouts {snap.downstream_timeout_ratio:.0%}",
            ],
            "cut timeouts, stop retries into it, open the breaker, fail over or degrade",
        )

    if snap.db_lock_waiters >= th.lock_waiters:
        warnings.append("lock waiters exist but the pool is not saturated: not the bottleneck yet")
    return done(
        Cause.INCONCLUSIVE,
        "no node matched",
        ["degraded, but no shared resource is saturated and no dependency is slow"],
        "widen the window, compare by endpoint and by pod, check the edge and the client side",
    )


def _healthy() -> Snapshot:
    return Snapshot(
        rps=200.0,
        mean_latency_s=0.05,
        p99_latency_s=0.25,
        in_flight=10.0,
        ratio_5xx=0.001,
        ratio_429=0.0,
        retry_fraction=0.02,
        cpu_util=0.35,
        loop_lag_p99_s=0.004,
        db_pool_size=20,
        db_pool_in_use=4,
        db_lock_waiters=0,
        downstream_p99_s=0.05,
        downstream_timeout_ratio=0.0,
        threadpool_limit=40,
        threadpool_in_use=3,
    )


def with_latency(snap: Snapshot, *, mean_s: float, p99_s: float, **changes: object) -> Snapshot:
    """Copy *snap* with new latencies and an ``in_flight`` that obeys Little's law."""
    return replace(
        snap,
        mean_latency_s=mean_s,
        p99_latency_s=p99_s,
        in_flight=snap.rps * mean_s,
        **changes,  # type: ignore[arg-type]
    )


def scenario(name: str) -> Snapshot:
    """Return the named simulated snapshot; every one is Little-consistent by construction."""
    base = _healthy()
    if name == "healthy":
        return base
    if name == "traffic_spike":
        return with_latency(
            replace(base, rps=520.0), mean_s=0.9, p99_s=4.0,
            ratio_429=0.30, cpu_util=0.98, loop_lag_p99_s=0.35, db_pool_in_use=20,
        )  # fmt: skip
    if name == "rate_limited_tenant":
        return with_latency(
            replace(base, rps=230.0), mean_s=0.06, p99_s=0.3, ratio_429=0.12, cpu_util=0.42
        )
    if name == "downstream_stall":
        return with_latency(
            base, mean_s=2.5, p99_s=3.0,
            ratio_5xx=0.4, retry_fraction=0.1, cpu_util=0.12,
            downstream_p99_s=3.0, downstream_timeout_ratio=0.4,
        )  # fmt: skip
    if name == "db_lock_convoy":
        return with_latency(
            base, mean_s=4.0, p99_s=10.0,
            ratio_5xx=0.35, retry_fraction=0.15, cpu_util=0.08,
            db_pool_in_use=20, db_lock_waiters=17,
        )  # fmt: skip
    if name == "event_loop_cpu":
        return with_latency(
            base, mean_s=1.2, p99_s=3.5,
            ratio_5xx=0.05, cpu_util=0.99, loop_lag_p99_s=1.1,
            db_pool_in_use=20, downstream_p99_s=0.9,
        )  # fmt: skip
    if name == "event_loop_blocking_io":
        return with_latency(
            base, mean_s=1.2, p99_s=3.5,
            ratio_5xx=0.05, cpu_util=0.08, loop_lag_p99_s=0.6,
            db_pool_in_use=20, downstream_p99_s=0.9,
        )  # fmt: skip
    if name == "threadpool_saturation":
        return with_latency(
            base, mean_s=3.0, p99_s=6.0,
            ratio_5xx=0.1, cpu_util=0.1, threadpool_in_use=40,
        )  # fmt: skip
    if name == "bad_deploy_canary":
        return with_latency(
            replace(
                base,
                canary=Canary(0.55, 0.002, 1.5, 0.24),
                change_age_s=420.0,
                onset_age_s=360.0,
            ),
            mean_s=0.1, p99_s=1.5, ratio_5xx=0.06,
        )  # fmt: skip
    if name == "bad_deploy_full_rollout":
        return replace(base, ratio_5xx=0.5, change_age_s=300.0, onset_age_s=240.0)
    if name == "deploy_during_spike":
        return with_latency(
            replace(base, rps=600.0, change_age_s=300.0, onset_age_s=280.0),
            mean_s=1.0, p99_s=4.5, cpu_util=0.97,
        )  # fmt: skip
    if name == "retry_storm_over_stall":
        return with_latency(
            replace(base, rps=520.0), mean_s=2.5, p99_s=3.0,
            ratio_5xx=0.4, retry_fraction=0.6, cpu_util=0.15,
            downstream_p99_s=3.0, downstream_timeout_ratio=0.5,
        )  # fmt: skip
    raise KeyError(name)


SCENARIO_NAMES: tuple[str, ...] = (
    "healthy",
    "traffic_spike",
    "rate_limited_tenant",
    "downstream_stall",
    "db_lock_convoy",
    "event_loop_cpu",
    "event_loop_blocking_io",
    "threadpool_saturation",
    "bad_deploy_canary",
    "bad_deploy_full_rollout",
    "deploy_during_spike",
    "retry_storm_over_stall",
)
