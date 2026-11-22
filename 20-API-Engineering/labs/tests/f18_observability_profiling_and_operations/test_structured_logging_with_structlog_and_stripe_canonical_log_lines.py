"""Lab: canonical log lines with structlog 26.1.0 as pure ASGI middleware."""

from __future__ import annotations

import asyncio
import contextvars
import io
import json
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from structlog.contextvars import bind_contextvars, clear_contextvars, get_contextvars

from api_labs.f18_observability_profiling_and_operations.canonical_log_lines import (
    CANONICAL_EVENT,
    CanonicalLogMiddleware,
    RequestAccumulator,
    annotate,
    build_logger,
    current_accumulator,
    db_query,
    parse_lines,
    parse_traceparent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE_ID}-b7ad6b7169203331-01"
HEX32 = re.compile(r"[0-9a-f]{32}")


class Sink(io.StringIO):
    """A text sink that also records every write in an ordered journal."""

    def __init__(self, journal: list[str] | None = None) -> None:
        super().__init__()
        self.journal = journal if journal is not None else []

    def write(self, s: str, /) -> int:
        self.journal.append("log")
        return super().write(s)


class BrokenSink(io.StringIO):
    def write(self, s: str, /) -> int:
        raise OSError("disk full")


@pytest.fixture(autouse=True)
def _clean_structlog_context() -> Iterator[None]:
    clear_contextvars()
    yield
    clear_contextvars()


def make_app(
    sink: io.StringIO, routes: list[Route], **kwargs: Any
) -> tuple[Starlette, list[CanonicalLogMiddleware]]:
    """A Starlette app whose user middleware is the canonical line middleware.

    The list receives the middleware instance when Starlette builds its stack.
    """
    logger = build_logger(sink)
    built: list[CanonicalLogMiddleware] = []

    def factory(app: ASGIApp) -> ASGIApp:
        middleware = CanonicalLogMiddleware(app, logger=logger, **kwargs)
        built.append(middleware)
        return middleware

    return Starlette(routes=routes, middleware=[Middleware(factory)]), built


