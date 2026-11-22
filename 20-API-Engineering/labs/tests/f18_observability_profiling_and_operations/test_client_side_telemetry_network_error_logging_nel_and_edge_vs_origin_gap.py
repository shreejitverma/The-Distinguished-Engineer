"""Unit tests for NEL policy handling, Reporting delivery, the collector, SLIs, and canaries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f18_observability_profiling_and_operations.client_side_telemetry import (
    MAX_HEADER_BYTES,
    MAX_REPORT_AGE_SECONDS,
    MAX_REPORT_COUNT,
    REPORTS_CONTENT_TYPE,
    CanaryRunner,
    Endpoint,
    EndpointGroup,
    Fault,
    NelReport,
    NetworkError,
    Origin,
    OriginWindow,
    PolicyCache,
    PolicyRemoval,
    ProbeResult,
    ReportingCache,
    ReportStore,
    RequestOutcome,
    SimulatedNetwork,
    choose_endpoint,
    chromium_preflight_accepts,
    create_collector_app,
    detection_delay_seconds,
    edge_origin_gap,
    generate_report,
    parse_nel_header,
    parse_report_to,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette

SITE = Origin("https", "www.example.com")
IP = "192.0.2.1"
POLICY = '{"report_to": "nel", "max_age": 2592000, "success_fraction": 1.0}'
DNS_FAIL = NetworkError("dns", "dns.name_not_resolved")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def primed(header: str = POLICY, now: float = 0.0) -> PolicyCache:
    cache = PolicyCache()
    assert cache.process_header(header, origin=SITE, received_ip=IP, now=now) is not None
    return cache


def make_report(
    url: str = "https://www.example.com/", ts: float = 0.0, group: str = "nel"
) -> NelReport:
    return NelReport(url=url, user_agent="ua", body={"phase": "dns"}, group=group, timestamp=ts)


# ---------------------------------------------------------------- NEL header parsing


@pytest.mark.unit
def test_nel_valid_policy_uses_spec_defaults_for_fractions() -> None:
    policy = parse_nel_header(
        '{"report_to": "nel", "max_age": 60}', origin=SITE, received_ip=IP, now=5.0
    )
    assert not isinstance(policy, PolicyRemoval)
    assert policy is not None
    assert (policy.success_fraction, policy.failure_fraction) == (0.0, 1.0)
    assert policy.include_subdomains is False
    assert policy.created == 5.0
    assert policy.received_ip == IP


@pytest.mark.unit
def test_nel_max_age_zero_removes_cached_policy_without_report_to() -> None:
    cache = primed()
    assert len(cache) == 1
    assert cache.process_header('{"max_age": 0}', origin=SITE, received_ip=IP, now=1.0) is None
    assert len(cache) == 0
    assert isinstance(
        parse_nel_header('{"max_age": 0}', origin=SITE, received_ip=IP, now=0.0), PolicyRemoval
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    [
        "",
        "not json",
        "[1]",
        '{"max_age": 60}',
        '{"report_to": 7, "max_age": 60}',
        '{"report_to": "nel"}',
        '{"report_to": "nel", "max_age": -1}',
        '{"report_to": "nel", "max_age": 5.5}',
        '{"report_to": "nel", "max_age": "60"}',
        '{"report_to": "nel", "max_age": true}',
        '{"report_to": "nel", "max_age": 60, "success_fraction": 1.5}',
        '{"report_to": "nel", "max_age": 60, "failure_fraction": -0.1}',
        '{"report_to": "nel", "max_age": 60, "failure_fraction": NaN}',
        '{"report_to": "nel", "max_age": 60, "success_fraction": true}',
    ],
)
def test_nel_invalid_header_is_ignored_entirely(header: str) -> None:
    assert parse_nel_header(header, origin=SITE, received_ip=IP, now=0.0) is None


@pytest.mark.unit
def test_nel_header_size_limit_is_exactly_16_kib() -> None:
    base = '{"report_to": "nel", "max_age": 60, "pad": ""}'
    pad = MAX_HEADER_BYTES - len(base)
    at_limit = base.replace('""', '"' + "x" * pad + '"')
    assert len(at_limit.encode()) == MAX_HEADER_BYTES
    assert parse_nel_header(at_limit, origin=SITE, received_ip=IP, now=0.0) is not None
    over = at_limit.replace('"x', '"xx', 1)
    assert len(over.encode()) == MAX_HEADER_BYTES + 1
    assert parse_nel_header(over, origin=SITE, received_ip=IP, now=0.0) is None


@pytest.mark.unit
def test_nel_only_the_first_list_element_is_considered() -> None:
    good = '{"report_to": "a", "max_age": 60}'
    other = '{"report_to": "b", "max_age": 60}'
    first = parse_nel_header(f"{good}, {other}", origin=SITE, received_ip=IP, now=0.0)
    assert first is not None
    assert not isinstance(first, PolicyRemoval)
    assert first.report_to == "a"
    assert (
        parse_nel_header(f'{{"max_age": "x"}}, {good}', origin=SITE, received_ip=IP, now=0.0)
        is None
    )


@pytest.mark.unit
def test_nel_policy_from_insecure_origin_is_ignored() -> None:
    http_origin = Origin("http", "www.example.com")
    assert parse_nel_header(POLICY, origin=http_origin, received_ip=IP, now=0.0) is None


@pytest.mark.unit
def test_nel_include_subdomains_rejected_on_single_label_host() -> None:
    header = '{"report_to": "nel", "max_age": 60, "include_subdomains": true}'
    assert parse_nel_header(header, origin=Origin("https", "com"), received_ip=IP, now=0.0) is None
    assert parse_nel_header(header, origin=SITE, received_ip=IP, now=0.0) is not None


# ---------------------------------------------------------------- policy cache


@pytest.mark.unit
def test_policy_expiry_boundary_is_strictly_greater_than_max_age() -> None:
    cache = primed('{"report_to": "nel", "max_age": 100}', now=0.0)
    assert cache.choose(SITE, 100.0) is not None
    assert cache.choose(SITE, 100.001) is None


@pytest.mark.unit
def test_subdomain_uses_parent_policy_only_when_parent_includes_subdomains() -> None:
    sub = Origin("https", "api.eu.example.com")
    plain = PolicyCache()
    plain.process_header(
        '{"report_to": "nel", "max_age": 60}',
        origin=Origin("https", "example.com"),
        received_ip=IP,
        now=0.0,
    )
    assert plain.choose(sub, 1.0) is None
    wide = PolicyCache()
    wide.process_header(
        '{"report_to": "nel", "max_age": 60, "include_subdomains": true}',
        origin=Origin("https", "example.com"),
        received_ip=IP,
        now=0.0,
    )
    chosen = wide.choose(sub, 1.0)
    assert chosen is not None
    assert chosen.origin.host == "example.com"
    assert wide.choose(sub, 61.0) is None


@pytest.mark.unit
def test_expired_exact_policy_falls_through_to_unexpired_parent() -> None:
    cache = PolicyCache()
    parent = Origin("https", "example.com")
    cache.process_header(
        '{"report_to": "parent", "max_age": 1000, "include_subdomains": true}',
        origin=parent,
        received_ip=IP,
        now=0.0,
    )
    cache.process_header(
        '{"report_to": "child", "max_age": 10}', origin=SITE, received_ip=IP, now=0.0
    )
    early = cache.choose(SITE, 5.0)
    late = cache.choose(SITE, 50.0)
    assert early is not None
    assert early.report_to == "child"
    assert late is not None
    assert late.report_to == "parent"


# ---------------------------------------------------------------- report generation


@pytest.mark.unit
def test_success_is_not_reported_unless_success_fraction_is_set() -> None:
    quiet = primed('{"report_to": "nel", "max_age": 60}')
    ok = RequestOutcome("https://www.example.com/", status_code=200)
    assert generate_report(quiet, ok, now=1.0, roll=0.0) is None
    loud = primed(POLICY)
    report = generate_report(loud, ok, now=1.0, roll=0.0)
    assert report is not None
    assert (report.body["phase"], report.body["type"]) == ("application", "ok")


@pytest.mark.unit
def test_failure_is_reported_by_default_with_full_sampling() -> None:
    cache = primed('{"report_to": "nel", "max_age": 60}')
    report = generate_report(
        cache, RequestOutcome("https://www.example.com/", error=DNS_FAIL), now=1.0, roll=0.999
    )
    assert report is not None
    assert report.body["sampling_fraction"] == 1.0
    assert (report.body["phase"], report.body["type"]) == ("dns", "dns.name_not_resolved")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (399, "ok"),
        (400, "http.error"),
        (404, "http.error"),
        (503, "http.error"),
        (599, "http.error"),
    ],
)
def test_http_4xx_and_5xx_are_failures_for_sampling(status: int, expected: str) -> None:
    cache = primed('{"report_to": "nel", "max_age": 60, "success_fraction": 0.0}')
    report = generate_report(
        cache, RequestOutcome("https://www.example.com/", status_code=status), now=1.0, roll=0.5
    )
    if expected == "ok":
        assert report is None
    else:
        assert report is not None
        assert report.body["type"] == expected


@pytest.mark.unit
def test_sampling_boundaries_follow_chromium_not_the_literal_spec_roll() -> None:
    half = primed('{"report_to": "nel", "max_age": 60, "failure_fraction": 0.5}')
    bad = RequestOutcome("https://www.example.com/", error=DNS_FAIL)
    assert generate_report(half, bad, now=1.0, roll=0.499) is not None
    assert generate_report(half, bad, now=1.0, roll=0.5) is None
    zero = primed('{"report_to": "nel", "max_age": 60, "failure_fraction": 0.0}')
    assert generate_report(zero, bad, now=1.0, roll=0.0) is None


@pytest.mark.unit
def test_server_ip_change_downgrades_report_to_dns_address_changed() -> None:
    cache = primed()
    outcome = RequestOutcome(
        "https://www.example.com/x",
        status_code=200,
        server_ip="198.51.100.9",
        protocol="h2",
        elapsed_ms=321,
    )
    report = generate_report(cache, outcome, now=1.0, roll=0.0)
    assert report is not None
    assert report.body["phase"] == "dns"
    assert report.body["type"] == "dns.address_changed"
    assert report.body["status_code"] == 0
    assert report.body["elapsed_time"] == 0
    assert report.url == "https://www.example.com/"


@pytest.mark.unit
def test_matching_server_ip_keeps_application_phase_details() -> None:
    cache = primed()
    outcome = RequestOutcome(
        "https://www.example.com/x?q=1#frag", status_code=200, server_ip=IP, elapsed_ms=321
    )
    report = generate_report(cache, outcome, now=1.0, roll=0.0)
    assert report is not None
    assert report.body["status_code"] == 200
    assert report.body["elapsed_time"] == 321
    assert report.url == "https://www.example.com/x?q=1"


@pytest.mark.unit
def test_dns_and_connection_reports_strip_path_and_query() -> None:
    cache = primed()
    for error in (DNS_FAIL, NetworkError("connection", "tcp.timed_out")):
        report = generate_report(
            cache,
            RequestOutcome("https://www.example.com/secret/path?token=1", error=error),
            now=1.0,
            roll=0.0,
        )
        assert report is not None
        assert report.url == "https://www.example.com/"


@pytest.mark.unit
def test_subdomain_policy_reports_only_dns_failures_for_subdomains() -> None:
    cache = PolicyCache()
    cache.process_header(
        '{"report_to": "nel", "max_age": 60, "include_subdomains": true}',
        origin=Origin("https", "example.com"),
        received_ip=IP,
        now=0.0,
    )
    url = "https://new.example.com/"
    dns = generate_report(cache, RequestOutcome(url, error=DNS_FAIL), now=1.0, roll=0.0)
    tcp = generate_report(
        cache,
        RequestOutcome(url, error=NetworkError("connection", "tcp.refused")),
        now=1.0,
        roll=0.0,
    )
    assert dns is not None
    assert dns.body["type"] == "dns.name_not_resolved"
    assert tcp is None


@pytest.mark.unit
def test_stale_policy_still_reports_once_then_is_deleted() -> None:
    cache = primed('{"report_to": "nel", "max_age": 9999999}', now=0.0)
    outcome = RequestOutcome("https://www.example.com/", error=DNS_FAIL)
    assert generate_report(cache, outcome, now=172_800.0, roll=0.0) is not None
    assert len(cache) == 1
    assert generate_report(cache, outcome, now=172_800.5, roll=0.0) is not None
    assert len(cache) == 0
    assert generate_report(cache, outcome, now=172_801.0, roll=0.0) is None


@pytest.mark.unit
def test_no_report_for_insecure_request_or_unknown_origin() -> None:
    cache = primed()
    http_url = RequestOutcome("http://www.example.com/", error=DNS_FAIL)
    other = RequestOutcome("https://other.test/", error=DNS_FAIL)
    assert generate_report(cache, http_url, now=1.0, roll=0.0) is None
    assert generate_report(cache, other, now=1.0, roll=0.0) is None


# ---------------------------------------------------------------- Reporting groups


@pytest.mark.unit
def test_report_to_spec_example_parses_with_defaults() -> None:
    value = '{"group": "network-errors", "max_age": 2592000, "endpoints": [{"url": "https://example.com/upload-reports"}]}'
    result = parse_report_to(value, now=0.0)
    assert result is not None
    assert len(result.groups) == 1
    group = result.groups[0]
    assert (group.name, group.max_age, group.include_subdomains) == (
        "network-errors",
        2592000,
        False,
    )
    assert (group.endpoints[0].priority, group.endpoints[0].weight) == (1, 1)


@pytest.mark.unit
def test_report_to_default_group_name_duplicates_and_removal() -> None:
    value = (
        '{"max_age": 60, "endpoints": [{"url": "https://a.test/r"}]},'
        '{"max_age": 60, "endpoints": [{"url": "https://b.test/r"}]},'
        '{"group": "gone", "max_age": 0}'
    )
    result = parse_report_to(value, now=0.0)
    assert result is not None
    assert [g.name for g in result.groups] == ["default"]
    assert result.groups[0].endpoints[0].url == "https://a.test/r"
    assert result.removed == ["gone"]


@pytest.mark.unit
def test_report_to_drops_insecure_and_invalid_endpoints_and_empty_groups() -> None:
    value = (
        '{"group": "g", "max_age": 60, "endpoints": ['
        '{"url": "http://insecure.test/r"},'
        '{"url": "https://ok.test/r", "priority": -1},'
        '{"url": "https://ok.test/r", "weight": 1.5},'
        '{"url": "https://good.test/r", "priority": 2, "weight": 3}]},'
        '{"group": "empty", "max_age": 60, "endpoints": [{"url": "http://x.test/"}]}'
    )
    result = parse_report_to(value, now=0.0)
    assert result is not None
    assert [g.name for g in result.groups] == ["g"]
    assert [(e.url, e.priority, e.weight) for e in result.groups[0].endpoints] == [
        ("https://good.test/r", 2, 3)
    ]


@pytest.mark.unit
def test_choose_endpoint_uses_lowest_priority_class_then_fails_over() -> None:
    primary = Endpoint("https://p.test/r", priority=1)
    backup = Endpoint("https://b.test/r", priority=2)
    group = EndpointGroup("g", 600, False, 0.0, [backup, primary])
    assert choose_endpoint(group, 1.0, 0.0) is primary
    primary.retry_after = 100.0
    assert choose_endpoint(group, 1.0, 0.0) is backup
    assert choose_endpoint(group, 100.0, 0.0) is primary
    backup.retry_after = 500.0
    primary.retry_after = 500.0
    assert choose_endpoint(group, 1.0, 0.0) is None


@pytest.mark.unit
def test_choose_endpoint_weights_split_by_rand_and_expired_group_returns_none() -> None:
    light = Endpoint("https://l.test/r", weight=1)
    heavy = Endpoint("https://h.test/r", weight=3)
    group = EndpointGroup("g", 600, False, 0.0, [light, heavy])
    picks = [choose_endpoint(group, 1.0, r / 100) for r in range(100)]
    assert picks.count(light) == 25
    assert picks.count(heavy) == 75
    assert choose_endpoint(group, 601.0, 0.0) is None


# ---------------------------------------------------------------- delivery queue


def reporting_cache(*endpoints: Endpoint) -> ReportingCache:
    cache = ReportingCache()
    cache.groups["nel"] = EndpointGroup("nel", 86_400, False, 0.0, list(endpoints))
    return cache


class Recorder:
    def __init__(self, *statuses: int | None) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    async def __call__(self, url: str, payload: bytes) -> int | None:
        self.calls.append((url, json.loads(payload)))
        return self.statuses.pop(0) if self.statuses else 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delivery_posts_age_in_milliseconds_and_clears_queue_on_2xx() -> None:
    cache = reporting_cache(Endpoint("https://c.test/nel"))
    cache.enqueue(make_report(ts=10.0))
    send = Recorder(204)
    stats = await cache.deliver(100.0, send)
    assert stats.delivered == 1
    assert cache.queue == []
    url, payload = send.calls[0]
    assert url == "https://c.test/nel"
    assert payload[0]["age"] == 90_000
    assert payload[0]["type"] == "network-error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_upload_backs_off_exponentially_and_skips_pending_endpoint() -> None:
    endpoint = Endpoint("https://c.test/nel")
    cache = reporting_cache(endpoint)
    cache.enqueue(make_report(ts=0.0))
    send = Recorder(500, None, 503)
    await cache.deliver(0.0, send)
    assert endpoint.retry_after == 60.0
    await cache.deliver(30.0, send)
    assert len(send.calls) == 1
    await cache.deliver(60.0, send)
    assert endpoint.retry_after == 60.0 + 120.0
    await cache.deliver(180.0, send)
    assert endpoint.retry_after == 180.0 + 240.0
    assert endpoint.failures == 3
    assert len(cache.queue) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_410_removes_endpoint_and_keeps_reports_for_the_backup() -> None:
    primary = Endpoint("https://p.test/nel", priority=1)
    backup = Endpoint("https://b.test/nel", priority=2)
    cache = reporting_cache(primary, backup)
    cache.enqueue(make_report(ts=0.0))
    send = Recorder(410, 200)
    first = await cache.deliver(1.0, send)
    assert first.removed_endpoints == 1
    assert len(cache.queue) == 1
    second = await cache.deliver(2.0, send)
    assert second.delivered == 1
    assert cache.queue == []
    assert [c[0] for c in send.calls] == ["https://p.test/nel", "https://b.test/nel"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reports_expire_after_fifteen_minutes_while_collector_is_unreachable() -> None:
    cache = reporting_cache(Endpoint("https://c.test/nel"))
    cache.enqueue(make_report(ts=0.0))
    send = Recorder(None)
    await cache.deliver(MAX_REPORT_AGE_SECONDS - 1, send)
    assert len(cache.queue) == 1
    stats = await cache.deliver(MAX_REPORT_AGE_SECONDS, Recorder(200))
    assert stats.dropped == 1
    assert stats.delivered == 0
    assert cache.queue == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_is_dropped_after_five_attempts() -> None:
    endpoint = Endpoint("https://c.test/nel")
    cache = reporting_cache(endpoint)
    cache.enqueue(make_report(ts=0.0))
    send = Recorder(*([500] * 10))
    for attempt in range(5):
        endpoint.retry_after = None
        await cache.deliver(float(attempt), send)
    assert len(send.calls) == 5
    assert len(cache.queue) == 1
    endpoint.retry_after = None
    stats = await cache.deliver(6.0, send)
    assert stats.dropped == 1
    assert len(send.calls) == 5
    assert cache.queue == []


@pytest.mark.unit
def test_queue_is_capped_and_evicts_the_oldest_report() -> None:
    cache = ReportingCache()
    for i in range(MAX_REPORT_COUNT + 1):
        cache.enqueue(make_report(ts=float(i)))
    assert len(cache.queue) == MAX_REPORT_COUNT
    assert cache.queue[0].report.timestamp == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_report_without_matching_group_is_never_sent() -> None:
    cache = reporting_cache(Endpoint("https://c.test/nel"))
    cache.enqueue(make_report(group="other"))
    send = Recorder()
    stats = await cache.deliver(1.0, send)
    assert send.calls == []
    assert stats.delivered == 0
    assert len(cache.queue) == 1


# ---------------------------------------------------------------- collector


def valid_item(**body: object) -> dict[str, object]:
    merged: dict[str, object] = {
        "sampling_fraction": 1.0,
        "phase": "dns",
        "type": "dns.name_not_resolved",
        "status_code": 0,
    }
    merged.update(body)
    return {"age": 2000, "type": "network-error", "url": "https://www.example.com/", "body": merged}


def collector(clock: FakeClock, store: ReportStore | None = None) -> tuple[Starlette, ReportStore]:
    target = store if store is not None else ReportStore()
    return create_collector_app(target, clock), target


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_accepts_batch_and_derives_event_time_from_age_not_client_clock() -> None:
    app, store = collector(FakeClock(5000.0))
    async with client_for(app) as http:
        resp = await http.post(
            "/nel",
            content=json.dumps([valid_item()]),
            headers={
                "content-type": f"{REPORTS_CONTENT_TYPE}; charset=utf-8",
                "x-lab-client-region": "eu",
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "rejected": 0}
    assert store.window("eu", 4998.0, 4999.0).reports == 1
    assert store.window("eu", 4999.0, 6000.0).reports == 0
    assert store.window("eu", 0.0, 4998.0).reports == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("media_type", ["application/json", "text/plain", ""])
async def test_collector_rejects_other_media_types_with_415(media_type: str) -> None:
    app, store = collector(FakeClock())
    async with client_for(app) as http:
        resp = await http.post("/nel", content="[]", headers={"content-type": media_type})
    assert resp.status_code == 415
    assert len(store) == 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["{}", "not json", '[{"x": NaN}]', '"str"'])
async def test_collector_rejects_malformed_payloads_with_400(body: str) -> None:
    app, store = collector(FakeClock())
    async with client_for(app) as http:
        resp = await http.post("/nel", content=body, headers={"content-type": REPORTS_CONTENT_TYPE})
    assert resp.status_code == 400
    assert len(store) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_validates_each_report_independently() -> None:
    batch: list[dict[str, object]] = [
        valid_item(),
        valid_item(sampling_fraction=0.0),
        valid_item(phase="bogus"),
        valid_item(status_code="200"),
        {"age": -1, "type": "network-error", "url": "u", "body": {}},
        {"age": 1, "type": "csp-violation", "url": "u", "body": {}},
        valid_item(sampling_fraction=0.25, type="ok", phase="application", status_code=200),
    ]
    app, store = collector(FakeClock())
    async with client_for(app) as http:
        resp = await http.post(
            "/nel", content=json.dumps(batch), headers={"content-type": REPORTS_CONTENT_TYPE}
        )
    assert resp.json() == {"accepted": 2, "rejected": 5}
    assert len(store) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_rejects_oversized_body_with_413() -> None:
    store = ReportStore()
    app = create_collector_app(store, FakeClock(), max_body_bytes=100)
    async with client_for(app) as http:
        resp = await http.post(
            "/nel",
            content=json.dumps([valid_item()] * 3),
            headers={"content-type": REPORTS_CONTENT_TYPE},
        )
    assert resp.status_code == 413
    assert len(store) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_preflight_satisfies_the_chromium_acceptance_check() -> None:
    app, _ = collector(FakeClock())
    async with client_for(app) as http:
        options = await http.options("/nel")
        get = await http.get("/nel")
    assert chromium_preflight_accepts(
        options.status_code, dict(options.headers), "https://www.example.com"
    )
    assert get.status_code == 405


@pytest.mark.unit
def test_chromium_preflight_check_rejects_missing_or_wrong_headers() -> None:
    good = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Content-Type"}
    origin = "https://www.example.com"
    assert chromium_preflight_accepts(200, good, origin)
    assert not chromium_preflight_accepts(405, good, origin)
    assert not chromium_preflight_accepts(204, {"Access-Control-Allow-Origin": "*"}, origin)
    assert not chromium_preflight_accepts(
        204, {**good, "Access-Control-Allow-Origin": "https://evil.test"}, origin
    )
    assert chromium_preflight_accepts(
        204, {**good, "Access-Control-Allow-Origin": "HTTPS://WWW.EXAMPLE.COM"}, origin
    )


# ---------------------------------------------------------------- client SLI


def ingest(
    store: ReportStore, region: str, items: list[dict[str, object]], now: float = 1000.0
) -> None:
    from api_labs.f18_observability_profiling_and_operations.client_side_telemetry import (
        parse_reports,
    )

    accepted, rejected = parse_reports(items, received_at=now, region=region)
    assert rejected == 0
    store.add(accepted)


@pytest.mark.unit
def test_inverse_probability_weighting_corrects_the_success_sampling_bias() -> None:
    store = ReportStore()
    ok = valid_item(sampling_fraction=0.1, type="ok", phase="application", status_code=200)
    bad = valid_item(sampling_fraction=1.0, type="tcp.reset", phase="connection")
    ingest(store, "us", [ok] * 10 + [bad] * 10)
    weighted = store.window("us", 0.0, 2000.0)
    raw = store.window("us", 0.0, 2000.0, weighted=False)
    assert raw.sli == pytest.approx(0.5)
    assert weighted.sli == pytest.approx(100 / 110)


@pytest.mark.unit
def test_no_reports_means_unknown_not_perfect() -> None:
    assert ReportStore().window("us", 0.0, 10.0).sli is None


@pytest.mark.unit
def test_http_4xx_is_good_by_default_and_bad_in_strict_mode() -> None:
    store = ReportStore()
    four = valid_item(type="http.error", phase="application", status_code=404)
    five = valid_item(type="http.error", phase="application", status_code=503)
    ingest(store, "us", [four, five])
    assert store.window("us", 0.0, 2000.0).sli == pytest.approx(0.5)
    assert store.window("us", 0.0, 2000.0, strict_4xx=True).sli == 0.0


@pytest.mark.unit
def test_sli_window_is_half_open_and_region_scoped() -> None:
    store = ReportStore()
    ingest(store, "us", [valid_item()], now=1000.0)
    ingest(store, "eu", [valid_item(type="ok", phase="application")], now=1000.0)
    event_time = 1000.0 - 2.0
    assert store.window("us", event_time, event_time + 1).reports == 1
    assert store.window("us", event_time + 0.0001, event_time + 1).reports == 0
    assert store.window("us", event_time - 1, event_time).reports == 0
    assert store.window("eu", 0.0, 2000.0).sli == 1.0
    assert store.failure_types("us", 0.0, 2000.0) == {"dns:dns.name_not_resolved": 1}


# ---------------------------------------------------------------- edge-versus-origin gap


@pytest.mark.unit
def test_gap_flags_region_where_users_fail_but_origin_looks_healthy() -> None:
    store = ReportStore()
    ingest(store, "eu", [valid_item()] * 30)
    verdicts = {
        v.region: v
        for v in edge_origin_gap(
            origin={"eu": OriginWindow(200, 0), "us": OriginWindow(1000, 0)},
            baseline_requests={"eu": 1000, "us": 1000},
            store=store,
            start=0.0,
            end=2000.0,
        )
    }
    assert verdicts["eu"].blind_spot is True
    assert verdicts["eu"].origin_sli == 1.0
    assert verdicts["eu"].client_sli == 0.0
    assert verdicts["eu"].volume_ratio == pytest.approx(0.2)
    assert verdicts["us"].blind_spot is False
    assert verdicts["us"].client_sli is None


@pytest.mark.unit
def test_gap_is_not_blind_when_origin_also_burns_or_samples_are_too_few() -> None:
    store = ReportStore()
    ingest(store, "eu", [valid_item()] * 30)
    ingest(store, "us", [valid_item()] * 5)
    result = {
        v.region: v
        for v in edge_origin_gap(
            origin={"eu": OriginWindow(100, 50), "us": OriginWindow(100, 0)},
            baseline_requests={},
            store=store,
            start=0.0,
            end=2000.0,
        )
    }
    assert result["eu"].blind_spot is False
    assert result["us"].blind_spot is False
    assert result["us"].volume_ratio is None


# ---------------------------------------------------------------- canary


@pytest.mark.unit
def test_probe_stops_at_the_first_failing_phase_and_sums_stage_latency() -> None:
    net = SimulatedNetwork()
    ok = net.probe("eu", "https://www.example.com/health")
    assert (ok.ok, ok.elapsed_ms) == (True, 110)
    net.inject("eu", "www.example.com", Fault("connection", "tls.cert.date_invalid"))
    bad = net.probe("eu", "https://www.example.com/health")
    assert (bad.phase, bad.type, bad.elapsed_ms) == ("connection", "tls.cert.date_invalid", 80)
    net.inject("eu", "www.example.com", Fault("dns", "dns.name_not_resolved"))
    dns = net.probe("eu", "https://www.example.com/health")
    assert (dns.phase, dns.elapsed_ms) == ("dns", 20)
    assert net.probe("us", "https://www.example.com/health").ok


@pytest.mark.unit
def test_timeout_faults_cost_the_probe_timeout() -> None:
    net = SimulatedNetwork()
    net.inject("eu", "www.example.com", Fault("connection", "tcp.timed_out"))
    assert net.probe("eu", "https://www.example.com/").elapsed_ms == 20 + 3000


@pytest.mark.unit
def test_clear_removes_every_fault_for_the_region_and_host() -> None:
    net = SimulatedNetwork()
    net.inject("eu", "www.example.com", Fault("dns", "dns.failed"))
    net.inject("eu", "www.example.com", Fault("application", "http.error", 503))
    net.clear("eu", "www.example.com")
    assert net.probe("eu", "https://www.example.com/").ok


def probe_result(region: str, ok: bool, phase: str = "dns") -> ProbeResult:
    return ProbeResult(region, ok, phase if not ok else "application", "x" if not ok else "ok", 1)


@pytest.mark.unit
def test_canary_opens_only_after_consecutive_failures_and_a_success_resets() -> None:
    runner = CanaryRunner(fail_threshold=3, recover_threshold=2)
    assert runner.observe(probe_result("eu", False)) is None
    assert runner.state("eu") == "suspect"
    assert runner.observe(probe_result("eu", True)) is None
    assert runner.state("eu") == "healthy"
    assert [runner.observe(probe_result("eu", False)) for _ in range(3)] == [None, None, "opened"]
    assert runner.observe(probe_result("eu", False)) is None
    assert runner.state("eu") == "failing"


@pytest.mark.unit
def test_canary_recovery_needs_consecutive_successes_and_a_failure_restarts_it() -> None:
    runner = CanaryRunner(fail_threshold=2, recover_threshold=2)
    runner.observe(probe_result("eu", False))
    assert runner.observe(probe_result("eu", False)) == "opened"
    assert runner.observe(probe_result("eu", True)) is None
    assert runner.observe(probe_result("eu", False)) is None
    assert runner.state("eu") == "failing"
    assert runner.observe(probe_result("eu", True)) is None
    assert runner.observe(probe_result("eu", True)) == "closed"
    assert runner.state("eu") == "healthy"


@pytest.mark.unit
def test_canary_classifies_regional_versus_global_failure() -> None:
    regions = ["us", "eu", "ap"]
    runner = CanaryRunner(fail_threshold=1)
    assert runner.classify(regions) == "none"
    runner.observe(probe_result("eu", False))
    assert runner.classify(regions) == "regional:eu"
    for region in ("us", "ap"):
        runner.observe(probe_result(region, False))
    assert runner.classify(regions) == "global"
    mixed = CanaryRunner(fail_threshold=1)
    mixed.observe(probe_result("us", False, "dns"))
    mixed.observe(probe_result("eu", False, "connection"))
    assert mixed.classify(["us", "eu"]) == "regional:eu,us"


@pytest.mark.unit
def test_detection_delay_is_interval_times_threshold_minus_one() -> None:
    assert detection_delay_seconds(60.0, 3) == 120.0
    assert detection_delay_seconds(30.0, 1) == 0.0


@pytest.mark.unit
def test_canary_vantage_points_do_not_see_an_isp_specific_fault() -> None:
    net = SimulatedNetwork()
    net.inject("isp-x-residential", "www.example.com", Fault("connection", "tcp.reset"))
    runner = CanaryRunner(fail_threshold=1)
    regions = ["us-east", "eu-west"]
    for _ in range(3):
        for region in regions:
            runner.observe(net.probe(region, "https://www.example.com/"))
    assert runner.classify(regions) == "none"
    assert not net.probe("isp-x-residential", "https://www.example.com/").ok


# ---------------------------------------------------------------- end to end


class Rolls:
    """Evenly spaced rolls in [0, 1): exactly `rate` of them fall below `rate`."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.i = 0

    def next(self) -> float:
        value = (self.i % self.n + 0.5) / self.n
        self.i += 1
        return value


