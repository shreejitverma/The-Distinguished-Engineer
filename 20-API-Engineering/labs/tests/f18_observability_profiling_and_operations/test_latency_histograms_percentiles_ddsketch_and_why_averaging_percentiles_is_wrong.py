"""Lab for the latency histogram note: mergeable summaries versus averaged percentiles."""

from __future__ import annotations

import itertools
import math

import httpx
import pytest
from prometheus_client import CollectorRegistry, Histogram

from api_labs.f18_observability_profiling_and_operations.latency_histograms_percentiles_ddsketch_and_why_averaging_percentiles_is_wrong import (  # noqa: E501
    LATENCY_BUCKETS,
    ClassicHistogram,
    DDSketch,
    ExponentialHistogram,
    average_of_quantiles,
    exact_quantile,
    fleet_p99,
    instrumented_app,
    lognormal_population,
    sum_by_le,
    two_pod_fleet,
)


def _classic(values: list[float], bounds: tuple[float, ...] = LATENCY_BUCKETS) -> ClassicHistogram:
    h = ClassicHistogram(bounds)
    for v in values:
        h.observe(v)
    return h


def _native(values: list[float], schema: int = 3) -> ExponentialHistogram:
    h = ExponentialHistogram(schema)
    for v in values:
        h.observe(v)
    return h


def _sketch(values: list[float], alpha: float = 0.01, max_bins: int = 2048) -> DDSketch:
    s = DDSketch(alpha, max_bins)
    for v in values:
        s.add(v)
    return s


def _pareto(n: int, shape: float = 1.5, scale: float = 0.01) -> list[float]:
    """Deterministic heavy tail: the Pareto inverse CDF at the midpoints (i + 0.5) / n."""
    return [scale * (1.0 - (i + 0.5) / n) ** (-1.0 / shape) for i in range(n)]


def _bucket_around(value: float) -> tuple[float, float]:
    bounds = (0.0, *LATENCY_BUCKETS)
    return next((lo, hi) for lo, hi in itertools.pairwise(bounds) if value <= hi)


# --- the claim: averaging percentiles is invalid, merging distributions is not -------------------


@pytest.mark.unit
def test_mean_of_pod_p99_is_wrong_in_both_directions() -> None:
    mixed = fleet_p99(0.10)
    # A 10% slow pod: the plain mean of p99s overshoots, the traffic-weighted mean undershoots.
    assert mixed.mean_of_pod_p99 / mixed.true > 1.15
    assert mixed.true / mixed.traffic_weighted_mean_of_pod_p99 > 2.5
    # A 0.5% slow pod: the plain mean of p99s is nearly an order of magnitude off.
    rare = fleet_p99(0.005)
    assert rare.mean_of_pod_p99 / rare.true > 8.0


@pytest.mark.unit
def test_same_per_pod_p99_and_sizes_can_give_different_fleet_p99() -> None:
    steady = [1.0] * 1000
    half_slow = [1.0] * 500 + [100.0] * 500
    rare_slow = [1.0] * 985 + [100.0] * 15
    # Both pairs have per-pod p99 (1, 100) and sizes (1000, 1000) ...
    assert exact_quantile(half_slow, 0.99) == exact_quantile(rare_slow, 0.99) == 100.0
    # ... but the fleet p99 is 100 for one pair and 1 for the other: no f(p99s, sizes) exists.
    assert exact_quantile(steady + half_slow, 0.99) == 100.0
    assert exact_quantile(steady + rare_slow, 0.99) == 1.0
    # Merged histograms keep the information the quantile throws away.
    bounds = (1.0, 100.0)
    a = _classic(steady, bounds).merge(_classic(half_slow, bounds))
    b = _classic(steady, bounds).merge(_classic(rare_slow, bounds))
    assert a.quantile(0.99) != b.quantile(0.99)


@pytest.mark.unit
def test_no_fixed_weight_recovers_the_fleet_p99() -> None:
    def best_weight(share: float) -> float:
        fast, slow = two_pod_fleet(share)
        p_fast, p_slow = exact_quantile(fast, 0.99), exact_quantile(slow, 0.99)
        truth = exact_quantile(fast + slow, 0.99)
        lo, hi = 0.0, 1.0  # weight on the slow pod; the mixture is monotone in it
        for _ in range(50):
            mid = (lo + hi) / 2
            if average_of_quantiles([p_fast, p_slow], [1 - mid, mid]) < truth:
                lo = mid
            else:
                hi = mid
        return lo

    w_ten, w_one = best_weight(0.10), best_weight(0.01)
    # The weight that happens to work depends on the data and is not the traffic share.
    assert abs(w_ten - w_one) > 0.2
    assert abs(w_ten - 0.10) > 0.1