def client_for(app: ASGIApp, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def http_scope(
    path: str = "/",
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    query: bytes = b"",
    method: str = "GET",
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }


async def call_asgi(
    app: ASGIApp,
    scope: Scope,
    *,
    inbound: list[Message] | None = None,
    send_hook: Callable[[Message], Awaitable[None]] | None = None,
) -> list[Message]:
    """Drive an ASGI app directly; ``inbound`` defaults to one empty request body."""
    queue = list(inbound or [{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[Message] = []

    async def receive() -> Message:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if send_hook is not None:
            await send_hook(message)
        sent.append(message)

    await app(scope, receive, send)
    return sent


def only_line(sink: io.StringIO) -> dict[str, Any]:
    lines = [line for line in parse_lines(sink.getvalue()) if line["event"] == CANONICAL_EVENT]
    assert len(lines) == 1
    return dict(lines[0])


# ------------------------------------------------------------------ routes


def build_routes(logger_sink: io.StringIO) -> list[Route]:
    log = build_logger(logger_sink)

    async def ok(request: Request) -> Response:
        annotate(auth_user_id="usr_123", rate_limit_remaining=99)
        with db_query():
            await asyncio.sleep(0)
        with db_query():
            await asyncio.sleep(0)
        log.info("charge_started", amount_cents=1999)
        return JSONResponse({"ok": True})

    def sync_handler(request: Request) -> Response:
        # Runs in a worker thread inside a copied Context.
        for _ in range(int(request.path_params["n"])):
            with db_query():
                pass
        annotate(auth_user_id="usr_thread")
        return JSONResponse({"ok": True})

    async def echo(request: Request) -> Response:
        i = int(request.path_params["i"])
        annotate(auth_user_id=f"usr_{i}")
        for _ in range(i % 4 + 1):
            with db_query():
                await asyncio.sleep(0)
        return JSONResponse({"i": i})

    async def boom(request: Request) -> Response:
        annotate(auth_user_id="usr_boom")
        raise RuntimeError("kaboom")

    async def stream(request: Request) -> Response:
        async def body() -> AsyncIterator[bytes]:
            for chunk in (b"aaaa", b"bb", b"c"):
                yield chunk

        return StreamingResponse(body())

    async def midstream(request: Request) -> Response:
        async def body() -> AsyncIterator[bytes]:
            yield b"partial"
            raise RuntimeError("generator failed")

        return StreamingResponse(body())

    async def read_body(request: Request) -> Response:
        return JSONResponse({"n": len(await request.body())})

    async def ignore_body(request: Request) -> Response:
        return JSONResponse({"n": 0})

    async def db_fails(request: Request) -> Response:
        try:
            with db_query():
                raise ValueError("query failed")
        except ValueError:
            pass
        return JSONResponse({"ok": True})

    async def hostile(request: Request) -> Response:
        annotate(
            http_status=999,
            db_query_count=-5,
            request_id="evil",
            custom="kept",
            amount=Decimal("1.50"),
        )
        return JSONResponse({"ok": True})

    return [
        Route("/ok", ok),
        Route("/sync/{n:int}", sync_handler),
        Route("/echo/{i:int}", echo),
        Route("/boom", boom),
        Route("/stream", stream),
        Route("/midstream", midstream),
        Route("/body", read_body, methods=["POST"]),
        Route("/nobody", ignore_body, methods=["POST"]),
        Route("/db-fails", db_fails),
        Route("/hostile", hostile),
    ]


# ------------------------------------------------------------------- tests


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_canonical_line_per_request_with_accumulated_fields() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app) as client:
        response = await client.get(
            "/ok", headers={"x-request-id": "req-abc", "traceparent": TRACEPARENT}
        )
    assert response.status_code == 200

    lines = parse_lines(sink.getvalue())
    assert [line["event"] for line in lines] == ["charge_started", CANONICAL_EVENT]
    intermediate, canonical = lines

    assert canonical["request_id"] == "req-abc"
    assert canonical["trace_id"] == TRACE_ID
    assert canonical["trace_sampled"] is True
    assert canonical["http_method"] == "GET"
    assert canonical["http_path"] == "/ok"
    assert canonical["http_route"] == "/ok"
    assert canonical["http_status"] == 200
    assert canonical["outcome"] == "completed"
    assert canonical["auth_user_id"] == "usr_123"
    assert canonical["rate_limit_remaining"] == 99
    assert canonical["db_query_count"] == 2
    assert canonical["db_duration_ms"] >= 0.0
    assert canonical["level"] == "info"
    # The intermediate line is correlated through structlog.contextvars.
    assert intermediate["request_id"] == "req-abc"
    assert intermediate["trace_id"] == TRACE_ID
    canonical_raw = sink.getvalue().splitlines()[-1]
    print(canonical_raw)
    print(f"canonical line bytes: {len(canonical_raw.encode())}")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_threadpool_handler_mutations_reach_the_line() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app) as client:
        assert (await client.get("/sync/3")).status_code == 200
    line = only_line(sink)
    assert line["db_query_count"] == 3
    assert line["auth_user_id"] == "usr_thread"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_structlog_bind_contextvars_in_a_thread_is_invisible_to_the_caller() -> None:
    def worker() -> None:
        bind_contextvars(bound_in_thread=1)
        assert get_contextvars() == {"bound_in_thread": 1}

    await run_in_threadpool(worker)
    assert get_contextvars() == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_fields() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app) as client:
        responses = await asyncio.gather(
            *(client.get(f"/echo/{i}", headers={"x-request-id": f"req-{i}"}) for i in range(25))
        )
    assert all(r.status_code == 200 for r in responses)
    lines = {line["request_id"]: line for line in parse_lines(sink.getvalue())}
    assert len(lines) == 25
    for i in range(25):
        assert lines[f"req-{i}"]["auth_user_id"] == f"usr_{i}"
        assert lines[f"req-{i}"]["db_query_count"] == i % 4 + 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_context_is_cleaned_up_after_success_and_failure() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app, raise_app_exceptions=False) as client:
        await client.get("/ok")
        assert current_accumulator() is None
        assert get_contextvars() == {}
        await client.get("/boom")
        assert current_accumulator() is None
        assert get_contextvars() == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_exception_emits_one_inferred_500_line_and_still_raises() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app, raise_app_exceptions=False) as client:
        response = await client.get("/boom")
    assert response.status_code == 500
    line = only_line(sink)
    assert line["http_status"] == 500
    assert line["status_inferred"] is True
    assert line["outcome"] == "exception"
    assert line["error_class"] == "RuntimeError"
    assert line["error_message"] == "kaboom"
    assert line["auth_user_id"] == "usr_boom"
    assert line["level"] == "error"

    sink2 = Sink()
    app2, _ = make_app(sink2, build_routes(sink2))
    async with client_for(app2) as client2:
        with pytest.raises(RuntimeError, match="kaboom"):
            await client2.get("/boom")
    assert only_line(sink2)["outcome"] == "exception"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_exception_after_response_start_keeps_the_sent_status() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    messages: list[Message] = []

    async def hook(message: Message) -> None:
        messages.append(message)

    with pytest.raises(RuntimeError, match="generator failed"):
        await call_asgi(app, http_scope("/midstream"), send_hook=hook)
    line = only_line(sink)
    assert messages[0]["status"] == 200
    assert line["http_status"] == 200
    assert line["status_inferred"] is False
    assert line["outcome"] == "exception"
    assert line["error_class"] == "RuntimeError"
    assert line["response_bytes"] == len(b"partial")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_streaming_line_is_emitted_after_the_last_chunk_with_exact_timings() -> None:
    journal: list[str] = []
    sink = Sink(journal)
    ticks = iter([0.0, 0.010, 0.050])
    app, _ = make_app(sink, build_routes(sink), clock=lambda: next(ticks))

    async def hook(message: Message) -> None:
        if message["type"] == "http.response.body":
            journal.append(f"send:more_body={message.get('more_body', False)}")

    await call_asgi(app, http_scope("/stream"), send_hook=hook)
    assert journal[-2:] == ["send:more_body=False", "log"]
    assert journal.count("log") == 1
    line = only_line(sink)
    assert line["ttfb_ms"] == 10.0
    assert line["duration_ms"] == 50.0
    assert line["response_bytes"] == len(b"aaaabbc")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_bytes_count_only_what_the_handler_consumed() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app) as client:
        await client.post("/body", content=b"abcde")
        await client.post("/nobody", content=b"abcde")
    first, second = parse_lines(sink.getvalue())
    assert first["request_bytes"] == 5
    assert second["request_bytes"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_oserror_is_a_client_disconnect_not_a_server_error() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    sent_types: list[str] = []

    async def hook(message: Message) -> None:
        sent_types.append(message["type"])
        if message["type"] == "http.response.body":
            raise BrokenPipeError("peer went away")

    with pytest.raises(BrokenPipeError):
        await call_asgi(app, http_scope("/stream"), send_hook=hook)
    line = only_line(sink)
    assert line["outcome"] == "client_disconnected"
    assert line["client_disconnected"] is True
    assert line["http_status"] == 200
    assert line["level"] == "info"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_disconnect_only_counts_before_the_response_is_complete() -> None:
    async def finished_then_polls(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x"})
        assert (await receive())["type"] == "http.request"
        assert (await receive())["type"] == "http.disconnect"

    async def disconnected_early(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        assert (await receive())["type"] == "http.disconnect"

    logger_sink = Sink()
    middleware = CanonicalLogMiddleware(finished_then_polls, logger=build_logger(logger_sink))
    await call_asgi(middleware, http_scope())
    assert only_line(logger_sink)["client_disconnected"] is False

    early_sink = Sink()
    early = CanonicalLogMiddleware(disconnected_early, logger=build_logger(early_sink))
    await call_asgi(early, http_scope())
    line = only_line(early_sink)
    assert line["client_disconnected"] is True
    assert line["outcome"] == "no_response"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_emits_one_line_and_reraises() -> None:
    started = asyncio.Event()

    async def hangs(scope: Scope, receive: Receive, send: Send) -> None:
        started.set()
        await asyncio.Event().wait()

    sink = Sink()
    middleware = CanonicalLogMiddleware(hangs, logger=build_logger(sink))
    task = asyncio.create_task(call_asgi(middleware, http_scope()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    line = only_line(sink)
    assert line["outcome"] == "cancelled"
    assert "error_class" not in line
    assert current_accumulator() is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_that_returns_without_a_response_is_an_inferred_500() -> None:
    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    sink = Sink()
    await call_asgi(CanonicalLogMiddleware(silent, logger=build_logger(sink)), http_scope())
    line = only_line(sink)
    assert line["http_status"] == 500
    assert line["status_inferred"] is True
    assert line["outcome"] == "no_response"
    assert line["level"] == "error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_sink_never_fails_the_request(caplog: pytest.LogCaptureFixture) -> None:
    app, built = make_app(BrokenSink(), build_routes(Sink()))
    with caplog.at_level(logging.ERROR, logger="api_labs.canonical_log_lines"):
        async with client_for(app) as client:
            response = await client.get("/ok")
    assert response.status_code == 200
    assert built[0].emit_failures == 1
    assert [r.getMessage() for r in caplog.records] == ["canonical line emission failed"]
    assert current_accumulator() is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TRACEPARENT, (TRACE_ID, True)),
        (f"00-{TRACE_ID}-b7ad6b7169203331-00", (TRACE_ID, False)),
        (f"01-{TRACE_ID}-b7ad6b7169203331-01-extra-fields", (TRACE_ID, True)),
        (f"{'00'}-{'0' * 32}-b7ad6b7169203331-01", None),
        (f"00-{TRACE_ID}-{'0' * 16}-01", None),
        (f"00-{TRACE_ID.upper()}-b7ad6b7169203331-01", None),
        (f"ff-{TRACE_ID}-b7ad6b7169203331-01", None),
        (f"00-{TRACE_ID}-b7ad6b7169203331-01-extra", None),
        (f"01-{TRACE_ID}-b7ad6b7169203331-01extra", None),
        (f"00-{TRACE_ID[:-1]}-b7ad6b7169203331-01", None),
        ("", None),
    ],
)
def test_parse_traceparent_follows_the_w3c_rules(
    value: str, expected: tuple[str, bool] | None
) -> None:
    assert parse_traceparent(value) == expected


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [(b"traceparent", f"00-{'0' * 32}-b7ad6b7169203331-01".encode())],
        [(b"traceparent", TRACEPARENT.encode()), (b"traceparent", TRACEPARENT.encode())],
        [],
    ],
)
async def test_unusable_traceparent_restarts_the_trace(headers: list[tuple[bytes, bytes]]) -> None:
    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body"})

    sink = Sink()
    middleware = CanonicalLogMiddleware(ok, logger=build_logger(sink))
    await call_asgi(middleware, http_scope(headers=headers))
    line = only_line(sink)
    assert HEX32.fullmatch(line["trace_id"])
    assert line["trace_id"] not in {TRACE_ID, "0" * 32}
    assert "trace_sampled" not in line


