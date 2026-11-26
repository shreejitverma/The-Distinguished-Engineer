"""Lab for the async request-reply note: polling, WebSub, signed webhooks, SSE, SSRF."""

import base64
import hashlib
import hmac
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import pytest
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from api_labs.f20_graphql_json_rpc_and_event_apis.http_callback_subscriptions_websub_and_async_request_reply_patterns import (  # noqa: E501
    KEEPALIVE,
    MAX_LEASE_SECONDS,
    STANDARD_WEBHOOKS_SCHEDULE,
    EventLog,
    JobService,
    ManualClock,
    SseEvent,
    UnsafeCallbackError,
    WebhookReceiver,
    WebhookSender,
    WebhookVerificationError,
    WebSubHub,
    WebSubSubscriber,
    append_query,
    backoff_delays,
    encode_event,
    fixed_delays,
    jittered,
    notification_app,
    parse_retry_after,
    parse_stream,
    poll,
    poll_job,
    sign_message,
    sse,
    validate_callback_url,
    verify_message,
    webhook,
    webhook_headers,
)

# --- helpers ---------------------------------------------------------------------------

KEY = b"lab-signing-key-EXAMPLE-NOT-A-REAL-KEY"
TOPIC = "https://publisher.example/feed"
PUBLIC_IP = "93.184.216.34"


def asgi(app: ASGIApp, base_url: str = "http://test") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def resolve_lab(host: str) -> Sequence[str]:
    table: Mapping[str, Sequence[str]] = {
        "subscriber.example": [PUBLIC_IP],
        "example.com": [PUBLIC_IP],
        "internal.example": ["10.1.2.3"],
        "mixed.example": [PUBLIC_IP, "192.168.1.1"],
    }
    return table.get(host, [])


# --- 1. cost model ---------------------------------------------------------------------


@pytest.mark.unit
def test_exponential_backoff_polling_finds_a_90_second_job_in_7_requests() -> None:
    outcome = poll(90.0, backoff_delays(1.0, 2.0, 30.0), horizon=600.0)
    assert (outcome.requests, outcome.empty, outcome.latency) == (7, 6, 1.0)


@pytest.mark.unit
def test_fixed_interval_polling_spends_more_requests_than_backoff() -> None:
    fixed = poll(90.0, fixed_delays(5.0), horizon=600.0)
    backoff = poll(90.0, backoff_delays(1.0, 2.0, 30.0), horizon=600.0)
    assert (fixed.requests, fixed.empty, fixed.latency) == (18, 17, 0.0)
    assert fixed.requests > backoff.requests
    assert fixed.latency is not None
    assert backoff.latency is not None
    assert fixed.latency < backoff.latency


@pytest.mark.unit
def test_polling_that_outlives_the_horizon_reports_never_and_all_requests_empty() -> None:
    outcome = poll(1000.0, backoff_delays(1.0, 2.0, 30.0), horizon=100.0)
    assert outcome.latency is None
    assert outcome.empty == outcome.requests > 0


@pytest.mark.unit
def test_webhook_latency_is_the_retry_schedule_when_the_receiver_is_down() -> None:
    healthy = webhook(10.0, STANDARD_WEBHOOKS_SCHEDULE, receiver_up_at=0.0)
    assert (healthy.requests, healthy.empty, healthy.latency) == (1, 0, 0.0)
    recovering = webhook(10.0, STANDARD_WEBHOOKS_SCHEDULE, receiver_up_at=210.0)
    assert (recovering.requests, recovering.empty, recovering.latency) == (3, 2, 305.0)
    dead = webhook(10.0, (5.0, 300.0, 1800.0), receiver_up_at=float("inf"))
    assert (dead.requests, dead.latency) == (4, None)


@pytest.mark.unit
def test_sse_costs_one_request_and_heals_a_drop_with_last_event_id() -> None:
    steady = sse(90.0)
    assert (steady.requests, steady.empty, steady.keepalives, steady.latency) == (1, 0, 6, 0.0)
    unlucky = sse(90.0, drop_at=88.0, reconnect_delay=3.0)
    assert (unlucky.requests, unlucky.latency) == (2, 1.0)
    early_drop = sse(90.0, drop_at=50.0, reconnect_delay=3.0)
    assert (early_drop.requests, early_drop.latency) == (2, 0.0)


# --- 2. retry helpers ------------------------------------------------------------------


