"""GCRA (ATM Forum TM 4.0 Section 4.4.2): a pure reference and an atomic Redis/Valkey Lua limiter.

All times are integer microseconds since the Unix epoch: 2**53 us is about 285 years, so every
value stays exact in a Lua 5.1 double, which nanoseconds (about 1.8e18 today) would not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import redis.exceptions
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from redis.asyncio import Redis
    from starlette.requests import Request

US_PER_SECOND = 1_000_000
US_PER_MS = 1_000


@dataclass(frozen=True, slots=True)
class GcraPolicy:
    """`rate` requests per `period_us`, at most `burst` of them back to back.

    Emission interval T = period_us // rate (floored, so the effective rate errs high by < 1 us
    per request); tolerance tau = (burst - 1) * T, the TM 4.0 limit L for zero-spacing arrivals.
    """

    rate: int
    period_us: int
    burst: int

    def __post_init__(self) -> None:
        if self.rate < 1 or self.period_us < 1 or self.burst < 1:
            raise ValueError("rate, period_us, and burst must all be >= 1")
        if self.period_us // self.rate < 1:
            raise ValueError("emission interval rounds to 0 us; lower rate or lengthen period")

    @property
    def emission_interval_us(self) -> int:
        return self.period_us // self.rate

    @property
    def tolerance_us(self) -> int:
        return (self.burst - 1) * self.emission_interval_us


@dataclass(frozen=True, slots=True)
class GcraDecision:
    """Outcome of one admission check.

    `retry_after_us` is 0 when allowed, the exact wait when denied, and None when waiting cannot
    help (the cost exceeds the burst, or the store is unavailable and the limiter fails closed).
    """

    allowed: bool
    limit: int
    remaining: int
    retry_after_us: int | None
    reset_after_us: int
    degraded: bool = False


def gcra_virtual_scheduling(
    tat_us: int | None, now_us: int, policy: GcraPolicy, cost: int = 1
) -> tuple[GcraDecision, int | None]:
    """TM 4.0 Figure 4-1, virtual scheduling side, generalized to `cost` cells arriving together.

    Returns the decision and the TAT to store, or None when the state must not change
    (a non-conforming cell leaves TAT unchanged).
    """
    if cost < 1:
        raise ValueError("cost must be >= 1")
    t = policy.emission_interval_us
    tau = policy.tolerance_us
    tat = now_us if tat_us is None else max(tat_us, now_us)
    last_cell_tat = tat + (cost - 1) * t
    if last_cell_tat - now_us <= tau:
        new_tat = tat + cost * t
        remaining = (now_us + tau - new_tat) // t + 1
        return GcraDecision(True, policy.burst, remaining, 0, new_tat - now_us), new_tat
    retry = None if (cost - 1) * t > tau else last_cell_tat - tau - now_us
    remaining = max((now_us + tau - tat) // t + 1, 0)
    return GcraDecision(False, policy.burst, remaining, retry, tat - now_us), None


def gcra_continuous_state(
    bucket_us: int, lct_us: int | None, now_us: int, policy: GcraPolicy
) -> tuple[bool, int, int | None]:
    """TM 4.0 Figure 4-1, continuous-state leaky bucket side: returns (conforming, X, LCT)."""
    increment = policy.emission_interval_us
    limit = policy.tolerance_us
    drained = 0 if lct_us is None else max(bucket_us - (now_us - lct_us), 0)
    if drained > limit:
        return False, bucket_us, lct_us
    return True, drained + increment, now_us


# KEYS[i]: TAT of policy i, integer microseconds since the Unix epoch.
# ARGV[1]: cost; ARGV[2]: now in microseconds, or '' to read the server clock with TIME;
# ARGV[2*i+1], ARGV[2*i+2]: emission interval T and tolerance tau of policy i, in microseconds.
# Reply: {allowed, remaining, retry_after_us (-1 = never), reset_after_us}.
# Every key is read before any key is written: a cell conforms only if it conforms to every
# policy, and only conforming cells update state (TM 4.0 Section 4.4.2).
GCRA_LUA = """
local cost = tonumber(ARGV[1])
local now
if ARGV[2] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000000 + tonumber(t[2])
else
  now = tonumber(ARGV[2])
end
local tats = {}
local allowed = 1
local retry_after = 0
for i = 1, #KEYS do
  local t = tonumber(ARGV[2 * i + 1])
  local tau = tonumber(ARGV[2 * i + 2])
  local stored = redis.call('GET', KEYS[i])
  local tat = now
  if stored then
    tat = math.max(tonumber(stored), now)
  end
  tats[i] = tat
  local last_cell_tat = tat + (cost - 1) * t
  if last_cell_tat - now > tau then
    allowed = 0
    if (cost - 1) * t > tau then
      retry_after = -1
    elseif retry_after ~= -1 then
      retry_after = math.max(retry_after, last_cell_tat - tau - now)
    end
  end
