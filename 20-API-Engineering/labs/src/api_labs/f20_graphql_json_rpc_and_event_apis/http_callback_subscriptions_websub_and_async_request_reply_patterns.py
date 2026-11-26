"""Asynchronous request-reply topologies: polling, WebSub, signed webhooks, SSE.

Nothing here opens a socket or sleeps.
Time is a ``ManualClock`` that tests advance, and every HTTP hop runs through
``httpx.ASGITransport`` or ``httpx.MockTransport`` so the real header and status-code
semantics are exercised in process.

Sections, in file order:

1. ``ManualClock`` and a closed-form cost model of the four topologies.
2. ``parse_retry_after`` and the retry schedule helpers.
3. Standard Webhooks signing, a verifying receiver, and a retrying sender.
4. ``validate_callback_url``, the SSRF guard a subscription endpoint needs.
5. A WebSub hub and subscriber (W3C Recommendation).
6. The ``202`` + ``Location`` + ``Retry-After`` + ``303`` job API.
7. Server-Sent Events encoding, parsing, and ``Last-Event-ID`` resume.
"""

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import repeat
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

type Clock = Callable[[], float]


class ManualClock:
    """A virtual clock: nothing in the lab sleeps, tests advance time explicitly."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot run backwards")
        self.now += seconds


# --- 1. Cost model ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one operation costs a topology, and how long the client learns of it late.

    ``requests`` counts HTTP requests, ``empty`` the requests that carried no news,
    ``keepalives`` the comment frames an open SSE stream spent, and ``latency`` the
    seconds between completion and the client knowing (``None`` if never within the horizon).
    """

    topology: str
    requests: int
    empty: int
    keepalives: int
    latency: float | None


def backoff_delays(first: float, factor: float, cap: float) -> Iterator[float]:
    """Yield ``first``, ``first * factor``, ... clamped at ``cap``, forever."""
    delay = first
    while True:
        yield delay
        delay = min(cap, delay * factor)


def fixed_delays(interval: float) -> Iterator[float]:
    return repeat(interval)


def poll(completes_at: float, delays: Iterator[float], horizon: float) -> Outcome:
    """Poll after each delay; the first poll at or after ``completes_at`` sees the result."""
    clock = 0.0
    for requests, delay in enumerate(delays):
        clock += delay
        if clock > horizon:
            return Outcome("poll", requests, requests, 0, None)
        if clock >= completes_at:
            return Outcome("poll", requests + 1, requests, 0, clock - completes_at)
    raise RuntimeError("poll() needs an infinite delay iterator")


def webhook(completes_at: float, retry_delays: Sequence[float], receiver_up_at: float) -> Outcome:
    """Push at completion, then retry after each delay until the receiver is up."""
    clock = completes_at
    attempts = 0
    for delay in (0.0, *retry_delays):
        clock += delay
        attempts += 1
        if clock >= receiver_up_at:
            return Outcome("webhook", attempts, attempts - 1, 0, clock - completes_at)
    return Outcome("webhook", attempts, attempts, 0, None)


