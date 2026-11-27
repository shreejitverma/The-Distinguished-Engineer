"""Async gRPC interceptors for grpc.aio 1.85.1: JWT auth, OpenTelemetry tracing, retries.

Server side, `grpc.aio.ServerInterceptor.intercept_service` runs once per RPC attempt and sees only
the method name and the invocation metadata, never the request.
It returns the `RpcMethodHandler` that will serve the call, so both rejection (return a handler
that aborts) and instrumentation (return a handler that wraps the real one) are handler rewrites.

Client side, interceptors run once per logical call, above the C-core retry layer, so
service-config retries are invisible to them.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import grpc  # type: ignore[import-untyped]
import grpc.aio  # type: ignore[import-untyped]
import jwt

if TYPE_CHECKING:
    from opentelemetry.context import Context

from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# ---------------------------------------------------------------------------
# Retry service config (gRFC A6)
# ---------------------------------------------------------------------------

CLIENT_MAX_ATTEMPTS_CAP: Final = 5
"""Client-side ceiling on `maxAttempts` (gRFC A6, C-core MAX_MAX_RETRY_ATTEMPTS)."""

PREVIOUS_ATTEMPTS_KEY: Final = "grpc-previous-rpc-attempts"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """One `retryPolicy` block of a `methodConfig` entry."""

    max_attempts: int
    initial_backoff_s: float
    max_backoff_s: float
    backoff_multiplier: float
    retryable_codes: tuple[grpc.StatusCode, ...]

    def __post_init__(self) -> None:
        if self.max_attempts < 2:
            raise ValueError("maxAttempts must be at least 2")
        if self.initial_backoff_s <= 0 or self.max_backoff_s <= 0:
            raise ValueError("initialBackoff and maxBackoff must be greater than 0")
        if self.backoff_multiplier <= 0:
            raise ValueError("backoffMultiplier must be greater than 0")
        if not self.retryable_codes:
            raise ValueError("retryableStatusCodes must be non-empty")
        if grpc.StatusCode.OK in self.retryable_codes:
            raise ValueError("OK is not a retryable status code")


def effective_max_attempts(configured: int) -> int:
    """Return the attempt count the client will actually use (A6 clamps, it does not reject)."""
    return min(configured, CLIENT_MAX_ATTEMPTS_CAP)


def backoff_delays(policy: RetryPolicy, jitter: float = 1.0) -> list[float]:
    """Delays before each retry: `min(initial * mult**(n-1), max) * jitter`, jitter in 0.8-1.2."""
    if not 0.8 <= jitter <= 1.2:
        raise ValueError("A6 jitter is a factor in [0.8, 1.2]")
    retries = effective_max_attempts(policy.max_attempts) - 1
    return [
        min(policy.initial_backoff_s * policy.backoff_multiplier**n, policy.max_backoff_s) * jitter
        for n in range(retries)
    ]


def _duration(seconds: float) -> str:
    return f"{seconds:.9g}s"


def build_retry_service_config(
    *,
    service: str,
    policy: RetryPolicy,
    method: str | None = None,
    throttle_max_tokens: int | None = None,
    throttle_token_ratio: float | None = None,
) -> str:
    """Serialize a service config with one retry policy and optional `retryThrottling`."""
    name: dict[str, str] = {"service": service}
    if method is not None:
        name["method"] = method
    config: dict[str, Any] = {
        "methodConfig": [
            {
                "name": [name],
                "retryPolicy": {
                    "maxAttempts": policy.max_attempts,
                    "initialBackoff": _duration(policy.initial_backoff_s),
                    "maxBackoff": _duration(policy.max_backoff_s),
                    "backoffMultiplier": policy.backoff_multiplier,
                    "retryableStatusCodes": [code.name for code in policy.retryable_codes],
                },
            }
        ]
    }
    if (throttle_max_tokens is None) != (throttle_token_ratio is None):
        raise ValueError("retryThrottling needs both maxTokens and tokenRatio")
    if throttle_max_tokens is not None and throttle_token_ratio is not None:
        if not 0 < throttle_max_tokens <= 1000:
            raise ValueError("maxTokens must be in (0, 1000]")
        if throttle_token_ratio <= 0:
            raise ValueError("tokenRatio must be greater than 0")
        config["retryThrottling"] = {
            "maxTokens": throttle_max_tokens,
            "tokenRatio": throttle_token_ratio,
        }
    return json.dumps(config, sort_keys=True)


def retry_channel_options(
    service_config: str, *, enable_retries: bool = True
) -> list[tuple[str, str | int]]:
    """Channel arguments that install a service config and switch the retry layer on or off."""
    return [
        ("grpc.enable_retries", 1 if enable_retries else 0),
        ("grpc.service_config", service_config),
    ]


# ---------------------------------------------------------------------------
# JWT verification (RFC 7519, RFC 8725, RFC 6750, RFC 9110 Section 11.1)
# ---------------------------------------------------------------------------

_B64TOKEN: Final = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
MAX_TOKEN_BYTES: Final = 4096
INVALID_CREDENTIALS: Final = "invalid credentials"


class AuthError(Exception):
    """Authentication or authorization failure; `detail` is safe to send to the client."""

    def __init__(self, code: grpc.StatusCode, reason: str, detail: str = INVALID_CREDENTIALS):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, derived only from claims that passed verification."""

    subject: str
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class JwtPolicy:
    """Everything a verifier needs, fixed at construction time and never read from the token."""

    key: str | bytes
    algorithms: tuple[str, ...]
    issuer: str
    audience: str
    leeway_s: float = 0.0
    required_claims: tuple[str, ...] = ("exp", "iss", "aud", "sub")
    required_typ: str | None = None
    """Required `typ` header (RFC 9068 Section 4 uses `at+jwt`); None disables the check."""


