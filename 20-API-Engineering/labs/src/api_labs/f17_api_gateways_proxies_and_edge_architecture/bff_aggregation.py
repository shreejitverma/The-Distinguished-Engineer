"""BFF fan-out: parallel upstream calls, per-call deadlines, hedging, and partial degradation.

``fan_out`` runs every upstream call as a child of one AnyIO task group, under one overall
budget.
A child never lets an optional upstream's failure escape: it records a degraded ``Outcome`` and
substitutes a deep copy of the policy's fallback.
A critical upstream's failure is raised as ``UpstreamFailure``, which cancels every sibling and
reaches the owner as an exception group that ``except*`` converts to ``AggregationFailed``.
No sockets and no wall-clock sleeps: the "slow upstream" in tests is ``sleep_forever()``.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import anyio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

JsonObject = dict[str, object]
Call = Callable[[int], Awaitable[JsonObject]]
Status = Literal["ok", "timeout", "error"]

DEFAULT_TOTAL_DEADLINE_S = 0.6
_PROBLEM_TITLES = {502: "Bad Gateway", 504: "Gateway Timeout"}


@dataclass(frozen=True, slots=True)
class Policy:
    """How long to wait for one upstream and whether the page can survive without it."""

    deadline_s: float
    critical: bool = False
    fallback: JsonObject | None = None
    hedge_after_s: float | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.deadline_s) or self.deadline_s <= 0:
            raise ValueError("deadline_s must be a finite number greater than 0")
        if self.critical and self.fallback is not None:
            raise ValueError("a critical upstream has no fallback: its failure fails the page")
        if not self.critical and self.fallback is None:
            raise ValueError("an optional upstream needs a fallback")
        hedge = self.hedge_after_s
        if hedge is not None and (not math.isfinite(hedge) or not 0 < hedge < self.deadline_s):
            raise ValueError("hedge_after_s must satisfy 0 < hedge_after_s < deadline_s")


DEFAULT_POLICIES: dict[str, Policy] = {
    "orders": Policy(deadline_s=0.5, critical=True),
    "inventory": Policy(deadline_s=0.25, fallback={"stock": "unknown"}),
    "recommendations": Policy(deadline_s=0.08, fallback={"items": [], "source": "fallback"}),
}


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened to one upstream; ``detail`` is for logs and never sent to clients."""

    name: str
    status: Status
    attempts: int
    detail: str


