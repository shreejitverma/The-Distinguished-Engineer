"""Token-per-minute limiting for LLM APIs: reserve before dispatch, reconcile after the stream.

One Redis/Valkey token bucket per tenant holds integer micro-tokens (1 token = 1_000_000), so
refill stays exact: a bucket of `capacity` tokens refilled at `rate` tokens per minute gains
`elapsed_us * rate // 60` micro-tokens, and every stored value stays an integer below 2**53,
which a Lua 5.1 double represents exactly.

Phase 1 (`reserve`) debits `prompt estimate + choices * max_tokens` in one Lua script and records
a reservation under the request id. Phase 2 (`settle`) deletes the reservation and returns
`reserved - charge` to the bucket, where `charge` is what the request really consumed. Both
phases are single Lua scripts, so each is atomic on the server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

import anyio
import redis.exceptions
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from redis.asyncio import Redis
    from starlette.types import Receive, Scope, Send

MICRO = 1_000_000
US_PER_MINUTE = 60_000_000
DEFAULT_MAX_TOKENS = 256
MAX_ALLOWED_MAX_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class TpmPolicy:
    """A bucket of `capacity` tokens that refills at `refill_per_minute` tokens per minute.

    A provider "TPM limit of L" is `capacity=L, refill_per_minute=L`: a full minute of quota can be
    spent at once, and an empty bucket is full again after one minute.
    """

    capacity: int
    refill_per_minute: int
    reservation_ttl_ms: int = 120_000

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.refill_per_minute < 1 or self.reservation_ttl_ms < 1:
            raise ValueError("capacity, refill_per_minute, and reservation_ttl_ms must be >= 1")


@dataclass(frozen=True, slots=True)
class Usage:
    """Provider-reported usage; cached prompt tokens are billed to the bucket at zero."""

    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.prompt_tokens, self.completion_tokens, self.cached_prompt_tokens) < 0:
            raise ValueError("token counts must be >= 0")
        if self.cached_prompt_tokens > self.prompt_tokens:
            raise ValueError("cached_prompt_tokens cannot exceed prompt_tokens")

    @property
    def billable(self) -> int:
        return self.prompt_tokens - self.cached_prompt_tokens + self.completion_tokens


def estimate_prompt_tokens(prompt: str) -> int:
    """Lab heuristic: one token per 4 UTF-8 bytes, rounded up. A real gateway uses the tokenizer."""
    return -(-len(prompt.encode()) // 4)


def reservation_amount(prompt_tokens: int, max_tokens: int, choices: int = 1) -> int:
    """The worst case a request can consume: the whole prompt plus every choice at `max_tokens`."""
    if prompt_tokens < 0 or max_tokens < 1 or choices < 1:
        raise ValueError("prompt_tokens >= 0, max_tokens >= 1, choices >= 1 required")
    return prompt_tokens + choices * max_tokens


def bucket_key(tenant: str) -> str:
    """The `{tenant}` hash tag keeps the bucket and its reservations in one Cluster slot."""
    return f"tpm:{{{tenant}}}:bucket"


def reservation_key(tenant: str, request_id: str) -> str:
    return f"tpm:{{{tenant}}}:resv:{request_id}"


# Shared prelude. A missing bucket is a full bucket. `ts` never moves backwards, so a clock step
# back cannot make the same elapsed time refill twice. Timestamps are microseconds.
_LUA_PRELUDE = """
local now
if ARGV[1] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000000 + tonumber(t[2])
else
  now = tonumber(ARGV[1])
end
local rate = tonumber(ARGV[2])
local cap = tonumber(ARGV[3]) * 1000000
local micro = cap
local ts = now
local stored = redis.call('HMGET', KEYS[1], 'micro', 'ts')
if stored[1] then
  micro = tonumber(stored[1])
  ts = tonumber(stored[2])
  local elapsed = now - ts
  if elapsed > 0 then
    micro = math.min(cap, micro + math.floor(elapsed * rate / 60))
  end
  ts = math.max(ts, now)
end
local function save()
  if micro >= cap then
    redis.call('DEL', KEYS[1])
  else
    local ms = math.ceil((cap - micro) * 60 / rate / 1000) + 1000
    redis.call('HSET', KEYS[1], 'micro', micro, 'ts', ts)
    redis.call('PEXPIRE', KEYS[1], ms)
  end
end
"""

# KEYS: bucket, reservation. ARGV: now_us or '', refill/min, capacity, amount, ttl_ms.
# Reply: {status, balance_tokens, retry_after_us}; status 1 admitted, 2 replay of an existing
# reservation (no second debit), 0 denied (retry_after_us says when), -1 can never fit.
RESERVE_LUA = (
    _LUA_PRELUDE
    + """
local amount = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])
if redis.call('EXISTS', KEYS[2]) == 1 then
  return {2, math.floor(micro / 1000000), 0}
