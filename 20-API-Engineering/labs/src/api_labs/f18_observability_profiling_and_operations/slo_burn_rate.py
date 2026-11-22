"""SLIs, error budgets, and multiwindow multi-burn-rate alerting for an HTTP API.

The pieces, in the order a request travels through them:

- ``StatusPolicy`` turns an HTTP status into GOOD, BAD, or EXCLUDED for the availability SLI.
  A ``5xx`` is BAD; a ``4xx`` is the client's error and is EXCLUDED (dropped from numerator and
  denominator) or counted GOOD, depending on the policy.
- ``SliSeries`` stores per-step good/bad counts as prefix sums, which is what ``increase()`` over
  a Prometheus counter gives a ``rate(...[window])`` expression.
- ``BurnRateRule`` is one (long window, short window, burn threshold) row of the Google SRE
  Workbook table; it fires only while BOTH windows burn faster than the threshold.
- ``SliMiddleware`` is a pure ASGI middleware that records one observation per request, including
  the unhandled-exception case a status-only recorder never sees.
"""

from __future__ import annotations

import itertools
import math
import time
from bisect import bisect_left
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

STEP_SECONDS = 60
SECONDS_PER_DAY = 86_400
PERIOD_30D = 30 * SECONDS_PER_DAY


class Outcome(Enum):
    GOOD = "good"
    BAD = "bad"
    EXCLUDED = "excluded"


class ClientErrors(Enum):
    """What the availability SLI does with a ``4xx`` that is not listed as the server's fault."""

    EXCLUDE = "exclude"  # drop from numerator and denominator ("valid events" only)
    COUNT_AS_GOOD = "count-as-good"  # keep in the denominator as a success (Workbook example)


@dataclass(frozen=True, slots=True)
class StatusPolicy:
    """Maps an HTTP status code to an availability outcome.

    ``server_fault_4xx`` lists client-error codes that are really ours: ``429`` from our own load
    shedding, or ``401`` when the identity provider we depend on is down.
    """

    client_errors: ClientErrors = ClientErrors.EXCLUDE
    server_fault_4xx: frozenset[int] = frozenset()

    def classify(self, status: int) -> Outcome:
        if not 100 <= status <= 599:
            raise ValueError(f"not an HTTP status code: {status}")
        if status >= 500:
            return Outcome.BAD
        if status >= 400:
            if status in self.server_fault_4xx:
                return Outcome.BAD
            if self.client_errors is ClientErrors.EXCLUDE:
                return Outcome.EXCLUDED
        return Outcome.GOOD


@dataclass(frozen=True, slots=True)
class RequestEvent:
    status: int
    duration_s: float


@dataclass(frozen=True, slots=True)
class Counts:
    """Good and bad valid events; EXCLUDED events never reach a ``Counts``."""

    good: int = 0
    bad: int = 0

    @property
    def total(self) -> int:
        return self.good + self.bad

    @property
    def error_ratio(self) -> float | None:
        """``bad / total``, or ``None`` with no traffic (PromQL would return NaN here)."""
        return self.bad / self.total if self.total else None

    def __add__(self, other: Counts) -> Counts:
        return Counts(self.good + other.good, self.bad + other.bad)


def availability_counts(events: Iterable[RequestEvent], policy: StatusPolicy) -> Counts:
    good = bad = 0
    for event in events:
        outcome = policy.classify(event.status)
        if outcome is Outcome.GOOD:
            good += 1
        elif outcome is Outcome.BAD:
            bad += 1
    return Counts(good, bad)


def latency_counts(
    events: Iterable[RequestEvent], policy: StatusPolicy, threshold_s: float
) -> Counts:
    """Latency SLI over requests that did not fail: a fast ``500`` must not flatter latency."""
    good = bad = 0
    for event in events:
        if policy.classify(event.status) is not Outcome.GOOD:
            continue
        if event.duration_s <= threshold_s:
            good += 1
        else:
            bad += 1
    return Counts(good, bad)


class MissingBucketError(ValueError):
    """The SLO threshold is not a bucket boundary, so no exact fraction exists."""