@pytest.mark.unit
@pytest.mark.parametrize("share", [0.005, 0.01, 0.05, 0.10, 0.50])
def test_merged_distributions_track_the_true_fleet_p99(share: float) -> None:
    r = fleet_p99(share)
    assert r.native_merge == pytest.approx(r.true, rel=0.05)  # schema 3: growth factor 1.0905
    assert r.ddsketch_merge == pytest.approx(r.true, rel=0.011)  # alpha = 0.01 plus rank slack
    # The classic merge is exact in the buckets; its error is bounded by the bucket width only.
    lower, upper = _bucket_around(r.true)
    assert lower < r.classic_merge <= upper


# --- merge semantics ------------------------------------------------------------------------------


@pytest.mark.unit
def test_merge_equals_observing_the_union() -> None:
    fast, slow = two_pod_fleet(0.10, 4_000)
    assert _classic(fast).merge(_classic(slow)).counts == _classic(fast + slow).counts
    merged_native = _native(fast).merge(_native(slow))
    union_native = _native(fast + slow)
    assert merged_native.buckets == union_native.buckets
    assert merged_native.zero_count == union_native.zero_count
    merged_sketch = _sketch(fast).merge(_sketch(slow))
    assert merged_sketch.bins == _sketch(fast + slow).bins
    assert merged_sketch.count == len(fast) + len(slow)


@pytest.mark.unit
def test_classic_histograms_with_different_bounds_are_not_mergeable() -> None:
    a, b = ClassicHistogram((0.1, 0.5)), ClassicHistogram((0.1, 0.4))
    with pytest.raises(ValueError, match="not mergeable"):
        a.merge(b)


@pytest.mark.unit
def test_sum_by_le_over_mixed_bucket_layouts_is_silently_non_monotonic() -> None:
    old_pod = _classic([0.05] * 10, (0.1, 0.5))
    new_pod = _classic([0.05], (0.1, 0.25, 0.5))
    series = sum_by_le([old_pod, new_pod])
    # le=0.25 exists only on the new pod, so it sums to 1 while le=0.1 sums to 11.
    assert series == [(0.1, 11), (0.25, 1), (0.5, 11), (math.inf, 11)]
    counts = [c for _, c in series]
    assert counts != sorted(counts)
    same_layout = sum_by_le([old_pod, _classic([0.05], (0.1, 0.5))])
    assert [c for _, c in same_layout] == sorted(c for _, c in same_layout)


@pytest.mark.unit
def test_ddsketch_with_different_gamma_is_not_mergeable() -> None:
    with pytest.raises(ValueError, match="different gamma"):
        DDSketch(0.01).merge(DDSketch(0.02))


# --- classic histogram: interpolation and its error -------------------------------------------


@pytest.mark.unit
def test_classic_quantile_reproduces_the_prometheus_doc_spike_example() -> None:
    bounds = (0.1, 0.2, 0.3, 0.45)
    at_220ms = _classic([0.22] * 1000, bounds)
    # Prometheus docs: the 95th percentile is estimated at 295ms for a spike at 220ms.
    assert at_220ms.quantile(0.95) == pytest.approx(0.295)
    at_320ms = _classic([0.32] * 1000, bounds)
    # ... and at 443ms once the spike moves to 320ms (true value 320ms).
    assert at_320ms.quantile(0.95) == pytest.approx(0.4425)


@pytest.mark.unit
def test_classic_quantile_error_is_bounded_by_the_bucket_width_only() -> None:
    values = [0.22] * 1000
    h = _classic(values, (0.1, 0.2, 0.3, 0.45))
    for q in (0.01, 0.5, 0.99):
        assert 0.2 <= h.quantile(q) <= 0.3
    assert abs(h.quantile(0.99) - 0.22) > 0.07  # the interpolation assumes a uniform bucket


@pytest.mark.unit
def test_classic_quantile_in_the_inf_bucket_returns_the_last_finite_bound() -> None:
    h = _classic([50.0] * 10, (1.0, 10.0))
    assert h.quantile(0.5) == 10.0
    assert h.quantile(0.99) == 10.0


@pytest.mark.unit
def test_classic_quantile_edge_cases_follow_the_documented_special_cases() -> None:
    empty = ClassicHistogram((1.0,))
    assert math.isnan(empty.quantile(0.5))
    h = _classic([0.5], (1.0,))
    assert h.quantile(-0.1) == -math.inf
    assert h.quantile(1.1) == math.inf
    assert math.isnan(h.quantile(math.nan))
    # Lowest bucket with a non-positive upper bound returns that bound.
    assert _classic([-3.0], (-1.0, 1.0)).quantile(0.5) == -1.0


