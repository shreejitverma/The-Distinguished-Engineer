"""Deterministic models of rolling, blue-green, canary, and cell-based deployments.

Everything here is pure arithmetic or an in-process ASGI app: no sockets, no clocks, no
randomness.
Each section mirrors one mechanism of the note and cites the source it models.
"""

import hashlib
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import pairwise

from starlette.types import Receive, Scope, Send

# ---------------------------------------------------------------------------------------
# Rolling update: the Kubernetes Deployment controller's fenceposts and reconcile order
# (kubernetes v1.37.2 pkg/controller/deployment/util/deployment_util.go, ResolveFenceposts;
# pkg/controller/deployment/rolling.go, rolloutRolling).
# ---------------------------------------------------------------------------------------

IntOrPercent = int | str


def scale_fencepost(value: IntOrPercent, total: int, *, round_up: bool) -> int:
    """Resolve an int-or-percent field the way apimachinery GetScaledValueFromIntOrPercent does."""
    if isinstance(value, int):
        return value
    if not value.endswith("%") or not value[:-1].isdigit():
        raise ValueError(f"invalid int-or-percent value: {value!r}")
    numerator = int(value[:-1]) * total
    return -(-numerator // 100) if round_up else numerator // 100


@dataclass(frozen=True, slots=True)
class Fenceposts:
    """Resolved ``maxSurge`` and ``maxUnavailable`` for one Deployment size."""

    surge: int
    unavailable: int


def resolve_fenceposts(
    max_surge: IntOrPercent, max_unavailable: IntOrPercent, desired: int
) -> Fenceposts:
    """Surge rounds up, unavailable rounds down, and a double zero forces unavailable to 1."""
    surge = scale_fencepost(max_surge, desired, round_up=True)
    unavailable = scale_fencepost(max_unavailable, desired, round_up=False)
    if surge == 0 and unavailable == 0:
        unavailable = 1
    return Fenceposts(surge, min(unavailable, desired))


@dataclass(frozen=True, slots=True)
class RolloutTick:
    """Pod counts after one controller sync."""

    tick: int
    old: int
    new: int
    available: int

    @property
    def total(self) -> int:
        return self.old + self.new

    @property
    def mixed(self) -> bool:
        return self.old > 0 and self.new > 0


@dataclass(frozen=True, slots=True)
class RolloutResult:
    """The full trace of a simulated rollout."""

    ticks: tuple[RolloutTick, ...]
    completed: bool

    @property
    def max_total(self) -> int:
        return max(t.total for t in self.ticks)

    @property
    def min_available(self) -> int:
        return min(t.available for t in self.ticks)

    @property
    def mixed_ticks(self) -> int:
        return sum(1 for t in self.ticks if t.mixed)


def simulate_rolling_update(
    desired: int,
    max_surge: IntOrPercent = "25%",
    max_unavailable: IntOrPercent = "25%",
    *,
    new_pods_become_ready: bool = True,
    max_ticks: int = 200,
) -> RolloutResult:
    """Replay the controller: scale the new set up first, otherwise scale the old set down.

    A pod created in one tick is ready at the start of the next.
    Scale-down is instantaneous, so terminating pods and drain time are not modelled.
    """
    fences = resolve_fenceposts(max_surge, max_unavailable, desired)
    min_available = desired - fences.unavailable
    old, new, new_ready = desired, 0, 0
    ticks: list[RolloutTick] = []
    previous = (old, new, new_ready)
    for tick in range(1, max_ticks + 1):
        new_ready = new if new_pods_become_ready else 0
        max_total = desired + fences.surge
        if old + new < max_total:  # NewRSNewReplicas: scale up, capped at the desired size
            new += min(max_total - (old + new), desired - new)
        else:
            available = old + new_ready
            max_scaled_down = (old + new) - min_available - (new - new_ready)
            if max_scaled_down > 0 and available > min_available:
                old -= min(old, available - min_available)
        ticks.append(RolloutTick(tick, old, new, old + new_ready))
        if old == 0 and new == desired and new_ready == desired:
            return RolloutResult(tuple(ticks), completed=True)
        state = (old, new, new_ready)
        if state == previous:
            break
        previous = state
    return RolloutResult(tuple(ticks), completed=False)


# ---------------------------------------------------------------------------------------
# Blue-green: one atomic pointer flip, connection pinning, and the HTTP/2 GOAWAY rules
# (RFC 9113 Section 6.8).
# ---------------------------------------------------------------------------------------


class Color(Enum):
    BLUE = "blue"
    GREEN = "green"

    @property
    def other(self) -> "Color":
        return Color.GREEN if self is Color.BLUE else Color.BLUE


@dataclass(slots=True)
class Connection:
    """A client connection pinned to whichever environment was live when it was opened."""

    color: Color
    closed: bool = False


@dataclass(slots=True)
class BlueGreenRouter:
    """Two identical environments and a router that points at one of them."""

    active: Color = Color.BLUE
    inflight: dict[Color, int] = field(default_factory=lambda: {Color.BLUE: 0, Color.GREEN: 0})

    def accept(self) -> Color:
        """Route one request per-request (an L7 decision) and count it in flight."""
        self.inflight[self.active] += 1
        return self.active

    def finish(self, color: Color) -> None:
        if self.inflight[color] <= 0:
            raise ValueError(f"no request in flight on {color.value}")
        self.inflight[color] -= 1

    def connect(self) -> Connection:
        return Connection(self.active)

    def route(self, conn: Connection, *, per_request: bool) -> Color:
        """An L4 balancer keeps the connection's environment; an L7 one re-decides each request."""
        if conn.closed:
            raise ValueError("connection is closed")
        return self.active if per_request else conn.color

    def cut_over(self) -> Color:
        """Flip the pointer and return the environment that is now idle (the rollback target)."""
        previous = self.active
        self.active = previous.other
        return previous

    def drained(self, color: Color) -> bool:
        """True when *color* is not live and has no request in flight."""
        return color is not self.active and self.inflight[color] == 0


MAX_STREAM_ID = 2**31 - 1


def retryable_streams(opened: Iterable[int], last_stream_id: int) -> tuple[int, ...]:
    """Streams above the GOAWAY last-stream-id were never processed and may move connections."""
    return tuple(sorted(s for s in opened if s > last_stream_id))


def check_goaway_sequence(last_stream_ids: Sequence[int]) -> None:
    """An endpoint MUST NOT increase the last stream identifier it sends (RFC 9113 Section 6.8)."""
    for earlier, later in pairwise(last_stream_ids):
        if later > earlier:
            raise ValueError(f"GOAWAY last stream id increased from {earlier} to {later}")


# ---------------------------------------------------------------------------------------
# Canary: sticky bucketing, weighted selection, and the statistics of a small sample.
# ---------------------------------------------------------------------------------------

BUCKETS = 10_000


def _digest64(*parts: str) -> int:
    data = "\x00".join(parts).encode()
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "big")


