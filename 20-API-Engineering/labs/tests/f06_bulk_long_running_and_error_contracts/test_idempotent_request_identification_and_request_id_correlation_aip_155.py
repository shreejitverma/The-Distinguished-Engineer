"""Lab: client request_id (AIP-155) vs server X-Request-ID vs W3C traceparent correlation."""

import asyncio
import contextvars
import secrets
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import UUID4, TypeAdapter, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api_labs.f06_bulk_long_running_and_error_contracts.request_identification import (
    ANY_RFC9562_VERSION,
    PROBLEM_JSON,
    Correlation,
    CorrelationMiddleware,
    Lab,
    Log,
    RandomIds,
    RequestIdError,
    _current,  # pyright: ignore[reportPrivateUsage]
    build_lab,
    current_correlation,
    parse_request_id,
    parse_traceparent,
)

V4 = "919108f7-52d1-4320-9bac-f847db4148a8"  # RFC 9562 Appendix A.3
V7 = "017F22E2-79B0-7CC3-98C4-DC0C0C07398F"  # RFC 9562 Appendix A.6, uppercase as printed
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"  # W3C Trace Context 3.2.3 example
PARENT = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE}-{PARENT}-01"


class SequentialIds:
    """Deterministic IdSource: attempt N gets UUIDv4 with integer N in its random bits."""

    def __init__(self) -> None:
        self.n = 0

    def _next(self) -> int:
        self.n += 1
        return self.n

    def request_id(self) -> str:
        return str(uuid.UUID(int=self._next(), version=4))

    def trace_id(self) -> str:
        return f"{self._next():032x}"

    def span_id(self) -> str:
        return f"{self._next():016x}"


def client(lab: Lab) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=lab.app), base_url="http://test")


