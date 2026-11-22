"""Client-side telemetry lab: NEL policies, Reporting groups, a collector, SLIs, and canaries.

Models the browser side (policy cache, report generation, delivery queue), the collector side
(an ASGI endpoint that ingests `application/reports+json`), the client-side SLI computed from
sampled reports, the edge-versus-origin gap check, and a synthetic canary state machine.
Everything is deterministic: clocks, random rolls, and the network are injected.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

Phase = Literal["dns", "connection", "application"]
PHASES: Final[frozenset[str]] = frozenset({"dns", "connection", "application"})

MAX_HEADER_BYTES: Final = 16 * 1024
NEL_POLICY_STALE_SECONDS: Final = 172_800
REPORTS_CONTENT_TYPE: Final = "application/reports+json"

MAX_REPORT_COUNT: Final = 100
MAX_REPORT_AGE_SECONDS: Final = 900.0
MAX_REPORT_ATTEMPTS: Final = 5
BACKOFF_INITIAL_SECONDS: Final = 60.0
BACKOFF_FACTOR: Final = 2.0
COLLECTOR_MAX_BODY_BYTES: Final = 64 * 1024


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-RFC 8259 JSON constant {name}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _wrap_json_list(header: str) -> list[object] | None:
    """Parse a `json-field-value`: a JSON list written without its outer brackets."""
    if len(header.encode("utf-8")) > MAX_HEADER_BYTES:
        return None
    try:
        parsed: object = json.loads(f"[{header}]", parse_constant=_reject_constant)
    except ValueError:
        return None
    if isinstance(parsed, list):
        return cast("list[object]", parsed)
    return None


@dataclass(frozen=True)
class Origin:
    """Scheme and host only; a simplification of the web origin (no port)."""

    scheme: str
    host: str

    @staticmethod
    def parse(url: str) -> Origin:
        parts = urlsplit(url)
        return Origin(parts.scheme.lower(), (parts.hostname or "").lower())

    @property
    def trustworthy(self) -> bool:
        return self.scheme == "https"

    def superdomains(self) -> list[str]:
        labels = self.host.split(".")
        return [".".join(labels[i:]) for i in range(1, len(labels))]


@dataclass(frozen=True)
class NelPolicy:
    origin: Origin
    received_ip: str
    report_to: str
    max_age: int
    include_subdomains: bool
    success_fraction: float
    failure_fraction: float
    created: float

    def expired(self, now: float) -> bool:
        return now - self.created > self.max_age

    def stale(self, now: float) -> bool:
        return now - self.created > NEL_POLICY_STALE_SECONDS


@dataclass(frozen=True)
class PolicyRemoval:
    origin: Origin


def parse_nel_header(
    value: str, *, origin: Origin, received_ip: str, now: float
) -> NelPolicy | PolicyRemoval | None:
    """Apply NEL 4.2 to the first list element; None means the header is ignored."""
    if not origin.trustworthy:
        return None
    items = _wrap_json_list(value)
    if not items:
        return None
    item = _as_dict(items[0])
    if item is None:
        return None
    max_age = item.get("max_age")
    if not _is_int(max_age) or cast("int", max_age) < 0:
        return None
    if max_age == 0:
        return PolicyRemoval(origin)
    report_to = item.get("report_to")
    if not isinstance(report_to, str):
        return None
    fractions: dict[str, float] = {}
    for key, default in (("success_fraction", 0.0), ("failure_fraction", 1.0)):
        raw = item.get(key, default)
        if not _is_number(raw) or not 0.0 <= cast("float", raw) <= 1.0:
            return None
        fractions[key] = float(cast("float", raw))
    include = item.get("include_subdomains") is True
    if include and "." not in origin.host:
        return None  # stand-in for "an eTLD may not set include_subdomains"
    return NelPolicy(
        origin=origin,
        received_ip=received_ip,
        report_to=report_to,
        max_age=cast("int", max_age),
        include_subdomains=include,
        success_fraction=fractions["success_fraction"],
        failure_fraction=fractions["failure_fraction"],
        created=now,
    )


class PolicyCache:
    """NEL 3.6 policy cache for one network partition."""

    def __init__(self) -> None:
        self._policies: dict[Origin, NelPolicy] = {}

    def __len__(self) -> int:
        return len(self._policies)

    def process_header(
        self, value: str, *, origin: Origin, received_ip: str, now: float
    ) -> NelPolicy | None:
        result = parse_nel_header(value, origin=origin, received_ip=received_ip, now=now)
        if isinstance(result, PolicyRemoval):
            self._policies.pop(result.origin, None)
            return None
        if result is not None:
            self._policies[origin] = result
        return result

    def choose(self, origin: Origin, now: float) -> NelPolicy | None:
        """NEL 5.1: exact unexpired policy, else the nearest unexpired parent with subdomains."""
        exact = self._policies.get(origin)
        if exact is not None and not exact.expired(now):
            return exact
        for parent in origin.superdomains():
            candidate = self._policies.get(Origin(origin.scheme, parent))
            if (
                candidate is not None
                and candidate.include_subdomains
                and not candidate.expired(now)
            ):
                return candidate
        return None

    def discard(self, policy: NelPolicy) -> None:
        self._policies.pop(policy.origin, None)


@dataclass(frozen=True)
class NetworkError:
    phase: Phase
    type: str


@dataclass(frozen=True)
class RequestOutcome:
    url: str
    error: NetworkError | None = None
    status_code: int = 0
    method: str = "GET"
    server_ip: str = ""
    protocol: str = ""
    elapsed_ms: int = 0
    referrer: str = ""
    user_agent: str = "lab-agent/1.0"


@dataclass(frozen=True)
class NelReport:
    url: str
    user_agent: str
    body: dict[str, object]
    group: str
    timestamp: float


def _report_url(url: str, phase: str) -> str:
    parts = urlsplit(url)
    if phase in {"dns", "connection"}:
        return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def generate_report(
    cache: PolicyCache, outcome: RequestOutcome, *, now: float, roll: float
) -> NelReport | None:
    """NEL 5.4 with the sampling shortcut Chromium uses (a rate of 0.0 never reports)."""
    origin = Origin.parse(outcome.url)
    if not origin.trustworthy:
        return None
    policy = cache.choose(origin, now)
    if policy is None:
        return None
    http_error = 400 <= outcome.status_code < 600
    failed = outcome.error is not None or http_error
    if outcome.error is not None:
        phase: str = outcome.error.phase
        kind = outcome.error.type
    else:
        phase, kind = "application", "ok"
    if http_error:
        phase, kind = "application", "http.error"
    rate = policy.failure_fraction if failed else policy.success_fraction
    if rate <= 0.0 or (rate < 1.0 and roll >= rate):
        return None
    if phase != "dns" and policy.include_subdomains and policy.origin != origin:
        return None
    status, elapsed = outcome.status_code, outcome.elapsed_ms
    if phase != "dns" and outcome.server_ip and outcome.server_ip != policy.received_ip:
        phase, kind, status, elapsed = "dns", "dns.address_changed", 0, 0
    if policy.stale(now):
        cache.discard(policy)
    body: dict[str, object] = {
        "referrer": outcome.referrer,
        "sampling_fraction": rate,
        "server_ip": outcome.server_ip,
        "protocol": outcome.protocol,
        "method": outcome.method,
        "status_code": status,
        "elapsed_time": elapsed,
        "phase": phase,
        "type": kind,
    }
    return NelReport(
        url=_report_url(outcome.url, phase),
        user_agent=outcome.user_agent,
        body=body,
        group=policy.report_to,
        timestamp=now,
    )


@dataclass
class Endpoint:
    url: str
    priority: int = 1
    weight: int = 1
    failures: int = 0
    retry_after: float | None = None

    def pending(self, now: float) -> bool:
        return self.retry_after is not None and self.retry_after > now


@dataclass
class EndpointGroup:
    name: str
    max_age: int
    include_subdomains: bool
    created: float
    endpoints: list[Endpoint] = field(default_factory=lambda: list[Endpoint]())

    def expired(self, now: float) -> bool:
        return self.created + self.max_age < now


@dataclass(frozen=True)
class ReportToResult:
    groups: list[EndpointGroup]
    removed: list[str]


def _parse_endpoint(raw: object) -> Endpoint | None:
    item = _as_dict(raw)
    if item is None:
        return None
    url = item.get("url")
    if not isinstance(url, str) or not Origin.parse(url).trustworthy:
        return None
    priority = item.get("priority", 1)
    weight = item.get("weight", 1)
    for number in (priority, weight):
        if not _is_int(number) or cast("int", number) < 0:
            return None
    return Endpoint(url, cast("int", priority), cast("int", weight))


def parse_report_to(value: str, *, now: float) -> ReportToResult | None:
    """Parse the legacy `Report-To` header (Reporting API WD 2018-09-25 Section 3.1)."""
    items = _wrap_json_list(value)
    if items is None:
        return None
    groups: list[EndpointGroup] = []
    removed: list[str] = []
    seen: set[str] = set()
    for raw in items:
        item = _as_dict(raw)
        if item is None:
            continue
        max_age = item.get("max_age")
        name = item.get("group", "default")
        if not _is_int(max_age) or cast("int", max_age) < 0 or not isinstance(name, str):
            continue
        if name in seen:
            continue
        seen.add(name)
        if max_age == 0:
            removed.append(name)
            continue
        raw_endpoints = item.get("endpoints")
        if not isinstance(raw_endpoints, list):
            continue
        endpoints = [
            e
            for e in (_parse_endpoint(r) for r in cast("list[object]", raw_endpoints))
            if e is not None
        ]
        if endpoints:
            groups.append(
                EndpointGroup(
                    name=name,
                    max_age=cast("int", max_age),
                    include_subdomains=item.get("include_subdomains") is True,
                    created=now,
                    endpoints=endpoints,
                )
            )
    return ReportToResult(groups, removed)


def choose_endpoint(group: EndpointGroup, now: float, rand: float) -> Endpoint | None:
    """Reporting WD 4.3: lowest priority class among non-pending endpoints, then weighted pick."""
    if group.expired(now):
        return None
    eligible = [e for e in group.endpoints if not e.pending(now)]
    if not eligible:
        return None
    best = min(e.priority for e in eligible)
    klass = [e for e in eligible if e.priority == best]
    total = sum(e.weight for e in klass)
    if total == 0:
        return klass[0]
    target = rand * total
    for endpoint in klass:
        if target < endpoint.weight:
            return endpoint
        target -= endpoint.weight
    return klass[-1]


@dataclass
class QueuedReport:
    report: NelReport
    attempts: int = 0


@dataclass(frozen=True)
class DeliveryStats:
    delivered: int = 0
    failed_uploads: int = 0
    removed_endpoints: int = 0
    dropped: int = 0


Sender = Callable[[str, bytes], Awaitable[int | None]]


class ReportingCache:
    """Client-side reporting cache for one origin: endpoint groups plus the report queue."""

    def __init__(self) -> None:
        self.groups: dict[str, EndpointGroup] = {}
        self.queue: list[QueuedReport] = []

    def apply_report_to(self, result: ReportToResult) -> None:
        for name in result.removed:
            self.groups.pop(name, None)
        for group in result.groups:
            self.groups[group.name] = group

    def enqueue(self, report: NelReport) -> None:
        self.queue.append(QueuedReport(report))
        if len(self.queue) > MAX_REPORT_COUNT:
            self.queue.pop(0)

    def collect_garbage(self, now: float) -> int:
        keep = [
            q
            for q in self.queue
            if q.attempts < MAX_REPORT_ATTEMPTS
            and now - q.report.timestamp < MAX_REPORT_AGE_SECONDS
        ]
        dropped = len(self.queue) - len(keep)
        self.queue = keep
        return dropped

    async def deliver(self, now: float, send: Sender, rand: float = 0.0) -> DeliveryStats:
        """Reporting WD 4.4 and 4.5: one POST per endpoint; 2xx removes, 410 drops the endpoint."""
        dropped = self.collect_garbage(now)
        by_endpoint: dict[int, tuple[Endpoint, list[QueuedReport]]] = {}
        for queued in self.queue:
            group = self.groups.get(queued.report.group)
            endpoint = choose_endpoint(group, now, rand) if group is not None else None
            if group is None or endpoint is None:
                continue
            by_endpoint.setdefault(id(endpoint), (endpoint, []))[1].append(queued)
        delivered = failed = removed = 0
        for endpoint, batch in by_endpoint.values():
            payload = json.dumps(
                [
                    {
                        "age": int((now - q.report.timestamp) * 1000),
                        "type": "network-error",
                        "url": q.report.url,
                        "user_agent": q.report.user_agent,
                        "body": q.report.body,
                    }
                    for q in batch
                ]
            ).encode()
            for q in batch:
                q.attempts += 1
            status = await send(endpoint.url, payload)
            if status is not None and 200 <= status <= 299:
                endpoint.failures, endpoint.retry_after = 0, None
                sent = {id(q) for q in batch}
                self.queue = [q for q in self.queue if id(q) not in sent]
                delivered += len(batch)
            elif status == 410:
                for group in self.groups.values():
                    if endpoint in group.endpoints:
                        group.endpoints.remove(endpoint)
                removed += 1
            else:
                endpoint.failures += 1
                endpoint.retry_after = now + BACKOFF_INITIAL_SECONDS * BACKOFF_FACTOR ** (
                    endpoint.failures - 1
                )
                failed += 1
        return DeliveryStats(delivered, failed, removed, dropped)


@dataclass(frozen=True)
class StoredReport:
    region: str
    observed_at: float
    phase: str
    type: str
    status_code: int
    weight: float


def parse_reports(
    payload: object, *, received_at: float, region: str
) -> tuple[list[StoredReport], int]:
    """Validate each report independently; return (accepted, rejected_count)."""
    if not isinstance(payload, list):
        raise ValueError("payload must be a JSON array")
    accepted: list[StoredReport] = []
    rejected = 0
    for raw in cast("list[object]", payload):
        item = _as_dict(raw)
        body = _as_dict(item.get("body")) if item is not None else None
        if item is None or body is None or item.get("type") != "network-error":
            rejected += 1
            continue
        age = item.get("age")
        fraction = body.get("sampling_fraction")
        phase = body.get("phase")
        kind = body.get("type")
        status = body.get("status_code", 0)
        if (
            not _is_int(age)
            or cast("int", age) < 0
            or not _is_number(fraction)
            or not 0.0 < cast("float", fraction) <= 1.0
            or not isinstance(phase, str)
            or phase not in PHASES
            or not isinstance(kind, str)
            or not _is_int(status)
        ):
            rejected += 1
            continue
        accepted.append(
            StoredReport(
                region=region,
                observed_at=received_at - cast("int", age) / 1000.0,
                phase=phase,
                type=kind,
                status_code=cast("int", status),
                weight=1.0 / float(cast("float", fraction)),
            )
        )
    return accepted, rejected


@dataclass(frozen=True)
class SliWindow:
    good: float
    bad: float
    reports: int

    @property
    def sli(self) -> float | None:
        total = self.good + self.bad
        return None if total == 0 else self.good / total


def _is_bad(report: StoredReport, *, strict_4xx: bool) -> bool:
    if report.type == "ok":
        return False
    if report.type == "http.error":
        return strict_4xx or report.status_code >= 500
    return True


class ReportStore:
    def __init__(self) -> None:
        self._reports: list[StoredReport] = []

    def add(self, reports: Iterable[StoredReport]) -> None:
        self._reports.extend(reports)

    def __len__(self) -> int:
        return len(self._reports)

    def window(
        self,
        region: str,
        start: float,
        end: float,
        *,
        strict_4xx: bool = False,
        weighted: bool = True,
    ) -> SliWindow:
        good = bad = 0.0
        count = 0
        for r in self._reports:
            if r.region != region or not start <= r.observed_at < end:
                continue
            count += 1
            w = r.weight if weighted else 1.0
            if _is_bad(r, strict_4xx=strict_4xx):
                bad += w
            else:
                good += w
        return SliWindow(good, bad, count)

    def failure_types(self, region: str, start: float, end: float) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for r in self._reports:
            if r.region == region and start <= r.observed_at < end and r.type != "ok":
                counts[f"{r.phase}:{r.type}"] += 1
        return dict(counts)


_PREFLIGHT_HEADERS: Final = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "POST",
    "access-control-allow-headers": "content-type",
}


def chromium_preflight_accepts(status: int, headers: Mapping[str, str], report_origin: str) -> bool:
    """The acceptance test in Chromium's ReportingUploader HandlePreflightResponse."""
    lowered = {k.lower(): v.strip().lower() for k, v in headers.items()}
    allow_origin = lowered.get("access-control-allow-origin", "")
    allow_headers = lowered.get("access-control-allow-headers", "")
    return (
        200 <= status <= 299
        and allow_origin in {"*", report_origin.lower()}
        and allow_headers in {"*", "content-type"}
    )


