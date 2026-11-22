"""Lab for Envoy outlier detection, ejection backoff, priority load, and panic thresholds."""

from __future__ import annotations

import pytest

from api_labs.f17_api_gateways_proxies_and_edge_architecture.envoy_outlier_detection import (
    NO_HEALTHY_UPSTREAM,
    EjectionKind,
    Host,
    OutlierConfig,
    OutlierDetector,
    PriorityLoadBalancer,
    compute_priority_state,
    route_requests,
    success_rate_ejection_threshold,
)


def make_hosts(count: int, priority: int = 0, prefix: str = "h") -> list[Host]:
    return [Host(f"{prefix}{i}", priority=priority) for i in range(count)]


def fail_active_hc(hosts: list[Host], count: int) -> None:
    for host in hosts[:count]:
        host.failed_active_hc = True


def hammer(detector: OutlierDetector, host: Host, code: int, times: int, now_ms: int = 0) -> None:
    for _ in range(times):
        detector.put_http_response(host, code, now_ms)


@pytest.mark.unit
def test_consecutive_5xx_ejects_exactly_on_the_fifth_error() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    hammer(detector, hosts[0], 500, 4)
    assert hosts[0].healthy
    hammer(detector, hosts[0], 500, 1)
    assert not hosts[0].healthy
    assert detector.stats.enforced[EjectionKind.CONSECUTIVE_5XX] == 1


@pytest.mark.unit
def test_a_non_5xx_response_resets_the_consecutive_counter() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    for _ in range(3):
        hammer(detector, hosts[0], 500, 4)
        hammer(detector, hosts[0], 404, 1)
    assert hosts[0].healthy
    assert detector.stats.detected[EjectionKind.CONSECUTIVE_5XX] == 0


@pytest.mark.unit
def test_gateway_failure_is_detected_but_not_enforced_by_default() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig(consecutive_5xx=0))
    hammer(detector, hosts[0], 503, 5)
    assert detector.stats.detected[EjectionKind.CONSECUTIVE_GATEWAY_FAILURE] == 1
    assert detector.stats.enforced[EjectionKind.CONSECUTIVE_GATEWAY_FAILURE] == 0
    assert hosts[0].healthy


@pytest.mark.unit
def test_gateway_failure_ejects_once_enforcing_percent_is_raised() -> None:
    hosts = make_hosts(10)
    config = OutlierConfig(consecutive_5xx=0, enforcing_consecutive_gateway_failure=100)
    detector = OutlierDetector(hosts, config)
    hammer(detector, hosts[0], 504, 5)
    assert not hosts[0].healthy


@pytest.mark.unit
def test_plain_500_counts_as_5xx_but_resets_the_gateway_counter() -> None:
    hosts = make_hosts(10)
    config = OutlierConfig(consecutive_5xx=100, enforcing_consecutive_gateway_failure=100)
    detector = OutlierDetector(hosts, config)
    hammer(detector, hosts[0], 503, 4)
    hammer(detector, hosts[0], 500, 1)
    assert hosts[0].consecutive_gateway == 0
    assert hosts[0].consecutive_5xx == 5
    hammer(detector, hosts[0], 503, 1)
    assert hosts[0].healthy


@pytest.mark.unit
def test_max_ejection_percent_caps_ejections_and_counts_overflow() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    for host in hosts:
        hammer(detector, host, 500, 5)
    assert detector.ejections_active == 1
    assert detector.stats.ejections_overflow == 9


@pytest.mark.unit
def test_raising_max_ejection_percent_to_100_lets_every_host_be_ejected() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig(max_ejection_percent=100))
    for host in hosts:
        hammer(detector, host, 500, 5)
    assert detector.ejections_active == 10
    assert detector.stats.ejections_overflow == 0


