---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc9457.html"
  - "https://www.rfc-editor.org/rfc/rfc6585.html"
  - "https://google.aip.dev/193"
  - "https://google.aip.dev/194"
  - "https://grpc.github.io/grpc/core/md_doc_statuscodes.html"
  - "https://www.postgresql.org/docs/current/errcodes-appendix.html"
  - "https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html"
  - "https://www.sqlite.org/rescode.html"
  - "https://peps.python.org/pep-0249/"
  - "https://docs.python.org/3/library/sqlite3.html"
  - "https://docs.python.org/3/library/email.utils.html"
  - "https://docs.python.org/3/library/enum.html"
  - "https://raw.githubusercontent.com/encode/starlette/1.10.0/starlette/middleware/errors.py"
  - "https://raw.githubusercontent.com/encode/starlette/1.10.0/starlette/applications.py"
  - "https://raw.githubusercontent.com/encode/starlette/1.10.0/starlette/_exception_handler.py"
  - "https://raw.githubusercontent.com/encode/starlette/1.10.0/starlette/exceptions.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.162.0/fastapi/applications.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.162.0/fastapi/exception_handlers.py"
  - "https://raw.githubusercontent.com/sqlalchemy/sqlalchemy/rel_2_1_8/lib/sqlalchemy/exc.py"
  - "https://raw.githubusercontent.com/sqlalchemy/sqlalchemy/rel_2_1_8/lib/sqlalchemy/engine/create.py"
  - "https://raw.githubusercontent.com/encode/httpx/0.28.1/httpx/_transports/asgi.py"
  - "https://raw.githubusercontent.com/stripe/stripe-python/v16.1.0/stripe/_http_client.py"
  - "https://owasp.org/API-Security/editions/2023/en/0xa8-security-misconfiguration/"
  - "https://cwe.mitre.org/data/definitions/209.html"
  - "https://cwe.mitre.org/data/definitions/532.html"
  - "https://nvd.nist.gov/vuln/detail/CVE-2007-5172"
  - "https://nvd.nist.gov/vuln/detail/CVE-2021-3129"
  - "https://nvd.nist.gov/vuln/detail/CVE-2024-34069"
  - "https://raw.githubusercontent.com/stripe/stripe-python/v16.1.0/stripe/_api_requestor.py"
  - "https://raw.githubusercontent.com/Kludex/uvicorn/0.54.0/uvicorn/protocols/http/httptools_impl.py"
  - "https://raw.githubusercontent.com/pgjones/hypercorn/0.18.0/src/hypercorn/asyncio/task_group.py"
  - "https://raw.githubusercontent.com/emmett-framework/granian/v2.8.7/src/asgi/callbacks.rs"
  - "https://raw.githubusercontent.com/emmett-framework/granian/v2.8.7/src/utils.rs"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9457.txt"
  - "https://www.rfc-editor.org/rfc/rfc6585.txt"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2007-5172"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2021-3129"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2024-34069"
---

# Designing an Error Taxonomy: Separating Transient vs Permanent Failures and Preventing Information Disclosure

> [!summary] Claim
> An error contract is two promises in one response: what the client may do next (never retry, retry this call, restart the transaction, or retry only if the request was idempotent), and what it will never learn (SQL, stack frames, parameter values), so build the response from a closed catalog keyed by driver error codes, put the internals in the log under the same id the client sees, and let the client's retry decision depend on retryability x method idempotency x attempt budget rather than on the status code alone.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: classify every exception into one of four retryability classes (`permanent`, `transient`, `retry_transaction`, `indeterminate`) by the driver's error code, not its Python class, then render an RFC 9457 problem from a catalog row; `str(exc)` of a non-domain exception never reaches the wire.
- Contract rule: `transient` is a server promise that the request was not applied, so any method may retry it after `Retry-After` `(RFC 9110 Section 10.2.3)`; `indeterminate` (a `500`, a dropped connection, a gateway timeout) may be retried only for idempotent methods or a request carrying an idempotency key `(RFC 9110 Section 9.2.2)`.
- Trap: `sqlite3.OperationalError` is raised both for `database is locked` (`SQLITE_BUSY`, transient) and `no such table` (`SQLITE_ERROR`, a bug), and SQLAlchemy `2.1.8` puts the SQL text, the parameters, and the `table.column` in `str()` of the wrapper; `hide_parameters=True` removes only the parameters (all lab-proven).
- Portability: the lab passes 54 tests on CPython 3.12.13, 3.13.18, 3.14.10, 3.15.2, and the 3.14.6 free-threaded build; Starlette `1.10.0` checks `debug` before any installed `Exception` handler, so `FastAPI(debug=True)` returns a plain-text traceback even with a handler installed.
- When not to use it: do not expose a retryability extension on a public API you cannot keep truthful; if the server cannot guarantee "not applied", say `indeterminate`, and if the API is gRPC, use the status codes and AIP-194 rules directly instead of re-inventing them.

## Mental model

```mermaid
flowchart TD
    E[Exception raised in handler] --> D{DomainError?}
    D -- yes --> R[Catalog row:<br/>code, status, title, retryability]
    D -- no --> B{SQLAlchemy DBAPIError?}
    B -- no --> I[INTERNAL 500<br/>indeterminate]
    B -- yes --> C{connection_invalidated?}
    C -- yes --> I
    C -- no --> K{driver code}
    K -- "SQLITE_BUSY / SQLITE_LOCKED<br/>(code and 0xFF in 5, 6)" --> U[Unavailable 503<br/>transient + Retry-After]
    K -- "SQLITE_CONSTRAINT_UNIQUE 2067" --> A[AlreadyExists 409<br/>permanent]
    K -- "SQLSTATE 40001 / 40P01" --> T[Aborted 409<br/>retry_transaction]
    K -- other --> I
    R --> P[render_problem:<br/>problem+json from catalog only]
    U --> P
    A --> P
    T --> P
    I --> P
    P --> L[log: error_id + exc_info<br/>SQL, params, frames]
    P --> W[wire: type, title, status,<br/>detail, instance=urn:uuid, retryability]
    W --> CL{client decide_retry}
    CL -- permanent --> S1[surface to caller]
    CL -- retry_transaction --> S2[restart read-modify-write]
    CL -- "indeterminate and not<br/>idempotent or keyed" --> S1
    CL -- "transient, or indeterminate<br/>and idempotent or keyed" --> RT[sleep max of Retry-After, backoff<br/>then retry within budget]
```

