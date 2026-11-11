"""Error taxonomy: retryability classes, a scrubbing exception mapper, and a client retry policy."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fastapi import FastAPI, Request

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://errors.example.com/"
INTERNAL_DETAIL = "An internal error occurred; quote the instance URI when contacting support."


class Retryability(StrEnum):
    """What a client may do with the same request bytes after this error."""

    PERMANENT = "permanent"  # fails again until the caller changes the request or the state
    TRANSIENT = "transient"  # server guarantees it was not applied: any method may retry
    RETRY_TRANSACTION = "retry_transaction"  # restart the whole read-modify-write, not this call
    INDETERMINATE = "indeterminate"  # outcome unknown: retry only if idempotent or keyed


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """One row of the public error catalog: stable code, status, title, retryability."""

    code: str
    status: int
    title: str
    retryability: Retryability


class DomainError(Exception):
    """An error whose public detail was written for the client, never derived from internals."""

    spec: ClassVar[ErrorSpec]

    def __init__(self, public_detail: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(public_detail)
        self.public_detail = public_detail
        self.retry_after_s = retry_after_s


class InvalidArgument(DomainError):
    spec = ErrorSpec("invalid-argument", 400, "Invalid argument", Retryability.PERMANENT)


class NotFound(DomainError):
    spec = ErrorSpec("not-found", 404, "Not found", Retryability.PERMANENT)


class AlreadyExists(DomainError):
    spec = ErrorSpec("already-exists", 409, "Already exists", Retryability.PERMANENT)


class Aborted(DomainError):
    spec = ErrorSpec("aborted", 409, "Concurrent modification", Retryability.RETRY_TRANSACTION)


class ResourceExhausted(DomainError):
    spec = ErrorSpec("resource-exhausted", 429, "Too many requests", Retryability.TRANSIENT)


class Unavailable(DomainError):
    spec = ErrorSpec("unavailable", 503, "Service unavailable", Retryability.TRANSIENT)


INTERNAL = ErrorSpec("internal", 500, "Internal error", Retryability.INDETERMINATE)

# SQLite primary result codes are the low 8 bits of the extended code (sqlite.org/rescode.html).
_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6
_SQLITE_CONSTRAINT_UNIQUE = 2067
# PostgreSQL SQLSTATEs that PostgreSQL 18 docs section 13.5 says to retry as a whole transaction.
_PG_RETRY_TRANSACTION = frozenset({"40001", "40P01"})


def classify_driver_error(orig: BaseException) -> DomainError | None:
    """Map a DB-API driver exception to a public error by its code, never by its class."""
    if isinstance(orig, sqlite3.Error):
        code = getattr(orig, "sqlite_errorcode", None)
        if code is None:
            return None
        if code & 0xFF in (_SQLITE_BUSY, _SQLITE_LOCKED):
            return Unavailable("The resource is busy; retry later.", retry_after_s=1)
        if code == _SQLITE_CONSTRAINT_UNIQUE:
            return AlreadyExists("A resource with these unique fields already exists.")
        return None
    sqlstate = getattr(orig, "sqlstate", None)  # psycopg 3 and asyncpg both expose this
    if isinstance(sqlstate, str) and sqlstate in _PG_RETRY_TRANSACTION:
        return Aborted("The transaction conflicted with a concurrent one; restart it.")
    return None


def classify(exc: BaseException) -> DomainError | None:
    """Return the public error for exc, or None when it is an internal failure."""
    if isinstance(exc, DomainError):
        return exc
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated:
            # The connection dropped mid-statement: the commit may or may not have happened.
            return None
        return None if exc.orig is None else classify_driver_error(exc.orig)
    return None


@dataclass(frozen=True, slots=True)
class Problem:
    """A fully rendered RFC 9457 response: status, headers, and JSON body."""

    status: int
    headers: dict[str, str]
    body: dict[str, Any]


def render_problem(exc: BaseException, error_id: uuid.UUID) -> Problem:
    """Build the response from the catalog only; str(exc) of a non-domain error never leaks."""
    public = classify(exc)
    spec = INTERNAL if public is None else public.spec
    detail = INTERNAL_DETAIL if public is None else public.public_detail
    headers: dict[str, str] = {}
    retry_after = None if public is None else public.retry_after_s
    if retry_after is not None and spec.retryability is Retryability.TRANSIENT:
        headers["Retry-After"] = str(retry_after)
    body: dict[str, Any] = {
        "type": TYPE_BASE + spec.code,
        "title": spec.title,
        "status": spec.status,
        "detail": detail,
        "instance": error_id.urn,
        "retryability": spec.retryability.value,
    }
    return Problem(spec.status, headers, body)


def install_error_contract(
    app: FastAPI,
    logger: logging.Logger,
    new_error_id: Callable[[], uuid.UUID] = uuid.uuid4,
) -> None:
    """Register one handler for every exception; refuse apps whose debug page would bypass it."""
    if app.debug:
        raise ValueError("debug=True makes ServerErrorMiddleware return tracebacks to clients")

    async def handle(request: Request, exc: Exception) -> JSONResponse:
        error_id = new_error_id()
        problem = render_problem(exc, error_id)
        level = logging.ERROR if problem.status >= 500 else logging.INFO
        # Internals go to the log, keyed by the same id the client sees in `instance`.
        logger.log(
            level,
            "error_id=%s %s %s -> %d",
            error_id,
            request.method,
            request.url.path,
            problem.status,
            exc_info=exc,
        )
        return JSONResponse(
            problem.body, problem.status, headers=problem.headers, media_type=PROBLEM_JSON
        )

    app.add_exception_handler(Exception, handle)  # goes to ServerErrorMiddleware
    app.add_exception_handler(DomainError, handle)  # goes to ExceptionMiddleware


def parse_retry_after(value: str, now: datetime, cap_s: float) -> float | None:
    """Parse delay-seconds or any of the three HTTP-date forms, clamped to [0, cap_s]."""
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(value), cap_s)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:  # asctime form carries no zone; RFC 9110 says it is UTC
        when = when.replace(tzinfo=UTC)
    return min(max((when - now).total_seconds(), 0.0), cap_s)


IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})
_FALLBACK_TRANSIENT = frozenset({408, 429, 503})


def response_retryability(status: int, problem: Mapping[str, Any] | None) -> Retryability:
    """Trust the server's explicit class; otherwise fall back to the status code alone."""
    claimed = None if problem is None else problem.get("retryability")
    # An unknown or absent extension is ignored, as RFC 9457 Section 3.2 requires.
    if isinstance(claimed, str) and claimed in Retryability:
        return Retryability(claimed)
    if status in _FALLBACK_TRANSIENT:
        return Retryability.TRANSIENT
    if status >= 500:
        return Retryability.INDETERMINATE
    return Retryability.PERMANENT


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    delay_s: float
    reason: str


