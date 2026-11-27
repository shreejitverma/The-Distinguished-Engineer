"""Lab tests for async gRPC interceptors: JWT auth, tracing, metrics, and service-config retries."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import grpc  # type: ignore[import-untyped]
import grpc.aio  # type: ignore[import-untyped]
import jwt
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from api_labs.f19_grpc_and_protocol_buffers.grpc_interceptors import (
    AuthError,
    BearerUnaryUnaryInterceptor,
    ClientTracingInterceptor,
    JwtPolicy,
    JwtServerInterceptor,
    RetryPolicy,
    RpcMetrics,
    ServerTelemetryInterceptor,
    backoff_delays,
    bearer_interceptors,
    build_retry_service_config,
    current_principal,
    effective_max_attempts,
    extract_bearer,
    retry_channel_options,
    verify_bearer,
    with_metadata,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

KEY = "lab-hs256-key-NOT-A-REAL-SECRET-0123456789"
ISSUER = "https://issuer.example.test"
AUDIENCE = "t.Svc"
UNAVAILABLE = grpc.StatusCode.UNAVAILABLE
POLICY = JwtPolicy(key=KEY, algorithms=("HS256",), issuer=ISSUER, audience=AUDIENCE)
SCOPES = {
    "/t.Svc/Echo": "t.read",
    "/t.Svc/Whoami": "t.read",
    "/t.Svc/Count": "t.read",
    "/t.Svc/Chat": "t.read",
    "/t.Svc/Write": "t.write",
}


def uu_handler(behavior: Any, **kwargs: Any) -> Any:
    return cast("Any", grpc.unary_unary_rpc_method_handler(behavior, **kwargs))


def us_handler(behavior: Any, **kwargs: Any) -> Any:
    return cast("Any", grpc.unary_stream_rpc_method_handler(behavior, **kwargs))


def su_handler(behavior: Any, **kwargs: Any) -> Any:
    return cast("Any", grpc.stream_unary_rpc_method_handler(behavior, **kwargs))


def ss_handler(behavior: Any, **kwargs: Any) -> Any:
    return cast("Any", grpc.stream_stream_rpc_method_handler(behavior, **kwargs))


def open_channel(target: str, **kwargs: Any) -> Any:
    return cast("Any", grpc.aio.insecure_channel(target, **kwargs))


def client_details(
    method: bytes, timeout: float | None, metadata: Any, credentials: Any, wait_for_ready: Any
) -> Any:
    """`grpc.aio.ClientCallDetails` carries the method as bytes, though its base class says str."""
    return cast("Any", grpc.aio.ClientCallDetails)(
        method, timeout, metadata, credentials, wait_for_ready
    )


def mint(**overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": "alice",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 300,
        "scope": "t.read",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, KEY, algorithm="HS256")


def bearer(token: str) -> list[str]:
    return [f"Bearer {token}"]


def unary_policy(attempts: int = 4, codes: Sequence[Any] = (UNAVAILABLE,)) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=attempts,
        initial_backoff_s=0.01,
        max_backoff_s=0.05,
        backoff_multiplier=2.0,
        retryable_codes=tuple(codes),
    )


# ---------------------------------------------------------------------------
# Fakes for in-process (no socket) interceptor tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Details:
    method: str
    invocation_metadata: tuple[tuple[str, str], ...] = ()


def details(method: str, *metadata: tuple[str, str]) -> grpc.HandlerCallDetails:
    return cast("grpc.HandlerCallDetails", Details(method, metadata))


class FakeContext:
    """The slice of `grpc.aio.ServicerContext` the interceptors touch."""

    def __init__(self) -> None:
        self._code: Any = None
        self.aborted: tuple[Any, str] | None = None

    def code(self) -> Any:
        return self._code

    def set_code(self, code: Any) -> None:
        self._code = code

    async def abort(self, code: Any, details: str = "") -> None:
        self._code = code
        self.aborted = (code, details)
        raise grpc.aio.AbortError("Locally aborted.")


def build_chain(interceptors: Sequence[Any], terminal: Any) -> Any:
    """Compose interceptors the way cygrpc `_run_interceptor` does."""

    async def run(index: int, call_details: Any) -> Any:
        if index == len(interceptors):
            return terminal(call_details)

        async def continuation(next_details: Any) -> Any:
            return await run(index + 1, next_details)

        return await interceptors[index].intercept_service(continuation, call_details)

    def entry(call_details: Any) -> Any:
        return run(0, call_details)

    return entry


async def echo_behavior(request: bytes, context: Any) -> bytes:
    return request


def echo_terminal(call_details: Any) -> Any:
    if call_details.method.startswith("/t.Svc/"):
        return uu_handler(echo_behavior)
    return None


class Telemetry:
    """A tracer provider with an in-memory exporter plus the interceptor built on it."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.server_tracer = provider.get_tracer("lab.server")
        self.client_tracer = provider.get_tracer("lab.client")
        self.metrics = RpcMetrics()
        self.interceptor = ServerTelemetryInterceptor(
            self.server_tracer, self.metrics, logger=logger
        )

    def spans(self, kind: SpanKind) -> list[ReadableSpan]:
        return [s for s in self.exporter.get_finished_spans() if s.kind == kind]


# ---------------------------------------------------------------------------
# Token extraction and verification (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_bearer_scheme_is_case_insensitive(scheme: str) -> None:
    assert extract_bearer([f"{scheme} abc.def-ghi_jkl"]) == "abc.def-ghi_jkl"