def sse(
    completes_at: float,
    heartbeat: float = 15.0,
    drop_at: float | None = None,
    reconnect_delay: float = 3.0,
) -> Outcome:
    """One long-lived request; a drop before completion is healed by ``Last-Event-ID`` replay."""
    keepalives = int(completes_at // heartbeat)
    if drop_at is None or drop_at > completes_at:
        return Outcome("sse", 1, 0, keepalives, 0.0)
    return Outcome("sse", 2, 0, keepalives, max(0.0, drop_at + reconnect_delay - completes_at))


# --- 2. Retry helpers ------------------------------------------------------------------

# Example retry schedule, Standard Webhooks 1.0.0 "Deliverability and reliability".
STANDARD_WEBHOOKS_SCHEDULE: tuple[float, ...] = (
    5.0,
    300.0,
    1800.0,
    7200.0,
    18000.0,
    36000.0,
    50400.0,
    72000.0,
    86400.0,
)


def parse_retry_after(value: str, now: datetime) -> float:
    """Seconds to wait for a ``Retry-After`` value (RFC 9110 Section 10.2.3).

    ``now`` must be timezone-aware.
    Raises ``ValueError`` for anything that is neither ``delay-seconds`` nor an HTTP-date.
    """
    stripped = value.strip()
    if stripped.isascii() and stripped.isdigit():
        return float(int(stripped))
    when = parsedate_to_datetime(stripped)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def jittered(delay: float, fraction: float, rng: Callable[[], float]) -> float:
    """Spread ``delay`` over ``[delay * (1 - fraction), delay * (1 + fraction)]``."""
    return delay * (1.0 + fraction * (2.0 * rng() - 1.0))


# --- 3. Standard Webhooks --------------------------------------------------------------


class WebhookVerificationError(Exception):
    """The delivery is not authentic, not fresh, or not well formed."""


def sign_message(key: bytes, msg_id: str, timestamp: int, payload: bytes) -> str:
    """``v1,`` + base64 of HMAC-SHA256 over ``msg_id.timestamp.payload``."""
    if "." in msg_id:
        raise ValueError("msg_id must not contain '.'")
    signed = f"{msg_id}.{timestamp}.".encode() + payload
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def webhook_headers(
    keys: Sequence[bytes], msg_id: str, timestamp: int, payload: bytes
) -> dict[str, str]:
    """Headers for one attempt; several keys sign together for zero-downtime rotation."""
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": " ".join(sign_message(k, msg_id, timestamp, payload) for k in keys),
    }


def verify_message(
    keys: Sequence[bytes],
    headers: Mapping[str, str],
    payload: bytes,
    *,
    now: int,
    tolerance: int = 300,
) -> str:
    """Return the ``webhook-id`` of an authentic, fresh delivery, else raise.

    ``payload`` must be the raw request bytes, never a re-serialised parse.
    """
    try:
        msg_id = headers["webhook-id"]
        presented = headers["webhook-signature"].split(" ")
        timestamp = int(headers["webhook-timestamp"])
    except (KeyError, ValueError) as exc:
        raise WebhookVerificationError("missing or malformed webhook header") from exc
    if tolerance <= 0:
        raise ValueError("tolerance must be positive; zero would disable the replay check")
    if abs(now - timestamp) > tolerance:
        raise WebhookVerificationError("timestamp outside tolerance")
    try:
        expected = [sign_message(k, msg_id, timestamp, payload).encode() for k in keys]
    except ValueError as exc:
        raise WebhookVerificationError("malformed webhook-id") from exc
    matched = False
    for candidate in expected:
        for signature in presented:
            matched |= hmac.compare_digest(candidate, signature.encode())
    if not matched:
        raise WebhookVerificationError("no valid signature")
    return msg_id


class WebhookReceiver:
    """Verify, deduplicate on ``webhook-id``, enqueue, and acknowledge before processing."""

    def __init__(self, keys: Sequence[bytes], clock: Clock, tolerance: int = 300) -> None:
        self._keys = keys
        self._clock = clock
        self._tolerance = tolerance
        self.seen: set[str] = set()
        self.queue: list[bytes] = []
        self.processed: list[bytes] = []
        self.app = Starlette(routes=[Route("/webhook", self._endpoint, methods=["POST"])])

    async def _endpoint(self, request: Request) -> Response:
        body = await request.body()
        try:
            msg_id = verify_message(
                self._keys, request.headers, body, now=int(self._clock()), tolerance=self._tolerance
            )
        except WebhookVerificationError:
            return Response(status_code=401)
        if msg_id not in self.seen:
            self.seen.add(msg_id)
            self.queue.append(body)
        return Response(status_code=200)

    def drain(self) -> int:
        """Process the queue after the acknowledgement has gone out."""
        count = len(self.queue)
        self.processed.extend(self.queue)
        self.queue.clear()
        return count