@pytest.mark.unit
def test_classic_fraction_needs_an_exact_bucket_boundary() -> None:
    h = _classic([0.05] * 90 + [0.4] * 10, (0.1, 0.5))
    assert h.fraction_le(0.1) == pytest.approx(0.9)
    assert h.fraction_le(0.3) is None  # PromQL returns no series at all here


@pytest.mark.unit
def test_classic_bucket_upper_bound_is_inclusive() -> None:
    h = ClassicHistogram((0.1, 0.5))
    h.observe(0.1)
    h.observe(0.1000001)
    assert h.counts == [1, 1, 0]


# --- native (exponential) histogram ----------------------------------------------------------


@pytest.mark.unit
def test_native_schema_growth_factors_match_the_spec_table() -> None:
    assert ExponentialHistogram(0).growth_factor == pytest.approx(2.0)
    assert ExponentialHistogram(3).growth_factor == pytest.approx(1.0905, abs=1e-4)
    assert ExponentialHistogram(3).growth_factor <= 1.1 < ExponentialHistogram(2).growth_factor
    assert ExponentialHistogram(8).growth_factor == pytest.approx(1.0027, abs=1e-4)


@pytest.mark.unit
@pytest.mark.parametrize("schema", [-4, -2, 0, 1, 3, 8])
def test_native_index_puts_every_value_inside_its_bucket(schema: int) -> None:
    h = ExponentialHistogram(schema)
    for v in [1e-6, 0.003, 0.05, 0.9, 1.0, 2.0, 3.7, 1000.0, 2.0**20]:
        lower, upper = h.bounds(h.index(v))
        assert lower < v <= upper * (1 + 1e-12)


@pytest.mark.unit
def test_native_powers_of_two_sit_on_inclusive_upper_boundaries() -> None:
    h = ExponentialHistogram(0)
    assert h.index(1.0) == 0
    assert h.index(2.0) == 1
    assert h.index(2.0000001) == 2
    assert h.index(0.5) == -1


@pytest.mark.unit
@pytest.mark.parametrize("target", [2, 0, -3])
def test_native_schema_reduction_equals_observing_at_the_lower_schema(target: int) -> None:
    values = lognormal_population(0.05, 1.2, 5_000)
    reduced = _native(values, 3).reduced(target)
    direct = _native(values, target)
    assert reduced.buckets == direct.buckets
    assert reduced.count == direct.count


@pytest.mark.unit
def test_native_merge_uses_the_lowest_schema_and_loses_nothing() -> None:
    fine, coarse = (
        _native(two_pod_fleet(0.5, 2_000)[0], 5),
        _native(two_pod_fleet(0.5, 2_000)[1], 1),
    )
    merged = fine.merge(coarse)
    assert merged.schema == 1
    assert merged.count == fine.count + coarse.count
    assert merged.sum == pytest.approx(fine.sum + coarse.sum)


@pytest.mark.unit
def test_native_bucket_limit_halves_resolution_until_it_fits() -> None:
    wide = _native(lognormal_population(0.05, 2.0, 20_000), 8)
    assert len(wide.buckets) > 300
    limited = wide.limit_buckets(160)
    assert len(limited.buckets) <= 160
    assert limited.schema < wide.schema
    assert limited.count == wide.count
    # With an impossible limit the schema bottoms out at -4 instead of looping forever.
    assert wide.limit_buckets(1).schema == ExponentialHistogram.MIN_SCHEMA


@pytest.mark.unit
def test_native_quantile_relative_error_is_below_the_growth_factor() -> None:
    values = lognormal_population(0.05, 1.0, 20_000)
    h = _native(values, 3)
    for q in (0.5, 0.9, 0.99, 0.999):
        err = abs(h.quantile(q) - exact_quantile(values, q)) / exact_quantile(values, q)
        assert err <= h.growth_factor - 1.0


@pytest.mark.unit
def test_native_zero_bucket_catches_tiny_and_zero_observations() -> None:
    h = ExponentialHistogram(3, zero_threshold=0.001)
    for v in (0.0, 0.0005, 0.001, 0.002, 0.5):
        h.observe(v)
    assert h.zero_count == 3
    assert h.count == 5
    assert h.quantile(0.2) <= 0.001
    with pytest.raises(ValueError, match="non-negative"):
        h.observe(-1.0)


