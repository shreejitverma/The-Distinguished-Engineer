"""Deterministic model of Envoy outlier detection, priority load, and panic mode.

The model follows Envoy v1.40.2 `source/common/upstream/outlier_detection_impl.cc` and
`source/extensions/load_balancing_policies/common/load_balancer_impl.cc`.
It leaves out degraded hosts, locality weighting, host weights, and the cross-thread post
between worker threads and the main thread: detection and ejection run inline here.
Time is an explicit integer of milliseconds and randomness is injected, so every run is exact.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

GATEWAY_CODES = frozenset({502, 503, 504})
DEFAULT_OVERPROVISIONING_FACTOR = 140
DEFAULT_PANIC_THRESHOLD = 50
NO_HEALTHY_UPSTREAM = "no healthy upstream"


class EjectionKind(Enum):
    """The ejection types this model implements."""

    CONSECUTIVE_5XX = "consecutive_5xx"
    CONSECUTIVE_GATEWAY_FAILURE = "consecutive_gateway_failure"
    SUCCESS_RATE = "success_rate"


@dataclass(frozen=True, slots=True)
class OutlierConfig:
    """Envoy `OutlierDetection` fields with the v1.40.2 defaults."""

    consecutive_5xx: int = 5
    consecutive_gateway_failure: int = 5
    interval_ms: int = 10_000
    base_ejection_time_ms: int = 30_000
    max_ejection_time_ms: int | None = None
    max_ejection_time_jitter_ms: int = 0
    max_ejection_percent: int = 10
    always_eject_one_host: bool = False
    enforcing_consecutive_5xx: int = 100
    enforcing_consecutive_gateway_failure: int = 0
    enforcing_success_rate: int = 100
    success_rate_minimum_hosts: int = 5
    success_rate_request_volume: int = 100
    success_rate_stdev_factor: int = 1900
    successful_active_health_check_uneject_host: bool = True

    @property
    def effective_max_ejection_time_ms(self) -> int:
        """`max_ejection_time`, or the larger of 300000 ms and `base_ejection_time`."""
        if self.max_ejection_time_ms is not None:
            return self.max_ejection_time_ms
        return max(300_000, self.base_ejection_time_ms)


@dataclass(slots=True)
class Host:
    """One upstream endpoint with the health flags and counters Envoy keeps per host."""

    name: str
    priority: int = 0
    failed_active_hc: bool = False
    failed_outlier: bool = False
    consecutive_5xx: int = 0
    consecutive_gateway: int = 0
    num_ejections: int = 0
    eject_backoff: int = 0
    last_ejection_ms: int | None = None
    last_unejection_ms: int | None = None
    jitter_ms: int = 0
    window_success: int = 0
    window_total: int = 0
    last_window: tuple[int, int] | None = None

    @property
    def healthy(self) -> bool:
        """A host is healthy when neither active health checking nor outlier detection flags it."""
        return not (self.failed_active_hc or self.failed_outlier)


@dataclass(slots=True)
class OutlierStats:
    """The subset of `cluster.<name>.outlier_detection.*` counters this model keeps."""

    detected: Counter[EjectionKind] = field(default_factory=lambda: Counter[EjectionKind]())
    enforced: Counter[EjectionKind] = field(default_factory=lambda: Counter[EjectionKind]())
    ejections_overflow: int = 0


def success_rate_ejection_threshold(
    success_rates: Sequence[float], stdev_factor_millis: int
) -> tuple[float, float]:
    """Return `(mean, mean - factor * stdev)` using the population standard deviation."""
    mean = sum(success_rates) / len(success_rates)
    variance = sum((rate - mean) ** 2 for rate in success_rates) / len(success_rates)
    return mean, mean - (stdev_factor_millis / 1000.0) * math.sqrt(variance)


class OutlierDetector:
    """Passive health checking for one cluster: consecutive-error and success-rate ejection."""

    def __init__(
        self,
        hosts: Sequence[Host],
        config: OutlierConfig,
        *,
        roll: Callable[[], int] = lambda: 0,
        jitter: Callable[[int], int] = lambda maximum: 0,
    ) -> None:
        if config.effective_max_ejection_time_ms < config.base_ejection_time_ms:
            raise ValueError("max_ejection_time cannot be smaller than base_ejection_time")
        self.hosts = list(hosts)
        self.config = config
        self.stats = OutlierStats()
        self._roll = roll
        self._jitter = jitter

    @property
    def ejections_active(self) -> int:
        """Number of hosts currently carrying the outlier-ejected flag."""
        return sum(1 for host in self.hosts if host.failed_outlier)

    def put_http_response(self, host: Host, code: int, now_ms: int) -> None:
        """Report one upstream HTTP response, as `Filter::onUpstreamHeaders` does per attempt."""
        host.window_total += 1
        if 500 <= code <= 599:
            if code in GATEWAY_CODES:
                host.consecutive_gateway += 1
                if host.consecutive_gateway == self.config.consecutive_gateway_failure:
                    self._on_consecutive(host, EjectionKind.CONSECUTIVE_GATEWAY_FAILURE, now_ms)
            else:
                host.consecutive_gateway = 0
            host.consecutive_5xx += 1
            if host.consecutive_5xx == self.config.consecutive_5xx:
                self._on_consecutive(host, EjectionKind.CONSECUTIVE_5XX, now_ms)
        else:
            host.window_success += 1
            host.consecutive_5xx = 0
            host.consecutive_gateway = 0

    def on_active_health_check_success(self, host: Host, now_ms: int) -> None:
        """Uneject on a passing active health check, unless the operator disabled that."""
        if (
            self.config.successful_active_health_check_uneject_host
            and not host.failed_active_hc
            and host.failed_outlier
        ):
            self._uneject(host, now_ms)

    def on_interval(self, now_ms: int) -> None:
        """Run one `DetectorImpl::onIntervalTimer` sweep."""
        for host in self.hosts:
            self._check_for_uneject(host, now_ms)
            host.last_window = (host.window_success, host.window_total)
            host.window_success = 0
            host.window_total = 0
        self._process_success_rate(now_ms)
        for host in self.hosts:
            if (
                not host.failed_outlier
                and host.last_unejection_ms is not None
                and now_ms - host.last_unejection_ms >= self.config.interval_ms
                and host.eject_backoff != 0
            ):
                host.eject_backoff -= 1

    def _enforced(self, kind: EjectionKind) -> bool:
        config = self.config
        percent = {
            EjectionKind.CONSECUTIVE_5XX: config.enforcing_consecutive_5xx,
            EjectionKind.CONSECUTIVE_GATEWAY_FAILURE: config.enforcing_consecutive_gateway_failure,
            EjectionKind.SUCCESS_RATE: config.enforcing_success_rate,
        }[kind]
        return self._roll() < percent

    def _on_consecutive(self, host: Host, kind: EjectionKind, now_ms: int) -> None:
        if host.failed_outlier:
            return
        self.stats.detected[kind] += 1
        self._eject(host, kind, now_ms)
        if kind is EjectionKind.CONSECUTIVE_5XX:
            host.consecutive_5xx = 0
        else:
            host.consecutive_gateway = 0

    def _eject(self, host: Host, kind: EjectionKind, now_ms: int) -> None:
        cap = min(100, self.config.max_ejection_percent)
        ejected_percent = 100.0 * (self.ejections_active + 1) / len(self.hosts)
        should_eject = ejected_percent <= cap
        if self.config.always_eject_one_host:
            should_eject = self.ejections_active == 0 or should_eject
        if not should_eject:
            self.stats.ejections_overflow += 1
            return
        if not self._enforced(kind):
            return
        self.stats.enforced[kind] += 1
        host.failed_outlier = True
        host.num_ejections += 1
        host.last_ejection_ms = now_ms
        host.jitter_ms = self._jitter(self.config.max_ejection_time_jitter_ms)
        base = self.config.base_ejection_time_ms
        if host.eject_backoff * base < self.config.effective_max_ejection_time_ms + base:
            host.eject_backoff += 1

    def _check_for_uneject(self, host: Host, now_ms: int) -> None:
        if not host.failed_outlier or host.last_ejection_ms is None:
            return
        duration = min(
            self.config.base_ejection_time_ms * host.eject_backoff,
            self.config.effective_max_ejection_time_ms,
        )
        if duration + host.jitter_ms <= now_ms - host.last_ejection_ms:
            self._uneject(host, now_ms)

    def _uneject(self, host: Host, now_ms: int) -> None:
        host.failed_outlier = False
        host.consecutive_5xx = 0
        host.consecutive_gateway = 0
        host.last_unejection_ms = now_ms

    def _process_success_rate(self, now_ms: int) -> None:
        if len(self.hosts) < self.config.success_rate_minimum_hosts:
            return
        valid: list[tuple[Host, float]] = []
        for host in self.hosts:
            if host.failed_outlier or not host.last_window:
                continue
            success, total = host.last_window
            if total >= self.config.success_rate_request_volume:
                valid.append((host, success * 100.0 / total))
        if not valid or len(valid) < self.config.success_rate_minimum_hosts:
            return
        _, threshold = success_rate_ejection_threshold(
            [rate for _, rate in valid], self.config.success_rate_stdev_factor
        )
        for host, rate in valid:
            if rate < threshold:
                self.stats.detected[EjectionKind.SUCCESS_RATE] += 1
                self._eject(host, EjectionKind.SUCCESS_RATE, now_ms)


@dataclass(frozen=True, slots=True)
class PriorityState:
    """Result of Envoy's per-priority health, load, and panic calculation."""

    health: tuple[int, ...]
    load: tuple[int, ...]
    panic: tuple[bool, ...]
    normalized_total_availability: int

    @property
    def total_panic(self) -> bool:
        """True when every priority level is in panic mode."""
        return bool(self.panic) and all(self.panic)