@pytest.mark.unit
def test_always_eject_one_host_overrides_a_zero_percent_cap() -> None:
    capped = OutlierDetector(make_hosts(10), OutlierConfig(max_ejection_percent=0))
    hammer(capped, capped.hosts[0], 500, 5)
    assert capped.ejections_active == 0
    forced = OutlierDetector(
        make_hosts(10), OutlierConfig(max_ejection_percent=0, always_eject_one_host=True)
    )
    for host in forced.hosts:
        hammer(forced, host, 500, 5)
    assert forced.ejections_active == 1


@pytest.mark.unit
def test_default_cap_ejects_nothing_in_a_cluster_of_fewer_than_ten_hosts() -> None:
    small = OutlierDetector(make_hosts(5), OutlierConfig())
    hammer(small, small.hosts[0], 500, 5)
    assert small.ejections_active == 0
    assert small.stats.ejections_overflow == 1
    forced = OutlierDetector(make_hosts(5), OutlierConfig(always_eject_one_host=True))
    for host in forced.hosts:
        hammer(forced, host, 500, 5)
    assert forced.ejections_active == 1


@pytest.mark.unit
def test_enforcing_percent_zero_detects_without_ejecting() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig(enforcing_consecutive_5xx=0))
    hammer(detector, hosts[0], 500, 5)
    assert detector.stats.detected[EjectionKind.CONSECUTIVE_5XX] == 1
    assert detector.stats.enforced[EjectionKind.CONSECUTIVE_5XX] == 0
    assert hosts[0].healthy


@pytest.mark.unit
def test_ejection_time_grows_with_each_consecutive_ejection() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig(interval_ms=10_000))
    host = hosts[0]
    hammer(detector, host, 500, 5, now_ms=0)
    detector.on_interval(20_000)
    assert host.failed_outlier
    detector.on_interval(30_000)
    assert not host.failed_outlier
    hammer(detector, host, 500, 5, now_ms=30_000)
    detector.on_interval(80_000)
    assert host.failed_outlier
    detector.on_interval(90_000)
    assert not host.failed_outlier
    assert host.num_ejections == 2


@pytest.mark.unit
def test_ejection_time_is_capped_by_max_ejection_time() -> None:
    hosts = make_hosts(10)
    config = OutlierConfig(base_ejection_time_ms=30_000, max_ejection_time_ms=45_000)
    detector = OutlierDetector(hosts, config)
    host = hosts[0]
    host.eject_backoff = 5
    hammer(detector, host, 500, 5, now_ms=0)
    detector.on_interval(44_000)
    assert host.failed_outlier
    detector.on_interval(45_000)
    assert not host.failed_outlier


@pytest.mark.unit
def test_backoff_multiplier_drops_one_step_per_clean_interval() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    host = hosts[0]
    hammer(detector, host, 500, 5, now_ms=0)
    detector.on_interval(30_000)
    assert host.eject_backoff == 1
    detector.on_interval(40_000)
    assert host.eject_backoff == 0
    detector.on_interval(50_000)
    assert host.eject_backoff == 0


@pytest.mark.unit
def test_jitter_delays_unejection() -> None:
    hosts = make_hosts(10)
    config = OutlierConfig(max_ejection_time_jitter_ms=5_000)
    detector = OutlierDetector(hosts, config, jitter=lambda maximum: maximum)
    hammer(detector, hosts[0], 500, 5, now_ms=0)
    detector.on_interval(30_000)
    assert hosts[0].failed_outlier
    detector.on_interval(35_000)
    assert not hosts[0].failed_outlier


@pytest.mark.unit
def test_unejection_clears_the_consecutive_counters() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    host = hosts[0]
    hammer(detector, host, 500, 5, now_ms=0)
    hammer(detector, host, 500, 3, now_ms=1)
    assert host.consecutive_5xx == 3
    detector.on_interval(30_000)
    assert host.consecutive_5xx == 0


@pytest.mark.unit
def test_active_health_check_success_unejects_unless_disabled() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    hammer(detector, hosts[0], 500, 5)
    detector.on_active_health_check_success(hosts[0], 1_000)
    assert hosts[0].healthy

    pinned_hosts = make_hosts(10)
    pinned = OutlierDetector(
        pinned_hosts, OutlierConfig(successful_active_health_check_uneject_host=False)
    )
    hammer(pinned, pinned_hosts[0], 500, 5)
    pinned.on_active_health_check_success(pinned_hosts[0], 1_000)
    assert not pinned_hosts[0].healthy