@dataclass(frozen=True, slots=True)
class Attempt:
    at: float
    status: int | None
    waited_next: float | None


@dataclass(slots=True)
class DeliveryResult:
    delivered: bool = False
    disabled: bool = False
    attempts: list[Attempt] = field(default_factory=lambda: list[Attempt]())


class WebhookSender:
    """At-least-once delivery with the Standard Webhooks status-code policy.

    ``2xx`` succeeds, ``410`` disables the endpoint, a ``3xx`` is a failure (redirects are
    never followed), and everything else retries on the schedule, stretched by ``Retry-After``.
    Every attempt re-signs with a fresh timestamp and the same ``webhook-id``.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: ManualClock,
        schedule: Sequence[float] = STANDARD_WEBHOOKS_SCHEDULE,
    ) -> None:
        self._client = client
        self._clock = clock
        self._schedule = schedule

    async def deliver(
        self, url: str, keys: Sequence[bytes], msg_id: str, payload: bytes
    ) -> DeliveryResult:
        result = DeliveryResult()
        for index in range(len(self._schedule) + 1):
            headers = webhook_headers(keys, msg_id, int(self._clock()), payload)
            headers["content-type"] = "application/json"
            started = self._clock()
            retry_after: str | None = None
            try:
                response = await self._client.post(url, content=payload, headers=headers)
            except httpx.TransportError:
                status = None
            else:
                status = response.status_code
                retry_after = response.headers.get("retry-after")
            if status is not None and 200 <= status < 300:
                result.delivered = True
                result.attempts.append(Attempt(started, status, None))
                return result
            if status == 410:
                result.disabled = True
                result.attempts.append(Attempt(started, status, None))
                return result
            if index == len(self._schedule):
                result.attempts.append(Attempt(started, status, None))
                return result
            wait = self._schedule[index]
            if retry_after is not None:
                now = datetime.fromtimestamp(started, UTC)
                with contextlib.suppress(ValueError, TypeError):
                    wait = max(wait, parse_retry_after(retry_after, now))
            result.attempts.append(Attempt(started, status, wait))
            self._clock.advance(wait)
        return result


# --- 4. SSRF guard ---------------------------------------------------------------------


class UnsafeCallbackError(ValueError):
    """The callback URL points somewhere a hub must never connect."""


def _is_forbidden_address(text: str) -> bool:
    addr = ipaddress.ip_address(text)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not addr.is_global or addr.is_multicast


def validate_callback_url(
    url: str, resolve: Callable[[str], Sequence[str]], *, allow_http: bool = False
) -> str:
    """Return ``url`` if every address it can reach is global unicast, else raise.

    ``resolve`` maps a hostname to its addresses.
    The caller must then connect to one of the addresses checked here, not resolve again.
    """
    parts = urlsplit(url)
    allowed = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in allowed:
        raise UnsafeCallbackError(f"scheme {parts.scheme!r} not allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeCallbackError("userinfo not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeCallbackError("no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        last_label = host.rstrip(".").rsplit(".", 1)[-1]
        if last_label.isdigit() or last_label.lower().startswith("0x"):
            raise UnsafeCallbackError("non-canonical numeric host") from None
        addresses = list(resolve(host))
        if not addresses:
            raise UnsafeCallbackError("host does not resolve") from None
    else:
        addresses = [host]
    for address in addresses:
        if _is_forbidden_address(address):
            raise UnsafeCallbackError(f"{address} is not a global unicast address")
    return url


# --- 5. WebSub -------------------------------------------------------------------------

MAX_LEASE_SECONDS = 864_000  # 10 days, the default WebSub Section 8.2 suggests
MAX_SECRET_BYTES = 199  # "MUST be less than 200 bytes" (WebSub Section 5.1)
SUBSCRIBER_HASHES = ("sha256", "sha384", "sha512")  # WebSub Section 8.3: minimum SHA-256


def append_query(url: str, params: Mapping[str, str]) -> str:
    """Append ``params`` after any existing query, as WebSub Section 5.1.1 requires."""
    parts = urlsplit(url)
    extra = urlencode(params)
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))


@dataclass(slots=True)
class Subscription:
    callback: str
    topic: str
    secret: bytes | None
    expires_at: float


@dataclass(frozen=True, slots=True)
class PendingRequest:
    mode: str
    callback: str
    topic: str
    secret: bytes | None
    lease_seconds: int


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    callback: str
    statuses: tuple[int | None, ...]
    delivered: bool


class WebSubHub:
    """The hub role: accept ``202``, verify intent asynchronously, enforce leases, fan out."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: ManualClock,
        resolve: Callable[[str], Sequence[str]],
        *,
        topics: Sequence[str],
        hub_url: str = "https://hub.example/",
        challenge: Callable[[], str] = lambda: "challenge-0001",
        retry_delays: Sequence[float] = (1.0, 5.0),
    ) -> None:
        self._client = client
        self._clock = clock
        self._resolve = resolve
        self._topics = set(topics)
        self._hub_url = hub_url
        self._challenge = challenge
        self._retry_delays = retry_delays
        self.pending: list[PendingRequest] = []
        self.subscriptions: dict[tuple[str, str], Subscription] = {}
        self.app = Starlette(routes=[Route("/", self._endpoint, methods=["POST"])])

    async def _endpoint(self, request: Request) -> Response:
        if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            return Response("form body required", status_code=415, media_type="text/plain")
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)

        def first(name: str) -> str | None:
            values = form.get(name)
            return values[0] if values else None

        mode, callback, topic = first("hub.mode"), first("hub.callback"), first("hub.topic")
        if mode not in ("subscribe", "unsubscribe") or not callback or not topic:
            return Response(
                "hub.mode, hub.callback, hub.topic required", 400, media_type="text/plain"
            )
        if topic not in self._topics:
            return Response("unknown topic", status_code=404, media_type="text/plain")
        secret = first("hub.secret")
        if secret is not None and len(secret.encode()) > MAX_SECRET_BYTES:
            return Response("hub.secret too long", status_code=400, media_type="text/plain")
        lease_text = first("hub.lease_seconds")
        lease = MAX_LEASE_SECONDS
        if lease_text is not None and mode == "subscribe":
            if not (lease_text.isascii() and lease_text.isdigit() and int(lease_text) > 0):
                return Response("bad hub.lease_seconds", 400, media_type="text/plain")
            lease = min(int(lease_text), MAX_LEASE_SECONDS)
        try:
            validate_callback_url(callback, self._resolve)
        except UnsafeCallbackError as exc:
            return Response(f"unsafe callback: {exc}", status_code=400, media_type="text/plain")
        self.pending.append(
            PendingRequest(mode, callback, topic, secret.encode() if secret else None, lease)
        )
        return Response(status_code=202)

    async def process_pending(self) -> None:
        """Run intent verification, which logically starts after the ``202`` went out."""
        batch, self.pending = self.pending, []
        for request in batch:
            await self._verify(request)

    async def _verify(self, request: PendingRequest) -> None:
        challenge = self._challenge()
        params = {"hub.mode": request.mode, "hub.topic": request.topic, "hub.challenge": challenge}
        if request.mode == "subscribe":
            params["hub.lease_seconds"] = str(request.lease_seconds)
        try:
            response = await self._client.get(append_query(request.callback, params))
        except httpx.TransportError:
            return
        if not (200 <= response.status_code < 300 and response.content == challenge.encode()):
            return  # verification failed: leave the previous state untouched
        key = (request.topic, request.callback)
        if request.mode == "subscribe":
            expires = self._clock() + request.lease_seconds
            self.subscriptions[key] = Subscription(
                request.callback, request.topic, request.secret, expires
            )
        else:
            self.subscriptions.pop(key, None)

    async def publish(self, topic: str, body: bytes, content_type: str) -> list[DeliveryRecord]:
        """Distribute ``body`` to every live subscription of ``topic``."""
        records: list[DeliveryRecord] = []
        for key, sub in list(self.subscriptions.items()):
            if sub.topic != topic:
                continue
            if sub.expires_at <= self._clock():
                del self.subscriptions[key]  # leases are enforced, never perpetual
                continue
            records.append(await self._deliver(key, sub, body, content_type))
        return records

    async def _deliver(
        self, key: tuple[str, str], sub: Subscription, body: bytes, content_type: str
    ) -> DeliveryRecord:
        headers = {
            "content-type": content_type,
            "link": f'<{self._hub_url}>; rel="hub", <{sub.topic}>; rel="self"',
        }
        if sub.secret is not None:
            digest = hmac.new(sub.secret, body, "sha256").hexdigest()
            headers["x-hub-signature"] = f"sha256={digest}"
        statuses: list[int | None] = []
        for delay in (0.0, *self._retry_delays):
            self._clock.advance(delay)
            try:
                status: int | None = (
                    await self._client.post(sub.callback, content=body, headers=headers)
                ).status_code
            except httpx.TransportError:
                status = None
            statuses.append(status)
            if status is not None and 200 <= status < 300:
                return DeliveryRecord(sub.callback, tuple(statuses), True)
            if status == 410:
                self.subscriptions.pop(key, None)
                break
        return DeliveryRecord(sub.callback, tuple(statuses), False)