end
local need = amount * 1000000
if need > cap then
  return {-1, math.floor(micro / 1000000), 0}
end
if micro < need then
  return {0, math.floor(micro / 1000000), math.ceil((need - micro) * 60 / rate)}
end
micro = micro - need
save()
redis.call('HSET', KEYS[2], 'amount', amount, 'deadline', now + ttl_ms * 1000)
redis.call('PEXPIRE', KEYS[2], ttl_ms)
return {1, math.floor(micro / 1000000), 0}
"""
)

# KEYS: bucket, reservation. ARGV: now_us or '', refill/min, capacity, charge.
# Reply: {status, delta_tokens, balance_tokens}; status 1 settled (delta = reserved - charge, may
# be negative), 0 no such reservation (unknown or already settled), 3 reservation expired before
# the settle arrived: it stays fully charged and nothing is refunded.
SETTLE_LUA = (
    _LUA_PRELUDE
    + """
local charge = tonumber(ARGV[4])
local rec = redis.call('HMGET', KEYS[2], 'amount', 'deadline')
if not rec[1] then
  return {0, 0, math.floor(micro / 1000000)}
end
redis.call('DEL', KEYS[2])
if now > tonumber(rec[2]) then
  return {3, 0, math.floor(micro / 1000000)}
end
local delta = tonumber(rec[1]) - charge
micro = math.max(-cap, math.min(cap, micro + delta * 1000000))
save()
return {1, delta, math.floor(micro / 1000000)}
"""
)


class Verdict(StrEnum):
    ADMITTED = "admitted"
    DENIED = "denied"
    TOO_LARGE = "too_large"
    BYPASSED = "bypassed"


class SettleStatus(StrEnum):
    SETTLED = "settled"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Admission:
    """Result of phase 1. `retry_after_us` is None when waiting cannot help."""

    verdict: Verdict
    tenant: str
    request_id: str
    reserved: int
    balance: int
    retry_after_us: int | None

    @property
    def admitted(self) -> bool:
        return self.verdict in (Verdict.ADMITTED, Verdict.BYPASSED)


@dataclass(frozen=True, slots=True)
class Settlement:
    """Result of phase 2: `delta` > 0 refunded, < 0 charged beyond the reservation."""

    status: SettleStatus
    delta: int
    balance: int


class TpmLimiter:
    """Two-phase token limiter over one Redis/Valkey bucket per tenant.

    `clock=None` reads the server clock with `TIME` inside the script, so every app server agrees
    on "now"; pass a clock only for deterministic tests. `fail_open` chooses what an unreachable
    store means at reserve time; a failed settle never raises, it reports `UNAVAILABLE`.
    """

    def __init__(
        self,
        client: Redis,
        policy: TpmPolicy,
        *,
        clock: Callable[[], int] | None = None,
        fail_open: bool = False,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._fail_open = fail_open
        self._reserve = client.register_script(RESERVE_LUA)
        self._settle = client.register_script(SETTLE_LUA)

    @property
    def policy(self) -> TpmPolicy:
        return self._policy

    def _now(self) -> int | str:
        return "" if self._clock is None else self._clock()

    async def reserve(self, tenant: str, request_id: str, amount: int) -> Admission:
        if amount < 1:
            raise ValueError("amount must be >= 1")
        p = self._policy
        try:
            reply = await self._reserve(
                keys=[bucket_key(tenant), reservation_key(tenant, request_id)],
                args=[self._now(), p.refill_per_minute, p.capacity, amount, p.reservation_ttl_ms],
            )
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
            verdict = Verdict.BYPASSED if self._fail_open else Verdict.DENIED
            return Admission(verdict, tenant, request_id, 0, 0, None)
        status, balance, retry_us = cast("list[int]", reply)
        if status in (1, 2):
            return Admission(Verdict.ADMITTED, tenant, request_id, amount, balance, 0)
        if status == -1:
            return Admission(Verdict.TOO_LARGE, tenant, request_id, amount, balance, None)
        return Admission(Verdict.DENIED, tenant, request_id, amount, balance, retry_us)

    async def settle(self, admission: Admission, charge: int) -> Settlement:
        if charge < 0:
            raise ValueError("charge must be >= 0")
        if admission.verdict is not Verdict.ADMITTED:
            return Settlement(SettleStatus.UNKNOWN, 0, 0)
        p = self._policy
        try:
            reply = await self._settle(
                keys=[
                    bucket_key(admission.tenant),
                    reservation_key(admission.tenant, admission.request_id),
                ],
                args=[self._now(), p.refill_per_minute, p.capacity, charge],
            )
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
            return Settlement(SettleStatus.UNAVAILABLE, 0, 0)
        status, delta, balance = cast("list[int]", reply)
        by_code = {1: SettleStatus.SETTLED, 0: SettleStatus.UNKNOWN, 3: SettleStatus.EXPIRED}
        return Settlement(by_code[status], delta, balance)


@dataclass(frozen=True, slots=True)
class UpstreamChunk:
    """One upstream event: a content delta of `tokens` tokens, or the final `usage` chunk."""

    tokens: int = 0
    usage: Usage | None = None


class UpstreamError(Exception):
    """The upstream model failed mid-stream."""


@dataclass(slots=True)
class StreamMeter:
    """Counts what the stream really produced; owned by the request, not by the generator.

    Charge hierarchy: the provider's final usage is authoritative; without it (client gone, upstream
    died before the usage chunk) charge the prompt estimate plus the tokens observed so far.
    """

    prompt_estimate: int
    observed_tokens: int = 0
    usage: Usage | None = field(default=None)

    def observe(self, chunk: UpstreamChunk) -> None:
        self.observed_tokens += chunk.tokens
        if chunk.usage is not None:
            self.usage = chunk.usage

    @property
    def charge(self) -> int:
        if self.usage is not None:
            return self.usage.billable
        return self.prompt_estimate + self.observed_tokens

    async def frames(self, upstream: AsyncIterator[UpstreamChunk]) -> AsyncIterator[bytes]:
        async for chunk in upstream:
            self.observe(chunk)
            body: dict[str, object]
            if chunk.usage is not None:
                body = {
                    "usage": {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }
                }
            else:
                body = {"delta": "tok", "tokens": chunk.tokens}
            yield b"data: " + json.dumps(body).encode() + b"\n\n"
        yield b"data: [DONE]\n\n"


def scripted_upstream(
    completion_tokens: int,
    *,
    prompt_tokens: int | None = None,
    cached_prompt_tokens: int = 0,
    fail_after: int | None = None,
    send_usage: bool = True,
) -> Callable[[int, int], AsyncIterator[UpstreamChunk]]:
    """A deterministic fake model: one token per chunk, a checkpoint between chunks."""

    async def stream(prompt_estimate: int, max_tokens: int) -> AsyncIterator[UpstreamChunk]:
        produced = min(completion_tokens, max_tokens)
        for index in range(produced):
            if fail_after is not None and index == fail_after:
                raise UpstreamError("model crashed")
            await anyio.lowlevel.checkpoint()
            yield UpstreamChunk(tokens=1)
        if send_usage:
            actual_prompt = prompt_estimate if prompt_tokens is None else prompt_tokens
            yield UpstreamChunk(usage=Usage(actual_prompt, produced, cached_prompt_tokens))

    return stream


class CompletionsApp:
    """Pure ASGI endpoint: reserve, stream, and settle in `finally` under a shielded scope.

    The settle lives in this frame, not in the body generator: a generator's `finally` runs only if
    the consumer resumes or closes it, which a disconnect or a failed `send` does not guarantee.
    """

    def __init__(
        self,
        limiter: TpmLimiter,
        upstream: Callable[[int, int], AsyncIterator[UpstreamChunk]],
    ) -> None:
        self._limiter = limiter
        self._upstream = upstream
        self.settlements: list[Settlement] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        tenant = request.headers.get("x-tenant", "anonymous")
        request_id = request.headers.get("x-request-id", "req-0")
        body = cast("dict[str, Any]", json.loads(await request.body()))
        prompt_estimate = estimate_prompt_tokens(str(body.get("prompt", "")))
        max_tokens = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
        if not 1 <= max_tokens <= MAX_ALLOWED_MAX_TOKENS:
            bad = JSONResponse({"error": "max_tokens out of range"}, status_code=400)
            await bad(scope, receive, send)
            return
        choices = int(body.get("n", 1))
        amount = reservation_amount(prompt_estimate, max_tokens, choices)
        admission = await self._limiter.reserve(tenant, request_id, amount)
        limit = self._limiter.policy.capacity
        if admission.verdict is Verdict.TOO_LARGE:
            err = JSONResponse({"error": "request exceeds bucket capacity"}, status_code=400)
            await err(scope, receive, send)
            return
        if not admission.admitted:
            headers = {"x-ratelimit-limit-tokens": str(limit)}
            if admission.retry_after_us is not None:
                headers["retry-after"] = str(max(1, -(-admission.retry_after_us // MICRO)))
            denied = JSONResponse({"error": "rate limited"}, status_code=429, headers=headers)
            await denied(scope, receive, send)
            return
        meter = StreamMeter(prompt_estimate)
        response = StreamingResponse(
            meter.frames(self._upstream(prompt_estimate, max_tokens * choices)),
            media_type="text/event-stream",
            headers={
                "x-ratelimit-limit-tokens": str(limit),
                "x-ratelimit-remaining-tokens": str(admission.balance),
            },
        )
        try:
            await response(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                self.settlements.append(await self._limiter.settle(admission, meter.charge))
