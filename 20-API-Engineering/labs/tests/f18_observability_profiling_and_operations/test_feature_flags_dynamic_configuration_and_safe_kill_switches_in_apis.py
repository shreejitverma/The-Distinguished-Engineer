"""Tests for local flag evaluation: bucketing, kill switches, validation, and stale fallback."""

import json
import time
from typing import Any

import httpx
import pytest

from api_labs.f18_observability_profiling_and_operations.feature_flags_runtime import (
    BUCKETS,
    MAX_DOC_BYTES,
    Context,
    FlagStore,
    Policy,
    bucket_of,
    create_app,
    hash64,
)

TENANTS = [f"tenant-{i}" for i in range(100_000)]
KILL_CREDENTIAL = "kill-key-EXAMPLE-NOT-A-REAL-SECRET"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def flag(key: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": key,
        "salt": key,
        "variants": {"off": False, "on": True},
        "off_variant": "off",
        "default_variant": "off",
    }
    return base | overrides


def rollout(key: str, on_bp: int, **overrides: Any) -> dict[str, Any]:
    splits = [{"variant": "on", "weight": on_bp}, {"variant": "off", "weight": BUCKETS - on_bp}]
    return flag(key, splits=splits, **overrides)


def doc(version: int, *flags: dict[str, Any]) -> str:
    return json.dumps({"version": version, "flags": list(flags)})


def store_with(*flags: dict[str, Any], clock: FakeClock | None = None) -> FlagStore:
    store = FlagStore(Policy(ttl_s=30, max_stale_s=300), clock or FakeClock())
    assert store.load(doc(1, *flags)).accepted
    return store


def on_set(store: FlagStore, key: str) -> set[str]:
    view = store.view()
    return {t for t in TENANTS if view.resolve(key, Context(t), False).value}


@pytest.mark.unit
def test_hash64_matches_rfc_4231_test_case_1() -> None:
    # RFC 4231 Section 4.2: HMAC-SHA-256, key = 20 bytes of 0x0b, data = "Hi There".
    assert hash64("\x0b" * 20, "Hi There") == int("b0344c61d8db3853", 16)


@pytest.mark.unit
def test_bucket_known_answers_are_stable_across_processes_and_runtimes() -> None:
    assert bucket_of("checkout-flow", "tenant-1") == 8373
    assert bucket_of("checkout-flow", "tenant-2") == 7760
    assert bucket_of("payments-v2", "tenant-1") == 5122
    assert bucket_of("checkout-flow", "tenant-\u00e9") == 4130  # UTF-8 bytes, not code points


@pytest.mark.unit
def test_rollout_census_is_within_four_sigma_of_target() -> None:
    store = store_with(rollout("census", 1000))
    count = len(on_set(store, "census"))
    sigma = (len(TENANTS) * 0.1 * 0.9) ** 0.5
    print(f"census: {count} of {len(TENANTS)} at 1000 bp, expected 10000, sigma {sigma:.1f}")
    assert abs(count - 10_000) < 4 * sigma


@pytest.mark.unit
def test_raising_the_percentage_only_adds_tenants() -> None:
    small = on_set(store_with(rollout("grow", 1000)), "grow")
    large = on_set(store_with(rollout("grow", 2000)), "grow")
    print(f"monotonic: {len(small)} -> {len(large)} tenants, lost {len(small - large)}")
    assert small <= large
    assert len(large) > len(small)


@pytest.mark.unit
def test_different_flag_salts_decorrelate_the_cohorts() -> None:
    a = on_set(store_with(rollout("flag-a", 1000)), "flag-a")
    b = on_set(store_with(rollout("flag-b", 1000)), "flag-b")
    both = len(a & b)
    print(f"decorrelation: {len(a)} in a, {len(b)} in b, {both} in both (independent: 1000)")
    assert 700 < both < 1300  # same cohort would be ~10000


@pytest.mark.unit
def test_rotating_the_salt_reshuffles_the_cohort() -> None:
    before = on_set(store_with(rollout("rot", 1000, salt="salt-1")), "rot")
    after = on_set(store_with(rollout("rot", 1000, salt="salt-2")), "rot")
    print(f"salt rotation: kept {len(before & after)} of {len(before)}")
    assert 700 < len(before & after) < 1300


@pytest.mark.unit
def test_bucket_by_tenant_gives_every_user_of_a_tenant_the_same_variant() -> None:
    store = store_with(rollout("sticky", 5000, bucket_by="tenant_id"))
    view = store.view()
    for tenant in TENANTS[:200]:
        results = {
            view.resolve("sticky", Context(f"user-{u}", {"tenant_id": tenant}), False).value
            for u in range(5)
        }
        assert len(results) == 1


@pytest.mark.unit
def test_per_user_bucketing_splits_users_inside_one_tenant() -> None:
    view = store_with(rollout("per-user", 5000)).view()
    results = {view.resolve("per-user", Context(f"user-{u}"), False).value for u in range(50)}
    assert results == {True, False}


@pytest.mark.unit
def test_missing_targeting_key_returns_default_with_error_code() -> None:
    result = store_with(rollout("needs-key", 5000)).evaluate("needs-key", Context(), False)
    assert (result.value, result.reason, result.error_code) == (
        False,
        "ERROR",
        "TARGETING_KEY_MISSING",
    )