@pytest.mark.unit
@pytest.mark.parametrize(
    "values",
    [
        [],
        ["Bearer a.b", "Bearer c.d"],
        ["Basic dXNlcjpwYXNz"],
        ["Bearer"],
        ["Bearer "],
        ["Bearer a b"],
        ["Bearer a.b\r\nX-Evil: 1"],
        ["Bearer " + "a" * 4097],
    ],
    ids=["none", "two-values", "basic", "bare", "empty", "space-in-token", "crlf", "oversize"],
)
def test_malformed_authorization_is_unauthenticated(values: list[str]) -> None:
    with pytest.raises(AuthError) as info:
        extract_bearer(values)
    assert info.value.code == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.unit
def test_valid_token_yields_principal_from_verified_claims() -> None:
    principal = verify_bearer(POLICY, bearer(mint(sub="alice", scope="t.read t.write")))
    assert principal.subject == "alice"
    assert principal.scopes == frozenset({"t.read", "t.write"})


@pytest.mark.unit
def test_token_without_exp_is_rejected_because_exp_is_required() -> None:
    token = mint(exp=None)
    with pytest.raises(AuthError) as info:
        verify_bearer(POLICY, bearer(token))
    assert "MissingRequiredClaimError" in info.value.reason
    relaxed = JwtPolicy(
        key=KEY,
        algorithms=("HS256",),
        issuer=ISSUER,
        audience=AUDIENCE,
        required_claims=("iss", "aud", "sub"),
    )
    assert verify_bearer(relaxed, bearer(token)).subject == "alice"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"exp": int(time.time()) - 60}, "ExpiredSignatureError"),
        ({"aud": "other"}, "InvalidAudienceError"),
        ({"iss": "https://evil.example.test"}, "InvalidIssuerError"),
        ({"nbf": int(time.time()) + 600}, "ImmatureSignatureError"),
    ],
    ids=["expired", "audience", "issuer", "not-before"],
)
def test_claim_failures_are_unauthenticated_with_generic_detail(
    overrides: dict[str, Any], needle: str
) -> None:
    with pytest.raises(AuthError) as info:
        verify_bearer(POLICY, bearer(mint(**overrides)))
    assert info.value.code == grpc.StatusCode.UNAUTHENTICATED
    assert needle in info.value.reason
    assert info.value.detail == "invalid credentials"


@pytest.mark.unit
def test_leeway_accepts_a_just_expired_token() -> None:
    token = mint(exp=int(time.time()) - 5)
    with pytest.raises(AuthError):
        verify_bearer(POLICY, bearer(token))
    lenient = JwtPolicy(
        key=KEY, algorithms=("HS256",), issuer=ISSUER, audience=AUDIENCE, leeway_s=30
    )
    assert verify_bearer(lenient, bearer(token)).subject == "alice"


@pytest.mark.unit
def test_alg_none_and_wrong_algorithm_are_rejected() -> None:
    claims = {"sub": "alice", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300}
    unsigned = jwt.encode(claims, "", algorithm="none")
    with pytest.raises(AuthError) as none_info:
        verify_bearer(POLICY, bearer(unsigned))
    assert "InvalidAlgorithmError" in none_info.value.reason
    hs512 = jwt.encode(claims, KEY * 2, algorithm="HS512")
    with pytest.raises(AuthError) as hs512_info:
        verify_bearer(POLICY, bearer(hs512))
    assert "InvalidAlgorithmError" in hs512_info.value.reason


