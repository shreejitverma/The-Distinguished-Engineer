"""Lab: the draft-ietf-httpapi-idempotency-key-header-08 state machine, end to end over ASGI."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from api_labs.f16_resilience_rate_limiting_and_idempotency.idempotency_key import (
    IdempotencyConfig,
    IdempotencyMiddleware,
    IdempotencyStore,
    Outcome,
    SfParseError,
    State,
    StoredResponse,
    parse_idempotency_key,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

KEY = '"8e03978e-40d5-43e8-bc93-6894a57f9324"'
SCOPE_KEY = ("tenant-a", "8e03978e-40d5-43e8-bc93-6894a57f9324")


@dataclass
class FakeClock:
    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now


def tenant_of(scope: Scope) -> str:
    for name, value in scope["headers"]:
        if name == b"authorization":
            return bytes(value).decode("ascii").removeprefix("Bearer ")
    return "anonymous"


@dataclass
class Harness:
    client: httpx.AsyncClient
    store: IdempotencyStore
    clock: FakeClock
    calls: list[bytes]
    gate: asyncio.Event
    entered: asyncio.Event


def build(*, cache_server_errors: bool = False, gate_open: bool = True) -> Harness:
    clock = FakeClock()
    store = IdempotencyStore(ttl_seconds=86_400.0, lease_seconds=90.0, clock=clock)
    calls: list[bytes] = []
    gate = asyncio.Event()
    entered = asyncio.Event()
    if gate_open:
        gate.set()

    async def charges(request: Request) -> Response:
        body = await request.body()
        calls.append(body)
        entered.set()
        await gate.wait()
        payload = json.loads(body)
        if payload.get("card") == "declined":
            return JSONResponse({"error": "card_declined"}, status_code=402)
        if payload.get("card") == "boom":
            return JSONResponse({"error": "upstream"}, status_code=500)
        if payload.get("card") == "raise":
            raise RuntimeError("handler crashed")
        return JSONResponse(
            {"id": f"ch_{len(calls)}", "amount": payload["amount"]},
            status_code=201,
            headers={"x-charge-seq": str(len(calls))},
        )

    async def list_charges(request: Request) -> Response:
        calls.append(b"GET")
        return JSONResponse({"data": []})

    app = Starlette(
        routes=[
            Route("/v1/charges", charges, methods=["POST"]),
            Route("/v1/refunds", charges, methods=["POST"]),
            Route("/v1/charges", list_charges, methods=["GET"]),
        ]
    )
    config = IdempotencyConfig(cache_server_errors=cache_server_errors, principal_of=tenant_of)
    wrapped = IdempotencyMiddleware(app, store, config)
    transport = httpx.ASGITransport(app=wrapped, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return Harness(client, store, clock, calls, gate, entered)


def headers(key: str | None = KEY, tenant: str = "tenant-a") -> dict[str, str]:
    out = {"authorization": f"Bearer {tenant}", "content-type": "application/json"}
    if key is not None:
        out["idempotency-key"] = key
    return out


BODY = b'{"amount": 2000, "currency": "usd"}'


# --- RFC 9651 parsing of the field (draft-08 Section 2.1) ---------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([b'"8e03978e-40d5-43e8-bc93-6894a57f9324"'], "8e03978e-40d5-43e8-bc93-6894a57f9324"),
        ([b'  "clkyoesmbgybucifusbbtdsbohtyuuwz"  '], "clkyoesmbgybucifusbbtdsbohtyuuwz"),
        ([b'"abc";v=1;x;y="z"'], "abc"),
        ([b'"a\\"b\\\\c"'], 'a"b\\c'),
        ([b'""'], ""),
    ],
    ids=["uuid", "surrounding-sp", "params-ignored", "escapes", "empty-string"],
)
def test_parse_accepts_sf_string_items(lines: list[bytes], expected: str) -> None:
    assert parse_idempotency_key(lines).value == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "lines",
    [
        [b"KG5LxwFBepaKHyUD"],  # Stripe-style unquoted value parses as a Token, not a String
        [b"8e03978e-40d5-43e8-bc93-6894a57f9324"],  # unquoted UUID: Integer 8 then leftovers
        [b'"first-key-aaaaaaaa"', b'"second-key-bbbbbbb"'],  # two lines combine to a List
        [b'"abc'],  # unterminated
        [b'"a\\nb"'],  # only \" and \\ escapes are allowed
        [b'"caf\xc3\xa9"'],  # non-ASCII UTF-8 inside a String
        [b'"abc";V=1'],  # parameter keys are lowercase
        [b'"abc" junk'],  # leftover input
        [b'\t"abc"'],  # leading HTAB is not discarded, only SP
    ],
    ids=[
        "token",
        "unquoted-uuid",
        "duplicate-lines",
        "unterminated",
        "bad-escape",
        "non-ascii",
        "uppercase-param",
        "leftover",
        "leading-htab",
    ],
)
def test_parse_rejects_everything_else(lines: list[bytes]) -> None:
    with pytest.raises(SfParseError):
        parse_idempotency_key(lines)


# --- Draft-08 Section 2.7 error handling --------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_key_is_400_problem_with_describedby_link() -> None:
    h = build()
    r = await h.client.post("/v1/charges", content=BODY, headers=headers(key=None))
    assert r.status_code == 400
    assert r.headers["content-type"] == "application/problem+json"
    assert r.headers["link"] == (
        '<https://developer.example.com/idempotency>; rel="describedby"; type="text/html"'
    )
    assert r.json()["title"] == "Idempotency-Key is missing"
    assert h.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unquoted_key_is_ignored_and_treated_as_missing() -> None:
    h = build()
    r = await h.client.post("/v1/charges", content=BODY, headers=headers(key="KG5LxwFBepaKHyUD"))
    assert r.status_code == 400
    assert r.json()["title"] == "Idempotency-Key is missing"
    assert "not a Structured Field String" in r.json()["detail"]
    assert h.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_low_entropy_key_fails_published_format() -> None:
    h = build()
    r = await h.client.post("/v1/charges", content=BODY, headers=headers(key='"1"'))
    assert r.status_code == 400
    assert r.json()["title"] == "Idempotency-Key is invalid"
    assert h.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_replays_exact_status_headers_and_body_once_executed() -> None:
    h = build()
    first = await h.client.post("/v1/charges", content=BODY, headers=headers())
    second = await h.client.post("/v1/charges", content=BODY, headers=headers())
    assert first.status_code == second.status_code == 201
    assert first.content == second.content
    assert second.headers["x-charge-seq"] == "1"
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert len(h.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_key_different_payload_is_422_and_does_not_execute() -> None:
    h = build()
    await h.client.post("/v1/charges", content=BODY, headers=headers())
    other = b'{"amount": 9999, "currency": "usd"}'
    r = await h.client.post("/v1/charges", content=other, headers=headers())
    assert r.status_code == 422
    assert r.json()["title"] == "Idempotency-Key is already used"
    assert len(h.calls) == 1
    replay = await h.client.post("/v1/charges", content=BODY, headers=headers())
    assert replay.json()["amount"] == 2000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_key_on_another_endpoint_is_422() -> None:
    h = build()
    await h.client.post("/v1/charges", content=BODY, headers=headers())
    r = await h.client.post("/v1/refunds", content=BODY, headers=headers())
    assert r.status_code == 422
    assert len(h.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reordered_json_is_a_mismatch_under_raw_byte_fingerprints() -> None:
    h = build()
    await h.client.post("/v1/charges", content=BODY, headers=headers())
    reordered = b'{"currency": "usd", "amount": 2000}'
    r = await h.client.post("/v1/charges", content=reordered, headers=headers())
    assert r.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_duplicate_gets_409_then_replay_after_completion() -> None:
    h = build(gate_open=False)
    first = asyncio.create_task(h.client.post("/v1/charges", content=BODY, headers=headers()))
    await asyncio.wait_for(h.entered.wait(), timeout=5.0)  # first request is in the handler
    assert h.store.state_of(SCOPE_KEY) is State.IN_FLIGHT
    dup = await h.client.post("/v1/charges", content=BODY, headers=headers())
    assert dup.status_code == 409
    assert dup.json()["title"] == "A request is outstanding for this Idempotency-Key"
    h.gate.set()
    assert (await first).status_code == 201
    replay = await h.client.post("/v1/charges", content=BODY, headers=headers())
    assert replay.headers["idempotent-replayed"] == "true"
    assert len(h.calls) == 1


# --- What gets recorded: 4xx, 5xx, crashes ------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_4xx_is_recorded_and_replayed() -> None:
    h = build()
    declined = b'{"amount": 2000, "card": "declined"}'
    a = await h.client.post("/v1/charges", content=declined, headers=headers())
    b = await h.client.post("/v1/charges", content=declined, headers=headers())
    assert a.status_code == b.status_code == 402
    assert b.headers["idempotent-replayed"] == "true"
    assert len(h.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_5xx_releases_the_key_so_a_retry_re_executes() -> None:
    h = build()
    boom = b'{"amount": 2000, "card": "boom"}'
    a = await h.client.post("/v1/charges", content=boom, headers=headers())
    b = await h.client.post("/v1/charges", content=boom, headers=headers())
    assert a.status_code == b.status_code == 500
    assert "idempotent-replayed" not in b.headers
    assert len(h.calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stripe_v1_policy_caches_5xx() -> None:
    h = build(cache_server_errors=True)
    boom = b'{"amount": 2000, "card": "boom"}'
    await h.client.post("/v1/charges", content=boom, headers=headers())
    b = await h.client.post("/v1/charges", content=boom, headers=headers())
    assert b.status_code == 500
    assert b.headers["idempotent-replayed"] == "true"
    assert len(h.calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crash_releases_even_when_server_errors_are_cached() -> None:
    h = build(cache_server_errors=True)
    crash = b'{"amount": 2000, "card": "raise"}'
    a = await h.client.post("/v1/charges", content=crash, headers=headers())
    assert a.status_code == 500
    assert h.store.state_of(SCOPE_KEY) is None
    await h.client.post("/v1/charges", content=crash, headers=headers())
    assert len(h.calls) == 2


# --- Leases, fencing, TTL, scoping --------------------------------------------------------


@pytest.mark.unit
def test_expired_lease_is_taken_over_and_stale_owner_is_fenced() -> None:
    clock = FakeClock()
    store = IdempotencyStore(ttl_seconds=86_400.0, lease_seconds=90.0, clock=clock)
    crashed = store.begin(SCOPE_KEY, "fp")
    assert crashed.outcome is Outcome.PROCEED
    clock.now += 89.0
    assert store.begin(SCOPE_KEY, "fp").outcome is Outcome.IN_PROGRESS
    clock.now += 1.0
    takeover = store.begin(SCOPE_KEY, "fp")
    assert takeover.outcome is Outcome.PROCEED
    assert takeover.token > crashed.token
    late = StoredResponse(201, (), b"stale")
    assert store.complete(SCOPE_KEY, crashed.token, late) is False
    assert store.release(SCOPE_KEY, crashed.token) is False
    fresh = StoredResponse(201, (), b"fresh")
    assert store.complete(SCOPE_KEY, takeover.token, fresh) is True
    replay = store.begin(SCOPE_KEY, "fp")
    assert replay.outcome is Outcome.REPLAY
    assert replay.response == fresh


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_reuse_after_ttl_executes_a_new_request() -> None:
    h = build()
    await h.client.post("/v1/charges", content=BODY, headers=headers())
    h.clock.now += 86_399.0
    replay = await h.client.post("/v1/charges", content=BODY, headers=headers())
    assert replay.headers["idempotent-replayed"] == "true"
    h.clock.now += 1.0
    fresh = await h.client.post("/v1/charges", content=BODY, headers=headers())
    assert "idempotent-replayed" not in fresh.headers
    assert fresh.json()["id"] == "ch_2"
    assert len(h.calls) == 2


@pytest.mark.unit
def test_reaper_purges_only_expired_records() -> None:
    clock = FakeClock()
    store = IdempotencyStore(ttl_seconds=100.0, lease_seconds=10.0, clock=clock)
    old = store.begin(("t", "old-key"), "fp")
    store.complete(("t", "old-key"), old.token, StoredResponse(201, (), b""))
    clock.now += 50.0
    store.begin(("t", "new-key"), "fp")
    clock.now += 50.0
    assert store.purge_expired() == 1
    assert store.state_of(("t", "old-key")) is None
    assert store.state_of(("t", "new-key")) is State.IN_FLIGHT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_key_in_two_tenants_never_collides_or_leaks() -> None:
    h = build()
    a = await h.client.post("/v1/charges", content=BODY, headers=headers(tenant="tenant-a"))
    b = await h.client.post("/v1/charges", content=BODY, headers=headers(tenant="tenant-b"))
    assert a.json()["id"] == "ch_1"
    assert b.json()["id"] == "ch_2"
    assert "idempotent-replayed" not in b.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_bypasses_enforcement() -> None:
    h = build()
    for _ in range(2):
        r = await h.client.get("/v1/charges", headers=headers(key=None))
        assert r.status_code == 200
    assert h.calls == [b"GET", b"GET"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_body_is_413_before_any_state_is_written() -> None:
    h = build()
    big = b'{"pad": "' + b"x" * 1_048_576 + b'"}'
    r = await h.client.post("/v1/charges", content=big, headers=headers())
    assert r.status_code == 413
    assert h.store.state_of(SCOPE_KEY) is None
    assert h.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mixed_case_header_name_from_a_non_lowercasing_server_is_found() -> None:
    clock = FakeClock()
    store = IdempotencyStore(clock=clock)
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def receive() -> Message:
        return {"type": "http.request", "body": BODY, "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/charges",
        "query_string": b"",
        "headers": [(b"Idempotency-Key", KEY.encode("ascii"))],
    }
    await IdempotencyMiddleware(app, store, IdempotencyConfig())(scope, receive, send)
    assert sent[0]["status"] == 201
    assert store.state_of(("anonymous", SCOPE_KEY[1])) is State.COMPLETED