@pytest.mark.unit
def test_flag_without_splits_does_not_need_a_targeting_key() -> None:
    result = store_with(flag("static", default_variant="on")).evaluate("static", Context(), False)
    assert (result.value, result.reason, result.error_code) == (True, "STATIC", None)


@pytest.mark.unit
def test_allow_list_beats_the_rollout() -> None:
    store = store_with(rollout("allow", 0, allow={"tenant-1": "on"}))
    assert store.evaluate("allow", Context("tenant-1"), False).reason == "TARGETING_MATCH"
    assert store.evaluate("allow", Context("tenant-1"), False).value is True
    assert store.evaluate("allow", Context("tenant-2"), False).value is False


@pytest.mark.unit
def test_kill_switch_beats_allow_list_and_rollout() -> None:
    store = store_with(rollout("kill", BUCKETS, allow={"tenant-1": "on"}))
    assert store.evaluate("kill", Context("tenant-1"), False).value is True
    store.kill("kill")
    for subject in ("tenant-1", "tenant-2"):
        result = store.evaluate("kill", Context(subject), True)
        assert (result.value, result.reason) == (False, "KILL_SWITCH")


@pytest.mark.unit
def test_kill_survives_a_snapshot_swap_until_revived() -> None:
    store = store_with(rollout("kill", BUCKETS))
    store.kill("kill")
    assert store.load(doc(2, rollout("kill", BUCKETS))).accepted
    assert store.evaluate("kill", Context("t"), True).reason == "KILL_SWITCH"
    store.revive("kill")
    assert store.evaluate("kill", Context("t"), False).value is True


@pytest.mark.unit
def test_disabled_flag_serves_the_off_variant() -> None:
    result = store_with(rollout("dis", BUCKETS, enabled=False)).evaluate("dis", Context("t"), True)
    assert (result.value, result.reason) == (False, "DISABLED")


@pytest.mark.unit
def test_unknown_flag_returns_default_with_flag_not_found() -> None:
    result = store_with(flag("known")).evaluate("missing", Context("t"), "safe")
    assert (result.value, result.error_code) == ("safe", "FLAG_NOT_FOUND")


@pytest.mark.unit
def test_bool_variant_is_not_accepted_for_an_int_default() -> None:
    result = store_with(flag("typed", default_variant="on")).evaluate("typed", Context("t"), 0)
    assert (result.value, result.error_code) == (0, "TYPE_MISMATCH")


@pytest.mark.unit
def test_empty_store_returns_default_with_provider_not_ready() -> None:
    result = FlagStore().evaluate("any", Context("t"), "safe")
    assert (result.value, result.error_code, result.freshness) == (
        "safe",
        "PROVIDER_NOT_READY",
        "empty",
    )


@pytest.mark.unit
def test_snapshot_goes_fresh_then_stale_then_expired_on_the_monotonic_clock() -> None:
    clock = FakeClock()
    store = store_with(flag("f", default_variant="on"), clock=clock)
    seen: list[tuple[float, str, bool, str | None]] = []
    for age in (0, 30, 31, 300, 301):
        clock.now = 1000.0 + age
        r = store.evaluate("f", Context("t"), False)
        seen.append((age, r.freshness, r.value, r.error_code))
    print(f"freshness ladder: {seen}")
    assert seen == [
        (0, "fresh", True, None),
        (30, "fresh", True, None),
        (31, "stale", True, None),
        (300, "stale", True, None),
        (301, "expired", False, "GENERAL"),
    ]


@pytest.mark.unit
def test_kill_switch_applies_while_the_snapshot_is_stale() -> None:
    clock = FakeClock()
    store = store_with(rollout("k", BUCKETS), clock=clock)
    clock.now += 100
    store.kill("k")
    result = store.evaluate("k", Context("t"), True)
    assert (result.value, result.reason, result.freshness) == (False, "KILL_SWITCH", "stale")


@pytest.mark.unit
def test_touch_confirms_the_version_and_resets_freshness() -> None:
    clock = FakeClock()
    store = store_with(flag("f"), clock=clock)
    clock.now += 100
    assert store.view().freshness == "stale"
    store.touch()
    assert store.view().freshness == "fresh"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        (doc(2, flag("bad", splits=[{"variant": "on", "weight": 4000}])), "invalid"),
        (doc(2, flag("bad", off_variant="nope")), "invalid"),
        (doc(2, flag("dup"), flag("dup")), "duplicate flag key"),
        ("{not json", "invalid"),
        (json.dumps({"version": 2, "flags": [], "extra": 1}), "invalid"),
        ("x" * (MAX_DOC_BYTES + 1), "document too large"),
    ],
)
def test_bad_documents_never_replace_the_last_known_good(raw: str, detail: str) -> None:
    store = store_with(flag("good", default_variant="on"))
    result = store.load(raw)
    assert not result.accepted
    assert detail in result.detail
    assert store.view().version == 1
    assert store.evaluate("good", Context("t"), False).value is True