# --- DDSketch ---------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("alpha", [0.01, 0.05])
def test_ddsketch_relative_error_never_exceeds_alpha_on_a_heavy_tail(alpha: float) -> None:
    values = _pareto(20_000)
    sketch = _sketch(values, alpha)
    for q in (0.0, 0.01, 0.5, 0.9, 0.99, 0.999, 0.9999, 1.0):
        truth = exact_quantile(values, q)
        assert abs(sketch.quantile(q) - truth) <= alpha * truth * (1 + 1e-9)


@pytest.mark.unit
def test_ddsketch_bucket_count_is_logarithmic_not_linear() -> None:
    sketch = _sketch(_pareto(20_000), 0.01)
    assert len(sketch.bins) < 300  # 20,000 distinct values, about 250 buckets
    assert sum(sketch.bins.values()) == 20_000


@pytest.mark.unit
def test_ddsketch_collapse_trades_low_quantile_accuracy_for_a_hard_size_limit() -> None:
    values = _pareto(20_000)
    full, capped = _sketch(values, 0.01), _sketch(values, 0.01, max_bins=120)
    assert len(capped.bins) == 120
    assert capped.count == full.count
    p99_truth = exact_quantile(values, 0.99)
    assert abs(capped.quantile(0.99) - p99_truth) <= 0.01 * p99_truth * (1 + 1e-9)
    p01_truth = exact_quantile(values, 0.01)
    assert abs(capped.quantile(0.01) - p01_truth) > 0.05 * p01_truth


@pytest.mark.unit
def test_ddsketch_zero_and_empty_handling() -> None:
    sketch = DDSketch()
    with pytest.raises(ValueError, match="empty"):
        sketch.quantile(0.5)
    for v in (0.0, 0.0, 1.0, 2.0):
        sketch.add(v)
    assert sketch.quantile(0.0) == 0.0
    assert sketch.quantile(1.0) == pytest.approx(2.0, rel=0.01)
    assert sketch.sum == 3.0


# --- the real prometheus-client 0.26.0 --------------------------------------------------------


@pytest.mark.unit
def test_prometheus_client_histogram_defaults_and_inclusive_le() -> None:
    assert Histogram.DEFAULT_BUCKETS[:-1] == LATENCY_BUCKETS
    assert Histogram.DEFAULT_BUCKETS[-1] == math.inf
    registry = CollectorRegistry()
    h = Histogram("probe_seconds", "probe", buckets=(0.1, 0.5), registry=registry)
    h.observe(0.1)
    sample = {
        (s.name, s.labels.get("le")): s.value
        for metric in registry.collect()
        for s in metric.samples
    }
    assert sample[("probe_seconds_bucket", "0.1")] == 1.0
    assert sample[("probe_seconds_bucket", "0.5")] == 1.0
    assert sample[("probe_seconds_count", None)] == 1.0


@pytest.mark.unit
def test_prometheus_client_histogram_rejects_unsorted_buckets() -> None:
    with pytest.raises(ValueError, match="sorted"):
        Histogram("bad_seconds", "bad", buckets=(0.5, 0.1), registry=CollectorRegistry())


@pytest.fixture
def ticks() -> list[float]:
    return [0.0, 0.3, 10.0, 10.04]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scrape_with_openmetrics_2_still_exposes_classic_buckets_only(
    ticks: list[float],
) -> None:
    registry = CollectorRegistry()
    clock_values = iter(ticks)
    app = instrumented_app(registry, lambda: next(clock_values), (0.1, 0.5, 1.0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/work")).status_code == 200  # observes 0.3s
        assert (await client.get("/work")).status_code == 200  # observes 0.04s
        scrape = await client.get(
            "/metrics", headers={"Accept": "application/openmetrics-text; version=2.0.0"}
        )
    assert scrape.headers["content-type"].startswith("application/openmetrics-text; version=2.0.0")
    body = scrape.text
    assert 'http_request_duration_seconds_bucket{le="0.1",route="/work"} 1.0' in body
    assert 'http_request_duration_seconds_bucket{le="0.5",route="/work"} 2.0' in body
    assert "schema:" not in body  # the Python Histogram never emits a native histogram
    assert body.endswith("# EOF\n")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_scrape_is_the_classic_text_format(ticks: list[float]) -> None:
    registry = CollectorRegistry()
    clock_values = iter(ticks)
    app = instrumented_app(registry, lambda: next(clock_values), (0.1, 0.5))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/work")
        scrape = await client.get("/metrics")
    assert scrape.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "# TYPE http_request_duration_seconds histogram" in scrape.text