def _below_panic_threshold(healthy: int, count: int, threshold: int) -> bool:
    """`isHostSetInPanic`: healthy percent of the level is below the threshold (empty is 0%)."""
    return 100 * healthy < threshold * count if count else threshold > 0


def compute_priority_state(
    priorities: Sequence[Sequence[Host]],
    *,
    panic_threshold: int = DEFAULT_PANIC_THRESHOLD,
    overprovisioning_factor: int = DEFAULT_OVERPROVISIONING_FACTOR,
) -> PriorityState:
    """Mirror `recalculatePerPriorityState`, `recalculatePerPriorityPanic`, and total panic."""
    threshold = min(100, panic_threshold)
    counts = [len(level) for level in priorities]
    healthy = [sum(1 for host in level if host.healthy) for level in priorities]
    health = [
        min(100, overprovisioning_factor * healthy[i] // counts[i]) if counts[i] else 0
        for i in range(len(priorities))
    ]
    normalized = min(sum(health), 100)
    load = [0] * len(priorities)
    if normalized > 0:
        remaining = 100
        first_available = -1
        for i, value in enumerate(health):
            if first_available < 0 and value > 0:
                first_available = i
            load[i] = min(remaining, value * 100 // normalized)
            remaining -= load[i]
        if remaining:
            load[first_available] += remaining
    if threshold == 0 and normalized == 0:
        load[0] = 100
        return PriorityState(tuple(health), tuple(load), (False,) * len(load), normalized)
    panic = [
        normalized != 100 and _below_panic_threshold(healthy[i], counts[i], threshold)
        for i in range(len(priorities))
    ]
    if panic and all(panic):
        total_hosts = sum(counts)
        if total_hosts == 0:
            load = [100] + [0] * (len(load) - 1)
        else:
            load = [100 * count // total_hosts for count in counts]
            first_non_empty = next(i for i, count in enumerate(counts) if count)
            load[first_non_empty] += 100 - sum(load)
    return PriorityState(tuple(health), tuple(load), tuple(panic), normalized)


class PriorityLoadBalancer:
    """Round-robin host choice over priority levels with Envoy's panic semantics."""

    def __init__(
        self,
        priorities: Sequence[Sequence[Host]],
        *,
        panic_threshold: int = DEFAULT_PANIC_THRESHOLD,
        fail_traffic_on_panic: bool = False,
        overprovisioning_factor: int = DEFAULT_OVERPROVISIONING_FACTOR,
    ) -> None:
        self.priorities = [list(level) for level in priorities]
        self.panic_threshold = panic_threshold
        self.fail_traffic_on_panic = fail_traffic_on_panic
        self.overprovisioning_factor = overprovisioning_factor
        self.lb_healthy_panic = 0
        self._cursor: Counter[tuple[int, bool]] = Counter()

    def state(self) -> PriorityState:
        """Recompute the priority state from the current host health flags."""
        return compute_priority_state(
            self.priorities,
            panic_threshold=self.panic_threshold,
            overprovisioning_factor=self.overprovisioning_factor,
        )

    def choose_host(self, hash_value: int) -> Host | None:
        """Pick a host, or `None` where Envoy answers `503 no healthy upstream`."""
        state = self.state()
        point = hash_value % 100 + 1
        aggregate = 0
        priority = 0
        for index, share in enumerate(state.load):
            aggregate += share
            if point <= aggregate:
                priority = index
                break
        in_panic = state.panic[priority]
        if in_panic:
            self.lb_healthy_panic += 1
            if self.fail_traffic_on_panic:
                return None
            candidates = self.priorities[priority]
        else:
            candidates = [host for host in self.priorities[priority] if host.healthy]
        if not candidates:
            return None
        key = (priority, in_panic)
        chosen = candidates[self._cursor[key] % len(candidates)]
        self._cursor[key] += 1
        return chosen


def route_requests(balancer: PriorityLoadBalancer, count: int) -> Counter[str]:
    """Route `count` requests with priority hash `i` and tally picks per host name."""
    tally: Counter[str] = Counter()
    for i in range(count):
        host = balancer.choose_host(i)
        tally[host.name if host is not None else NO_HEALTHY_UPSTREAM] += 1
    return tally
