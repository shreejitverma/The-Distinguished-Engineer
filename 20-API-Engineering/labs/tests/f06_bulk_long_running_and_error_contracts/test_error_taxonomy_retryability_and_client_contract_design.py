"""Lab: transient vs permanent error classes, Retry-After, and scrubbed problem responses."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from api_labs.f06_bulk_long_running_and_error_contracts.error_taxonomy import (
    INTERNAL_DETAIL,
    Aborted,
    AlreadyExists,
    DomainError,
    InvalidArgument,
    NotFound,
    ResourceExhausted,
    Retryability,
    Unavailable,
    classify,
    decide_retry,
    install_error_contract,
    parse_retry_after,
    render_problem,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from fastapi import Request, Response

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
VICTIM_EMAIL = "victim@example.com"
LEAK_MARKERS = ("[SQL", "INSERT", "accounts", "sqlite", "Traceback", 'File "', VICTIM_EMAIL)


class IdFactory:
    """Deterministic error ids so tests can join a response to its log record."""

    def __init__(self) -> None:
        self.issued: list[uuid.UUID] = []

    def __call__(self) -> uuid.UUID:
        self.issued.append(uuid.UUID(int=len(self.issued) + 1))
        return self.issued[-1]


def make_engine(db: Path, *, hide_parameters: bool = False) -> Engine:
    # timeout=0: a locked database fails immediately with SQLITE_BUSY instead of waiting 5 s.
    return create_engine(
        f"sqlite:///{db}",
        hide_parameters=hide_parameters,
        connect_args={"timeout": 0, "check_same_thread": False},
    )


def build_app(engine: Engine, logger: logging.Logger, ids: IdFactory) -> FastAPI:
    app = FastAPI()
    install_error_contract(app, logger, ids)

    @app.post("/accounts", status_code=201)
    def create_account(body: dict[str, str]) -> dict[str, str]:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO accounts (email) VALUES (:email)"), body)
        return {"email": body["email"]}

    @app.get("/report")
    def report() -> list[str]:
        with engine.connect() as conn:
            return [str(r) for r in conn.execute(text("SELECT * FROM missing_table"))]

    @app.get("/bug")
    def bug() -> dict[str, str]:
        config: dict[str, str] = {}
        return {"dsn": config["postgresql://admin:hunter2@db.internal:5432/prod"]}

    @app.post("/quota")
    def quota() -> None:
        raise ResourceExhausted("Write quota exceeded for this project.", retry_after_s=30)

    @app.post("/transfers/{tid}:commit")
    def commit(tid: str) -> None:
        raise Aborted(f"Transfer {tid} changed since it was read; re-read and retry.")

    return app


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "lab.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT UNIQUE)")
    return path


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("lab.errors")


@pytest.fixture
def ids() -> IdFactory:
    return IdFactory()


@pytest.fixture
def engine(db: Path) -> Iterator[Engine]:
    eng = make_engine(db)
    yield eng
    eng.dispose()


@pytest_asyncio.fixture
async def client(
    engine: Engine, logger: logging.Logger, ids: IdFactory
) -> AsyncIterator[httpx.AsyncClient]:
    # ServerErrorMiddleware always re-raises after sending the 500; do not let it fail the test.
    transport = httpx.ASGITransport(app=build_app(engine, logger, ids), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def assert_scrubbed(response: httpx.Response) -> None:
    for marker in LEAK_MARKERS:
        assert marker not in response.text, marker


def logged_text(caplog: pytest.LogCaptureFixture) -> str:
    # Other loggers may capture at INFO in a full-suite run, so take this lab's last record.
    record = next(r for r in reversed(caplog.records) if r.name == "lab.errors")
    return record.getMessage() + "\n" + logging.Formatter().formatException(record.exc_info)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "status", "retryability"),
    [
        (InvalidArgument("x"), 400, Retryability.PERMANENT),
        (NotFound("x"), 404, Retryability.PERMANENT),
        (AlreadyExists("x"), 409, Retryability.PERMANENT),
        (Aborted("x"), 409, Retryability.RETRY_TRANSACTION),
        (ResourceExhausted("x"), 429, Retryability.TRANSIENT),
        (Unavailable("x"), 503, Retryability.TRANSIENT),
        (KeyError("x"), 500, Retryability.INDETERMINATE),
    ],
)
def test_catalog_assigns_status_and_retryability(
    error: Exception, status: int, retryability: Retryability
) -> None:
    problem = render_problem(error, uuid.UUID(int=7))
    assert problem.status == status
    assert problem.body["status"] == status
    assert problem.body["retryability"] == retryability.value
    assert problem.body["instance"] == "urn:uuid:00000000-0000-0000-0000-000000000007"


@pytest.mark.unit
def test_retry_after_is_sent_only_for_transient_errors() -> None:
    assert render_problem(Unavailable("x", retry_after_s=2), uuid.uuid4()).headers == {
        "Retry-After": "2"
    }
    assert render_problem(Unavailable("x"), uuid.uuid4()).headers == {}
    # A permanent error must never invite a retry, even if a developer set a delay.
    assert render_problem(InvalidArgument("x", retry_after_s=5), uuid.uuid4()).headers == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_insert_is_409_permanent_and_leaks_no_sql(
    client: httpx.AsyncClient, ids: IdFactory, caplog: pytest.LogCaptureFixture
) -> None:
    first = await client.post("/accounts", json={"email": VICTIM_EMAIL})
    assert first.status_code == 201
    with caplog.at_level(logging.INFO, logger="lab.errors"):
        dup = await client.post("/accounts", json={"email": VICTIM_EMAIL})
    assert dup.status_code == 409
    assert dup.headers["content-type"] == "application/problem+json"
    assert dup.json()["type"] == "https://errors.example.com/already-exists"
    assert dup.json()["retryability"] == "permanent"
    assert "retry-after" not in dup.headers
    assert_scrubbed(dup)
    # The internals still exist, in the log, under the id the client was given.
    log = logged_text(caplog)
    assert dup.json()["instance"] == ids.issued[-1].urn
    assert f"error_id={ids.issued[-1]}" in log
    assert "[SQL: INSERT INTO accounts (email) VALUES (?)]" in log
    assert VICTIM_EMAIL in log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_locked_database_is_503_transient_and_the_retry_succeeds(
    client: httpx.AsyncClient, db: Path
) -> None:
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # take the RESERVED lock the INSERT needs
    busy = await client.post("/accounts", json={"email": "a@example.com"})
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "1"
    assert busy.json()["retryability"] == "transient"
    assert_scrubbed(busy)
    decision = decide_retry(
        method="POST",
        status=busy.status_code,
        headers=busy.headers,
        problem=busy.json(),
        idempotency_key_sent=False,
        attempt=1,
        max_attempts=3,
        now=NOW,
    )
    # TRANSIENT means "not applied", so even a POST without an Idempotency-Key may retry.
    assert decision.retry
    assert decision.delay_s == 1.0
    holder.execute("ROLLBACK")
    holder.close()
    retried = await client.post("/accounts", json={"email": "a@example.com"})
    assert retried.status_code == 201


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_driver_class_splits_into_transient_and_internal(
    client: httpx.AsyncClient, db: Path, engine: Engine
) -> None:
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    with pytest.raises(OperationalError) as busy, engine.begin() as conn:
        conn.execute(text("INSERT INTO accounts (email) VALUES ('b@example.com')"))
    holder.execute("ROLLBACK")
    holder.close()
    with pytest.raises(OperationalError) as missing, engine.connect() as conn:
        conn.execute(text("SELECT * FROM missing_table"))
    # Both are sqlite3.OperationalError: the class cannot tell a lock from a missing table.
    assert type(busy.value.orig) is type(missing.value.orig) is sqlite3.OperationalError
    assert isinstance(classify(busy.value), Unavailable)
    assert classify(missing.value) is None
    report = await client.get("/report")
    assert report.status_code == 500
    assert report.json()["retryability"] == "indeterminate"
    assert "missing_table" not in report.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_bug_is_opaque_500_and_still_reraised(
    engine: Engine, logger: logging.Logger, ids: IdFactory
) -> None:
    app = build_app(engine, logger, ids)
    quiet = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=quiet, base_url="http://test") as c:
        r = await c.get("/bug")
    assert r.status_code == 500
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["detail"] == INTERNAL_DETAIL
    assert "hunter2" not in r.text
    assert "KeyError" not in r.text
    loud = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=loud, base_url="http://test") as c:
        with pytest.raises(KeyError, match="hunter2"):
            await c.get("/bug")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_debug_mode_bypasses_the_installed_handler_and_leaks(
    logger: logging.Logger,
) -> None:
    app = FastAPI(debug=True)

    async def handler(request: Request, exc: Exception) -> Response:
        raise AssertionError("never called: debug_response runs first")

    app.add_exception_handler(Exception, handler)

    @app.get("/bug")
    def bug() -> None:
        raise RuntimeError("password=hunter2")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/bug")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("Traceback (most recent call last):")
    assert "password=hunter2" in r.text
    with pytest.raises(ValueError, match="debug=True"):
        install_error_contract(app, logger)


@pytest.mark.unit
def test_hide_parameters_hides_values_but_not_the_sql(db: Path) -> None:
    eng = make_engine(db, hide_parameters=True)
    stmt = text("INSERT INTO accounts (email) VALUES (:email)")
    with eng.begin() as conn:
        conn.execute(stmt, {"email": VICTIM_EMAIL})
    with pytest.raises(IntegrityError) as dup, eng.begin() as conn:
        conn.execute(stmt, {"email": VICTIM_EMAIL})
    eng.dispose()
    message = str(dup.value)
    assert VICTIM_EMAIL not in message
    assert "[SQL parameters hidden due to hide_parameters=True]" in message
    assert "[SQL: INSERT INTO accounts (email) VALUES (?)]" in message
    assert "UNIQUE constraint failed: accounts.email" in message


@pytest.mark.unit
def test_invalidated_connection_is_indeterminate_not_transient(db: Path) -> None:
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    other = sqlite3.connect(db, timeout=0, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError) as busy:
        other.execute("BEGIN IMMEDIATE")
    holder.close()
    other.close()
    assert busy.value.sqlite_errorname == "SQLITE_BUSY"
    healthy = DBAPIError("COMMIT", None, busy.value)
    dropped = DBAPIError("COMMIT", None, busy.value, connection_invalidated=True)
    assert isinstance(classify(healthy), Unavailable)
    assert classify(dropped) is None
    assert render_problem(dropped, uuid.uuid4()).body["retryability"] == "indeterminate"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (5, Unavailable),  # SQLITE_BUSY
        (261, Unavailable),  # SQLITE_BUSY_RECOVERY
        (517, Unavailable),  # SQLITE_BUSY_SNAPSHOT
        (773, Unavailable),  # SQLITE_BUSY_TIMEOUT
        (262, Unavailable),  # SQLITE_LOCKED_SHAREDCACHE
        (2067, AlreadyExists),  # SQLITE_CONSTRAINT_UNIQUE
        (1, type(None)),  # SQLITE_ERROR, e.g. "no such table"
    ],
)
def test_sqlite_extended_codes_classify_by_primary_code(code: int, expected: type[object]) -> None:
    err = sqlite3.OperationalError("driver message")
    err.sqlite_errorcode = code  # the attribute the sqlite3 module sets from the C API
    assert type(classify(DBAPIError("SELECT 1", None, err))) is expected


class FakeSerializationFailure(Exception):
    """Stands in for psycopg.errors.SerializationFailure, which carries the same attribute."""

    sqlstate = "40001"


@pytest.mark.unit
def test_postgres_serialization_failure_is_retry_transaction() -> None:
    wrapped = DBAPIError(
        "UPDATE t SET v = 1", None, FakeSerializationFailure("could not serialize")
    )
    public = classify(wrapped)
    assert isinstance(public, Aborted)
    assert public.spec.retryability is Retryability.RETRY_TRANSACTION


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quota_and_abort_round_trip_to_client_decisions(
    client: httpx.AsyncClient,
) -> None:
    quota = await client.post("/quota")
    assert (quota.status_code, quota.headers["retry-after"]) == (429, "30")
    aborted = await client.post("/transfers/t1:commit")
    assert aborted.status_code == 409
    common: dict[str, Any] = {
        "method": "POST",
        "idempotency_key_sent": True,
        "attempt": 1,
        "max_attempts": 3,
        "now": NOW,
    }
    q = decide_retry(status=429, headers=quota.headers, problem=quota.json(), **common)
    a = decide_retry(status=409, headers=aborted.headers, problem=aborted.json(), **common)
    assert (q.retry, q.delay_s) == (True, 30.0)
    assert (a.retry, a.reason) == (False, "restart the read-modify-write at the caller")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_domain_errors_are_handled_inside_and_not_reraised(
    engine: Engine, logger: logging.Logger, ids: IdFactory
) -> None:
    # The DomainError handler runs in ExceptionMiddleware, so ServerErrorMiddleware never sees
    # the exception and never re-raises it; the default transport would raise if it did.
    loud = httpx.ASGITransport(app=build_app(engine, logger, ids))
    async with httpx.AsyncClient(transport=loud, base_url="http://test") as c:
        quota = await c.post("/quota")
    assert quota.status_code == 429
    assert quota.json()["retryability"] == "transient"


@pytest.mark.unit
@pytest.mark.parametrize("hint", ["0", "Tue, 10 Nov 2026 11:00:00 GMT"])
def test_client_waits_for_the_longer_of_hint_and_backoff(hint: str) -> None:
    decision = decide_retry(
        method="GET",
        status=503,
        headers={"retry-after": hint},
        problem=None,
        idempotency_key_sent=False,
        attempt=3,
        max_attempts=4,
        now=NOW,
    )
    # A zero or past hint must not shorten the client's own backoff (0.1 x 2^2 = 0.4 s).
    assert (decision.retry, decision.delay_s) == (True, 0.4)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", 120.0),
        ("  7 ", 7.0),
        ("999999999", 3600.0),
        ("Tue, 10 Nov 2026 12:00:30 GMT", 30.0),
        ("Tuesday, 10-Nov-26 12:00:45 GMT", 45.0),
        ("Tue Nov 10 12:01:00 2026", 60.0),
        ("Tue, 10 Nov 2026 11:00:00 GMT", 0.0),
        ("-5", None),
        ("1.5", None),
        ("\u0661\u0662", None),
        ("soon", None),
        ("", None),
    ],
)
def test_parse_retry_after_accepts_all_rfc_9110_forms(value: str, expected: float | None) -> None:
    assert parse_retry_after(value, NOW, cap_s=3600.0) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "status", "problem", "keyed", "retry"),
    [
        ("POST", 500, None, False, False),
        ("POST", 500, None, True, True),
        ("PUT", 502, None, False, True),
        ("POST", 504, None, False, False),
        ("POST", 503, None, False, True),
        ("POST", 408, None, False, True),
        ("GET", 404, None, False, False),
        ("POST", 409, None, True, False),
        ("POST", 409, {"retryability": "retry_transaction"}, True, False),
        ("POST", 500, {"retryability": "transient"}, False, True),
        ("POST", 500, {"retryability": "maybe"}, False, False),
        ("GET", 400, {"retryability": "permanent"}, False, False),
    ],
)
def test_decide_retry_matrix(
    method: str, status: int, problem: dict[str, str] | None, keyed: bool, retry: bool
) -> None:
    decision = decide_retry(
        method=method,
        status=status,
        headers={},
        problem=problem,
        idempotency_key_sent=keyed,
        attempt=1,
        max_attempts=3,
        now=NOW,
    )
    assert decision.retry is retry, decision.reason


@pytest.mark.unit
def test_backoff_grows_and_the_budget_stops_retries() -> None:
    delays = [
        decide_retry(
            method="GET",
            status=503,
            headers={},
            problem=None,
            idempotency_key_sent=False,
            attempt=n,
            max_attempts=4,
            now=NOW,
        )
        for n in (1, 2, 3, 4)
    ]
    assert [d.delay_s for d in delays[:3]] == [0.1, 0.2, 0.4]
    assert delays[3].retry is False


@pytest.mark.unit
def test_domain_error_public_detail_is_the_only_detail() -> None:
    err: DomainError = NotFound("Account acct_42 was not found.")
    err.add_note("internal: shard=7 row_lock=held")
    body = render_problem(err, uuid.uuid4()).body
    assert body["detail"] == "Account acct_42 was not found."
    assert "shard" not in str(body)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_faults_log_at_error_and_client_faults_below_it(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="lab.errors"):
        bug = await client.get("/bug")
        quota = await client.post("/quota")
    assert (bug.status_code, quota.status_code) == (500, 429)
    levels = [record.levelno for record in caplog.records if record.name == "lab.errors"]
    assert levels == [logging.ERROR, logging.INFO]
