"""API6:2023 lab: behavioral velocity limits, proof-of-work step-up, holds, and coupons.

Every component takes an injected clock, so tests drive time and nothing here sleeps,
touches the network, or reads the wall clock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import heapq
import hmac
import ipaddress
import json
import math
import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

type Clock = Callable[[], float]
type Entropy = Callable[[int], bytes]


class ManualClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- Windowed counters -------------------------------------------------------------------


class FixedWindowCounter:
    """Clock-aligned fixed window: one counter per key per floor(now / window)."""

    def __init__(self, limit: int, window: float, clock: Clock) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._counts: dict[str, tuple[int, int]] = {}

    def hit(self, key: str) -> bool:
        index = math.floor(self._clock() / self._window)
        current_index, count = self._counts.get(key, (index, 0))
        if current_index != index:
            count = 0
        if count >= self._limit:
            return False
        self._counts[key] = (index, count + 1)
        return True


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


class SlidingLogLimiter:
    """Exact sliding window: a deque of admitted timestamps per key."""

    def __init__(self, limit: int, window: float, clock: Clock) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def _log(self, key: str, now: float) -> deque[float]:
        log = self._events.setdefault(key, deque())
        while log and log[0] <= now - self.window:
            log.popleft()
        return log

    def peek(self, key: str) -> Decision:
        now = self._clock()
        log = self._log(key, now)
        if len(log) >= self.limit:
            return Decision(False, 0, max(1, math.ceil(log[0] + self.window - now)))
        return Decision(True, self.limit - len(log), 0)

    def hit(self, key: str) -> Decision:
        decision = self.peek(key)
        if decision.allowed:
            self._events[key].append(self._clock())
            return Decision(True, decision.remaining - 1, 0)
        return decision


# --- Multi-dimensional velocity ----------------------------------------------------------

_NON_ALNUM: Final = re.compile(r"[^0-9a-z]+")


def normalize_address(raw: str) -> str:
    """Case-fold and drop punctuation and spaces so trivial variants share one key."""
    return _NON_ALNUM.sub("", raw.casefold())


def ip_partition(address: str) -> str:
    """Rate-limit IPv4 per /24 and IPv6 per /64, the smallest block one customer is routed."""
    ip = ipaddress.ip_address(address)
    prefix = 24 if ip.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


@dataclass(frozen=True, slots=True)
class PurchaseContext:
    account: str
    card_fingerprint: str
    shipping_address: str
    client_ip: str


@dataclass(frozen=True, slots=True)
class VelocityResult:
    allowed: bool
    violated: tuple[str, ...]
    remaining: int
    retry_after: int


class VelocityPolicy:
    """One sliding log per dimension; a purchase must fit under every dimension at once."""

    def __init__(self, limits: Mapping[str, int], window: float, clock: Clock) -> None:
        self._limiters = {
            dimension: SlidingLogLimiter(limit, window, clock)
            for dimension, limit in limits.items()
        }

    @staticmethod
    def keys(ctx: PurchaseContext) -> dict[str, str]:
        return {
            "account": ctx.account,
            "card": ctx.card_fingerprint,
            "address": normalize_address(ctx.shipping_address),
            "ip": ip_partition(ctx.client_ip),
        }

    def check(self, ctx: PurchaseContext, *, record: bool) -> VelocityResult:
        keys = self.keys(ctx)
        decisions = {dim: lim.peek(keys[dim]) for dim, lim in self._limiters.items()}
        violated = tuple(dim for dim, d in decisions.items() if not d.allowed)
        if violated:
            retry = max(decisions[dim].retry_after for dim in violated)
            return VelocityResult(False, violated, 0, retry)
        if record:
            for dim, limiter in self._limiters.items():
                limiter.hit(keys[dim])
        spent = 1 if record else 0
        remaining = min(d.remaining for d in decisions.values()) - spent
        return VelocityResult(True, (), remaining, 0)


# --- Risk tiers --------------------------------------------------------------------------


class Tier(StrEnum):
    ALLOW = "allow"
    PROOF_OF_WORK = "proof-of-work"
    STEP_UP = "step-up"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class RiskSignals:
    seconds_since_hold: float
    account_age_hours: float
    accounts_on_card: int
    velocity_remaining: int


def risk_score(s: RiskSignals) -> int:
    points = 0
    if s.seconds_since_hold < 1.0:  # OWASP API6: cart to purchase in under one second
        points += 40
    if s.account_age_hours < 24.0:
        points += 20
    points += 25 * min(max(s.accounts_on_card - 1, 0), 3)
    if s.velocity_remaining == 0:
        points += 15
    return points


def tier_for(points: int) -> Tier:
    if points < 30:
        return Tier.ALLOW
    if points < 60:
        return Tier.PROOF_OF_WORK
    if points < 90:
        return Tier.STEP_UP
    return Tier.DENY


# --- Proof of work (interactive hashcash, server-bound, single use) ----------------------


class PowVerdict(StrEnum):
    OK = "ok"
    MALFORMED = "malformed"
    BAD_MAC = "bad-mac"
    EXPIRED = "expired"
    WRONG_BINDING = "wrong-binding"
    INSUFFICIENT_WORK = "insufficient-work"
    REPLAYED = "replayed"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def work_digest(challenge: str, counter: int) -> bytes:
    return hashlib.sha256(challenge.encode("ascii") + counter.to_bytes(8, "big")).digest()


def leading_zero_bits(digest: bytes) -> int:
    return len(digest) * 8 - int.from_bytes(digest, "big").bit_length()


def solve(challenge: str, bits: int) -> int:
    """Return the first counter whose digest has `bits` leading zero bits (2**bits expected)."""
    counter = 0
    while leading_zero_bits(work_digest(challenge, counter)) < bits:
        counter += 1
    return counter


class ProofOfWorkGate:
    """Stateless HMAC-signed challenges, stateful spent set for double-spend prevention."""

    def __init__(self, key: bytes, clock: Clock, entropy: Entropy, ttl: int = 120) -> None:
        self._key = key
        self._clock = clock
        self._entropy = entropy
        self._ttl = ttl
        self._spent: dict[str, int] = {}

    def issue(self, flow: str, subject: str, bits: int) -> str:
        claims = {
            "b": bits,
            "e": int(self._clock()) + self._ttl,
            "f": flow,
            "n": self._entropy(16).hex(),
            "s": subject,
        }
        payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        mac = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(mac)}"

    def verify(self, challenge: str, counter: int, *, flow: str, subject: str) -> PowVerdict:
        try:
            payload_b64, mac_b64 = challenge.split(".")
            payload, mac = _unb64(payload_b64), _unb64(mac_b64)
        except ValueError:  # binascii.Error is a ValueError subclass
            return PowVerdict.MALFORMED
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return PowVerdict.BAD_MAC
        claims = json.loads(payload)
        now = int(self._clock())
        if now >= claims["e"]:
            return PowVerdict.EXPIRED
        if claims["f"] != flow or claims["s"] != subject:
            return PowVerdict.WRONG_BINDING
        if not 0 <= counter < 2**64:
            return PowVerdict.MALFORMED
        if leading_zero_bits(work_digest(challenge, counter)) < claims["b"]:
            return PowVerdict.INSUFFICIENT_WORK
        self._spent = {n: e for n, e in self._spent.items() if e > now}
        if claims["n"] in self._spent:
            return PowVerdict.REPLAYED
        self._spent[claims["n"]] = claims["e"]
        return PowVerdict.OK

    @property
    def spent_entries(self) -> int:
        return len(self._spent)


# --- Inventory holds with automatic expiry -----------------------------------------------


class HoldError(StrEnum):
    SOLD_OUT = "sold-out"
    ACCOUNT_LIMIT = "account-limit"
    TOO_MANY_HOLDS = "too-many-holds"
    UNKNOWN_HOLD = "unknown-hold"
    EXPIRED = "expired"
    NOT_OWNER = "not-owner"


@dataclass(frozen=True, slots=True)
class Hold:
    hold_id: str
    account: str
    sku: str
    quantity: int
    created_at: float
    expires_at: float


class InventoryLedger:
    """stock = sold + held + available, always; holds expire lazily on every access."""

    def __init__(
        self,
        stock: Mapping[str, int],
        *,
        hold_ttl: float,
        per_account_limit: int,
        max_holds_per_account: int,
        clock: Clock,
    ) -> None:
        self._stock = dict(stock)
        self._sold = dict.fromkeys(stock, 0)
        self._ttl = hold_ttl
        self._per_account = per_account_limit
        self._max_holds = max_holds_per_account
        self._clock = clock
        self._holds: dict[str, Hold] = {}
        self._heap: list[tuple[float, str]] = []
        self._expired: set[str] = set()
        self._purchased: dict[tuple[str, str], int] = {}
        self._seq = 0

    def _expire(self) -> None:
        now = self._clock()
        while self._heap and self._heap[0][0] <= now:
            _, hold_id = heapq.heappop(self._heap)
            if self._holds.pop(hold_id, None) is not None:
                self._expired.add(hold_id)

    def _held(self, sku: str) -> int:
        return sum(h.quantity for h in self._holds.values() if h.sku == sku)

    def snapshot(self, sku: str) -> tuple[int, int, int]:
        """Return (available, held, sold)."""
        self._expire()
        held, sold = self._held(sku), self._sold[sku]
        return self._stock[sku] - held - sold, held, sold

    def get(self, hold_id: str) -> Hold | None:
        self._expire()
        return self._holds.get(hold_id)

    def hold(self, account: str, sku: str, quantity: int) -> Hold | HoldError:
        available, _, _ = self.snapshot(sku)
        mine = [h for h in self._holds.values() if h.account == account]
        committed = self._purchased.get((account, sku), 0) + sum(
            h.quantity for h in mine if h.sku == sku
        )
        if committed + quantity > self._per_account:
            return HoldError.ACCOUNT_LIMIT
        if len(mine) >= self._max_holds:
            return HoldError.TOO_MANY_HOLDS
        if quantity > available:
            return HoldError.SOLD_OUT
        self._seq += 1
        now = self._clock()
        hold = Hold(f"h{self._seq}", account, sku, quantity, now, now + self._ttl)
        self._holds[hold.hold_id] = hold
        heapq.heappush(self._heap, (hold.expires_at, hold.hold_id))
        return hold

    def confirm(self, hold_id: str, account: str) -> Hold | HoldError:
        self._expire()
        hold = self._holds.get(hold_id)
        if hold is None:
            return HoldError.EXPIRED if hold_id in self._expired else HoldError.UNKNOWN_HOLD
        if hold.account != account:
            return HoldError.NOT_OWNER
        del self._holds[hold_id]
        self._sold[hold.sku] += hold.quantity
        key = (account, hold.sku)
        self._purchased[key] = self._purchased.get(key, 0) + hold.quantity
        return hold


# --- Coupon codes ------------------------------------------------------------------------

CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_code(entropy: Entropy, length: int = 10) -> str:
    """Uniform Crockford base32: 256 is a multiple of 32, so `b & 31` has no modulo bias."""
    return "".join(CROCKFORD[b & 31] for b in entropy(length))


def expected_guesses_per_hit(alphabet: int, length: int, valid_codes: int) -> float:
    space: int = alphabet**length
    return space / valid_codes


class Redeem(StrEnum):
    REDEEMED = "redeemed"
    REJECTED = "rejected"
    THROTTLED = "throttled"


class RacyCouponBook:
    """Check, await, then act: the shape of a SELECT followed by a separate UPDATE (CWE-837)."""

    def __init__(self, codes: Iterable[str]) -> None:
        self._unredeemed = set(codes)
        self.redemptions: list[tuple[str, str]] = []

    async def redeem(self, account: str, code: str) -> Redeem:
        if code not in self._unredeemed:
            return Redeem.REJECTED
        await asyncio.sleep(0)  # the database round trip between the check and the write
        self._unredeemed.discard(code)
        self.redemptions.append((code, account))
        return Redeem.REDEEMED


class CouponBook:
    """Atomic single use, one uniform rejection, and failure budgets per account and campaign."""

    def __init__(
        self,
        codes: Iterable[str],
        *,
        account_failures: int,
        campaign_failures: int,
        window: float,
        clock: Clock,
    ) -> None:
        self._unredeemed = set(codes)
        self.redemptions: list[tuple[str, str]] = []
        self._per_account = SlidingLogLimiter(account_failures, window, clock)
        self._campaign = SlidingLogLimiter(campaign_failures, window, clock)

    def throttle(self, account: str) -> Decision:
        mine, everyone = self._per_account.peek(account), self._campaign.peek("*")
        return mine if not mine.allowed else everyone

    async def redeem(self, account: str, code: str) -> Redeem:
        if not self.throttle(account).allowed:
            return Redeem.THROTTLED
        await asyncio.sleep(0)  # the same round trip, but before the atomic step
        normalized = code.strip().upper()
        if normalized in self._unredeemed:  # test-and-set with no await in between
            self._unredeemed.remove(normalized)
            self.redemptions.append((normalized, account))
            return Redeem.REDEEMED
        self._per_account.hit(account)
        self._campaign.hit("*")
        return Redeem.REJECTED


# --- The HTTP surface --------------------------------------------------------------------

QUOTA_EXCEEDED: Final = "https://iana.org/assignments/http-problem-types#quota-exceeded"
PROBLEM_BASE: Final = "https://api.example.com/problems/"
POW_HEADER: Final = "pow-solution"


def problem(
    status: int, kind: str, headers: Mapping[str, str] | None = None, **extra: object
) -> JSONResponse:
    body: dict[str, object] = {"type": kind, "status": status, **extra}
    return JSONResponse(body, status, headers=headers, media_type="application/problem+json")


def throttled(
    policies: Sequence[str], retry_after: int, quota: tuple[int, float] | None = None
) -> JSONResponse:
    """429 with Retry-After, RateLimit (draft-11), and RateLimit-Policy only when q and w exist."""
    headers = {
        "Retry-After": str(retry_after),
        "RateLimit": ", ".join(f'"{p}";r=0;t={retry_after}' for p in policies),
    }
    if quota is not None:
        q, w = quota
        headers["RateLimit-Policy"] = ", ".join(f'"{p}";q={q};w={int(w)}' for p in policies)
    return problem(429, QUOTA_EXCEEDED, headers, **{"violated-policies": list(policies)})


@dataclass(slots=True)
class Shop:
    ledger: InventoryLedger
    hold_rate: SlidingLogLimiter
    velocity: VelocityPolicy
    pow_gate: ProofOfWorkGate
    coupons: CouponBook
    account_created_at: dict[str, float]
    clock: Clock
    pow_bits: int = 12
    card_accounts: dict[str, set[str]] = field(default_factory=dict[str, set[str]])


def build_app(shop: Shop) -> Starlette:
    async def create_hold(request: Request) -> JSONResponse:
        account = request.headers["x-account"]
        body = await request.json()
        rate = shop.hold_rate.hit(account)
        if not rate.allowed:
            limiter = shop.hold_rate
            return throttled(["holds"], rate.retry_after, (limiter.limit, limiter.window))
        result = shop.ledger.hold(account, body["sku"], int(body["quantity"]))
        if isinstance(result, HoldError):
            return problem(409, PROBLEM_BASE + result.value)
        expires_in = int(result.expires_at - shop.clock())
        return JSONResponse({"hold_id": result.hold_id, "expires_in": expires_in}, 201)

    async def checkout(request: Request) -> JSONResponse:
        account = request.headers["x-account"]
        body = await request.json()
        hold = shop.ledger.get(body["hold_id"])
        if hold is None or hold.account != account:
            return problem(409, PROBLEM_BASE + "hold-not-active")
        client_ip = request.client.host if request.client else "0.0.0.0"  # noqa: S104
        ctx = PurchaseContext(account, body["card"], body["address"], client_ip)
        velocity = shop.velocity.check(ctx, record=False)
        if not velocity.allowed:
            return throttled(velocity.violated, velocity.retry_after)
        cards = shop.card_accounts.setdefault(ctx.card_fingerprint, set())
        cards.add(account)
        signals = RiskSignals(
            seconds_since_hold=shop.clock() - hold.created_at,
            account_age_hours=(shop.clock() - shop.account_created_at[account]) / 3600,
            accounts_on_card=len(cards),
            velocity_remaining=velocity.remaining,
        )
        tier = tier_for(risk_score(signals))
        if tier is Tier.PROOF_OF_WORK:
            solution = request.headers.get(POW_HEADER, "")
            challenge, _, counter = solution.rpartition(":")
            verdict = (
                shop.pow_gate.verify(challenge, int(counter), flow="checkout", subject=account)
                if counter.isdigit()
                else PowVerdict.MALFORMED
            )
            if verdict is not PowVerdict.OK:
                fresh = shop.pow_gate.issue("checkout", account, shop.pow_bits)
                return problem(
                    403,
                    PROBLEM_BASE + "proof-of-work-required",
                    challenge=fresh,
                    bits=shop.pow_bits,
                    reason=verdict.value,
                )
        elif tier is not Tier.ALLOW:
            return problem(403, PROBLEM_BASE + tier.value)
        shop.velocity.check(ctx, record=True)
        confirmed = shop.ledger.confirm(hold.hold_id, account)
        if isinstance(confirmed, HoldError):
            return problem(409, PROBLEM_BASE + confirmed.value)
        return JSONResponse({"order": confirmed.hold_id, "tier": tier.value}, 200)

    async def redeem_coupon(request: Request) -> JSONResponse:
        account = request.headers["x-account"]
        body = await request.json()
        outcome = await shop.coupons.redeem(account, str(body["code"]))
        if outcome is Redeem.THROTTLED:
            gate = shop.coupons.throttle(account)
            return throttled(["coupon-failures"], gate.retry_after)
        if outcome is Redeem.REJECTED:
            return problem(422, PROBLEM_BASE + "coupon-not-applicable")
        return JSONResponse({"redeemed": True}, 200)

    return Starlette(
        routes=[
            Route("/holds", create_hold, methods=["POST"]),
            Route("/checkout", checkout, methods=["POST"]),
            Route("/coupons/redeem", redeem_coupon, methods=["POST"]),
        ]
    )
