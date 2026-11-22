"""Per-request time accounting for ASGI apps: sys.monitoring task steps plus SQLAlchemy events.

A request's wall time is split into three disjoint parts:

- ``cpu_ns``: CPU time the loop thread spent running the request's coroutine steps
  (``time.thread_time_ns``).
- ``blocked_ns``: time inside a step that was not CPU (a blocking ``time.sleep``, blocking I/O,
  preemption by the kernel).
- ``wait_ns``: time the request's tasks were suspended (awaiting I/O, a timer, or the loop
  running other tasks).

Per-query SQL time is a second, overlapping view: it is wall time between the SQLAlchemy
``before_cursor_execute`` and ``after_cursor_execute`` events and normally sits inside ``wait_ns``.
"""

from __future__ import annotations

import contextvars
import inspect
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self, cast

from sqlalchemy import event

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import CodeType, TracebackType

    from sqlalchemy.engine import Connection, Engine, ExceptionContext
    from sqlalchemy.ext.asyncio import AsyncEngine
    from starlette.types import ASGIApp, Receive, Scope, Send

CURRENT_PROFILE: contextvars.ContextVar[RequestProfile | None] = contextvars.ContextVar(
    "current_request_profile", default=None
)

_COROUTINE_FLAGS = inspect.CO_COROUTINE | inspect.CO_ITERABLE_COROUTINE | inspect.CO_ASYNC_GENERATOR
# Tool ids 0-5 are shared by every monitoring tool in the interpreter.
# cProfile holds PROFILER_ID (2) while enabled, so the meter defaults to an unassigned id.
METER_TOOL_ID = 3
_STATEMENT_START_KEY = "request_profile_statement_start"
_MAX_FINGERPRINT = 200

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")
_PLACEHOLDER_LIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_WHITESPACE = re.compile(r"\s+")


def fingerprint(statement: str) -> str:
    """Collapse a SQL statement to a bounded-cardinality grouping key."""
    text = _STRING_LITERAL.sub("?", statement)
    text = _NUMBER_LITERAL.sub("?", text)
    text = _PLACEHOLDER_LIST.sub("(?+)", text)
    return _WHITESPACE.sub(" ", text).strip()[:_MAX_FINGERPRINT]


@dataclass(slots=True)
class QueryStat:
    """Aggregate of every execution of one statement fingerprint inside one request."""

    fingerprint: str
    count: int = 0
    total_ns: int = 0
    errors: int = 0


@dataclass(slots=True)
class RequestProfile:
    """Time accounting for one HTTP request."""

    route: str = ""
    wall_ns: int = 0
    run_ns: int = 0
    cpu_ns: int = 0
    process_cpu_ns: int = 0
    steps: int = 0
    queries: dict[str, QueryStat] = field(default_factory=dict[str, QueryStat])

    @property
    def wait_ns(self) -> int:
        """Wall time during which none of the request's tasks was running on the loop thread."""
        return max(self.wall_ns - self.run_ns, 0)

    @property
    def blocked_ns(self) -> int:
        """Time on the loop thread that was not CPU: blocking sleeps, blocking I/O, preemption."""
        return max(self.run_ns - self.cpu_ns, 0)

    @property
    def sql_ns(self) -> int:
        return sum(stat.total_ns for stat in self.queries.values())

    @property
    def sql_count(self) -> int:
        return sum(stat.count for stat in self.queries.values())

    def record_query(self, key: str, elapsed_ns: int, *, error: bool = False) -> None:
        stat = self.queries.setdefault(key, QueryStat(key))
        stat.count += 1
        stat.total_ns += elapsed_ns
        stat.errors += int(error)


class _ThreadState(threading.local):
    """Per-thread step state: coroutine nesting depth and the clocks read when it left zero."""

    depth: int = 0
    start_wall_ns: int = 0
    start_cpu_ns: int = 0