Read the diagram as two halves joined by one wire message.
On the server, an exception flows down a classifier whose only inputs are the exception's type for domain errors and the driver's numeric code for database errors; anything the classifier does not recognize falls to the `INTERNAL` row, which is `indeterminate` because the server does not know how far the request got.
The renderer takes only the catalog row, the author-written public detail, and a fresh error id, so nothing on the wire is derived from `str(exc)` of an infrastructure exception.
The same id goes to two sinks: the log line, which carries the full traceback, SQL, and parameters, and the `instance` member, which the client quotes to support.
On the client, `decide_retry` combines the server's retryability claim (or a status-code fallback when the claim is absent) with the method's idempotency, the presence of an idempotency key, and an attempt budget.

## How it works

### Four retryability classes, not two

"Transient vs permanent" is the right intuition and the wrong data model, because the question a client must answer is "is repeating these exact bytes safe and useful", and that has four answers.

| Class | Server promise | Client action | HTTP examples in the lab | gRPC analogue |
|---|---|---|---|---|
| `permanent` | the same request fails again until the request or the state changes | surface | `400`, `404`, `409` already exists | `INVALID_ARGUMENT`, `NOT_FOUND`, `ALREADY_EXISTS`, `FAILED_PRECONDITION` |
| `transient` | the request was not applied and is expected to succeed later | retry any method after the hint | `429`, `503` | `UNAVAILABLE` |
| `retry_transaction` | a concurrency check failed; replaying this call alone is wrong | restart the read-modify-write | `409` aborted | `ABORTED` |
| `indeterminate` | the outcome is unknown; it may have been applied | retry only if idempotent or keyed | `500`, `502`, `504` | `UNKNOWN`, `INTERNAL`, `DEADLINE_EXCEEDED` |

The gRPC status code guide states the split between the first three directly, in its `FAILED_PRECONDITION` entry (gRPC Core status codes):
- "Use UNAVAILABLE if the client can retry just the failing call";
- "Use ABORTED if the client should retry at a higher level (e.g., when a client-specified test-and-set fails, indicating the client should restart a read-modify-write sequence)";
- "Use FAILED_PRECONDITION if the client should not retry until the system state has been explicitly fixed".
The same guide makes the fourth class necessary: `DEADLINE_EXCEEDED` "may be returned even if the operation has completed successfully" for operations that change state, and `UNAVAILABLE` carries the caveat "it is not always safe to retry non-idempotent operations" (gRPC Core status codes, codes 4 and 14).
AIP-194 is stricter than this table: clients should automatically retry only `UNAVAILABLE`, should not automatically retry transactional requests but "retry the entire transaction block from the start", and treat `RESOURCE_EXHAUSTED` as generally non-retryable because quota "may not be expected to work for several hours" (AIP-194, Guidance and Retryable codes).
The lab deliberately marks `429` as `transient` only because its server sends a bounded `Retry-After` and the client caps every delay; a quota measured in hours belongs in the `permanent` row.
The gRPC guide also says "there is no fixed list of status codes on which it is appropriate to retry in all applications", because the library itself generates the same code for different causes (gRPC Core status codes, closing paragraph); that is the argument for an explicit server claim instead of a client-side status table.

### Why `transient` must mean "not applied"

RFC 9110 lets a client repeat an idempotent request automatically after a communication failure, and says a client "SHOULD NOT automatically retry a request with a non-idempotent method unless it has some means to know that the request semantics are actually idempotent, regardless of the method, or some means to detect that the original request was never applied" `(RFC 9110 Section 9.2.2)`.
A `transient` claim is that "means to detect": the server says the request was never applied, so a `POST` without an idempotency key may retry.
The server can make that claim only when every side effect of the request lived in one database transaction that rolled back.
SQLite documents that `SQLITE_BUSY` "can occur at any point in a transaction: when the transaction is first started, during any write or update operations, or when the transaction commits" (sqlite.org rescode, `(5) SQLITE_BUSY`), so a busy error after an earlier statement in the same transaction is still "not applied" only because `engine.begin()` rolls the whole transaction back; a handler that sent an email or called a payment provider before the busy statement cannot honestly say `transient`.
Everything else that is unknown is `indeterminate`, and its safe retry needs an idempotency key, covered in [[Idempotency-Key-Header-Specification-and-State-Machine-Draft-08]] and [[Idempotent-Request-Identification-and-Request-ID-Correlation-AIP-155]].

### Classify by driver code, never by exception class

