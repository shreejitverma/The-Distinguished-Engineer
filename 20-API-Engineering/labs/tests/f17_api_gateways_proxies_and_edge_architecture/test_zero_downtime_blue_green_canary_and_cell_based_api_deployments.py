"""Lab for Deployment Topologies for Critical APIs (rolling, blue-green, canary, cells)."""

import math
from collections.abc import Callable

import httpx
import pytest

from api_labs.f17_api_gateways_proxies_and_edge_architecture.deployment_topologies import (
    MAX_STREAM_ID,
    BlueGreenRouter,
    CanaryOutcome,
    CellRouterApp,
    Color,
    JudgePolicy,
    Layout,
    Observation,
    PoisonImpact,
    Verdict,
    assignment,
    blended_error_rate,
    bucket,
    cell_for_mod,
    cell_for_rendezvous,
    check_goaway_sequence,
    crashed_cells_after_bad_data_push,
    fisher_exact_greater,
    in_canary,
    judge,
    moved_fraction,
    poison_pill_impact,
    requests_to_see_one_failure,
    resolve_fenceposts,
    retryable_streams,
    run_canary,
    shuffle_shard_count,
    simulate_rolling_update,
    staged_apply,
    weighted_pick,
    zero_failure_upper_bound,
)

TENANTS_1000 = [f"tenant-{i:04d}" for i in range(1000)]


# ----------------------------------------------------------------------------- rolling


@pytest.mark.unit
def test_fenceposts_round_surge_up_and_unavailable_down() -> None:
    ten = resolve_fenceposts("25%", "25%", 10)
    assert (ten.surge, ten.unavailable) == (3, 2)  # ceil(2.5), floor(2.5)
    three = resolve_fenceposts("25%", "25%", 3)
    assert (three.surge, three.unavailable) == (1, 0)  # ceil(0.75), floor(0.75)


@pytest.mark.unit
def test_double_zero_fenceposts_force_one_unavailable() -> None:
    fences = resolve_fenceposts("0%", "10%", 5)  # floor(0.5) == 0, so both resolve to zero
    assert (fences.surge, fences.unavailable) == (0, 1)


@pytest.mark.unit
def test_unavailable_is_clamped_to_desired_and_bad_percent_rejected() -> None:
    assert resolve_fenceposts(0, 10, 3).unavailable == 3
    with pytest.raises(ValueError, match="invalid int-or-percent"):
        resolve_fenceposts("25", 0, 4)
    with pytest.raises(ValueError, match="invalid int-or-percent"):
        resolve_fenceposts("x%", 0, 4)


@pytest.mark.unit
def test_default_rollout_of_three_matches_kubernetes_documentation() -> None:
    result = simulate_rolling_update(3)
    assert result.completed
    assert result.max_total == 4  # "at most 4 Pods were created at all times"
    assert result.min_available == 3  # "at least 3 Pods were available"
    assert result.mixed_ticks >= 1
    last = result.ticks[-1]
    assert (last.old, last.new) == (0, 3)


@pytest.mark.unit
def test_rollout_of_ten_stays_inside_both_fenceposts() -> None:
    result = simulate_rolling_update(10, 3, 2)
    assert result.completed
    assert result.max_total == 13  # desired + maxSurge, reached
    assert result.min_available == 8  # desired - maxUnavailable, reached


@pytest.mark.unit
def test_zero_surge_rollout_never_exceeds_desired_total() -> None:
    result = simulate_rolling_update(4, 0, 1)
    assert result.completed
    assert result.max_total == 4
    assert result.min_available == 3


@pytest.mark.unit
def test_crash_looping_version_stalls_with_minimum_capacity_intact() -> None:
    result = simulate_rolling_update(10, 3, 2, new_pods_become_ready=False)
    assert not result.completed
    last = result.ticks[-1]
    # The kubernetes rolling.go comment: 13 pods, minAvailable 8, 5 unavailable new pods.
    assert (last.old, last.new, last.available) == (8, 5, 8)
    assert result.min_available == 8


# ------------------------------------------------------------------------- blue-green


