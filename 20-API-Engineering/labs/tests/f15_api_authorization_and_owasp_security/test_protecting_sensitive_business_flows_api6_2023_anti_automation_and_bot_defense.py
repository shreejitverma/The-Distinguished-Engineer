"""Lab for API6:2023 Unrestricted Access to Sensitive Business Flows."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from slowapi.util import get_ipaddr, get_remote_address
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_labs.f15_api_authorization_and_owasp_security.sensitive_business_flows import (
    CROCKFORD,
    CouponBook,
    Decision,
    FixedWindowCounter,
    HoldError,
    InventoryLedger,
    ManualClock,
    PowVerdict,
    ProofOfWorkGate,
    PurchaseContext,
    RacyCouponBook,
    Redeem,
    RiskSignals,
    Shop,
    SlidingLogLimiter,
    Tier,
    VelocityPolicy,
    VelocityResult,
    build_app,
    expected_guesses_per_hit,
    generate_code,
    ip_partition,
    risk_score,
    solve,
    tier_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

KEY = b"lab-only-hmac-key-not-a-secret"
T0 = 1_000_000.0


def seq_entropy(label: str) -> Callable[[int], bytes]:
    """Deterministic stand-in for os.urandom: SHA-256 of a label and a counter."""
    calls = 0

    def entropy(n: int) -> bytes:
        nonlocal calls
        calls += 1
        return hashlib.sha256(f"{label}-{calls}".encode()).digest()[:n]

    return entropy


# --- Windowed counters -------------------------------------------------------------------


@pytest.mark.unit
def test_fixed_window_admits_twice_the_limit_across_a_boundary() -> None:
    clock = ManualClock(59.5)
    fixed = FixedWindowCounter(limit=5, window=60, clock=clock)
    log = SlidingLogLimiter(limit=5, window=60, clock=clock)
    first = [(fixed.hit("bot"), log.hit("bot").allowed) for _ in range(5)]
    clock.advance(0.5)  # t = 60.0, a new fixed window
    second = [(fixed.hit("bot"), log.hit("bot")) for _ in range(5)]
    assert first == [(True, True)] * 5
    assert [f for f, _ in second] == [True] * 5  # 10 purchases in 0.5 s
    assert {d.allowed for _, d in second} == {False}
    assert second[0][1].retry_after == 60  # ceil(59.5 + 60 - 60.0)


@pytest.mark.unit
def test_sliding_log_retry_after_counts_down_to_the_oldest_event_expiry() -> None:
    clock = ManualClock(0.0)
    log = SlidingLogLimiter(limit=3, window=60, clock=clock)
    for t in (0.0, 10.0, 20.0):
        clock.now = t
        assert log.hit("acct").allowed
    clock.now = 30.0
    assert log.hit("acct").retry_after == 30
    clock.now = 59.9
    assert not log.peek("acct").allowed
    clock.now = 60.0  # the event at t = 0 leaves the window exactly here
    assert log.hit("acct") == Decision(True, 0, 0)


# --- Velocity dimensions -----------------------------------------------------------------


@pytest.mark.unit
def test_ipv6_rotation_inside_one_64_shares_a_bucket() -> None:
    assert ip_partition("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert ip_partition("2001:db8:1:2:ffff:ffff:ffff:9") == "2001:db8:1:2::/64"
    assert ip_partition("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert ip_partition("203.0.113.77") == "203.0.113.0/24"


def purchase_policy(clock: ManualClock) -> VelocityPolicy:
    return VelocityPolicy({"account": 2, "card": 3, "address": 3, "ip": 50}, 86_400, clock)


@pytest.mark.unit
def test_velocity_catches_49_accounts_sharing_one_card_and_address() -> None:
    clock = ManualClock(T0)
    policy = purchase_policy(clock)
    addresses = ["12 Main St., Apt 4", "12 MAIN ST APT 4", "12 main st apt4"]
    results: list[VelocityResult] = []
    for n in range(49):
        ctx = PurchaseContext(f"acct{n}", "card-A", addresses[n % 3], f"2001:db8::{n:x}")
        results.append(policy.check(ctx, record=True))
        clock.advance(60)
    assert [r.allowed for r in results].count(True) == 3
    assert results[3].violated == ("card", "address")
    assert results[3].retry_after == 86_400 - 3 * 60


@pytest.mark.unit
def test_denied_request_consumes_no_quota_in_other_dimensions() -> None:
    clock = ManualClock(T0)
    policy = purchase_policy(clock)
    first = policy.check(PurchaseContext("a1", "card-X", "1 A St", "198.51.100.1"), record=True)
    assert first.allowed
    for other in ("a2", "a3"):
        policy.check(PurchaseContext(other, "card-X", f"{other} St", "198.51.100.1"), record=True)
    blocked = policy.check(PurchaseContext("a1", "card-X", "1 A St", "198.51.100.1"), record=True)
    assert blocked.violated == ("card",)
    second = policy.check(PurchaseContext("a1", "card-Y", "1 A St", "198.51.100.1"), record=True)
    assert second.allowed
    third = policy.check(PurchaseContext("a1", "card-Z", "1 A St", "198.51.100.1"), record=True)
    assert third.violated == ("account",)


# --- Risk tiers --------------------------------------------------------------------------


@pytest.mark.unit
def test_risk_tiers_escalate_on_owasp_non_human_signals() -> None:
    human = RiskSignals(45.0, 2_000.0, 1, 2)
    fast = RiskSignals(0.4, 2_000.0, 1, 2)
    fast_new = RiskSignals(0.4, 3.0, 1, 2)
    farm = RiskSignals(0.4, 3.0, 3, 0)
    scored = [(risk_score(s), tier_for(risk_score(s))) for s in (human, fast, fast_new, farm)]
    assert scored == [
        (0, Tier.ALLOW),
        (40, Tier.PROOF_OF_WORK),
        (60, Tier.STEP_UP),
        (125, Tier.DENY),
    ]
    assert risk_score(RiskSignals(1.0, 2_000.0, 1, 2)) == 0  # exactly 1 s is not "under"


# --- Proof of work -----------------------------------------------------------------------


@pytest.mark.unit
def test_pow_solution_verifies_once_and_then_is_a_replay() -> None:
    gate = ProofOfWorkGate(KEY, ManualClock(T0), seq_entropy("pow"))
    challenge = gate.issue("checkout", "alice", 12)
    counter = solve(challenge, 12)
    assert counter == 83
    assert gate.verify(challenge, counter, flow="checkout", subject="alice") is PowVerdict.OK
    assert gate.verify(challenge, counter, flow="checkout", subject="alice") is PowVerdict.REPLAYED
    assert gate.spent_entries == 1


@pytest.mark.unit
def test_pow_rejects_wrong_binding_tampering_short_work_and_expiry() -> None:
    clock = ManualClock(T0)
    gate = ProofOfWorkGate(KEY, clock, seq_entropy("pow"), ttl=120)
    challenge = gate.issue("checkout", "alice", 12)
    assert gate.verify(challenge, 83, flow="checkout", subject="bob") is PowVerdict.WRONG_BINDING
    assert gate.verify(challenge, 83, flow="coupon", subject="alice") is PowVerdict.WRONG_BINDING
    assert gate.verify(challenge, 82, flow="checkout", subject="alice") is (
        PowVerdict.INSUFFICIENT_WORK
    )
    payload_b64, mac_b64 = challenge.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    claims["b"] = 0
    forged_payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    forged = base64.urlsafe_b64encode(forged_payload).decode().rstrip("=") + "." + mac_b64
    assert gate.verify(forged, 0, flow="checkout", subject="alice") is PowVerdict.BAD_MAC
    assert gate.verify("not-a-challenge", 0, flow="checkout", subject="alice") is (
        PowVerdict.MALFORMED
    )
    clock.advance(120)
    assert gate.verify(challenge, 83, flow="checkout", subject="alice") is PowVerdict.EXPIRED


@pytest.mark.unit
def test_pow_mean_work_tracks_two_to_the_bits() -> None:
    means: dict[int, float] = {}
    for bits in (8, 10, 12):
        gate = ProofOfWorkGate(KEY, ManualClock(T0), seq_entropy(f"mean{bits}"))
        attempts = [solve(gate.issue("checkout", "alice", bits), bits) + 1 for _ in range(64)]
        means[bits] = sum(attempts) / len(attempts)
    assert means == {8: 237.4375, 10: 1003.546875, 12: 4030.421875}
    assert all(0.9 < mean / 2**bits < 1.1 for bits, mean in means.items())


@pytest.mark.unit
def test_pow_spent_set_is_pruned_after_challenge_expiry() -> None:
    clock = ManualClock(T0)
    gate = ProofOfWorkGate(KEY, clock, seq_entropy("prune"), ttl=120)
    for _ in range(3):
        c = gate.issue("checkout", "alice", 4)
        assert gate.verify(c, solve(c, 4), flow="checkout", subject="alice") is PowVerdict.OK
    assert gate.spent_entries == 3
    clock.advance(120)
    c = gate.issue("checkout", "alice", 4)
    assert gate.verify(c, solve(c, 4), flow="checkout", subject="alice") is PowVerdict.OK
    assert gate.spent_entries == 1


# --- Inventory holds ---------------------------------------------------------------------


def seat_ledger(clock: ManualClock) -> InventoryLedger:
    return InventoryLedger(
        {"seat": 100},
        hold_ttl=600,
        per_account_limit=6,
        max_holds_per_account=2,
        clock=clock,
    )


@pytest.mark.unit
def test_one_account_is_capped_at_the_purchase_limit_across_holds_and_orders() -> None:
    clock = ManualClock(T0)
    ledger = seat_ledger(clock)
    first = ledger.hold("bot", "seat", 4)
    assert not isinstance(first, HoldError)
    assert ledger.hold("bot", "seat", 3) is HoldError.ACCOUNT_LIMIT
    assert not isinstance(ledger.confirm(first.hold_id, "bot"), HoldError)
    assert ledger.hold("bot", "seat", 3) is HoldError.ACCOUNT_LIMIT
    assert not isinstance(ledger.hold("bot", "seat", 2), HoldError)
    assert ledger.snapshot("seat") == (94, 2, 4)


@pytest.mark.unit
def test_seventeen_accounts_deny_inventory_until_the_ttl_returns_it() -> None:
    clock = ManualClock(T0)
    ledger = seat_ledger(clock)
    for n in range(16):
        assert not isinstance(ledger.hold(f"bot{n}", "seat", 6), HoldError)
    assert not isinstance(ledger.hold("bot16", "seat", 4), HoldError)
    assert ledger.snapshot("seat") == (0, 100, 0)
    clock.advance(599.9)
    assert ledger.hold("fan", "seat", 2) is HoldError.SOLD_OUT
    clock.advance(0.1)  # t = T0 + 600: every bot hold expires here
    assert ledger.snapshot("seat") == (100, 0, 0)
    fan = ledger.hold("fan", "seat", 2)
    assert not isinstance(fan, HoldError)
    assert ledger.confirm("h1", "bot0") is HoldError.EXPIRED
    assert ledger.confirm("h999", "bot0") is HoldError.UNKNOWN_HOLD
    assert ledger.confirm(fan.hold_id, "bot0") is HoldError.NOT_OWNER


@pytest.mark.unit
def test_stock_is_conserved_through_holds_orders_and_expiry() -> None:
    clock = ManualClock(T0)
    ledger = seat_ledger(clock)
    for step in range(300):
        account = f"a{step % 40}"
        result = ledger.hold(account, "seat", 1 + step % 3)
        if not isinstance(result, HoldError) and step % 4 == 0:
            ledger.confirm(result.hold_id, account)
        clock.advance(37)
        available, held, sold = ledger.snapshot("seat")
        assert available + held + sold == 100
        assert min(available, held, sold) >= 0
    assert ledger.snapshot("seat")[2] > 0


# --- Coupons -----------------------------------------------------------------------------


@pytest.mark.unit
def test_code_space_decides_how_fast_an_enumerator_finds_value() -> None:
    per_hour = 4_000_000  # GiftGhostBot peak, SecurityWeek 2017-03-27
    six_digits = expected_guesses_per_hit(10, 6, 10_000)
    eight_crockford = expected_guesses_per_hit(32, 8, 10_000)
    assert six_digits == 100.0
    assert per_hour / six_digits == 40_000.0
    assert round(eight_crockford) == 109_951_163
    assert round(eight_crockford / per_hour, 1) == 27.5  # hours per valid code


@pytest.mark.unit
def test_generated_codes_are_uniform_crockford_without_modulo_bias() -> None:
    every_byte = bytes(range(256))
    code = generate_code(lambda n: every_byte[:n], length=256)
    assert {code.count(ch) for ch in CROCKFORD} == {8}
    assert not set("ILOU") & set(code)
    assert generate_code(seq_entropy("code")) == "HX6SYQ0M2A"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_then_act_redeems_a_single_use_code_twice() -> None:
    racy = RacyCouponBook({"WELCOME10"})
    outcomes = await asyncio.gather(racy.redeem("a1", "WELCOME10"), racy.redeem("a2", "WELCOME10"))
    assert list(outcomes) == [Redeem.REDEEMED, Redeem.REDEEMED]
    assert len(racy.redemptions) == 2
    safe = CouponBook(
        {"WELCOME10"}, account_failures=5, campaign_failures=50, window=3600, clock=ManualClock()
    )
    outcomes = await asyncio.gather(safe.redeem("a1", "WELCOME10"), safe.redeem("a2", "WELCOME10"))
    assert list(outcomes) == [Redeem.REDEEMED, Redeem.REJECTED]
    assert safe.redemptions == [("WELCOME10", "a1")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_budgets_stop_one_account_and_a_distributed_enumeration() -> None:
    clock = ManualClock(T0)
    book = CouponBook(
        {"HX6SYQ0M2A"}, account_failures=5, campaign_failures=50, window=3600, clock=clock
    )
    assert [await book.redeem("solo", f"GUESS{n}") for n in range(5)] == [Redeem.REJECTED] * 5
    assert await book.redeem("solo", "HX6SYQ0M2A") is Redeem.THROTTLED
    assert book.throttle("solo").retry_after == 3600
    for n in range(9):
        for g in range(5):
            assert await book.redeem(f"farm{n}", f"F{n}-{g}") is Redeem.REJECTED
    assert await book.redeem("fresh", "HX6SYQ0M2A") is Redeem.THROTTLED  # campaign budget
    clock.advance(3600)
    assert await book.redeem("fresh", "hx6syq0m2a ") is Redeem.REDEEMED
    assert await book.redeem("other", "HX6SYQ0M2A") is Redeem.REJECTED  # used == unknown


# --- HTTP surface ------------------------------------------------------------------------


def make_shop(clock: ManualClock) -> Shop:
    return Shop(
        ledger=seat_ledger(clock),
        hold_rate=SlidingLogLimiter(3, 3600, clock),
        velocity=purchase_policy(clock),
        pow_gate=ProofOfWorkGate(KEY, clock, seq_entropy("http")),
        coupons=CouponBook(
            {"HX6SYQ0M2A"}, account_failures=2, campaign_failures=50, window=3600, clock=clock
        ),
        account_created_at={"alice": 0.0, "mallory": T0 - 3600},
        clock=clock,
    )


def client_for(app: Starlette, host: str = "203.0.113.9") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(host, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_instant_checkout_gets_a_pow_challenge_then_succeeds_once() -> None:
    clock = ManualClock(T0)
    alice = {"x-account": "alice"}
    order = {"card": "card-A", "address": "1 Fan Rd"}
    async with client_for(build_app(make_shop(clock))) as client:
        hold = await client.post("/holds", json={"sku": "seat", "quantity": 2}, headers=alice)
        assert hold.status_code == 201
        assert hold.json() == {"hold_id": "h1", "expires_in": 600}
        clock.advance(0.2)
        first = await client.post("/checkout", json={"hold_id": "h1", **order}, headers=alice)
        assert first.status_code == 403
        assert first.headers["content-type"] == "application/problem+json"
        problem = first.json()
        assert problem["type"].endswith("/proof-of-work-required")
        assert (problem["bits"], problem["reason"]) == (12, "malformed")
        counter = solve(problem["challenge"], problem["bits"])
        solved = {**alice, "pow-solution": f"{problem['challenge']}:{counter}"}
        paid = await client.post("/checkout", json={"hold_id": "h1", **order}, headers=solved)
        assert (paid.status_code, paid.json()) == (200, {"order": "h1", "tier": "proof-of-work"})
        await client.post("/holds", json={"sku": "seat", "quantity": 2}, headers=alice)
        clock.advance(0.2)
        replay = await client.post("/checkout", json={"hold_id": "h2", **order}, headers=solved)
        assert (replay.status_code, replay.json()["reason"]) == (403, "replayed")
        clock.advance(30)
        human = await client.post("/checkout", json={"hold_id": "h2", **order}, headers=alice)
        assert (human.status_code, human.json()["tier"]) == (200, "allow")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_account_instant_checkout_is_sent_to_step_up() -> None:
    clock = ManualClock(T0)
    mallory = {"x-account": "mallory"}
    async with client_for(build_app(make_shop(clock))) as client:
        await client.post("/holds", json={"sku": "seat", "quantity": 1}, headers=mallory)
        clock.advance(0.3)
        body = {"hold_id": "h1", "card": "card-M", "address": "9 Bot Ln"}
        response = await client.post("/checkout", json=body, headers=mallory)
    assert response.status_code == 403
    assert response.json()["type"] == "https://api.example.com/problems/step-up"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hold_spinning_gets_429_with_retry_after_and_ratelimit_fields() -> None:
    clock = ManualClock(T0)
    alice = {"x-account": "alice"}
    async with client_for(build_app(make_shop(clock))) as client:
        responses: list[httpx.Response] = []
        for _ in range(4):
            body = {"sku": "seat", "quantity": 1}
            responses.append(await client.post("/holds", json=body, headers=alice))
            clock.advance(10)
    # The third attempt passes the rate limit but hits max_holds_per_account=2.
    assert [r.status_code for r in responses] == [201, 201, 409, 429]
    last = responses[-1]
    assert last.headers["retry-after"] == "3570"  # ceil(T0 + 3600 - (T0 + 30))
    assert last.headers["ratelimit-policy"] == '"holds";q=3;w=3600'
    assert last.headers["ratelimit"] == '"holds";r=0;t=3570'
    assert last.json()["violated-policies"] == ["holds"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coupon_endpoint_rejects_uniformly_then_throttles() -> None:
    clock = ManualClock(T0)
    eve = {"x-account": "eve"}
    async with client_for(build_app(make_shop(clock))) as client:
        bad = [await client.post("/coupons/redeem", json={"code": c}, headers=eve) for c in "AB"]
        locked = await client.post("/coupons/redeem", json={"code": "HX6SYQ0M2A"}, headers=eve)
    assert {(r.status_code, r.content) for r in bad} == {
        (422, b'{"type":"https://api.example.com/problems/coupon-not-applicable","status":422}')
    }
    assert (locked.status_code, locked.headers["retry-after"]) == (429, "3600")
    assert locked.headers["ratelimit"] == '"coupon-failures";r=0;t=3600'
    assert "ratelimit-policy" not in locked.headers  # no q or w to advertise


# --- Keying on the client address --------------------------------------------------------


def request_with(headers: list[tuple[bytes, bytes]], host: str = "10.0.0.5") -> Request:
    scope = {"type": "http", "headers": headers, "client": (host, 5000)}
    return Request(scope)


@pytest.mark.unit
def test_slowapi_get_ipaddr_trusts_an_underscore_header_and_ignores_the_real_one() -> None:
    spoofed = request_with([(b"x_forwarded_for", b"random-per-request-7f3a")])
    standard = request_with([(b"x-forwarded-for", b"198.51.100.7")])
    assert get_ipaddr(spoofed) == "random-per-request-7f3a"
    assert get_ipaddr(standard) == "10.0.0.5"
    assert get_remote_address(spoofed) == "10.0.0.5"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_proxy_headers_keys_on_the_rightmost_untrusted_hop() -> None:
    async def whoami(request: Request) -> PlainTextResponse:
        return PlainTextResponse(request.client.host if request.client else "")

    # uvicorn and httpx declare incompatible ASGI callable types; the protocol is the same.
    inner: Any = Starlette(routes=[Route("/", whoami)])
    app: Any = ProxyHeadersMiddleware(inner, trusted_hosts="10.0.0.1")
    xff = {"x-forwarded-for": "1.2.3.4, 198.51.100.7"}  # 1.2.3.4 was typed by the client
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as via_proxy:
        assert (await via_proxy.get("/", headers=xff)).text == "198.51.100.7"
    direct = httpx.ASGITransport(app=app, client=("192.0.2.50", 40000))
    async with httpx.AsyncClient(transport=direct, base_url="http://test") as no_proxy:
        assert (await no_proxy.get("/", headers=xff)).text == "192.0.2.50"
