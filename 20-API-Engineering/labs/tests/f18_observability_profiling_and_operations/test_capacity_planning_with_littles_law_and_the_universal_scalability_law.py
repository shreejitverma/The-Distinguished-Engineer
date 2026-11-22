"""Lab for Capacity Planning with Little's Law and the Universal Scalability Law."""

from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from api_labs.f18_observability_profiling_and_operations.capacity_planning import (
    InFlightTracker,
    PoolModel,
    Request,
    UslFit,
    amdahl_speedup,
    best_integer_concurrency,
    erlang_c,
    fit_usl,
    gauge_mean,
    little_reading,
    littles_law,
    min_servers,
    mm1_latency,
    simulate_pool,
    simulate_queue,
    usl_peak,
    usl_relative,
    usl_throughput,
)

if TYPE_CHECKING:
    from starlette.requests import Request as StarletteRequest
    from starlette.types import Message, Receive, Scope, Send

LEVELS = (1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 18, 19, 20, 22, 24, 28, 32, 40, 48, 64)
FIT_LEVELS = (1, 2, 4, 8, 16, 24, 32, 48, 64)
POOL = PoolModel(parallel_time=9.0, serial_time=0.5, coherency_time=0.02)
CYCLES = 20_000


# --- Little's Law ------------------------------------------------------------------------------


@pytest.mark.unit
def test_littles_law_solves_whichever_quantity_is_missing() -> None:
    assert littles_law(rate=800, latency=0.05) == pytest.approx(40.0)
    assert littles_law(concurrency=40, latency=0.05) == pytest.approx(800.0)
    assert littles_law(concurrency=40, rate=800) == pytest.approx(0.05)


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"concurrency": 1.0, "rate": 1.0, "latency": 1.0},
        {"rate": 1.0},
        {},
        {"rate": -1.0, "latency": 1.0},
        {"rate": 0.0, "latency": 1.0},
    ],
)
def test_littles_law_rejects_wrong_arity_and_non_positive_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"exactly two|must be positive"):
        littles_law(**kwargs)


@pytest.mark.unit
def test_little_reading_is_exact_on_hand_computed_traces() -> None:
    empty_at_both_ends = [Request(0.0, 10.0), Request(5.0, 15.0)]
    exact = little_reading(empty_at_both_ends, 0.0, 15.0)
    assert exact.mean_in_system == pytest.approx(20.0 / 15.0)
    assert exact.arrival_rate * exact.mean_latency == pytest.approx(20.0 / 15.0)
    assert exact.residual == pytest.approx(0.0, abs=1e-12)

    cut_while_busy = little_reading(empty_at_both_ends, 0.0, 10.0)
    assert cut_while_busy.mean_in_system == pytest.approx(1.5)
    assert cut_while_busy.arrival_rate * cut_while_busy.mean_latency == pytest.approx(2.0)
    assert cut_while_busy.residual == pytest.approx(-0.5)


@pytest.mark.unit
def test_little_identity_is_exact_on_a_simulated_run_and_breaks_on_a_truncated_window() -> None:
    run = simulate_queue(rate=70.0, mean_service=0.01, count=5_000, seed=2)
    end = max(r.departure for r in run.requests)
    full = little_reading(run.requests, 0.0, end)
    assert abs(full.residual) < 1e-9 * full.mean_in_system

    truncated = little_reading(run.requests, 0.0, end / 50)
    assert abs(truncated.residual) > 1e-3 * truncated.mean_in_system


@pytest.mark.unit
def test_gauge_mean_integrates_a_step_function() -> None:
    events = [(0.0, 1), (1.0, 1), (3.0, -1), (4.0, -1)]
    assert gauge_mean(events, 0.0, 4.0) == pytest.approx(1.5)
    assert gauge_mean(events, 1.0, 3.0) == pytest.approx(2.0)
    assert gauge_mean(events, 3.0, 4.0) == pytest.approx(1.0)


# --- Queueing: the hockey stick and Erlang C ---------------------------------------------------


@pytest.mark.unit
def test_mm1_simulation_matches_s_over_one_minus_rho_and_blows_up_near_saturation() -> None:
    service = 0.01
    measured: dict[float, float] = {}
    for rho in (0.5, 0.9):
        run = simulate_queue(rate=rho / service, mean_service=service, count=200_000, seed=3)
        measured[rho] = sum(r.latency for r in run.requests) / len(run.requests)
        assert measured[rho] == pytest.approx(mm1_latency(rho / service, service), rel=0.05)
    assert measured[0.9] > 4.5 * measured[0.5]
    with pytest.raises(ValueError, match="utilization"):
        mm1_latency(100.0, service)