@pytest.mark.unit
def test_cut_over_sends_new_requests_to_green_but_blue_drains() -> None:
    router = BlueGreenRouter()
    old_request = router.accept()
    assert old_request is Color.BLUE
    idle = router.cut_over()
    assert idle is Color.BLUE
    assert router.accept() is Color.GREEN
    assert not router.drained(Color.BLUE)  # one request still in flight on blue
    router.finish(old_request)
    assert router.drained(Color.BLUE)
    assert not router.drained(Color.GREEN)  # the live environment is never "drained"


@pytest.mark.unit
def test_rollback_is_a_second_flip_to_the_still_warm_environment() -> None:
    router = BlueGreenRouter()
    assert router.cut_over() is Color.BLUE
    assert router.cut_over() is Color.GREEN  # green was live, so the flip returns it as idle
    assert router.active is Color.BLUE


@pytest.mark.unit
def test_l4_pinned_connection_keeps_reaching_the_old_environment() -> None:
    router = BlueGreenRouter()
    conn = router.connect()
    router.cut_over()
    assert router.route(conn, per_request=False) is Color.BLUE
    assert router.route(conn, per_request=True) is Color.GREEN
    conn.closed = True  # GOAWAY, then the client reconnects
    assert router.connect().color is Color.GREEN
    with pytest.raises(ValueError, match="closed"):
        router.route(conn, per_request=True)


@pytest.mark.unit
def test_finish_without_a_request_in_flight_is_an_error() -> None:
    with pytest.raises(ValueError, match="no request in flight"):
        BlueGreenRouter().finish(Color.GREEN)


@pytest.mark.unit
def test_goaway_marks_only_streams_above_last_stream_id_as_retryable() -> None:
    opened = [1, 3, 5, 7, 9]
    assert retryable_streams(opened, 5) == (7, 9)
    assert retryable_streams(opened, MAX_STREAM_ID) == ()  # the first, "shutdown imminent" GOAWAY
    assert retryable_streams(opened, 0) == (1, 3, 5, 7, 9)


@pytest.mark.unit
def test_goaway_last_stream_id_must_never_increase() -> None:
    check_goaway_sequence([MAX_STREAM_ID, 5, 5])
    with pytest.raises(ValueError, match="increased from 5 to 7"):
        check_goaway_sequence([MAX_STREAM_ID, 5, 7])


# ---------------------------------------------------------------------------- canary


@pytest.mark.unit
def test_canary_ramp_only_adds_keys() -> None:
    keys = [f"user-{i}" for i in range(5000)]
    sets = [{k for k in keys if in_canary(k, "rel-1", bp)} for bp in (100, 500, 2500)]
    assert sets[0] < sets[1] < sets[2]  # strict subsets: nobody flips back to stable on a ramp up


@pytest.mark.unit
def test_bucket_fraction_tracks_the_weight() -> None:
    keys = [f"user-{i}" for i in range(20000)]
    hits = sum(1 for k in keys if in_canary(k, "rel-1", 500))
    assert 800 <= hits <= 1200  # 5% of 20000 is 1000, sigma is about 31
    assert all(0 <= bucket(k, "rel-1") < 10_000 for k in keys[:100])


@pytest.mark.unit
def test_a_new_salt_picks_different_canary_users() -> None:
    keys = [f"user-{i}" for i in range(20000)]
    first = {k for k in keys if in_canary(k, "rel-1", 500)}
    second = {k for k in keys if in_canary(k, "rel-2", 500)}
    assert first != second
    assert len(first & second) < len(first) // 4  # independent salts overlap by about 5%


@pytest.mark.unit
def test_weighted_pick_uses_weight_over_sum_and_zero_gets_nothing() -> None:
    picks = [weighted_pick([("stable", 95), ("canary", 5)], p) for p in range(100)]
    assert picks.count("canary") == 5
    picks = [weighted_pick([("a", 1), ("b", 3)], p) for p in range(4)]
    assert picks == ["a", "b", "b", "b"]  # weights need not sum to 100
    assert {weighted_pick([("off", 0), ("on", 1)], 0)} == {"on"}


@pytest.mark.unit
def test_weighted_pick_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="weights must be >= 0"):
        weighted_pick([("a", -1), ("b", 2)], 0)
    with pytest.raises(ValueError, match="point must be in"):
        weighted_pick([("a", 0)], 0)
    with pytest.raises(ValueError, match="point must be in"):
        weighted_pick([("a", 1), ("b", 1)], 2)


