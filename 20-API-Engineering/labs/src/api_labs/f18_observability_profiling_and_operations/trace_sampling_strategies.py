"""Head-based vs tail-based trace sampling, modelled on the OpenTelemetry SDK and Collector.

Head side: stock `ParentBased(TraceIdRatioBased)` from `opentelemetry-sdk` 1.45.2.
Tail side: `TailBufferProcessor`, an in-process model of the Collector `tail_sampling` processor
(per-trace buffer, `num_traces` eviction, `decision_wait`, decision cache, policy flow).
The model is a teaching lab, not a re-implementation of the Go code.
"""

from __future__ import annotations

import hashlib
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.id_generator import IdGenerator
from opentelemetry.sdk.trace.sampling import (
    AlwaysRecordSampler,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace import SpanKind, StatusCode, Tracer, set_span_in_context

if TYPE_CHECKING:
    from opentelemetry.context import Context

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SLOW_NS = 500_000_000
STATUS_ATTR = "http.response.status_code"
LEGACY_STATUS_ATTR = "http.status_code"
ROUTE_ATTR = "http.route"
_LOW_64 = (1 << 64) - 1


def head_sampler(ratio: float) -> Sampler:
    """The SDK default shape for production: honour the parent, ratio-sample roots."""
    return ParentBased(TraceIdRatioBased(ratio))


def record_all_sampler(ratio: float) -> Sampler:
    """Head sampler that still records dropped spans so a processor can see them.

    `AlwaysRecordSampler` turns `DROP` into `RECORD_ONLY`: `is_recording()` is true, the
    `sampled` trace flag stays 0, and `SimpleSpanProcessor` and `BatchSpanProcessor` skip the span.
    """
    return AlwaysRecordSampler(head_sampler(ratio))


class SeededIdGenerator(IdGenerator):
    """Deterministic, uniformly random IDs so lab runs are reproducible."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)  # noqa: S311 - deterministic lab IDs, not security

    def generate_span_id(self) -> int:
        return self._rng.getrandbits(64) or 1

    def generate_trace_id(self) -> int:
        return self._rng.getrandbits(128) or 1

    def is_trace_id_random(self) -> bool:
        return True


@dataclass(frozen=True)
class TailPolicy:
    """Keep rules evaluated once per trace; `drop_routes` always wins, as the `drop` policy does."""

    error_status_gte: int = 500
    latency_threshold_ns: int = SLOW_NS
    baseline_ratio: float = 0.01
    drop_routes: frozenset[str] = frozenset({"/healthz", "/readyz"})

    def __post_init__(self) -> None:
        if not 0.0 <= self.baseline_ratio <= 1.0:
            raise ValueError("baseline_ratio must be in [0.0, 1.0]")


def _is_error(span: ReadableSpan, policy: TailPolicy) -> bool:
    if span.status.status_code is StatusCode.ERROR:
        return True
    attrs = span.attributes or {}
    status = attrs.get(STATUS_ATTR, attrs.get(LEGACY_STATUS_ATTR))
    return isinstance(status, int) and status >= policy.error_status_gte


def trace_id_of(span: ReadableSpan) -> int:
    if span.context is None:
        raise ValueError("span has no SpanContext")
    trace_id: int = span.context.trace_id
    return trace_id


def baseline_keep(trace_id: int, ratio: float) -> bool:
    """Same low-64-bit rule as the SDK `TraceIdRatioBased`, so head and tail agree per trace."""
    bound = TraceIdRatioBased.get_bound_for_rate(ratio)
    return (trace_id & _LOW_64) < bound


def evaluate_trace(spans: Sequence[ReadableSpan], policy: TailPolicy) -> tuple[bool, str]:
    """Return `(keep, reason)`: drop routes first, then error, latency, then the baseline coin."""
    for span in spans:
        if (span.attributes or {}).get(ROUTE_ATTR) in policy.drop_routes:
            return False, "drop"
    if any(_is_error(s, policy) for s in spans):
        return True, "error"
    starts = [s.start_time for s in spans if s.start_time is not None]
    ends = [s.end_time for s in spans if s.end_time is not None]
    if starts and ends and max(ends) - min(starts) > policy.latency_threshold_ns:
        return True, "latency"
    if spans and baseline_keep(trace_id_of(spans[0]), policy.baseline_ratio):
        return True, "baseline"
    return False, "dropped"


@dataclass
class TailStats:
    kept: dict[str, int] = field(default_factory=lambda: {})
    dropped: int = 0
    evicted_too_early: int = 0
    late_spans_inherited: int = 0


@dataclass
class _Pending:
    spans: list[ReadableSpan]
    first_seen_ns: int


class TailBufferProcessor(SpanProcessor):
    """Buffer spans per trace, decide when the local root ends or `decision_wait` expires."""

    def __init__(
        self,
        exporter: SpanExporter,
        policy: TailPolicy,
        *,
        num_traces: int = 50_000,
        decision_wait_ns: int = 30_000_000_000,
        decision_cache_size: int = 0,
        clock: Callable[[], int],
    ) -> None:
        if num_traces < 1:
            raise ValueError("num_traces must be >= 1")
        self._exporter = exporter
        self._policy = policy
        self._num_traces = num_traces
        self._wait_ns = decision_wait_ns
        self._cache_size = decision_cache_size
        self._clock = clock
        self._pending: OrderedDict[int, _Pending] = OrderedDict()
        self._decided: OrderedDict[int, bool] = OrderedDict()
        self.stats = TailStats()

    @property
    def pending_traces(self) -> int:
        return len(self._pending)

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        trace_id = trace_id_of(span)
        cached = self._decided.get(trace_id)
        if cached is not None:
            self._decided.move_to_end(trace_id)
            self.stats.late_spans_inherited += 1
            if cached:
                self._exporter.export((span,))
            return
        entry = self._pending.get(trace_id)
        if entry is None:
            if len(self._pending) >= self._num_traces:
                self._pending.popitem(last=False)
                self.stats.evicted_too_early += 1
            entry = _Pending([], self._clock())
            self._pending[trace_id] = entry
        entry.spans.append(span)
        if span.parent is None or span.parent.is_remote:
            self._decide(trace_id)

    def flush_expired(self) -> int:
        """Decide every trace older than `decision_wait`; return how many were decided."""
        now = self._clock()
        expired = [t for t, e in self._pending.items() if now - e.first_seen_ns >= self._wait_ns]
        for trace_id in expired:
            self._decide(trace_id)
        return len(expired)

    def _decide(self, trace_id: int) -> None:
        entry = self._pending.pop(trace_id)
        keep, reason = evaluate_trace(entry.spans, self._policy)
        if keep:
            self.stats.kept[reason] = self.stats.kept.get(reason, 0) + 1
            self._exporter.export(tuple(entry.spans))
        else:
            self.stats.dropped += 1
        if self._cache_size:
            self._decided[trace_id] = keep
            while len(self._decided) > self._cache_size:
                self._decided.popitem(last=False)

    def shutdown(self) -> None:
        self._pending.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@dataclass(frozen=True)
class Request:
    status: int
    duration_ms: int
    route: str = "/orders"


def generate_workload(n: int, seed: int, error_rate: float, slow_rate: float) -> list[Request]:
    """`n` requests: fraction `error_rate` return 500, `slow_rate` take 800 ms, the rest 20 ms."""
    rng = random.Random(seed)  # noqa: S311 - deterministic lab workload
    out: list[Request] = []
    for _ in range(n):
        roll = rng.random()
        if roll < error_rate:
            out.append(Request(500, 35))
        elif roll < error_rate + slow_rate:
            out.append(Request(200, 800))
        else:
            out.append(Request(200, 20))
    return out


def emit_request(tracer: Tracer, req: Request, start_ns: int) -> int:
    """Emit a server span with one `db` child; return the root span's trace id."""
    root = tracer.start_span("GET /orders", kind=SpanKind.SERVER, start_time=start_ns)
    # Set after start: a RECORD_ONLY span loses the attributes passed to start_span (see tests).
    root.set_attribute(ROUTE_ATTR, req.route)
    dur = req.duration_ms * 1_000_000
    ctx = set_span_in_context(root)
    child = tracer.start_span("db", context=ctx, kind=SpanKind.CLIENT, start_time=start_ns + 1_000)
    child.end(end_time=start_ns + dur - 1_000)
    root.set_attribute(STATUS_ATTR, req.status)
    if req.status >= 500:
        root.set_status(StatusCode.ERROR)
    root.end(end_time=start_ns + dur)
    return root.get_span_context().trace_id


@dataclass(frozen=True)
class RunResult:
    mode: str
    total: int
    total_errors: int
    total_slow: int
    kept_traces: int
    kept_errors: int
    kept_slow: int
    spans_exported: int


def run_pipeline(
    workload: Sequence[Request], *, mode: str, ratio: float = 0.01, seed: int = 7
) -> RunResult:
    """Run `workload` through `mode` in {"head", "tail"} and count what reached the exporter."""
    exporter = InMemorySpanExporter()
    now = [0]
    if mode == "head":
        provider = TracerProvider(sampler=head_sampler(ratio), id_generator=SeededIdGenerator(seed))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif mode == "tail":
        provider = TracerProvider(
            sampler=record_all_sampler(ratio), id_generator=SeededIdGenerator(seed)
        )
        provider.add_span_processor(
            TailBufferProcessor(exporter, TailPolicy(baseline_ratio=ratio), clock=lambda: now[0])
        )
    else:
        raise ValueError(f"unknown mode {mode!r}")
    tracer = provider.get_tracer("lab")
    errors: set[int] = set()
    slow: set[int] = set()
    for i, req in enumerate(workload):
        now[0] = i * 1_000_000
        trace_id = emit_request(tracer, req, start_ns=now[0])
        if req.status >= 500:
            errors.add(trace_id)
        elif req.duration_ms * 1_000_000 > SLOW_NS:
            slow.add(trace_id)
    spans = exporter.get_finished_spans()
    kept = {trace_id_of(s) for s in spans}
    return RunResult(
        mode=mode,
        total=len(workload),
        total_errors=len(errors),
        total_slow=len(slow),
        kept_traces=len(kept),
        kept_errors=len(kept & errors),
        kept_slow=len(kept & slow),
        spans_exported=len(spans),
    )


def estimate_total(kept_strata: dict[str, int], ratio: float) -> float:
    """Unbiased trace-count estimate: error and latency keeps count 1, baseline keeps 1/ratio."""
    always = kept_strata.get("error", 0) + kept_strata.get("latency", 0)
    return always + kept_strata.get("baseline", 0) / ratio


def route_modulo(trace_id: int, n_backends: int) -> int:
    return trace_id % n_backends


def route_rendezvous(trace_id: int, backends: Sequence[str]) -> str:
    """Highest-random-weight hashing: removing or adding one backend moves ~1/N of the traces."""
    return max(
        backends,
        key=lambda b: hashlib.blake2b(f"{b}:{trace_id}".encode(), digest_size=8).digest(),
    )


def reroute_fraction(
    trace_ids: Sequence[int], before: Callable[[int], str], after: Callable[[int], str]
) -> float:
    moved = sum(1 for t in trace_ids if before(t) != after(t))
    return moved / len(trace_ids)


class TracingMiddleware:
    """Raw ASGI middleware: one `SERVER` span per request, status from `http.response.start`."""

    def __init__(self, app: ASGIApp, tracer: Tracer, clock: Callable[[], int]) -> None:
        self.app = app
        self._tracer = tracer
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        span = self._tracer.start_span(
            f"{scope['method']} {scope['path']}", kind=SpanKind.SERVER, start_time=self._clock()
        )
        span.set_attribute(ROUTE_ATTR, scope["path"])
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if status == 0:
                status = 500
                await send({"type": "http.response.start", "status": 500, "headers": []})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            raise
        finally:
            span.set_attribute(STATUS_ATTR, status)
            if status >= 500:
                span.set_status(StatusCode.ERROR)
            span.end(end_time=self._clock())