@pytest.mark.unit
def test_erlang_c_matches_the_direct_formula_and_the_single_server_case() -> None:
    assert erlang_c(1, 0.7) == pytest.approx(0.7)
    servers, load = 10, 7.0
    tail = load**servers / math.factorial(servers) * servers / (servers - load)
    head = sum(load**k / math.factorial(k) for k in range(servers))
    assert erlang_c(servers, load) == pytest.approx(tail / (head + tail), rel=1e-12)
    assert erlang_c(4, 4.0) == 1.0


@pytest.mark.unit
def test_erlang_c_predicts_the_simulated_wait_fraction() -> None:
    run = simulate_queue(rate=300.0, mean_service=0.01, count=100_000, servers=4, seed=5)
    assert run.wait_fraction == pytest.approx(erlang_c(4, 3.0), abs=0.02)


@pytest.mark.unit
def test_min_servers_needs_headroom_above_the_littles_law_mean() -> None:
    for load, target in ((5.0, 0.05), (10.0, 0.05), (100.0, 0.05)):
        servers = min_servers(load, target)
        assert servers > load
        assert erlang_c(servers, load) <= target
        assert erlang_c(servers - 1, load) > target
    assert min_servers(10.0, 0.05) == 17
    assert min_servers(100.0, 0.05) - 100 < 2 * math.sqrt(100.0)


# --- Amdahl and the USL ------------------------------------------------------------------------


@pytest.mark.unit
def test_amdahl_saturates_at_the_reciprocal_of_the_serial_fraction() -> None:
    sigma = 0.05
    speedups = [amdahl_speedup(n, sigma) for n in (1, 2, 8, 64, 1024, 10**9)]
    assert speedups == sorted(speedups)
    assert speedups[0] == 1.0
    assert speedups[-1] == pytest.approx(1 / sigma, rel=1e-6)
    assert max(speedups) < 1 / sigma


@pytest.mark.unit
@pytest.mark.parametrize(("alpha", "beta"), [(0.03, 0.0004), (0.1, 0.002), (0.0, 0.001)])
def test_usl_peak_is_the_closed_form_and_throughput_falls_beyond_it(
    alpha: float, beta: float
) -> None:
    peak = usl_peak(alpha, beta)
    assert peak == pytest.approx(math.sqrt((1 - alpha) / beta))
    brute = max(range(1, 2_000), key=lambda n: usl_relative(n, alpha, beta))
    assert best_integer_concurrency(alpha, beta) == brute
    assert usl_relative(peak * 2, alpha, beta) < usl_relative(peak, alpha, beta)
    assert usl_relative(peak / 2, alpha, beta) < usl_relative(peak, alpha, beta)


@pytest.mark.unit
def test_usl_without_coherency_has_no_peak() -> None:
    assert math.isinf(usl_peak(0.05, 0.0))
    with pytest.raises(ValueError, match="no throughput maximum"):
        best_integer_concurrency(0.05, 0.0)
    assert usl_relative(10**6, 0.05, 0.0) < 20.0
    assert usl_relative(1, 0.3, 0.5) == 1.0


@pytest.mark.unit
def test_usl_fit_recovers_exact_parameters_from_noise_free_data() -> None:
    gamma, alpha, beta = 1_000.0, 0.03, 0.0004
    levels = (1, 2, 4, 8, 16, 32, 64)
    fit = fit_usl([(n, usl_throughput(n, gamma, alpha, beta)) for n in levels])
    assert fit.gamma == pytest.approx(gamma, rel=1e-9)
    assert fit.alpha == pytest.approx(alpha, rel=1e-9)
    assert fit.beta == pytest.approx(beta, rel=1e-9)
    assert fit.physical
    assert fit.peak == pytest.approx(usl_peak(alpha, beta), rel=1e-9)
    assert fit.predict(48) == pytest.approx(usl_throughput(48, gamma, alpha, beta), rel=1e-9)


@pytest.mark.unit
def test_usl_fit_with_three_percent_noise_still_locates_the_peak() -> None:
    gamma, alpha, beta = 1_000.0, 0.03, 0.0004
    rng = random.Random(1)  # noqa: S311 - deterministic noise, not security
    levels = (1, 2, 4, 8, 16, 32, 64)
    samples = [
        (n, usl_throughput(n, gamma, alpha, beta) * (1 + rng.uniform(-0.03, 0.03))) for n in levels
    ]
    fit = fit_usl(samples)
    assert fit.peak == pytest.approx(usl_peak(alpha, beta), rel=0.10)
    assert fit.gamma == pytest.approx(gamma, rel=0.05)


@pytest.mark.unit
@pytest.mark.parametrize(
    "samples",
    [
        [(1, 10.0), (2, 19.0)],
        [(1, 10.0), (1, 10.5), (2, 19.0), (2, 19.5)],
        [(0, 10.0), (2, 19.0), (4, 30.0)],
        [(1, 10.0), (2, -1.0), (4, 30.0)],
    ],
)
def test_usl_fit_rejects_degenerate_samples(samples: list[tuple[float, float]]) -> None:
    with pytest.raises(ValueError, match=r"at least three|need N >= 1"):
        fit_usl(samples)