@pytest.mark.unit
def test_retry_after_accepts_delay_seconds_and_http_date() -> None:
    now = datetime(1999, 12, 31, 23, 57, 59, tzinfo=UTC)
    assert parse_retry_after("120", now) == 120.0
    assert parse_retry_after("Fri, 31 Dec 1999 23:59:59 GMT", now) == 120.0
    assert parse_retry_after("Fri, 31 Dec 1999 23:00:00 GMT", now) == 0.0
    for bad in ("soon", "-5", "1.5"):
        with pytest.raises(ValueError, match=r"."):
            parse_retry_after(bad, now)


@pytest.mark.unit
def test_standard_webhooks_schedule_spans_75_hours_35_minutes_5_seconds() -> None:
    assert len(STANDARD_WEBHOOKS_SCHEDULE) == 9
    assert sum(STANDARD_WEBHOOKS_SCHEDULE) == 75 * 3600 + 35 * 60 + 5


@pytest.mark.unit
def test_jitter_spreads_a_delay_symmetrically() -> None:
    assert jittered(100.0, 0.2, lambda: 0.0) == pytest.approx(80.0)
    assert jittered(100.0, 0.2, lambda: 0.5) == pytest.approx(100.0)
    assert jittered(100.0, 0.2, lambda: 1.0) == pytest.approx(120.0)


# --- 3. Standard Webhooks --------------------------------------------------------------

BODY = b'{"type":"contact.created","data":{"id":"1f81eb52"}}'


@pytest.mark.unit
def test_signature_is_hmac_sha256_over_id_dot_timestamp_dot_payload() -> None:
    signed = b"msg_1.1674087231." + BODY
    expected = "v1," + base64.b64encode(hmac.new(KEY, signed, hashlib.sha256).digest()).decode()
    assert sign_message(KEY, "msg_1", 1674087231, BODY) == expected
    variants = {
        sign_message(KEY, "msg_2", 1674087231, BODY),
        sign_message(KEY, "msg_1", 1674087232, BODY),
        sign_message(KEY, "msg_1", 1674087231, BODY + b" "),
        sign_message(b"another-key", "msg_1", 1674087231, BODY),
    }
    assert expected not in variants
    assert len(variants) == 4


@pytest.mark.unit
def test_a_dot_in_the_message_id_is_refused_because_it_would_blur_the_field_boundary() -> None:
    with pytest.raises(ValueError, match=r"'\.'"):
        sign_message(KEY, "msg.1", 1, BODY)


@pytest.mark.unit
def test_verification_accepts_either_key_during_rotation_and_rejects_strangers() -> None:
    new_key = b"new-key-EXAMPLE-NOT-A-REAL-KEY"
    headers = webhook_headers([KEY, new_key], "msg_1", 1000, BODY)
    assert headers["webhook-signature"].count("v1,") == 2
    assert verify_message([new_key], headers, BODY, now=1000) == "msg_1"
    assert verify_message([KEY], headers, BODY, now=1000) == "msg_1"
    with pytest.raises(WebhookVerificationError, match="no valid signature"):
        verify_message([b"stranger"], headers, BODY, now=1000)


@pytest.mark.unit
def test_verification_must_see_the_raw_bytes_not_a_reserialised_parse() -> None:
    headers = webhook_headers([KEY], "msg_1", 1000, b'{"a":1,"b":2}')
    assert verify_message([KEY], headers, b'{"a":1,"b":2}', now=1000) == "msg_1"
    with pytest.raises(WebhookVerificationError, match="no valid signature"):
        verify_message([KEY], headers, b'{"a": 1, "b": 2}', now=1000)


@pytest.mark.unit
def test_replay_outside_the_tolerance_window_is_rejected_in_both_directions() -> None:
    headers = webhook_headers([KEY], "msg_1", 1000, BODY)
    assert verify_message([KEY], headers, BODY, now=1300) == "msg_1"
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_message([KEY], headers, BODY, now=1301)
    with pytest.raises(WebhookVerificationError, match="tolerance"):
        verify_message([KEY], headers, BODY, now=699)
    with pytest.raises(ValueError, match="positive"):
        verify_message([KEY], headers, BODY, now=1000, tolerance=0)