@pytest.mark.unit
def test_blended_error_rate_hides_a_canary_regression() -> None:
    # Google SRE Workbook: 20% errors on a 5% canary shows up as 1% fleet-wide.
    assert blended_error_rate(0.05, 0.20, 0.0) == pytest.approx(0.01)


@pytest.mark.unit
def test_zero_failure_upper_bound_is_exact_and_near_rule_of_three() -> None:
    n = 3000
    bound = zero_failure_upper_bound(n)
    assert (1 - bound) ** n == pytest.approx(0.05)
    assert abs(bound - 3 / n) / (3 / n) < 0.005  # 3/n is the approximation, not the answer
    assert bound < 3 / n  # the exact bound is slightly smaller


@pytest.mark.unit
def test_requests_needed_to_see_one_failure_has_a_tight_threshold() -> None:
    assert requests_to_see_one_failure(0.01) == 299
    assert requests_to_see_one_failure(0.001) == 2995
    for rate in (0.01, 0.001):
        n = requests_to_see_one_failure(rate)
        assert (1 - rate) ** n <= 0.05 < (1 - rate) ** (n - 1)


def _exact_fisher_greater(a_f: int, a_n: int, b_f: int, b_n: int) -> float:
    total, failures = a_n + b_n, a_f + b_f
    tail = sum(
        math.comb(failures, k) * math.comb(total - failures, a_n - k)
        for k in range(a_f, min(failures, a_n) + 1)
    )
    return tail / math.comb(total, a_n)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("a_f", "a_n", "b_f", "b_n"),
    [(3, 10, 0, 10), (8, 20, 2, 30), (0, 10, 0, 10), (5, 40, 5, 60)],
)
def test_fisher_matches_an_exact_integer_oracle(a_f: int, a_n: int, b_f: int, b_n: int) -> None:
    assert fisher_exact_greater(a_f, a_n, b_f, b_n) == pytest.approx(
        _exact_fisher_greater(a_f, a_n, b_f, b_n), rel=1e-9
    )


@pytest.mark.unit
def test_fisher_is_one_sided() -> None:
    worse = fisher_exact_greater(30, 300, 3, 3000)
    better = fisher_exact_greater(3, 300, 300, 3000)  # canary 1% against control 10%
    assert worse < 1e-6
    assert better > 0.99


@pytest.mark.unit
def test_judge_holds_when_either_sample_is_too_small() -> None:
    assert judge(Observation(5, 50), Observation(0, 5000)) is Verdict.HOLD
    assert judge(Observation(0, 5000), Observation(5, 50)) is Verdict.HOLD


@pytest.mark.unit
def test_judge_rolls_back_a_clear_regression_and_promotes_equal_rates() -> None:
    assert judge(Observation(30, 300), Observation(3, 3000)) is Verdict.ROLLBACK
    assert judge(Observation(3, 3000), Observation(3, 3000)) is Verdict.PROMOTE
    assert judge(Observation(0, 3000), Observation(30, 3000)) is Verdict.PROMOTE  # canary better


@pytest.mark.unit
def test_judge_effect_size_guard_ignores_significant_but_tiny_excess() -> None:
    canary, control = Observation(1200, 1_000_000), Observation(1000, 1_000_000)
    assert fisher_exact_greater(1200, 1_000_000, 1000, 1_000_000) < 0.01  # significant
    assert judge(canary, control) is Verdict.PROMOTE  # excess 0.0002 < min_excess 0.001
    assert judge(canary, control, JudgePolicy(min_excess=0.0)) is Verdict.ROLLBACK


type ObserveFn = Callable[[int], tuple[Observation, Observation]]
STEPS = (1, 5, 25, 50)  # no 100% step: with no control left there is nothing to compare


def _observer(
    traffic: int, canary_rate: float, control_rate: float, *, bad_from_percent: int = 0
) -> ObserveFn:
    def observe(percent: int) -> tuple[Observation, Observation]:
        canary_n = traffic * percent // 100
        control_n = traffic - canary_n
        rate = canary_rate if percent >= bad_from_percent else control_rate
        return (
            Observation(round(canary_n * rate), canary_n),
            Observation(round(control_n * control_rate), control_n),
        )

    return observe