current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "current_principal", default=None
)


def extract_bearer(values: Sequence[str]) -> str:
    """Return the b64token from exactly one `Authorization: Bearer <token>` value."""
    if len(values) != 1:
        raise AuthError(
            grpc.StatusCode.UNAUTHENTICATED, f"expected 1 authorization, got {len(values)}"
        )
    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer":
        raise AuthError(grpc.StatusCode.UNAUTHENTICATED, "authorization scheme is not Bearer")
    token = token.lstrip(" ")
    if len(token) > MAX_TOKEN_BYTES or _B64TOKEN.fullmatch(token) is None:
        raise AuthError(grpc.StatusCode.UNAUTHENTICATED, "malformed bearer token")
    return token


def verify_bearer(policy: JwtPolicy, values: Sequence[str]) -> Principal:
    """Verify the token and return the principal, or raise `AuthError` (never leaks the reason)."""
    token = extract_bearer(values)
    try:
        if policy.required_typ is not None:
            typ = jwt.get_unverified_header(token).get("typ")
            if typ not in (policy.required_typ, f"application/{policy.required_typ}"):
                raise AuthError(grpc.StatusCode.UNAUTHENTICATED, f"unexpected typ {typ!r}")
        claims: dict[str, Any] = jwt.decode(
            token,
            policy.key,
            algorithms=list(policy.algorithms),
            audience=policy.audience,
            issuer=policy.issuer,
            leeway=policy.leeway_s,
            options={"require": list(policy.required_claims)},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(grpc.StatusCode.UNAUTHENTICATED, f"{type(exc).__name__}: {exc}") from exc
    scope: object = claims.get("scope", "")
    scopes = frozenset(scope.split()) if isinstance(scope, str) else frozenset[str]()
    return Principal(subject=str(claims["sub"]), scopes=scopes)


def metadata_values(metadata: Iterable[tuple[str, str | bytes]], key: str) -> list[str]:
    """All text values for a lower-case metadata key (binary `-bin` values are skipped)."""
    return [v for k, v in metadata if k == key and isinstance(v, str)]


# ---------------------------------------------------------------------------
# Server interceptors
# ---------------------------------------------------------------------------


def _reject_handler(code: grpc.StatusCode, detail: str) -> Any:
    """A handler that aborts at once, without reading a request.

    Its arity is stream-stream on purpose: the server never waits for a request message, so a
    bidirectional client that talks only after the server does cannot deadlock against it.
    """

    async def reject(request_iterator: Any, context: Any) -> None:
        await context.abort(code, detail)

    return cast("Any", grpc.stream_stream_rpc_method_handler(reject))


class JwtServerInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """Authenticate and authorize before the continuation, fail closed on unknown methods."""

    def __init__(
        self,
        policy: JwtPolicy,
        *,
        required_scopes: Mapping[str, str],
        public_methods: frozenset[str] = frozenset(),
        logger: logging.Logger | None = None,
    ) -> None:
        self._policy = policy
        self._required_scopes = dict(required_scopes)
        self._public = public_methods
        self._log = logger or logging.getLogger(__name__)

    def authorize(
        self, method: str, metadata: Iterable[tuple[str, str | bytes]]
    ) -> Principal | None:
        """Return the principal (None for a public method) or raise `AuthError`."""
        if method in self._public:
            return None
        principal = verify_bearer(self._policy, metadata_values(metadata, "authorization"))
        needed = self._required_scopes.get(method)
        if needed is None:
            raise AuthError(
                grpc.StatusCode.PERMISSION_DENIED, "method has no scope policy", "denied"
            )
        if needed not in principal.scopes:
            raise AuthError(grpc.StatusCode.PERMISSION_DENIED, f"missing scope {needed}", "denied")
        return principal

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        try:
            principal = self.authorize(
                handler_call_details.method, handler_call_details.invocation_metadata
            )
        except AuthError as exc:
            self._log.warning(
                "auth rejected method=%r code=%s reason=%s",
                handler_call_details.method,
                exc.code.name,
                exc.reason,
            )
            return _reject_handler(exc.code, exc.detail)
        if principal is not None:
            current_principal.set(principal)
        return await continuation(handler_call_details)


@dataclass(slots=True)
class RpcMetrics:
    """In-memory counters standing in for a Prometheus or OTel metrics pipeline."""

    started: dict[str, int] = field(default_factory=lambda: {})
    finished: dict[tuple[str, str], int] = field(default_factory=lambda: {})
    seconds: dict[str, list[float]] = field(default_factory=lambda: {})

    def start(self, method: str) -> None:
        self.started[method] = self.started.get(method, 0) + 1

    def finish(self, method: str, code: grpc.StatusCode, elapsed_s: float) -> None:
        key = (method, code.name)
        self.finished[key] = self.finished.get(key, 0) + 1
        self.seconds.setdefault(method, []).append(elapsed_s)


OTHER_METHOD: Final = "_OTHER"
"""`rpc.method` value for a method no handler serves (bounded label cardinality)."""

# Server span status is ERROR only for these codes (OpenTelemetry gRPC semantic conventions).
SERVER_ERROR_CODES: Final = frozenset(
    {
        grpc.StatusCode.UNKNOWN,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNIMPLEMENTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DATA_LOSS,
    }
)


def _rpc_method(path: str) -> str:
    return path.lstrip("/")


def _final_code(context: Any, *, failed: bool) -> grpc.StatusCode:
    """Mirror cygrpc: a raised non-abort exception with no code set becomes UNKNOWN."""
    code = context.code()
    if code is None:
        return grpc.StatusCode.UNKNOWN if failed else grpc.StatusCode.OK
    return code


class _ServerScope:
    """One observed server call; `finish` is idempotent so a span ends exactly once."""

    def __init__(self, owner: ServerTelemetryInterceptor, method: str, span: Span) -> None:
        self._owner = owner
        self._method = method
        self._span = span
        self._started = owner.clock()
        self._done = False
        owner.metrics.start(method)

    def finish(self, code: grpc.StatusCode) -> None:
        if self._done:
            return
        self._done = True
        elapsed = self._owner.clock() - self._started
        span = self._span
        span.set_attribute("rpc.status_code", code.name)
        if code in SERVER_ERROR_CODES:
            span.set_attribute("error.type", code.name)
            span.set_status(Status(StatusCode.ERROR))
        span.end()
        self._owner.metrics.finish(self._method, code, elapsed)
        principal = current_principal.get()
        self._owner.log.info(
            "rpc method=%s code=%s duration_ms=%.3f trace_id=%032x subject=%s",
            self._method,
            code.name,
            elapsed * 1000,
            span.get_span_context().trace_id,
            principal.subject if principal else "-",
        )


class ServerTelemetryInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    """Wrap the handler the rest of the chain returns with a SERVER span, metrics, and a log line.

    Register it before the auth interceptor so rejections pass through it too.
    """

    def __init__(
        self,
        tracer: Tracer,
        metrics: RpcMetrics,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.metrics = metrics
        self.log = logger or logging.getLogger(__name__)
        self.clock = clock
        self._tracer = tracer
        self._propagator = TraceContextTextMapPropagator()

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler[Any, Any] | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        handler = await continuation(handler_call_details)
        if handler is None:
            self.metrics.start(OTHER_METHOD)
            self.metrics.finish(OTHER_METHOD, grpc.StatusCode.UNIMPLEMENTED, 0.0)
            return handler
        metadata = handler_call_details.invocation_metadata
        previous = metadata_values(metadata, PREVIOUS_ATTEMPTS_KEY)
        return self._wrap(
            handler,
            _rpc_method(handler_call_details.method),
            self._propagator.extract(dict(metadata_values_pairs(metadata))),
            previous[0] if previous else "0",
        )

    def _scope(self, method: str, parent: Context, attempt: str) -> _ServerScope:
        span = self._tracer.start_span(
            method,
            context=parent,
            kind=SpanKind.SERVER,
            attributes={
                "rpc.system.name": "grpc",
                "rpc.method": method,
                "app.grpc.previous_attempts": attempt,
            },
        )
        return _ServerScope(self, method, span)

    def _wrap(self, handler: Any, method: str, parent: Context, attempt: str) -> Any:
        """Return a handler of the same arity and the same coroutine/async-generator kind.

        cygrpc picks its calling convention with `inspect.iscoroutinefunction` and
        `inspect.isasyncgenfunction` on the handler, so a wrapper of the wrong kind breaks the call.
        """
        scope_for = self._scope
        kwargs = {
            "request_deserializer": handler.request_deserializer,
            "response_serializer": handler.response_serializer,
        }
        if not handler.response_streaming:
            unary_inner = handler.stream_unary if handler.request_streaming else handler.unary_unary

            async def unary_response(request: Any, context: Any) -> Any:
                scope = scope_for(method, parent, attempt)
                try:
                    response = await unary_inner(request, context)
                except asyncio.CancelledError:
                    scope.finish(grpc.StatusCode.CANCELLED)
                    raise
                except BaseException:
                    scope.finish(_final_code(context, failed=True))
                    raise
                scope.finish(_final_code(context, failed=False))
                return response

            if handler.request_streaming:
                return grpc.stream_unary_rpc_method_handler(unary_response, **kwargs)
            return grpc.unary_unary_rpc_method_handler(unary_response, **kwargs)

        stream_inner = handler.stream_stream if handler.request_streaming else handler.unary_stream
        stream_behavior: Any
        if inspect.isasyncgenfunction(stream_inner):

            async def stream_gen(request: Any, context: Any) -> AsyncIterator[Any]:
                scope = scope_for(method, parent, attempt)
                try:
                    async for response in stream_inner(request, context):
                        yield response
                except (asyncio.CancelledError, GeneratorExit):
                    scope.finish(grpc.StatusCode.CANCELLED)
                    raise
                except BaseException:
                    scope.finish(_final_code(context, failed=True))
                    raise
                scope.finish(_final_code(context, failed=False))

            stream_behavior = stream_gen
        else:

            async def stream_coro(request: Any, context: Any) -> None:
                scope = scope_for(method, parent, attempt)
                try:
                    await stream_inner(request, context)
                except asyncio.CancelledError:
                    scope.finish(grpc.StatusCode.CANCELLED)
                    raise
                except BaseException:
                    scope.finish(_final_code(context, failed=True))
                    raise
                scope.finish(_final_code(context, failed=False))

            stream_behavior = stream_coro

        if handler.request_streaming:
            return grpc.stream_stream_rpc_method_handler(stream_behavior, **kwargs)
        return grpc.unary_stream_rpc_method_handler(stream_behavior, **kwargs)


def metadata_values_pairs(
    metadata: Iterable[tuple[str, str | bytes]],
) -> list[tuple[str, str]]:
    """Text-valued metadata pairs, suitable as an OpenTelemetry propagation carrier."""
    return [(k, v) for k, v in metadata if isinstance(v, str)]


# ---------------------------------------------------------------------------
# Client interceptors
# ---------------------------------------------------------------------------

TokenProvider = Callable[[], Awaitable[str]]


def with_metadata(
    details: grpc.aio.ClientCallDetails,
    extra: Sequence[tuple[str, str]],
    *,
    replace: frozenset[str] = frozenset(),
) -> grpc.aio.ClientCallDetails:
    """Copy `details` with `extra` metadata added; keys in `replace` are dropped first."""
    kept = [(k, v) for k, v in (details.metadata or ()) if k not in replace]
    return grpc.aio.ClientCallDetails(
        details.method,
        details.timeout,
        grpc.aio.Metadata(*kept, *extra),
        details.credentials,
        details.wait_for_ready,
    )


class _BearerBase:
    """Shared token logic; each call arity needs its own registered class.

    Runs once per logical call: a retry attempt replays this metadata unchanged.
    """

    def __init__(self, token_provider: TokenProvider) -> None:
        self._token_provider = token_provider

    async def _authorize(self, details: grpc.aio.ClientCallDetails) -> grpc.aio.ClientCallDetails:
        token = await self._token_provider()
        return with_metadata(
            details, [("authorization", f"Bearer {token}")], replace=frozenset({"authorization"})
        )


class BearerUnaryUnaryInterceptor(_BearerBase, grpc.aio.UnaryUnaryClientInterceptor):  # type: ignore[misc]
    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        return await continuation(await self._authorize(client_call_details), request)


class BearerUnaryStreamInterceptor(_BearerBase, grpc.aio.UnaryStreamClientInterceptor):  # type: ignore[misc]
    async def intercept_unary_stream(
        self, continuation: Any, client_call_details: Any, request: Any
    ) -> Any:
        return await continuation(await self._authorize(client_call_details), request)


class BearerStreamUnaryInterceptor(_BearerBase, grpc.aio.StreamUnaryClientInterceptor):  # type: ignore[misc]
    async def intercept_stream_unary(
        self, continuation: Any, client_call_details: Any, request_iterator: Any
    ) -> Any:
        return await continuation(await self._authorize(client_call_details), request_iterator)


class BearerStreamStreamInterceptor(_BearerBase, grpc.aio.StreamStreamClientInterceptor):  # type: ignore[misc]
    async def intercept_stream_stream(
        self, continuation: Any, client_call_details: Any, request_iterator: Any
    ) -> Any:
        return await continuation(await self._authorize(client_call_details), request_iterator)


def bearer_interceptors(token_provider: TokenProvider) -> list[Any]:
    """One interceptor per call arity.

    `grpc.aio.Channel.__init__` registers an interceptor under the FIRST interface it implements
    (an `if/elif` ladder), so a single class implementing several would be silently skipped for
    every other arity.
    """
    return [
        BearerUnaryUnaryInterceptor(token_provider),
        BearerUnaryStreamInterceptor(token_provider),
        BearerStreamUnaryInterceptor(token_provider),
        BearerStreamStreamInterceptor(token_provider),
    ]


class ClientTracingInterceptor(grpc.aio.UnaryUnaryClientInterceptor):  # type: ignore[misc]
    """One CLIENT span per logical unary call, covering every retry attempt beneath it."""

    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer
        self._propagator = TraceContextTextMapPropagator()

    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: grpc.aio.ClientCallDetails, request: Any
    ) -> Any:
        raw: str | bytes = client_call_details.method
        method = _rpc_method(raw.decode() if isinstance(raw, bytes) else raw)
        with self._tracer.start_as_current_span(
            method,
            kind=SpanKind.CLIENT,
            attributes={"rpc.system.name": "grpc", "rpc.method": method},
        ) as span:
            carrier: dict[str, str] = {}
            self._propagator.inject(carrier)
            details = with_metadata(
                client_call_details, list(carrier.items()), replace=frozenset(carrier)
            )
            call = await continuation(details, request)
            try:
                await call
            except grpc.aio.AioRpcError as exc:
                span.set_attribute("rpc.status_code", exc.code().name)
                span.set_attribute("error.type", exc.code().name)
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("rpc.status_code", grpc.StatusCode.OK.name)
            return call