class WebSubSubscriber:
    """The subscriber role: answer intent verification, authenticate distributions."""

    def __init__(self, topic: str, token: str, secret: bytes | None = None) -> None:
        self.topic = topic
        self.token = token
        self.secret = secret
        self.intent: str | None = None
        self.gone = False
        self.lease_seconds: int | None = None
        self.received: list[bytes] = []
        self.rejected: list[str] = []
        self.app = Starlette(routes=[Route("/cb/{token}", self._callback, methods=["GET", "POST"])])

    @property
    def callback_url(self) -> str:
        return f"https://subscriber.example/cb/{self.token}"

    async def _callback(self, request: Request) -> Response:
        if request.path_params["token"] != self.token:
            return Response(status_code=404)
        if request.method == "GET":
            return self._verify_intent(request)
        if self.gone:
            return Response(status_code=410)
        return await self._distribution(request)

    def _verify_intent(self, request: Request) -> Response:
        query = request.query_params
        mode = query.get("hub.mode")
        if mode == "denied":
            self.intent = None
            return Response(status_code=200)
        challenge = query.get("hub.challenge", "")
        sane = re.fullmatch(r"[+\-./0-9=A-Z_a-z]{1,200}", challenge) is not None
        if mode != self.intent or query.get("hub.topic") != self.topic or not sane:
            return Response(status_code=404)
        if mode == "subscribe":
            self.lease_seconds = int(query.get("hub.lease_seconds", "0"))
        return Response(
            challenge,
            media_type="application/octet-stream",
            headers={"x-content-type-options": "nosniff"},
        )

    async def _distribution(self, request: Request) -> Response:
        body = await request.body()
        if self.secret is not None:
            method, _, hexsig = request.headers.get("x-hub-signature", "").partition("=")
            if method not in SUBSCRIBER_HASHES:
                self.rejected.append("missing or weak signature")
                return Response(status_code=202)  # ack so the hub does not retry a forgery
            expected = hmac.new(self.secret, body, method).hexdigest()
            if not hmac.compare_digest(expected.encode(), hexsig.encode()):
                self.rejected.append("bad signature")
                return Response(status_code=202)
        self.received.append(body)
        return Response(status_code=204)