def bucket(key: str, salt: str) -> int:
    """Map a stable key (user, tenant, session) to one of 10000 buckets."""
    return _digest64(salt, key) % BUCKETS


def in_canary(key: str, salt: str, basis_points: int) -> bool:
    """A key is in the canary while its bucket is below the weight, so ramps only add keys."""
    return bucket(key, salt) < basis_points


def weighted_pick(weights: Sequence[tuple[str, int]], point: int) -> str:
    """Gateway API semantics: share = weight / sum(weights), weight 0 receives nothing."""
    if any(w < 0 for _, w in weights):
        raise ValueError("weights must be >= 0")
    total = sum(w for _, w in weights)
    if total <= 0 or not 0 <= point < total:
        raise ValueError("point must be in [0, sum of weights) and the sum must be > 0")
    for name, weight in weights:
        if point < weight:
            return name
        point -= weight
    raise AssertionError("unreachable")


def blended_error_rate(canary_share: float, canary_rate: float, stable_rate: float) -> float:
    """The fleet-wide error rate hides a canary regression in proportion to its share."""
    return canary_share * canary_rate + (1.0 - canary_share) * stable_rate


def zero_failure_upper_bound(requests: int, confidence: float = 0.95) -> float:
    """Largest true failure rate still compatible with seeing no failure in *requests* (exact)."""
    return 1.0 - float((1.0 - confidence) ** (1.0 / requests))