@dataclass(frozen=True, slots=True)
class CumulativeHistogram:
    """A classic Prometheus histogram: ``cumulative[i]`` counts observations ``<= bounds[i]``."""

    bounds: tuple[float, ...]
    cumulative: tuple[int, ...]
    count: int  # the ``+Inf`` bucket

    def __post_init__(self) -> None:
        if len(self.bounds) != len(self.cumulative) or not self.bounds:
            raise ValueError("bounds and cumulative must be the same non-zero length")
        if list(self.bounds) != sorted(set(self.bounds)) or self.bounds[0] <= 0:
            raise ValueError("bounds must be strictly increasing and positive")
        series = (*self.cumulative, self.count)
        if any(a > b for a, b in itertools.pairwise(series)):
            raise ValueError("cumulative counts must be non-decreasing")

    @classmethod
    def observe(cls, durations: Iterable[float], bounds: Sequence[float]) -> CumulativeHistogram:
        ordered = sorted(durations)
        return cls(
            tuple(bounds),
            tuple(bisect_left(ordered, b + math.ulp(b)) for b in bounds),  # ``<=`` is inclusive
            len(ordered),
        )

    def good_exact(self, threshold_s: float) -> Counts:
        """``le="threshold"`` over ``_count``: exact, but only at a configured boundary."""
        try:
            index = self.bounds.index(threshold_s)
        except ValueError:
            raise MissingBucketError(f"no bucket boundary at {threshold_s}") from None
        good = self.cumulative[index]
        return Counts(good, self.count - good)

    def good_estimate(self, threshold_s: float) -> float:
        """Fraction ``<= threshold`` with linear interpolation inside the containing bucket."""
        if threshold_s > self.bounds[-1]:
            raise MissingBucketError("threshold is above the highest finite bucket")
        if not self.count:
            raise ValueError("empty histogram")
        index = bisect_left(self.bounds, threshold_s)
        lower = self.bounds[index - 1] if index else 0.0
        below = self.cumulative[index - 1] if index else 0
        in_bucket = self.cumulative[index] - below
        span = self.bounds[index] - lower
        return (below + in_bucket * (threshold_s - lower) / span) / self.count


def _check_slo(slo: float) -> None:
    if not 0.0 < slo < 1.0:
        raise ValueError(f"SLO must be strictly between 0 and 1, got {slo}")


def error_budget(slo: float) -> float:
    _check_slo(slo)
    return 1.0 - slo


def burn_rate(error_ratio: float, slo: float) -> float:
    """How many times faster than 'exactly spend the budget over the period' we are burning."""
    return error_ratio / error_budget(slo)


def time_to_exhaustion_s(burn: float, period_s: int = PERIOD_30D) -> float:
    return period_s / burn


def budget_consumed(burn: float, window_s: int, period_s: int = PERIOD_30D) -> float:
    """Fraction of the period's budget spent by running at ``burn`` for ``window_s``."""
    return burn * window_s / period_s


def burn_threshold(budget_fraction: float, window_s: int, period_s: int = PERIOD_30D) -> float:
    """Burn rate that spends ``budget_fraction`` of the budget in ``window_s``."""
    return budget_fraction * period_s / window_s


def max_burn_rate(slo: float) -> float:
    """Burn rate of a 100 percent outage: ``1 / (1 - slo)``."""
    return 1.0 / error_budget(slo)


def full_outage_exhaustion_s(slo: float, period_s: int = PERIOD_30D) -> float:
    return period_s * error_budget(slo)


class Severity(Enum):
    PAGE = "page"
    TICKET = "ticket"