@pytest.mark.unit
@pytest.mark.asyncio
async def test_untrusted_request_id_is_replaced() -> None:
    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body"})

    for bad in (b"has space", b"new\nline", b"x" * 65, b""):
        sink = Sink()
        middleware = CanonicalLogMiddleware(
            ok, logger=build_logger(sink), id_factory=lambda: "minted-1"
        )
        await call_asgi(middleware, http_scope(headers=[(b"x-request-id", bad)]))
        assert only_line(sink)["request_id"] == "minted-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handlers_cannot_overwrite_reserved_keys_and_odd_values_are_visible() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app) as client:
        await client.get("/hostile", headers={"x-request-id": "real-id"})
    line = only_line(sink)
    assert line["http_status"] == 200
    assert line["db_query_count"] == 0
    assert line["request_id"] == "real-id"
    assert line["custom"] == "kept"
    assert line["fields_dropped"] == 3
    # JSONRenderer's fallback is repr(), so a Decimal silently becomes a string.
    assert line["amount"] == "Decimal('1.50')"


@pytest.mark.unit
def test_accumulator_caps_fields_and_truncates_values() -> None:
    accumulator = RequestAccumulator(max_fields=64, max_value_chars=512)
    accumulator.set(**{f"k{i}": i for i in range(100)})
    accumulator.set(big="x" * 10_000)
    snapshot = accumulator.snapshot()
    # 2 built-in keys + 62 accepted keys fill the cap; 38 + 1 later keys are dropped.
    assert snapshot["fields_dropped"] == 38 + 1
    assert "k61" in snapshot
    assert "k62" not in snapshot
    assert "big" not in snapshot
    accumulator.set(k0="y" * 10_000)
    truncated = accumulator.snapshot()["k0"]
    assert len(truncated) == 512
    assert truncated.endswith("...")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_log_injection_and_secrets_stay_out_of_the_line() -> None:
    async def ok(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body"})

    sink = Sink()
    middleware = CanonicalLogMiddleware(ok, logger=build_logger(sink))
    path = '/a\n{"event": "forged"}"'
    scope = http_scope(
        path,
        headers=[
            (b"authorization", b"Bearer EXAMPLE-NOT-A-REAL-TOKEN"),
            (b"user-agent", b"u" * 1000),
        ],
        query=b"api_key=EXAMPLE-NOT-A-REAL-KEY",
    )
    await call_asgi(middleware, scope)
    raw = sink.getvalue()
    assert raw.count("\n") == 1
    assert "EXAMPLE-NOT-A-REAL" not in raw
    line = only_line(sink)
    assert line["http_path"] == path
    assert len(line["user_agent"]) == 256


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched() -> None:
    seen: list[str] = []

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    sink = Sink()
    middleware = CanonicalLogMiddleware(lifespan_app, logger=build_logger(sink))
    await middleware({"type": "lifespan"}, call_asgi, call_asgi)  # type: ignore[arg-type]
    assert seen == ["lifespan"]
    assert sink.getvalue() == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_db_query_counts_a_query_that_raised() -> None:
    sink = Sink()
    app, _ = make_app(sink, build_routes(sink))
    async with client_for(app) as client:
        await client.get("/db-fails")
    assert only_line(sink)["db_query_count"] == 1


@pytest.mark.unit
def test_annotate_and_db_query_are_noops_outside_a_request() -> None:
    annotate(auth_user_id="ignored")
    with db_query():
        pass
    assert current_accumulator() is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_basehttpmiddleware_splits_context_but_the_accumulator_survives() -> None:
    probe: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)

    async def passthrough(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        return await call_next(request)

    async def endpoint(request: Request) -> Response:
        probe.set("set-in-endpoint")
        with db_query():
            pass
        return JSONResponse({"ok": True})

    def observe(app: ASGIApp, seen: list[str | None]) -> ASGIApp:
        async def outer(scope: Scope, receive: Receive, send: Send) -> None:
            await app(scope, receive, send)
            seen.append(probe.get())

        return outer

    seen_direct: list[str | None] = []
    seen_split: list[str | None] = []
    direct = Starlette(routes=[Route("/", endpoint)])
    split = Starlette(
        routes=[Route("/", endpoint)],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=passthrough)],
    )
    sink = Sink()

    async def run_once(app: ASGIApp, seen: list[str | None]) -> None:
        wrapped = CanonicalLogMiddleware(observe(app, seen), logger=build_logger(sink))
        async with client_for(wrapped) as client:
            await client.get("/")

    # Each run gets its own task, hence its own copy of the Context: the direct
    # endpoint runs in the caller's task and would otherwise leak `probe` into the next run.
    await asyncio.create_task(run_once(direct, seen_direct))
    await asyncio.create_task(run_once(split, seen_split))
    assert seen_direct == ["set-in-endpoint"]
    assert seen_split == [None]
    assert [line["db_query_count"] for line in parse_lines(sink.getvalue())] == [1, 1]


@pytest.mark.unit
def test_canonical_line_is_valid_json_with_a_stable_always_present_schema() -> None:
    sink = Sink()

    async def run() -> None:
        async def ok(scope: Scope, receive: Receive, send: Send) -> None:
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body"})

        await call_asgi(CanonicalLogMiddleware(ok, logger=build_logger(sink)), http_scope())

    asyncio.run(run())
    line = json.loads(sink.getvalue())
    for key in (
        "auth_user_id",
        "rate_limit_remaining",
        "db_query_count",
        "db_duration_ms",
        "trace_id",
        "request_id",
        "http_status",
        "duration_ms",
        "ttfb_ms",
        "timestamp",
    ):
        assert key in line
    assert line["auth_user_id"] is None
    assert line["db_query_count"] == 0
    assert line["timestamp"].endswith("Z")
