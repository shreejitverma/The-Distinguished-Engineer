"""Lab for the gRPC load balancing note: pick_first vs round_robin, L4 pinning, xDS-era models."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from api_labs.f19_grpc_and_protocol_buffers.grpc_load_balancing import (
    Backoff,
    L4Proxy,
    State,
    address_family,
    aggregate_round_robin,
    build_round_robin_picker,
    call_who,
    choose_lb_policy,
    earliest_resolution_s,
    eventually,
    happy_eyeballs_delay_ms,
    imbalance,
    interleave_by_family,
    ipv4_target,
    lb_service_config,
    make_channel,
    pick_first_target,
    pick_from_lb_config,
    sample,
    start_backends,
    survive_call,
    warm_up,
    wrr_usable_weight,
    wrr_weight,
)

R, C, D, T = State.READY, State.CONNECTING, State.IDLE, State.TRANSIENT_FAILURE
NAMES = ["b0", "b1", "b2"]


# --- unit: round_robin state aggregation and picker --------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([R, T, T], R),
        ([T, C, T], C),
        ([T, D, T], C),  # IDLE counts as CONNECTING in round_robin.cc, never reported as IDLE
        ([T, T, T], T),
        ([], T),  # empty address list reports TRANSIENT_FAILURE
        ([C, R, D], R),
    ],
)
def test_round_robin_aggregates_children_with_first_matching_rule(
    states: list[State], expected: State
) -> None:
    assert aggregate_round_robin(states) is expected


@pytest.mark.unit
@pytest.mark.parametrize("start", [0, 1, 2, 7])
def test_round_robin_picker_cycles_ready_children_in_order_from_its_start_index(
    start: int,
) -> None:
    picker = build_round_robin_picker(NAMES, [R, R, R], start=start)
    assert picker is not None
    picks = [picker.pick() for _ in range(6)]
    expected = [NAMES[(start + i) % 3] for i in range(6)]
    assert picks == expected
    assert Counter(picks) == {"b0": 2, "b1": 2, "b2": 2}


@pytest.mark.unit
def test_round_robin_picker_contains_only_ready_children() -> None:
    picker = build_round_robin_picker(NAMES, [R, T, R], start=0)
    assert picker is not None
    assert [picker.pick() for _ in range(4)] == ["b0", "b2", "b0", "b2"]
    assert build_round_robin_picker(NAMES, [C, T, D], start=0) is None


# --- unit: pick_first ordering ------------------------------------------------------------------
@pytest.mark.unit
def test_pick_first_settles_on_first_reachable_address_in_order() -> None:
    addresses = ["a:1", "b:1", "c:1"]
    assert pick_first_target(addresses, ["c:1", "b:1"]) == "b:1"
    assert pick_first_target(addresses, ["a:1", "b:1", "c:1"]) == "a:1"
    assert pick_first_target(addresses, []) is None


@pytest.mark.unit
def test_pick_first_interleaves_address_families_per_rfc_8305() -> None:
    mixed = ["10.0.0.1:80", "10.0.0.2:80", "[::1]:80", "[::2]:80", "10.0.0.3:80"]
    assert interleave_by_family(mixed) == [
        "10.0.0.1:80",
        "[::1]:80",
        "10.0.0.2:80",
        "[::2]:80",
        "10.0.0.3:80",
    ]
    v6_first = ["[::1]:80", "10.0.0.1:80", "[::2]:80"]
    assert interleave_by_family(v6_first) == ["[::1]:80", "10.0.0.1:80", "[::2]:80"]
    assert address_family("[::1]:80") == "ipv6"
    assert address_family("10.0.0.1:80") == "ipv4"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("configured", "expected"), [(None, 250), (10, 100), (100, 100), (500, 500), (9999, 2000)]
)
def test_happy_eyeballs_delay_defaults_to_250ms_and_clamps_to_100_2000(
    configured: int | None, expected: int
) -> None:
    assert happy_eyeballs_delay_ms(configured) == expected


# --- unit: backoff and re-resolution -------------------------------------------------------------
@pytest.mark.unit
def test_backoff_grows_by_1_6_and_caps_at_120_seconds() -> None:
    backoff = Backoff()
    delays = [backoff.next_delay(0.5) for _ in range(13)]  # 0.5 -> jitter factor exactly 1.0
    assert delays[0] == pytest.approx(1.0)
    assert delays[1] == pytest.approx(1.6)
    assert delays[2] == pytest.approx(2.56)
    assert delays[10] == pytest.approx(1.6**10)  # 109.95 s, still under the cap
    assert delays[11] == 120.0  # 1.6**11 = 175.9 s is capped
    assert delays[12] == 120.0


@pytest.mark.unit
def test_backoff_jitter_stays_within_twenty_percent_and_reset_restarts() -> None:
    low, high = Backoff(), Backoff()
    assert low.next_delay(0.0) == pytest.approx(0.8)
    assert high.next_delay(0.999999) == pytest.approx(1.2, rel=1e-5)
    low.next_delay(0.5)
    low.reset()
    assert low.next_delay(0.5) == pytest.approx(1.0)


@pytest.mark.unit
def test_dns_reresolution_inside_the_cooldown_is_deferred_to_30_seconds_after_the_last() -> None:
    assert earliest_resolution_s(None, 100.0) == 100.0
    assert earliest_resolution_s(100.0, 105.0) == 130.0
    assert earliest_resolution_s(100.0, 131.0) == 131.0
    assert earliest_resolution_s(100.0, 101.0, min_between_s=0.0) == 101.0


# --- unit: policy selection ----------------------------------------------------------------------
@pytest.mark.unit
def test_lb_policy_precedence_is_config_then_deprecated_name_then_channel_arg_then_pick_first() -> (
    None
):
    config: list[dict[str, Any]] = [{"round_robin": {}}]
    assert choose_lb_policy(lb_config=config, deprecated_policy="pick_first") == "round_robin"
    assert choose_lb_policy(deprecated_policy="round_robin", channel_arg="pick_first") == (
        "round_robin"
    )
    assert choose_lb_policy(channel_arg="round_robin") == "round_robin"
    assert choose_lb_policy() == "pick_first"


@pytest.mark.unit
def test_unknown_channel_arg_policy_falls_back_to_pick_first_but_unknown_config_is_an_error() -> (
    None
):
    assert choose_lb_policy(channel_arg="no_such_policy") == "pick_first"
    with pytest.raises(ValueError, match="No known policies in list: future_lb"):
        pick_from_lb_config([{"future_lb": {}}])
    assert pick_from_lb_config([{"future_lb": {}}, {"round_robin": {}}]) == "round_robin"
    with pytest.raises(ValueError, match="oneOf violation"):
        pick_from_lb_config([{"round_robin": {}, "pick_first": {}}])


@pytest.mark.unit
def test_lb_service_config_is_the_documented_json_shape() -> None:
    assert json.loads(lb_service_config("round_robin")) == {
        "loadBalancingConfig": [{"round_robin": {}}]
    }
    cfg = json.loads(lb_service_config("pick_first", {"shuffleAddressList": True}))
    assert cfg["loadBalancingConfig"] == [{"pick_first": {"shuffleAddressList": True}}]


# --- unit: weighted round robin ------------------------------------------------------------------
@pytest.mark.unit
def test_wrr_weight_is_qps_over_utilization_with_an_error_penalty() -> None:
    assert wrr_weight(qps=100.0, eps=0.0, utilization=0.5) == pytest.approx(200.0)
    assert wrr_weight(qps=100.0, eps=10.0, utilization=0.5) == pytest.approx(100.0 / 0.6)
    assert wrr_weight(qps=100.0, eps=10.0, utilization=0.5, error_penalty=0.0) == pytest.approx(
        200.0
    )
    assert wrr_weight(qps=0.0, eps=0.0, utilization=0.5) == 0.0
    assert wrr_weight(qps=100.0, eps=0.0, utilization=0.0) == 0.0


@pytest.mark.unit
def test_wrr_weight_is_unusable_during_blackout_and_after_expiration() -> None:
    common = {"last_update_s": 1000.0, "non_empty_since_s": 1000.0, "weight": 42.0}
    assert wrr_usable_weight(now_s=1005.0, **common) == 0.0  # 5 s into the 10 s blackout
    assert wrr_usable_weight(now_s=1010.0, **common) == 42.0
    assert wrr_usable_weight(now_s=1179.9, **common) == 42.0
    assert wrr_usable_weight(now_s=1180.0, **common) == 0.0  # 180 s since the last report


# --- socket: the real runtime --------------------------------------------------------------------
@pytest.mark.socket
@pytest.mark.asyncio
async def test_pick_first_sends_every_rpc_to_the_first_address() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        async with make_channel(target) as channel:
            tally = await sample(channel, 30)
    assert tally == {"b0": 30}
    assert imbalance(tally, NAMES) == pytest.approx(3.0)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_round_robin_spreads_rpcs_evenly_once_every_subchannel_is_ready() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        options = [("grpc.service_config", lb_service_config("round_robin"))]
        async with make_channel(target, options) as channel:
            await warm_up(channel, set(NAMES))
            tally = await sample(channel, 30)
    assert tally == {"b0": 10, "b1": 10, "b2": 10}
    assert imbalance(tally, NAMES) == pytest.approx(1.0)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_lb_policy_name_channel_arg_selects_round_robin() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        async with make_channel(target, [("grpc.lb_policy_name", "round_robin")]) as channel:
            await warm_up(channel, set(NAMES))
            tally = await sample(channel, 30)
    assert tally == {"b0": 10, "b1": 10, "b2": 10}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_service_config_outranks_the_lb_policy_name_channel_arg() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        options = [
            ("grpc.service_config", lb_service_config("pick_first")),
            ("grpc.lb_policy_name", "round_robin"),
        ]
        async with make_channel(target, options) as channel:
            tally = await sample(channel, 12)
    assert tally == {"b0": 12}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unknown_lb_policy_name_channel_arg_degrades_to_pick_first() -> None:
    async with start_backends(2) as backends:
        target = ipv4_target([b.address for b in backends])
        async with make_channel(target, [("grpc.lb_policy_name", "no_such_policy")]) as channel:
            tally = await sample(channel, 8)
    assert tally == {"b0": 8}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_pick_first_shuffle_address_list_spreads_clients_across_backends() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        plain: Counter[str] = Counter()
        shuffled: Counter[str] = Counter()
        shuffle_cfg = lb_service_config("pick_first", {"shuffleAddressList": True})
        for _ in range(24):
            async with make_channel(target) as channel:
                plain[await call_who(channel, wait_for_ready=True)] += 1
            async with make_channel(target, [("grpc.service_config", shuffle_cfg)]) as channel:
                shuffled[await call_who(channel, wait_for_ready=True)] += 1
    assert plain == {"b0": 24}
    assert len(shuffled) >= 2


@pytest.mark.socket
@pytest.mark.asyncio
async def test_round_robin_drops_a_stopped_backend_and_keeps_balancing_the_rest() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        options = [("grpc.service_config", lb_service_config("round_robin"))]
        async with make_channel(target, options) as channel:
            await warm_up(channel, set(NAMES))
            await backends[1].stop()

            async def only_survivors() -> bool:
                window = [await survive_call(channel) for _ in range(6)]
                return all(name in ("b0", "b2") for name in window)

            await eventually(only_survivors)
            tally = await sample(channel, 20)
    assert tally == {"b0": 10, "b2": 10}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_pick_first_fails_over_to_the_next_address_in_list_order() -> None:
    async with start_backends(3) as backends:
        target = ipv4_target([b.address for b in backends])
        async with make_channel(target) as channel:
            assert await call_who(channel, wait_for_ready=True) == "b0"
            await backends[0].stop()

            async def moved() -> bool:
                return await survive_call(channel) == "b1"

            await eventually(moved)
            tally = await sample(channel, 9)
    assert tally == {"b1": 9}
    assert backends[2].hits == 0


@pytest.mark.socket
@pytest.mark.asyncio
async def test_l4_proxy_pins_one_channel_to_one_backend_whatever_the_policy() -> None:
    async with start_backends(3) as backends:
        proxy = L4Proxy([b.port for b in backends])
        port = await proxy.start()
        try:
            options = [("grpc.service_config", lb_service_config("round_robin"))]
            async with make_channel(f"127.0.0.1:{port}", options) as channel:
                tally = await sample(channel, 30)
        finally:
            await proxy.stop()
    assert proxy.connections_accepted == 1
    assert tally == {"b0": 30}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_l4_proxy_balances_connections_so_many_clients_spread_but_each_stays_pinned() -> None:
    async with start_backends(3) as backends:
        proxy = L4Proxy([b.port for b in backends])
        port = await proxy.start()
        total: Counter[str] = Counter()
        try:
            for _ in range(3):
                async with make_channel(f"127.0.0.1:{port}") as channel:
                    total += await sample(channel, 10)
        finally:
            await proxy.stop()
    assert proxy.assignments == [0, 1, 2]
    assert total == {"b0": 10, "b1": 10, "b2": 10}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_max_connection_age_makes_an_l4_balanced_client_move_between_backends() -> None:
    age_options = [("grpc.max_connection_age_ms", 300), ("grpc.max_connection_age_grace_ms", 100)]
    async with start_backends(3, age_options) as aged:
        proxy = L4Proxy([b.port for b in aged])
        port = await proxy.start()
        try:
            async with make_channel(f"127.0.0.1:{port}") as channel:
                seen: set[str] = set()

                async def moved_around() -> bool:
                    seen.add(await call_who(channel, wait_for_ready=True))
                    return len(seen) >= 2

                await eventually(moved_around, timeout_s=15.0, step_s=0.05)
        finally:
            await proxy.stop()
    assert proxy.connections_accepted >= 2

    async with start_backends(3) as steady:
        proxy = L4Proxy([b.port for b in steady])
        port = await proxy.start()
        try:
            async with make_channel(f"127.0.0.1:{port}") as channel:
                for _ in range(20):
                    await call_who(channel, wait_for_ready=True)
        finally:
            await proxy.stop()
    assert proxy.connections_accepted == 1