# --- 6. The 202 / Location / Retry-After / 303 job API ---------------------------------


@dataclass(slots=True)
class Job:
    job_id: str
    duration: float
    ready_at: float
    canceled: bool = False


def _problem(status: int, title: str, detail: str) -> Response:
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class JobService:
    """Long-running operations over plain HTTP.

    ``POST /jobs`` answers ``202`` with ``Location`` and ``Retry-After``, ``GET`` on the job
    answers ``200`` while running and ``303`` to ``/jobs/{id}/result`` when done, and a result
    older than ``ttl`` seconds answers ``410 Gone``.
    """

    def __init__(self, clock: Clock, *, ttl: float = 3600.0, sync_limit: float = 2.0) -> None:
        self._clock = clock
        self._ttl = ttl
        self._sync_limit = sync_limit
        self._jobs: dict[str, Job] = {}
        self._gone: set[str] = set()
        self._idempotency: dict[str, tuple[bytes, str]] = {}
        self.app = Starlette(
            routes=[
                Route("/jobs", self._create, methods=["POST"]),
                Route("/jobs/{job_id}", self._status, methods=["GET", "DELETE"]),
                Route("/jobs/{job_id}/result", self._result, methods=["GET"]),
            ]
        )

    def _purge(self) -> None:
        now = self._clock()
        for job_id, job in list(self._jobs.items()):
            if now >= job.ready_at + self._ttl:
                del self._jobs[job_id]
                self._gone.add(job_id)

    def _retry_after(self, job: Job) -> str:
        return str(max(1, math.ceil(job.ready_at - self._clock())))

    def _lookup(self, job_id: str) -> Job | Response:
        self._purge()
        if job_id in self._gone:
            return _problem(410, "Gone", "the result was retained for its TTL and then removed")
        job = self._jobs.get(job_id)
        if job is None:
            return _problem(404, "Not Found", "no such job")
        return job

    async def _create(self, request: Request) -> Response:
        self._purge()
        raw = await request.body()
        try:
            duration = float(json.loads(raw)["duration"])
        except (ValueError, KeyError, TypeError):
            return _problem(400, "Bad Request", 'body must be {"duration": <seconds>}')
        key = request.headers.get("idempotency-key")
        if key is not None and key in self._idempotency:
            fingerprint, job_id = self._idempotency[key]
            if fingerprint != raw:
                return _problem(422, "Unprocessable Content", "Idempotency-Key reused, new body")
            job = self._jobs[job_id]
        else:
            prefers = [t.strip().lower() for t in request.headers.get("prefer", "").split(",")]
            if duration <= self._sync_limit and "respond-async" not in prefers:
                return JSONResponse({"status": "succeeded", "result": {"duration": duration}})
            job_id = f"job-{len(self._jobs) + len(self._gone) + 1}"
            job = Job(job_id, duration, self._clock() + duration)
            self._jobs[job_id] = job
            if key is not None:
                self._idempotency[key] = (raw, job_id)
        return JSONResponse(
            {"id": job.job_id, "status": "running"},
            status_code=202,
            headers={"location": f"/jobs/{job.job_id}", "retry-after": self._retry_after(job)},
        )

    async def _status(self, request: Request) -> Response:
        found = self._lookup(request.path_params["job_id"])
        if isinstance(found, Response):
            return found
        if request.method == "DELETE":
            if found.canceled or self._clock() >= found.ready_at:
                return _problem(409, "Conflict", "job already finished")
            found.canceled = True
            return Response(status_code=204)
        if found.canceled:
            return JSONResponse({"id": found.job_id, "status": "canceled"})
        if self._clock() >= found.ready_at:
            return Response(status_code=303, headers={"location": f"/jobs/{found.job_id}/result"})
        return JSONResponse(
            {"id": found.job_id, "status": "running"},
            headers={"retry-after": self._retry_after(found)},
        )

    async def _result(self, request: Request) -> Response:
        found = self._lookup(request.path_params["job_id"])
        if isinstance(found, Response):
            return found
        if found.canceled or self._clock() < found.ready_at:
            return _problem(404, "Not Found", "no result for this job")
        return JSONResponse({"id": found.job_id, "result": {"duration": found.duration}})


