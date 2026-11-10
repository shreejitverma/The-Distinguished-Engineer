"""Unit tests for an RFC 9111 cache implemented as an httpx.AsyncBaseTransport wrapper."""

from __future__ import annotations

import gzip
import time
from dataclasses import dataclass
from email.utils import formatdate
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from api_labs.f03_http_caching_and_conditional_requests.client_cache import (
    DELTA_SECONDS_CAP,
    CachingTransport,
    parse_cache_control,
    parse_delta_seconds,
    parse_http_date,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, MutableMapping

T0 = 1_793_000_000.0
LAST_MODIFIED = "Sun, 01 Nov 2026 08:30:00 GMT"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class Reply:
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b"v1"
    delay: float = 0.0  # seconds the origin takes; advances the shared clock
    date_age: float = 0.0  # how far in the past the origin's Date is
    send_date: bool = True


class Origin(httpx.AsyncBaseTransport):
    """Records every request that reaches the origin, then answers through httpx.ASGITransport."""

    def __init__(self, clock: Clock, handler: Callable[[httpx.Request], Reply]) -> None:
        self.clock = clock
        self.handler = handler
        self.seen: list[httpx.Request] = []
        self._reply = Reply()
        self._asgi = httpx.ASGITransport(app=self._app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        self._reply = self.handler(request)
        self.clock.now += self._reply.delay
        return await self._asgi.handle_async_request(request)

    async def _app(
        self,
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        reply = self._reply
        date = formatdate(self.clock.now - reply.date_age, usegmt=True)
        headers = [(k.encode(), v.encode()) for k, v in reply.headers]
        if reply.send_date:
            headers.append((b"date", date.encode()))
        await send({"type": "http.response.start", "status": reply.status, "headers": headers})
        await send({"type": "http.response.body", "body": reply.body})


def build(
    handler: Callable[[httpx.Request], Reply], **options: Any
) -> tuple[httpx.AsyncClient, CachingTransport, Origin, Clock]:
    clock = Clock()
    origin = Origin(clock, handler)
    cache = CachingTransport(origin, clock=clock, **options)
    client = httpx.AsyncClient(transport=cache, base_url="http://test", timeout=3.5)
    return client, cache, origin, clock


def versioned(
    cache_control: str, *, etag: str = '"v1"', body: bytes = b"v1"
) -> Callable[[httpx.Request], Reply]:
    """An origin that answers 304 when If-None-Match carries its current tag."""
    headers = (("cache-control", cache_control), ("etag", etag), ("last-modified", LAST_MODIFIED))

    def handler(request: httpx.Request) -> Reply:
        if request.headers.get("if-none-match") == etag:
            return Reply(status=304, headers=headers, body=b"")
        return Reply(headers=headers, body=body)

    return handler


def status(response: httpx.Response) -> object:
    return response.extensions["cache_status"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fresh_response_is_served_from_cache_with_age() -> None:
    client, _, origin, clock = build(versioned("max-age=60"))
    async with client:
        first = await client.get("/a")
        clock.now += 10
        second = await client.get("/a")
    assert (status(first), status(second)) == ("miss", "hit")
    assert second.text == "v1"
    assert second.headers["age"] == "10"
    assert len(origin.seen) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_entry_turns_stale_when_age_reaches_max_age() -> None:
    client, _, origin, clock = build(versioned("max-age=60"))
    async with client:
        await client.get("/a")
        clock.now += 59
        at_59 = await client.get("/a")
        clock.now += 1
        at_60 = await client.get("/a")
    assert status(at_59) == "hit"
    assert status(at_60) == "revalidated"
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upstream_age_and_response_delay_count_toward_current_age() -> None:
    reply = Reply(headers=(("cache-control", "max-age=60"), ("age", "30")), delay=5.0)
    client, _, origin, clock = build(lambda _request: reply)
    async with client:
        await client.get("/a")  # corrected_initial_age = 30 + 5 = 35
        clock.now += 24
        at_59 = await client.get("/a")
        clock.now += 1
        at_60 = await client.get("/a")
    assert (status(at_59), at_59.headers["age"]) == ("hit", "59")
    assert status(at_60) == "miss"
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apparent_age_from_an_old_date_header() -> None:
    reply = Reply(headers=(("cache-control", "max-age=60"),), date_age=40.0)
    client, _, origin, clock = build(lambda _request: reply)
    async with client:
        await client.get("/a")  # apparent_age = 40
        clock.now += 19
        at_59 = await client.get("/a")
        clock.now += 1
        await client.get("/a")
    assert (status(at_59), at_59.headers["age"]) == ("hit", "59")
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_entry_revalidates_with_both_validators_and_replays_body() -> None:
    client, _, origin, _ = build(versioned("max-age=0"))
    async with client:
        await client.get("/a")
        second = await client.get("/a")
    conditional = origin.seen[1].headers
    assert conditional["if-none-match"] == '"v1"'
    assert conditional["if-modified-since"] == LAST_MODIFIED
    assert (second.status_code, second.text, status(second)) == (200, "v1", "revalidated")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_304_freshens_headers_but_never_content_length() -> None:
    def handler(request: httpx.Request) -> Reply:
        if "if-none-match" in request.headers:
            update = (
                ("etag", '"v1"'),
                ("cache-control", "max-age=120"),
                ("content-length", "0"),
                ("x-version", "2"),
            )
            return Reply(status=304, headers=update, body=b"")
        return Reply(headers=(("etag", '"v1"'), ("cache-control", "max-age=0")), body=b"hello")

    client, _, origin, clock = build(handler)
    async with client:
        await client.get("/a")
        revalidated = await client.get("/a")
        clock.now += 100
        hit = await client.get("/a")
    assert revalidated.headers["content-length"] == "5"
    assert revalidated.headers["x-version"] == "2"
    assert (hit.text, status(hit)) == ("hello", "hit")
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_304_that_names_no_stored_response_triggers_unconditional_refetch() -> None:
    def handler(request: httpx.Request) -> Reply:
        if request.url.path == "/a" and len(origin.seen) == 1:
            return Reply(headers=(("etag", '"v1"'), ("cache-control", "max-age=0")))
        if "if-none-match" in request.headers:
            return Reply(status=304, headers=(("etag", '"v9"'),), body=b"")
        return Reply(headers=(("etag", '"v9"'),), body=b"fresh")

    client, _, origin, _ = build(handler)
    async with client:
        await client.get("/a")
        second = await client.get("/a")
    assert second.text == "fresh"
    assert len(origin.seen) == 3
    assert "if-none-match" not in origin.seen[2].headers


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "returned", "expected"),
    [
        ('W/"v1"', 'W/"v1"', "revalidated"),
        ('"v1"', 'W/"v1"', "revalidated"),
        ('W/"v1"', '"v1"', "miss"),
    ],
)
async def test_304_selection_uses_strong_match_for_strong_and_opaque_match_for_weak(
    stored: str, returned: str, expected: str
) -> None:
    def handler(request: httpx.Request) -> Reply:
        if "if-none-match" in request.headers:
            return Reply(status=304, headers=(("etag", returned),), body=b"")
        return Reply(headers=(("etag", stored), ("cache-control", "max-age=0")))

    client, _, _, _ = build(handler)
    async with client:
        await client.get("/a")
        second = await client.get("/a")
    assert status(second) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_response_to_conditional_request_replaces_entry() -> None:
    def handler(request: httpx.Request) -> Reply:
        if "if-none-match" in request.headers:
            return Reply(headers=(("etag", '"v2"'), ("cache-control", "max-age=60")), body=b"v2")
        return Reply(headers=(("etag", '"v1"'), ("cache-control", "max-age=0")))

    client, _, origin, _ = build(handler)
    async with client:
        await client.get("/a")
        replaced = await client.get("/a")
        hit = await client.get("/a")
    assert (replaced.text, status(replaced)) == ("v2", "miss")
    assert (hit.text, status(hit)) == ("v2", "hit")
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gzip_body_is_stored_raw_and_decoded_once_on_replay() -> None:
    plain = b"x" * 1000
    coded = gzip.compress(plain, mtime=0)
    reply = Reply(
        headers=(("cache-control", "max-age=60"), ("content-encoding", "gzip")), body=coded
    )
    client, cache, _, _ = build(lambda _request: reply)
    async with client:
        first = await client.get("/a")
        hit = await client.get("/a")
    assert first.content == plain
    assert (hit.content, status(hit)) == (plain, "hit")
    assert cache.store["http://test/a"].body == coded


class FailingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"part"
        raise httpx.ReadError("connection reset mid-body")

    async def aclose(self) -> None:
        self.closed = True


class FailingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.stream = FailingStream()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = [("cache-control", "max-age=60")]
        return httpx.Response(200, headers=headers, stream=self.stream, request=request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_stream_is_closed_when_buffering_fails() -> None:
    inner = FailingTransport()
    async with httpx.AsyncClient(transport=CachingTransport(inner), base_url="http://test") as c:
        with pytest.raises(httpx.ReadError):
            await c.get("/a")
    assert inner.stream.closed


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_cc", "response_cc"),
    [("no-store", "max-age=60"), (None, "max-age=60, no-store")],
)
async def test_no_store_in_request_or_response_prevents_storage(
    request_cc: str | None, response_cc: str
) -> None:
    client, cache, origin, _ = build(versioned(response_cc))
    headers = {} if request_cc is None else {"cache-control": request_cc}
    async with client:
        await client.get("/a", headers=headers)
        assert dict(cache.store) == {}
        await client.get("/a")
    assert len(origin.seen) == 2
    assert "if-none-match" not in origin.seen[1].headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_cache_response_is_stored_but_always_revalidated() -> None:
    client, _, origin, _ = build(versioned("max-age=600, no-cache"))
    async with client:
        await client.get("/a")
        second = await client.get("/a")
    assert status(second) == "revalidated"
    assert origin.seen[1].headers["if-none-match"] == '"v1"'


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("request_cc", ["no-cache", "max-age=0"])
async def test_request_directives_force_revalidation_of_a_fresh_entry(request_cc: str) -> None:
    client, _, origin, clock = build(versioned("max-age=600"))
    async with client:
        await client.get("/a")
        clock.now += 1
        forced = await client.get("/a", headers={"cache-control": request_cc})
    assert status(forced) == "revalidated"
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_only_if_cached_never_contacts_the_origin() -> None:
    client, _, origin, clock = build(versioned("max-age=60"))
    only = {"cache-control": "only-if-cached"}
    async with client:
        cold = await client.get("/a", headers=only)
        await client.get("/a")
        warm = await client.get("/a", headers=only)
        clock.now += 60
        stale = await client.get("/a", headers=only)
    assert cold.status_code == 504
    assert (warm.status_code, status(warm)) == (200, "hit")
    assert stale.status_code == 504
    assert len(origin.seen) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_unsafe_method_invalidates_target_and_same_origin_location() -> None:
    def handler(request: httpx.Request) -> Reply:
        if request.method == "POST":
            links = (("location", "/b"), ("content-location", "http://other.test/b"))
            return Reply(status=201, headers=links, body=b"")
        return Reply(headers=(("cache-control", "max-age=60"),))

    client, cache, _, _ = build(handler)
    async with client:
        for url in ("/a", "/b", "http://other.test/b"):
            await client.get(url)
        await client.post("/a")
    assert set(cache.store) == {"http://other.test/b"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_response_to_unsafe_method_keeps_entries() -> None:
    def handler(request: httpx.Request) -> Reply:
        if request.method == "DELETE":
            return Reply(status=500, body=b"")
        return Reply(headers=(("cache-control", "max-age=60"),))

    client, cache, _, _ = build(handler)
    async with client:
        await client.get("/a")
        await client.delete("/a")
    assert set(cache.store) == {"http://test/a"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("vary", "second_language", "expected"), [
    ("accept-language", "en", "hit"),
    ("accept-language", "fr", "miss"),
    ("*", "en", "miss"),
])  # fmt: skip
async def test_vary_nominated_fields_must_match(
    vary: str, second_language: str, expected: str
) -> None:
    reply = Reply(headers=(("cache-control", "max-age=60"), ("vary", vary)))
    client, _, _, _ = build(lambda _request: reply)
    async with client:
        await client.get("/a", headers={"accept-language": "en"})
        second = await client.get("/a", headers={"accept-language": second_language})
    assert status(second) == expected


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("shared", "bob_sees"), [(False, "alice"), (True, "bob")])
async def test_private_mode_serves_one_users_response_to_another(
    shared: bool, bob_sees: str
) -> None:
    def handler(request: httpx.Request) -> Reply:
        user = request.headers["authorization"].removeprefix("Bearer ")
        return Reply(headers=(("cache-control", "private, max-age=60"),), body=user.encode())

    client, _, _, _ = build(handler, shared=shared)
    async with client:
        await client.get("/me", headers={"authorization": "Bearer alice"})
        bob = await client.get("/me", headers={"authorization": "Bearer bob"})
    assert bob.text == bob_sees


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("cache_control", "stored"), [
    ("max-age=60", False),
    ("public, max-age=60", True),
    ("s-maxage=60", True),
    ("must-revalidate, max-age=60", True),
])  # fmt: skip
async def test_shared_cache_stores_authorized_response_only_with_explicit_permission(
    cache_control: str, stored: bool
) -> None:
    reply = Reply(headers=(("cache-control", cache_control),))
    client, cache, _, _ = build(lambda _request: reply, shared=True)
    async with client:
        await client.get("/a", headers={"authorization": "Bearer alice"})
    assert ("http://test/a" in cache.store) is stored


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("headers", "shared", "fresh_at_10s"), [
    ((("expires", formatdate(T0 + 60, usegmt=True)),), False, True),
    ((("expires", "0"),), False, False),
    ((("expires", "Mon, 01 Jan 2035 00:00:00 PST"),), False, False),
    ((("cache-control", "max-age=0"), ("expires", formatdate(T0 + 60, usegmt=True))), False, False),
    ((("cache-control", "max-age=abc"),), False, False),
    ((("cache-control", "max-age=60, max-age=0"),), False, True),
    ((("cache-control", "max-age=60, s-maxage=0"),), False, True),
    ((("cache-control", "max-age=60, s-maxage=0"),), True, False),
])  # fmt: skip
async def test_freshness_lifetime_first_match_and_invalid_values(
    headers: tuple[tuple[str, str], ...], shared: bool, fresh_at_10s: bool
) -> None:
    reply = Reply(headers=headers)
    client, _, origin, clock = build(lambda _request: reply, shared=shared)
    async with client:
        await client.get("/a")
        clock.now += 10
        await client.get("/a")
    assert (len(origin.seen) == 1) is fresh_at_10s


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validator_only_response_is_stored_and_revalidated_every_time() -> None:
    def handler(request: httpx.Request) -> Reply:
        if request.headers.get("if-none-match") == '"v1"':
            return Reply(status=304, headers=(("etag", '"v1"'),), body=b"")
        return Reply(headers=(("etag", '"v1"'),))

    client, _, origin, _ = build(handler)
    async with client:
        await client.get("/a")
        second = await client.get("/a")
    assert status(second) == "revalidated"
    assert origin.seen[1].headers["if-none-match"] == '"v1"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caller_preconditions_bypass_the_cache() -> None:
    client, _, origin, _ = build(versioned("max-age=60"))
    async with client:
        await client.get("/a")
        own = await client.get("/a", headers={"if-none-match": '"v1"'})
    assert (own.status_code, status(own)) == (304, "bypass")
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_content_and_head_are_never_stored() -> None:
    reply = Reply(status=206, headers=(("cache-control", "max-age=60"),))
    client, cache, origin, _ = build(lambda _request: reply)
    async with client:
        await client.get("/a")
        head = await client.head("/b")
    assert dict(cache.store) == {}
    assert status(head) == "bypass"
    assert len(origin.seen) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_is_bounded_by_entries_lru_and_body_size() -> None:
    def handler(request: httpx.Request) -> Reply:
        body = b"12345" if request.url.path == "/big" else b"v1"
        return Reply(headers=(("cache-control", "max-age=60"),), body=body)

    client, cache, _, _ = build(handler, max_entries=2, max_body_bytes=4)
    async with client:
        for path in ("/a", "/b", "/a", "/c", "/big"):
            await client.get(path)
    assert list(cache.store) == ["http://test/a", "http://test/c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_exactly_at_the_size_limit_is_stored_and_one_byte_more_is_not() -> None:
    def handler(request: httpx.Request) -> Reply:
        body = b"1234" if request.url.path == "/at" else b"12345"
        return Reply(headers=(("cache-control", "max-age=60"),), body=body)

    client, cache, _, _ = build(handler, max_body_bytes=4)
    async with client:
        for path in ("/at", "/over"):
            await client.get(path)
    assert list(cache.store) == ["http://test/at"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conditional_request_keeps_the_client_timeout_extension() -> None:
    client, _, origin, _ = build(versioned("max-age=0"))
    async with client:
        await client.get("/a")
        await client.get("/a")
    expected = {"connect": 3.5, "read": 3.5, "write": 3.5, "pool": 3.5}
    assert origin.seen[1].extensions["timeout"] == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hop_by_hop_fields_are_not_stored() -> None:
    headers = (
        ("cache-control", "max-age=60"),
        ("connection", "x-trace"),
        ("x-trace", "1"),
        ("keep-alive", "timeout=5"),
        ("x-keep", "y"),
    )
    client, _, _, _ = build(lambda _request: Reply(headers=headers))
    async with client:
        await client.get("/a")
        hit = await client.get("/a")
    assert "x-trace" not in hit.headers
    assert "keep-alive" not in hit.headers
    assert hit.headers["x-keep"] == "y"


@pytest.mark.unit
def test_cache_control_parser_keeps_quoted_commas_and_first_occurrence() -> None:
    parsed = parse_cache_control(['no-cache="set-cookie, x-a", max-age=5', "MAX-AGE=9, private"])
    assert parsed == {"no-cache": "set-cookie, x-a", "max-age": "5", "private": None}


@pytest.mark.unit
@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is Unix only")
@pytest.mark.parametrize(
    "value",
    ["Sun, 06 Nov 1994 08:49:37 GMT", "Sunday, 06-Nov-94 08:49:37 GMT", "Sun Nov  6 08:49:37 1994"],
)
def test_all_three_date_forms_parse_as_utc_whatever_the_local_zone(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert parse_http_date(value) == 784111777.0
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_date_falls_back_to_the_receipt_time() -> None:
    reply = Reply(headers=(("cache-control", "max-age=60"),), send_date=False)
    client, _, origin, clock = build(lambda _request: reply)
    async with client:
        await client.get("/a")
        clock.now += 10
        hit = await client.get("/a")
    assert (status(hit), hit.headers["age"]) == ("hit", "10")
    assert len(origin.seen) == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("validators", "expected"), [
    ((("last-modified", LAST_MODIFIED),), "revalidated"),
    ((("last-modified", "Mon, 02 Nov 2026 08:30:00 GMT"),), "miss"),
    ((), "miss"),
])  # fmt: skip
async def test_304_selection_by_last_modified_or_no_validator(
    validators: tuple[tuple[str, str], ...], expected: str
) -> None:
    def handler(request: httpx.Request) -> Reply:
        if "if-modified-since" in request.headers:
            return Reply(status=304, headers=validators, body=b"")
        return Reply(headers=(("last-modified", LAST_MODIFIED), ("cache-control", "max-age=0")))

    client, _, origin, _ = build(handler)
    async with client:
        await client.get("/a")
        second = await client.get("/a")
    assert status(second) == expected
    assert len(origin.seen) == (2 if expected == "revalidated" else 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_max_age_within_the_limit_is_a_hit() -> None:
    client, _, origin, clock = build(versioned("max-age=600"))
    async with client:
        await client.get("/a")
        clock.now += 10
        hit = await client.get("/a", headers={"cache-control": "max-age=30"})
    assert status(hit) == "hit"
    assert len(origin.seen) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_method_without_location_invalidates_only_the_target() -> None:
    def handler(request: httpx.Request) -> Reply:
        if request.method == "DELETE":
            return Reply(status=204, body=b"")
        return Reply(headers=(("cache-control", "max-age=60"),))

    client, cache, _, _ = build(handler)
    async with client:
        await client.get("/a")
        await client.get("/b")
        await client.delete("/a")
    assert set(cache.store) == {"http://test/b"}


@pytest.mark.unit
def test_cache_control_parser_skips_malformed_members() -> None:
    assert parse_cache_control([",,=bad, max-age=5"]) == {"max-age": "5"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("shared", "stored"), [(False, True), (True, False)])
async def test_shared_cache_never_stores_private_responses(shared: bool, stored: bool) -> None:
    reply = Reply(headers=(("cache-control", "private, max-age=60"),))
    client, cache, _, _ = build(lambda _request: reply, shared=shared)
    async with client:
        await client.get("/a")
    assert ("http://test/a" in cache.store) is stored


class InterleavingOrigin(httpx.AsyncBaseTransport):
    """While a revalidation is in flight, another unsafe request invalidates the same URL."""

    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(204, request=request)
        if "if-none-match" in request.headers:
            assert self.client is not None
            await self.client.post("/a")
            return httpx.Response(304, headers=[("etag", '"v9"')], request=request)
        headers = [("etag", '"v1"'), ("cache-control", "max-age=0")]
        return httpx.Response(200, headers=headers, content=b"v1", request=request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unmatched_304_survives_a_concurrent_invalidation_of_the_same_url() -> None:
    origin = InterleavingOrigin()
    async with httpx.AsyncClient(
        transport=CachingTransport(origin), base_url="http://test"
    ) as client:
        origin.client = client
        await client.get("/a")
        second = await client.get("/a")
    assert (second.text, status(second)) == ("v1", "miss")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_transport_responses_built_with_content_are_stored_raw() -> None:
    plain = b"y" * 500
    coded = gzip.compress(plain, mtime=0)

    def handler(request: httpx.Request) -> httpx.Response:
        headers = [("cache-control", "max-age=60"), ("content-encoding", "gzip")]
        return httpx.Response(200, headers=headers, content=coded)

    cache = CachingTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=cache, base_url="http://test") as client:
        first = await client.get("/a")
        hit = await client.get("/a")
    assert (first.content, hit.content, status(hit)) == (plain, plain, "hit")
    assert cache.store["http://test/a"].body == coded


class SyncOnlyStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"sync"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_stream_from_inner_transport_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers=[("cache-control", "max-age=60")], stream=SyncOnlyStream()
        )

    cache = CachingTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=cache, base_url="http://test") as client:
        with pytest.raises(TypeError, match="sync stream"):
            await client.get("/a")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("60", 60),
        ("0" * 5000 + "5", 5),
        ("2147483649", DELTA_SECONDS_CAP),
        ("9" * 5000, DELTA_SECONDS_CAP),
    ],
)
def test_delta_seconds_are_capped_at_2_pow_31(value: str, expected: int) -> None:
    assert parse_delta_seconds(value) == expected


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("headers", "fresh_at_10s"), [
    ((("cache-control", "max-age=" + "9" * 5000),), True),
    ((("cache-control", "max-age=" + "9" * 400),), True),
    ((("cache-control", "max-age=60"), ("age", "9" * 5000)), False),
])  # fmt: skip
async def test_hostile_delta_seconds_do_not_crash_the_client(
    headers: tuple[tuple[str, str], ...], fresh_at_10s: bool
) -> None:
    reply = Reply(headers=headers)
    client, _, origin, clock = build(lambda _request: reply)
    async with client:
        await client.get("/a")
        clock.now += 10
        await client.get("/a")
    assert (len(origin.seen) == 1) is fresh_at_10s