def decide_retry(
    *,
    method: str,
    status: int,
    headers: Mapping[str, str],
    problem: Mapping[str, Any] | None,
    idempotency_key_sent: bool,
    attempt: int,
    max_attempts: int,
    now: datetime,
    base_delay_s: float = 0.1,
    cap_s: float = 60.0,
) -> RetryDecision:
    """Client policy: retryability x method idempotency x attempt budget -> retry or surface."""
    if attempt >= max_attempts:
        return RetryDecision(False, 0.0, "attempt budget exhausted")
    kind = response_retryability(status, problem)
    if kind is Retryability.PERMANENT:
        return RetryDecision(False, 0.0, "permanent: change the request or the state")
    if kind is Retryability.RETRY_TRANSACTION:
        return RetryDecision(False, 0.0, "restart the read-modify-write at the caller")
    safe_to_repeat = method.upper() in IDEMPOTENT_METHODS or idempotency_key_sent
    if kind is Retryability.INDETERMINATE and not safe_to_repeat:
        return RetryDecision(False, 0.0, "indeterminate outcome on a non-idempotent request")
    backoff = min(base_delay_s * 2 ** (attempt - 1), cap_s)
    raw = headers.get("retry-after")
    hinted = None if raw is None else parse_retry_after(raw, now, cap_s)
    return RetryDecision(True, backoff if hinted is None else max(hinted, backoff), kind.value)