@pytest.mark.unit
def test_bad_release_is_aborted_at_the_first_step() -> None:
    outcome = run_canary(STEPS, _observer(100_000, 0.10, 0.001))
    assert outcome == CanaryOutcome(Verdict.ROLLBACK, 1, 1)  # exposure was 1% of traffic


@pytest.mark.unit
def test_good_release_ramps_through_every_step() -> None:
    outcome = run_canary(STEPS, _observer(100_000, 0.001, 0.001))
    assert outcome == CanaryOutcome(Verdict.PROMOTE, 50, len(STEPS))


@pytest.mark.unit
def test_load_dependent_regression_is_caught_only_at_the_step_that_triggers_it() -> None:
    outcome = run_canary(STEPS, _observer(100_000, 0.05, 0.001, bad_from_percent=25))
    assert outcome == CanaryOutcome(Verdict.ROLLBACK, 25, 3)


@pytest.mark.unit
def test_first_step_with_too_little_traffic_holds_instead_of_promoting() -> None:
    outcome = run_canary(STEPS, _observer(10_000, 0.0, 0.0))  # 1% of 10000 is 100 < 300
    assert outcome == CanaryOutcome(Verdict.HOLD, 1, 1)


# ------------------------------------------------------------------------------- cells


@pytest.mark.unit
def test_cell_for_mod_is_deterministic_in_range_and_roughly_uniform() -> None:
    tenants = [f"tenant-{i}" for i in range(10_000)]
    cells = [cell_for_mod(t, 10) for t in tenants]
    assert cells == [cell_for_mod(t, 10) for t in tenants]
    counts = [cells.count(c) for c in range(10)]
    assert all(800 <= c <= 1200 for c in counts)


@pytest.mark.unit
def test_poison_pill_in_a_shared_pool_takes_every_tenant_down() -> None:
    workers = [f"w{i}" for i in range(20)]
    impact = poison_pill_impact(Layout.SHARED, TENANTS_1000, workers, TENANTS_1000[7])
    assert impact == PoisonImpact(fully_down=1000, degraded=0, unaffected=0)


@pytest.mark.unit
def test_poison_pill_in_cells_affects_exactly_the_poison_tenants_cell() -> None:
    workers = [f"w{i}" for i in range(20)]
    poison = TENANTS_1000[7]
    impact = poison_pill_impact(Layout.CELLS, TENANTS_1000, workers, poison, cell_count=10)
    same_cell = sum(1 for t in TENANTS_1000 if cell_for_mod(t, 10) == cell_for_mod(poison, 10))
    assert impact.fully_down == same_cell
    assert impact.degraded == 0  # cells share nothing, so there is no partial damage either
    assert impact.unaffected == 1000 - same_cell
    assert 0.05 <= impact.fully_down / 1000 <= 0.15  # about 1/N for N = 10


@pytest.mark.unit
def test_shuffle_shards_leave_other_tenants_at_least_one_worker() -> None:
    workers = [f"w{i}" for i in range(8)]
    tenants = [f"tenant-{i:05d}" for i in range(2800)]
    poison = tenants[0]
    dead = assignment(Layout.SHUFFLE, poison, workers, k=2)
    identical = 0
    for tenant in tenants:
        mine = assignment(Layout.SHUFFLE, tenant, workers, k=2)
        assert len(mine) == 2
        if mine == dead:
            identical += 1
        else:
            assert len(mine & dead) <= 1  # AWS: at most one worker of another shard is affected
    impact = poison_pill_impact(Layout.SHUFFLE, tenants, workers, poison, k=2)
    assert impact.fully_down == identical
    assert impact.degraded > 0
    assert impact.fully_down / len(tenants) < 0.07  # expected 1/28 = 3.6%


@pytest.mark.unit
def test_blast_radius_shrinks_from_shared_to_cells_to_shuffle() -> None:
    workers = [f"w{i}" for i in range(8)]
    tenants = [f"tenant-{i:05d}" for i in range(2800)]
    poison = tenants[0]
    shared = poison_pill_impact(Layout.SHARED, tenants, workers, poison).fully_down
    cells = poison_pill_impact(Layout.CELLS, tenants, workers, poison, cell_count=4).fully_down
    shuffle = poison_pill_impact(Layout.SHUFFLE, tenants, workers, poison, k=2).fully_down
    assert shared == len(tenants)
    assert 0.15 * len(tenants) <= cells <= 0.35 * len(tenants)  # about 1/4
    assert shuffle < cells // 3