def create_collector_app(
    store: ReportStore,
    clock: Callable[[], float],
    *,
    max_body_bytes: int = COLLECTOR_MAX_BODY_BYTES,
) -> Starlette:
    """A NEL collector. `x-lab-client-region` stands in for an edge geo lookup of the client IP."""

    async def preflight(_: Request) -> Response:
        return Response(status_code=204, headers=dict(_PREFLIGHT_HEADERS))

    async def ingest(request: Request) -> Response:
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != REPORTS_CONTENT_TYPE:
            return JSONResponse({"error": "unsupported media type"}, status_code=415)
        body = await request.body()
        if len(body) > max_body_bytes:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            payload: object = json.loads(body, parse_constant=_reject_constant)
            accepted, rejected = parse_reports(
                payload,
                received_at=clock(),
                region=request.headers.get("x-lab-client-region", "unknown"),
            )
        except ValueError:
            return JSONResponse({"error": "malformed reports"}, status_code=400)
        store.add(accepted)
        return JSONResponse({"accepted": len(accepted), "rejected": rejected})

    return Starlette(
        routes=[
            Route("/nel", ingest, methods=["POST"]),
            Route("/nel", preflight, methods=["OPTIONS"]),
        ]
    )


@dataclass(frozen=True)
class OriginWindow:
    requests: int
    server_errors: int

    @property
    def sli(self) -> float | None:
        return None if self.requests == 0 else 1.0 - self.server_errors / self.requests