def requests_to_see_one_failure(rate: float, confidence: float = 0.95) -> int:
    """Smallest sample in which a true failure *rate* shows at least one failure."""
    return math.ceil(math.log(1.0 - confidence) / math.log(1.0 - rate))


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact_greater(
    canary_failures: int, canary_requests: int, control_failures: int, control_requests: int
) -> float:
    """One-sided Fisher exact p-value that the canary fails more often than the control."""
    total = canary_requests + control_requests
    failures = canary_failures + control_failures
    log_denominator = _log_comb(total, canary_requests)
    tail = 0.0
    for k in range(canary_failures, min(failures, canary_requests) + 1):
        log_term = (
            _log_comb(failures, k) + _log_comb(total - failures, canary_requests - k)
        ) - log_denominator
        tail += math.exp(log_term)
    return min(tail, 1.0)


class Verdict(Enum):
    PROMOTE = "promote"
    HOLD = "hold"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class Observation:
    """Failures out of requests for one population over one window."""

    failures: int
    requests: int

    def __post_init__(self) -> None:
        if not 0 <= self.failures <= self.requests:
            raise ValueError("need 0 <= failures <= requests")

    @property
    def rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0


@dataclass(frozen=True, slots=True)
class JudgePolicy:
    """Lab thresholds, not a standard: Flagger and Kayenta expose their own."""

    min_requests: int = 300
    alpha: float = 0.01
    min_excess: float = 0.001


DEFAULT_POLICY = JudgePolicy()


def judge(
    canary: Observation, control: Observation, policy: JudgePolicy = DEFAULT_POLICY
) -> Verdict:
    """Hold on too little data, roll back on a significant excess, otherwise promote."""
    if canary.requests < policy.min_requests or control.requests < policy.min_requests:
        return Verdict.HOLD
    excess = canary.rate - control.rate
    p_value = fisher_exact_greater(
        canary.failures, canary.requests, control.failures, control.requests
    )
    if excess >= policy.min_excess and p_value < policy.alpha:
        return Verdict.ROLLBACK
    return Verdict.PROMOTE


@dataclass(frozen=True, slots=True)
class CanaryOutcome:
    verdict: Verdict
    last_step_percent: int
    steps_run: int


def run_canary(
    steps_percent: Sequence[int],
    observe: Callable[[int], tuple[Observation, Observation]],
    policy: JudgePolicy = DEFAULT_POLICY,
) -> CanaryOutcome:
    """Ramp through *steps_percent*; stop at the first step that is not a clear promote."""
    for index, percent in enumerate(steps_percent):
        canary, control = observe(percent)
        verdict = judge(canary, control, policy)
        if verdict is not Verdict.PROMOTE:
            return CanaryOutcome(verdict, percent, index + 1)
    return CanaryOutcome(Verdict.PROMOTE, steps_percent[-1], len(steps_percent))


# ---------------------------------------------------------------------------------------
# Cells: deterministic placement, blast radius of a poison pill, and staged application.
# ---------------------------------------------------------------------------------------


def cell_for_mod(tenant: str, n_cells: int, salt: str = "cells-v1") -> int:
    """hash(tenant) mod N: simple and uniform, but N changing remaps almost every tenant."""
    return _digest64(salt, tenant) % n_cells


def cell_for_rendezvous(tenant: str, cells: Sequence[str]) -> str:
    """Highest-random-weight hashing: adding a cell only moves tenants that choose the new one."""
    return max(cells, key=lambda c: (_digest64(c, tenant), c))


def top_k_workers(tenant: str, workers: Sequence[str], k: int) -> frozenset[str]:
    """The tenant's shuffle shard: its k highest-scoring workers."""
    ranked = sorted(workers, key=lambda w: (-_digest64(w, tenant), w))
    return frozenset(ranked[:k])


def shuffle_shard_count(workers: int, k: int) -> int:
    """Number of distinct shuffle shards of size *k* over *workers* workers."""
    return math.comb(workers, k)


def moved_fraction(
    tenants: Sequence[str], before: Callable[[str], str], after: Callable[[str], str]
) -> float:
    """Share of tenants whose cell changes between two placement functions."""
    return sum(1 for t in tenants if before(t) != after(t)) / len(tenants)


class Layout(Enum):
    SHARED = "shared"
    CELLS = "cells"
    SHUFFLE = "shuffle"