@pytest.mark.unit
def test_missing_or_tampered_headers_are_verification_errors() -> None:
    headers = webhook_headers([KEY], "msg_1", 1000, BODY)
    with pytest.raises(WebhookVerificationError, match="missing"):
        verify_message([KEY], {}, BODY, now=1000)
    with pytest.raises(WebhookVerificationError, match="malformed"):
        verify_message([KEY], {**headers, "webhook-timestamp": "yesterday"}, BODY, now=1000)
    with pytest.raises(WebhookVerificationError, match="malformed webhook-id"):
        verify_message([KEY], {**headers, "webhook-id": "msg.1"}, BODY, now=1000)
    with pytest.raises(WebhookVerificationError, match="no valid signature"):
        verify_message([KEY], {**headers, "webhook-id": "msg_9"}, BODY, now=1000)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_receiver_acknowledges_first_dedupes_redelivery_and_rejects_forgery() -> None:
    clock = ManualClock(1000.0)
    receiver = WebhookReceiver([KEY], clock)
    async with asgi(receiver.app) as client:
        headers = webhook_headers([KEY], "msg_1", 1000, BODY)
        assert (await client.post("/webhook", content=BODY, headers=headers)).status_code == 200
        assert (await client.post("/webhook", content=BODY, headers=headers)).status_code == 200
        assert receiver.queue == [BODY]
        assert receiver.processed == []
        forged = {**headers, "webhook-id": "msg_2"}
        assert (await client.post("/webhook", content=BODY, headers=forged)).status_code == 401
    assert receiver.seen == {"msg_1"}
    assert receiver.drain() == 1
    assert receiver.processed == [BODY]


class RecordingTransport(httpx.MockTransport):
    """A scripted receiver: one status (or exception) per request, plus a request log."""

    def __init__(
        self,
        script: Sequence[int | Exception],
        extra: Mapping[int, Mapping[str, str]] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._script = list(script)
        self._extra = extra or {}
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self._script[min(len(self.requests), len(self._script)) - 1]
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step, headers=dict(self._extra.get(step, {})))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sender_retries_with_the_same_id_a_fresh_timestamp_and_the_schedule() -> None:
    clock = ManualClock(1000.0)
    transport = RecordingTransport([503, 500, 200])
    async with httpx.AsyncClient(transport=transport) as client:
        result = await WebhookSender(client, clock).deliver(
            "https://r.example/h", [KEY], "msg_1", BODY
        )
    assert result.delivered
    assert not result.disabled
    assert [a.status for a in result.attempts] == [503, 500, 200]
    assert [a.at for a in result.attempts] == [1000.0, 1005.0, 1305.0]
    assert clock.now == 1305.0
    assert {r.headers["webhook-id"] for r in transport.requests} == {"msg_1"}
    assert [r.headers["webhook-timestamp"] for r in transport.requests] == ["1000", "1005", "1305"]
    assert len({r.headers["webhook-signature"] for r in transport.requests}) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sender_stops_on_410_and_never_follows_a_redirect() -> None:
    async with httpx.AsyncClient(transport=RecordingTransport([410])) as client:
        gone = await WebhookSender(client, ManualClock()).deliver(
            "https://r.example/h", [KEY], "m", BODY
        )
    assert gone.disabled
    assert not gone.delivered
    assert len(gone.attempts) == 1

    redirect = RecordingTransport([301], extra={301: {"location": "https://elsewhere.example/"}})
    async with httpx.AsyncClient(transport=redirect) as client:
        sender = WebhookSender(client, ManualClock(), schedule=(1.0, 1.0))
        moved = await sender.deliver("https://r.example/h", [KEY], "m", BODY)
    assert not moved.delivered
    assert len(moved.attempts) == 3
    assert {str(r.url) for r in redirect.requests} == {"https://r.example/h"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sender_treats_a_transport_error_as_a_retryable_failure() -> None:
    transport = RecordingTransport([httpx.ConnectError("refused"), 200])
    async with httpx.AsyncClient(transport=transport) as client:
        sender = WebhookSender(client, ManualClock(), schedule=(2.0,))
        result = await sender.deliver("https://r.example/h", [KEY], "m", BODY)
    assert [a.status for a in result.attempts] == [None, 200]
    assert result.delivered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_after_can_lengthen_but_never_shorten_the_schedule() -> None:
    long_wait = RecordingTransport([429, 200], extra={429: {"retry-after": "60"}})
    async with httpx.AsyncClient(transport=long_wait) as client:
        sender = WebhookSender(client, ManualClock(), schedule=(5.0,))
        longer = await sender.deliver("https://r.example/h", [KEY], "m", BODY)
    assert longer.attempts[0].waited_next == 60.0

    short_wait = RecordingTransport([503, 200], extra={503: {"retry-after": "1"}})
    async with httpx.AsyncClient(transport=short_wait) as client:
        sender = WebhookSender(client, ManualClock(), schedule=(5.0,))
        shorter = await sender.deliver("https://r.example/h", [KEY], "m", BODY)
    assert shorter.attempts[0].waited_next == 5.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_permanently_failing_endpoint_exhausts_the_whole_75_hour_schedule() -> None:
    clock = ManualClock()
    transport = RecordingTransport([500])
    async with httpx.AsyncClient(transport=transport) as client:
        result = await WebhookSender(client, clock).deliver("https://r.example/h", [KEY], "m", BODY)
    assert not result.delivered
    assert len(result.attempts) == len(STANDARD_WEBHOOKS_SCHEDULE) + 1 == 10
    assert result.attempts[-1].waited_next is None
    assert clock.now == sum(STANDARD_WEBHOOKS_SCHEDULE)


class LostAckTransport(httpx.AsyncBaseTransport):
    """Forward to the receiver, but drop the first response on the way back."""

    def __init__(self, app: ASGIApp) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("acknowledgement lost", request=request)
        return response


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_lost_acknowledgement_causes_a_duplicate_that_the_receiver_absorbs() -> None:
    clock = ManualClock(1000.0)
    receiver = WebhookReceiver([KEY], clock)
    transport = LostAckTransport(receiver.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        result = await WebhookSender(client, clock).deliver("/webhook", [KEY], "msg_1", BODY)
    assert result.delivered
    assert [a.status for a in result.attempts] == [None, 200]
    assert transport.calls == 2
    assert receiver.drain() == 1
    assert receiver.processed == [BODY]


# --- 4. SSRF guard ---------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/cb",
        "ftp://example.com/cb",
        "https://user:pw@example.com/cb",
        "https:///cb",
        "https://127.0.0.1/cb",
        "https://[::1]/cb",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/cb",
        "https://100.64.0.1/cb",
        "https://[::ffff:127.0.0.1]/cb",
        "https://2130706433/cb",
        "https://0x7f.1/cb",
        "https://127.1/cb",
        "https://internal.example/cb",
        "https://mixed.example/cb",
        "https://nowhere.example/cb",
    ],
)
def test_ssrf_guard_refuses_every_route_to_an_internal_address(url: str) -> None:
    with pytest.raises(UnsafeCallbackError):
        validate_callback_url(url, resolve_lab)


