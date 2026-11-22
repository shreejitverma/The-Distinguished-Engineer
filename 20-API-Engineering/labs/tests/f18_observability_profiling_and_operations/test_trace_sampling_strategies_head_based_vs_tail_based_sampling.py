"""Head-based vs tail-based sampling: SDK ParentBased behaviour and the tail-buffer model."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    TraceIdRatioBased,
)
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    TraceFlags,
    Tracer,
    set_span_in_context,
)
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from starlette.requests import Request as StarletteRequest

from api_labs.f18_observability_profiling_and_operations.trace_sampling_strategies import (
    Request,
    SeededIdGenerator,
    TailBufferProcessor,
    TailPolicy,
    TracingMiddleware,
    emit_request,
    estimate_total,
    evaluate_trace,
    generate_workload,
    head_sampler,
    record_all_sampler,
    reroute_fraction,
    route_modulo,
    route_rendezvous,
    run_pipeline,
    trace_id_of,
)

MS = 1_000_000
_NO_BASELINE = TailPolicy(baseline_ratio=0.0)


class _Rig:
    """A tail-sampling provider with a manual clock."""

    def __init__(
        self,
        *,
        ratio: float = 0.0,
        num_traces: int = 100,
        cache: int = 0,
        wait_ns: int = 30_000 * MS,
        seed: int = 1,
    ) -> None:
        self.now = 0
        self.exporter = InMemorySpanExporter()
        self.tail = TailBufferProcessor(
            self.exporter,
            TailPolicy(baseline_ratio=ratio),
            num_traces=num_traces,
            decision_wait_ns=wait_ns,
            decision_cache_size=cache,
            clock=lambda: self.now,
        )
        self.provider = TracerProvider(
            sampler=record_all_sampler(ratio), id_generator=SeededIdGenerator(seed)
        )
        self.provider.add_span_processor(self.tail)
        self.tracer: Tracer = self.provider.get_tracer("rig")

    def exported_trace_ids(self) -> set[int]:
        return {trace_id_of(s) for s in self.exporter.get_finished_spans()}


def _remote_ctx(*, sampled: bool) -> Context:
    flags = TraceFlags(TraceFlags.SAMPLED if sampled else TraceFlags.DEFAULT)
    sc = SpanContext(0xABCDEF << 64 | 1, 7, is_remote=True, trace_flags=flags)
    return set_span_in_context(NonRecordingSpan(sc))


@pytest.mark.unit
def test_parentbased_follows_remote_sampled_flag_over_the_ratio() -> None:
    sampler = ParentBased(TraceIdRatioBased(0.0))
    kept = sampler.should_sample(_remote_ctx(sampled=True), 1 << 100, "x")
    assert kept.decision is Decision.RECORD_AND_SAMPLE
    sampler_all = ParentBased(TraceIdRatioBased(1.0))
    dropped = sampler_all.should_sample(_remote_ctx(sampled=False), 1, "x")
    assert dropped.decision is Decision.DROP


@pytest.mark.unit
def test_bare_ratio_sampler_ignores_the_parent_flag() -> None:
    bare = TraceIdRatioBased(0.0)
    result = bare.should_sample(_remote_ctx(sampled=True), 1, "x")
    assert result.decision is Decision.DROP


@pytest.mark.unit
def test_ratio_sampling_is_monotonic_across_ratios() -> None:
    rng = random.Random(3)  # noqa: S311
    ids = [rng.getrandbits(128) | 1 for _ in range(20_000)]
    low = TraceIdRatioBased(0.01)
    high = TraceIdRatioBased(0.10)
    kept_low = {
        i for i in ids if low.should_sample(None, i, "x").decision is Decision.RECORD_AND_SAMPLE
    }
    kept_high = {
        i for i in ids if high.should_sample(None, i, "x").decision is Decision.RECORD_AND_SAMPLE
    }
    assert kept_low < kept_high
    assert 100 <= len(kept_low) <= 300


class _Collect(SpanProcessor):
    def __init__(self) -> None:
        self.seen: list[ReadableSpan] = []

    def on_end(self, span: ReadableSpan) -> None:
        self.seen.append(span)


@pytest.mark.unit
def test_record_all_sampler_reaches_processors_but_not_the_exporter() -> None:
    exporter = InMemorySpanExporter()
    collect = _Collect()
    provider = TracerProvider(sampler=record_all_sampler(0.0))
    provider.add_span_processor(collect)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    span = provider.get_tracer("t").start_span("s")
    assert span.is_recording()
    assert not span.get_span_context().trace_flags.sampled
    span.end()
    assert len(collect.seen) == 1
    assert exporter.get_finished_spans() == ()


@pytest.mark.unit
def test_plain_head_sampler_never_records_dropped_spans() -> None:
    collect = _Collect()
    provider = TracerProvider(sampler=head_sampler(0.0))
    provider.add_span_processor(collect)
    span = provider.get_tracer("t").start_span("s")
    assert not span.is_recording()
    span.end()
    assert collect.seen == []


@pytest.mark.unit
def test_head_sampling_loses_errors_while_tail_keeps_every_error_and_slow_trace() -> None:
    work = generate_workload(20_000, seed=11, error_rate=0.005, slow_rate=0.01)
    head = run_pipeline(work, mode="head", ratio=0.01)
    tail = run_pipeline(work, mode="tail", ratio=0.01)
    assert head.total_errors > 50
    assert head.kept_errors / head.total_errors < 0.05
    assert tail.kept_errors == tail.total_errors
    assert tail.kept_slow == tail.total_slow
    healthy = tail.total - tail.total_errors - tail.total_slow
    assert tail.kept_traces < tail.total_errors + tail.total_slow + 0.02 * healthy
    assert tail.kept_traces > tail.total_errors + tail.total_slow
    assert tail.spans_exported == 2 * tail.kept_traces


def _span_trace(rig: _Rig, req: Request) -> int:
    rig.now += 1 * MS
    return emit_request(rig.tracer, req, start_ns=rig.now)


@pytest.mark.unit
def test_drop_route_overrides_error_retention() -> None:
    rig = _Rig()
    _span_trace(rig, Request(500, 30, route="/healthz"))
    kept = _span_trace(rig, Request(500, 30, route="/orders"))
    assert rig.exported_trace_ids() == {kept}
    assert rig.tail.stats.dropped == 1


@pytest.mark.unit
def test_latency_uses_earliest_start_to_latest_end_not_span_sum() -> None:
    collect = _Collect()
    provider = TracerProvider(sampler=record_all_sampler(0.0))
    provider.add_span_processor(collect)
    tracer = provider.get_tracer("t")
    root = tracer.start_span("r", kind=SpanKind.SERVER, start_time=0)
    for start in (1 * MS, 700 * MS):
        child = tracer.start_span("c", context=set_span_in_context(root), start_time=start)
        child.end(end_time=start + 50 * MS)
    root.end(end_time=800 * MS)
    busy = sum((s.end_time or 0) - (s.start_time or 0) for s in collect.seen[:2])
    assert busy == 100 * MS
    assert busy < _NO_BASELINE.latency_threshold_ns
    assert evaluate_trace(collect.seen[:2], _NO_BASELINE) == (True, "latency")
    adjacent = TracerProvider(sampler=record_all_sampler(0.0))
    adjacent_collect = _Collect()
    adjacent.add_span_processor(adjacent_collect)
    tracer2 = adjacent.get_tracer("t")
    for start in (0, 50 * MS):
        tracer2.start_span("c", start_time=start).end(end_time=start + 50 * MS)
    assert evaluate_trace(adjacent_collect.seen, _NO_BASELINE) == (False, "dropped")


@pytest.mark.unit
def test_record_only_spans_lose_creation_time_attributes_but_keep_later_ones() -> None:
    dropped = TracerProvider(sampler=record_all_sampler(0.0)).get_tracer("t")
    sampled = TracerProvider(sampler=record_all_sampler(1.0)).get_tracer("t")
    for tracer, expected in ((dropped, {"late": 2}), (sampled, {"early": 1, "late": 2})):
        span = tracer.start_span("s", attributes={"early": 1})
        span.set_attribute("late", 2)
        assert isinstance(span, SdkSpan)
        assert dict(span.attributes or {}) == expected


@pytest.mark.unit
def test_buffer_overflow_evicts_oldest_trace_and_a_late_root_is_judged_alone() -> None:
    rig = _Rig(num_traces=2)
    roots: list[Span] = []
    for i in range(4):
        root = rig.tracer.start_span(f"r{i}", kind=SpanKind.SERVER, start_time=i * MS)
        child = rig.tracer.start_span("c", context=set_span_in_context(root), start_time=i * MS)
        child.end(end_time=i * MS + 1)
        roots.append(root)
    assert rig.tail.pending_traces == 2
    assert rig.tail.stats.evicted_too_early == 2
    roots[0].set_attribute("http.response.status_code", 500)
    roots[0].end(end_time=10 * MS)
    assert [s.name for s in rig.exporter.get_finished_spans()] == ["r0"]


@pytest.mark.unit
def test_late_span_inherits_cached_keep_decision() -> None:
    rig = _Rig(cache=10)
    root = rig.tracer.start_span("r", kind=SpanKind.SERVER, start_time=0)
    late = rig.tracer.start_span("late", context=set_span_in_context(root), start_time=1)
    root.set_attribute("http.response.status_code", 503)
    root.end(end_time=5 * MS)
    late.end(end_time=6 * MS)
    assert {s.name for s in rig.exporter.get_finished_spans()} == {"r", "late"}
    assert rig.tail.stats.late_spans_inherited == 1


@pytest.mark.unit
def test_late_span_without_cache_is_buffered_and_lost_to_the_baseline() -> None:
    rig = _Rig(cache=0)
    root = rig.tracer.start_span("r", kind=SpanKind.SERVER, start_time=0)
    late = rig.tracer.start_span("late", context=set_span_in_context(root), start_time=1)
    root.set_attribute("http.response.status_code", 503)
    root.end(end_time=5 * MS)
    late.end(end_time=6 * MS)
    assert {s.name for s in rig.exporter.get_finished_spans()} == {"r"}
    assert rig.tail.pending_traces == 1
    rig.now += 31_000 * MS
    assert rig.tail.flush_expired() == 1
    assert {s.name for s in rig.exporter.get_finished_spans()} == {"r"}


@pytest.mark.unit
def test_decision_wait_decides_orphan_traces_only_after_expiry() -> None:
    rig = _Rig(wait_ns=10_000 * MS)
    orphan = rig.tracer.start_span("r", kind=SpanKind.SERVER, start_time=0)
    child = rig.tracer.start_span("c", context=set_span_in_context(orphan), start_time=0)
    child.set_attribute("http.response.status_code", 500)
    child.end(end_time=1 * MS)
    rig.now = 9_999 * MS
    assert rig.tail.flush_expired() == 0
    rig.now = 10_000 * MS
    assert rig.tail.flush_expired() == 1
    assert [s.name for s in rig.exporter.get_finished_spans()] == ["c"]


@pytest.mark.unit
def test_unweighted_scale_up_overestimates_while_stratified_estimate_is_close() -> None:
    work = generate_workload(30_000, seed=5, error_rate=0.01, slow_rate=0.02)
    rig = _Rig(ratio=0.01, num_traces=1000, seed=9)
    for i, req in enumerate(work):
        rig.now = i * MS
        emit_request(rig.tracer, req, start_ns=rig.now)
    kept = rig.tail.stats.kept
    naive = sum(kept.values()) / 0.01
    stratified = estimate_total(kept, 0.01)
    assert naive > 1.5 * len(work)
    assert abs(stratified - len(work)) / len(work) < 0.15


@pytest.mark.unit
def test_rendezvous_hashing_moves_far_fewer_traces_than_modulo_on_scale_out() -> None:
    rng = random.Random(1)  # noqa: S311
    ids = [rng.getrandbits(128) for _ in range(20_000)]
    four = [f"c{i}" for i in range(4)]
    five = [*four, "c4"]
    modulo = reroute_fraction(
        ids, lambda t: str(route_modulo(t, 4)), lambda t: str(route_modulo(t, 5))
    )
    hrw = reroute_fraction(
        ids, lambda t: route_rendezvous(t, four), lambda t: route_rendezvous(t, five)
    )
    assert modulo > 0.7
    assert 0.15 < hrw < 0.25


@pytest.mark.unit
def test_trace_id_routing_keeps_a_trace_on_one_collector_and_span_id_routing_splits_it() -> None:
    backends = ["a", "b", "c"]
    rig = _Rig()
    root = rig.tracer.start_span("r", kind=SpanKind.SERVER, start_time=0)
    kids = [
        rig.tracer.start_span(f"k{i}", context=set_span_in_context(root), start_time=0)
        for i in range(8)
    ]
    spans = [root, *kids]
    by_trace = {route_rendezvous(s.get_span_context().trace_id, backends) for s in spans}
    by_span = {route_rendezvous(s.get_span_context().span_id, backends) for s in spans}
    assert len(by_trace) == 1
    assert len(by_span) > 1


def _make_app(clock: list[int]) -> tuple[TracingMiddleware, InMemorySpanExporter]:
    async def ok(request: StarletteRequest) -> Response:
        return PlainTextResponse("ok")

    async def slow(request: StarletteRequest) -> Response:
        clock[0] += 800 * MS
        return PlainTextResponse("slow")

    async def boom(request: StarletteRequest) -> Response:
        raise RuntimeError("boom")

    app = Starlette(routes=[Route("/ok", ok), Route("/slow", slow), Route("/boom", boom)])
    rig_exporter = InMemorySpanExporter()
    tail = TailBufferProcessor(rig_exporter, TailPolicy(baseline_ratio=0.0), clock=lambda: clock[0])
    provider = TracerProvider(sampler=record_all_sampler(0.0), id_generator=SeededIdGenerator(2))
    provider.add_span_processor(tail)
    return TracingMiddleware(app, provider.get_tracer("asgi"), lambda: clock[0]), rig_exporter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_tail_keeps_the_500_and_the_slow_request_and_drops_the_fast_200s() -> None:
    clock = [0]
    mw, exporter = _make_app(clock)
    transport = httpx.ASGITransport(app=mw, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/ok", "/ok", "/boom", "/slow", "/ok"):
            clock[0] += 1 * MS
            await client.get(path)
    kept = {s.attributes["http.route"]: s for s in exporter.get_finished_spans() if s.attributes}
    assert set(kept) == {"/boom", "/slow"}
    assert kept["/boom"].attributes is not None
    assert kept["/boom"].attributes["http.response.status_code"] == 500
