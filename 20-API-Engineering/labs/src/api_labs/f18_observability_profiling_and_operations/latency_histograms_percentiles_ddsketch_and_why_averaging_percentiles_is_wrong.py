"""Latency histograms, percentile estimation, and why averaging percentiles is wrong.

Three mergeable distribution summaries, each implemented from its specification:

* ``ClassicHistogram``: fixed ``le`` buckets, linear interpolation (Prometheus classic).
* ``ExponentialHistogram``: a standard-schema Prometheus native histogram (positive values only).
* ``DDSketch``: a relative-error quantile sketch (Masson, Rim, Lee, PVLDB 2019).

Plus the exact baselines and the broken aggregation (``average_of_quantiles``) the note refutes.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Awaitable, Callable, Iterable, MutableMapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

from prometheus_client import CollectorRegistry, Histogram
from prometheus_client.exposition import choose_encoder

INF = math.inf

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def exact_quantile(values: Sequence[float], q: float) -> float:
    """Return ``x_(floor(q * (n - 1)))`` of the sorted values, the DDSketch rank convention."""
    if not values:
        raise ValueError("no observations")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def lognormal_population(median: float, sigma: float, n: int) -> list[float]:
    """Deterministic lognormal sample: the inverse CDF at the n midpoints (i + 0.5) / n."""
    dist = NormalDist()
    return [median * math.exp(sigma * dist.inv_cdf((i + 0.5) / n)) for i in range(n)]


def average_of_quantiles(
    per_pod_quantiles: Sequence[float], weights: Sequence[float] | None = None
) -> float:
    """The invalid aggregation: a (weighted) mean of per-pod quantiles."""
    if not per_pod_quantiles:
        raise ValueError("no pods")
    ws = list(weights) if weights is not None else [1.0] * len(per_pod_quantiles)
    return sum(w * x for w, x in zip(ws, per_pod_quantiles, strict=True)) / sum(ws)


LATENCY_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
)  # fmt: skip
"""The prometheus-client 0.26.0 ``Histogram.DEFAULT_BUCKETS`` without ``+Inf``."""


def two_pod_fleet(slow_share: float, n: int = 20_000) -> tuple[list[float], list[float]]:
    """Latencies in seconds: a healthy pod and a degraded pod with ``slow_share`` of traffic."""
    n_slow = round(n * slow_share)
    return lognormal_population(0.05, 0.4, n - n_slow), lognormal_population(0.40, 0.8, n_slow)


@dataclass(frozen=True)
class FleetP99:
    """Every way of computing a fleet p99 for one traffic split."""

    slow_share: float
    true: float
    mean_of_pod_p99: float
    traffic_weighted_mean_of_pod_p99: float
    classic_merge: float
    native_merge: float
    ddsketch_merge: float


def fleet_p99(slow_share: float, n: int = 20_000) -> FleetP99:
    fast, slow = two_pod_fleet(slow_share, n)
    pods = [(fast, ClassicHistogram(LATENCY_BUCKETS), ExponentialHistogram(3), DDSketch())]
    pods.append((slow, ClassicHistogram(LATENCY_BUCKETS), ExponentialHistogram(3), DDSketch()))
    for values, classic, native, sketch in pods:
        for v in values:
            classic.observe(v)
            native.observe(v)
            sketch.add(v)
    pod_p99 = [exact_quantile(values, 0.99) for values, *_ in pods]
    return FleetP99(
        slow_share=slow_share,
        true=exact_quantile(fast + slow, 0.99),
        mean_of_pod_p99=average_of_quantiles(pod_p99),
        traffic_weighted_mean_of_pod_p99=average_of_quantiles(pod_p99, [len(fast), len(slow)]),
        classic_merge=pods[0][1].merge(pods[1][1]).quantile(0.99),
        native_merge=pods[0][2].merge(pods[1][2]).quantile(0.99),
        ddsketch_merge=pods[0][3].merge(pods[1][3]).quantile(0.99),
    )


class ClassicHistogram:
    """Prometheus classic histogram: per-bucket counts over fixed inclusive upper bounds."""

    def __init__(self, bounds: Sequence[float]) -> None:
        ordered = [float(b) for b in bounds]
        if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
            raise ValueError("bounds must be strictly increasing")
        if not ordered or ordered[-1] != INF:
            ordered.append(INF)
        if len(ordered) < 2:
            raise ValueError("need at least two buckets")
        self.bounds = ordered
        self.counts = [0] * len(ordered)

    def observe(self, value: float) -> None:
        """Increment the first bucket whose upper bound is >= value (``le`` is inclusive)."""
        self.counts[bisect_left(self.bounds, value)] += 1

    @property
    def total(self) -> int:
        return sum(self.counts)

    def cumulative(self) -> list[int]:
        out: list[int] = []
        running = 0
        for c in self.counts:
            running += c
            out.append(running)
        return out

    def merge(self, other: ClassicHistogram) -> ClassicHistogram:
        """Sum bucket counts; only valid for identical bounds, which is the classic contract."""
        if self.bounds != other.bounds:
            raise ValueError("classic histograms with different bounds are not mergeable")
        merged = ClassicHistogram(self.bounds)
        merged.counts = [a + b for a, b in zip(self.counts, other.counts, strict=True)]
        return merged

    def fraction_le(self, bound: float) -> float | None:
        """``bucket{le=bound} / count``; ``None`` when no bucket has that exact bound."""
        if bound not in self.bounds or self.total == 0:
            return None
        return self.cumulative()[self.bounds.index(bound)] / self.total

    def quantile(self, q: float) -> float:
        """Linear interpolation inside the target bucket, with the documented special cases."""
        if math.isnan(q) or self.total == 0:
            return math.nan
        if q < 0:
            return -INF
        if q > 1:
            return INF
        cum = self.cumulative()
        rank = q * self.total
        b = next(i for i, c in enumerate(cum) if c >= rank and c > 0)
        if b == len(self.bounds) - 1:
            return self.bounds[-2]
        if b == 0 and self.bounds[0] <= 0:
            return self.bounds[0]
        lower = 0.0 if b == 0 else self.bounds[b - 1]
        below = 0 if b == 0 else cum[b - 1]
        upper = self.bounds[b]
        return lower + (upper - lower) * ((rank - below) / self.counts[b])


def sum_by_le(histograms: Sequence[ClassicHistogram]) -> list[tuple[float, int]]:
    """What ``sum by (le) (...)`` does: add the cumulative series that share an ``le`` label.

    A pod that lacks a bound simply contributes no series to it, so layouts that differ
    produce a non-monotonic result instead of an error.
    """
    totals: dict[float, int] = {}
    for h in histograms:
        for bound, cum in zip(h.bounds, h.cumulative(), strict=True):
            totals[bound] = totals.get(bound, 0) + cum
    return sorted(totals.items())


class ExponentialHistogram:
    """Standard-schema native histogram for non-negative values.

    Bucket ``i`` covers ``(base**(i-1), base**i]`` with ``base = 2**(2**-schema)``.
    Values ``<= zero_threshold`` go to the zero bucket.
    """

    MIN_SCHEMA = -4
    MAX_SCHEMA = 8

    def __init__(self, schema: int = 3, zero_threshold: float = 2.0**-128) -> None:
        if not self.MIN_SCHEMA <= schema <= self.MAX_SCHEMA:
            raise ValueError("schema must be in [-4, 8]")
        if zero_threshold < 0:
            raise ValueError("zero_threshold must be >= 0")
        self.schema = schema
        self.zero_threshold = zero_threshold
        self.zero_count = 0
        self.buckets: dict[int, int] = {}
        self.sum = 0.0

    @property
    def growth_factor(self) -> float:
        return float(2.0 ** (2.0**-self.schema))

    @property
    def count(self) -> int:
        return self.zero_count + sum(self.buckets.values())

    def index(self, value: float) -> int:
        """Smallest ``i`` with ``value <= base**i``; exact at powers of two via ``frexp``."""
        frac, exp = math.frexp(value)
        if self.schema > 0:
            if frac == 0.5:
                return (exp - 1) << self.schema
            return math.ceil(math.log2(value) * (1 << self.schema))
        k = exp - 1 if frac == 0.5 else exp
        return -(-k // (1 << -self.schema))

    def bounds(self, index: int) -> tuple[float, float]:
        """Lower (exclusive) and upper (inclusive) bound of bucket ``index``."""
        step = 2.0**-self.schema
        return 2.0 ** ((index - 1) * step), 2.0 ** (index * step)

    def observe(self, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("lab histogram accepts finite non-negative values only")
        self.sum += value
        if value <= self.zero_threshold:
            self.zero_count += 1
            return
        i = self.index(value)
        self.buckets[i] = self.buckets.get(i, 0) + 1

    def reduced(self, target: int) -> ExponentialHistogram:
        """Merge neighbours down to a lower schema: ``i -> ceil(i / 2**(schema - target))``."""
        if target > self.schema:
            raise ValueError("cannot increase resolution")
        out = ExponentialHistogram(target, self.zero_threshold)
        out.zero_count = self.zero_count
        out.sum = self.sum
        divisor = 1 << (self.schema - target)
        for i, c in self.buckets.items():
            j = -(-i // divisor)
            out.buckets[j] = out.buckets.get(j, 0) + c
        return out

    def limit_buckets(self, max_buckets: int) -> ExponentialHistogram:
        """Reduce the schema until the populated bucket count fits, stopping at schema -4."""
        out = self
        while len(out.buckets) > max_buckets and out.schema > self.MIN_SCHEMA:
            out = out.reduced(out.schema - 1)
        return out

    def merge(self, other: ExponentialHistogram) -> ExponentialHistogram:
        """Merge at the lower of the two schemas (equal zero thresholds only in this lab)."""
        if self.zero_threshold != other.zero_threshold:
            raise ValueError("lab merge requires equal zero thresholds")
        target = min(self.schema, other.schema)
        a, b = self.reduced(target), other.reduced(target)
        for i, c in b.buckets.items():
            a.buckets[i] = a.buckets.get(i, 0) + c
        a.zero_count += b.zero_count
        a.sum += b.sum
        return a

    def quantile(self, q: float) -> float:
        """Linear inside the zero bucket, geometric (exponential) interpolation elsewhere."""
        n = self.count
        if n == 0 or math.isnan(q):
            return math.nan
        if q < 0:
            return -INF
        if q > 1:
            return INF
        rank = q * n
        if rank <= self.zero_count:
            return self.zero_threshold * (rank / self.zero_count) if self.zero_count else 0.0
        running = self.zero_count
        for i in sorted(self.buckets):
            c = self.buckets[i]
            if running + c >= rank:
                lower, upper = self.bounds(i)
                frac = (rank - running) / c
                return lower * float((upper / lower) ** frac)
            running += c
        return self.bounds(max(self.buckets))[1]


class DDSketch:
    """Relative-error quantile sketch with the paper's collapse-lowest bucket limit."""

    def __init__(self, relative_accuracy: float = 0.01, max_bins: int = 2048) -> None:
        if not 0.0 < relative_accuracy < 1.0:
            raise ValueError("relative_accuracy must be in (0, 1)")
        if max_bins < 1:
            raise ValueError("max_bins must be >= 1")
        self.relative_accuracy = relative_accuracy
        self.gamma = (1.0 + relative_accuracy) / (1.0 - relative_accuracy)
        self._log_gamma = math.log(self.gamma)
        self.max_bins = max_bins
        self.bins: dict[int, int] = {}
        self.zero_count = 0
        self.count = 0
        self.sum = 0.0

    def key(self, value: float) -> int:
        """Bucket index ``ceil(log_gamma(value))``: bucket ``i`` is ``(gamma**(i-1), gamma**i]``."""
        return math.ceil(math.log(value) / self._log_gamma)

    def value(self, key: int) -> float:
        """Representative value ``2 * gamma**i / (gamma + 1)``, alpha-accurate bucket-wide."""
        return 2.0 * self.gamma**key / (self.gamma + 1.0)

    def add(self, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("lab sketch accepts finite non-negative values only")
        if value == 0.0:
            self.zero_count += 1
        else:
            k = self.key(value)
            self.bins[k] = self.bins.get(k, 0) + 1
            self._collapse()
        self.count += 1
        self.sum += value

    def _collapse(self) -> None:
        while len(self.bins) > self.max_bins:
            ordered = sorted(self.bins)
            lowest, second = ordered[0], ordered[1]
            self.bins[second] += self.bins.pop(lowest)

    def merge(self, other: DDSketch) -> DDSketch:
        if self.gamma != other.gamma:
            raise ValueError("sketches with different gamma are not mergeable")
        out = DDSketch(self.relative_accuracy, min(self.max_bins, other.max_bins))
        for source in (self, other):
            for k, c in source.bins.items():
                out.bins[k] = out.bins.get(k, 0) + c
            out.zero_count += source.zero_count
            out.count += source.count
            out.sum += source.sum
        out._collapse()
        return out

    def quantile(self, q: float) -> float:
        if self.count == 0:
            raise ValueError("empty sketch")
        if not 0.0 <= q <= 1.0:
            raise ValueError("q must be in [0, 1]")
        rank = q * (self.count - 1)
        if rank < self.zero_count:
            return 0.0
        running = float(self.zero_count)
        for k in sorted(self.bins):
            running += self.bins[k]
            if running > rank:
                return self.value(k)
        return self.value(max(self.bins))


def instrumented_app(
    registry: CollectorRegistry, clock: Callable[[], float], buckets: Iterable[float]
) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
    """Pure-ASGI app: ``/metrics`` scrape endpoint plus a timed endpoint on a classic Histogram."""
    latency = Histogram(
        "http_request_duration_seconds",
        "Request latency",
        ["route"],
        buckets=tuple(buckets),
        registry=registry,
    )

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        await receive()
        if scope["path"] == "/metrics":
            accept = next(
                (v.decode() for k, v in scope["headers"] if k == b"accept"),
                "",
            )
            encoder, content_type = choose_encoder(accept)
            body = encoder(registry)
            status, ctype = 200, content_type.encode()
        else:
            start = clock()
            body = b"ok"
            status, ctype = 200, b"text/plain"
            latency.labels(route="/work").observe(clock() - start)
        headers = [(b"content-type", ctype)]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return app


def main() -> None:
    """Print the comparison table the note shows (``python -m`` entry point)."""
    print("slow_share  true  mean_p99  weighted_p99  classic  native  ddsketch   (seconds)")
    for share in (0.005, 0.01, 0.05, 0.10, 0.50):
        r = fleet_p99(share)
        print(
            f"{r.slow_share:>9.3f} {r.true:>6.3f} {r.mean_of_pod_p99:>9.3f} "
            f"{r.traffic_weighted_mean_of_pod_p99:>13.3f} {r.classic_merge:>8.3f} "
            f"{r.native_merge:>7.3f} {r.ddsketch_merge:>9.3f}"
        )


if __name__ == "__main__":
    main()