@pytest.mark.unit
def test_required_typ_header_rejects_a_token_of_another_kind() -> None:
    strict = JwtPolicy(
        key=KEY,
        algorithms=("HS256",),
        issuer=ISSUER,
        audience=AUDIENCE,
        required_typ="at+jwt",
    )
    claims = {"sub": "alice", "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 300}
    access = jwt.encode(claims, KEY, algorithm="HS256", headers={"typ": "at+jwt"})
    prefixed = jwt.encode(claims, KEY, algorithm="HS256", headers={"typ": "application/at+jwt"})
    plain = jwt.encode(claims, KEY, algorithm="HS256")
    assert verify_bearer(strict, bearer(access)).subject == "alice"
    assert verify_bearer(strict, bearer(prefixed)).subject == "alice"
    with pytest.raises(AuthError) as info:
        verify_bearer(strict, bearer(plain))
    assert "unexpected typ 'JWT'" in info.value.reason
    assert verify_bearer(POLICY, bearer(plain)).subject == "alice"


@pytest.mark.unit
def test_tampered_signature_is_rejected() -> None:
    header, payload, signature = mint().split(".")
    forged = ".".join(
        [header, payload, signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")]
    )
    with pytest.raises(AuthError) as info:
        verify_bearer(POLICY, bearer(forged))
    assert "InvalidSignatureError" in info.value.reason


# ---------------------------------------------------------------------------
# Server auth interceptor (unit, no sockets)
# ---------------------------------------------------------------------------


def jwt_interceptor(**kwargs: Any) -> JwtServerInterceptor:
    return JwtServerInterceptor(POLICY, required_scopes=SCOPES, **kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejection_never_calls_the_continuation() -> None:
    called: list[str] = []

    async def continuation(call_details: Any) -> Any:
        called.append(call_details.method)
        return uu_handler(echo_behavior)

    handler: Any = await jwt_interceptor().intercept_service(continuation, details("/t.Svc/Echo"))
    assert called == []
    context = FakeContext()
    with pytest.raises(grpc.aio.AbortError):
        await handler.stream_stream(None, context)
    assert context.aborted == (grpc.StatusCode.UNAUTHENTICATED, "invalid credentials")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_token_reaches_handler_and_sets_principal() -> None:
    seen: list[str] = []

    async def behavior(request: bytes, context: Any) -> bytes:
        principal = current_principal.get()
        assert principal is not None
        seen.append(principal.subject)
        return request

    def terminal(call_details: Any) -> Any:
        return uu_handler(behavior)

    chain = build_chain([jwt_interceptor()], terminal)
    handler = await chain(details("/t.Svc/Echo", ("authorization", f"Bearer {mint()}")))
    assert await handler.unary_unary(b"hi", FakeContext()) == b"hi"
    assert seen == ["alice"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_scope_and_unlisted_method_are_permission_denied() -> None:
    chain = build_chain([jwt_interceptor()], echo_terminal)
    token = ("authorization", f"Bearer {mint(scope='t.read')}")
    for method in ("/t.Svc/Write", "/t.Svc/NotInPolicy"):
        handler = await chain(details(method, token))
        context = FakeContext()
        with pytest.raises(grpc.aio.AbortError):
            await handler.stream_stream(None, context)
        assert context.aborted == (grpc.StatusCode.PERMISSION_DENIED, "denied"), method


@pytest.mark.unit
@pytest.mark.asyncio
async def test_public_method_skips_authentication() -> None:
    interceptor = jwt_interceptor(public_methods=frozenset({"/t.Svc/Ping"}))
    chain = build_chain([interceptor], echo_terminal)
    handler = await chain(details("/t.Svc/Ping"))
    assert await handler.unary_unary(b"x", FakeContext()) == b"x"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_principal_is_isolated_between_concurrent_rpcs() -> None:
    gate = asyncio.Event()
    seen: dict[str, str] = {}

    async def behavior(request: bytes, context: Any) -> bytes:
        await gate.wait()
        principal = current_principal.get()
        assert principal is not None
        seen[request.decode()] = principal.subject
        return request

    def terminal(call_details: Any) -> Any:
        return uu_handler(behavior)

    chain = build_chain([jwt_interceptor()], terminal)

    async def one_rpc(name: bytes, subject: str) -> None:
        call_details = details("/t.Svc/Echo", ("authorization", f"Bearer {mint(sub=subject)}"))
        handler = await chain(call_details)
        await handler.unary_unary(name, FakeContext())

    tasks = [
        asyncio.create_task(one_rpc(b"first", "alice")),
        asyncio.create_task(one_rpc(b"second", "bob")),
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    assert seen == {"first": "alice", "second": "bob"}


# ---------------------------------------------------------------------------
# Server telemetry interceptor (unit, no sockets)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_span_continues_the_incoming_traceparent() -> None:
    telemetry = Telemetry()
    trace_id, parent_id = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    traceparent = f"00-{trace_id}-{parent_id}-01"
    chain = build_chain([telemetry.interceptor], echo_terminal)
    handler = await chain(details("/t.Svc/Echo", ("traceparent", traceparent)))
    await handler.unary_unary(b"x", FakeContext())
    (span,) = telemetry.spans(SpanKind.SERVER)
    assert span.name == "t.Svc/Echo"
    assert span.context is not None
    assert span.parent is not None
    assert f"{span.context.trace_id:032x}" == trace_id
    assert f"{span.parent.span_id:016x}" == parent_id
    attributes = dict(span.attributes or {})
    assert attributes["rpc.system.name"] == "grpc"
    assert attributes["rpc.method"] == "t.Svc/Echo"
    assert attributes["rpc.status_code"] == "OK"
    assert span.status.status_code == StatusCode.UNSET


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "is_error"),
    [
        (grpc.StatusCode.INVALID_ARGUMENT, False),
        (grpc.StatusCode.NOT_FOUND, False),
        (grpc.StatusCode.UNAUTHENTICATED, False),
        (grpc.StatusCode.UNAVAILABLE, True),
        (grpc.StatusCode.INTERNAL, True),
        (grpc.StatusCode.DEADLINE_EXCEEDED, True),
    ],
)
async def test_server_span_is_error_only_for_the_semconv_server_codes(
    code: Any, is_error: bool
) -> None:
    telemetry = Telemetry()

    async def failing(request: bytes, context: Any) -> bytes:
        await context.abort(code, "x")
        return b""

    def terminal(call_details: Any) -> Any:
        return uu_handler(failing)

    handler = await build_chain([telemetry.interceptor], terminal)(details("/t.Svc/Echo"))
    with pytest.raises(grpc.aio.AbortError):
        await handler.unary_unary(b"x", FakeContext())
    (span,) = telemetry.spans(SpanKind.SERVER)
    assert dict(span.attributes or {})["rpc.status_code"] == code.name
    assert (span.status.status_code == StatusCode.ERROR) is is_error
    assert telemetry.metrics.finished == {("t.Svc/Echo", code.name): 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_plain_exception_is_recorded_as_unknown_and_reraised() -> None:
    telemetry = Telemetry()

    async def boom(request: bytes, context: Any) -> bytes:
        raise RuntimeError("boom")

    def terminal(call_details: Any) -> Any:
        return uu_handler(boom)

    handler = await build_chain([telemetry.interceptor], terminal)(details("/t.Svc/Echo"))
    with pytest.raises(RuntimeError, match="boom"):
        await handler.unary_unary(b"x", FakeContext())
    assert telemetry.metrics.finished == {("t.Svc/Echo", "UNKNOWN"): 1}
    (span,) = telemetry.spans(SpanKind.SERVER)
    assert span.status.status_code == StatusCode.ERROR


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_is_recorded_and_propagated() -> None:
    telemetry = Telemetry()
    started = asyncio.Event()

    async def slow(request: bytes, context: Any) -> bytes:
        started.set()
        await asyncio.sleep(30)
        return b""

    def terminal(call_details: Any) -> Any:
        return uu_handler(slow)

    handler = await build_chain([telemetry.interceptor], terminal)(details("/t.Svc/Echo"))
    task = asyncio.create_task(handler.unary_unary(b"x", FakeContext()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert telemetry.metrics.finished == {("t.Svc/Echo", "CANCELLED"): 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_method_is_counted_under_the_bounded_other_label() -> None:
    telemetry = Telemetry()
    chain = build_chain([telemetry.interceptor], echo_terminal)
    assert await chain(details("/nope.Svc/Random-8f3a")) is None
    assert telemetry.metrics.finished == {("_OTHER", "UNIMPLEMENTED"): 1}
    assert telemetry.metrics.started == {"_OTHER": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_metrics_use_the_injected_clock() -> None:
    ticks = iter([10.0, 10.25])
    telemetry = Telemetry()
    interceptor = ServerTelemetryInterceptor(
        telemetry.server_tracer, telemetry.metrics, clock=lambda: next(ticks)
    )
    handler = await build_chain([interceptor], echo_terminal)(details("/t.Svc/Echo"))
    await handler.unary_unary(b"x", FakeContext())
    assert telemetry.metrics.seconds == {"t.Svc/Echo": [0.25]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrappers_keep_the_handler_kind_cygrpc_dispatches_on() -> None:
    async def unary_stream_gen(request: bytes, context: Any) -> AsyncIterator[bytes]:
        yield request

    async def unary_stream_rw(request: bytes, context: Any) -> None:
        return None

    async def stream_unary(request_iterator: Any, context: Any) -> bytes:
        return b""

    async def stream_stream_gen(request_iterator: Any, context: Any) -> AsyncIterator[bytes]:
        yield b""

    async def stream_stream_rw(request_iterator: Any, context: Any) -> None:
        return None

    handlers: dict[str, Any] = {
        "/t.Svc/A": uu_handler(echo_behavior),
        "/t.Svc/B": us_handler(unary_stream_gen),
        "/t.Svc/C": us_handler(unary_stream_rw),
        "/t.Svc/D": su_handler(stream_unary),
        "/t.Svc/E": ss_handler(stream_stream_gen),
        "/t.Svc/F": ss_handler(stream_stream_rw),
    }
    telemetry = Telemetry()

    def terminal(call_details: Any) -> Any:
        return handlers[call_details.method]

    chain = build_chain([telemetry.interceptor], terminal)
    wrapped = {m: await chain(details(m)) for m in handlers}
    assert inspect.iscoroutinefunction(wrapped["/t.Svc/A"].unary_unary)
    assert inspect.isasyncgenfunction(wrapped["/t.Svc/B"].unary_stream)
    assert inspect.iscoroutinefunction(wrapped["/t.Svc/C"].unary_stream)
    assert inspect.iscoroutinefunction(wrapped["/t.Svc/D"].stream_unary)
    assert inspect.isasyncgenfunction(wrapped["/t.Svc/E"].stream_stream)
    assert inspect.iscoroutinefunction(wrapped["/t.Svc/F"].stream_stream)
    for method, original in handlers.items():
        assert wrapped[method].request_streaming == original.request_streaming
        assert wrapped[method].response_streaming == original.response_streaming


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logs_carry_code_and_subject_but_never_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("lab.grpc.log-test")
    telemetry = Telemetry(logger)
    interceptors = [telemetry.interceptor, jwt_interceptor(logger=logger)]
    chain = build_chain(interceptors, echo_terminal)
    good = mint()
    bad = mint(aud="other")
    with caplog.at_level(logging.INFO, logger=logger.name):
        handler = await chain(details("/t.Svc/Echo", ("authorization", f"Bearer {good}")))
        await handler.unary_unary(b"x", FakeContext())
        rejected = await chain(details("/t.Svc/Echo", ("authorization", f"Bearer {bad}")))
        with pytest.raises(grpc.aio.AbortError):
            await rejected.stream_stream(None, FakeContext())
    assert good not in caplog.text
    assert bad not in caplog.text
    assert "code=OK" in caplog.text
    assert "subject=alice" in caplog.text
    assert "code=UNAUTHENTICATED" in caplog.text
    assert "InvalidAudienceError" in caplog.text


# ---------------------------------------------------------------------------
# Client helpers (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_with_metadata_replaces_listed_keys_and_keeps_the_rest() -> None:
    original = client_details(
        b"/t.Svc/Echo",
        2.5,
        grpc.aio.Metadata(("authorization", "Bearer old"), ("x-a", "1")),
        None,
        True,
    )
    updated: Any = with_metadata(
        original, [("authorization", "Bearer new")], replace=frozenset({"authorization"})
    )
    assert list(updated.metadata) == [("x-a", "1"), ("authorization", "Bearer new")]
    assert (updated.method, updated.timeout, updated.wait_for_ready) == (b"/t.Svc/Echo", 2.5, True)
    assert list(original.metadata) == [("authorization", "Bearer old"), ("x-a", "1")]
    none_meta = client_details(b"/t.Svc/Echo", None, None, None, None)
    added: Any = with_metadata(none_meta, [("k", "v")])
    assert list(added.metadata) == [("k", "v")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bearer_interceptor_fetches_a_fresh_token_for_every_call() -> None:
    tokens = iter(["t1", "t2"])
    sent: list[Any] = []

    async def provider() -> str:
        return next(tokens)

    async def continuation(call_details: Any, request: Any) -> str:
        sent.append(list(call_details.metadata))
        return "call"

    interceptor = BearerUnaryUnaryInterceptor(provider)
    base = client_details(b"/t.Svc/Echo", None, None, None, None)
    await interceptor.intercept_unary_unary(continuation, base, b"x")
    await interceptor.intercept_unary_unary(continuation, base, b"x")
    assert sent == [[("authorization", "Bearer t1")], [("authorization", "Bearer t2")]]


# ---------------------------------------------------------------------------
# Retry policy arithmetic and service-config construction (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backoff_follows_the_a6_formula_and_is_capped() -> None:
    policy = RetryPolicy(4, 0.1, 1.0, 2.0, (UNAVAILABLE,))
    assert backoff_delays(policy) == pytest.approx([0.1, 0.2, 0.4])
    capped = RetryPolicy(5, 0.1, 0.3, 2.0, (UNAVAILABLE,))
    assert backoff_delays(capped) == pytest.approx([0.1, 0.2, 0.3, 0.3])
    assert backoff_delays(policy, jitter=1.2) == pytest.approx([0.12, 0.24, 0.48])
    with pytest.raises(ValueError, match="jitter"):
        backoff_delays(policy, jitter=1.3)


@pytest.mark.unit
def test_attempts_above_the_client_cap_are_clamped_to_five() -> None:
    assert [effective_max_attempts(n) for n in (2, 5, 6, 100)] == [2, 5, 5, 5]
    policy = RetryPolicy(9, 0.1, 1.0, 2.0, (UNAVAILABLE,))
    assert len(backoff_delays(policy)) == 4


@pytest.mark.unit
def test_service_config_json_shape() -> None:
    config = build_retry_service_config(
        service="t.Svc",
        method="Flaky",
        policy=RetryPolicy(4, 0.1, 1.0, 2.0, (UNAVAILABLE, grpc.StatusCode.ABORTED)),
        throttle_max_tokens=10,
        throttle_token_ratio=0.1,
    )
    assert json.loads(config) == {
        "methodConfig": [
            {
                "name": [{"service": "t.Svc", "method": "Flaky"}],
                "retryPolicy": {
                    "maxAttempts": 4,
                    "initialBackoff": "0.1s",
                    "maxBackoff": "1s",
                    "backoffMultiplier": 2.0,
                    "retryableStatusCodes": ["UNAVAILABLE", "ABORTED"],
                },
            }
        ],
        "retryThrottling": {"maxTokens": 10, "tokenRatio": 0.1},
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        (1, 0.1, 1.0, 2.0, (UNAVAILABLE,)),
        (3, 0.0, 1.0, 2.0, (UNAVAILABLE,)),
        (3, 0.1, 0.0, 2.0, (UNAVAILABLE,)),
        (3, 0.1, 1.0, 0.0, (UNAVAILABLE,)),
        (3, 0.1, 1.0, 2.0, ()),
        (3, 0.1, 1.0, 2.0, (grpc.StatusCode.OK,)),
    ],
    ids=["one-attempt", "zero-initial", "zero-max", "zero-multiplier", "no-codes", "ok-code"],
)
def test_invalid_retry_policies_are_rejected_at_construction(args: Any) -> None:
    with pytest.raises(ValueError, match=r"maxAttempts|Backoff|backoffMultiplier|retryable|OK"):
        RetryPolicy(*args)


@pytest.mark.unit
def test_throttling_needs_both_fields_and_a_sane_range() -> None:
    policy = unary_policy()
    with pytest.raises(ValueError, match="needs both"):
        build_retry_service_config(service="t.Svc", policy=policy, throttle_max_tokens=10)
    with pytest.raises(ValueError, match="maxTokens"):
        build_retry_service_config(
            service="t.Svc", policy=policy, throttle_max_tokens=1001, throttle_token_ratio=0.1
        )


@pytest.mark.unit
def test_channel_options_switch_retries_on_and_off() -> None:
    assert retry_channel_options("{}") == [
        ("grpc.enable_retries", 1),
        ("grpc.service_config", "{}"),
    ]
    assert retry_channel_options("{}", enable_retries=False)[0] == ("grpc.enable_retries", 0)


# ---------------------------------------------------------------------------
# Real grpc.aio server and channel on 127.0.0.1:0 (socket)
# ---------------------------------------------------------------------------


class Service:
    """A generic-handler service: no generated code, bytes in and bytes out."""

    def __init__(self, fail_first: int = 0, fail_code: Any = UNAVAILABLE) -> None:
        self.fail_first = fail_first
        self.fail_code = fail_code
        self.attempts = 0
        self.previous_attempts: list[str | None] = []
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def echo(self, request: bytes, context: Any) -> bytes:
        return request

    async def whoami(self, request: bytes, context: Any) -> bytes:
        principal = current_principal.get()
        return principal.subject.encode() if principal else b"anonymous"

    async def flaky(self, request: bytes, context: Any) -> bytes:
        self.attempts += 1
        metadata = dict(context.invocation_metadata())
        self.previous_attempts.append(metadata.get("grpc-previous-rpc-attempts"))
        if self.attempts <= self.fail_first:
            await context.abort(self.fail_code, f"attempt {self.attempts} failed")
        return request

    async def flaky_headers(self, request: bytes, context: Any) -> bytes:
        self.attempts += 1
        await context.send_initial_metadata((("x-early", "1"),))
        await context.abort(UNAVAILABLE, "failed after headers")
        return request

    async def block(self, request: bytes, context: Any) -> bytes:
        self.entered.set()
        await self.gate.wait()
        return b"done"

    async def count(self, request: bytes, context: Any) -> AsyncIterator[bytes]:
        for i in range(3):
            yield f"{request.decode()}-{i}".encode()

    async def chat(self, request_iterator: Any, context: Any) -> None:
        async for message in request_iterator:
            await context.write(message)

    def generic_handler(self) -> Any:
        return grpc.method_handlers_generic_handler(
            "t.Svc",
            {
                "Echo": uu_handler(self.echo),
                "Whoami": uu_handler(self.whoami),
                "Flaky": uu_handler(self.flaky),
                "Block": uu_handler(self.block),
                "FlakyHeaders": uu_handler(self.flaky_headers),
                "Count": us_handler(self.count),
                "Chat": ss_handler(self.chat),
            },
        )


@asynccontextmanager
async def running_server(
    service: Service, interceptors: Sequence[Any] = (), max_concurrent: int | None = None
) -> AsyncGenerator[str]:
    server = grpc.aio.server(
        interceptors=list(interceptors), maximum_concurrent_rpcs=max_concurrent
    )
    server.add_generic_rpc_handlers((service.generic_handler(),))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(None)


async def static_token(token: str) -> str:
    return token


def authed_channel(target: str, token: str | None, **kwargs: Any) -> Any:
    interceptors: list[Any] = []
    if token is not None:
        provider_token = token

        async def provider() -> str:
            return provider_token

        interceptors.extend(bearer_interceptors(provider))
    return open_channel(target, interceptors=interceptors, **kwargs)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_authenticated_unary_call_sees_the_principal_in_the_handler() -> None:
    interceptors = [jwt_interceptor()]
    async with (
        running_server(Service(), interceptors) as target,
        authed_channel(target, mint(sub="alice")) as channel,
    ):
        reply = await channel.unary_unary("/t.Svc/Whoami")(b"", timeout=10)
    assert reply == b"alice"


@pytest.mark.socket
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["unary", "server-stream", "bidi"])
async def test_missing_token_is_unauthenticated_for_every_call_kind_without_hanging(
    kind: str,
) -> None:
    async with (
        running_server(Service(), [jwt_interceptor()]) as target,
        authed_channel(target, None) as channel,
    ):
        if kind == "unary":
            call = channel.unary_unary("/t.Svc/Echo")(b"x", timeout=10)
        elif kind == "server-stream":
            call = channel.unary_stream("/t.Svc/Count")(b"x", timeout=10)
        else:
            call = channel.stream_stream("/t.Svc/Chat")(timeout=10)
        code = await asyncio.wait_for(call.code(), timeout=5)
        details_text = await call.details()
    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert details_text == "invalid credentials"


@pytest.mark.socket
@pytest.mark.asyncio
async def test_bearer_interceptor_authenticates_streaming_calls_too() -> None:
    async with (
        running_server(Service(), [jwt_interceptor()]) as target,
        authed_channel(target, mint()) as channel,
    ):
        stream = channel.unary_stream("/t.Svc/Count")(b"n", timeout=10)
        messages = [m async for m in stream]
        chat = channel.stream_stream("/t.Svc/Chat")(timeout=10)
        await chat.write(b"ping")
        await chat.done_writing()
        echoed = [m async for m in chat]
    assert messages == [b"n-0", b"n-1", b"n-2"]
    assert echoed == [b"ping"]


@pytest.mark.socket
@pytest.mark.asyncio
async def test_telemetry_outside_auth_observes_rejections_and_inside_does_not() -> None:
    results: dict[str, RpcMetrics] = {}
    for label, telemetry_first in (("outer", True), ("inner", False)):
        telemetry = Telemetry()
        auth = jwt_interceptor()
        ordered = (
            [telemetry.interceptor, auth] if telemetry_first else [auth, telemetry.interceptor]
        )
        async with (
            running_server(Service(), ordered) as target,
            authed_channel(target, None) as channel,
        ):
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await channel.unary_unary("/t.Svc/Echo")(b"x", timeout=10)
        assert info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        results[label] = telemetry.metrics
    assert results["outer"].finished == {("t.Svc/Echo", "UNAUTHENTICATED"): 1}
    assert results["inner"].finished == {}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_streaming_handlers_work_through_the_telemetry_wrappers() -> None:
    telemetry = Telemetry()
    async with (
        running_server(Service(), [telemetry.interceptor]) as target,
        open_channel(target) as channel,
    ):
        messages = [m async for m in channel.unary_stream("/t.Svc/Count")(b"n", timeout=10)]
        chat = channel.stream_stream("/t.Svc/Chat")(timeout=10)
        await chat.write(b"a")
        await chat.done_writing()
        echoed = [m async for m in chat]
    assert messages == [b"n-0", b"n-1", b"n-2"]
    assert echoed == [b"a"]
    assert telemetry.metrics.finished == {("t.Svc/Count", "OK"): 1, ("t.Svc/Chat", "OK"): 1}


@pytest.mark.socket
@pytest.mark.asyncio
async def test_trace_context_crosses_the_wire_from_client_span_to_server_span() -> None:
    telemetry = Telemetry()
    interceptors = [telemetry.interceptor, jwt_interceptor()]
    async with running_server(Service(), interceptors) as target:
        client_interceptors: list[Any] = [
            ClientTracingInterceptor(telemetry.client_tracer),
            *bearer_interceptors(lambda: static_token(mint())),
        ]
        async with open_channel(target, interceptors=client_interceptors) as channel:
            assert await channel.unary_unary("/t.Svc/Echo")(b"x", timeout=10) == b"x"
    (client_span,) = telemetry.spans(SpanKind.CLIENT)
    (server_span,) = telemetry.spans(SpanKind.SERVER)
    assert client_span.context is not None
    assert server_span.context is not None
    assert server_span.context.trace_id == client_span.context.trace_id
    assert server_span.parent is not None
    assert server_span.parent.span_id == client_span.context.span_id
    assert dict(client_span.attributes or {})["rpc.status_code"] == "OK"
    assert client_span.status.status_code == StatusCode.UNSET


@pytest.mark.socket
@pytest.mark.asyncio
async def test_client_span_is_error_for_any_non_ok_status() -> None:
    telemetry = Telemetry()
    interceptors = [ClientTracingInterceptor(telemetry.client_tracer)]
    async with (
        running_server(Service(fail_first=9, fail_code=grpc.StatusCode.NOT_FOUND)) as target,
        open_channel(target, interceptors=interceptors) as channel,
    ):
        with pytest.raises(grpc.aio.AioRpcError):
            await channel.unary_unary("/t.Svc/Flaky")(b"x", timeout=10)
    (span,) = telemetry.spans(SpanKind.CLIENT)
    assert dict(span.attributes or {})["rpc.status_code"] == "NOT_FOUND"
    assert span.status.status_code == StatusCode.ERROR


# --- retries ---------------------------------------------------------------


class CountingClientInterceptor(grpc.aio.UnaryUnaryClientInterceptor):  # type: ignore[misc]
    def __init__(self) -> None:
        self.calls = 0

    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        self.calls += 1
        return await continuation(client_call_details, request)


def retry_options(policy: RetryPolicy, **kwargs: Any) -> list[tuple[str, str | int]]:
    config = build_retry_service_config(service="t.Svc", method="Flaky", policy=policy, **kwargs)
    return retry_channel_options(config)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unavailable_is_retried_and_the_server_sees_each_attempt_numbered() -> None:
    service = Service(fail_first=2)
    counter = CountingClientInterceptor()
    telemetry = Telemetry()
    async with running_server(service, [telemetry.interceptor]) as target:
        channel = open_channel(
            target, options=retry_options(unary_policy()), interceptors=[counter]
        )
        async with channel:
            assert await channel.unary_unary("/t.Svc/Flaky")(b"ok", timeout=10) == b"ok"
    assert service.attempts == 3
    assert service.previous_attempts == [None, "1", "2"]
    assert counter.calls == 1
    assert telemetry.metrics.started == {"t.Svc/Flaky": 3}
    attempts = [
        dict(s.attributes or {})["app.grpc.previous_attempts"]
        for s in telemetry.spans(SpanKind.SERVER)
    ]
    assert attempts == ["0", "1", "2"]
    assert telemetry.metrics.finished == {
        ("t.Svc/Flaky", "UNAVAILABLE"): 2,
        ("t.Svc/Flaky", "OK"): 1,
    }


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_status_outside_retryable_codes_is_not_retried() -> None:
    service = Service(fail_first=9, fail_code=grpc.StatusCode.INVALID_ARGUMENT)
    async with (
        running_server(service) as target,
        open_channel(target, options=retry_options(unary_policy())) as ch,
    ):
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await ch.unary_unary("/t.Svc/Flaky")(b"x", timeout=10)
    assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert service.attempts == 1


@pytest.mark.socket
@pytest.mark.asyncio
async def test_attempts_stop_at_max_attempts_and_the_last_status_is_returned() -> None:
    service = Service(fail_first=99)
    async with (
        running_server(service) as target,
        open_channel(target, options=retry_options(unary_policy(3))) as ch,
    ):
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await ch.unary_unary("/t.Svc/Flaky")(b"x", timeout=10)
    assert info.value.code() == UNAVAILABLE
    assert info.value.details() == "attempt 3 failed"
    assert service.attempts == 3


@pytest.mark.socket
@pytest.mark.asyncio
async def test_max_attempts_above_five_is_clamped_by_the_client_core() -> None:
    service = Service(fail_first=99)
    async with (
        running_server(service) as target,
        open_channel(target, options=retry_options(unary_policy(9))) as ch,
    ):
        with pytest.raises(grpc.aio.AioRpcError):
            await ch.unary_unary("/t.Svc/Flaky")(b"x", timeout=10)
    assert service.attempts == 5


@pytest.mark.socket
@pytest.mark.asyncio
async def test_enable_retries_zero_ignores_the_service_config_policy() -> None:
    service = Service(fail_first=99)
    config = build_retry_service_config(service="t.Svc", method="Flaky", policy=unary_policy())
    options = retry_channel_options(config, enable_retries=False)
    async with (
        running_server(service) as target,
        open_channel(target, options=options) as channel,
    ):
        with pytest.raises(grpc.aio.AioRpcError):
            await channel.unary_unary("/t.Svc/Flaky")(b"x", timeout=10)
    assert service.attempts == 1


@pytest.mark.socket
@pytest.mark.asyncio
async def test_retry_throttling_stops_retries_once_tokens_fall_to_half() -> None:
    service = Service(fail_first=99)
    options = retry_options(unary_policy(5), throttle_max_tokens=4, throttle_token_ratio=0.1)
    async with (
        running_server(service) as target,
        open_channel(target, options=options) as channel,
    ):
        call = channel.unary_unary("/t.Svc/Flaky")
        with pytest.raises(grpc.aio.AioRpcError):
            await call(b"x", timeout=10)
        first_call_attempts = service.attempts
        with pytest.raises(grpc.aio.AioRpcError):
            await call(b"x", timeout=10)
        second_call_attempts = service.attempts - first_call_attempts
    assert first_call_attempts == 2
    assert second_call_attempts == 1


@pytest.mark.socket
@pytest.mark.asyncio
async def test_retry_policy_applies_only_to_the_named_method() -> None:
    service = Service(fail_first=99)
    config = build_retry_service_config(service="t.Svc", method="Other", policy=unary_policy())
    options = retry_channel_options(config)
    async with (
        running_server(service) as target,
        open_channel(target, options=options) as channel,
    ):
        with pytest.raises(grpc.aio.AioRpcError):
            await channel.unary_unary("/t.Svc/Flaky")(b"x", timeout=10)
    assert service.attempts == 1


class UnaryAndStreamCounter(
    grpc.aio.UnaryUnaryClientInterceptor,  # type: ignore[misc]
    grpc.aio.UnaryStreamClientInterceptor,  # type: ignore[misc]
):
    """Implements two interfaces in one class: the channel registers it for only the first."""

    def __init__(self) -> None:
        self.unary_calls = 0
        self.stream_calls = 0

    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        self.unary_calls += 1
        return await continuation(client_call_details, request)

    async def intercept_unary_stream(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        self.stream_calls += 1
        return await continuation(client_call_details, request)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_a_multi_interface_client_interceptor_is_registered_for_the_first_kind_only() -> None:
    counter = UnaryAndStreamCounter()
    async with (
        running_server(Service()) as target,
        open_channel(target, interceptors=[counter]) as channel,
    ):
        await channel.unary_unary("/t.Svc/Echo")(b"x", timeout=10)
        _ = [m async for m in channel.unary_stream("/t.Svc/Count")(b"n", timeout=10)]
    assert counter.unary_calls == 1
    assert counter.stream_calls == 0


class CountingServerInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    def __init__(self) -> None:
        self.calls = 0

    async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        self.calls += 1
        return await continuation(handler_call_details)


class ExplodingServerInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        raise RuntimeError("secret-internal-detail")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_exception_escaping_an_interceptor_reaches_the_client_as_unknown_with_its_text() -> (
    None
):
    async with (
        running_server(Service(), [ExplodingServerInterceptor()]) as target,
        open_channel(target) as channel,
    ):
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await channel.unary_unary("/t.Svc/Echo")(b"x", timeout=10)
    assert info.value.code() == grpc.StatusCode.UNKNOWN
    assert "secret-internal-detail" in (info.value.details() or "")


@pytest.mark.socket
@pytest.mark.asyncio
async def test_shed_rpcs_run_interceptors_but_never_reach_handler_wrappers() -> None:
    counting = CountingServerInterceptor()
    telemetry = Telemetry()
    service = Service()
    async with (
        running_server(service, [counting, telemetry.interceptor], max_concurrent=1) as target,
        open_channel(target) as channel,
    ):
        first = asyncio.ensure_future(channel.unary_unary("/t.Svc/Block")(b"", timeout=10))
        await asyncio.wait_for(service.entered.wait(), timeout=5)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await channel.unary_unary("/t.Svc/Echo")(b"x", timeout=10)
        service.gate.set()
        assert await first == b"done"
    assert info.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert counting.calls == 2
    assert telemetry.metrics.started == {"t.Svc/Block": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_rejects_objects_that_are_not_server_interceptors() -> None:
    with pytest.raises(ValueError, match="must be ServerInterceptor"):
        grpc.aio.server(interceptors=cast("Any", [object()]))


@pytest.mark.socket
@pytest.mark.asyncio
async def test_sending_initial_metadata_before_failing_commits_the_rpc_and_disables_retries() -> (
    None
):
    service = Service()
    config = build_retry_service_config(service="t.Svc", policy=unary_policy())
    options = retry_channel_options(config)
    async with (
        running_server(service) as target,
        open_channel(target, options=options) as channel,
    ):
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await channel.unary_unary("/t.Svc/FlakyHeaders")(b"x", timeout=10)
    assert info.value.code() == UNAVAILABLE
    assert service.attempts == 1


class UnaryShapedRejectInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """The tempting rejection: a unary-unary handler, which cygrpc serves by reading one request."""

    async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        async def reject(request: bytes, context: Any) -> bytes:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid credentials")
            return b""

        return uu_handler(reject)


@pytest.mark.socket
@pytest.mark.asyncio
async def test_unary_shaped_rejection_leaves_a_silent_bidi_client_waiting_for_its_deadline() -> (
    None
):
    async with (
        running_server(Service(), [UnaryShapedRejectInterceptor()]) as target,
        open_channel(target) as channel,
    ):
        silent = channel.stream_stream("/t.Svc/Chat")(timeout=1)
        assert await silent.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        talking = channel.stream_stream("/t.Svc/Chat")(timeout=10)
        await talking.write(b"hello")
        assert await asyncio.wait_for(talking.code(), timeout=5) == grpc.StatusCode.UNAUTHENTICATED