end
local remaining = -1
local reset_after = 0
for i = 1, #KEYS do
  local t = tonumber(ARGV[2 * i + 1])
  local tau = tonumber(ARGV[2 * i + 2])
  local tat = tats[i]
  local left
  if allowed == 1 then
    tat = tat + cost * t
    redis.call('SET', KEYS[i], tat, 'PX', math.ceil((tat - now) / 1000))
    left = math.floor((now + tau - tat) / t) + 1
  else
    left = math.max(math.floor((now + tau - tat) / t) + 1, 0)
  end
  if remaining == -1 or left < remaining then
    remaining = left
  end
  reset_after = math.max(reset_after, tat - now)
end
return {allowed, remaining, retry_after, reset_after}
"""


class RedisGcraLimiter:
    """GCRA over one or more policies, decided atomically by one Lua script per request.

    `clock=None` (the default) reads the server clock with `TIME`, so every app server agrees on
    "now"; pass a clock only for deterministic tests. `fail_open` chooses what an unreachable
    store means; script and argument errors always propagate.
    """

    def __init__(
        self,
        client: Redis,
        *,
        clock: Callable[[], int] | None = None,
        fail_open: bool = False,
    ) -> None:
        self._script = client.register_script(GCRA_LUA)
        self._clock = clock
        self._fail_open = fail_open

    @property
    def script_sha(self) -> str:
        """SHA1 of `GCRA_LUA`, the name `EVALSHA` uses; `SCRIPT LOAD` restores it after a flush."""
        return self._script.sha

    async def acquire(self, key: str, policy: GcraPolicy, cost: int = 1) -> GcraDecision:
        return await self.acquire_all([(key, policy)], cost)

    async def acquire_all(
        self, limits: Sequence[tuple[str, GcraPolicy]], cost: int = 1
    ) -> GcraDecision:
        if not limits:
            raise ValueError("at least one (key, policy) pair is required")
        if cost < 1:
            raise ValueError("cost must be >= 1")
        limit = min(policy.burst for _, policy in limits)
        args: list[int | str] = [cost, "" if self._clock is None else self._clock()]
        for _, policy in limits:
            args += [policy.emission_interval_us, policy.tolerance_us]
        try:
            reply = await self._script(keys=[key for key, _ in limits], args=args)
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
            return GcraDecision(self._fail_open, limit, 0, None, 0, degraded=True)
        allowed, remaining, retry_after, reset_after = cast("list[int]", reply)
        return GcraDecision(
            allowed=allowed == 1,
            limit=limit,
            remaining=remaining,
            retry_after_us=None if retry_after < 0 else retry_after,
            reset_after_us=reset_after,
        )


class NaiveGetSetGcraLimiter:
    """Anti-pattern: GET, decide in Python, SET. The read-modify-write spans two round trips.

    `asyncio.sleep(0)` stands in for the network round trip between the two commands; fakeredis
    answers in-process without yielding, which would hide the race a real network exposes.
    """

    def __init__(self, client: Redis, *, clock: Callable[[], int]) -> None:
        self._client = client
        self._clock = clock

    async def acquire(self, key: str, policy: GcraPolicy) -> GcraDecision:
        raw = cast("bytes | None", await self._client.get(key))
        await asyncio.sleep(0)
        now = self._clock()
        decision, new_tat = gcra_virtual_scheduling(None if raw is None else int(raw), now, policy)
        if new_tat is not None:
            await self._client.set(key, new_tat, px=-(-(new_tat - now) // US_PER_MS))
        return decision


def retry_after_seconds(retry_after_us: int) -> int:
    """`Retry-After` is `1*DIGIT` seconds (RFC 9110 Section 10.2.3): round up, never down to 0."""
    return max(1, -(-retry_after_us // US_PER_SECOND))


def build_app(limiter: RedisGcraLimiter, policy: GcraPolicy) -> Starlette:
    """One route limited per caller: 429 + `Retry-After` when limited, 503 if the store is down."""

    async def quota(request: Request) -> Response:
        caller = request.headers.get("x-client-id", "anonymous")
        decision = await limiter.acquire(f"gcra:{{{caller}}}:api", policy)
        if decision.allowed:
            return JSONResponse({"remaining": decision.remaining})
        if decision.degraded:
            return JSONResponse({"error": "rate limiter unavailable"}, status_code=503)
        headers: dict[str, str] = {}
        if decision.retry_after_us is not None:
            headers["Retry-After"] = str(retry_after_seconds(decision.retry_after_us))
        return JSONResponse({"error": "rate limited"}, status_code=429, headers=headers)

    return Starlette(routes=[Route("/quota", quota)])