def raw_scope(headers: list[tuple[bytes, bytes]]) -> Scope:
    """A bare ASGI HTTP scope, so header names reach the app exactly as written here."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/whoami",
        "raw_path": b"/v1/whoami",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }


async def drive(app: ASGIApp, scope: Scope, sent: list[Message]) -> None:
    """Run one bodyless request through an ASGI app, collecting every message it sends."""

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)


async def run_wrapped(
    inner: ASGIApp, sent: list[Message], log: Log, headers: tuple[tuple[bytes, bytes], ...] = ()
) -> None:
    """Drive one GET through CorrelationMiddleware wrapped around a bare ASGI app."""
    app = CorrelationMiddleware(inner, ids=SequentialIds(), log=log, trust_inbound=False)
    await drive(app, raw_scope(list(headers)), sent)


# --- request_id grammar ---------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [V4, V4.upper(), "F47AC10B-58CC-4372-8567-0E02B2C3D479"],
    ids=["rfc9562-a3", "uppercase", "mixed-source"],
)
def test_accepts_36_char_v4_in_any_case_and_normalizes_to_lowercase(raw: str) -> None:
    value = parse_request_id(raw)
    assert value.version == 4
    assert str(value) == raw.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("00000000-0000-0000-0000-000000000000", "nil UUID"),
        ("ffffffff-ffff-ffff-ffff-ffffffffffff", "version"),
        ("F47AC10B-58CC-0372-8567-0E02B2C3D479", "version"),  # field_info.proto example: v0
        ("f47ac10b-58cc-4372-c567-0e02b2c3d479", "version"),  # Microsoft variant, not RFC
        (V7, "version"),  # valid UUID, but AIP-155 annotates UUID4
        (f"{{{V4}}}", "36-character"),
        (f"urn:uuid:{V4}", "36-character"),
        (V4.replace("-", ""), "36-character"),
        ("9-1-9-1-08f752d143209bacf847db4148a8", "36-character"),
        (V4 + "\n", "36-character"),
        (12345, "36-character"),
    ],
    ids=[
        "nil",
        "max",
        "version-0",
        "microsoft-variant",
        "v7-under-v4-policy",
        "braces",
        "urn",
        "no-hyphens",
        "hyphens-anywhere",
        "trailing-newline",
        "not-a-string",
    ],
)
def test_rejects_everything_but_a_36_char_rfc9562_v4(raw: object, reason: str) -> None:
    with pytest.raises(RequestIdError, match=reason):
        parse_request_id(raw)


@pytest.mark.unit
def test_stdlib_and_pydantic_parsers_are_lax_so_a_grammar_check_must_come_first() -> None:
    lax_forms = [
        f"{{{V4}}}",
        f"urn:uuid:{V4}",
        V4.replace("-", ""),
        "9-1-9-1-08f752d143209bacf847db4148a8",
    ]
    for lax in lax_forms:
        assert str(uuid.UUID(lax)) == V4
    for lax in lax_forms[:3]:  # the Rust parser rejects only the misplaced hyphens
        assert str(TypeAdapter(UUID4).validate_python(lax)) == V4
    with pytest.raises(ValidationError, match="invalid group length"):
        TypeAdapter(UUID4).validate_python(lax_forms[3])
    assert uuid.UUID("00000000-0000-0000-0000-000000000000").version is None


@pytest.mark.unit
def test_v7_is_accepted_only_when_the_policy_names_version_7() -> None:
    assert parse_request_id(V7, frozenset({4, 7})).version == 7
    assert str(parse_request_id(V7, ANY_RFC9562_VERSION)) == V7.lower()


# --- traceparent ----------------------------------------------------------------------------

TRACEPARENT_CASES = [
    TRACEPARENT,
    f"00-{TRACE}-{PARENT}-00",
    f"00-{TRACE}-{PARENT}-02",  # an unknown flag bit: sampled only if bit 0 is masked out
    f"  {TRACEPARENT}\t",
    f"00-{TRACE.upper()}-{PARENT}-01",
    f"00-{'0' * 32}-{PARENT}-01",
    f"00-{TRACE}-{'0' * 16}-01",
    f"ff-{TRACE}-{PARENT}-01",
    f"00-{TRACE}-{PARENT}-01-extra",
    f"01-{TRACE}-{PARENT}-01-future",
    f"01-{TRACE}-{PARENT}-01",  # a later version at exactly 55 characters
    f"00-{TRACE}-{PARENT}-01x",  # trailing text that is not a "-" separated field
    f"00-{TRACE}-{PARENT}",
    "garbage",
]


@pytest.mark.unit
@pytest.mark.parametrize("value", TRACEPARENT_CASES)
def test_traceparent_parser_agrees_with_opentelemetry_propagator(value: str) -> None:
    ours = parse_traceparent(value)
    context = TraceContextTextMapPropagator().extract({"traceparent": value})
    theirs = trace.get_current_span(context).get_span_context()
    assert (ours is not None) == theirs.is_valid
    if ours is not None:
        assert ours.trace_id == f"{theirs.trace_id:032x}"
        assert ours.parent_id == f"{theirs.span_id:016x}"
        assert ours.sampled == theirs.trace_flags.sampled


# --- HTTP: three identifiers ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_reuses_request_id_but_every_attempt_gets_its_own_x_request_id() -> None:
    lab = build_lab(ids=SequentialIds())
    async with client(lab) as c:
        body = {"title": "Dune", "request_id": V4}
        first = await c.post("/v1/books", json=body)
        retry = await c.post("/v1/books", json=body)
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json() == {"name": "books/1", "title": "Dune"}
    assert len(lab.store.books) == 1
    attempt_ids = {first.headers["x-request-id"], retry.headers["x-request-id"]}
    assert len(attempt_ids) == 2
    assert V4 not in attempt_ids
    business = [r for r in lab.log if r["event"] in {"created", "replayed"}]
    assert [r["event"] for r in business] == ["created", "replayed"]
    assert {r["client_request_id"] for r in business} == {V4}
    assert {r["server_request_id"] for r in business} == attempt_ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uppercase_spelling_of_the_same_uuid_is_the_same_request() -> None:
    lab = build_lab()
    async with client(lab) as c:
        await c.post("/v1/books", json={"title": "Dune", "request_id": V4})
        again = await c.post("/v1/books", json={"title": "Dune", "request_id": V4.upper()})
    assert again.json()["name"] == "books/1"
    assert len(lab.store.books) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_request_id_with_a_different_payload_is_422_and_creates_nothing() -> None:
    lab = build_lab()
    async with client(lab) as c:
        await c.post("/v1/books", json={"title": "Dune", "request_id": V4})
        reused = await c.post("/v1/books", json={"title": "Emma", "request_id": V4})
    assert reused.status_code == 422
    assert reused.headers["content-type"] == PROBLEM_JSON
    assert len(lab.store.books) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_id_is_scoped_per_principal_so_tenants_never_see_each_others_replay() -> None:
    lab = build_lab()
    async with client(lab) as c:
        body = {"title": "Dune", "request_id": V4}
        alice = await c.post("/v1/books", json=body, headers={"x-principal": "alice"})
        bob = await c.post("/v1/books", json=body, headers={"x-principal": "bob"})
    assert alice.json()["name"] == "books/1"
    assert bob.json()["name"] == "books/2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_id_is_scoped_per_method_so_another_method_is_not_a_replay() -> None:
    lab = build_lab()
    async with client(lab) as c:
        body = {"title": "Dune", "request_id": V4}
        book = await c.post("/v1/books", json=body)
        shelf = await c.post("/v1/shelves", json=body)
        shelf_retry = await c.post("/v1/shelves", json=body)
    assert book.json()["name"] == "books/1"
    assert shelf.json() == shelf_retry.json() == {"name": "shelves/1", "title": "Dune"}
    assert (len(lab.store.books), len(lab.store.shelves)) == (1, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_without_request_id_a_retry_creates_a_duplicate() -> None:
    lab = build_lab()
    async with client(lab) as c:
        await c.post("/v1/books", json={"title": "Dune"})
        await c.post("/v1/books", json={"title": "Dune"})
    assert [b["name"] for b in lab.store.books] == ["books/1", "books/2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nil_request_id_is_a_400_problem_whose_instance_is_the_attempt_id() -> None:
    lab = build_lab(ids=SequentialIds())
    nil = "00000000-0000-0000-0000-000000000000"
    async with client(lab) as c:
        r = await c.post("/v1/books", json={"title": "Dune", "request_id": nil})
    assert r.status_code == 400
    assert r.headers["content-type"] == PROBLEM_JSON
    problem = r.json()
    assert problem["instance"] == f"urn:uuid:{r.headers['x-request-id']}"
    assert problem["trace_id"] == lab.log[-1]["trace_id"]
    assert problem["errors"] == [
        {"field": "request_id", "message": "Value error, request_id must not be the nil UUID"}
    ]
    assert nil not in r.text
    assert lab.store.books == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "principal", "field"),
    [
        ({"title": "Dune", "requestId": V4}, "alice", "requestId"),
        ({"title": ""}, "alice", "title"),
        ({"title": "x" * 201}, "alice", "title"),
        ({"title": "Dune"}, "Alice Smith", "x-principal"),
        ({"title": "Dune"}, "a" * 33, "x-principal"),
    ],
    ids=[
        "unknown-field",
        "empty-title",
        "title-201-chars",
        "principal-charset",
        "principal-33-chars",
    ],
)
async def test_malformed_create_is_a_400_problem_naming_the_field_and_creates_nothing(
    body: dict[str, str], principal: str, field: str
) -> None:
    lab = build_lab()
    async with client(lab) as c:
        r = await c.post("/v1/books", json=body, headers={"x-principal": principal})
        ok = await c.post("/v1/books", json={"title": "x" * 200}, headers={"x-principal": "a" * 32})
    assert r.status_code == 400
    assert r.headers["content-type"] == PROBLEM_JSON
    assert [e["field"] for e in r.json()["errors"]] == [field]
    assert ok.status_code == 201  # the bounds themselves are accepted
    assert len(lab.store.books) == 1


# --- inbound X-Request-ID and traceparent ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_untrusted_inbound_x_request_id_is_replaced_and_flagged() -> None:
    lab = build_lab(ids=SequentialIds())
    async with client(lab) as c:
        r = await c.get("/v1/whoami", headers={"x-request-id": V4})
        ignored = lab.log[-1]["inbound_request_id_ignored"]
        await c.get("/v1/whoami")
    assert r.headers["x-request-id"] != V4
    assert r.json()["server_request_id"] == r.headers["x-request-id"]
    assert ignored is True
    assert lab.log[-1]["inbound_request_id_ignored"] is False  # nothing was sent, nothing ignored


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "adopted"),
    [
        ([("x-request-id", V7)], V7.lower()),
        ([("x-request-id", "../../etc/passwd")], None),
        ([("x-request-id", "a" * 4096)], None),
        ([("x-request-id", V4), ("x-request-id", V7)], None),
    ],
    ids=["valid-uuid-normalized", "junk", "oversized", "duplicate-field-lines"],
)
async def test_trusted_edge_x_request_id_is_adopted_only_if_single_and_well_formed(
    headers: list[tuple[str, str]], adopted: str | None
) -> None:
    lab = build_lab(ids=SequentialIds(), trust_inbound=True)
    async with client(lab) as c:
        r = await c.get("/v1/whoami", headers=headers)
    if adopted is None:
        assert r.headers["x-request-id"] not in {v for _, v in headers}
        assert lab.log[-1]["inbound_request_id_ignored"] is True
        assert not any(v in repr(lab.log) for _, v in headers)  # the raw value is never logged
    else:
        assert r.headers["x-request-id"] == adopted
        assert lab.log[-1]["inbound_request_id_ignored"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_traceparent_is_continued_with_a_new_span_and_invalid_one_restarts() -> None:
    lab = build_lab(ids=SequentialIds())
    async with client(lab) as c:
        joined = (await c.get("/v1/whoami", headers={"traceparent": TRACEPARENT})).json()
        bad = f"00-{TRACE.upper()}-{PARENT}-01"
        restarted = (await c.get("/v1/whoami", headers={"traceparent": bad})).json()
    assert joined["trace_id"] == TRACE
    assert joined["parent_id"] == PARENT
    assert restarted["trace_id"] != TRACE
    assert restarted["parent_id"] is None
    spans = [r["span_id"] for r in lab.log if r["event"] == "access"]
    assert PARENT not in spans
    assert len(set(spans)) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_traceparent_lines_restart_the_trace() -> None:
    lab = build_lab(ids=SequentialIds())
    other = f"00-{'a' * 32}-{PARENT}-01"
    async with client(lab) as c:
        lines = [("traceparent", TRACEPARENT), ("traceparent", other)]
        restarted = (await c.get("/v1/whoami", headers=lines)).json()
    assert restarted["trace_id"] not in {TRACE, "a" * 32}
    assert restarted["parent_id"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_header_names_are_matched_case_insensitively_on_raw_asgi_scopes() -> None:
    # httpx.ASGITransport lowercases names; ASGI servers are not required to, so drive raw ASGI.
    lab = build_lab(ids=SequentialIds(), trust_inbound=True)
    scope = raw_scope([(b"X-Request-ID", V4.encode()), (b"Traceparent", TRACEPARENT.encode())])
    sent: list[Message] = []
    await drive(lab.app, scope, sent)
    start = sent[0]
    assert start["status"] == 200
    assert (b"x-request-id", V4.encode()) in start["headers"]
    access = lab.log[-1]
    assert access["trace_id"] == TRACE
    assert access["parent_id"] == PARENT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_ascii_header_bytes_are_ignored_instead_of_crashing_the_middleware() -> None:
    # RFC 9110 Section 5.5 lets a field value carry obs-text (bytes 0x80 to 0xFF).
    lab = build_lab(ids=SequentialIds(), trust_inbound=True)
    scope = raw_scope([(b"x-request-id", b"\xff\xfe"), (b"traceparent", b"00-\xe9")])
    sent: list[Message] = []
    await drive(lab.app, scope, sent)
    assert sent[0]["status"] == 200
    access = lab.log[-1]
    assert (b"x-request-id", str(access["server_request_id"]).encode()) in sent[0]["headers"]
    assert access["inbound_request_id_ignored"] is True
    assert access["parent_id"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sampled_bit_and_parent_of_a_continued_trace_reach_the_handler_context() -> None:
    seen: list[Correlation] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(current_correlation())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    unsampled = f"00-{TRACE}-{PARENT}-00"
    await run_wrapped(inner, [], [], ((b"traceparent", TRACEPARENT.encode()),))
    await run_wrapped(inner, [], [], ((b"traceparent", unsampled.encode()),))
    await run_wrapped(inner, [], [])
    assert [c.sampled for c in seen] == [True, False, False]
    assert [c.parent_id for c in seen] == [PARENT, PARENT, None]
    assert [c.trace_id == TRACE for c in seen] == [True, True, False]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_with_the_original_send_and_no_log() -> None:
    received: list[Send] = []
    log: Log = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        received.append(send)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        raise AssertionError("nothing is sent for this scope")

    app = CorrelationMiddleware(inner, ids=SequentialIds(), log=log, trust_inbound=False)
    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    assert received == [send]
    assert log == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_x_request_id_set_by_the_app_is_replaced_not_duplicated() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        own = [(b"x-request-id", b"app-chosen")]
        await send({"type": "http.response.start", "status": 204, "headers": own})
        await send({"type": "http.response.body", "body": b""})

    sent: list[Message] = []
    log: Log = []
    await run_wrapped(inner, sent, log)
    lines = [v for k, v in sent[0]["headers"] if k == b"x-request-id"]
    assert lines == [str(log[-1]["server_request_id"]).encode()]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_production_id_source_mints_uuid4_attempt_ids_and_valid_w3c_ids() -> None:
    lab = build_lab()
    async with client(lab) as c:
        r = await c.get("/v1/whoami")
    attempt = r.headers["x-request-id"]
    assert str(parse_request_id(attempt)) == attempt  # 36 characters, version 4, lowercase
    minted = f"00-{r.json()['trace_id']}-{lab.log[-1]['span_id']}-00"
    assert parse_traceparent(minted) is not None


@pytest.mark.unit
def test_random_ids_redraw_an_all_zero_span_id(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter(["0" * 16, PARENT])

    def token_hex(nbytes: int) -> str:
        assert nbytes == 8
        return next(draws)

    monkeypatch.setattr(secrets, "token_hex", token_hex)
    assert RandomIds().span_id() == PARENT


# --- failure paths and context isolation ----------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_exception_still_returns_x_request_id_and_a_safe_problem() -> None:
    lab = build_lab(ids=SequentialIds())
    transport = httpx.ASGITransport(app=lab.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/v1/boom")
    assert r.status_code == 500
    assert r.headers["content-type"] == PROBLEM_JSON
    attempt = r.headers["x-request-id"]
    assert r.json()["instance"] == f"urn:uuid:{attempt}"
    assert "hunter2" not in r.text
    crash = next(rec for rec in lab.log if rec["event"] == "unhandled_exception")
    assert crash["server_request_id"] == attempt
    assert r.json()["trace_id"] == crash["trace_id"]
    assert crash["exception"] == "RuntimeError"  # the class name only, never the message
    assert "hunter2" not in repr(lab.log)
    assert lab.log[-1]["status"] == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crash_after_response_start_is_reraised_without_a_second_response() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream broke")

    sent: list[Message] = []
    log: Log = []
    with pytest.raises(RuntimeError, match="stream broke"):
        await run_wrapped(inner, sent, log)
    assert [m["type"] for m in sent] == ["http.response.start"]
    assert [r["event"] for r in log] == ["unhandled_exception", "access"]
    assert log[-1]["status"] == 200


class StampOnly:
    """The naive design: set a context variable and stamp the header, but do not catch."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def stamped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message["headers"], (b"x-request-id", b"1")]}
            await send(message)

        token = NAIVE.set("1")
        try:
            await self.app(scope, receive, stamped)
        finally:
            NAIVE.reset(token)