@pytest.mark.unit
def test_version_must_increase_so_a_rollback_is_a_new_version() -> None:
    store = store_with(flag("f"))
    assert store.load(doc(1, flag("f", default_variant="on"))).detail == "version not newer"
    assert store.load(doc(0, flag("f"))).accepted is False
    assert store.load(doc(2, flag("f", default_variant="on"))).accepted
    assert store.evaluate("f", Context("t"), False).value is True


@pytest.mark.unit
def test_shrink_guard_rejects_a_blank_or_truncated_snapshot() -> None:
    store = store_with(flag("a"), flag("b"), flag("c"), flag("d"))
    assert store.load(doc(2)).detail.startswith("shrink guard")
    assert store.load(doc(2, flag("a"))).detail.startswith("shrink guard")
    assert store.load(doc(2, flag("a"), flag("b"))).accepted  # exactly half is allowed


@pytest.mark.unit
def test_policy_rejects_ttl_longer_than_max_stale() -> None:
    with pytest.raises(ValueError, match="ttl_s <= max_stale_s"):
        Policy(ttl_s=60, max_stale_s=30)


@pytest.mark.unit
def test_a_pinned_view_is_unchanged_by_a_later_swap() -> None:
    store = store_with(flag("f", default_variant="off"))
    pinned = store.view()
    assert store.load(doc(2, flag("f", default_variant="on"))).accepted
    assert pinned.resolve("f", Context("t"), False).value is False
    assert pinned.version == 1
    assert store.view().resolve("f", Context("t"), False).value is True
    with pytest.raises(TypeError):
        pinned.snapshot.flags["x"] = None  # type: ignore[index, union-attr]  # pyright: ignore


@pytest.mark.unit
def test_evaluation_cost_budget() -> None:
    store = store_with(rollout("bench", 5000))
    view = store.view()
    ctx = Context("tenant-42")
    n = 20_000
    start = time.perf_counter_ns()
    for _ in range(n):
        view.resolve("bench", ctx, False)
    per_call = (time.perf_counter_ns() - start) / n
    print(f"resolve: {per_call:.0f} ns/call over {n} calls (one run, indicative)")
    assert per_call < 200_000  # 200 us: catches I/O or JSON parsing creeping into the hot path


def make_client(store: FlagStore, swap: Any = None) -> httpx.AsyncClient:
    async def between() -> None:
        if swap is not None:
            swap()

    app = create_app(store, kill_credential=KILL_CREDENTIAL, between_reads=between)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def checkout_doc(version: int, flow: str, pay: str) -> str:
    flow_flag = flag(
        "checkout-flow",
        variants={"legacy": "legacy", "v2": "v2"},
        off_variant="legacy",
        default_variant=flow,
    )
    return doc(version, flow_flag, flag("payments-v2", default_variant=pay))


def v1_and_v2() -> tuple[str, str]:
    return checkout_doc(1, "legacy", "off"), checkout_doc(2, "v2", "on")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pinned_request_reads_one_version_even_if_a_swap_lands_between_reads() -> None:
    store = FlagStore()
    v1, v2 = v1_and_v2()
    assert store.load(v1).accepted
    async with make_client(store, swap=lambda: store.load(v2)) as client:
        body = (await client.get("/checkout")).json()
    print(f"pinned: {body}")
    assert body == {"flow": ["legacy", 1], "pay": [False, 1]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unpinned_request_can_tear_across_a_swap() -> None:
    store = FlagStore()
    v1, v2 = v1_and_v2()
    assert store.load(v1).accepted
    async with make_client(store, swap=lambda: store.load(v2)) as client:
        body = (await client.get("/checkout-unpinned")).json()
    print(f"unpinned: {body}")
    assert body == {"flow": ["legacy", 1], "pay": [True, 2]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kill_route_requires_a_bearer_credential_and_flips_behavior() -> None:
    store = FlagStore()
    assert store.load(doc(1, flag("export-enabled", default_variant="on"))).accepted
    async with make_client(store) as client:
        assert (await client.get("/export")).status_code == 200
        denied = await client.post("/admin/kill/export-enabled")
        bad = {"Authorization": "Bearer x"}
        wrong = await client.post("/admin/kill/export-enabled", headers=bad)
        assert (denied.status_code, wrong.status_code) == (401, 401)
        assert denied.headers["www-authenticate"] == "Bearer"
        assert (await client.get("/export")).status_code == 200
        ok = await client.post(
            "/admin/kill/export-enabled", headers={"Authorization": f"bearer {KILL_CREDENTIAL}"}
        )
        assert ok.status_code == 204
        assert (await client.get("/export")).status_code == 503


@pytest.mark.unit
@pytest.mark.asyncio
async def test_killed_feature_sheds_with_503_and_retry_after_not_404() -> None:
    store = FlagStore()
    assert store.load(doc(1, flag("export-enabled", default_variant="on"))).accepted
    store.kill("export-enabled")
    async with make_client(store) as client:
        response = await client.get("/export")
    retry = response.headers["retry-after"]
    print(f"shed: {response.status_code} Retry-After={retry} {response.json()}")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == {"error": "disabled", "reason": "KILL_SWITCH"}