async def run_fleet(*, collector_url: str, store: ReportStore) -> None:
    """100 returning clients per region; eu-west cannot resolve www.example.com."""
    app = create_collector_app(store, FakeClock(2000.0))
    header = '{"report_to": "nel", "max_age": 2592000, "success_fraction": 0.1}'
    report_to = (
        f'{{"group": "nel", "max_age": 2592000, "endpoints": [{{"url": "{collector_url}"}}]}}'
    )
    rolls = Rolls(100)
    async with client_for(app) as http:
        for region in ("us-east", "eu-west"):

            async def send(url: str, payload: bytes, region: str = region) -> int | None:
                if Origin.parse(url).host == "www.example.com" and region == "eu-west":
                    return None
                resp = await http.post(
                    "/nel",
                    content=payload,
                    headers={"content-type": REPORTS_CONTENT_TYPE, "x-lab-client-region": region},
                )
                return resp.status_code

            for _ in range(100):
                policies = primed(header)
                outcome = (
                    RequestOutcome("https://www.example.com/", error=DNS_FAIL)
                    if region == "eu-west"
                    else RequestOutcome("https://www.example.com/", status_code=200, server_ip=IP)
                )
                report = generate_report(policies, outcome, now=1990.0, roll=rolls.next())
                if report is None:
                    continue
                reporting = ReportingCache()
                parsed = parse_report_to(report_to, now=1990.0)
                assert parsed is not None
                reporting.apply_report_to(parsed)
                reporting.enqueue(report)
                await reporting.deliver(2000.0, send)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_regional_dns_blackhole_is_invisible_to_origin_but_visible_to_nel() -> None:
    store = ReportStore()
    await run_fleet(collector_url="https://reports.example.net/nel", store=store)
    verdicts = {
        v.region: v
        for v in edge_origin_gap(
            origin={"us-east": OriginWindow(100, 0), "eu-west": OriginWindow(0, 0)},
            baseline_requests={"us-east": 100, "eu-west": 100},
            store=store,
            start=0.0,
            end=3000.0,
        )
    }
    assert store.window("us-east", 0.0, 3000.0).reports == 10
    assert store.window("eu-west", 0.0, 3000.0).reports == 100
    assert verdicts["us-east"].client_sli == 1.0
    assert verdicts["us-east"].blind_spot is False
    assert verdicts["eu-west"].origin_sli is None
    assert verdicts["eu-west"].client_sli == 0.0
    assert verdicts["eu-west"].volume_ratio == 0.0
    assert verdicts["eu-west"].blind_spot is True
    assert store.failure_types("eu-west", 0.0, 3000.0) == {"dns:dns.name_not_resolved": 100}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collector_on_the_failing_origin_leaves_the_blackhole_unreported() -> None:
    store = ReportStore()
    await run_fleet(collector_url="https://www.example.com/nel", store=store)
    assert store.window("eu-west", 0.0, 3000.0).reports == 0
    assert store.window("us-east", 0.0, 3000.0).reports == 10
    verdict = edge_origin_gap(
        origin={"eu-west": OriginWindow(0, 0)},
        baseline_requests={"eu-west": 100},
        store=store,
        start=0.0,
        end=3000.0,
    )[0]
    assert verdict.client_sli is None
    assert verdict.blind_spot is False


@pytest.mark.unit
def test_canary_detects_the_blackhole_that_origin_5xx_counters_never_see() -> None:
    net = SimulatedNetwork()
    runner = CanaryRunner(fail_threshold=3)
    regions = ["us-east", "eu-west", "ap-south"]
    net.inject("eu-west", "www.example.com", Fault("dns", "dns.name_not_resolved"))
    events: list[tuple[int, str, str]] = []
    for round_index in range(4):
        for region in regions:
            event = runner.observe(net.probe(region, "https://www.example.com/health"))
            if event is not None:
                events.append((round_index * 60, region, event))
    assert events == [(120, "eu-west", "opened")]
    assert runner.classify(regions) == "regional:eu-west"
    assert events[0][0] == detection_delay_seconds(60.0, 3)