PEP 249 defines `OperationalError` as errors "related to the database's operation and not necessarily under the control of the programmer, e.g. an unexpected disconnect occurs, the data source name is not found, a transaction could not be processed, a memory allocation error occurred" (PEP 249, Exceptions), which mixes transient and permanent causes in one class.
CPython's `sqlite3` raises `OperationalError` for both `database is locked` and `no such table: missing`, and since Python 3.11 every `sqlite3.Error` from the library carries `sqlite_errorcode` and `sqlite_errorname` (Python docs, `sqlite3.Error`); the lab observed `5 SQLITE_BUSY` and `1 SQLITE_ERROR` for those two messages, and `2067 SQLITE_CONSTRAINT_UNIQUE` for a duplicate key.
SQLite result codes are layered: "The least significant 8 bits of the result code define a broad category and are called the 'primary result code'" (sqlite.org rescode, Section 2), so `261 SQLITE_BUSY_RECOVERY`, `517 SQLITE_BUSY_SNAPSHOT`, and `773 SQLITE_BUSY_TIMEOUT` all reduce to `5` with `code & 0xFF`.
PostgreSQL's equivalent is the five-character SQLSTATE: class `40` is Transaction Rollback, with `40001 serialization_failure` and `40P01 deadlock_detected`, and class `23` is Integrity Constraint Violation, with `23505 unique_violation` (PostgreSQL 18 Appendix A).
PostgreSQL 18 says applications at Repeatable Read or Serializable "must be prepared to retry transactions that fail due to serialization errors", that "It is important to retry the complete transaction, including all logic that decides which SQL to issue", and that `23505` may be a disguised serialization failure but "might represent persistent error conditions rather than transient failures" (PostgreSQL 18 Section 13.5).
That is the `retry_transaction` row: the server cannot retry for the client, and a client that re-sends the same `UPDATE` bytes is retrying at the wrong level.
psycopg 3 exposes the code as `sqlstate` on its exception classes, and `psycopg.errors.SerializationFailure` subclasses `OperationalError` (psycopg 3.3.8 `psycopg/errors.py`, `SerializationFailure`), while asyncpg sets `sqlstate = '40P01'` on `DeadlockDetectedError` (asyncpg 0.32.0 `asyncpg/exceptions/__init__.py`, `DeadlockDetectedError`); both were read in the installed packages only.

SQLAlchemy wraps the driver exception: `DBAPIError.__init__` builds the message as `"(%s.%s) %s" % (orig.__class__.__module__, orig.__class__.__name__, text)` and stores `connection_invalidated` `(sqlalchemy 2.1.8 lib/sqlalchemy/exc.py, DBAPIError.__init__)`.
The lab treats `connection_invalidated=True` as `indeterminate` whatever the driver code says, because a connection lost mid-`COMMIT` leaves the commit outcome unknown.

### Where the leak lives: `StatementError._sql_message`

SQLAlchemy formats every statement error as the driver message, then `[SQL: <statement>]`, then either `[parameters: <repr>]` or `[SQL parameters hidden due to hide_parameters=True]`, then a documentation link `(sqlalchemy 2.1.8 lib/sqlalchemy/exc.py, StatementError._sql_message)`.
The `create_engine` docstring says `hide_parameters=True` keeps parameters out of INFO logging and out of "the string representation of StatementError objects" `(sqlalchemy 2.1.8 lib/sqlalchemy/engine/create.py, create_engine)`; it says nothing about the statement, and the lab shows the statement and the driver's `UNIQUE constraint failed: accounts.email` survive it.
So an error mapper that returns `str(exc)` in `detail` publishes the schema (table and column names) and, without `hide_parameters`, the offending value, which here is another user's email address: an account-enumeration and PII leak in one field.
RFC 9457 names this risk: problem details "are not a debugging tool for the underlying implementation", authors must consider "the risk of exposing attack vectors by exposing implementation internals through error messages" `(RFC 9457 Section 4)`, and generators are "encouraged to avoid making implementation details such as a stack dump available through the HTTP interface" `(RFC 9457 Section 5)`.
The `detail` member "ought to focus on helping the client correct the problem, rather than giving debugging information" `(RFC 9457 Section 3.1.4)`.
The lab's rule is an allowlist, not a scrubber: `detail` is either the author-written `public_detail` of a `DomainError` or a fixed internal sentence, so there is no regular expression to bypass.

### Starlette's two exception paths and the debug bypass

Starlette splits handlers at build time: a handler keyed by `500` or `Exception` becomes the `handler` of `ServerErrorMiddleware`, the outermost middleware, and every other key goes to `ExceptionMiddleware`, the innermost `(starlette 1.10.0 starlette/applications.py, Starlette.build_middleware_stack)`; FastAPI `0.162.0` duplicates that split and inserts `ExceptionTelemetryMiddleware` just inside `ServerErrorMiddleware` `(fastapi 0.162.0 fastapi/applications.py, FastAPI.build_middleware_stack)`.
`ExceptionMiddleware` finds a handler by walking `type(exc).__mro__` `(starlette 1.10.0 starlette/_exception_handler.py, _lookup_exception_handler)`, so one handler registered for `DomainError` covers every subclass.
`ServerErrorMiddleware.__call__` checks `if self.debug:` before `elif self.handler is None:` and the installed handler, so in debug mode it calls `debug_response`, which returns an HTML traceback when `Accept` contains `text/html` and otherwise `traceback.format_exception` as plain text `(starlette 1.10.0 starlette/middleware/errors.py, ServerErrorMiddleware.__call__, ServerErrorMiddleware.debug_response)`.
After sending any response it executes `raise exc` under the comment "This allows servers to log the error, or allows test clients to optionally raise the error within the test case" `(starlette 1.10.0 starlette/middleware/errors.py, ServerErrorMiddleware.__call__)`.
httpx `0.28.1` `ASGITransport` re-raises that exception unless constructed with `raise_app_exceptions=False` `(httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.__init__, ASGITransport.handle_async_request)`, which is why the lab's client fixture sets it and one test proves the `KeyError` still propagates with the default.
The three servers all catch that re-raise and log it again, so every `500` produces two log records: the lab's, keyed by `error_id`, and the server's, which carries the same traceback and the exception text but no `error_id`.
Uvicorn `0.54.0` logs `Exception in ASGI application` at `ERROR` with `exc_info` `(uvicorn 0.54.0 uvicorn/protocols/http/httptools_impl.py, RequestResponseCycle.run_asgi)`.
Hypercorn `0.18.0` logs `Error in ASGI Framework` through `config.log.exception` `(hypercorn 0.18.0 src/hypercorn/asyncio/task_group.py, _handle)`.
Granian `2.8.7` logs `Application callable raised an exception` followed by the formatted traceback `(granian 2.8.7 src/utils.rs, log_application_callable_exception)`.
Because that second record is outside the handler, scrubbing the response does not scrub the server log: a `KeyError` whose key is a DSN prints the DSN in both records, so the log pipeline needs the same access control as the application log.
The lab's `install_error_contract` refuses an app with `debug=True`, because no handler can win against that branch order.