@pytest.mark.unit
def test_active_health_check_does_not_uneject_a_host_that_failed_the_check() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    hammer(detector, hosts[0], 500, 5)
    hosts[0].failed_active_hc = True
    detector.on_active_health_check_success(hosts[0], 1_000)
    assert hosts[0].failed_outlier


@pytest.mark.unit
def test_success_rate_threshold_matches_the_envoy_source_example() -> None:
    mean, threshold = success_rate_ejection_threshold([50, 100, 100, 100, 100], 1900)
    assert mean == pytest.approx(90.0)
    assert threshold == pytest.approx(52.0)


@pytest.mark.unit
def test_success_rate_ejects_only_the_statistical_outlier() -> None:
    hosts = make_hosts(6)
    detector = OutlierDetector(hosts, OutlierConfig(max_ejection_percent=100))
    for host in hosts[:5]:
        for i in range(100):
            detector.put_http_response(host, 200 if i < 99 else 500, 0)
    for i in range(100):
        detector.put_http_response(hosts[5], 500 if i % 5 < 2 else 200, 0)
    detector.on_interval(10_000)
    assert [host.name for host in hosts if host.failed_outlier] == ["h5"]
    assert detector.stats.enforced[EjectionKind.SUCCESS_RATE] == 1


@pytest.mark.unit
def test_success_rate_needs_minimum_hosts_with_enough_volume() -> None:
    hosts = make_hosts(6)
    detector = OutlierDetector(hosts, OutlierConfig(max_ejection_percent=100))
    for host in hosts[:3]:
        for _ in range(100):
            detector.put_http_response(host, 200, 0)
    for i in range(100):
        detector.put_http_response(hosts[3], 500 if i % 5 < 2 else 200, 0)
    for host in hosts[4:]:
        for i in range(10):
            detector.put_http_response(host, 500 if i % 2 else 200, 0)
    detector.on_interval(10_000)
    assert detector.ejections_active == 0
    assert detector.stats.detected[EjectionKind.SUCCESS_RATE] == 0


@pytest.mark.unit
def test_single_priority_keeps_full_load_while_overprovisioning_covers_the_loss() -> None:
    hosts = make_hosts(100)
    fail_active_hc(hosts, 28)
    state = compute_priority_state([hosts])
    assert state.health == (100,)
    assert state.panic == (False,)
    fail_active_hc(hosts, 29)
    assert compute_priority_state([hosts]).health == (99,)


@pytest.mark.unit
def test_panic_flips_exactly_below_the_threshold() -> None:
    at_threshold = make_hosts(10)
    fail_active_hc(at_threshold, 5)
    assert compute_priority_state([at_threshold]).panic == (False,)
    below = make_hosts(10)
    fail_active_hc(below, 6)
    assert compute_priority_state([below]).panic == (True,)


@pytest.mark.unit
def test_sixty_percent_unhealthy_fails_open_to_all_hosts() -> None:
    hosts = make_hosts(10)
    fail_active_hc(hosts, 6)
    balancer = PriorityLoadBalancer([hosts])
    tally = route_requests(balancer, 1000)
    assert dict(tally) == {f"h{i}": 100 for i in range(10)}
    assert balancer.lb_healthy_panic == 1000
    served_by_survivors = sum(tally[host.name] for host in hosts if host.healthy)
    assert served_by_survivors == 400


@pytest.mark.unit
def test_disabling_panic_concentrates_all_traffic_on_the_survivors() -> None:
    hosts = make_hosts(10)
    fail_active_hc(hosts, 6)
    balancer = PriorityLoadBalancer([hosts], panic_threshold=0)
    tally = route_requests(balancer, 1000)
    assert dict(tally) == {f"h{i}": 250 for i in range(6, 10)}
    assert balancer.lb_healthy_panic == 0