def assignment(
    layout: Layout, tenant: str, workers: Sequence[str], *, cell_count: int = 1, k: int = 2
) -> frozenset[str]:
    """The workers that may serve *tenant* under each layout."""
    if layout is Layout.SHARED:
        return frozenset(workers)
    if layout is Layout.CELLS:
        if len(workers) % cell_count:
            raise ValueError("workers must divide evenly into cells")
        size = len(workers) // cell_count
        cell = cell_for_mod(tenant, cell_count)
        return frozenset(workers[cell * size : (cell + 1) * size])
    return top_k_workers(tenant, workers, k)


@dataclass(frozen=True, slots=True)
class PoisonImpact:
    """How many tenants lost every worker, some workers, or none."""

    fully_down: int
    degraded: int
    unaffected: int


def poison_pill_impact(
    layout: Layout,
    tenants: Sequence[str],
    workers: Sequence[str],
    poison_tenant: str,
    *,
    cell_count: int = 1,
    k: int = 2,
) -> PoisonImpact:
    """A poison request crashes each worker it reaches; a retrying client reaches its whole set."""
    dead = assignment(layout, poison_tenant, workers, cell_count=cell_count, k=k)
    down = degraded = 0
    for tenant in tenants:
        mine = assignment(layout, tenant, workers, cell_count=cell_count, k=k)
        if mine <= dead:
            down += 1
        elif mine & dead:
            degraded += 1
    return PoisonImpact(down, degraded, len(tenants) - down - degraded)


@dataclass(frozen=True, slots=True)
class StagedResult:
    applied: tuple[str, ...]
    unhealthy: tuple[str, ...]
    halted: bool


def staged_apply(
    cells: Sequence[str],
    waves: Sequence[int],
    apply: Callable[[str], None],
    healthy: Callable[[str], bool],
) -> StagedResult:
    """Apply a change wave by wave and stop after the first wave that reports an unhealthy cell."""
    if sum(waves) < len(cells) or any(w < 1 for w in waves):
        raise ValueError("waves must be positive and cover every cell")
    applied: list[str] = []
    start = 0
    for size in waves:
        batch = cells[start : start + size]
        if not batch:
            break
        for cell in batch:
            apply(cell)
            applied.append(cell)
        bad = tuple(c for c in batch if not healthy(c))
        if bad:
            return StagedResult(tuple(applied), bad, halted=True)
        start += size
    return StagedResult(tuple(applied), (), halted=False)


def crashed_cells_after_bad_data_push(
    n_cells: int, binary_waves: Sequence[int], *, data_waves: Sequence[int] | None
) -> int:
    """A latent crash ships unexercised, then a blank-field data push meets it.

    ``data_waves=None`` replicates the data to every cell at once (the 2025-06-12 Service
    Control shape); a wave list stages the same push cell by cell.
    """
    cells = [f"cell-{i}" for i in range(n_cells)]
    has_bug: set[str] = set()
    staged_apply(cells, binary_waves, has_bug.add, lambda _cell: True)  # path never exercised
    crashed: set[str] = set()

    def push(cell: str) -> None:
        if cell in has_bug:
            crashed.add(cell)

    if data_waves is None:
        for cell in cells:
            push(cell)
    else:
        staged_apply(cells, data_waves, push, lambda cell: cell not in crashed)
    return len(crashed)


class CellRouterApp:
    """The thinnest cell router: hash the tenant, pick a cell, never inspect the payload."""

    def __init__(self, n_cells: int, dead_cells: frozenset[int] = frozenset()) -> None:
        self.n_cells = n_cells
        self.dead_cells = dead_cells

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await _drain_body(receive)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        tenant = headers.get("x-tenant-id")
        if not tenant:
            await _respond(send, 400, [])
            return
        cell = cell_for_mod(tenant, self.n_cells)
        out = [(b"x-cell", str(cell).encode())]
        if cell in self.dead_cells:
            out.append((b"retry-after", b"30"))
            await _respond(send, 503, out)
        else:
            await _respond(send, 200, out)


async def _drain_body(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body", False):
            return


async def _respond(send: Send, status: int, headers: list[tuple[bytes, bytes]]) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-length", b"0"), *headers],
        }
    )
    await send({"type": "http.response.body", "body": b""})
