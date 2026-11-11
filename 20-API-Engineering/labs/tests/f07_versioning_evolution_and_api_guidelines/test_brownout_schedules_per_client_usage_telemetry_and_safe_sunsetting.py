"""Brownout windows, per-tenant sunset telemetry, and 410 Gone cutover as ASGI middleware."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import sysconfig
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo

import httpx
import hypercorn.asyncio.run
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from granian.constants import Interfaces
from granian.server.embed import Server as GranianServer
from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.config import Config as HypercornConfig
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f07_versioning_evolution_and_api_guidelines.brownout_sunset import (
    OVERFLOW_TENANT,
    PROBLEM_TYPE,
    Outcome,
    Phase,
    SunsetMiddleware,
    SunsetPolicy,
    SunsetTelemetry,
    Window,
    hourly_windows,
    http_date,
    sf_date,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from aioquic.quic.events import QuicEvent
    from hypercorn.typing import ASGIFramework
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEPRECATION = datetime(2027, 1, 1, tzinfo=UTC)
SUNSET = datetime(2027, 4, 1, tzinfo=UTC)
BROWNOUT_DAY = datetime(2027, 3, 1, tzinfo=UTC)
POLICY_URL = "https://developer.example.com/v1-retirement"
SF_DATE = re.compile(r"^@-?[0-9]{1,15}$")  # RFC 9651 Appendix C: sf-date = "@" sf-integer
LINK = (
    f'<{POLICY_URL}>; rel="deprecation"; type="text/html", '
    f'<{POLICY_URL}>; rel="sunset"; type="text/html"'
)
SIGNALS = {
    "deprecation": "@1798761600",
    "sunset": "Thu, 01 Apr 2027 00:00:00 GMT",
    "link": LINK,
}
PLUS_TWO = timezone(timedelta(hours=2))


@dataclass
class FakeClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Harness:
    clock: FakeClock
    telemetry: SunsetTelemetry
    inner_hits: list[str] = field(default_factory=list[str])
    http_versions: list[str] = field(default_factory=list[str])


def make_policy(**overrides: object) -> SunsetPolicy:
    kwargs: dict[str, object] = {
        "path_prefix": "/v1/",
        "deprecation": DEPRECATION,
        "sunset": SUNSET,
        "windows": hourly_windows(
            BROWNOUT_DAY,
            BROWNOUT_DAY + timedelta(days=1),
            minute=0,
            duration=timedelta(minutes=15),
        ),
        "policy_url": POLICY_URL,
        "waivers": {"acme": SUNSET - timedelta(days=7)},
    }
    kwargs.update(overrides)
    return SunsetPolicy(**kwargs)  # type: ignore[arg-type]


def tenant_from_header(scope: Scope) -> str | None:
    # Lab shortcut: production code takes the tenant from the authenticated principal.
    for key, value in scope["headers"]:
        if key == b"tenant-id":
            return bytes(value).decode("latin-1")
    return None


def build_app(
    at: datetime, *, max_tenants: int = 1000, **policy_overrides: object
) -> tuple[SunsetMiddleware, Harness]:
    harness = Harness(clock=FakeClock(at), telemetry=SunsetTelemetry(max_tenants=max_tenants))

    async def items(request: Request) -> JSONResponse:
        harness.inner_hits.append(request.url.path)
        return JSONResponse({"items": []})

    def tenant_of(scope: Scope) -> str | None:
        harness.http_versions.append(scope["http_version"])
        return tenant_from_header(scope)

    app = Starlette(
        routes=[Route(path, items) for path in ("/v1/items", "/v2/items", "/v10/items")]
    )
    wrapped = SunsetMiddleware(
        app,
        policy=make_policy(**policy_overrides),
        telemetry=harness.telemetry,
        tenant_of=tenant_of,
        clock=harness.clock,
    )
    return wrapped, harness


def build(
    at: datetime, *, max_tenants: int = 1000, **policy_overrides: object
) -> tuple[httpx.AsyncClient, Harness]:
    wrapped, harness = build_app(at, max_tenants=max_tenants, **policy_overrides)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://test")
    return client, harness


@pytest.mark.unit
@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (DEPRECATION - timedelta(microseconds=1), Phase.ACTIVE),
        (DEPRECATION, Phase.DEPRECATED),
        (BROWNOUT_DAY - timedelta(microseconds=1), Phase.DEPRECATED),
        (BROWNOUT_DAY, Phase.BROWNOUT),
        (BROWNOUT_DAY + timedelta(minutes=15) - timedelta(microseconds=1), Phase.BROWNOUT),
        (BROWNOUT_DAY + timedelta(minutes=15), Phase.DEPRECATED),
        (BROWNOUT_DAY + timedelta(hours=23, minutes=14), Phase.BROWNOUT),
        (BROWNOUT_DAY + timedelta(days=1), Phase.DEPRECATED),
        (SUNSET - timedelta(microseconds=1), Phase.DEPRECATED),
        (SUNSET, Phase.SUNSET),
    ],
    ids=[
        "before-deprecation",
        "at-deprecation",
        "before-first-window",
        "window-start-inclusive",
        "window-last-microsecond",
        "window-end-exclusive",
        "last-window",
        "day-after-brownouts",
        "just-before-sunset",
        "at-sunset",
    ],
)
def test_phase_boundaries_are_half_open(at: datetime, expected: Phase) -> None:
    assert make_policy().phase(at) is expected


@pytest.mark.unit
def test_hourly_windows_cover_exactly_a_quarter_of_each_hour() -> None:
    windows = hourly_windows(
        BROWNOUT_DAY, BROWNOUT_DAY + timedelta(days=1), minute=0, duration=timedelta(minutes=15)
    )
    covered = sum((w.end - w.start for w in windows), timedelta(0))
    assert len(windows) == 24
    assert covered == timedelta(hours=6)
    assert covered / timedelta(days=1) == 0.25
    assert windows[0] == Window(BROWNOUT_DAY, BROWNOUT_DAY + timedelta(minutes=15))
    offset = hourly_windows(
        BROWNOUT_DAY + timedelta(minutes=20),
        BROWNOUT_DAY + timedelta(hours=2),
        minute=10,
        duration=timedelta(minutes=5),
    )
    assert [w.start.minute for w in offset] == [10]
    assert offset[0].start == BROWNOUT_DAY + timedelta(hours=1, minutes=10)
    with pytest.raises(ValueError, match="inside its own hour"):
        hourly_windows(BROWNOUT_DAY, SUNSET, minute=50, duration=timedelta(minutes=15))


@pytest.mark.unit
def test_bisect_window_lookup_matches_arithmetic_reference() -> None:
    windows = hourly_windows(DEPRECATION, SUNSET, minute=0, duration=timedelta(minutes=15))
    policy = make_policy(windows=windows)
    assert len(windows) == 2160
    probe = DEPRECATION
    checked = 0
    while probe < SUNSET:
        # Independent reference: the schedule is "HH:00 to HH:15 every hour".
        hour = probe.replace(minute=0, second=0, microsecond=0)
        expected = Window(hour, hour + timedelta(minutes=15)) if probe.minute < 15 else None
        assert policy.window_at(probe) == expected, probe.isoformat()
        probe += timedelta(minutes=7, seconds=30)
        checked += 1
    assert checked == 17280
    assert policy.window_at(DEPRECATION - timedelta(seconds=1)) is None


@pytest.mark.unit
def test_policy_rejects_inconsistent_retirement_contracts() -> None:
    with pytest.raises(ValueError, match="earlier than deprecation"):
        make_policy(sunset=DEPRECATION - timedelta(seconds=1), windows=(), waivers={})
    with pytest.raises(ValueError, match="overlaps or leaves range"):
        make_policy(windows=(Window(SUNSET - timedelta(minutes=5), SUNSET + timedelta(minutes=5)),))
    with pytest.raises(ValueError, match="overlaps or leaves range"):
        make_policy(
            windows=(
                Window(BROWNOUT_DAY, BROWNOUT_DAY + timedelta(hours=2)),
                Window(BROWNOUT_DAY + timedelta(hours=1), BROWNOUT_DAY + timedelta(hours=3)),
            )
        )
    with pytest.raises(ValueError, match="outlives the sunset"):
        make_policy(waivers={"acme": SUNSET + timedelta(days=1)})
    with pytest.raises(ValueError, match="aware UTC"):
        make_policy(deprecation=datetime(2027, 1, 1))  # naive on purpose
    with pytest.raises(ValueError, match="aware UTC"):
        make_policy().phase(datetime(2027, 3, 1, tzinfo=timezone(timedelta(hours=2))))
    with pytest.raises(ValueError, match="look like"):
        make_policy(path_prefix="/v1")
    with pytest.raises(ValueError, match="empty window"):
        Window(BROWNOUT_DAY, BROWNOUT_DAY)
    with pytest.raises(ValueError, match="overlaps or leaves range"):
        make_policy(windows=(Window(DEPRECATION - timedelta(hours=1), DEPRECATION),))
    with pytest.raises(ValueError, match="look like"):
        make_policy(path_prefix="v1/")


@pytest.mark.unit
@pytest.mark.parametrize(
    "construct",
    [
        lambda: Window(BROWNOUT_DAY.astimezone(PLUS_TWO), BROWNOUT_DAY + timedelta(minutes=15)),
        lambda: Window(BROWNOUT_DAY, (BROWNOUT_DAY + timedelta(minutes=15)).astimezone(PLUS_TWO)),
        lambda: make_policy(waivers={"acme": (SUNSET - timedelta(days=7)).astimezone(PLUS_TWO)}),
        lambda: hourly_windows(
            BROWNOUT_DAY.astimezone(PLUS_TWO), SUNSET, minute=0, duration=timedelta(minutes=15)
        ),
        lambda: hourly_windows(
            BROWNOUT_DAY, SUNSET.astimezone(PLUS_TWO), minute=0, duration=timedelta(minutes=15)
        ),
        lambda: sf_date(DEPRECATION.astimezone(PLUS_TWO)),
        lambda: sf_date(datetime(2027, 1, 1)),  # naive on purpose
    ],
    ids=[
        "window-start",
        "window-end",
        "waiver-expiry",
        "hourly-first",
        "hourly-until",
        "sf-date-offset",
        "sf-date-naive",
    ],
)
def test_every_instant_must_be_an_aware_utc_datetime(construct: Callable[[], object]) -> None:
    # The same instant in +02:00 is rejected: one zone keeps comparisons and rendering unambiguous.
    with pytest.raises(ValueError, match="aware UTC"):
        construct()


@pytest.mark.unit
def test_policy_accepts_the_inclusive_edges_of_a_contract() -> None:
    # RFC 9745 Section 4 forbids only a Sunset earlier than the Deprecation; equal is legal.
    same_instant = make_policy(sunset=DEPRECATION, windows=(), waivers={})
    assert same_instant.phase(DEPRECATION - timedelta(microseconds=1)) is Phase.ACTIVE
    assert same_instant.phase(DEPRECATION) is Phase.SUNSET
    last_hour = Window(SUNSET - timedelta(hours=1), SUNSET)
    until_the_end = make_policy(windows=(last_hour,), waivers={"acme": SUNSET})
    assert until_the_end.phase(SUNSET - timedelta(microseconds=1)) is Phase.BROWNOUT
    assert until_the_end.phase(SUNSET) is Phase.SUNSET
    assert until_the_end.waivers == {"acme": SUNSET}


@pytest.mark.unit
def test_back_to_back_windows_tile_time_without_a_gap_or_a_shared_instant() -> None:
    seam = BROWNOUT_DAY + timedelta(hours=1)
    first = Window(BROWNOUT_DAY, seam)
    second = Window(seam, seam + timedelta(hours=1))
    policy = make_policy(windows=(first, second))
    assert policy.window_at(seam - timedelta(microseconds=1)) == first
    assert policy.window_at(seam) == second
    assert policy.phase(seam) is Phase.BROWNOUT
    assert policy.phase(second.end) is Phase.DEPRECATED
    # hourly_windows may fill its hour exactly, which produces the same tiling.
    full_hours = hourly_windows(
        BROWNOUT_DAY, BROWNOUT_DAY + timedelta(hours=2), minute=0, duration=timedelta(hours=1)
    )
    assert full_hours == (first, second)
    last_quarter = hourly_windows(BROWNOUT_DAY, seam, minute=45, duration=timedelta(minutes=15))
    assert last_quarter == (Window(seam - timedelta(minutes=15), seam),)
    with pytest.raises(ValueError, match="inside its own hour"):
        hourly_windows(BROWNOUT_DAY, seam, minute=-1, duration=timedelta(minutes=15))


@pytest.mark.unit
def test_sf_date_is_integer_seconds_from_the_unix_epoch() -> None:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert sf_date(epoch) == "@0"
    assert sf_date(epoch - timedelta(seconds=1)) == "@-1"
    assert sf_date(DEPRECATION) == "@1798761600"
    assert SF_DATE.match(sf_date(datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)))


@pytest.mark.unit
def test_sunset_http_date_requires_utc_normalization() -> None:
    zoned = datetime(2027, 4, 1, tzinfo=ZoneInfo("UTC"))
    with pytest.raises(ValueError, match="requires a UTC datetime"):
        format_datetime(zoned, usegmt=True)
    assert http_date(zoned) == "Thu, 01 Apr 2027 00:00:00 GMT"
    tokyo = datetime(2027, 4, 1, 9, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert http_date(tokyo) == "Thu, 01 Apr 2027 00:00:00 GMT"
    with pytest.raises(ValueError, match="aware datetime"):
        http_date(datetime(2027, 4, 1))  # naive on purpose


@pytest.mark.unit
@pytest.mark.asyncio
async def test_active_phase_passes_through_without_signals() -> None:
    client, harness = build(DEPRECATION - timedelta(days=1))
    async with client:
        response = await client.get("/v1/items", headers={"tenant-id": "acme"})
    assert response.status_code == 200
    assert "deprecation" not in response.headers
    assert "sunset" not in response.headers
    assert harness.telemetry.counts["acme", Outcome.SERVED] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deprecated_phase_serves_with_rfc9745_and_rfc8594_signals() -> None:
    client, harness = build(DEPRECATION + timedelta(days=10))
    async with client:
        response = await client.get("/v1/items", headers={"tenant-id": "acme"})
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert response.headers["deprecation"] == "@1798761600"
    assert SF_DATE.match(response.headers["deprecation"])
    assert response.headers["sunset"] == "Thu, 01 Apr 2027 00:00:00 GMT"
    assert response.headers["link"] == LINK
    assert harness.inner_hits == ["/v1/items"]
    assert harness.telemetry.counts["acme", Outcome.SERVED_DEPRECATED] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signals_replace_values_the_application_already_set() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": (
                    (b"sunset", b"Fri, 01 Jan 2038 00:00:00 GMT"),
                    (b"content-length", b"0"),
                    (b"sunset", b"Sat, 02 Jan 2038 00:00:00 GMT"),
                ),
            }
        )
        await send({"type": "http.response.body", "body": b""})

    middleware = SunsetMiddleware(
        inner,
        policy=make_policy(),
        telemetry=SunsetTelemetry(),
        tenant_of=tenant_from_header,
        clock=FakeClock(DEPRECATION + timedelta(days=1)),
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware({"type": "http", "path": "/v1/items", "headers": []}, receive, send)
    # Exact order on purpose: this pins the Starlette 1.10.0 MutableHeaders.__setitem__ semantics
    # the note cites (first match replaced in place, duplicates removed, new names appended).
    assert sent[0]["headers"] == [
        (b"sunset", b"Thu, 01 Apr 2027 00:00:00 GMT"),
        (b"content-length", b"0"),
        (b"deprecation", b"@1798761600"),
        (b"link", LINK.encode("latin-1")),
    ]
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signals_are_added_to_the_response_start_message_only() -> None:
    trailers = [(b"server-timing", b"app;dur=1")]

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [], "trailers": True})
        await send({"type": "http.response.body", "body": b"ok"})
        await send({"type": "http.response.trailers", "headers": list(trailers)})

    middleware = SunsetMiddleware(
        inner,
        policy=make_policy(),
        telemetry=SunsetTelemetry(),
        tenant_of=tenant_from_header,
        clock=FakeClock(DEPRECATION + timedelta(days=1)),
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware({"type": "http", "path": "/v1/items", "headers": []}, receive, send)
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.trailers",
    ]
    assert [name for name, _ in sent[0]["headers"]] == [b"deprecation", b"sunset", b"link"]
    assert sent[1] == {"type": "http.response.body", "body": b"ok"}
    assert sent[2] == {"type": "http.response.trailers", "headers": trailers}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_brownout_window_returns_uncacheable_410_problem_details() -> None:
    at = BROWNOUT_DAY + timedelta(hours=3, minutes=5)
    client, harness = build(at)
    async with client:
        response = await client.get("/v1/items", headers={"tenant-id": "globex"})
    assert response.status_code == 410
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["deprecation"] == "@1798761600"
    assert "retry-after" not in response.headers
    problem = response.json()
    assert problem == {
        "type": PROBLEM_TYPE,
        "title": "API version retired",
        "status": 410,
        "detail": f"/v1/ is retired; see {POLICY_URL}",
        "phase": "brownout",
        "sunset": "2027-04-01T00:00:00+00:00",
        "brownout_ends": "2027-03-01T03:15:00+00:00",
    }
    assert problem["status"] == response.status_code  # RFC 9457 Section 3.1.2
    assert harness.inner_hits == []
    assert harness.telemetry.counts["globex", Outcome.BROWNOUT_REJECTED] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_window_edge_instants_over_http() -> None:
    end = BROWNOUT_DAY + timedelta(hours=3, minutes=15)
    client, harness = build(end - timedelta(microseconds=1))
    async with client:
        last_inside = await client.get("/v1/items", headers={"tenant-id": "globex"})
        harness.clock.now = end
        first_after = await client.get("/v1/items", headers={"tenant-id": "globex"})
    assert last_inside.status_code == 410
    assert last_inside.headers["cache-control"] == "no-store"
    assert last_inside.json()["brownout_ends"] == "2027-03-01T03:15:00+00:00"
    assert first_after.status_code == 200
    assert {"deprecation", "sunset", "link"} <= set(first_after.headers)
    assert "cache-control" not in first_after.headers
    assert harness.inner_hits == ["/v1/items"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant", "waiver_header", "offset_from_sunset", "expected_status", "outcome"),
    [
        ("acme", "1", timedelta(days=28), 200, Outcome.WAIVED),
        ("acme", None, timedelta(days=28), 410, Outcome.BROWNOUT_REJECTED),
        ("acme", "0", timedelta(days=28), 410, Outcome.BROWNOUT_REJECTED),
        ("globex", "1", timedelta(days=28), 410, Outcome.BROWNOUT_REJECTED),
        ("acme", "1", timedelta(days=7), 410, Outcome.BROWNOUT_REJECTED),
        ("acme", "1", timedelta(days=6), 410, Outcome.BROWNOUT_REJECTED),
    ],
    ids=[
        "granted-and-asked",
        "granted-not-asked",
        "granted-but-declined",
        "asked-not-granted",
        "grant-expiry-instant-exclusive",
        "grant-expired",
    ],
)
async def test_waiver_needs_client_ask_and_unexpired_server_grant(
    tenant: str,
    waiver_header: str | None,
    offset_from_sunset: timedelta,
    expected_status: int,
    outcome: Outcome,
) -> None:
    at = SUNSET - offset_from_sunset
    windows = (Window(at - timedelta(minutes=1), at + timedelta(minutes=14)),)
    harness = Harness(clock=FakeClock(at), telemetry=SunsetTelemetry())

    async def items(request: Request) -> JSONResponse:
        harness.inner_hits.append(request.url.path)
        return JSONResponse({"items": []})

    wrapped = SunsetMiddleware(
        Starlette(routes=[Route("/v1/items", items)]),
        policy=make_policy(windows=windows),
        telemetry=harness.telemetry,
        tenant_of=tenant_from_header,
        clock=harness.clock,
    )
    headers = {"tenant-id": tenant}
    if waiver_header is not None:
        headers["brownout-waiver"] = waiver_header
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/items", headers=headers)
    assert response.status_code == expected_status
    assert harness.telemetry.counts[tenant, outcome] == 1
    if expected_status == 200:
        assert response.headers["sunset"] == "Thu, 01 Apr 2027 00:00:00 GMT"
        assert harness.inner_hits == ["/v1/items"]
    else:
        assert harness.inner_hits == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sunset_is_unconditional_and_briefly_cacheable() -> None:
    client, harness = build(SUNSET + timedelta(hours=1))
    async with client:
        response = await client.get(
            "/v1/items", headers={"tenant-id": "acme", "brownout-waiver": "1"}
        )
    assert response.status_code == 410
    assert response.headers["cache-control"] == "max-age=300"
    problem = response.json()
    assert problem["phase"] == "sunset"
    assert "brownout_ends" not in problem
    assert harness.inner_hits == []
    assert harness.telemetry.counts["acme", Outcome.GONE] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gone_max_age_and_waiver_header_come_from_the_policy() -> None:
    in_window = SUNSET - timedelta(days=28)
    window = Window(in_window - timedelta(minutes=1), in_window + timedelta(minutes=14))
    client, harness = build(
        in_window, windows=(window,), gone_max_age=60, waiver_header="sunset-waiver"
    )
    async with client:
        default_name = await client.get(
            "/v1/items", headers={"tenant-id": "acme", "brownout-waiver": "1"}
        )
        configured_name = await client.get(
            "/v1/items", headers={"tenant-id": "acme", "sunset-waiver": "1"}
        )
        harness.clock.now = SUNSET
        gone = await client.get("/v1/items", headers={"tenant-id": "acme", "sunset-waiver": "1"})
    assert (default_name.status_code, configured_name.status_code) == (410, 200)
    assert gone.status_code == 410
    assert gone.headers["cache-control"] == "max-age=60"
    assert dict(harness.telemetry.counts) == {
        ("acme", Outcome.BROWNOUT_REJECTED): 1,
        ("acme", Outcome.WAIVED): 1,
        ("acme", Outcome.GONE): 1,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prefix_match_does_not_capture_sibling_versions() -> None:
    client, harness = build(SUNSET + timedelta(days=1))
    async with client:
        v2 = await client.get("/v2/items", headers={"tenant-id": "acme"})
        v10 = await client.get("/v10/items", headers={"tenant-id": "acme"})
        bare = await client.get("/v1", headers={"tenant-id": "acme"})
    assert (v2.status_code, v10.status_code) == (200, 200)
    assert "deprecation" not in v10.headers
    assert bare.status_code == 410
    assert harness.inner_hits == ["/v2/items", "/v10/items"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telemetry_counts_outcomes_per_tenant_and_names_stragglers() -> None:
    client, harness = build(DEPRECATION + timedelta(days=1))
    async with client:
        await client.get("/v1/items", headers={"tenant-id": "acme"})
        await client.get("/v1/items", headers={"tenant-id": "initech"})
        harness.clock.now = BROWNOUT_DAY + timedelta(minutes=1)
        await client.get("/v1/items", headers={"tenant-id": "globex"})
        await client.get("/v1/items")
        harness.clock.now = BROWNOUT_DAY + timedelta(minutes=30)
        await client.get("/v1/items", headers={"tenant-id": "globex"})
        await client.get("/v2/items", headers={"tenant-id": "initech"})
    counts = harness.telemetry.counts
    assert dict(counts) == {
        ("acme", Outcome.SERVED_DEPRECATED): 1,
        ("initech", Outcome.SERVED_DEPRECATED): 1,
        ("globex", Outcome.BROWNOUT_REJECTED): 1,
        ("(anonymous)", Outcome.BROWNOUT_REJECTED): 1,
        ("globex", Outcome.SERVED_DEPRECATED): 1,
    }
    assert harness.telemetry.stragglers(since=BROWNOUT_DAY) == ["(anonymous)", "globex"]
    assert harness.telemetry.stragglers(since=DEPRECATION) == [
        "(anonymous)",
        "acme",
        "globex",
        "initech",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_telemetry_tenant_cardinality_is_bounded() -> None:
    client, harness = build(DEPRECATION + timedelta(days=1), max_tenants=2)
    async with client:
        for tenant in ("acme", "globex", "initech", "umbrella", "acme"):
            await client.get("/v1/items", headers={"tenant-id": tenant})
    assert set(harness.telemetry.last_seen) == {"acme", "globex", OVERFLOW_TENANT}
    assert harness.telemetry.counts[OVERFLOW_TENANT, Outcome.SERVED_DEPRECATED] == 2
    assert harness.telemetry.counts["acme", Outcome.SERVED_DEPRECATED] == 2
    # record() reports the bucket it used, so a caller can label its own metrics the same way.
    assert harness.telemetry.record("hooli", Outcome.GONE, SUNSET) == OVERFLOW_TENANT
    assert harness.telemetry.record("globex", Outcome.GONE, SUNSET) == "globex"
    assert "hooli" not in harness.telemetry.last_seen


@pytest.mark.unit
def test_last_seen_never_moves_backwards_and_straggler_bound_is_inclusive() -> None:
    telemetry = SunsetTelemetry()
    later = BROWNOUT_DAY + timedelta(hours=1)
    assert telemetry.record("acme", Outcome.SERVED_DEPRECATED, later) == "acme"
    telemetry.record("acme", Outcome.SERVED_DEPRECATED, BROWNOUT_DAY)  # out-of-order arrival
    assert telemetry.last_seen == {"acme": later}
    assert telemetry.counts["acme", Outcome.SERVED_DEPRECATED] == 2
    assert telemetry.stragglers(since=later) == ["acme"]
    assert telemetry.stragglers(since=later + timedelta(microseconds=1)) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_bypass_the_policy() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: object, send: object) -> None:
        seen.append(scope["type"])

    def clock() -> datetime:
        raise AssertionError("clock must not be read for non-http scopes")

    middleware = SunsetMiddleware(
        inner,
        policy=make_policy(),
        telemetry=SunsetTelemetry(),
        tenant_of=tenant_from_header,
        clock=clock,
    )

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await middleware({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


# --- socket tier: the same middleware behind real ASGI servers and HTTP versions ----------------

IN_WINDOW = BROWNOUT_DAY + timedelta(hours=3, minutes=5)
WINDOW_END = BROWNOUT_DAY + timedelta(hours=3, minutes=15)
BROWNOUT_HEADERS = {
    **SIGNALS,
    "cache-control": "no-store",
    "content-length": "288",
    "content-type": "application/problem+json",
}
ServerKind = Literal["uvicorn-h11", "uvicorn-httptools", "hypercorn", "hypercorn-quic", "granian"]


@dataclass(frozen=True)
class Ports:
    plain: int
    tls: int = 0
    quic: int = 0


@pytest.fixture(scope="module")
def tls_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for `localhost` and `127.0.0.1`, as PEM files."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    today = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(today - timedelta(days=1))
        .not_valid_after(today + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    directory = tmp_path_factory.mktemp("tls")
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def _fail_if_exited(task: asyncio.Task[None], server: str) -> None:
    if task.done():
        task.result()  # re-raises the server's own startup error, if it had one
        raise AssertionError(f"{server} exited before it started")


async def _wait_until_listening(port: int, task: asyncio.Task[None], server: str) -> None:
    await asyncio.sleep(0)  # let the server task take its first step before the first probe
    for _ in range(500):
        _fail_if_exited(task, server)
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.01)
            continue
        # A probe whose own local port is the target reached itself, not a listening server.
        reached_itself = writer.get_extra_info("sockname")[1] == port
        writer.close()
        await writer.wait_closed()
        if not reached_itself:
            return
    raise AssertionError(f"{server} did not accept connections")


@asynccontextmanager
async def live_server(
    kind: ServerKind, app: ASGIApp, cert: tuple[Path, Path]
) -> AsyncGenerator[Ports]:
    """Serve `app` on ephemeral loopback ports with a real ASGI server and yield the ports."""
    if kind in ("uvicorn-h11", "uvicorn-httptools"):
        http_impl: Literal["h11", "httptools"] = "h11" if kind == "uvicorn-h11" else "httptools"
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=0, http=http_impl, lifespan="off", log_level="warning"
            )
        )
        task = asyncio.create_task(uvicorn_server.serve())
        try:
            async with asyncio.timeout(10):
                while not uvicorn_server.started:
                    _fail_if_exited(task, "uvicorn")
                    await asyncio.sleep(0.01)
            yield Ports(plain=uvicorn_server.servers[0].sockets[0].getsockname()[1])
        finally:
            uvicorn_server.should_exit = True
            async with asyncio.timeout(10):
                await task
        return
    if kind == "granian":
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port: int = probe.getsockname()[1]
        granian_server = GranianServer(
            app,
            address="127.0.0.1",
            port=port,
            interface=Interfaces.ASGINL,
            websockets=False,
            log_enabled=False,
        )
        task = asyncio.create_task(granian_server.serve())
        try:
            await _wait_until_listening(port, task, "granian")
            yield Ports(plain=port)
        finally:
            granian_server.stop()  # type: ignore[no-untyped-call]
            async with asyncio.timeout(10):
                await task
        return
    config = HypercornConfig()
    config.certfile, config.keyfile = str(cert[0]), str(cert[1])
    config.bind = ["127.0.0.1:0"]  # TLS, because a certificate is configured
    config.insecure_bind = ["127.0.0.1:0"]
    if kind == "hypercorn-quic":
        config.quic_bind = ["127.0.0.1:0"]
    config.loglevel = "WARNING"
    # Hypercorn 0.18.0 leaves its QUIC task blocked on the datagram queue after termination and
    # never closes that transport, so shutdown waits out graceful_timeout (3 s by default).
    config.graceful_timeout = 0.1
    sockets = config.create_sockets()
    ports = Ports(
        plain=sockets.insecure_sockets[0].getsockname()[1],
        tls=sockets.secure_sockets[0].getsockname()[1],
        quic=sockets.quic_sockets[0].getsockname()[1] if sockets.quic_sockets else 0,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        hypercorn.asyncio.run.worker_serve(  # pyright: ignore[reportUnknownMemberType]
            ASGIWrapper(cast("ASGIFramework", app)),
            config,
            sockets=sockets,
            shutdown_trigger=stop.wait,
        )
    )
    try:
        await _wait_until_listening(ports.plain, task, "hypercorn")
        yield ports
    finally:
        stop.set()
        async with asyncio.timeout(10):
            await task


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "tls", "http2", "wire_version", "scope_version"),
    [
        ("uvicorn-h11", False, False, "HTTP/1.1", "1.1"),
        ("uvicorn-httptools", False, False, "HTTP/1.1", "1.1"),
        ("hypercorn", False, False, "HTTP/1.1", "1.1"),
        ("hypercorn", False, True, "HTTP/2", "2"),
        ("hypercorn", True, True, "HTTP/2", "2"),
        ("granian", False, False, "HTTP/1.1", "1.1"),
        ("granian", False, True, "HTTP/2", "2"),
    ],
    ids=[
        "uvicorn-h11-http1.1",
        "uvicorn-httptools-http1.1",
        "hypercorn-http1.1",
        "hypercorn-h2c-prior-knowledge",
        "hypercorn-h2-over-tls-alpn",
        "granian-http1.1",
        "granian-h2c-prior-knowledge",
    ],
)
async def test_real_servers_put_the_same_signals_and_brownout_410_on_the_wire(
    kind: ServerKind,
    tls: bool,
    http2: bool,
    wire_version: str,
    scope_version: str,
    tls_cert: tuple[Path, Path],
) -> None:
    app, harness = build_app(IN_WINDOW)
    async with live_server(kind, app, tls_cert) as ports:
        base_url = f"https://127.0.0.1:{ports.tls}" if tls else f"http://127.0.0.1:{ports.plain}"
        verify = ssl.create_default_context(cafile=str(tls_cert[0]))
        async with httpx.AsyncClient(
            base_url=base_url, verify=verify, http1=not http2, http2=http2, timeout=10
        ) as client:
            rejected = await client.get("/v1/items", headers={"tenant-id": "globex"})
            harness.clock.now = WINDOW_END
            served = await client.get("/v1/items", headers={"tenant-id": "globex"})
    assert (rejected.http_version, served.http_version) == (wire_version, wire_version)
    assert harness.http_versions == [scope_version, scope_version]
    assert rejected.status_code == 410
    assert {name: rejected.headers.get_list(name) for name in BROWNOUT_HEADERS} == {
        name: [value] for name, value in BROWNOUT_HEADERS.items()
    }
    # The raw field lines httpx parsed carry the lowercase names and the exact values.
    for name, value in SIGNALS.items():
        assert (name.encode(), value.encode()) in rejected.headers.raw
    assert len(rejected.content) == 288
    assert rejected.json()["brownout_ends"] == "2027-03-01T03:15:00+00:00"
    assert served.status_code == 200
    assert served.json() == {"items": []}
    assert {name: served.headers.get_list(name) for name in SIGNALS} == {
        name: [value] for name, value in SIGNALS.items()
    }
    assert "cache-control" not in served.headers
    assert harness.inner_hits == ["/v1/items"]
    assert dict(harness.telemetry.counts) == {
        ("globex", Outcome.BROWNOUT_REJECTED): 1,
        ("globex", Outcome.SERVED_DEPRECATED): 1,
    }


@pytest.mark.socket
@pytest.mark.asyncio
async def test_hypercorn_http3_over_quic_carries_the_same_signals_and_brownout_410(
    tls_cert: tuple[Path, Path],
) -> None:
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        # aioquic 1.7.2 and pylsqpack 1.0.0 publish abi3 wheels only; uv does not select those for
        # a free-threaded build, and the source builds failed on 3.15.0b3t and 3.15.2t.
        pytest.importorskip("aioquic")
    import aioquic.asyncio.client
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration

    class H3Client(QuicConnectionProtocol):
        """The smallest HTTP/3 client that can send a GET and collect one response per stream."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.h3 = H3Connection(self._quic)
            self.headers: dict[int, list[tuple[bytes, bytes]]] = {}
            self.bodies: dict[int, bytearray] = {}
            self.finished: dict[int, asyncio.Future[None]] = {}

        def quic_event_received(self, event: QuicEvent) -> None:
            for h3_event in self.h3.handle_event(event):
                if isinstance(h3_event, HeadersReceived):
                    self.headers[h3_event.stream_id] = list(h3_event.headers)
                elif isinstance(h3_event, DataReceived):
                    self.bodies[h3_event.stream_id] += h3_event.data
                else:
                    continue
                if h3_event.stream_ended:
                    self.finished[h3_event.stream_id].set_result(None)

        async def get(self, authority: str, path: str) -> tuple[list[tuple[bytes, bytes]], bytes]:
            stream_id = self._quic.get_next_available_stream_id()
            self.finished[stream_id] = asyncio.get_running_loop().create_future()
            self.bodies[stream_id] = bytearray()
            self.h3.send_headers(
                stream_id,
                [
                    (b":method", b"GET"),
                    (b":scheme", b"https"),
                    (b":authority", authority.encode()),
                    (b":path", path.encode()),
                    (b"tenant-id", b"globex"),
                ],
                end_stream=True,
            )
            self.transmit()
            async with asyncio.timeout(10):
                await self.finished[stream_id]
            return self.headers[stream_id], bytes(self.bodies[stream_id])

    app, harness = build_app(IN_WINDOW)
    quic = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, server_name="localhost")
    quic.load_verify_locations(cafile=str(tls_cert[0]))
    async with (
        asyncio.timeout(20),  # bounds the QUIC handshake as well as both requests
        live_server("hypercorn-quic", app, tls_cert) as ports,
        aioquic.asyncio.client.connect(  # pyright: ignore[reportUnknownMemberType]
            "127.0.0.1", ports.quic, configuration=quic, create_protocol=H3Client
        ) as client,
    ):
        assert isinstance(client, H3Client)
        authority = f"localhost:{ports.quic}"
        rejected_headers, rejected_body = await client.get(authority, "/v1/items")
        harness.clock.now = WINDOW_END
        served_headers, served_body = await client.get(authority, "/v1/items")
    assert harness.http_versions == ["3", "3"]
    # RFC 9114 Section 4.2 requires lowercase field names; these are the decoded QPACK fields.
    assert all(name == name.lower() for name, _ in (*rejected_headers, *served_headers))
    assert rejected_headers[0] == (b":status", b"410")
    for name, value in BROWNOUT_HEADERS.items():
        assert [v for n, v in rejected_headers if n == name.encode()] == [value.encode()]
    assert len(rejected_body) == 288
    assert served_headers[0] == (b":status", b"200")
    for name, value in SIGNALS.items():
        assert [v for n, v in served_headers if n == name.encode()] == [value.encode()]
    assert b"cache-control" not in dict(served_headers)
    assert served_body == b'{"items":[]}'
    assert dict(harness.telemetry.counts) == {
        ("globex", Outcome.BROWNOUT_REJECTED): 1,
        ("globex", Outcome.SERVED_DEPRECATED): 1,
    }