@dataclass(frozen=True, slots=True)
class BurnRateRule:
    severity: Severity
    long_window_s: int
    short_window_s: int
    burn_threshold: float
    min_events: int = 0  # long-window traffic floor; 0 disables the low-traffic guard

    def __post_init__(self) -> None:
        if not 0 < self.short_window_s < self.long_window_s:
            raise ValueError("need 0 < short window < long window")
        if self.long_window_s % STEP_SECONDS or self.short_window_s % STEP_SECONDS:
            raise ValueError("windows must be whole evaluation steps")
        if self.burn_threshold <= 0 or self.min_events < 0:
            raise ValueError("burn_threshold must be positive and min_events non-negative")

    def reachable(self, slo: float) -> bool:
        """Can any error ratio ``<= 1`` strictly exceed the threshold?"""
        return self.burn_threshold < max_burn_rate(slo)

    def budget_at_trigger(self, period_s: int = PERIOD_30D) -> float:
        """Budget fraction spent once the long window alone has crossed the threshold."""
        return budget_consumed(self.burn_threshold, self.long_window_s, period_s)


# Table 5-8 of the SRE Workbook chapter "Alerting on SLOs", for a 99.9 percent SLO.
GOOGLE_RULES: tuple[BurnRateRule, ...] = (
    BurnRateRule(Severity.PAGE, 3_600, 300, 14.4),
    BurnRateRule(Severity.PAGE, 21_600, 1_800, 6.0),
    BurnRateRule(Severity.TICKET, 259_200, 21_600, 1.0),
)


