"""Lab for SLIs, SLOs, error budgets, and multiwindow multi-burn-rate alerting."""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import httpx
import pytest
from prometheus_client import Histogram
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from api_labs.f18_observability_profiling_and_operations.slo_burn_rate import (
    GOOGLE_RULES,
    BurnRateRule,
    ClientErrors,
    Counts,
    CumulativeHistogram,
    MissingBucketError,
    Observation,
    Outcome,
    RequestEvent,
    Severity,
    SliMiddleware,
    SliSeries,
    StatusPolicy,
    availability_counts,
    budget_consumed,
    burn_rate,
    burn_threshold,
    evaluate_rule,
    full_outage_exhaustion_s,
    latency_counts,
    render_prometheus_rules,
    replay,
    replay_for_clause,
    segments,
    time_to_exhaustion_s,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

SLO = 0.999
PAGE_1H = GOOGLE_RULES[0]
HEALTHY = 120  # steps of healthy history so every window is full
PER_STEP = 100_000  # requests per minute; big enough that rounding never matters


# ---------------------------------------------------------------- the SLI definition


@pytest.mark.unit
def test_server_errors_are_bad_and_client_errors_are_excluded_by_default() -> None:
    policy = StatusPolicy()
    assert [policy.classify(s) for s in (200, 301, 404, 422, 500, 503)] == [
        Outcome.GOOD,
        Outcome.GOOD,
        Outcome.EXCLUDED,
        Outcome.EXCLUDED,
        Outcome.BAD,
        Outcome.BAD,
    ]
    assert StatusPolicy(ClientErrors.COUNT_AS_GOOD).classify(404) is Outcome.GOOD
    with pytest.raises(ValueError, match="not an HTTP status"):
        policy.classify(600)


@pytest.mark.unit
def test_server_fault_4xx_overrides_the_client_error_policy() -> None:
    policy = StatusPolicy(server_fault_4xx=frozenset({429}))
    assert policy.classify(429) is Outcome.BAD
    assert policy.classify(404) is Outcome.EXCLUDED


@pytest.mark.unit
def test_counting_4xx_as_good_dilutes_a_5xx_outage() -> None:
    events = (
        [RequestEvent(500, 0.01)] * 100
        + [RequestEvent(200, 0.01)] * 100
        + [RequestEvent(404, 0.01)] * 800  # a scanner flood
    )
    diluted = availability_counts(events, StatusPolicy(ClientErrors.COUNT_AS_GOOD))
    excluded = availability_counts(events, StatusPolicy(ClientErrors.EXCLUDE))
    assert diluted.error_ratio == pytest.approx(0.10)
    assert excluded.error_ratio == pytest.approx(0.50)


@pytest.mark.unit
def test_latency_sli_ignores_fast_failures_and_client_errors() -> None:
    events = [
        RequestEvent(200, 0.05),
        RequestEvent(200, 0.40),
        RequestEvent(500, 0.001),  # a fast failure must not flatter latency
        RequestEvent(404, 0.001),
    ]
    assert latency_counts(events, StatusPolicy(), 0.25) == Counts(good=1, bad=1)


@pytest.mark.unit
def test_error_ratio_is_none_without_traffic() -> None:
    assert Counts().error_ratio is None
    series = SliSeries()
    series.append(Counts())
    assert not evaluate_rule(series, PAGE_1H, SLO).firing


# ---------------------------------------------------------------- histogram SLIs


@pytest.mark.unit
def test_default_prometheus_buckets_have_250ms_but_not_300ms() -> None:
    bounds = tuple(b for b in Histogram.DEFAULT_BUCKETS if b != float("inf"))
    histogram = CumulativeHistogram.observe([0.2, 0.25, 0.26, 0.9], bounds)
    assert histogram.good_exact(0.25) == Counts(good=2, bad=2)  # le is inclusive
    with pytest.raises(MissingBucketError):
        histogram.good_exact(0.3)


@pytest.mark.unit
def test_interpolation_can_be_wildly_wrong_inside_a_wide_bucket() -> None:
    bounds = tuple(b for b in Histogram.DEFAULT_BUCKETS if b != float("inf"))
    histogram = CumulativeHistogram.observe([0.29] * 100, bounds)  # every request is under 300ms
    assert histogram.good_estimate(0.3) == pytest.approx(0.2)  # the estimate says 20 percent
    assert histogram.good_estimate(0.5) == pytest.approx(1.0)


@pytest.mark.unit
def test_histogram_rejects_non_monotonic_buckets() -> None:
    with pytest.raises(ValueError, match="non-decreasing"):
        CumulativeHistogram((0.1, 0.2), (5, 3), 6)


# ---------------------------------------------------------------- budget arithmetic


@pytest.mark.unit
def test_workbook_table_5_4_burn_rates_and_time_to_exhaustion() -> None:
    day = 86_400
    assert burn_rate(0.001, SLO) == pytest.approx(1.0)
    assert time_to_exhaustion_s(1.0) == 30 * day
    assert time_to_exhaustion_s(2.0) == 15 * day
    assert time_to_exhaustion_s(10.0) == 3 * day
    assert time_to_exhaustion_s(1000.0) / 60 == pytest.approx(43.2)


@pytest.mark.unit
def test_burn_thresholds_follow_from_budget_fraction_and_window() -> None:
    assert burn_threshold(0.02, 3_600) == pytest.approx(14.4)
    assert burn_threshold(0.05, 6 * 3_600) == pytest.approx(6.0)
    assert burn_threshold(0.10, 3 * 86_400) == pytest.approx(1.0)
    assert burn_threshold(0.05, 3_600) == pytest.approx(36.0)
    assert budget_consumed(14.4, 3_600) == pytest.approx(0.02)
    assert [r.budget_at_trigger() for r in GOOGLE_RULES] == pytest.approx([0.02, 0.05, 0.10])


@pytest.mark.unit
def test_page_rule_is_unreachable_for_a_90_percent_slo() -> None:
    assert PAGE_1H.reachable(0.999)
    assert not PAGE_1H.reachable(0.90)  # a total outage burns at 10, below 14.4
    assert full_outage_exhaustion_s(0.99999) == pytest.approx(25.92)


# ---------------------------------------------------------------- the evaluator


@pytest.mark.unit
def test_total_outage_pages_on_the_first_bad_minute() -> None:
    steps = segments((HEALTHY, PER_STEP, 0.0), (10, PER_STEP, 1.0))
    assert replay(steps, PAGE_1H, SLO).first_fire_step == HEALTHY + 1


@pytest.mark.unit
def test_a_moderate_burn_pages_only_after_the_long_window_fills() -> None:
    # 1.5 percent errors = burn 15; the 1h average crosses 1.44 percent after 57.6 minutes.
    steps = segments((HEALTHY, PER_STEP, 0.0), (120, PER_STEP, 0.015))
    assert replay(steps, PAGE_1H, SLO).first_fire_step == HEALTHY + 58


@pytest.mark.unit
def test_a_burn_just_under_the_threshold_never_pages_the_1h_rule() -> None:
    steps = segments((HEALTHY, PER_STEP, 0.0), (300, PER_STEP, 0.014))  # burn 14
    assert replay(steps, PAGE_1H, SLO).steps_firing == 0


@pytest.mark.unit
def test_a_short_blip_does_not_page() -> None:
    steps = segments((HEALTHY, PER_STEP, 0.0), (1, PER_STEP, 0.3), (60, PER_STEP, 0.0))
    assert replay(steps, PAGE_1H, SLO).steps_firing == 0


@pytest.mark.unit
def test_short_window_resets_the_alert_five_minutes_after_errors_stop() -> None:
    steps = segments((HEALTHY, PER_STEP, 0.0), (10, PER_STEP, 0.15), (90, PER_STEP, 0.0))
    both = replay(steps, PAGE_1H, SLO)
    long_only = replay(steps, PAGE_1H, SLO, use_short_window=False)
    assert both.first_fire_step == long_only.first_fire_step == HEALTHY + 6
    errors_end = HEALTHY + 10
    assert both.last_fire_step == errors_end + 4  # clear on the 5th minute after
    assert long_only.last_fire_step == errors_end + 54  # 1h window still holds the burst
    assert both.steps_firing == 9
    assert long_only.steps_firing == 59


@pytest.mark.unit
def test_rule_needs_both_windows_to_exceed_the_threshold() -> None:
    steps = segments((HEALTHY, PER_STEP, 0.0), (10, PER_STEP, 0.15), (30, PER_STEP, 0.0))
    series = SliSeries()
    for counts in steps:
        series.append(counts)
    reading = evaluate_rule(series, PAGE_1H, SLO)
    assert reading.long_burn is not None
    assert reading.long_burn > PAGE_1H.burn_threshold  # the 1h window is still hot
    assert reading.short_burn == 0.0  # the 5m window has recovered
    assert not reading.firing


@pytest.mark.unit
def test_for_clause_never_fires_on_alternating_spikes_that_burn_139_percent() -> None:
    cycle = segments((5, PER_STEP, 1.0), (5, PER_STEP, 0.0))
    steps = list(itertools.chain.from_iterable(itertools.repeat(cycle, 12)))
    bad = sum(c.bad for c in steps)
    monthly_budget = PER_STEP * 43_200 * (1 - SLO)  # same traffic for 30 days
    assert bad / monthly_budget == pytest.approx(1.389, abs=0.001)
    for_alert = replay_for_clause(steps, SLO, window_s=60, threshold=14.4, for_s=600)
    assert for_alert.steps_firing == 0
    assert replay(steps, PAGE_1H, SLO).first_fire_step is not None


@pytest.mark.unit
def test_low_traffic_single_failure_pages_unless_guarded() -> None:
    # 10 requests per hour: one request every 6th minute, and the newest one (minute 114) fails.
    steps: list[Counts] = []
    for minute in range(115):
        steps.append(Counts() if minute % 6 else Counts(good=1))
    steps[-1] = Counts(bad=1)
    assert replay(steps, PAGE_1H, SLO).firing[-1]
    guarded = BurnRateRule(Severity.PAGE, 3_600, 300, 14.4, min_events=100)
    assert replay(steps, guarded, SLO).steps_firing == 0
    assert burn_rate(1 / 10, SLO) == pytest.approx(100.0)
    assert pytest.approx(0.1389, abs=0.0001) == 1 / (10 * 24 * 30 * (1 - SLO))


@pytest.mark.unit
def test_rule_validation() -> None:
    with pytest.raises(ValueError, match="short window < long window"):
        BurnRateRule(Severity.PAGE, 300, 3_600, 14.4)
    with pytest.raises(ValueError, match="whole evaluation steps"):
        BurnRateRule(Severity.PAGE, 3_600, 90, 14.4)


@pytest.mark.unit
def test_rendered_rules_use_5xx_numerator_and_the_policy_denominator() -> None:
    text = render_prometheus_rules("api", SLO, GOOGLE_RULES)
    assert text.count("- record:") == 5  # 5m, 30m, 1h, 6h, 3d
    assert 'sum(rate(http_requests_total{job="api",status=~"5.."}[1h]))' in text
    assert 'sum(rate(http_requests_total{job="api",status!~"4.."}[1h]))' in text
    assert "job:slo_errors_per_request:ratio_rate1h > (14.4 * 0.001)\n      and\n" in text
    assert "ratio_rate5m > (14.4 * 0.001)" in text
    assert "severity: ticket" in text
    keep = render_prometheus_rules("api", SLO, GOOGLE_RULES, ClientErrors.COUNT_AS_GOOD)
    assert "status!~" not in keep


# ---------------------------------------------------------------- the ASGI recorder


async def _ok(request: Request) -> Response:
    return PlainTextResponse("ok")


async def _item(request: Request) -> Response:
    return PlainTextResponse(request.path_params["item_id"])


async def _missing(request: Request) -> Response:
    raise HTTPException(404)


async def _boom(request: Request) -> Response:
    raise RuntimeError("bug")


async def _unavailable(request: Request) -> Response:
    return PlainTextResponse("shed", status_code=503)


async def _stream_then_fail(request: Request) -> Response:
    async def body() -> AsyncIterator[bytes]:
        yield b"first"
        raise RuntimeError("dies after the headers were sent")

    return StreamingResponse(body())


def _app(seen: list[Observation]) -> Starlette:
    ticks = itertools.count()
    return Starlette(
        routes=[
            Route("/ok", _ok),
            Route("/items/{item_id}", _item),
            Route("/missing", _missing),
            Route("/boom", _boom),
            Route("/unavailable", _unavailable),
            Route("/stream-fail", _stream_then_fail),
        ],
        middleware=[
            Middleware(SliMiddleware, sink=seen.append, clock=lambda: next(ticks) * 0.25),
        ],
    )


def _client(app: Starlette) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_records_status_route_template_and_duration() -> None:
    seen: list[Observation] = []
    async with _client(_app(seen)) as client:
        assert (await client.get("/items/1")).status_code == 200
        assert (await client.get("/items/2")).status_code == 200
        assert (await client.get("/missing")).status_code == 404
        assert (await client.get("/nope")).status_code == 404
    assert [o.status for o in seen] == [200, 200, 404, 404]
    assert seen[0].route == seen[1].route == "/items/{item_id}"  # not /items/1
    assert seen[2].route == "/missing"
    assert seen[3].route == "unmatched"
    assert seen[0].duration_s == pytest.approx(0.25)  # fake clock: one tick between reads


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_exception_is_recorded_as_500_not_lost() -> None:
    seen: list[Observation] = []
    async with _client(_app(seen)) as client:
        response = await client.get("/boom")
    assert response.status_code == 500  # produced by ServerErrorMiddleware, outside the recorder
    assert seen == [Observation("/boom", 500, 0.25, truncated=False)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_after_headers_is_recorded_bad_and_truncated() -> None:
    seen: list[Observation] = []
    async with _client(_app(seen)) as client:
        response = await client.get("/stream-fail")
    assert (response.status_code, response.content) == (200, b"first")  # the wire said 200
    assert [(o.status, o.truncated) for o in seen] == [(500, True)]  # the recorder says bad


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observations_feed_the_availability_sli_under_both_policies() -> None:
    seen: list[Observation] = []
    async with _client(_app(seen)) as client:
        for path in ("/ok", "/ok", "/ok", "/missing", "/missing", "/boom", "/unavailable"):
            await client.get(path)
    events = [RequestEvent(o.status, o.duration_s) for o in seen]
    assert availability_counts(events, StatusPolicy()) == Counts(good=3, bad=2)
    assert availability_counts(events, StatusPolicy(ClientErrors.COUNT_AS_GOOD)) == Counts(5, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_unrecorded() -> None:
    seen: list[Observation] = []
    calls: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(str(scope["type"]))

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await SliMiddleware(inner, seen.append)({"type": "lifespan"}, receive, send)
    assert calls == ["lifespan"]
    assert seen == []