@dataclass(frozen=True)
class RegionVerdict:
    region: str
    origin_sli: float | None
    client_sli: float | None
    client_reports: int
    volume_ratio: float | None
    blind_spot: bool


def edge_origin_gap(
    *,
    origin: Mapping[str, OriginWindow],
    baseline_requests: Mapping[str, int],
    store: ReportStore,
    start: float,
    end: float,
    slo: float = 0.999,
    min_reports: int = 20,
) -> list[RegionVerdict]:
    """Flag regions where users fail while the origin's own 5xx ratio still meets the SLO."""
    verdicts: list[RegionVerdict] = []
    for region in sorted(set(origin) | set(baseline_requests)):
        window = origin.get(region, OriginWindow(0, 0))
        client = store.window(region, start, end)
        baseline = baseline_requests.get(region, 0)
        ratio = window.requests / baseline if baseline else None
        client_sli = client.sli
        origin_ok = window.sli is None or window.sli >= slo
        blind = (
            client_sli is not None
            and client.reports >= min_reports
            and client_sli < slo
            and origin_ok
        )
        verdicts.append(RegionVerdict(region, window.sli, client_sli, client.reports, ratio, blind))
    return verdicts


@dataclass(frozen=True)
class Fault:
    phase: Phase
    type: str
    status_code: int = 0