@pytest.mark.unit
def test_ssrf_guard_accepts_global_addresses_and_http_only_when_allowed() -> None:
    assert validate_callback_url("https://example.com/cb?a=1", resolve_lab)
    assert validate_callback_url("https://[2606:4700:4700::1111]/cb", resolve_lab)
    assert validate_callback_url("http://example.com/cb", resolve_lab, allow_http=True)


# --- 5. WebSub -------------------------------------------------------------------------


class Flaky:
    """ASGI wrapper: answer the first ``failures`` distribution POSTs with ``status``."""

    def __init__(self, inner: ASGIApp, failures: int, status: int = 503) -> None:
        self._inner = inner
        self.failures = failures
        self._status = status

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and self.failures > 0:
            self.failures -= 1
            headers = {"location": "https://elsewhere.example/"} if self._status == 307 else {}
            await Response(status_code=self._status, headers=headers)(scope, receive, send)
            return
        await self._inner(scope, receive, send)


@dataclass
class Rig:
    hub: WebSubHub
    sub: WebSubSubscriber
    hub_client: httpx.AsyncClient
    clock: ManualClock
    hub_requests: list[httpx.Request] = field(default_factory=lambda: list[httpx.Request]())

    async def subscribe(
        self, mode: str = "subscribe", extra: Mapping[str, str] | None = None
    ) -> httpx.Response:
        data = {"hub.mode": mode, "hub.callback": self.sub.callback_url, "hub.topic": TOPIC}
        return await self.hub_client.post("/", data={**data, **(extra or {})})


