"""RED and USE metrics over OpenTelemetry HTTP semantic conventions (1.45.2 SDK, 0.66b2 contrib)."""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from opentelemetry.util.http import sanitize_method

from api_labs.f18_observability_profiling_and_operations.red_and_use_metrics import (
    ACTIVE_METRIC,
    DURATION_BUCKETS_S,
    DURATION_METRIC,
    OTHER_METHOD,
    RedMiddleware,
    UseResource,
    duration_points,
    histogram_quantile,
    normalize_method,
    raw_path_label_antipattern,
    register_use_gauges,
    summarize_red,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

OPT_IN_ENV = "OTEL_SEMCONV_STABILITY_OPT_IN"
ALL_METHODS_ENV = "OTEL_PYTHON_INSTRUMENTATION_HTTP_CAPTURE_ALL_METHODS"


class FakeClock:
    """A manually advanced clock so durations are exact."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeRoute:
    """Stands in for the Starlette route object that the router stores in ``scope["route"]``."""

    path_format: str


def make_provider() -> tuple[MeterProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    return MeterProvider(metric_readers=[reader]), reader


def stub_app(
    clock: FakeClock, route: str | None, duration_s: float, status: int, *, raises: bool = False
) -> ASGIApp:
    """An ASGI app that sets its route, advances the clock by ``duration_s``, and answers."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        if route is not None:
            scope["route"] = FakeRoute(route)
        clock.now += duration_s
        if raises:
            raise ValueError("handler failed")
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    return app


async def call_raw(app: ASGIApp, method: str, path: str) -> int:
    """Drive an ASGI app with a hand-built scope, keeping the method exactly as given."""
    status = 0

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("127.0.0.1", 50000),
    }
    await app(scope, receive, send)
    return status


def build_app() -> FastAPI:
    app = FastAPI()
    sub = FastAPI()
    router = APIRouter(prefix="/users")

    @app.get("/orders/{order_id}")
    async def get_order(order_id: int) -> dict[str, int]:
        return {"id": order_id}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("handler failed")

    @router.get("/{user_id}/orders/{order_id}")
    async def user_order(user_id: int, order_id: int) -> dict[str, int]:
        return {"user": user_id, "id": order_id}

    @sub.get("/items/{item_id}")
    async def get_item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    app.include_router(router)
    app.mount("/v2", sub)
    return app