@pytest.mark.unit
def test_physical_flag_rejects_out_of_range_parameters() -> None:
    assert UslFit(1.0, 0.1, 0.01).physical
    assert not UslFit(1.0, -0.1, 0.01).physical
    assert not UslFit(1.0, 0.1, -0.01).physical
    assert not UslFit(1.0, 1.5, 0.01).physical
    assert not UslFit(0.0, 0.1, 0.01).physical


# --- A simulated contended pool: retrograde throughput and the fitted ceiling -------------------


@pytest.mark.unit
def test_contended_pool_throughput_rises_peaks_then_falls() -> None:
    throughput = {n: simulate_pool(n, POOL, CYCLES, seed=7) for n in LEVELS}
    peak_n = max(throughput, key=lambda n: throughput[n])
    assert 16 <= peak_n <= 24
    assert throughput[2] > 1.9 * throughput[1]
    assert throughput[64] < 0.5 * throughput[peak_n]
    assert throughput[40] < throughput[peak_n]


@pytest.mark.unit
def test_fitted_usl_peak_matches_the_simulated_peak() -> None:
    throughput = {n: simulate_pool(n, POOL, CYCLES, seed=7) for n in LEVELS}
    simulated_peak = max(throughput, key=lambda n: throughput[n])
    fit = fit_usl([(n, throughput[n]) for n in FIT_LEVELS])
    assert fit.peak == pytest.approx(simulated_peak, rel=0.15)
    assert fit.beta > 0.001


@pytest.mark.unit
def test_without_the_coherency_cost_the_pool_never_retrogrades() -> None:
    flat = PoolModel(parallel_time=9.0, serial_time=0.5, coherency_time=0.0)
    throughput = {n: simulate_pool(n, flat, CYCLES, seed=7) for n in (1, 8, 16, 24, 32, 48, 64)}
    assert throughput[64] >= 0.99 * max(throughput.values())
    assert throughput[64] == pytest.approx(1 / flat.serial_time, rel=0.01)


@pytest.mark.unit
def test_fitting_only_below_the_knee_wildly_overestimates_the_ceiling() -> None:
    throughput = {n: simulate_pool(n, POOL, CYCLES, seed=7) for n in LEVELS}
    simulated_peak = max(throughput, key=lambda n: throughput[n])
    low = fit_usl([(n, throughput[n]) for n in (1, 2, 4, 8, 12)])
    assert low.peak > 2 * simulated_peak


@pytest.mark.unit
def test_simulate_pool_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="workers >= 1"):
        simulate_pool(0, POOL, CYCLES)
    with pytest.raises(ValueError, match="cycles >= 20"):
        simulate_pool(4, POOL, 5)


# --- Little's Law on a real ASGI app -----------------------------------------------------------


WORKERS = 20
FAILING = 4


def _build_app() -> tuple[InFlightTracker, asyncio.Barrier]:
    barrier = asyncio.Barrier(WORKERS)

    async def work(_: StarletteRequest) -> PlainTextResponse:
        await barrier.wait()
        await asyncio.sleep(0.01)
        return PlainTextResponse("ok")

    async def boom(_: StarletteRequest) -> PlainTextResponse:
        await barrier.wait()
        raise RuntimeError("handler failed")

    app = Starlette(routes=[Route("/work", work), Route("/boom", boom)])
    return InFlightTracker(app), barrier


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tracker_gauge_matches_lambda_times_w_even_when_handlers_raise() -> None:
    tracker, _ = _build_app()
    transport = httpx.ASGITransport(app=tracker, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        paths = ["/work"] * (WORKERS - FAILING) + ["/boom"] * FAILING
        responses = await asyncio.gather(*(client.get(path) for path in paths))

    assert sorted(r.status_code for r in responses).count(500) == FAILING
    assert len(tracker.requests) == WORKERS
    assert tracker.peak_in_flight() == WORKERS
    assert sum(delta for _, delta in tracker.events) == 0

    start = min(r.arrival for r in tracker.requests)
    end = max(r.departure for r in tracker.requests)
    reading = little_reading(tracker.requests, start, end + 1e-9)
    gauge = gauge_mean(tracker.events, start, end + 1e-9)
    assert gauge == pytest.approx(reading.arrival_rate * reading.mean_latency, rel=1e-9)
    assert gauge == pytest.approx(reading.mean_in_system, rel=1e-9)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tracker_ignores_non_http_scopes() -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(str(scope["type"]))

    async def receive() -> Message:
        return {}

    async def send(_: Message) -> None:
        return None

    tracker = InFlightTracker(app)
    await tracker({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
    assert tracker.events == []
    assert tracker.requests == []