@dataclass(frozen=True, slots=True)
class PollTrace:
    requests: int
    waits: tuple[float, ...]
    result: dict[str, object] | None


async def poll_job(
    client: httpx.AsyncClient, clock: ManualClock, location: str, max_polls: int = 50
) -> PollTrace:
    """A well-behaved client: sleep exactly ``Retry-After``, follow ``303`` with ``GET``."""
    requests = 0
    waits: list[float] = []
    for _ in range(max_polls):
        response = await client.get(location)
        requests += 1
        if response.status_code == 303:
            final = await client.get(response.headers["location"])
            requests += 1
            return PollTrace(requests, tuple(waits), final.json())
        if response.status_code != 200 or response.json()["status"] != "running":
            return PollTrace(requests, tuple(waits), None)
        wait = float(response.headers["retry-after"])
        waits.append(wait)
        clock.advance(wait)
    return PollTrace(requests, tuple(waits), None)


# --- 7. Server-Sent Events -------------------------------------------------------------

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
KEEPALIVE = b": keep-alive\n\n"


@dataclass(frozen=True, slots=True)
class SseEvent:
    id: str | None
    event: str
    data: str


def encode_event(event: SseEvent) -> bytes:
    """One event block; line breaks in ``data`` become extra ``data:`` lines, never fields."""
    for name, value in (("id", event.id or ""), ("event", event.event)):
        if _LINE_BREAK.search(value) or "\x00" in value:
            raise ValueError(f"{name} must not contain NUL, CR, or LF")
    lines: list[str] = []
    if event.id is not None:
        lines.append(f"id: {event.id}")
    if event.event != "message":
        lines.append(f"event: {event.event}")
    lines.extend(f"data: {line}" for line in _LINE_BREAK.split(event.data))
    return ("\n".join(lines) + "\n\n").encode()