### The wire contract

The lab emits `Content-Type: application/problem+json` with the five RFC 9457 members and one extension, `retryability`.
RFC 9457 says consumers "MUST ignore any such extensions that they don't recognize" `(RFC 9457 Section 3.2)`, and recommends extension names that start with a letter, use `ALPHA`, `DIGIT`, and `_`, and are "three characters or longer" `(RFC 9457 Section 4)`; `retryability` and its value `retry_transaction` satisfy both.
`instance` is `urn:uuid:<error id>`, a URI reference that "identifies the specific occurrence of the problem" `(RFC 9457 Section 3.1.5)`, and the same UUID prefixes the log line.
`Retry-After` is `HTTP-date / delay-seconds`, where `delay-seconds = 1*DIGIT` is "a non-negative decimal integer" `(RFC 9110 Section 10.2.3)`; a `503` "MAY send a Retry-After header field" `(RFC 9110 Section 15.6.4)`, and a `429` "MAY include a Retry-After header" and "MUST NOT be stored by a cache" `(RFC 6585 Section 4)`.
RFC 9457 lets problem type definitions "specify the use of the Retry-After response header" `(RFC 9457 Section 4)`; the lab sends it only for `transient` rows, so a developer who sets a delay on a permanent error cannot accidentally invite retries.
A recipient that parses an HTTP-date "MUST accept all three HTTP-date formats" (IMF-fixdate, RFC 850, and asctime) `(RFC 9110 Section 5.6.7)`.
CPython's `email.utils.parsedate_to_datetime` parsed all three forms in the lab, but the asctime form carries no zone and comes back as a naive `datetime` (observed on CPython 3.14.6; the Python docs for `email.utils.parsedate_to_datetime` document a naive result only for a `-0000` zone); subtracting an aware `now` from it raises `TypeError`, so `parse_retry_after` pins naive results to UTC, as RFC 9110 defines every HTTP-date as UTC `(RFC 9110 Section 5.6.7)`.

### What production clients actually do

stripe-python `v16.1.0` `_should_retry` returns the value of a `stripe-should-retry: true|false` response header when present ("The API may ask us not to retry ... or advise us to retry (eg; in cases of lock timeouts)"), and otherwise retries `409` and every status `>= 500` `(stripe-python v16.1.0 stripe/_http_client.py, HTTPClient._should_retry)`.
Its comment notes the server is expected to send `stripe-should-retry: false` on most `500`s "since our idempotency framework would typically replay it anyway", which is the `indeterminate`-plus-idempotency-key rule expressed as a header.
Its backoff, `_sleep_time_seconds`, takes only the retry count: `min(INITIAL_DELAY * 2 ** (num_retries - 1), MAX_DELAY)` with `INITIAL_DELAY = 0.5` and `MAX_DELAY = 5`, then `_add_jitter_time` scales it into `[d/2, d]`, then the result is floored at `INITIAL_DELAY`; the file contains no reference to `Retry-After` `(stripe-python v16.1.0 stripe/_http_client.py, HTTPClient._sleep_time_seconds, HTTPClient._add_jitter_time)`.
The design lesson is the same as the lab's: the server is the only party that knows whether a failed request was applied, so the contract should let it say so explicitly, and the client should fall back to status heuristics only when it does not.

## Numbers and limits

| Quantity | Value | Source |
|---|---|---|
| `Retry-After` grammar | `HTTP-date / delay-seconds`, `delay-seconds = 1*DIGIT` (no sign, no fraction) | `(RFC 9110 Section 10.2.3)` |
| HTTP-date formats a recipient must accept | 3 (IMF-fixdate, RFC 850, asctime) | `(RFC 9110 Section 5.6.7)` |
| RFC 9457 extension member name | starts with a letter, `ALPHA` / `DIGIT` / `_`, >= 3 characters (SHOULD) | `(RFC 9457 Section 4)` |
| gRPC codes in the taxonomy | `DEADLINE_EXCEEDED` 4, `NOT_FOUND` 5, `ALREADY_EXISTS` 6, `FAILED_PRECONDITION` 9, `ABORTED` 10, `UNAVAILABLE` 14 | gRPC Core status codes (as of 2026-11) |
| codes AIP-194 says to retry automatically | 1 (`UNAVAILABLE`) | AIP-194 (as of 2026-11) |
| PostgreSQL retry-the-transaction SQLSTATEs | `40001`, `40P01`; `23505` and `23P01` only with care | PostgreSQL 18 Section 13.5 (as of 2026-11) |
| SQLite busy family | primary `5`; extended `261`, `517`, `773`; locked `6`, `262` | sqlite.org rescode (as of 2026-11) |
| SQLite unique violation | `2067 SQLITE_CONSTRAINT_UNIQUE` | sqlite.org rescode; observed in lab |
| stripe-python retry backoff | `INITIAL_DELAY = 0.5` s, `MAX_DELAY = 5` s | stripe-python v16.1.0 `stripe/_http_client.py` |
| Lab client defaults | backoff `0.1 x 2^(attempt-1)` s, cap `60` s, `max(Retry-After, backoff)` | lab `decide_retry`, `test_client_waits_for_the_longer_of_hint_and_backoff` |
| Lab problem body sizes | `239` B (`409`), `233` B (`429`), `261` B (`500`) `Content-Length` | measured in process through `httpx.ASGITransport` against the lab app, CPython 3.14.6, macOS 27.2 arm64 (Darwin 27.2.0) |
| Lab runtime | 54 tests in 0.25 s (5 runs, 0.24 to 0.26 s) | measured, `uv run --frozen pytest <lab test> -q -p no:cacheprovider`, CPython 3.14.6, macOS 27.2 arm64 (Darwin 27.2.0) |