@pytest.mark.unit
def test_cells_layout_requires_even_division() -> None:
    with pytest.raises(ValueError, match="divide evenly"):
        assignment(Layout.CELLS, "t", [f"w{i}" for i in range(7)], cell_count=2)


@pytest.mark.unit
def test_shuffle_shard_counts_match_the_aws_figures() -> None:
    assert shuffle_shard_count(8, 2) == 28
    assert shuffle_shard_count(2048, 4) == 2048 * 2047 * 2046 * 2045 // 24
    assert 730_000_000_000 <= shuffle_shard_count(2048, 4) < 731_000_000_000


@pytest.mark.unit
def test_rendezvous_hashing_moves_tenants_only_to_the_new_cell() -> None:
    tenants = [f"tenant-{i}" for i in range(9000)]
    before_cells = [f"c{i}" for i in range(8)]
    after_cells = [*before_cells, "c8"]
    moved = [
        t
        for t in tenants
        if cell_for_rendezvous(t, before_cells) != cell_for_rendezvous(t, after_cells)
    ]
    assert moved
    assert all(cell_for_rendezvous(t, after_cells) == "c8" for t in moved)
    assert 0.08 <= len(moved) / len(tenants) <= 0.14  # about 1/9


@pytest.mark.unit
def test_modulo_hashing_remaps_most_tenants_when_a_cell_is_added() -> None:
    tenants = [f"tenant-{i}" for i in range(9000)]
    fraction = moved_fraction(
        tenants, lambda t: str(cell_for_mod(t, 8)), lambda t: str(cell_for_mod(t, 9))
    )
    assert fraction > 0.8  # about 8/9


@pytest.mark.unit
def test_staged_apply_halts_after_the_first_unhealthy_wave() -> None:
    cells = [f"cell-{i}" for i in range(16)]
    applied: list[str] = []

    def run(bad: str) -> tuple[tuple[str, ...], bool]:
        applied.clear()
        result = staged_apply(cells, (1, 3, 12), applied.append, lambda c: c != bad)
        return result.applied, result.halted

    assert run("cell-0") == (("cell-0",), True)  # 1 of 16 cells touched
    assert run("cell-2") == (("cell-0", "cell-1", "cell-2", "cell-3"), True)  # wave 2 is 4 cells
    everything, halted = run("none")
    assert len(everything) == 16
    assert not halted


@pytest.mark.unit
def test_staged_apply_rejects_waves_that_do_not_cover_every_cell() -> None:
    with pytest.raises(ValueError, match="cover every cell"):
        staged_apply(["a", "b", "c"], (1, 1), lambda _c: None, lambda _c: True)
    with pytest.raises(ValueError, match="cover every cell"):
        staged_apply(["a"], (0, 1), lambda _c: None, lambda _c: True)


@pytest.mark.unit
def test_globally_replicated_data_defeats_cell_by_cell_code_rollout() -> None:
    waves = (1, 3, 12)
    assert crashed_cells_after_bad_data_push(16, waves, data_waves=None) == 16
    assert crashed_cells_after_bad_data_push(16, waves, data_waves=waves) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cell_router_returns_503_with_retry_after_only_for_the_dead_cell() -> None:
    app = CellRouterApp(8, dead_cells=frozenset({3}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        statuses: dict[str, int] = {}
        for tenant in TENANTS_1000[:400]:
            response = await client.get("/orders", headers={"x-tenant-id": tenant})
            assert response.status_code == (503 if response.headers["x-cell"] == "3" else 200)
            if response.status_code == 503:
                assert response.headers["retry-after"] == "30"
            statuses[tenant] = response.status_code
        again = await client.get("/orders", headers={"x-tenant-id": TENANTS_1000[0]})
        assert again.status_code == statuses[TENANTS_1000[0]]  # routing is sticky
        missing = await client.get("/orders")
    assert missing.status_code == 400
    down = sum(1 for s in statuses.values() if s == 503)
    assert 0 < down < 100  # about 1/8 of 400 tenants