@dataclass(frozen=True, slots=True)
class ParsedStream:
    events: tuple[SseEvent, ...]
    retry_ms: int | None
    last_event_id: str


def parse_stream(text: str) -> ParsedStream:
    """The WHATWG HTML Standard 9.2.5 and 9.2.6 parsing rules for a complete response body."""
    events: list[SseEvent] = []
    data: list[str] = []
    event_type = ""
    last_id = ""
    retry_ms: int | None = None
    for line in _LINE_BREAK.split(text)[:-1] if _LINE_BREAK.search(text) else []:
        if line == "":
            if data:
                events.append(SseEvent(last_id or None, event_type or "message", "\n".join(data)))
            data, event_type = [], ""
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            data.append(value)
        elif name == "event":
            event_type = value
        elif name == "id" and "\x00" not in value:
            last_id = value
        elif name == "retry" and value.isascii() and value.isdigit():
            retry_ms = int(value)
    return ParsedStream(tuple(events), retry_ms, last_id)


class EventLog:
    """An append-only log with monotonically increasing ids, the basis of resumable SSE."""

    def __init__(self) -> None:
        self._events: list[SseEvent] = []
        self.closed = False

    def append(self, event: str, data: str, *, terminal: bool = False) -> int:
        event_id = len(self._events) + 1
        self._events.append(SseEvent(str(event_id), event, data))
        self.closed = self.closed or terminal
        return event_id

    def since(self, last_event_id: int) -> list[SseEvent]:
        return self._events[last_event_id:]

    @property
    def last_id(self) -> int:
        return len(self._events)


def notification_app(log: EventLog, retry_ms: int = 3000) -> Starlette:
    """``GET /events``: replay after ``Last-Event-ID``; ``204`` once the stream is finished."""

    async def events(request: Request) -> Response:
        raw = request.headers.get("last-event-id", "")
        last = int(raw) if raw.isascii() and raw.isdigit() else 0
        if log.closed and last >= log.last_id:
            return Response(status_code=204)  # tells the browser to stop reconnecting

        async def body() -> AsyncIterator[bytes]:
            yield f"retry: {retry_ms}\n\n".encode()
            for item in log.since(last):
                yield encode_event(item)
            if not log.closed:
                yield KEEPALIVE  # a real server would now await new events

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    return Starlette(routes=[Route("/events", events, methods=["GET"])])