NAIVE: contextvars.ContextVar[str | None] = contextvars.ContextVar("naive", default=None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_exception_handler_runs_outside_user_middleware_so_it_loses_the_id() -> None:
    seen: list[str | None] = []

    async def on_error(request: Request, exc: Exception) -> JSONResponse:
        seen.append(NAIVE.get())
        return JSONResponse({"detail": "error"}, status_code=500)

    app = FastAPI()
    app.add_middleware(StampOnly)
    app.add_exception_handler(Exception, on_error)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/boom")
        ok = await c.get("/missing")
    assert r.status_code == 500
    assert "x-request-id" not in r.headers
    assert seen == [None]
    assert ok.headers["x-request-id"] == "1"  # the stamp works on every non-crash path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_requests_each_see_their_own_correlation_and_it_is_reset_after() -> None:
    lab = build_lab(ids=SequentialIds())
    async with client(lab) as c:
        responses = await asyncio.gather(*(c.get("/v1/whoami") for _ in range(20)))
        await c.get("/v1/whoami")  # ASGITransport runs the app in this task
    assert _current.get() is None
    with pytest.raises(RuntimeError, match="no correlation context"):
        current_correlation()
    for r in responses:
        assert r.json()["server_request_id"] == r.headers["x-request-id"]
    assert len({r.headers["x-request-id"] for r in responses}) == 20