@dataclass(frozen=True)
class ProbeResult:
    region: str
    ok: bool
    phase: str
    type: str
    elapsed_ms: int


STAGE_LATENCY_MS: Final[dict[str, int]] = {"dns": 20, "connection": 60, "application": 30}
PROBE_TIMEOUT_MS: Final = 3000


class SimulatedNetwork:
    """Per-(region, host) fault injection; an absent fault means the stage succeeds."""

    def __init__(self) -> None:
        self._faults: dict[tuple[str, str, str], Fault] = {}

    def inject(self, region: str, host: str, fault: Fault) -> None:
        self._faults[(region, host, fault.phase)] = fault

    def clear(self, region: str, host: str) -> None:
        for key in [k for k in self._faults if k[:2] == (region, host)]:
            del self._faults[key]

    def probe(self, region: str, url: str) -> ProbeResult:
        host = Origin.parse(url).host
        elapsed = 0
        for phase in ("dns", "connection", "application"):
            fault = self._faults.get((region, host, phase))
            if fault is None:
                elapsed += STAGE_LATENCY_MS[phase]
                continue
            elapsed += (
                PROBE_TIMEOUT_MS if fault.type.endswith("timed_out") else STAGE_LATENCY_MS[phase]
            )
            return ProbeResult(region, False, phase, fault.type, elapsed)
        return ProbeResult(region, True, "application", "ok", elapsed)