@pytest.mark.unit
def test_fail_traffic_on_panic_sends_nothing_upstream() -> None:
    hosts = make_hosts(10)
    fail_active_hc(hosts, 6)
    balancer = PriorityLoadBalancer([hosts], fail_traffic_on_panic=True)
    assert dict(route_requests(balancer, 100)) == {NO_HEALTHY_UPSTREAM: 100}
    assert balancer.lb_healthy_panic == 100


@pytest.mark.unit
def test_all_hosts_unhealthy_with_panic_disabled_is_no_healthy_upstream() -> None:
    hosts = make_hosts(4)
    fail_active_hc(hosts, 4)
    disabled = PriorityLoadBalancer([hosts], panic_threshold=0)
    assert dict(route_requests(disabled, 40)) == {NO_HEALTHY_UPSTREAM: 40}
    default = PriorityLoadBalancer([hosts])
    assert dict(route_requests(default, 40)) == {f"h{i}": 10 for i in range(4)}


@pytest.mark.unit
def test_default_outlier_cap_cannot_push_the_cluster_into_panic() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig())
    for host in hosts:
        hammer(detector, host, 500, 5)
    balancer = PriorityLoadBalancer([hosts])
    assert detector.ejections_active == 1
    assert not balancer.state().panic[0]
    assert "h0" not in route_requests(balancer, 900)


@pytest.mark.unit
def test_uncapped_outlier_ejection_triggers_panic_instead_of_a_blackout() -> None:
    hosts = make_hosts(10)
    detector = OutlierDetector(hosts, OutlierConfig(max_ejection_percent=100))
    for host in hosts:
        hammer(detector, host, 500, 5)
    assert detector.ejections_active == 10
    balancer = PriorityLoadBalancer([hosts])
    assert balancer.state().panic == (True,)
    assert NO_HEALTHY_UPSTREAM not in route_requests(balancer, 100)
    strict = PriorityLoadBalancer([hosts], panic_threshold=0)
    assert dict(route_requests(strict, 100)) == {NO_HEALTHY_UPSTREAM: 100}


@pytest.mark.unit
def test_lower_priority_absorbs_load_so_panic_is_ignored() -> None:
    p0 = make_hosts(100, 0, "a")
    p1 = make_hosts(100, 1, "b")
    fail_active_hc(p0, 75)
    state = compute_priority_state([p0, p1])
    assert state.normalized_total_availability == 100
    assert state.load == (35, 65)
    assert state.panic == (False, False)


@pytest.mark.unit
def test_both_priorities_low_enters_total_panic_split_by_host_count() -> None:
    p0 = make_hosts(100, 0, "a")
    p1 = make_hosts(100, 1, "b")
    fail_active_hc(p0, 75)
    fail_active_hc(p1, 75)
    state = compute_priority_state([p0, p1])
    assert state.normalized_total_availability == 70
    assert state.panic == (True, True)
    assert state.load == (50, 50)


@pytest.mark.unit
def test_total_panic_load_follows_host_counts_not_health() -> None:
    p0 = make_hosts(2, 0, "a")
    p1 = make_hosts(8, 1, "b")
    fail_active_hc(p0, 2)
    fail_active_hc(p1, 8)
    assert compute_priority_state([p0, p1]).load == (20, 80)


@pytest.mark.unit
def test_mixed_panic_applies_per_priority_with_source_rounding() -> None:
    p0 = make_hosts(100, 0, "a")
    p1 = make_hosts(100, 1, "b")
    fail_active_hc(p0, 95)
    fail_active_hc(p1, 35)
    state = compute_priority_state([p0, p1])
    assert state.health == (7, 91)
    assert state.panic == (True, False)
    assert state.load == (8, 92)


@pytest.mark.unit
def test_panic_threshold_of_100_panics_whenever_total_health_is_below_100() -> None:
    hosts = make_hosts(10)
    fail_active_hc(hosts, 3)
    assert compute_priority_state([hosts], panic_threshold=100).panic == (True,)
    assert compute_priority_state([hosts], panic_threshold=60).panic == (False,)