The body sizes matter for one reason: a problem body the size of a few hundred bytes leaves no room for a stack trace, so a sudden jump in `500` response size on a dashboard is a cheap detector for a leaked debug page.

## Portability

| Leg | Result | Evidence |
|---|---|---|
| CPython 3.12.13 | 54 passed | lab run, uv-managed interpreter; the matrix pin 3.12.16 is not installed here and was not run |
| CPython 3.13.18 | 54 passed | lab run, Homebrew interpreter |
| CPython 3.14.10 | 54 passed | lab run, Homebrew interpreter; the workspace `.venv` 3.14.6 also passes |
| CPython 3.15.2 | 54 passed | lab run, Homebrew interpreter |
| CPython 3.15.2t free-threaded | not run | no 3.15.2t interpreter here; `uv run --python 3.15t` provides 3.15.0b3t, where SQLAlchemy 2.1.8 segfaults (exit `139`) on a bare `SELECT 1` against `sqlite://` (also on 3.15.0b3 with the GIL; works on Homebrew 3.15.2), so the leg is blocked upstream of this lab |
| CPython 3.14.6t free-threaded, `PYTHON_GIL=0` | 54 passed | lab run with `--no-project` and the pinned FastAPI, Starlette, SQLAlchemy, httpx, and pytest releases; `sys._is_gil_enabled()` is `False` (extra leg, not in the pinned matrix) |
| Debian Trixie (glibc 2.41), Alpine v3.23 (musl) containers | 54 passed on each | lab run on Linux aarch64 via Docker (OrbStack) with the `ghcr.io/astral-sh/uv:python3.14-trixie-slim` and `ghcr.io/astral-sh/uv:python3.14-alpine` images, CPython 3.14.10, `uv run --no-project` with the pinned FastAPI, Starlette, SQLAlchemy, httpx, pytest, and pytest-asyncio releases and `PYTHONPATH=src`; the lab source was mounted read-only; other interpreters were not run in containers |
| `"x" in Retryability` for a non-member string | returns `False` on 3.12+; raised `TypeError` before 3.12 | Python docs `enum`, "Changed in version 3.12"; the matrix floor is 3.12 |
| `sqlite3.Error.sqlite_errorcode` | present on every leg | Python docs, "Added in version 3.11"; lab run on all legs above |
| Starlette debug branch before handler | `1.10.0` | source read at tag, lab test |
| SQLAlchemy `hide_parameters` keeps SQL text | `2.1.8` | source read at tag, lab test; the `2.0.57` compatibility group was not run |
| Uvicorn `0.54.0`, Hypercorn `0.18.0`, Granian `2.8.7` | all three log the re-raised exception a second time, without `error_id`, and still return the lab's `261` B `500` problem body | socket run on CPython 3.14.6, macOS 27.2 arm64 (Darwin 27.2.0): each server started from the workspace `.venv` with `uvicorn probe_app:app`, `hypercorn probe_app:app`, and `granian --interface asgi probe_app:app` on `127.0.0.1:8801`, then `curl -i http://127.0.0.1:8801/bug`; log lines `Exception in ASGI application`, `Error in ASGI Framework`, `Application callable raised an exception`; source read at each tag |
| HTTP/1.1, HTTP/2, HTTP/3 | `Retry-After` and the problem body are version-independent | "Unless specified otherwise, fields are defined for all versions of HTTP" `(RFC 9110 Section 5.1)`; not exercised on the wire |

## Worked example

The [implementation](../labs/src/api_labs/f06_bulk_long_running_and_error_contracts/error_taxonomy.py) holds the catalog, the classifier, the renderer, the FastAPI installer, and the client policy; the [test](../labs/tests/f06_bulk_long_running_and_error_contracts/test_error_taxonomy_retryability_and_client_contract_design.py) drives a FastAPI app over a real SQLite file through `httpx.ASGITransport`.

The classifier, verbatim from the implementation (lines 83 to 116):

```python
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
```

The renderer and installer, verbatim (lines 128 to 176):

```python
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
```

The client policy, verbatim (lines 217 to 244):

```python
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
```

One end-to-end test, verbatim from the test module: a second connection holds `BEGIN IMMEDIATE`, the `POST` fails with `SQLITE_BUSY`, the server answers `503` with `Retry-After: 1`, the client policy says retry even without an idempotency key, and the retry after the lock is released succeeds.

```python
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
```

Three responses captured from the lab app in process (CPython 3.14.6, macOS 27.2 arm64): a duplicate account, the quota route, and the `/bug` route whose `KeyError` message contains a database DSN with a password.

```text
409 {'content-length': '239', 'content-type': 'application/problem+json'} {"type":"https://errors.example.com/already-exists","title":"Already exists","status":409,"detail":"A resource with these unique fields already exists.","instance":"urn:uuid:00000000-0000-0000-0000-000000000001","retryability":"permanent"}
429 {'retry-after': '30', 'content-length': '233', 'content-type': 'application/problem+json'} {"type":"https://errors.example.com/resource-exhausted","title":"Too many requests","status":429,"detail":"Write quota exceeded for this project.","instance":"urn:uuid:00000000-0000-0000-0000-000000000002","retryability":"transient"}
500 {'content-length': '261', 'content-type': 'application/problem+json'} {"type":"https://errors.example.com/internal","title":"Internal error","status":500,"detail":"An internal error occurred; quote the instance URI when contacting support.","instance":"urn:uuid:00000000-0000-0000-0000-000000000003","retryability":"indeterminate"}
```

In the same run, the logger wrote `error_id=00000000-0000-0000-0000-000000000003 GET /bug -> 500` followed by the full traceback ending in `KeyError: 'postgresql://admin:hunter2@db.internal:5432/prod'`: the secret left the wire but landed in the log, which is the next problem (see Security and robustness).

The test run, from `labs/` on CPython 3.14.6, macOS 27.2 arm64:

```text
$ uv run --frozen pytest tests/f06_bulk_long_running_and_error_contracts/test_error_taxonomy_retryability_and_client_contract_design.py -q -p no:cacheprovider
......................................................                   [100%]
54 passed in 0.27s
```

## Pitfalls

> [!warning] Mapping database errors by exception class
> Trigger: `except OperationalError: return 503` (or the reverse, `500` for every `OperationalError`).
> Symptom: a missing table after a bad migration is advertised as transient, so every client retries a request that can never succeed, or a lock timeout pages the on-call as a bug.
> Fix: branch on `sqlite_errorcode & 0xFF`, on SQLSTATE class `40` versus `23`, and on `connection_invalidated`; the lab's `test_same_driver_class_splits_into_transient_and_internal` shows both errors are `sqlite3.OperationalError` and only the code separates them.

> [!warning] Returning `str(exc)` as `detail`
> Trigger: a catch-all handler that writes `{"detail": str(exc)}` for convenience.
> Symptom: SQLAlchemy's `[SQL: INSERT INTO accounts (email) VALUES (?)]`, the `[parameters: (...)]` block, and the driver's `accounts.email` reach the client; CWE-209 lists CVE-2007-5172, where triggering database errors revealed a password in the error message, and NVD records it as Quicksilver Forums before 1.4.1 revealing "the database password in the resulting error message".
> Fix: render from the catalog; `hide_parameters=True` is a defense-in-depth knob for logs, not a fix, because the statement and the column names survive it (lab `test_hide_parameters_hides_values_but_not_the_sql`).

> [!warning] Shipping with `debug=True`
> Trigger: `FastAPI(debug=True)` or `Starlette(debug=True)` left on in a deployed environment, often via an environment flag defaulting the wrong way.
> Symptom: every unhandled exception returns `Traceback (most recent call last):` with frame source lines, even though an `Exception` handler is installed, because `ServerErrorMiddleware` checks `debug` first (lab `test_debug_mode_bypasses_the_installed_handler_and_leaks`).
> Debug pages in other frameworks have been worse than disclosure: NVD records CVE-2021-3129 as remote code execution in Ignition "exploitable on sites using debug mode with Laravel before 8.4.2", and CVE-2024-34069 as Werkzeug's debugger allowing code execution on a developer's machine under some circumstances.
> Fix: refuse to install the error contract on a debug app, as `install_error_contract` does, and assert `app.debug is False` in a deployment smoke test.

> [!warning] Advertising `transient` after a side effect
> Trigger: a handler calls an external service, then fails on a database lock, and the mapper says `503` `transient`.
> Symptom: a client correctly retries a `POST` without an idempotency key, and the external effect (an email, a charge) happens twice.
> Fix: emit `transient` only when every side effect was inside the transaction that rolled back; otherwise map to `indeterminate` and require an idempotency key for retries.

> [!warning] Parsing `Retry-After` naively
> Trigger: `int(headers["Retry-After"])`, or `parsedate_to_datetime(v) - now` with an aware `now`.
> Symptom: the HTTP-date form raises `ValueError`; the asctime form returns a naive `datetime` and the subtraction raises `TypeError`; `str.isdigit` accepts non-ASCII digits such as the Arabic-Indic digits `U+0661 U+0662`, which `float` turns into `12.0`; a hostile or buggy `Retry-After: 999999999` parks a worker for 31 years.
> Fix: accept only ASCII `1*DIGIT` or a parseable HTTP-date, pin naive results to UTC, and clamp to `[0, cap]` (lab `test_parse_retry_after_accepts_all_rfc_9110_forms`, which fails when the UTC pin, the cap, or the ASCII check is removed).

> [!warning] Case-sensitive header lookups in the retry policy
> Trigger: passing a plain `dict` such as `{"Retry-After": "5"}` to `decide_retry`, which looks up `"retry-after"`.
> Symptom: the hint is silently ignored and the client falls back to its own backoff.
> Fix: field names are case-insensitive `(RFC 9110 Section 5.1)`, so pass `httpx.Headers` or another case-insensitive mapping, as the lab's end-to-end tests do.

## Security and robustness

- Threat model: an attacker who can trigger errors (a duplicate key, a malformed filter, a crafted id) reads the error body to learn the schema, the driver, file paths, and other users' data; OWASP API8:2023 Security Misconfiguration lists "Error messages include stack traces, or expose other sensitive information" as a vulnerability condition.
- Enumeration: a `409` "already exists" on sign-up tells the caller that an email is registered; that is a product decision, and when it is not acceptable the catalog row must be generic and the response shape identical for both outcomes (see [[HTTP-Status-Code-Taxonomy-and-Selection-Decision-Trees]] for the `404` versus `403` version of the same problem, and AIP-193, which requires checking permission before existence and answering `PERMISSION_DENIED` "regardless of whether or not it exists").
- Logs are the second sink: the lab's log line carries the full traceback including a DSN password, which is CWE-532 Insertion of Sensitive Information into Log File; redact at the logging boundary as in [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]], and enable `hide_parameters=True` so SQL parameters stay out of engine logs.
- Correlation without disclosure: the `instance` URN is a random UUID, so it reveals nothing about request volume or ordering; a sequential error id would leak traffic rates.
- Retry amplification: a `transient` class that every client honours can multiply load during an outage; the client must cap attempts (`max_attempts`), honour `Retry-After`, and add jitter, and the fleet needs a retry budget, see [[Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina]] and [[Retry-Budgets-and-Preventing-Cascading-Retry-Amplification-Storms]].
- Trust boundary for the claim: a client should treat `retryability` as advisory input from the server it called, not from intermediaries; RFC 9457 notes that generic HTTP software is "unlikely to know of or respect the status code conveyed in this member" and that the body and the status can disagree `(RFC 9457 Section 5)`, so the lab keys its status fallback on the HTTP status, not the body's `status`.
- Product information: RFC 9110 warns that `Server` and `Via` "often reveal information about the respective sender's software systems" `(RFC 9110 Section 17.12)`; the same reasoning applies to framework-default error bodies such as Starlette's `Internal Server Error` text, which the catch-all handler replaces.

