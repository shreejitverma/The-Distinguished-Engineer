"""Canonical log line ASGI middleware built on structlog 26.1.0.

One request produces exactly one summary event, emitted from a ``finally`` block
after the response has been fully sent (or the request has failed):

- ``RequestAccumulator`` is a mutable, lock-protected object stored in a single
  ``ContextVar``.
  Handlers mutate the object; they never ``set`` the variable.
  A copied ``Context`` (``anyio.to_thread``, ``asyncio.create_task``) shares the
  same object, so threadpool handlers and child tasks still reach the summary.
- ``CanonicalLogMiddleware`` is a pure ASGI 3 callable.
  It binds ``request_id`` and ``trace_id`` with ``structlog.contextvars`` so that
  every intermediate log line carries them, then emits the summary event.
- Emission is wrapped in its own ``try``/``except``: a failing log sink never
  fails the request, and ``emit_failures`` counts the loss.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Final, TextIO, cast

import structlog
from structlog.contextvars import bind_contextvars, merge_contextvars, reset_contextvars

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send
    from structlog.typing import FilteringBoundLogger

CANONICAL_EVENT: Final = "canonical-log-line"
FALLBACK_LOGGER: Final = logging.getLogger("api_labs.canonical_log_lines")

# Keys owned by the middleware or the accumulator; handlers cannot set them.
RESERVED_KEYS: Final = frozenset(
    {
        "event",
        "level",
        "timestamp",
        "request_id",
        "trace_id",
        "trace_sampled",
        "http_method",
        "http_path",
        "http_route",
        "http_status",
        "status_inferred",
        "outcome",
        "duration_ms",
        "ttfb_ms",
        "request_bytes",
        "response_bytes",
        "user_agent",
        "client_disconnected",
        "error_class",
        "error_message",
        "db_query_count",
        "db_duration_ms",
        "fields_dropped",
    }
)

_REQUEST_ID_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TRACEPARENT_RE: Final = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<parent_id>[0-9a-f]{16})"
    r"-(?P<flags>[0-9a-f]{2})(?P<rest>.*)",
    re.DOTALL,
)
_ZERO_TRACE_ID: Final = "0" * 32
_ZERO_PARENT_ID: Final = "0" * 16


def truncate(value: Any, limit: int) -> Any:
    """Bound a user-influenced string; other values pass through unchanged."""
    if isinstance(value, str) and len(value) > limit:
        return value[: max(limit - 3, 0)] + "..."
    return value


class RequestAccumulator:
    """Per-request mutable summary fields, safe to mutate from any thread or task."""

    def __init__(self, *, max_fields: int = 64, max_value_chars: int = 512) -> None:
        self._lock = threading.Lock()
        self._fields: dict[str, Any] = {"auth_user_id": None, "rate_limit_remaining": None}
        self._db_query_count = 0
        self._db_duration_ms = 0.0
        self._dropped = 0
        self._max_fields = max_fields
        self._max_value_chars = max_value_chars

    def set(self, **fields: Any) -> None:
        """Last write wins. Reserved keys and keys beyond the cap are counted, not stored."""
        with self._lock:
            for key, value in fields.items():
                over_cap = key not in self._fields and len(self._fields) >= self._max_fields
                if key in RESERVED_KEYS or over_cap:
                    self._dropped += 1
                else:
                    self._fields[key] = truncate(value, self._max_value_chars)

    def add_db_query(self, duration_ms: float) -> None:
        with self._lock:
            self._db_query_count += 1
            self._db_duration_ms += duration_ms

    def snapshot(self) -> dict[str, Any]:
        """Copy of everything accumulated so far, including the always-present counters."""
        with self._lock:
            return {
                **self._fields,
                "db_query_count": self._db_query_count,
                "db_duration_ms": round(self._db_duration_ms, 3),
                "fields_dropped": self._dropped,
            }


_CURRENT: contextvars.ContextVar[RequestAccumulator | None] = contextvars.ContextVar(
    "api_labs_canonical_line", default=None
)


def current_accumulator() -> RequestAccumulator | None:
    """The active request's accumulator, or ``None`` outside a request (background jobs)."""
    return _CURRENT.get()