@asynccontextmanager
async def rig(
    *,
    secret: bytes | None = None,
    wrap: Callable[[ASGIApp], Flaky] | None = None,
    retry_delays: Sequence[float] = (1.0, 5.0),
) -> AsyncGenerator[Rig]:
    clock = ManualClock(5000.0)
    sub = WebSubSubscriber(TOPIC, "tok-8f3a1c", secret)
    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> None:
        seen.append(request)

    target: ASGIApp = wrap(sub.app) if wrap else sub.app
    out = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=target), event_hooks={"request": [record]}
    )
    hub = WebSubHub(out, clock, resolve_lab, topics=[TOPIC], retry_delays=retry_delays)
    async with out, asgi(hub.app, "https://hub.example") as hub_client:
        yield Rig(hub, sub, hub_client, clock, seen)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subscription_request_is_202_and_only_active_after_intent_verification() -> None:
    async with rig() as r:
        r.sub.intent = "subscribe"
        response = await r.subscribe(extra={"hub.lease_seconds": "600"})
        assert response.status_code == 202
        assert len(r.hub.pending) == 1
        assert not r.hub.subscriptions
        assert not r.hub_requests
        await r.hub.process_pending()
        assert [q.method for q in r.hub_requests] == ["GET"]
        query = dict(r.hub_requests[0].url.params)
        assert query["hub.mode"] == "subscribe"
        assert query["hub.topic"] == TOPIC
        assert query["hub.challenge"] == "challenge-0001"
        assert query["hub.lease_seconds"] == "600"
        (sub,) = r.hub.subscriptions.values()
        assert sub.expires_at == 5600.0
        assert r.sub.lease_seconds == 600


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsolicited_subscription_is_202_then_dies_at_verification() -> None:
    async with rig() as r:
        assert r.sub.intent is None
        assert (await r.subscribe()).status_code == 202
        await r.hub.process_pending()
        assert not r.hub.subscriptions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_challenge_must_be_echoed_byte_for_byte() -> None:
    async def wrong_echo(scope: Scope, receive: Receive, send: Send) -> None:
        await Response("not-the-challenge")(scope, receive, send)

    clock = ManualClock()
    async with asgi(wrong_echo) as out:
        hub = WebSubHub(out, clock, resolve_lab, topics=[TOPIC])
        async with asgi(hub.app) as hub_client:
            sub = WebSubSubscriber(TOPIC, "tok")
            data = {"hub.mode": "subscribe", "hub.callback": sub.callback_url, "hub.topic": TOPIC}
            assert (await hub_client.post("/", data=data)).status_code == 202
        await hub.process_pending()
    assert not hub.subscriptions