## The principal's view

The decision is to make retryability a first-class, versioned part of the API contract owned by the server, instead of letting every client team derive it from status codes.
The server is the only party that knows whether a request was applied, so the catalog row, not the status code, carries that fact; the status code stays correct for generic HTTP software, and the `retryability` extension (or a header such as stripe-python's `stripe-should-retry`) carries the precise claim.
The organizational cost is governance: every new failure mode needs a catalog row, a reviewed public detail, and a retryability class that someone is accountable for keeping true, and a wrong `transient` claim is a duplicate-charge incident rather than a cosmetic bug.
The second decision is that the error catalog is closed: handlers raise catalog types, infrastructure exceptions are classified by code in one place, and anything unclassified becomes an opaque `500` with an id, which trades developer convenience in debugging for the guarantee that no internal string reaches the wire.
Choose differently when the transport already has the vocabulary: on gRPC, use the canonical codes and AIP-193 `ErrorInfo` with AIP-194 retry rules (see [[Google-AIP-193-Error-Model-and-Rich-Error-Details]]), and on an internal RPC mesh with a service-mesh retry policy, keep the classes but enforce them in one shared client library instead of per-team code.
For APIs whose mutations are all idempotent by design (declarative `PUT` of desired state), the `indeterminate` class collapses into `transient` and the taxonomy can be simpler.

## Interview questions

> [!question] L5: Why is "4xx is permanent, 5xx is transient" an unsafe retry rule?
>
> > [!success]- Answer
> > `500` means the outcome is unknown, so retrying a non-idempotent `POST` can apply it twice.
> > `408` and `429` are 4xx yet retryable, and `409` from a failed concurrency check needs a restart of the read-modify-write, not a resend.
> > RFC 9110 Section 9.2.2 allows automatic retry of non-idempotent methods only when the client knows the request was never applied.
> >
> > Follow-up: Which extra input lets a client retry a `POST` after a `500` safely?

> [!question] L5: What does `Retry-After` look like on the wire, and what must a parser accept?
>
> > [!success]- Answer
> > Either `delay-seconds`, a non-negative decimal integer such as `120`, or an HTTP-date such as `Fri, 31 Dec 1999 23:59:59 GMT` (RFC 9110 Section 10.2.3).
> > A recipient must accept all three HTTP-date formats, IMF-fixdate, RFC 850, and asctime (RFC 9110 Section 5.6.7).
> > A robust client also clamps the value to a cap and treats an unparseable value as absent.
> >
> > Follow-up: Why does CPython's `parsedate_to_datetime` force you to handle naive datetimes?

> [!question] L6: Predict the output: on SQLAlchemy 2.1.8 with `create_engine("sqlite:///x.db", hide_parameters=True)`, a duplicate insert of `victim@example.com` into a unique `accounts.email` column raises `IntegrityError`; which of the email, the SQL text, and the column name appear in `str(exc)`?
>
> > [!success]- Answer
> > The email does not appear; the message contains `[SQL parameters hidden due to hide_parameters=True]`.
> > The SQL text appears as `[SQL: INSERT INTO accounts (email) VALUES (?)]`.
> > The column name appears in the driver message `UNIQUE constraint failed: accounts.email`.
> > `StatementError._sql_message` only swaps the parameters block; the lab asserts all three facts.
> >
> > Follow-up: What is the right place for `hide_parameters` in a defense-in-depth plan, given it does not make `str(exc)` safe for clients?

> [!question] L6: Debug this API: a FastAPI service registers `@app.exception_handler(Exception)` returning a generic problem body, yet a penetration test receives a plain-text Python traceback on a `500`. What is the cause?
>
> > [!success]- Answer
> > The app runs with `debug=True`.
> > Starlette 1.10.0 puts `Exception` handlers on `ServerErrorMiddleware`, and its `__call__` checks `if self.debug` before it consults the installed handler, so `debug_response` wins.
> > Without `text/html` in `Accept`, `debug_response` returns `traceback.format_exception` as `text/plain`.
> > Fix the deployment flag and make the error-contract installer refuse debug apps.
> >
> > Follow-up: Why does the same exception still appear in the server's own logs after the custom handler responds?

> [!question] L6: Two errors are both `sqlite3.OperationalError`: `database is locked` and `no such table: missing`. How do you map them, and why?
>
> > [!success]- Answer
> > Use `sqlite_errorcode`, available since Python 3.11: `5 SQLITE_BUSY` is a lock conflict and maps to `503` `transient` with `Retry-After`, while `1 SQLITE_ERROR` is a bug and maps to an opaque `500` `indeterminate`.
> > Mask with `& 0xFF` so extended codes such as `517 SQLITE_BUSY_SNAPSHOT` fall in the busy family.
> > PEP 249's `OperationalError` deliberately mixes operational causes, so the class carries no retryability information.
> >
> > Follow-up: What is the PostgreSQL equivalent, and which SQLSTATE class means retry the transaction?

> [!question] L7+: Your payments API returns `503` `transient` whenever the database is busy. A postmortem shows duplicate charges. Where is the contract broken?
>
> > [!success]- Answer
> > `transient` promises the request was not applied, but the handler called the card network before the database write that hit the lock.
> > The client was entitled to resend the `POST` without an idempotency key, so the external side effect ran twice.
> > Either move all side effects behind the transaction or an outbox, or classify that path as `indeterminate` and require an idempotency key for retries.
> > Add a review rule: every catalog row with `transient` must name the transaction that guarantees "not applied".
> >
> > Follow-up: How would you test that guarantee automatically for each endpoint?

> [!question] L7+: AIP-194 retries only `UNAVAILABLE`, while stripe-python retries `409` and every `5xx`. Reconcile the two designs.
>
> > [!success]- Answer
> > AIP-194 assumes clients may send non-idempotent requests without a deduplication key, so only "the call never ran" codes are safe.
> > Stripe's client sets an `Idempotency-Key` on every `POST` by default `(stripe-python v16.1.0 stripe/_api_requestor.py, _generate_idempotency_key)` and its server replays the stored result, so retrying an indeterminate `5xx` is safe there, and the server can still veto with `stripe-should-retry: false`.
> > The real variable is whether the request is deduplicated server-side; the taxonomy's `indeterminate` class plus an idempotency-key flag expresses both policies in one function.
> >
> > Follow-up: What does Stripe's `409` retry rely on that a generic `409 Conflict` from another API does not provide?

> [!question] L7+: Should the retryability claim live in a status code, a header, or the problem body, and who owns it?
>
> > [!success]- Answer
> > The status code must stay correct for generic HTTP software, but it cannot express "not applied" versus "unknown" for a `500`.
> > A body extension such as `retryability` is versionable and documented with the problem type; a header such as `stripe-should-retry` is visible to clients that do not parse bodies.
> > Clients must ignore unknown extensions (RFC 9457 Section 3.2), so the claim can be added without breaking anyone, and clients fall back to status heuristics when it is absent.
> > The owning team is the service team, because only the server knows its transaction boundaries; the shared client library owns the decision function.
> >
> > Follow-up: How would you deprecate a wrong `transient` claim already relied on by clients?

> [!question] L6: Why does the lab mark a dropped connection during `COMMIT` as `indeterminate` even when the driver code says `SQLITE_BUSY`?
>
> > [!success]- Answer
> > `connection_invalidated=True` on SQLAlchemy's `DBAPIError` means the connection was lost, so the server cannot know whether the commit reached durable storage.
> > The driver code describes the last statement the client saw, not the commit outcome.
> > Calling it `transient` would let non-idempotent clients retry a write that may have committed.
> >
> > Follow-up: Which gRPC status code carries the same "may have completed" warning?

## Related

Prerequisites: [[Google-AIP-193-Error-Model-and-Rich-Error-Details]]

Next: [[Mapping-Pydantic-Validation-Errors-to-RFC-9457-Problem-Details]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[Problem-Details-for-HTTP-APIs-RFC-9457]], [[HTTP-Status-Code-Taxonomy-and-Selection-Decision-Trees]], [[HTTP-Method-Semantics-Safety-and-Idempotency-Invariants]], [[Exception-Handling-in-FastAPI-HTTPException-vs-RequestValidationError]], [[Idempotency-Key-Header-Specification-and-State-Machine-Draft-08]], [[Idempotent-Request-Identification-and-Request-ID-Correlation-AIP-155]], [[Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina]], [[Retry-Budgets-and-Preventing-Cascading-Retry-Amplification-Storms]], [[Standard-RateLimit-Headers-IETF-Draft-and-429-Retry-After-Semantics]], [[Transaction-Isolation-Levels-and-Concurrency-Anomalies-in-APIs]], [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]], [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]]