class UpstreamFailure(Exception):
    """A critical upstream failed; raised by a child so the group cancels its siblings."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(f"{outcome.name}: {outcome.status} ({outcome.detail})")
        self.outcome = outcome


class AggregationFailed(Exception):
    """One or more critical upstreams failed: map to 504 if any timed out, else 502."""

    def __init__(self, failures: tuple[Outcome, ...]) -> None:
        super().__init__(", ".join(f"{f.name}:{f.status}" for f in failures))
        self.failures = failures

    @property
    def status_code(self) -> int:
        return 504 if any(f.status == "timeout" for f in self.failures) else 502


@dataclass(frozen=True, slots=True)
class Aggregate:
    """The merged page data plus a per-upstream outcome table."""

    data: dict[str, JsonObject]
    outcomes: dict[str, Outcome]

    @property
    def degraded(self) -> list[str]:
        return sorted(name for name, o in self.outcomes.items() if o.status != "ok")


@dataclass(slots=True)
class _Attempts:
    """Shared with the hedger so the count survives cancellation of the call."""

    started: int = 1


def _fallback_for(policy: Policy) -> JsonObject:
    if policy.fallback is None:  # pragma: no cover - Policy.__post_init__ forbids this
        raise RuntimeError("optional upstream without a fallback")
    return copy.deepcopy(policy.fallback)  # a shared dict would leak mutations across requests


async def _call_hedged(call: Call, hedge_after_s: float | None, attempts: _Attempts) -> JsonObject:
    """Run ``call(0)``; launch ``call(1)`` after ``hedge_after_s`` or at once if attempt 0 fails.

    The first success wins and cancels the other attempt.
    Hedge only idempotent, side-effect-free reads (RFC 9110 Section 9.2.2).
    """
    if hedge_after_s is None:
        return await call(0)
    winner: list[JsonObject] = []
    errors: list[Exception] = []
    first_failed = anyio.Event()

    async with anyio.create_task_group() as tg:

        async def attempt(index: int) -> None:
            try:
                value = await call(index)
            except Exception as exc:
                errors.append(exc)
                first_failed.set()
                return
            if not winner:
                winner.append(value)
                tg.cancel_scope.cancel()

        tg.start_soon(attempt, 0)
        with anyio.move_on_after(hedge_after_s):
            await first_failed.wait()
        attempts.started = 2
        tg.start_soon(attempt, 1)
    if winner:
        return winner[0]
    raise errors[-1]


async def _run_one(
    name: str,
    call: Call,
    policy: Policy,
    attempts: _Attempts,
    data: dict[str, JsonObject],
    outcomes: dict[str, Outcome],
) -> None:
    value: JsonObject | None = None
    status: Status = "ok"
    detail = ""
    try:
        with anyio.move_on_after(policy.deadline_s) as scope:
            value = await _call_hedged(call, policy.hedge_after_s, attempts)
    except Exception as exc:  # optional upstreams must not cancel their siblings
        status, detail = "error", type(exc).__name__
    else:
        if scope.cancelled_caught:
            status, detail = "timeout", "deadline"
    outcome = Outcome(name, status, attempts.started, detail)
    outcomes[name] = outcome
    if status == "ok" and value is not None:
        data[name] = value
    elif policy.critical:
        raise UpstreamFailure(outcome)
    else:
        data[name] = _fallback_for(policy)


async def fan_out(
    calls: Mapping[str, Call],
    policies: Mapping[str, Policy],
    *,
    total_deadline_s: float,
) -> Aggregate:
    """Call every upstream concurrently; degrade optional ones, fail fast on critical ones."""
    if calls.keys() != policies.keys():
        raise ValueError("calls and policies must name the same upstreams")
    if not math.isfinite(total_deadline_s) or total_deadline_s <= 0:
        raise ValueError("total_deadline_s must be a finite number greater than 0")
    data: dict[str, JsonObject] = {}
    outcomes: dict[str, Outcome] = {}
    attempts = {name: _Attempts() for name in calls}
    failures: tuple[Outcome, ...] = ()
    try:
        with anyio.move_on_after(total_deadline_s) as budget:
            async with anyio.create_task_group() as tg:
                for name, call in calls.items():
                    tg.start_soon(
                        _run_one, name, call, policies[name], attempts[name], data, outcomes
                    )
    except* UpstreamFailure as group:  # `return` is a SyntaxError inside except*
        failures = tuple(
            sorted(
                (e.outcome for e in group.exceptions if isinstance(e, UpstreamFailure)),
                key=lambda o: o.name,
            )
        )
    if not failures and budget.cancelled_caught:
        for name, policy in policies.items():
            if name in outcomes:
                continue
            outcome = Outcome(name, "timeout", attempts[name].started, "budget")
            outcomes[name] = outcome
            if policy.critical:
                failures += (outcome,)
            else:
                data[name] = _fallback_for(policy)
    if failures:
        raise AggregationFailed(failures)
    return Aggregate({n: data[n] for n in policies}, {n: outcomes[n] for n in policies})


def _page(result: Aggregate) -> JSONResponse:
    degraded = result.degraded
    body: JsonObject = {
        **result.data,
        "_meta": {
            "degraded": degraded,
            "outcomes": {
                name: {"status": o.status, "attempts": o.attempts}
                for name, o in result.outcomes.items()
            },
        },
    }
    headers: dict[str, str] = {}
    if degraded:
        headers["X-BFF-Degraded"] = ",".join(degraded)  # non-standard, lab-defined
        headers["Cache-Control"] = "no-store"  # RFC 9111 Section 5.2.2.5
    return JSONResponse(body, headers=headers)


def _problem(exc: AggregationFailed) -> JSONResponse:
    status = exc.status_code
    body: JsonObject = {
        "type": "about:blank",
        "title": _PROBLEM_TITLES[status],
        "status": status,
        "failed": [{"name": f.name, "status": f.status} for f in exc.failures],
    }
    return JSONResponse(
        body,
        status_code=status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
    )


def build_app(
    calls: Mapping[str, Call],
    policies: Mapping[str, Policy] | None = None,
    *,
    total_deadline_s: float = DEFAULT_TOTAL_DEADLINE_S,
) -> FastAPI:
    """A FastAPI BFF whose ``GET /home`` aggregates ``calls`` under ``policies``."""
    active = DEFAULT_POLICIES if policies is None else policies
    app = FastAPI()

    @app.get("/home")
    async def home() -> JSONResponse:
        try:
            result = await fan_out(calls, active, total_deadline_s=total_deadline_s)
        except AggregationFailed as exc:
            return _problem(exc)
        return _page(result)

    return app
