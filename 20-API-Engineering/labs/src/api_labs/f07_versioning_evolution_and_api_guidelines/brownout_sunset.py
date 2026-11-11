"""Per-tenant sunset telemetry, announced brownout windows, and 410 Gone cutover (pure ASGI)."""

from __future__ import annotations

import enum
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROBLEM_TYPE = "https://developer.example.com/problems/api-version-retired"
OVERFLOW_TENANT = "(other)"
ANONYMOUS_TENANT = "(anonymous)"


class Phase(enum.StrEnum):
    """Lifecycle phase of the deprecated surface at one instant."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    BROWNOUT = "brownout"
    SUNSET = "sunset"


class Outcome(enum.StrEnum):
    """What the middleware did with one request to the deprecated surface."""

    SERVED = "served"
    SERVED_DEPRECATED = "served_deprecated"
    WAIVED = "waived"
    BROWNOUT_REJECTED = "brownout_rejected"
    GONE = "gone"


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime, got {value!r}")


def http_date(value: datetime) -> str:
    """Render an aware instant as an IMF-fixdate (RFC 9110 Section 5.6.7)."""
    if value.tzinfo is None:
        raise ValueError("HTTP-date needs an aware datetime")
    # format_datetime(usegmt=True) accepts only tzinfo == timezone.utc, so normalize first.
    return format_datetime(value.astimezone(UTC), usegmt=True)


def sf_date(value: datetime) -> str:
    """Render an instant as a Structured Field Date (RFC 9651 Section 3.3.7)."""
    _require_utc("sf-date", value)
    return f"@{int(value.timestamp())}"


@dataclass(frozen=True, slots=True)
class Window:
    """One announced brownout window, half-open: start <= t < end."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _require_utc("window start", self.start)
        _require_utc("window end", self.end)
        if self.start >= self.end:
            raise ValueError(f"empty window {self.start.isoformat()}..{self.end.isoformat()}")

    def contains(self, at: datetime) -> bool:
        return self.start <= at < self.end


def hourly_windows(
    first: datetime, until: datetime, *, minute: int, duration: timedelta
) -> tuple[Window, ...]:
    """Windows starting at HH:minute every hour from first's hour while the start is < until."""
    _require_utc("first", first)
    _require_utc("until", until)
    if not 0 <= minute < 60 or not timedelta(0) < duration <= timedelta(hours=1) - timedelta(
        minutes=minute
    ):
        raise ValueError("window must start and end inside its own hour")
    hour = first.replace(minute=0, second=0, microsecond=0)
    windows: list[Window] = []
    while (start := hour + timedelta(minutes=minute)) < until:
        if start >= first:
            windows.append(Window(start, start + duration))
        hour += timedelta(hours=1)
    return tuple(windows)