CanaryState = Literal["healthy", "suspect", "failing"]


@dataclass
class _RegionTrack:
    state: CanaryState = "healthy"
    failures: int = 0
    successes: int = 0
    last: ProbeResult | None = None


class CanaryRunner:
    """Consecutive-failure state machine with hysteresis on recovery."""

    def __init__(self, *, fail_threshold: int = 3, recover_threshold: int = 2) -> None:
        self.fail_threshold = fail_threshold
        self.recover_threshold = recover_threshold
        self._tracks: dict[str, _RegionTrack] = defaultdict(_RegionTrack)

    def observe(self, result: ProbeResult) -> Literal["opened", "closed"] | None:
        track = self._tracks[result.region]
        track.last = result
        if result.ok:
            track.failures = 0
            if track.state == "failing":
                track.successes += 1
                if track.successes >= self.recover_threshold:
                    track.state, track.successes = "healthy", 0
                    return "closed"
                return None
            track.state = "healthy"
            return None
        track.successes = 0
        track.failures += 1
        if track.failures >= self.fail_threshold:
            opened = track.state != "failing"
            track.state = "failing"
            return "opened" if opened else None
        if track.state != "failing":
            track.state = "suspect"
        return None

    def state(self, region: str) -> CanaryState:
        return self._tracks[region].state

    def classify(self, regions: Iterable[str]) -> str:
        """`none`, `regional:<sorted regions>`, or `global` when every region fails in one phase."""
        failing = {r: self._tracks[r] for r in regions if self._tracks[r].state == "failing"}
        every = list(regions)
        if not failing:
            return "none"
        phases = {t.last.phase for t in failing.values() if t.last is not None}
        if len(failing) == len(every) and len(phases) == 1:
            return "global"
        return "regional:" + ",".join(sorted(failing))


def detection_delay_seconds(interval_seconds: float, fail_threshold: int) -> float:
    """Seconds from the first failing probe to the `opened` event."""
    return interval_seconds * math.ceil(fail_threshold - 1)
