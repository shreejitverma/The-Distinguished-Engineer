"""Lab: stale-while-revalidate and stale-if-error (RFC 5861) in a shared cache."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from api_labs.f03_http_caching_and_conditional_requests.stale_cache import (
    DELTA_SECONDS_MAX,
    Outcome,
    StaleCache,
    StalePolicy,
    delta_seconds,
    parse_cache_control,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import Receive, Scope, Send


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Origin:
    """A scriptable origin: status, body, Cache-Control, ETag, a gate, a failure switch."""

    cache_control: str = "max-age=600, stale-while-revalidate=30"
    body: bytes = b"v1"
    status: int = 200
    etag: bytes | None = None
    age: bytes | None = None
    raise_error: bool = False
    gate: asyncio.Event | None = None
    content_length_200: bytes | None = None
    content_length_304: bytes | None = None
    clock_to_advance: Clock | None = None
    advance_by: float = 0.0
    seen_if_none_match: list[bytes | None] = field(default_factory=list[bytes | None])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        inm = next((v for k, v in scope["headers"] if k == b"if-none-match"), None)
        self.seen_if_none_match.append(inm)
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_error:
            raise ConnectionRefusedError("origin down")
        if self.clock_to_advance is not None:
            self.clock_to_advance.now += self.advance_by  # the origin takes time to answer
        headers = [(b"cache-control", self.cache_control.encode())]
        if self.etag is not None:
            headers.append((b"etag", self.etag))
        if self.age is not None:
            headers.append((b"age", self.age))
        status = self.status
        body = self.body
        if self.etag is not None and inm == self.etag and status == 200:
            status, body = 304, b""
            if self.content_length_304 is not None:
                headers.append((b"content-length", self.content_length_304))
        elif self.content_length_200 is not None:
            headers.append((b"content-length", self.content_length_200))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    @property
    def calls(self) -> int:
        return len(self.seen_if_none_match)


@dataclass
class Rig:
    clock: Clock
    origin: Origin
    cache: StaleCache
    client: httpx.AsyncClient

    async def get(self, at: float, headers: dict[str, str] | None = None) -> httpx.Response:
        self.clock.now = at
        return await self.client.get("/item", headers=headers)


async def make_rig(origin: Origin, *, s_maxage_blocks_stale: bool = True) -> Rig:
    clock = Clock()
    cache = StaleCache(app=origin, clock=clock, s_maxage_blocks_stale=s_maxage_blocks_stale)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=cache), base_url="http://test")
    return Rig(clock=clock, origin=origin, cache=cache, client=client)


async def settle() -> None:
    """Let already-scheduled tasks run; asyncio schedules ready callbacks FIFO."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    r = await make_rig(Origin())
    yield r
    if r.origin.gate is not None:
        r.origin.gate.set()  # never leave a revalidation parked when a test fails
    await r.cache.drain()
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fresh_entry_is_served_without_contacting_origin(rig: Rig) -> None:
    await rig.get(0)
    response = await rig.get(599)
    assert response.headers["age"] == "599"
    assert response.headers["cache-status"] == "lab; hit; ttl=1"
    assert rig.origin.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rfc5861_3_1_stale_is_served_without_waiting_for_revalidation(rig: Rig) -> None:
    await rig.get(0)
    rig.origin.body = b"v2"
    rig.origin.gate = asyncio.Event()  # the origin now hangs until released
    stale = await asyncio.wait_for(rig.get(610), timeout=2.0)
    assert stale.text == "v1"
    assert stale.headers["age"] == "610"
    assert stale.headers["cache-status"] == "lab; hit; ttl=-10"
    assert "warning" not in stale.headers
    await settle()
    assert rig.origin.calls == 2  # the background revalidation is in flight
    rig.origin.gate.set()
    await rig.cache.drain()
    fresh = await rig.get(615)
    assert fresh.text == "v2"
    assert fresh.headers["age"] == "5"
    assert rig.cache.outcomes == [Outcome.FORWARDED, Outcome.STALE_WHILE_REVALIDATE, Outcome.HIT]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "expected"),
    [(630, Outcome.STALE_WHILE_REVALIDATE), (631, Outcome.FORWARDED)],
)
async def test_swr_window_ends_exactly_at_max_age_plus_delta(age: int, expected: Outcome) -> None:
    r = await make_rig(Origin())
    await r.get(0)
    r.origin.body = b"v2"
    response = await r.get(age)
    await r.cache.drain()
    assert r.cache.outcomes[-1] is expected
    assert response.text == ("v1" if expected is Outcome.STALE_WHILE_REVALIDATE else "v2")
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_stale_hits_trigger_one_background_revalidation(rig: Rig) -> None:
    await rig.get(0)
    rig.origin.gate = asyncio.Event()
    rig.clock.now = 610
    hits = asyncio.gather(*(rig.client.get("/item") for _ in range(20)))
    responses = await asyncio.wait_for(hits, timeout=2.0)
    assert {r.text for r in responses} == {"v1"}
    await settle()
    assert rig.origin.calls == 2  # 1 initial fill + 1 revalidation, not 21
    rig.origin.gate.set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_revalidation_is_conditional_and_304_freshens_entry() -> None:
    r = await make_rig(Origin(etag=b'"e1"'))
    await r.get(0)
    await r.get(610)
    await r.cache.drain()
    assert r.origin.seen_if_none_match == [None, b'"e1"']
    after = await r.get(700)
    assert after.text == "v1"
    assert after.headers["age"] == "90"
    assert r.cache.outcomes[-1] is Outcome.HIT
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_background_revalidation_keeps_the_stale_entry(rig: Rig) -> None:
    await rig.get(0)
    rig.origin.status = 503
    await rig.get(610)
    await rig.cache.drain()
    again = await rig.get(620)
    assert again.status_code == 200
    assert again.text == "v1"
    assert rig.cache.outcomes[-1] is Outcome.STALE_WHILE_REVALIDATE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_404_evicts_instead_of_serving_stale(rig: Rig) -> None:
    await rig.get(0)
    rig.origin.status = 404
    await rig.get(610)
    await rig.cache.drain()
    gone = await rig.get(611)
    assert gone.status_code == 404
    assert rig.cache.entries == {}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "status", "body"),
    [(900, 200, "success"), (1800, 200, "success"), (1801, 500, "failure")],
)
async def test_rfc5861_4_1_stale_if_error_window(age: int, status: int, body: str) -> None:
    r = await make_rig(Origin(cache_control="max-age=600, stale-if-error=1200", body=b"success"))
    await r.get(0)
    r.origin.status, r.origin.body = 500, b"failure"
    response = await r.get(age)
    assert (response.status_code, response.text) == (status, body)
    if status == 200:
        assert response.headers["age"] == str(age)
        expected_status = f"lab; fwd=stale; fwd-status=500; ttl={600 - age}"
        assert response.headers["cache-status"] == expected_status
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "status", "outcome"),
    [
        (80, 200, Outcome.STALE_WHILE_REVALIDATE),
        (160, 200, Outcome.STALE_IF_ERROR),
        (161, 500, Outcome.FORWARDED),
    ],
)
async def test_windows_are_independent_and_both_start_at_expiry(
    age: int, status: int, outcome: Outcome
) -> None:
    cc = "max-age=60, stale-while-revalidate=30, stale-if-error=100"
    r = await make_rig(Origin(cache_control=cc))
    await r.get(0)
    r.origin.status = 500
    response = await r.get(age)
    await r.cache.drain()
    assert response.status_code == status
    assert r.cache.outcomes[-1] is outcome
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_status", "served"),
    [(500, 200), (502, 200), (503, 200), (504, 200), (501, 501), (404, 404), (429, 429)],
)
async def test_only_500_502_503_504_count_as_errors(origin_status: int, served: int) -> None:
    r = await make_rig(Origin(cache_control="max-age=60, stale-if-error=600"))
    await r.get(0)
    r.origin.status = origin_status
    response = await r.get(120)
    assert response.status_code == served
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unreachable_origin_is_a_502_situation_masked_by_stale_if_error() -> None:
    r = await make_rig(Origin(cache_control="max-age=60, stale-if-error=600"))
    await r.get(0)
    r.origin.raise_error = True
    masked = await r.get(120)
    assert masked.status_code == 200
    assert masked.headers["cache-status"] == "lab; fwd=stale; fwd-status=502; ttl=-60"
    exposed = await r.get(661)
    assert exposed.status_code == 502
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cache_control", ["max-age=60", "max-age=0", "public"])
@pytest.mark.parametrize("origin_status", [200, 500])
async def test_expiry_boundary_without_directives_is_never_served_stale(
    cache_control: str, origin_status: int
) -> None:
    # RFC 9111 Section 4.2: current_age == freshness_lifetime is already stale, and
    # Section 4.2.4 forbids serving stale without explicit permission.
    r = await make_rig(Origin(cache_control=cache_control))
    await r.get(0)
    lifetime = 60 if cache_control == "max-age=60" else 0
    r.origin.status = origin_status
    response = await r.get(lifetime)
    assert response.status_code == origin_status
    assert r.origin.calls == 2
    assert r.cache.outcomes[-1] is Outcome.FORWARDED
    await r.cache.drain()
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_zero_windows_still_cover_the_expiry_instant() -> None:
    swr_rig = await make_rig(Origin(cache_control="max-age=60, stale-while-revalidate=0"))
    await swr_rig.get(0)
    swr = await swr_rig.get(60)
    assert swr.status_code == 200
    assert swr_rig.cache.outcomes[-1] is Outcome.STALE_WHILE_REVALIDATE
    await swr_rig.cache.drain()
    await swr_rig.client.aclose()

    sie_rig = await make_rig(Origin(cache_control="max-age=60, stale-if-error=0"))
    await sie_rig.get(0)
    sie_rig.origin.status = 500
    masked = await sie_rig.get(60)
    assert masked.status_code == 200
    assert sie_rig.cache.outcomes[-1] is Outcome.STALE_IF_ERROR
    exposed = await sie_rig.get(61)
    assert exposed.status_code == 500
    await sie_rig.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_stale_if_error_directive_applies_to_that_request_only() -> None:
    r = await make_rig(Origin(cache_control="max-age=60"))
    await r.get(0)
    r.origin.status = 503
    plain = await r.get(120)
    tolerant = await r.get(120, headers={"cache-control": "stale-if-error=3600"})
    assert plain.status_code == 503
    assert tolerant.status_code == 200
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("prohibitor", ["must-revalidate", "proxy-revalidate", "no-cache"])
async def test_prohibiting_directives_override_both_extensions(prohibitor: str) -> None:
    cc = f"max-age=600, {prohibitor}, stale-while-revalidate=30, stale-if-error=1200"
    r = await make_rig(Origin(cache_control=cc))
    await r.get(0)
    r.origin.body = b"v2"
    blocked = await r.get(610)
    assert blocked.text == "v2"  # forwarded synchronously, never served stale
    r.origin.raise_error = True
    down = await r.get(1300)
    assert down.status_code == 504  # RFC 9111 Section 5.2.2.2
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_cache_entry_is_validated_even_while_fresh() -> None:
    r = await make_rig(Origin(cache_control="max-age=600, no-cache", etag=b'"e1"'))
    await r.get(0)
    response = await r.get(10)
    assert (response.status_code, response.text) == (200, "v1")
    assert r.origin.seen_if_none_match == [None, b'"e1"']
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("strict", "expected"), [(True, "v2"), (False, "v1")])
async def test_s_maxage_implies_proxy_revalidate_unless_configured_lenient(
    strict: bool, expected: str
) -> None:
    r = await make_rig(
        Origin(cache_control="s-maxage=60, stale-while-revalidate=30"),
        s_maxage_blocks_stale=strict,
    )
    await r.get(0)
    r.origin.body = b"v2"
    response = await r.get(70)
    assert response.text == expected
    await r.cache.drain()
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_each_stale_hit_after_a_finished_revalidation_starts_a_new_one(rig: Rig) -> None:
    await rig.get(0)
    rig.origin.status = 503
    await rig.get(610)
    await rig.cache.drain()
    assert rig.origin.calls == 2
    await rig.get(620)
    await rig.cache.drain()
    assert rig.origin.calls == 3  # the in-flight slot was released, so a new attempt ran
    assert rig.cache.outcomes[1:] == [Outcome.STALE_WHILE_REVALIDATE] * 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_if_none_match_is_not_forwarded_to_the_origin() -> None:
    r = await make_rig(Origin(etag=b'"e1"'))
    cold = await r.get(0, headers={"if-none-match": '"e1"'})
    assert (cold.status_code, cold.text) == (200, "v1")
    assert r.origin.seen_if_none_match == [None]
    assert "/item?" in r.cache.entries
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_304_does_not_replace_the_stored_content_length() -> None:
    origin = Origin(etag=b'"e1"', content_length_200=b"2", content_length_304=b"0")
    r = await make_rig(origin)
    await r.get(0)
    await r.get(610)
    await r.cache.drain()
    assert r.origin.seen_if_none_match == [None, b'"e1"']
    after = await r.get(620)
    assert after.headers["content-length"] == "2"
    assert after.text == "v1"
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_s_maxage_takes_precedence_over_max_age_for_the_shared_cache() -> None:
    cc = "max-age=600, s-maxage=100, stale-while-revalidate=30"
    r = await make_rig(Origin(cache_control=cc), s_maxage_blocks_stale=False)
    await r.get(0)
    response = await r.get(110)  # 10 s past s-maxage, 490 s before max-age
    assert response.headers["cache-status"] == "lab; hit; ttl=-10"
    assert r.cache.outcomes[-1] is Outcome.STALE_WHILE_REVALIDATE
    await r.cache.drain()
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_delay_counts_toward_current_age() -> None:
    clock = Clock()
    origin = Origin(clock_to_advance=clock, advance_by=5.0)
    cache = StaleCache(app=origin, clock=clock)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=cache), base_url="http://test")
    r = Rig(clock=clock, origin=origin, cache=cache, client=client)
    await r.get(0)  # request_time 0, response_time 5
    hit = await r.get(10)  # resident 5 s + response_delay 5 s
    assert hit.headers["age"] == "10"
    await client.aclose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upstream_age_counts_toward_staleness() -> None:
    r = await make_rig(Origin(cache_control="max-age=600, stale-while-revalidate=30", age=b"500"))
    await r.get(0)
    r.origin.age = None
    response = await r.get(110)
    assert response.headers["age"] == "610"
    assert r.cache.outcomes[-1] is Outcome.STALE_WHILE_REVALIDATE
    await r.cache.drain()
    await r.client.aclose()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('max-age=600, Stale-While-Revalidate="30", stale-if-error=1200', (600, 30, 1200)),
        ('max-age=600, community="x, stale-if-error=60, y"', (600, None, None)),
        ("max-age=600, stale-while-revalidate=-1, stale-if-error=1e3", (600, None, None)),
        ("max-age=10, max-age=900, stale-if-error=5", (10, None, 5)),
        ("max-age=abc, stale-while-revalidate=30", (0, 30, None)),
        ("max-age=60, stale-while-revalidate=0, stale-if-error=0", (60, 0, 0)),
    ],
)
def test_directive_parsing(header: str, expected: tuple[int, int | None, int | None]) -> None:
    policy = StalePolicy.from_headers(
        [(b"cache-control", header.encode())], s_maxage_blocks_stale=True
    )
    got = (policy.freshness_lifetime, policy.stale_while_revalidate, policy.stale_if_error)
    assert got == expected


@pytest.mark.unit
def test_delta_seconds_overflow_is_capped_at_2_pow_31() -> None:
    assert delta_seconds("99999999999999999999") == DELTA_SECONDS_MAX == 2147483648
    assert parse_cache_control([b"max-age=1", b"stale-if-error=2"]) == {
        "max-age": "1",
        "stale-if-error": "2",
    }