@pytest.mark.unit
def test_verification_query_is_appended_after_the_callback_query() -> None:
    url = append_query("https://s.example/cb?foo=bar&hub.mode=mine", {"hub.mode": "subscribe"})
    assert url == "https://s.example/cb?foo=bar&hub.mode=mine&hub.mode=subscribe"
    assert append_query("https://s.example/cb", {"a": "b c"}) == "https://s.example/cb?a=b+c"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lease_is_capped_at_ten_days_and_enforced_at_publish_time() -> None:
    async with rig() as r:
        r.sub.intent = "subscribe"
        await r.subscribe(extra={"hub.lease_seconds": "99999999"})
        await r.hub.process_pending()
        assert r.sub.lease_seconds == MAX_LEASE_SECONDS == 864_000
        r.clock.advance(MAX_LEASE_SECONDS - 1)
        assert len(await r.hub.publish(TOPIC, b"x", "text/plain")) == 1
        r.clock.advance(1)
        assert await r.hub.publish(TOPIC, b"x", "text/plain") == []
        assert not r.hub.subscriptions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_distribution_is_signed_links_hub_and_self_and_reaches_the_subscriber() -> None:
    secret = b"websub-secret-EXAMPLE"
    async with rig(secret=secret) as r:
        r.sub.intent = "subscribe"
        await r.subscribe(extra={"hub.secret": secret.decode()})
        await r.hub.process_pending()
        (record,) = await r.hub.publish(TOPIC, b"<feed/>", "application/atom+xml")
        assert record.delivered
        assert record.statuses == (204,)
        assert r.sub.received == [b"<feed/>"]
        post = r.hub_requests[-1]
        digest = hmac.new(secret, b"<feed/>", hashlib.sha256).hexdigest()
        assert post.headers["x-hub-signature"] == f"sha256={digest}"
        assert post.headers["content-type"] == "application/atom+xml"
        assert post.headers["link"] == f'<https://hub.example/>; rel="hub", <{TOPIC}>; rel="self"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subscriber_drops_forged_unsigned_and_sha1_distributions_but_acks_them() -> None:
    secret = b"websub-secret-EXAMPLE"
    sub = WebSubSubscriber(TOPIC, "tok", secret)
    body = b"payload"
    good = hmac.new(secret, body, hashlib.sha256).hexdigest()
    sha1 = hmac.new(secret, body, hashlib.sha1).hexdigest()
    async with asgi(sub.app) as client:
        cases = {"sha256=" + "0" * 64: 202, "": 202, f"sha1={sha1}": 202, f"sha256={good}": 204}
        for header, expected in cases.items():
            response = await client.post(
                f"/cb/{sub.token}", content=body, headers={"x-hub-signature": header}
            )
            assert response.status_code == expected
    assert sub.received == [body]
    assert sub.rejected == [
        "bad signature",
        "missing or weak signature",
        "missing or weak signature",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_deliveries_are_retried_and_the_subscription_survives_exhaustion() -> None:
    holder: list[Flaky] = []

    def wrap(app: ASGIApp) -> Flaky:
        holder.append(Flaky(app, failures=5))
        return holder[0]

    async with rig(wrap=wrap) as r:
        r.sub.intent = "subscribe"
        await r.subscribe()
        await r.hub.process_pending()
        (failed,) = await r.hub.publish(TOPIC, b"one", "text/plain")
        assert failed.statuses == (503, 503, 503)
        assert not failed.delivered
        assert len(r.hub.subscriptions) == 1
        holder[0].failures = 2
        (healed,) = await r.hub.publish(TOPIC, b"two", "text/plain")
        assert healed.statuses == (503, 503, 204)
        assert healed.delivered
        assert r.sub.received == [b"two"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_410_from_the_subscriber_terminates_the_subscription() -> None:
    async with rig() as r:
        r.sub.intent = "subscribe"
        await r.subscribe()
        await r.hub.process_pending()
        r.sub.gone = True
        (record,) = await r.hub.publish(TOPIC, b"x", "text/plain")
        assert record.statuses == (410,)
        assert not record.delivered
        assert not r.hub.subscriptions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_redirect_from_the_callback_is_a_failure_and_is_not_followed() -> None:
    async with rig(wrap=lambda app: Flaky(app, failures=9, status=307)) as r:
        r.sub.intent = "subscribe"
        await r.subscribe()
        await r.hub.process_pending()
        (record,) = await r.hub.publish(TOPIC, b"x", "text/plain")
        assert record.statuses == (307, 307, 307)
        assert {q.url.host for q in r.hub_requests} == {"subscriber.example"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsubscribe_needs_the_same_verification_as_subscribe() -> None:
    async with rig() as r:
        r.sub.intent = "subscribe"
        await r.subscribe()
        await r.hub.process_pending()
        await r.subscribe("unsubscribe")
        await r.hub.process_pending()  # subscriber still intends "subscribe": 404
        assert len(r.hub.subscriptions) == 1
        r.sub.intent = "unsubscribe"
        await r.subscribe("unsubscribe")
        await r.hub.process_pending()
        assert not r.hub.subscriptions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hub_refuses_ssrf_callbacks_before_queueing_any_verification() -> None:
    async with rig() as r:
        for callback in ("https://169.254.169.254/latest/", "https://internal.example/cb"):
            data = {"hub.mode": "subscribe", "hub.callback": callback, "hub.topic": TOPIC}
            response = await r.hub_client.post("/", data=data)
            assert response.status_code == 400
            assert "unsafe callback" in response.text
        assert not r.hub.pending
        assert not r.hub_requests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hub_validates_the_subscription_request_parameters() -> None:
    async with rig() as r:
        base = {"hub.mode": "subscribe", "hub.callback": r.sub.callback_url, "hub.topic": TOPIC}
        post = r.hub_client.post
        assert (
            await post("/", content=b"x", headers={"content-type": "text/plain"})
        ).status_code == 415
        assert (await post("/", data={**base, "hub.mode": "poke"})).status_code == 400
        assert (
            await post("/", data={k: v for k, v in base.items() if k != "hub.topic"})
        ).status_code == 400
        assert (
            await post("/", data={**base, "hub.topic": "https://other.example/"})
        ).status_code == 404
        assert (await post("/", data={**base, "hub.secret": "s" * 200})).status_code == 400
        assert (await post("/", data={**base, "hub.secret": "s" * 199})).status_code == 202
        for lease in ("0", "abc", "-5"):
            assert (await post("/", data={**base, "hub.lease_seconds": lease})).status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_subscriber_refuses_unknown_tokens_unsafe_challenges_and_serves_octet_stream() -> (
    None
):
    sub = WebSubSubscriber(TOPIC, "tok")
    sub.intent = "subscribe"
    verify = {"hub.mode": "subscribe", "hub.topic": TOPIC, "hub.challenge": "abc-123"}
    async with asgi(sub.app) as client:
        assert (await client.get("/cb/other", params=verify)).status_code == 404
        evil = {**verify, "hub.challenge": "<script>alert(1)</script>"}
        assert (await client.get("/cb/tok", params=evil)).status_code == 404
        ok = await client.get("/cb/tok", params=verify)
        assert ok.status_code == 200
        assert ok.text == "abc-123"
        assert ok.headers["content-type"] == "application/octet-stream"
        assert ok.headers["x-content-type-options"] == "nosniff"
        denied = await client.get("/cb/tok", params={"hub.mode": "denied", "hub.topic": TOPIC})
        assert denied.status_code == 200
        assert sub.intent is None


# --- 6. 202 / Location / Retry-After / 303 ---------------------------------------------


@asynccontextmanager
async def jobs(ttl: float = 3600.0) -> AsyncGenerator[tuple[httpx.AsyncClient, ManualClock]]:
    clock = ManualClock()
    async with asgi(JobService(clock, ttl=ttl).app) as client:
        yield client, clock


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_is_202_with_location_and_retry_after() -> None:
    async with jobs() as (client, _):
        response = await client.post("/jobs", json={"duration": 30})
        assert response.status_code == 202
        assert response.headers["location"] == "/jobs/job-1"
        assert response.headers["retry-after"] == "30"
        assert response.json() == {"id": "job-1", "status": "running"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_jobs_answer_inline_unless_the_client_prefers_respond_async() -> None:
    async with jobs() as (client, _):
        inline = await client.post("/jobs", json={"duration": 1})
        assert inline.status_code == 200
        assert inline.json()["status"] == "succeeded"
        deferred = await client.post(
            "/jobs", json={"duration": 1}, headers={"prefer": "respond-async"}
        )
        assert deferred.status_code == 202
        assert "location" in deferred.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_polling_client_sleeps_retry_after_and_follows_303_with_get() -> None:
    async with jobs() as (client, clock):
        created = await client.post("/jobs", json={"duration": 30})
        trace = await poll_job(client, clock, created.headers["location"])
        assert trace.requests == 3
        assert trace.waits == (30.0,)
        assert trace.result == {"id": "job-1", "result": {"duration": 30.0}}
        assert (await client.get("/jobs/job-2")).status_code == 404  # no second job was created


@pytest.mark.unit
@pytest.mark.asyncio
async def test_303_status_response_points_at_the_result_resource() -> None:
    async with jobs() as (client, clock):
        await client.post("/jobs", json={"duration": 30})
        clock.advance(30)
        done = await client.get("/jobs/job-1")
        assert done.status_code == 303
        assert done.headers["location"] == "/jobs/job-1/result"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_after_counts_down_and_never_drops_below_one_second() -> None:
    async with jobs() as (client, clock):
        await client.post("/jobs", json={"duration": 30})
        clock.advance(10)
        assert (await client.get("/jobs/job-1")).headers["retry-after"] == "20"
        clock.advance(19.5)
        assert (await client.get("/jobs/job-1")).headers["retry-after"] == "1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_idempotency_key_replays_the_same_job_and_rejects_a_changed_body() -> None:
    async with jobs() as (client, _):
        headers = {"idempotency-key": "k-1"}
        first = await client.post("/jobs", json={"duration": 30}, headers=headers)
        again = await client.post("/jobs", json={"duration": 30}, headers=headers)
        assert first.headers["location"] == again.headers["location"] == "/jobs/job-1"
        assert (await client.get("/jobs/job-2")).status_code == 404
        changed = await client.post("/jobs", json={"duration": 99}, headers=headers)
        assert changed.status_code == 422
        assert changed.headers["content-type"] == "application/problem+json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_marks_the_job_canceled_and_a_second_cancel_conflicts() -> None:
    async with jobs() as (client, clock):
        await client.post("/jobs", json={"duration": 30})
        assert (await client.delete("/jobs/job-1")).status_code == 204
        assert (await client.get("/jobs/job-1")).json() == {"id": "job-1", "status": "canceled"}
        assert (await client.get("/jobs/job-1/result")).status_code == 404
        assert (await client.delete("/jobs/job-1")).status_code == 409
        await client.post("/jobs", json={"duration": 30})
        clock.advance(30)
        assert (await client.delete("/jobs/job-2")).status_code == 409


@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_expires_to_410_gone_while_an_unknown_job_is_404() -> None:
    async with jobs(ttl=100.0) as (client, clock):
        await client.post("/jobs", json={"duration": 30})
        clock.advance(129)
        assert (await client.get("/jobs/job-1/result")).status_code == 200
        clock.advance(1)
        for path in ("/jobs/job-1", "/jobs/job-1/result"):
            gone = await client.get(path)
            assert gone.status_code == 410
            assert gone.headers["content-type"] == "application/problem+json"
        assert (await client.get("/jobs/never-existed")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_bad_body_is_an_rfc_9457_problem_and_an_unfinished_result_is_404() -> None:
    async with jobs() as (client, _):
        bad = await client.post("/jobs", content=b"not json")
        assert bad.status_code == 400
        assert bad.headers["content-type"] == "application/problem+json"
        assert bad.json()["type"] == "about:blank"
        assert bad.json()["status"] == 400
        await client.post("/jobs", json={"duration": 30})
        assert (await client.get("/jobs/job-1/result")).status_code == 404


# --- 7. SSE ----------------------------------------------------------------------------


@pytest.mark.unit
def test_multi_line_data_becomes_data_lines_and_never_injects_a_field() -> None:
    assert encode_event(SseEvent("7", "status", "line1\nline2")) == (
        b"id: 7\nevent: status\ndata: line1\ndata: line2\n\n"
    )
    hostile = SseEvent("1", "message", "x\n\nevent: evil\nid: 999")
    parsed = parse_stream(encode_event(hostile).decode())
    assert len(parsed.events) == 1
    assert parsed.events[0].event == "message"
    assert parsed.last_event_id == "1"
    assert parsed.events[0].data == "x\n\nevent: evil\nid: 999"
    for bad in (
        SseEvent("a\nb", "message", "d"),
        SseEvent("1", "x\ry", "d"),
        SseEvent("a\x00", "m", "d"),
    ):
        with pytest.raises(ValueError, match="must not contain"):
            encode_event(bad)


@pytest.mark.unit
def test_parser_follows_the_whatwg_rules_for_comments_spaces_ids_and_partial_blocks() -> None:
    assert parse_stream(": just a comment\n\n").events == ()
    spaced = parse_stream("data:test\n\ndata: test\n\n")
    assert [e.data for e in spaced.events] == ["test", "test"]
    kept = parse_stream("id: 1\ndata: a\n\ndata: b\n\n")
    assert [e.id for e in kept.events] == ["1", "1"]
    assert kept.last_event_id == "1"
    truncated = parse_stream("data: a\n\ndata: b")
    assert [e.data for e in truncated.events] == ["a"]
    assert parse_stream("retry: 3000\n\n").retry_ms == 3000
    assert parse_stream("retry: soon\n\n").retry_ms is None
    assert parse_stream("id: 2\x00x\ndata: a\n\n").last_event_id == ""
    assert parse_stream("event: ping\r\ndata: a\r\n\r\n").events[0].event == "ping"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconnect_with_last_event_id_replays_only_the_gap() -> None:
    log = EventLog()
    log.append("status", "queued")
    log.append("status", "running")
    async with asgi(notification_app(log)) as client:
        first = await client.get("/events")
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["cache-control"] == "no-store"
        assert first.headers["x-accel-buffering"] == "no"
        seen = parse_stream(first.text)
        assert seen.retry_ms == 3000
        assert [e.id for e in seen.events] == ["1", "2"]
        assert KEEPALIVE.decode() in first.text  # log still open: the real stream would wait
        log.append("status", "succeeded", terminal=True)
        resumed = await client.get("/events", headers={"last-event-id": seen.last_event_id})
        gap = parse_stream(resumed.text)
        assert [e.id for e in gap.events] == ["3"]
        assert gap.events[0].data == "succeeded"
        assert KEEPALIVE.decode() not in resumed.text
        everything = parse_stream(
            (await client.get("/events", headers={"last-event-id": "junk"})).text
        )
        assert [e.id for e in everything.events] == ["1", "2", "3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_finished_stream_answers_204_so_the_browser_stops_reconnecting() -> None:
    log = EventLog()
    log.append("status", "succeeded", terminal=True)
    async with asgi(notification_app(log)) as client:
        done = await client.get("/events", headers={"last-event-id": "1"})
        assert done.status_code == 204
        assert done.content == b""
        assert (await client.get("/events")).status_code == 200
