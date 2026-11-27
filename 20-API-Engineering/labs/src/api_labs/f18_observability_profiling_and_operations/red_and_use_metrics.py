"""RED metrics on OpenTelemetry HTTP semantic conventions, and a USE-instrumented resource.

``RedMiddleware`` is a pure ASGI middleware that records one histogram,
``http.server.request.duration`` (unit ``s``), per request.
Rate is the histogram count, errors are the points that carry ``error.type``, and duration is
the bucket distribution, so one instrument answers all three RED questions.

``http.route`` is read from ``scope["route"]`` after the inner app has run, because Starlette's
router only stores the matched route there during dispatch.
A request that matched no route gets no ``http.route`` at all: the path is never a substitute.

``UseResource`` models a bounded worker pool with a clock-integrated utilization, a queue for
saturation, and a rejection counter for errors, so a short burst stays visible even when the
average does not move.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics.export import Histogram, HistogramDataPoint, MetricsData

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DURATION_METRIC: Final = "http.server.request.duration"
ACTIVE_METRIC: Final = "http.server.active_requests"
OTHER_METHOD: Final = "_OTHER"

# Known methods in semantic-conventions v1.45.0: RFC 9110, PATCH (RFC 5789), and QUERY.
KNOWN_METHODS: Final = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "QUERY", "TRACE"}
)

# The `ExplicitBucketBoundaries` advisory the HTTP metrics convention recommends, in seconds.
DURATION_BUCKETS_S: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)

type RouteLabeler = Callable[[Scope], str | None]


def normalize_method(raw: str) -> str:
    """Return the method if it is a known one, else ``_OTHER``; the match is case-sensitive."""
    return raw if raw in KNOWN_METHODS else OTHER_METHOD


def route_template(scope: Mapping[str, Any]) -> str | None:
    """Return ``root_path + route.path_format`` once routing has run, else ``None``."""
    template = getattr(scope.get("route"), "path_format", None)
    if not isinstance(template, str) or not template:
        return None
    return f"{scope.get('root_path', '')}{template}"


def raw_path_label_antipattern(scope: Mapping[str, Any]) -> str | None:
    """Label by the concrete URL path: one time series per distinct path. Never ship this."""
    path = scope.get("path")
    return path if isinstance(path, str) else None


def exception_type_name(exc: BaseException) -> str:
    """Fully qualified class name, with the module omitted for builtins."""
    module = type(exc).__module__
    qualname = type(exc).__qualname__
    return qualname if module == "builtins" else f"{module}.{qualname}"


class RedMiddleware:
    """Record ``http.server.request.duration`` and ``http.server.active_requests``."""

    def __init__(
        self,
        app: ASGIApp,
        meter: Meter,
        *,
        clock: Callable[[], float] = time.perf_counter,
        route_labeler: RouteLabeler = route_template,
    ) -> None:
        self.app = app
        self._clock = clock
        self._route_labeler = route_labeler
        self._duration = meter.create_histogram(
            DURATION_METRIC,
            unit="s",
            description="Duration of HTTP server requests.",
            explicit_bucket_boundaries_advisory=DURATION_BUCKETS_S,
        )
        self._active = meter.create_up_down_counter(
            ACTIVE_METRIC, unit="{request}", description="Number of active HTTP server requests."
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        active_attrs = {
            "http.request.method": normalize_method(scope["method"]),
            "url.scheme": scope.get("scheme", "http"),
        }
        status: int | None = None
        error_type: str | None = None

        async def recording_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        self._active.add(1, active_attrs)
        start = self._clock()
        try:
            await self.app(scope, receive, recording_send)
        except Exception as exc:
            error_type = exception_type_name(exc)
            raise
        finally:
            elapsed = max(self._clock() - start, 0.0)
            attrs: dict[str, str | int] = dict(active_attrs)
            route = self._route_labeler(scope)
            if route is not None:
                attrs["http.route"] = route
            if status is not None:
                attrs["http.response.status_code"] = status
                if status >= 500 and error_type is None:
                    error_type = str(status)
            if error_type is not None:
                attrs["error.type"] = error_type
            self._duration.record(elapsed, attrs)
            self._active.add(-1, active_attrs)


def duration_points(data: MetricsData | None) -> list[HistogramDataPoint]:
    """Every data point of ``http.server.request.duration`` in an export."""
    points: list[HistogramDataPoint] = []
    for resource_metrics in data.resource_metrics if data else ():
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == DURATION_METRIC and isinstance(metric.data, Histogram):
                    points.extend(metric.data.data_points)
    return points


def histogram_quantile(q: float, bounds: tuple[float, ...], counts: list[int]) -> float | None:
    """Estimate a quantile from bucket counts by linear interpolation inside the bucket.

    ``counts`` has ``len(bounds) + 1`` entries; the last is the ``+Inf`` bucket.
    A rank that lands in ``+Inf`` returns the highest finite bound, so the estimate
    is capped there however slow the requests really were.
    """
    total = sum(counts)
    if total == 0:
        return None
    rank = q * total
    cumulative = 0
    for index, count in enumerate(counts):
        previous = cumulative
        cumulative += count
        if count and cumulative >= rank:
            if index == len(bounds):
                return bounds[-1]
            lower = bounds[index - 1] if index else 0.0
            return lower + (bounds[index] - lower) * ((rank - previous) / count)
    return bounds[-1]


@dataclass(frozen=True)
class RedSummary:
    """Rate, Errors, Duration for one route (or all routes) over one window."""

    requests: int
    errors: int
    rate_per_s: float
    error_ratio: float
    p_ok_s: float | None
    p_error_s: float | None


def summarize_red(
    data: MetricsData | None,
    *,
    window_s: float,
    route: str | None = None,
    quantile: float = 0.99,
) -> RedSummary:
    """Derive RED from cumulative histogram data that started at the window start.

    Latency is reported separately for requests without and with ``error.type``,
    because fast failures flatter the combined distribution.
    """
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    requests = errors = 0
    merged: dict[bool, list[int]] = {}
    bounds: tuple[float, ...] = DURATION_BUCKETS_S
    for point in duration_points(data):
        attributes = point.attributes or {}
        if route is not None and attributes.get("http.route") != route:
            continue
        failed = "error.type" in attributes
        requests += point.count
        errors += point.count if failed else 0
        bounds = tuple(point.explicit_bounds)
        counts = merged.setdefault(failed, [0] * len(point.bucket_counts))
        for index, value in enumerate(point.bucket_counts):
            counts[index] += value
    return RedSummary(
        requests=requests,
        errors=errors,
        rate_per_s=requests / window_s,
        error_ratio=errors / requests if requests else math.nan,
        p_ok_s=histogram_quantile(quantile, bounds, merged[False]) if False in merged else None,
        p_error_s=histogram_quantile(quantile, bounds, merged[True]) if True in merged else None,
    )


Admission = Literal["running", "queued", "rejected"]


@dataclass(frozen=True)
class UseSnapshot:
    """USE readings for one resource since it was created."""

    utilization: float
    average_queue: float
    peak_queue: int
    queued_now: int
    in_use_now: int
    rejected: int


class UseResource:
    """A bounded pool (threads, connections, file descriptors) instrumented for USE.

    Utilization is the fraction of capacity-seconds spent busy, saturation is the wait queue,
    and errors are admissions refused because the queue was full.
    """

    def __init__(
        self,
        capacity: int,
        max_queue: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or max_queue < 0:
            raise ValueError("capacity must be >= 1 and max_queue >= 0")
        self._capacity = capacity
        self._max_queue = max_queue
        self._clock = clock
        self._started = self._last = clock()
        self._busy_integral = 0.0
        self._queue_integral = 0.0
        self._in_use = 0
        self._queued = 0
        self._peak_queue = 0
        self._rejected = 0

    def _advance(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        self._busy_integral += self._in_use * elapsed
        self._queue_integral += self._queued * elapsed
        self._last = now

    def acquire(self) -> Admission:
        """Take a slot, join the queue, or be refused."""
        self._advance()
        if self._in_use < self._capacity:
            self._in_use += 1
            return "running"
        if self._queued < self._max_queue:
            self._queued += 1
            self._peak_queue = max(self._peak_queue, self._queued)
            return "queued"
        self._rejected += 1
        return "rejected"

    def release(self) -> None:
        """Free a slot; the oldest queued request, if any, takes it immediately."""
        self._advance()
        if self._in_use == 0:
            raise RuntimeError("release without a matching acquire")
        if self._queued:
            self._queued -= 1
        else:
            self._in_use -= 1

    def snapshot(self) -> UseSnapshot:
        """Integrate up to now and report utilization, saturation, and errors."""
        self._advance()
        elapsed = self._last - self._started
        return UseSnapshot(
            utilization=self._busy_integral / (self._capacity * elapsed) if elapsed else 0.0,
            average_queue=self._queue_integral / elapsed if elapsed else 0.0,
            peak_queue=self._peak_queue,
            queued_now=self._queued,
            in_use_now=self._in_use,
            rejected=self._rejected,
        )


def register_use_gauges(meter: Meter, name: str, resource: UseResource) -> None:
    """Export a pool's USE readings as observable instruments in a custom ``app.*`` namespace."""
    attrs = {"app.pool.name": name}

    def utilization(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(resource.snapshot().utilization, attrs)]

    def queued(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(resource.snapshot().queued_now, attrs)]

    def rejected(_: CallbackOptions) -> Iterable[Observation]:
        return [Observation(resource.snapshot().rejected, attrs)]

    meter.create_observable_gauge(
        "app.pool.utilization", callbacks=[utilization], unit="1", description="Busy fraction."
    )
    meter.create_observable_gauge(
        "app.pool.queued", callbacks=[queued], unit="{request}", description="Waiting requests."
    )
    meter.create_observable_counter(
        "app.pool.rejected", callbacks=[rejected], unit="{request}", description="Refused."
    )