- Lab: `labs/tests/f06_bulk_long_running_and_error_contracts/test_error_taxonomy_retryability_and_client_contract_design.py`

## Further reading

- [RFC 9110 Section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2): the rule that non-idempotent requests may be retried only when the client can tell they were never applied.
- [RFC 9110 Section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3): the `Retry-After` grammar and its meaning on `503` and `3xx`.
- [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html): problem details, extension members, and the security considerations against exposing stack dumps.
- [RFC 6585 Section 4](https://www.rfc-editor.org/rfc/rfc6585.html#section-4): `429 Too Many Requests`, `Retry-After`, and the no-store rule.
- [gRPC status codes](https://grpc.github.io/grpc/core/md_doc_statuscodes.html): the `UNAVAILABLE` / `ABORTED` / `FAILED_PRECONDITION` decision rule and which codes the library itself generates.
- [AIP-194](https://google.aip.dev/194): Google's conservative automatic-retry list and the transaction-level retry rule.
- [PostgreSQL 18 Section 13.5](https://www.postgresql.org/docs/current/mvcc-serialization-failure-handling.html): why serialization failures must be retried as whole transactions.
- [SQLite result codes](https://www.sqlite.org/rescode.html): primary versus extended codes and the meaning of `SQLITE_BUSY`.
- [PEP 249](https://peps.python.org/pep-0249/): the DB-API exception hierarchy whose classes mix transient and permanent causes.
- [Starlette 1.10.0 `starlette/middleware/errors.py`](https://raw.githubusercontent.com/encode/starlette/1.10.0/starlette/middleware/errors.py): the debug-before-handler order and the unconditional re-raise.
- [Uvicorn 0.54.0 `httptools_impl.py`](https://raw.githubusercontent.com/Kludex/uvicorn/0.54.0/uvicorn/protocols/http/httptools_impl.py), [Hypercorn 0.18.0 `asyncio/task_group.py`](https://raw.githubusercontent.com/pgjones/hypercorn/0.18.0/src/hypercorn/asyncio/task_group.py), and [Granian 2.8.7 `src/utils.rs`](https://raw.githubusercontent.com/emmett-framework/granian/v2.8.7/src/utils.rs): where each server logs the exception that `ServerErrorMiddleware` re-raises, a second record without `error_id`.
- [SQLAlchemy 2.1.8 `lib/sqlalchemy/exc.py`](https://raw.githubusercontent.com/sqlalchemy/sqlalchemy/rel_2_1_8/lib/sqlalchemy/exc.py): where SQL text and parameters enter exception strings.
- [stripe-python v16.1.0 `stripe/_http_client.py`](https://raw.githubusercontent.com/stripe/stripe-python/v16.1.0/stripe/_http_client.py): a production client that lets the server override retry decisions with a header.
- [CWE-209](https://cwe.mitre.org/data/definitions/209.html): the weakness class for error messages containing sensitive information, with observed CVEs.