class TaskStepMeter:
    """Attribute loop-thread run and CPU time to the current request via ``sys.monitoring``.

    A task step is the stretch between the event loop resuming a task and the task's outermost
    coroutine yielding or returning.
    Nested ``await`` chains fire ``PY_START``/``PY_RESUME`` outermost first and
    ``PY_YIELD``/``PY_RETURN`` innermost first, so a depth counter finds the step edges.
    """

    def __init__(self, tool_id: int = METER_TOOL_ID) -> None:
        self._tool_id = tool_id
        self._state = _ThreadState()
        self._active = False

    def open_depth(self) -> int:
        """Coroutine nesting depth of the calling thread; non-zero means a step is open."""
        return self._state.depth

    def __enter__(self) -> Self:
        monitoring = sys.monitoring
        events = monitoring.events
        monitoring.use_tool_id(self._tool_id, "request-profile")
        self._active = True
        monitoring.register_callback(self._tool_id, events.PY_START, self._on_enter)
        monitoring.register_callback(self._tool_id, events.PY_RESUME, self._on_enter)
        monitoring.register_callback(self._tool_id, events.PY_THROW, self._on_throw)
        monitoring.register_callback(self._tool_id, events.PY_YIELD, self._on_exit)
        monitoring.register_callback(self._tool_id, events.PY_RETURN, self._on_exit)
        monitoring.register_callback(self._tool_id, events.PY_UNWIND, self._on_unwind)
        monitoring.set_events(
            self._tool_id,
            events.PY_START
            | events.PY_RESUME
            | events.PY_THROW
            | events.PY_YIELD
            | events.PY_RETURN
            | events.PY_UNWIND,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._active:
            return
        monitoring = sys.monitoring
        events = monitoring.events
        monitoring.set_events(self._tool_id, events.NO_EVENTS)
        for event_id in (
            events.PY_START,
            events.PY_RESUME,
            events.PY_THROW,
            events.PY_YIELD,
            events.PY_RETURN,
            events.PY_UNWIND,
        ):
            monitoring.register_callback(self._tool_id, event_id, None)
        monitoring.free_tool_id(self._tool_id)
        self._active = False

    def rebase(self) -> None:
        """Forget time already spent in the open step, so earlier code is not charged."""
        state = self._state
        if state.depth > 0:
            state.start_wall_ns = time.perf_counter_ns()
            state.start_cpu_ns = time.thread_time_ns()

    def flush(self, profile: RequestProfile) -> None:
        """Charge the still-open step to ``profile`` and restart its clocks."""
        state = self._state
        if state.depth > 0:
            self._charge(state, profile)
            state.start_wall_ns = time.perf_counter_ns()
            state.start_cpu_ns = time.thread_time_ns()

    @staticmethod
    def _charge(state: _ThreadState, profile: RequestProfile) -> None:
        profile.run_ns += time.perf_counter_ns() - state.start_wall_ns
        profile.cpu_ns += time.thread_time_ns() - state.start_cpu_ns
        profile.steps += 1

    def _enter(self) -> None:
        state = self._state
        if state.depth == 0:
            state.start_wall_ns = time.perf_counter_ns()
            state.start_cpu_ns = time.thread_time_ns()
        state.depth += 1

    def _exit(self) -> None:
        state = self._state
        if state.depth == 0:
            return
        state.depth -= 1
        if state.depth == 0:
            profile = CURRENT_PROFILE.get()
            if profile is not None:
                self._charge(state, profile)

    def _on_enter(self, code: CodeType, offset: int) -> object:
        if not code.co_flags & _COROUTINE_FLAGS:
            return sys.monitoring.DISABLE
        self._enter()
        return None

    def _on_exit(self, code: CodeType, offset: int, retval: object) -> object:
        if not code.co_flags & _COROUTINE_FLAGS:
            return sys.monitoring.DISABLE
        self._exit()
        return None

    def _on_throw(self, code: CodeType, offset: int, exc: BaseException) -> object:
        # PY_THROW and PY_UNWIND cannot return DISABLE before CPython 3.15: it raises ValueError.
        if code.co_flags & _COROUTINE_FLAGS:
            self._enter()
        return None

    def _on_unwind(self, code: CodeType, offset: int, exc: BaseException) -> object:
        if code.co_flags & _COROUTINE_FLAGS:
            self._exit()
        return None


class SqlAttribution:
    """Charge each SQL statement's wall time to the current request via SQLAlchemy events.

    The listeners attach to the synchronous engine underneath an ``AsyncEngine``.
    ``greenlet_spawn`` copies the awaiting task's context into the greenlet that runs them, so
    ``CURRENT_PROFILE`` is visible inside the callbacks.
    """

    def __init__(self, engine: Engine | AsyncEngine) -> None:
        self._engine = cast("Engine", getattr(engine, "sync_engine", engine))
        self._before = self._on_before
        self._after = self._on_after
        self._error = self._on_error

    def install(self) -> None:
        event.listen(self._engine, "before_cursor_execute", self._before)
        event.listen(self._engine, "after_cursor_execute", self._after)
        event.listen(self._engine, "handle_error", self._error)

    def uninstall(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before)
        event.remove(self._engine, "after_cursor_execute", self._after)
        event.remove(self._engine, "handle_error", self._error)

    def _on_before(
        self,
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        conn.info[_STATEMENT_START_KEY] = (time.perf_counter_ns(), statement)

    def _on_after(
        self,
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        self._finish(conn, error=False)

    def _on_error(self, exception_context: ExceptionContext) -> None:
        if exception_context.connection is not None:
            self._finish(exception_context.connection, error=True)

    @staticmethod
    def _finish(conn: Connection, *, error: bool) -> None:
        started: tuple[int, str] | None = conn.info.pop(_STATEMENT_START_KEY, None)
        profile = CURRENT_PROFILE.get()
        if started is None or profile is None:
            return
        start_ns, statement = started
        profile.record_query(fingerprint(statement), time.perf_counter_ns() - start_ns, error=error)


class ProfilingMiddleware:
    """Pure ASGI middleware that produces one ``RequestProfile`` per HTTP request."""

    def __init__(
        self, app: ASGIApp, meter: TaskStepMeter, sink: Callable[[RequestProfile], None]
    ) -> None:
        self._app = app
        self._meter = meter
        self._sink = sink

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        profile = RequestProfile()
        token = CURRENT_PROFILE.set(profile)
        self._meter.rebase()
        wall_start = time.perf_counter_ns()
        process_start = time.process_time_ns()
        try:
            await self._app(scope, receive, send)
        finally:
            self._meter.flush(profile)
            profile.wall_ns = time.perf_counter_ns() - wall_start
            profile.process_cpu_ns = time.process_time_ns() - process_start
            route: Any = scope.get("route")
            profile.route = getattr(route, "path", None) or "<unmatched>"
            CURRENT_PROFILE.reset(token)
            self._sink(profile)