@dataclass(frozen=True, slots=True)
class SunsetPolicy:
    """Retirement contract for every path under one version prefix."""

    path_prefix: str
    deprecation: datetime
    sunset: datetime
    windows: tuple[Window, ...]
    policy_url: str
    waivers: Mapping[str, datetime] = field(default_factory=dict[str, datetime])
    waiver_header: str = "brownout-waiver"
    gone_max_age: int = 300
    _starts: tuple[datetime, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not (self.path_prefix.startswith("/") and self.path_prefix.endswith("/")):
            raise ValueError("path_prefix must look like '/v1/'")
        _require_utc("deprecation", self.deprecation)
        _require_utc("sunset", self.sunset)
        if self.sunset < self.deprecation:
            # RFC 9745 Section 4: Sunset MUST NOT be earlier than Deprecation.
            raise ValueError("sunset is earlier than deprecation")
        previous_end = self.deprecation
        for window in self.windows:
            if window.start < previous_end or window.end > self.sunset:
                raise ValueError(f"window {window.start.isoformat()} overlaps or leaves range")
            previous_end = window.end
        # Sorted, non-overlapping windows make the per-request lookup a binary search.
        object.__setattr__(self, "_starts", tuple(w.start for w in self.windows))
        for tenant, expiry in self.waivers.items():
            _require_utc(f"waiver for {tenant}", expiry)
            if expiry > self.sunset:
                raise ValueError(f"waiver for {tenant} outlives the sunset")

    def matches(self, path: str) -> bool:
        return path == self.path_prefix[:-1] or path.startswith(self.path_prefix)

    def phase(self, at: datetime) -> Phase:
        _require_utc("clock", at)
        if at >= self.sunset:
            return Phase.SUNSET
        if at < self.deprecation:
            return Phase.ACTIVE
        if self.window_at(at) is not None:
            return Phase.BROWNOUT
        return Phase.DEPRECATED

    def window_at(self, at: datetime) -> Window | None:
        index = bisect_right(self._starts, at) - 1
        if index >= 0 and self.windows[index].contains(at):
            return self.windows[index]
        return None

    def waiver_honored(self, tenant: str, headers: Headers, at: datetime) -> bool:
        """Both the client's explicit ask and an unexpired server-side grant are required."""
        expiry = self.waivers.get(tenant)
        asked = headers.get(self.waiver_header) == "1"
        return asked and expiry is not None and at < expiry

    def signal_headers(self) -> dict[str, str]:
        return {
            "deprecation": sf_date(self.deprecation),
            "sunset": http_date(self.sunset),
            "link": (
                f'<{self.policy_url}>; rel="deprecation"; type="text/html", '
                f'<{self.policy_url}>; rel="sunset"; type="text/html"'
            ),
        }


@dataclass(slots=True)
class SunsetTelemetry:
    """Bounded per-tenant counters for the deprecated surface."""

    max_tenants: int = 1000
    counts: Counter[tuple[str, Outcome]] = field(default_factory=Counter[tuple[str, Outcome]])
    last_seen: dict[str, datetime] = field(default_factory=dict[str, datetime])

    def record(self, tenant: str, outcome: Outcome, at: datetime) -> str:
        if tenant not in self.last_seen and len(self.last_seen) >= self.max_tenants:
            tenant = OVERFLOW_TENANT
        self.counts[tenant, outcome] += 1
        self.last_seen[tenant] = max(at, self.last_seen.get(tenant, at))
        return tenant

    def stragglers(self, since: datetime) -> list[str]:
        """Tenants that still called the deprecated surface at or after `since`."""
        return sorted(tenant for tenant, seen in self.last_seen.items() if seen >= since)


def _problem(policy: SunsetPolicy, phase: Phase, window: Window | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE,
        "title": "API version retired",
        "status": 410,
        "detail": f"{policy.path_prefix} is retired; see {policy.policy_url}",
        "phase": phase.value,
        "sunset": policy.sunset.isoformat(),
    }
    if window is not None:
        body["brownout_ends"] = window.end.isoformat()
    return body


class SunsetMiddleware:
    """Signals deprecation, enforces brownout windows, and serves 410 after the sunset."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        policy: SunsetPolicy,
        telemetry: SunsetTelemetry,
        tenant_of: Callable[[Scope], str | None],
        clock: Callable[[], datetime],
    ) -> None:
        self.app = app
        self.policy = policy
        self.telemetry = telemetry
        self.tenant_of = tenant_of
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.policy.matches(scope["path"]):
            await self.app(scope, receive, send)
            return
        now = self.clock()
        phase = self.policy.phase(now)
        tenant = self.tenant_of(scope) or ANONYMOUS_TENANT
        if phase is Phase.ACTIVE:
            self.telemetry.record(tenant, Outcome.SERVED, now)
            await self.app(scope, receive, send)
            return
        if phase is Phase.SUNSET:
            self.telemetry.record(tenant, Outcome.GONE, now)
            cache = f"max-age={self.policy.gone_max_age}"
            await self._gone(scope, receive, send, phase, None, cache)
            return
        if phase is Phase.BROWNOUT:
            if not self.policy.waiver_honored(tenant, Headers(scope=scope), now):
                self.telemetry.record(tenant, Outcome.BROWNOUT_REJECTED, now)
                window = self.policy.window_at(now)
                await self._gone(scope, receive, send, phase, window, "no-store")
                return
            self.telemetry.record(tenant, Outcome.WAIVED, now)
        else:
            self.telemetry.record(tenant, Outcome.SERVED_DEPRECATED, now)
        await self.app(scope, receive, self._signalling(send))

    def _signalling(self, send: Send) -> Send:
        extra = self.policy.signal_headers()

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    headers[name] = value
            await send(message)

        return wrapped

    async def _gone(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        phase: Phase,
        window: Window | None,
        cache_control: str,
    ) -> None:
        headers = {**self.policy.signal_headers(), "cache-control": cache_control}
        response = JSONResponse(
            _problem(self.policy, phase, window),
            status_code=410,
            headers=headers,
            media_type="application/problem+json",
        )
        await response(scope, receive, send)