def annotate(**fields: Any) -> None:
    """Add fields to the current request's canonical line; a no-op outside a request."""
    accumulator = _CURRENT.get()
    if accumulator is not None:
        accumulator.set(**fields)


@contextlib.contextmanager
def db_query(clock: Callable[[], float] = time.perf_counter) -> Generator[None]:
    """Count one database query and add its duration, even if the query raises."""
    started = clock()
    try:
        yield
    finally:
        accumulator = _CURRENT.get()
        if accumulator is not None:
            accumulator.add_db_query((clock() - started) * 1000.0)


def build_logger(sink: TextIO, *, min_level: int = logging.INFO) -> FilteringBoundLogger:
    """A structlog logger that writes one sorted-key JSON object per line to ``sink``."""
    logger = structlog.wrap_logger(
        structlog.WriteLogger(sink),
        processors=[
            merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
    )
    return cast("FilteringBoundLogger", logger)


def parse_traceparent(value: str) -> tuple[str, bool] | None:
    """Return ``(trace_id, sampled)`` from a W3C ``traceparent`` value, or ``None`` to restart.

    Version ``00`` must have exactly 55 characters.
    A higher version (not ``ff``) is parsed by its first 55 characters when anything
    longer starts with ``-`` (W3C Trace Context Section 3.2.4).
    """
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None or match["version"] == "ff":
        return None
    if match["trace_id"] == _ZERO_TRACE_ID or match["parent_id"] == _ZERO_PARENT_ID:
        return None
    rest = match["rest"]
    if match["version"] == "00":
        if rest:
            return None
    elif rest and not rest.startswith("-"):
        return None
    return match["trace_id"], bool(int(match["flags"], 16) & 1)


def _single_header(scope: Scope, name: bytes) -> str | None:
    values = [value for key, value in scope["headers"] if key == name]
    if len(values) != 1:
        return None
    decoded: str = values[0].decode("latin-1")
    return decoded


class _RequestState:
    """Facts observed on the ASGI channels while the request runs."""

    __slots__ = (
        "client_disconnected",
        "error",
        "request_bytes",
        "response_bytes",
        "response_complete",
        "status",
        "ttfb",
    )

    def __init__(self) -> None:
        self.status: int | None = None
        self.ttfb: float | None = None
        self.request_bytes = 0
        self.response_bytes = 0
        self.response_complete = False
        self.client_disconnected = False
        self.error: BaseException | None = None


class CanonicalLogMiddleware:
    """Emit one structured summary event per HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        logger: FilteringBoundLogger,
        clock: Callable[[], float] = time.perf_counter,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        max_user_agent_chars: int = 256,
    ) -> None:
        self.app = app
        self.logger = logger
        self.clock = clock
        self.id_factory = id_factory
        self.max_user_agent_chars = max_user_agent_chars
        self.emit_failures = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = self.clock()
        request_id = self._request_id(scope)
        trace_id, trace_sampled = self._trace_context(scope)
        accumulator = RequestAccumulator()
        state = _RequestState()
        accumulator_token = _CURRENT.set(accumulator)
        log_tokens = bind_contextvars(request_id=request_id, trace_id=trace_id)

        async def receive_wrapper() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                state.request_bytes += len(message.get("body", b""))
            elif message["type"] == "http.disconnect" and not state.response_complete:
                # Servers also report http.disconnect once the response is complete.
                state.client_disconnected = True
            return message

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and state.status is None:
                state.status = int(message["status"])
                state.ttfb = self.clock() - started
            try:
                await send(message)
            except OSError:
                # ASGI: send() on a closed connection raises an OSError subclass.
                state.client_disconnected = True
                raise
            if message["type"] == "http.response.body":
                state.response_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    state.response_complete = True

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except BaseException as exc:
            state.error = exc
            raise
        finally:
            self._emit(
                scope,
                accumulator,
                state,
                request_id=request_id,
                trace_id=trace_id,
                trace_sampled=trace_sampled,
                duration=self.clock() - started,
            )
            reset_contextvars(**log_tokens)
            _CURRENT.reset(accumulator_token)

    def _request_id(self, scope: Scope) -> str:
        inbound = _single_header(scope, b"x-request-id")
        if inbound is not None and _REQUEST_ID_RE.fullmatch(inbound):
            return inbound
        return self.id_factory()

    def _trace_context(self, scope: Scope) -> tuple[str, bool | None]:
        inbound = _single_header(scope, b"traceparent")
        parsed = parse_traceparent(inbound) if inbound is not None else None
        if parsed is not None:
            return parsed
        return uuid.uuid4().hex, None

    def _emit(
        self,
        scope: Scope,
        accumulator: RequestAccumulator,
        state: _RequestState,
        *,
        request_id: str,
        trace_id: str,
        trace_sampled: bool | None,
        duration: float,
    ) -> None:
        try:
            fields = self._build_fields(
                scope,
                accumulator,
                state,
                request_id=request_id,
                trace_id=trace_id,
                trace_sampled=trace_sampled,
                duration=duration,
            )
            if fields["http_status"] >= 500 or fields["outcome"] == "exception":
                self.logger.error(CANONICAL_EVENT, **fields)
            else:
                self.logger.info(CANONICAL_EVENT, **fields)
        except Exception:
            # Never fail the request because the summary could not be written.
            self.emit_failures += 1
            FALLBACK_LOGGER.error("canonical line emission failed", exc_info=True)

    def _build_fields(
        self,
        scope: Scope,
        accumulator: RequestAccumulator,
        state: _RequestState,
        *,
        request_id: str,
        trace_id: str,
        trace_sampled: bool | None,
        duration: float,
    ) -> dict[str, Any]:
        error = state.error
        status = state.status
        inferred = False
        if error is None:
            outcome = "client_disconnected" if state.client_disconnected else "completed"
            if status is None:
                # The server will answer 500 when the app returns without a response.
                status, inferred, outcome = 500, True, "no_response"
        elif isinstance(error, asyncio.CancelledError):
            outcome = "cancelled"
        elif state.client_disconnected and isinstance(error, OSError):
            outcome = "client_disconnected"
        else:
            outcome = "exception"
            if status is None:
                # An outer ServerErrorMiddleware answers 500; this layer never sees it.
                status, inferred = 500, True

        route = scope.get("route")
        user_agent = _single_header(scope, b"user-agent")
        fields: dict[str, Any] = accumulator.snapshot()
        fields.update(
            request_id=request_id,
            trace_id=trace_id,
            http_method=scope["method"],
            http_path=scope["path"],
            http_route=getattr(route, "path", None),
            http_status=status if status is not None else 0,
            status_inferred=inferred,
            outcome=outcome,
            duration_ms=round(duration * 1000.0, 3),
            ttfb_ms=None if state.ttfb is None else round(state.ttfb * 1000.0, 3),
            request_bytes=state.request_bytes,
            response_bytes=state.response_bytes,
            user_agent=truncate(user_agent, self.max_user_agent_chars),
            client_disconnected=state.client_disconnected,
        )
        if trace_sampled is not None:
            fields["trace_sampled"] = trace_sampled
        if error is not None and not isinstance(error, asyncio.CancelledError):
            fields["error_class"] = type(error).__name__
            fields["error_message"] = truncate(str(error), 512)
        return fields


def parse_lines(raw: str) -> list[Mapping[str, Any]]:
    """Parse JSON-lines output; used by tests and the worked example."""
    return [json.loads(line) for line in raw.splitlines() if line]