class SliSeries:
    """Per-step counts stored as prefix sums, like ``increase()`` over a counter."""

    def __init__(self) -> None:
        self._bad = [0]
        self._total = [0]

    def __len__(self) -> int:
        return len(self._bad) - 1

    def append(self, counts: Counts) -> None:
        self._bad.append(self._bad[-1] + counts.bad)
        self._total.append(self._total[-1] + counts.total)

    def window(self, window_s: int, *, end: int | None = None) -> Counts:
        """Counts in the ``window_s`` ending at step ``end`` (exclusive; default: the newest)."""
        stop = len(self) if end is None else end
        start = max(0, stop - window_s // STEP_SECONDS)
        bad = self._bad[stop] - self._bad[start]
        total = self._total[stop] - self._total[start]
        return Counts(total - bad, bad)


@dataclass(frozen=True, slots=True)
class RuleReading:
    long_burn: float | None
    short_burn: float | None
    firing: bool


def _burn(counts: Counts, slo: float) -> float | None:
    ratio = counts.error_ratio
    return None if ratio is None else burn_rate(ratio, slo)


def evaluate_rule(
    series: SliSeries,
    rule: BurnRateRule,
    slo: float,
    *,
    end: int | None = None,
    use_short_window: bool = True,
) -> RuleReading:
    """``long_burn > T and short_burn > T``, strict, and false when a window has no traffic."""
    long_counts = series.window(rule.long_window_s, end=end)
    long_burn = _burn(long_counts, slo)
    short_burn = _burn(series.window(rule.short_window_s, end=end), slo)
    firing = (
        long_burn is not None
        and long_burn > rule.burn_threshold
        and long_counts.total >= rule.min_events
        and (not use_short_window or (short_burn is not None and short_burn > rule.burn_threshold))
    )
    return RuleReading(long_burn, short_burn, firing)


@dataclass(frozen=True, slots=True)
class FiringTrace:
    """The firing state at the end of each step, plus the facts an on-call cares about."""

    firing: tuple[bool, ...]

    @property
    def first_fire_step(self) -> int | None:
        """Number of steps of data seen when the alert first fired (1-based)."""
        return next((i + 1 for i, on in enumerate(self.firing) if on), None)

    @property
    def last_fire_step(self) -> int | None:
        return next((i + 1 for i in range(len(self.firing) - 1, -1, -1) if self.firing[i]), None)

    @property
    def rising_edges(self) -> int:
        return sum(
            1
            for prev, cur in zip((False, *self.firing), self.firing, strict=False)
            if cur and not prev
        )

    @property
    def steps_firing(self) -> int:
        return sum(self.firing)


def replay(
    steps: Iterable[Counts],
    rule: BurnRateRule,
    slo: float,
    *,
    use_short_window: bool = True,
) -> FiringTrace:
    series = SliSeries()
    out: list[bool] = []
    for counts in steps:
        series.append(counts)
        out.append(evaluate_rule(series, rule, slo, use_short_window=use_short_window).firing)
    return FiringTrace(tuple(out))


def replay_for_clause(
    steps: Iterable[Counts], slo: float, *, window_s: int, threshold: float, for_s: int
) -> FiringTrace:
    """Approach 3 of the Workbook: ``burn(window) > threshold`` for ``for_s`` (Prometheus ``for``).

    The pending timer restarts whenever one evaluation is below the threshold.
    """
    series = SliSeries()
    out: list[bool] = []
    active_since: int | None = None
    for index, counts in enumerate(steps):
        series.append(counts)
        burn = _burn(series.window(window_s), slo)
        if burn is not None and burn > threshold:
            if active_since is None:
                active_since = index
            out.append((index - active_since) * STEP_SECONDS >= for_s)
        else:
            active_since = None
            out.append(False)
    return FiringTrace(tuple(out))


def segments(*parts: tuple[int, int, float]) -> list[Counts]:
    """Build per-step counts from ``(steps, requests_per_step, error_ratio)`` segments."""
    out: list[Counts] = []
    for steps, per_step, ratio in parts:
        bad = round(per_step * ratio)
        out.extend(Counts(per_step - bad, bad) for _ in range(steps))
    return out


def _window_label(seconds: int) -> str:
    for unit, size in (("d", SECONDS_PER_DAY), ("h", 3_600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    raise ValueError(f"window must be whole minutes: {seconds}")


def render_prometheus_rules(
    job: str,
    slo: float,
    rules: Sequence[BurnRateRule],
    client_errors: ClientErrors = ClientErrors.EXCLUDE,
) -> str:
    """Recording and alerting rules for ``rules`` in the shape the Workbook shows (YAML text)."""
    budget = f"{error_budget(slo):.6g}"
    selector = f'job="{job}"'
    denominator = (
        f'{{{selector},status!~"4.."}}'
        if client_errors is ClientErrors.EXCLUDE
        else f"{{{selector}}}"
    )
    windows = sorted({w for r in rules for w in (r.long_window_s, r.short_window_s)})
    lines = ["groups:", f"- name: slo-{job}", "  rules:"]
    for w in windows:
        label = _window_label(w)
        lines += [
            f"  - record: job:slo_errors_per_request:ratio_rate{label}",
            "    expr: |",
            f'      sum(rate(http_requests_total{{{selector},status=~"5.."}}[{label}]))',
            "      /",
            f"      sum(rate(http_requests_total{denominator}[{label}]))",
        ]
    for r in rules:
        long_l, short_l = _window_label(r.long_window_s), _window_label(r.short_window_s)
        threshold = f"{r.burn_threshold:g} * {budget}"
        lines += [
            f"  - alert: ErrorBudgetBurn{r.severity.value.capitalize()}{long_l}{short_l}",
            "    expr: |",
            f"      job:slo_errors_per_request:ratio_rate{long_l} > ({threshold})",
            "      and",
            f"      job:slo_errors_per_request:ratio_rate{short_l} > ({threshold})",
            "    labels:",
            f"      severity: {r.severity.value}",
        ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class Observation:
    route: str  # the route template, never the raw path
    status: int
    duration_s: float
    truncated: bool = False  # headers were sent, then the app raised


class SliMiddleware:
    """Pure ASGI middleware that records exactly one ``Observation`` per HTTP request.

    It must sit INSIDE ``ServerErrorMiddleware`` (Starlette puts user middleware there), so an
    unhandled exception reaches it as an exception, not as a ``500`` response it could read.
    """

    def __init__(
        self,
        app: ASGIApp,
        sink: Callable[[Observation], None],
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.app = app
        self.sink = sink
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = self.clock()
        status: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            self._record(scope, 500, started, truncated=status is not None)
            raise
        self._record(scope, status if status is not None else 500, started)

    def _record(
        self, scope: Scope, status: int, started: float, *, truncated: bool = False
    ) -> None:
        route: Any = scope.get("route")
        template = getattr(route, "path", None)
        self.sink(
            Observation(
                template if isinstance(template, str) else "unmatched",
                status,
                self.clock() - started,
                truncated,
            )
        )