def client_for(app: ASGIApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def attribute_sets(reader: InMemoryMetricReader, name: str) -> list[dict[str, Any]]:
    """Attributes of every data point of the metric called ``name``."""
    found: list[dict[str, Any]] = []
    data = reader.get_metrics_data()
    for resource_metrics in data.resource_metrics if data else ():
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    found.extend(dict(p.attributes or {}) for p in metric.data.data_points)
    return found


def metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    names: set[str] = set()
    for resource_metrics in data.resource_metrics if data else ():
        for scope_metrics in resource_metrics.scope_metrics:
            names.update(m.name for m in scope_metrics.metrics)
    return names


def point_value(reader: InMemoryMetricReader, name: str) -> float:
    """Value of the single data point of a sum or gauge metric."""
    data = reader.get_metrics_data()
    values: list[float] = []
    for resource_metrics in data.resource_metrics if data else ():
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    values.extend(
                        float(p.value)
                        for p in metric.data.data_points
                        if isinstance(p, NumberDataPoint)
                    )
    assert len(values) == 1, (name, values)
    return values[0]


@pytest.fixture
def select_semconv_mode(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Set ``OTEL_SEMCONV_STABILITY_OPT_IN`` and reset contrib's once-only initialization."""
    stability: Any = importlib.import_module(
        "opentelemetry.instrumentation._semconv"
    )._OpenTelemetrySemanticConventionStability

    def select(mode: str) -> None:
        if mode:
            monkeypatch.setenv(OPT_IN_ENV, mode)
        else:
            monkeypatch.delenv(OPT_IN_ENV, raising=False)
        monkeypatch.setattr(stability, "_initialized", False)
        monkeypatch.setattr(stability, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING", {})

    return select


# --- RED on the lab middleware -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_label_is_the_template_not_the_raw_path() -> None:
    provider, reader = make_provider()
    app = RedMiddleware(build_app(), provider.get_meter("lab"))
    async with client_for(app) as client:
        for order_id in range(1000, 1050):
            assert (await client.get(f"/orders/{order_id}")).status_code == 200
    attrs = attribute_sets(reader, DURATION_METRIC)
    assert len(attrs) == 1
    assert attrs[0]["http.route"] == "/orders/{order_id}"
    assert summarize_red(reader.get_metrics_data(), window_s=1.0).requests == 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unmatched_path_carries_no_route_and_never_the_raw_path() -> None:
    provider, reader = make_provider()
    app = RedMiddleware(build_app(), provider.get_meter("lab"))
    async with client_for(app) as client:
        assert (await client.get("/nope/98412")).status_code == 404
    (attrs,) = attribute_sets(reader, DURATION_METRIC)
    assert "http.route" not in attrs
    assert attrs["http.response.status_code"] == 404
    assert all("98412" not in str(value) for value in attrs.values())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_included_router_and_mounted_app_keep_the_full_template() -> None:
    provider, reader = make_provider()
    app = RedMiddleware(build_app(), provider.get_meter("lab"))
    async with client_for(app) as client:
        await client.get("/users/3/orders/9")
        await client.get("/v2/items/abc")
    routes = {a["http.route"] for a in attribute_sets(reader, DURATION_METRIC)}
    assert routes == {"/users/{user_id}/orders/{order_id}", "/v2/items/{item_id}"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_path_labels_overflow_at_the_sdk_default_limit_of_2000() -> None:
    provider, reader = make_provider()
    labeled = RedMiddleware(
        build_app(), provider.get_meter("naive"), route_labeler=raw_path_label_antipattern
    )
    for order_id in range(2100):
        assert await call_raw(labeled, "GET", f"/orders/{order_id}") == 200
    points = duration_points(reader.get_metrics_data())
    overflow = [p for p in points if dict(p.attributes or {}) == {"otel.metric.overflow": True}]
    assert len(points) == 2001
    assert len(overflow) == 1
    assert overflow[0].count == 100
    assert sum(p.count for p in points) == 2100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_template_labels_stay_one_series_for_the_same_2100_requests() -> None:
    provider, reader = make_provider()
    templated = RedMiddleware(build_app(), provider.get_meter("good"))
    for order_id in range(2100):
        assert await call_raw(templated, "GET", f"/orders/{order_id}") == 200
    points = duration_points(reader.get_metrics_data())
    assert len(points) == 1
    assert points[0].count == 2100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_method_is_known_exactly_or_collapses_to_other() -> None:
    assert normalize_method("QUERY") == "QUERY"
    assert normalize_method("get") == OTHER_METHOD
    assert normalize_method("BREW") == OTHER_METHOD
    clock = FakeClock()
    provider, reader = make_provider()
    app = RedMiddleware(
        stub_app(clock, "/orders/{order_id}", 0.01, 200), provider.get_meter("lab"), clock=clock
    )
    for index in range(100):
        await call_raw(app, f"M{index}", "/orders/1")
    await call_raw(app, "GET", "/orders/1")
    methods = sorted(a["http.request.method"] for a in attribute_sets(reader, DURATION_METRIC))
    assert methods == ["GET", "_OTHER"]
    other = next(
        p
        for p in duration_points(reader.get_metrics_data())
        if p.attributes and p.attributes["http.request.method"] == "_OTHER"
    )
    assert other.count == 100


@pytest.mark.unit
def test_upstream_sanitize_method_uppercases_where_the_lab_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ALL_METHODS_ENV, raising=False)
    assert sanitize_method("get") == "GET"
    assert sanitize_method("BREW") == "_OTHER"
    assert sanitize_method("QUERY") == "QUERY"
    assert normalize_method("get") == "_OTHER"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_server_failures_carry_error_type() -> None:
    provider, reader = make_provider()
    app = RedMiddleware(build_app(), provider.get_meter("lab"))
    async with client_for(app) as client:
        for path in ("/orders/1", "/nope/1", "/users/x/orders/1", "/boom"):
            await client.get(path)
    by_status = {a["http.response.status_code"]: a for a in attribute_sets(reader, DURATION_METRIC)}
    assert set(by_status) == {200, 404, 422, 500}
    assert [s for s, a in by_status.items() if "error.type" in a] == [500]
    assert by_status[500]["error.type"] == "RuntimeError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_5xx_without_an_exception_uses_the_status_as_error_type() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    app = RedMiddleware(
        stub_app(clock, "/orders/{order_id}", 0.01, 503), provider.get_meter("lab"), clock=clock
    )
    await call_raw(app, "GET", "/orders/1")
    (attrs,) = attribute_sets(reader, DURATION_METRIC)
    assert attrs["error.type"] == "503"
    assert attrs["http.response.status_code"] == 503


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exception_before_a_response_has_error_type_and_no_status_code() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    app = RedMiddleware(
        stub_app(clock, "/orders/{order_id}", 0.01, 200, raises=True),
        provider.get_meter("lab"),
        clock=clock,
    )
    with pytest.raises(ValueError, match="handler failed"):
        await call_raw(app, "GET", "/orders/1")
    (attrs,) = attribute_sets(reader, DURATION_METRIC)
    assert attrs["error.type"] == "ValueError"
    assert "http.response.status_code" not in attrs
    assert point_value(reader, ACTIVE_METRIC) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_active_requests_is_one_in_flight_and_zero_after() -> None:
    provider, reader = make_provider()
    seen: list[float] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        seen.append(point_value(reader, ACTIVE_METRIC))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = RedMiddleware(app, provider.get_meter("lab"))
    await call_raw(wrapped, "GET", "/x")
    assert seen == [1]
    assert point_value(reader, ACTIVE_METRIC) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duration_histogram_is_in_seconds_with_the_semconv_buckets() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    app = RedMiddleware(
        stub_app(clock, "/orders/{order_id}", 0.02, 200), provider.get_meter("lab"), clock=clock
    )
    await call_raw(app, "GET", "/orders/1")
    data = reader.get_metrics_data()
    assert data is not None
    metric = data.resource_metrics[0].scope_metrics[0].metrics
    duration = next(m for m in metric if m.name == DURATION_METRIC)
    assert duration.unit == "s"
    (point,) = duration_points(data)
    assert tuple(point.explicit_bounds) == DURATION_BUCKETS_S
    assert point.sum == pytest.approx(0.02)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_summarize_red_reports_rate_errors_and_split_latency() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    meter = provider.get_meter("lab")
    good = RedMiddleware(stub_app(clock, "/orders/{order_id}", 0.02, 200), meter, clock=clock)
    bad = RedMiddleware(stub_app(clock, "/orders/{order_id}", 0.3, 500), meter, clock=clock)
    for _ in range(90):
        await call_raw(good, "GET", "/orders/1")
    for _ in range(10):
        await call_raw(bad, "GET", "/orders/1")
    summary = summarize_red(reader.get_metrics_data(), window_s=10.0)
    assert summary.requests == 100
    assert summary.errors == 10
    assert summary.rate_per_s == pytest.approx(10.0)
    assert summary.error_ratio == pytest.approx(0.1)
    assert summary.p_ok_s == pytest.approx(0.02485)
    assert summary.p_error_s == pytest.approx(0.4975)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_failures_do_not_hide_slow_successes() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    meter = provider.get_meter("lab")
    slow_ok = RedMiddleware(stub_app(clock, "/pay", 0.4, 200), meter, clock=clock)
    fast_fail = RedMiddleware(stub_app(clock, "/pay", 0.001, 500), meter, clock=clock)
    for _ in range(50):
        await call_raw(slow_ok, "GET", "/pay")
        await call_raw(fast_fail, "GET", "/pay")
    summary = summarize_red(reader.get_metrics_data(), window_s=1.0, route="/pay")
    assert summary.p_ok_s is not None
    assert summary.p_error_s is not None
    assert summary.p_ok_s > 0.25
    assert summary.p_error_s <= 0.005


@pytest.mark.unit
@pytest.mark.asyncio
async def test_summarize_red_filters_by_route() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    meter = provider.get_meter("lab")
    for route, count in (("/a", 3), ("/b", 5)):
        app = RedMiddleware(stub_app(clock, route, 0.01, 200), meter, clock=clock)
        for _ in range(count):
            await call_raw(app, "GET", route)
    data = reader.get_metrics_data()
    assert summarize_red(data, window_s=1.0, route="/a").requests == 3
    assert summarize_red(data, window_s=1.0, route="/b").requests == 5
    assert summarize_red(data, window_s=1.0).requests == 8


@pytest.mark.unit
def test_summarize_red_handles_no_data_and_rejects_a_zero_window() -> None:
    empty = summarize_red(None, window_s=5.0)
    assert (empty.requests, empty.errors, empty.rate_per_s) == (0, 0, 0.0)
    assert math.isnan(empty.error_ratio)
    assert empty.p_ok_s is None
    assert empty.p_error_s is None
    with pytest.raises(ValueError, match="window_s"):
        summarize_red(None, window_s=0.0)


@pytest.mark.unit
def test_histogram_quantile_interpolates_inside_a_bucket_and_caps_at_the_top_bound() -> None:
    assert histogram_quantile(0.5, (1.0, 2.0), [0, 10, 0]) == pytest.approx(1.5)
    assert histogram_quantile(0.5, (1.0, 2.0), [0, 0, 0]) is None
    assert histogram_quantile(0.99, DURATION_BUCKETS_S, [0] * 14 + [20]) == 10.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_thirty_second_requests_report_as_ten_seconds() -> None:
    clock = FakeClock()
    provider, reader = make_provider()
    app = RedMiddleware(stub_app(clock, "/slow", 30.0, 200), provider.get_meter("lab"), clock=clock)
    for _ in range(20):
        await call_raw(app, "GET", "/slow")
    summary = summarize_red(reader.get_metrics_data(), window_s=600.0)
    assert summary.p_ok_s == 10.0


# --- the real OpenTelemetry instrumentation ------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("", {"http.server.duration"}),
        ("http", {"http.server.request.duration"}),
        ("http/dup", {"http.server.duration", "http.server.request.duration"}),
    ],
)
async def test_opt_in_mode_selects_which_duration_metrics_are_emitted(
    select_semconv_mode: Callable[[str], None], mode: str, expected: set[str]
) -> None:
    select_semconv_mode(mode)
    provider, reader = make_provider()
    app = build_app()
    FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
    async with client_for(app) as client:
        await client.get("/orders/98412")
    durations = {n for n in metric_names(reader) if n.endswith("duration")}
    assert durations == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stable_mode_labels_the_route_template_and_omits_it_when_unmatched(
    select_semconv_mode: Callable[[str], None],
) -> None:
    select_semconv_mode("http")
    provider, reader = make_provider()
    app = build_app()
    FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
    async with client_for(app) as client:
        for path in ("/orders/98412", "/orders/7", "/nope/123", "/boom"):
            await client.get(path)
    points = {
        p.attributes["http.response.status_code"]: p
        for p in duration_points(reader.get_metrics_data())
        if p.attributes
    }
    ok = points[200]
    assert ok.count == 2
    assert dict(ok.attributes or {}) == {
        "url.scheme": "http",
        "network.protocol.version": "1.1",
        "http.request.method": "GET",
        "http.route": "/orders/{order_id}",
        "http.response.status_code": 200,
    }
    assert "http.route" not in (points[404].attributes or {})
    assert (points[500].attributes or {})["error.type"] == "500"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_mode_has_no_http_route_and_puts_the_template_in_http_target(
    select_semconv_mode: Callable[[str], None],
) -> None:
    select_semconv_mode("")
    provider, reader = make_provider()
    app = build_app()
    FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
    async with client_for(app) as client:
        await client.get("/orders/98412")
        await client.get("/nope/98412")
    attrs = attribute_sets(reader, "http.server.duration")
    assert all("http.route" not in a for a in attrs)
    assert {a.get("http.target") for a in attrs} == {"/orders/{order_id}", None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upstream_fastapi_instrumentor_collapses_a_mounted_app_to_the_mount_prefix(
    select_semconv_mode: Callable[[str], None],
) -> None:
    select_semconv_mode("http")
    provider, reader = make_provider()
    app = build_app()
    FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
    async with client_for(app) as client:
        await client.get("/v2/items/abc")
    (attrs,) = attribute_sets(reader, DURATION_METRIC)
    assert attrs["http.route"] == "/v2"


# --- USE -----------------------------------------------------------------------------------


@pytest.mark.unit
def test_average_utilization_stays_low_while_a_burst_queues_requests() -> None:
    clock = FakeClock()
    pool = UseResource(capacity=4, max_queue=8, clock=clock)
    clock.now = 98.0
    assert [pool.acquire() for _ in range(4)] == ["running"] * 4
    assert [pool.acquire() for _ in range(3)] == ["queued"] * 3
    clock.now = 100.0
    snap = pool.snapshot()
    assert snap.utilization == pytest.approx(0.02)
    assert snap.average_queue == pytest.approx(0.06)
    assert snap.peak_queue == 3
    assert snap.queued_now == 3
    assert snap.in_use_now == 4
    assert snap.rejected == 0


@pytest.mark.unit
def test_a_pool_busy_for_the_whole_window_is_fully_utilized_with_no_queue() -> None:
    clock = FakeClock()
    pool = UseResource(capacity=2, max_queue=0, clock=clock)
    pool.acquire()
    pool.acquire()
    clock.now = 10.0
    snap = pool.snapshot()
    assert snap.utilization == pytest.approx(1.0)
    assert snap.average_queue == 0.0


@pytest.mark.unit
def test_a_full_queue_rejects_and_release_promotes_the_oldest_waiter() -> None:
    pool = UseResource(capacity=1, max_queue=1, clock=FakeClock())
    assert [pool.acquire() for _ in range(4)] == ["running", "queued", "rejected", "rejected"]
    assert pool.snapshot().rejected == 2
    pool.release()
    after_promotion = pool.snapshot()
    assert (after_promotion.in_use_now, after_promotion.queued_now) == (1, 0)
    pool.release()
    assert pool.snapshot().in_use_now == 0
    with pytest.raises(RuntimeError, match="matching acquire"):
        pool.release()


@pytest.mark.unit
def test_use_resource_rejects_impossible_sizes() -> None:
    with pytest.raises(ValueError, match="capacity"):
        UseResource(capacity=0, max_queue=0)
    with pytest.raises(ValueError, match="max_queue"):
        UseResource(capacity=1, max_queue=-1)


@pytest.mark.unit
def test_use_readings_reach_the_metrics_pipeline_as_observable_instruments() -> None:
    clock = FakeClock()
    pool = UseResource(capacity=1, max_queue=1, clock=clock)
    provider, reader = make_provider()
    register_use_gauges(provider.get_meter("lab"), "db", pool)
    assert [pool.acquire() for _ in range(3)] == ["running", "queued", "rejected"]
    clock.now = 10.0
    assert point_value(reader, "app.pool.utilization") == pytest.approx(1.0)
    assert point_value(reader, "app.pool.queued") == 1
    assert point_value(reader, "app.pool.rejected") == 1
    (attrs,) = attribute_sets(reader, "app.pool.queued")
    assert attrs == {"app.pool.name": "db"}
