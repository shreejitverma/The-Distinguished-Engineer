---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://fastapi.tiangolo.com/tutorial/testing/"
  - "https://fastapi.tiangolo.com/advanced/async-tests/"
  - "https://www.starlette.io/testclient/"
  - "https://fastapi.tiangolo.com/advanced/testing-dependencies/"
  - "https://fastapi.tiangolo.com/advanced/sub-applications/"
  - "https://docs.python.org/3.15/library/sqlite3.html"
  - "https://github.com/fastapi/fastapi/issues/2003"
  - "https://github.com/fastapi/fastapi/issues/4100"
  - "https://www.starlette.io/lifespan/"
  - "https://www.encode.io/httpx/async/#calling-into-python-web-apps"
  - "https://github.com/florimondmanca/asgi-lifespan"
  - "https://asgi.readthedocs.io/en/latest/specs/main.html"
  - "https://asgi.readthedocs.io/en/latest/specs/www.html"
  - "https://asgi.readthedocs.io/en/latest/specs/lifespan.html"
  - "https://peps.python.org/pep-0525/"
  - "https://peps.python.org/pep-0492/"
  - "https://peps.python.org/pep-0343/"
  - "https://peps.python.org/pep-0703/"
  - "https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites"
  - "https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#sqlite-enabling-transactions"
  - "https://docs.python.org/3/library/sqlite3.html#transaction-control-via-the-autocommit-attribute"
  - "https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.execute"
  - "https://github.com/python/cpython/blob/v3.14.6/Modules/_sqlite/cursor.c"
  - "https://github.com/python/cpython/blob/v3.14.6/Modules/_sqlite/statement.c"
  - "https://www.sqlite.org/lang_savepoint.html"
  - "https://dev.mysql.com/doc/refman/8.4/en/savepoint.html"
  - "https://docs.sqlalchemy.org/en/20/orm/session_basics.html#is-the-session-thread-safe-is-asyncsession-safe-to-share-in-concurrent-tasks"
---

# Production-Grade FastAPI Testing: httpx.AsyncClient, LifespanManager, app.dependency_overrides, and Transactional Database Rollback Fixtures

> [!summary] Claim
> Production-grade FastAPI test suites get deterministic in-process execution and no state leakage by dispatching requests on the test's own event loop via `httpx.AsyncClient` with `httpx.ASGITransport`, driving the lifespan protocol through `asgi_lifespan.LifespanManager`, swapping dependencies through `app.dependency_overrides` with strict teardown, and isolating database writes inside an outer transaction that a SQLAlchemy session joins with `join_transaction_mode="create_savepoint"`, which on SQLite also needs the Python 3.12+ `sqlite3` `autocommit=False` mode.

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))` awaits the ASGI application callable directly in the caller's task on the active event loop, with no socket and no extra thread, while `asgi_lifespan.LifespanManager` drives the ASGI `lifespan` state machine that runs the app's startup and shutdown code.
- Contract rule: `app.dependency_overrides` is a plain mutable `dict` on the `FastAPI` instance; whenever it is non-empty, `solve_dependencies()` rebuilds every sub-dependant with `get_dependant()` on every request, so it belongs only in tests and must be restored with `pop()` (or avoided with a per-test app factory).
- The trap: calling Starlette's synchronous `TestClient` from an `async def` test blocks the test's loop and runs the app on a separate AnyIO `BlockingPortal` thread with its own loop, so awaiting a loop-bound object created by the test raises `RuntimeError` (`got Future ... attached to a different loop`, or `is bound to a different event loop`) and anything that needs the blocked test loop deadlocks.
- Portability fact: Python's `sqlite3` defaults to legacy transaction control, where `SAVEPOINT` does not trigger an implicit `BEGIN`, so the savepoint itself opens the transaction and its `RELEASE` commits; pass `connect_args={"autocommit": False}` (parameter added in Python 3.12, still not the default `(as of 2026-11)`) or use SQLAlchemy's `BEGIN` event hooks on older versions.
- When not to use it: in-process ASGI clients and dependency overrides do not exercise TLS, HTTP/2 or HTTP/3, reverse proxy header handling, the real server's lifespan and signal handling, or database features missing from the test driver; keep a smaller tier of tests against a running server and the production database engine.

## Mental model

```mermaid
sequenceDiagram
    participant Runner as "pytest-asyncio<br/>(MainThread Event Loop)"
    participant Lifespan as "LifespanManager<br/>(ASGI lifespan scope)"
    participant Engine as "AsyncEngine / Connection<br/>(autocommit=False)"
    participant App as "FastAPI App<br/>(app.dependency_overrides)"
    participant Client as "httpx.AsyncClient<br/>(ASGITransport in-process)"
    participant Session as "AsyncSession<br/>(create_savepoint)"

    Runner->>Lifespan: async with LifespanManager(app)
    Lifespan->>App: send {"type": "lifespan.startup"}
    Note over App: Run lifespan context manager<br/>Initialize app.state.ready = True
    App-->>Lifespan: receive {"type": "lifespan.startup.complete"}
    
    Runner->>Engine: connection = await engine.connect()
    Runner->>Engine: outer_trans = await connection.begin()
    Note over Engine: sqlite3 autocommit=False already<br/>holds BEGIN DEFERRED on the connection
    
    Runner->>App: app.dependency_overrides[get_db] = override_db
    Runner->>Client: await client.post("/accounts")
    Client->>App: handle_async_request(scope, receive, send)
    
    Note over App: solve_dependencies()<br/>Inspects dependency_overrides<br/>Yields test_session bound to connection
    App->>Session: session.add(account)
    App->>Session: await session.commit()
    Note over Session,Engine: SAVEPOINT sa_savepoint_1<br/>INSERT INTO ledger_accounts<br/>RELEASE SAVEPOINT sa_savepoint_1<br/>(Outer transaction remains uncommitted!)
    App-->>Client: 201 Created {"id": 1}
    
    Runner->>App: app.dependency_overrides.pop(get_db)
    Runner->>Engine: await outer_trans.rollback()
    Note over Engine: ROLLBACK<br/>(Discards every row written by the test)
    
    Runner->>Lifespan: exit context manager
    Lifespan->>App: send {"type": "lifespan.shutdown"}
    Note over App: Run lifespan teardown<br/>app.state.ready = False
    App-->>Lifespan: receive {"type": "lifespan.shutdown.complete"}
```

Every test execution passes through five coordinated boundary layers.
First, `LifespanManager` initiates the ASGI `lifespan` protocol so the app's startup code populates shared state before any HTTP traffic is sent.
Second, the test harness checks out a dedicated `AsyncConnection` from the database engine and opens a top-level transaction on it.
Third, the test harness configures `app.dependency_overrides` so that the database dependency yields an `AsyncSession` bound to that connection with nested transaction semantics (`SAVEPOINT`).
Fourth, `httpx.AsyncClient` dispatches HTTP requests directly to the application's ASGI callable on the test's own event loop, where route handlers commit against savepoints without committing the outer transaction.
Fifth, test teardown removes the override, rolls back the top-level transaction to discard every mutation, and the exit of `LifespanManager` sends `lifespan.shutdown` so the app's teardown code runs.

## How it works

### In-process ASGI dispatch vs Starlette TestClient architecture

Starlette provides a synchronous `TestClient` designed for convenience, but its threading model is hostile to asynchronous test suites `(starlette 1.10.0 starlette/testclient.py, TestClient)`.
`TestClient` subclasses `httpx2.Client`; the module imports `httpx2 as httpx` and only falls back to `httpx` with a `StarletteDeprecationWarning` when `httpx2` is missing `(starlette 1.10.0 starlette/testclient.py, module imports)`.
Requests go through a custom synchronous transport, `_TestClientTransport` `(starlette 1.10.0 starlette/testclient.py, _TestClientTransport)`.
Because `_TestClientTransport.handle_request()` is a synchronous method, it cannot await the asynchronous ASGI callable `app(scope, receive, send)` on the calling thread.
To bridge this gap, `TestClient` uses AnyIO's `BlockingPortal` `(starlette 1.10.0 starlette/testclient.py, TestClient.__enter__)`:

```python
# Simplified excerpt from Starlette 1.10.0 starlette/testclient.py
self.portal = portal = stack.enter_context(
    anyio.from_thread.start_blocking_portal(**self.async_backend)
)
```

`start_blocking_portal()` starts a worker thread that runs its own event loop.
Inside a `with TestClient(app):` block one portal serves the whole block, and `__enter__` also starts `self.lifespan` on it, so the `with` form runs the app's lifespan.
Without the `with` block, `TestClient._portal_factory` opens a fresh portal, and therefore a fresh thread and event loop, for every request; two consecutive `client.get()` calls in this lab ran on threads named `asyncio-portal-...` with different loop ids.
When `handle_request()` receives a request, it submits the ASGI coroutine to the portal with `portal.start_task_soon(run_app)` and blocks the calling thread on `portal.call(response_available.wait)` `(starlette 1.10.0 starlette/testclient.py, _TestClientTransport.handle_request)`.
This architecture creates three failure modes when the caller is itself an `async def` test:
First, any object bound to the test's loop fails when the app awaits it on the portal loop; reproduced here, awaiting a future created on the test loop raised `RuntimeError: Task <...> got Future <...> attached to a different loop`, and an `asyncio.Queue` already used on the test loop raised `RuntimeError: <Queue ...> is bound to a different event loop`.
Not every async resource is loop-bound: an `aiosqlite`-backed SQLAlchemy `AsyncConnection` created on the test loop worked from the portal loop in the same experiment, because `aiosqlite` creates its futures on whichever loop calls it; drivers that bind connections to one loop are the ones that break.
`asyncpg` is one: its `Connection` stores the loop it was created on and schedules its tasks and futures there `(asyncpg 0.32.0 asyncpg/connection.py, Connection.__init__)`.
Reproduced against local PostgreSQL 18.7 on loopback with a throwaway script (`labs/.venv/bin/python -I xloop.py`, CPython 3.14.6, Darwin 27.3.0): a connection opened on the test loop answered `select 1` from the test loop, and the same `await conn.fetchval("select 1")` inside a route reached through the synchronous `TestClient` raised `RuntimeError: ... attached to a different loop`, followed by an `InternalClientError: got result for unknown protocol state 3` logged on the abandoned future.
Second, the blocking call stalls the test's loop, so a handler that waits for the test loop (for example an `asyncio.Event` set by a callback scheduled on it) never progresses; in the lab such a handler only returned because it was wrapped in `asyncio.wait_for(..., 3)`, which raised `TimeoutError`.
Third, every request crosses threads, and without the `with` block every request also creates a thread and a loop; the Numbers table shows the measured cost.

Conversely, `httpx.AsyncClient` combined with `httpx.ASGITransport` runs the app in-process on the caller's event loop `(httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport)`.
Its core method `handle_async_request()` builds the ASGI HTTP scope dictionary directly `(httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.handle_async_request)`:

```python
# Simplified excerpt from httpx 0.28.1 httpx/_transports/asgi.py
scope = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": request.method,
    "headers": [(k.lower(), v) for (k, v) in request.headers.raw],
    "scheme": request.url.scheme,
    "path": request.url.path,
    "raw_path": request.url.raw_path.split(b"?")[0],
    "query_string": request.url.query,
    "server": (request.url.host, request.url.port),
    "client": self.client,
    "root_path": self.root_path,
}
```

The transport defines local asynchronous `receive()` and `send()` closures and awaits the application in the caller's task: `await self.app(scope, receive, send)`.
Because the client and the application share one event loop and one task, coroutines, locks, context variables, and database connections are used without thread boundaries or cross-loop futures.
Three consequences follow from the same source.
The scope always says `"http_version": "1.1"`, so HTTP/2 and HTTP/3 behavior is never exercised.
The response body is buffered: `send()` appends each `http.response.body` chunk to a list and `ASGIResponseStream` yields the joined bytes, so a `StreamingResponse` reaches the test only after the app finishes sending.
With the default `raise_app_exceptions=True`, an exception raised by the app propagates out of `await client.get(...)` instead of becoming a `500` response; with `raise_app_exceptions=False` the transport synthesizes status `500` when no response was started.

### ASGI Lifespan protocol and LifespanManager orchestration

`httpx.ASGITransport` only ever sends `type: "http"` scopes, so it never runs the ASGI Lifespan protocol `(httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.handle_async_request)`, `(ASGI Lifespan Protocol specification)`.
FastAPI's own documentation says the same: "If your application relies on lifespan events, the `AsyncClient` won't trigger these events", and it recommends `LifespanManager` from `asgi-lifespan`.
In a production server, Uvicorn opens a lifespan connection with `scope = {"type": "lifespan", "asgi": {"version": self.config.asgi_version, "spec_version": "2.0"}, "state": self.state}` `(uvicorn 0.54.0 uvicorn/lifespan/on.py, LifespanOn.main)`.
Starlette's router handles this scope inside `Router.lifespan()` `(starlette 1.10.0 starlette/routing.py, Router.lifespan)`:

```python
# Simplified excerpt from Starlette 1.10.0 starlette/routing.py
async def lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
    started = False
    app: Any = scope.get("app")
    await receive()  # Waits for {"type": "lifespan.startup"}
    try:
        async with self.lifespan_context(app) as maybe_state:
            if maybe_state is not None:
                if "state" not in scope:
                    raise RuntimeError('The server does not support "state" in the lifespan scope.')
                scope["state"].update(maybe_state)
            await send({"type": "lifespan.startup.complete"})
            started = True
            await receive()  # Waits for {"type": "lifespan.shutdown"}
    except BaseException:
        exc_text = traceback.format_exc()
        if started:
            await send({"type": "lifespan.shutdown.failed", "message": exc_text})
        else:
            await send({"type": "lifespan.startup.failed", "message": exc_text})
        raise
    else:
        await send({"type": "lifespan.shutdown.complete"})
```

If a test uses `httpx.AsyncClient(transport=ASGITransport(app=app))` alone, `Router.lifespan` never runs.
Resources created during startup (connection pools, HTTP client sessions, caches, models stored on `app.state`) do not exist, and endpoints that read them fail: in the lab, a guarded `/health` returns `503 Service Unavailable`, and an unguarded endpoint reading `request.app.state.ready` makes `await client.get(...)` raise `AttributeError: 'State' object has no attribute 'ready'` `(starlette 1.10.0 starlette/datastructures.py, State.__getattr__)`.
Shutdown code never runs either, so background tasks and connections opened at startup are never closed.

The `asgi-lifespan` library fills this gap with `LifespanManager` `(asgi_lifespan 2.1.0 asgi_lifespan/_manager.py, LifespanManager)`.
When entered via `async with LifespanManager(app) as manager:`, it does three things:
1. It wraps the application in `state_middleware`, exposed as `manager.app`, which sets `scope["state"]` to one shared `_state` dictionary on every scope that passes through it `(asgi_lifespan 2.1.0 asgi_lifespan/_manager.py, state_middleware)`.
2. It starts a background task running `self.app({"type": "lifespan"}, self.receive, self.send)`; the scope carries no `asgi` key, and `state` is added by the middleware `(asgi_lifespan 2.1.0 asgi_lifespan/_manager.py, LifespanManager.run_app)`.
3. It puts `{"type": "lifespan.startup"}` on a receive queue of capacity 2 and waits for `lifespan.startup.complete` under `asyncio.wait_for` with `startup_timeout=5`, raising the builtin `TimeoutError` on expiry `(asgi_lifespan 2.1.0 asgi_lifespan/_concurrency/asyncio.py, AsyncioBackend.run_and_fail_after)`.
The dictionary yielded by a lifespan function only reaches `request.state` when the client is built on `manager.app`; in the lab, `request.state.db` was `"pool"` through `ASGITransport(app=manager.app)` and missing through `ASGITransport(app=app)`, while attributes assigned to `app.state` worked either way.
On a clean exit, `__aexit__` pushes `{"type": "lifespan.shutdown"}`, waits for `lifespan.shutdown.complete` under `shutdown_timeout=5`, and re-raises any exception the lifespan task ended with.
If the test body raises, no `lifespan.shutdown` is sent; the background task is cancelled instead, so code in a `finally` around `yield` runs but code placed after `yield` without `finally` does not (measured in the lab: events `startup`, `finally`, `after-yield` on a clean exit, and only `startup`, `finally` when the body raised) `(asgi_lifespan 2.1.0 asgi_lifespan/_manager.py, LifespanManager.__aexit__)`.
See [[Structuring-Large-FastAPI-Applications-APIRouter-Lifespan-and-Domain-Modules]] for production lifespan context architectures.

### pytest-asyncio loop scope decides which loop the fixtures and the app share

`httpx.ASGITransport` runs the app on whichever loop awaits it, so the loop a fixture's resources were created on must be the loop the test runs on.
pytest-asyncio 1.4.1.post1 decides this per item: a test's loop scope is the `loop_scope` of its closest `asyncio` marker, otherwise `asyncio_default_test_loop_scope`; async fixtures take `loop_scope` from `@pytest_asyncio.fixture(loop_scope=...)` or `asyncio_default_fixture_loop_scope`, and each scope has its own runner (`_{scope}_scoped_runner`) `(pytest-asyncio 1.4.1.post1 pytest_asyncio/plugin.py, _get_default_test_loop_scope, PytestAsyncioFunction._loop_scope)`.
Leaving `asyncio_default_fixture_loop_scope` unset emits a `PytestDeprecationWarning` `(pytest-asyncio 1.4.1.post1 pytest_asyncio/plugin.py, pytest_configure)`.
The lab sets `asyncio_mode = "strict"` and `asyncio_default_fixture_loop_scope = "function"` in `labs/pyproject.toml`, so every test gets a fresh loop and builds and disposes its own engine.
The consequence follows from the loop-bound `asyncpg` result above: an engine or connection created in a wider-scoped async fixture than the test that uses it lives on a different loop, so a loop-bound driver fails with the same `attached to a different loop` error, while `aiosqlite` hides the mismatch.
Widen the fixture and the test loop scopes together (for example both `session`) when the engine is shared, and keep the per-test transaction and `AsyncSession` function-scoped.

### `app.dependency_overrides` mechanics and dynamic Dependant graph compilation

FastAPI's mechanism for replacing dependencies in tests is the `app.dependency_overrides` dictionary, initialized to `{}` in `FastAPI.__init__` and documented there as "for testing, to replace expensive dependencies with testing versions" `(fastapi 0.162.0 fastapi/applications.py, FastAPI.__init__)`.
The dictionary maps original dependency callables to replacement callables, and the app passes itself to its router as `dependency_overrides_provider`.
When a route is registered, FastAPI builds a static `Dependant` tree for the path operation `(fastapi 0.162.0 fastapi/dependencies/utils.py, get_dependant)`.
Overrides are applied at request time inside `solve_dependencies()` `(fastapi 0.162.0 fastapi/dependencies/utils.py, solve_dependencies)`:

```python
# Simplified excerpt from FastAPI 0.162.0 fastapi/dependencies/utils.py
if (
    dependency_overrides_provider
    and dependency_overrides_provider.dependency_overrides
):
    original_call = sub_dependant.call
    call = getattr(
        dependency_overrides_provider, "dependency_overrides", {}
    ).get(original_call, original_call)
    use_path: str = sub_dependant.path
    use_sub_dependant = get_dependant(
        path=use_path,
        call=call,
        name=sub_dependant.name,
        parent_oauth_scopes=_get_oauth_scopes(dependant=sub_dependant),
        scope=sub_dependant.scope,
    )
```

This code has four consequences:
First, recompilation is triggered by a non-empty dictionary, not by a match: the `if` tests only that `dependency_overrides` is truthy, and `.get(original_call, original_call)` falls back to the original, so every sub-dependant of every request is rebuilt by `get_dependant()` while any override is installed.
Measured with a counting wrapper around `get_dependant` on a route with two dependencies: zero calls per request with an empty dictionary, two calls per request with one unrelated key present.
Because `get_dependant()` inspects the replacement's signature, a replacement can declare its own `Depends(...)`, headers, or query parameters; the lab's nested-override test resolves a header-driven sub-dependency this way.
Second, OAuth2 scope inheritance: the rebuilt dependant receives `parent_oauth_scopes=_get_oauth_scopes(dependant=sub_dependant)`, so scopes declared on the route or parent dependency still flow into the replacement.
Third, cache key binding: after solving, FastAPI computes `sub_dependant_cache_key = _get_cache_key(dependant=sub_dependant, uses_scopes_cache=_uses_scopes_cache)` from the original `sub_dependant`, and the key is the tuple `(dependant.call, scopes_for_cache, computed_scope)` `(fastapi 0.162.0 fastapi/dependencies/models.py, _get_cache_key)`.
Every parameter that depends on the original callable with `use_cache=True` (the default) therefore shares one value per request produced by the override, which the lab asserts by counting one invocation for two parameters.
Fourth, global mutable state: the dictionary lives on the application object, so an override installed and not removed affects every later request to that object, including requests from later tests in the same process.
`pytest-xdist` workers are separate processes with separate app objects, so the realistic leak is sequential (test A forgets to `pop()`, test B in the same worker inherits it) or concurrent tasks inside one test that expect different overrides.
See [[Dependency-Injection-Engine-DAG-Resolution-and-Use-Cache-Mechanics]] for detailed DAG resolution algorithms.

### Transactional rollback fixtures with SQLAlchemy 2.0/2.1 SAVEPOINTs

Traditional database test isolation re-runs migrations, executes `Base.metadata.create_all()` and `drop_all()`, or issues `TRUNCATE TABLE` between tests.
Measured on this machine, those resets cost milliseconds per test, while rolling back an outer transaction costs tens of microseconds: on a local PostgreSQL 18.7, the median was `7.9 ms` for `TRUNCATE` of 10 tables, `3.7 ms` for `drop_all` plus `create_all` of one table, and `96 us` for the outer rollback (see the Numbers table).
Across 1,000 tests the `TRUNCATE` reset alone adds about 8 seconds on that setup, against about 0.1 seconds for rollbacks; production schemas with more tables, indexes, and foreign keys make DDL resets more expensive.

The rollback pattern replaces physical cleanup with a transaction that is never committed.
The schema is created once per test session (or once per `pytest-xdist` worker database).
Isolation per test comes from SQLAlchemy's `join_transaction_mode="create_savepoint"`, a `Session.__init__` parameter whose default is `"conditional_savepoint"` `(sqlalchemy 2.1.8 sqlalchemy/orm/session.py, Session.__init__)`.
The mode matters: when the bound connection already has a transaction but no savepoint, `"conditional_savepoint"` resolves to `"rollback_only"`, so a handler's `session.rollback()` would roll back the test's outer transaction, while `"create_savepoint"` always wraps the session's work in `begin_nested()` `(sqlalchemy 2.1.8 sqlalchemy/orm/session.py, SessionTransaction._connection_for_bind)`.
The lifecycle proceeds in five phases:
1. Connection acquisition: the fixture checks out one dedicated `AsyncConnection`: `connection = await engine.connect()`.
2. Outer transaction initiation: the fixture begins a top-level transaction: `outer_trans = await connection.begin()`.
3. Session binding with savepoints: the fixture constructs an `AsyncSession` bound to that connection:
   ```python
   session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
   ```
4. Savepoints during endpoint execution: the session begins lazily, so `SAVEPOINT sa_savepoint_1` is emitted at the first flush or query (the name comes from a per-connection counter, `"sa_savepoint_%s"` in `(sqlalchemy 2.1.8 sqlalchemy/engine/base.py, Connection._savepoint_impl)`).
   When the endpoint calls `await session.commit()`, SQLAlchemy emits `RELEASE SAVEPOINT sa_savepoint_1` and leaves the outer transaction open; the next savepoint, `sa_savepoint_2`, is emitted only at the next flush, not immediately after the commit (statement trace in this lab).
   If the endpoint calls `await session.rollback()`, SQLAlchemy emits `ROLLBACK TO SAVEPOINT` for the current savepoint.
5. Top-level rollback: when the test finishes, the fixture executes `await outer_trans.rollback()` and closes the connection, discarding every savepoint's work; the measured medians are `41 us` on in-memory SQLite and `96 us` on local PostgreSQL 18.7.

### SQLite legacy transaction control and the `autocommit=False` fix

Implementing transactional rollback on SQLite (such as `aiosqlite` in-memory test databases) runs into the `sqlite3` module's legacy transaction control.
In legacy mode the module opens a transaction implicitly only before `INSERT`, `UPDATE`, `DELETE`, or `REPLACE` when no transaction is open (Python `sqlite3` docs, `Cursor.execute`).
The check is in C: `_pysqlite_query_execute` calls `begin_transaction()` only when `autocommit == AUTOCOMMIT_LEGACY`, `isolation_level` is set, `statement->is_dml` is true, and SQLite reports autocommit `(CPython 3.14.6 Modules/_sqlite/cursor.c, _pysqlite_query_execute)`, and `is_dml` is set only for statements starting with `insert`, `update`, `delete`, or `replace` `(CPython 3.14.6 Modules/_sqlite/statement.c, pysqlite_statement_create)`.
SQLAlchemy's pysqlite dialect also emits no `BEGIN` of its own, so `connection.begin()` sends nothing to SQLite.

When the session then emits `SAVEPOINT sa_savepoint_1`, no transaction is open, and SQLite defines an outermost savepoint outside `BEGIN...COMMIT` to behave "the same as BEGIN DEFERRED TRANSACTION", and a `RELEASE` that empties the savepoint stack to be "the same as COMMIT" (SQLite `SAVEPOINT` documentation).
So `session.commit()` inside the route handler durably commits the row, and the fixture's later `outer_trans.rollback()` has nothing to undo.
It fails silently: in the lab experiment the legacy engine raised no error on the outer rollback and the row was still there afterwards (1 row versus 0 rows with `autocommit=False`).
SQLAlchemy's SQLite dialect documentation describes the same failure: a SAVEPOINT emitted before a BEGIN "fails to participate in the enclosing transaction, meaning a ROLLBACK of the transaction will not rollback elements that were part of a released savepoint" `(sqlalchemy 2.1.8 sqlalchemy/dialects/sqlite/base.py, module docstring)`.

Python 3.12 added the `autocommit` parameter to `sqlite3.connect()` ("Changed in version 3.12: Added the autocommit parameter"); its default is still `LEGACY_TRANSACTION_CONTROL`, and the docs say it will change to `False` in a future release `(as of 2026-11)`.
This is a regular CPython change, not a PEP; PEP 682 is the unrelated "Format Specifier for Signed Zero".
SQLAlchemy passes `connect_args` through to `aiosqlite.connect()`, which forwards keyword arguments to `sqlite3.connect()` `(aiosqlite 0.22.1 aiosqlite/core.py, connect)`:

```python
engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"autocommit": False, "check_same_thread": False},
    poolclass=StaticPool,
)
```

With `autocommit=False`, the driver, not SQLAlchemy, keeps a transaction open at all times: `connect()`, `commit()`, and `rollback()` each open a new transaction with `BEGIN DEFERRED` (Python `sqlite3` docs, "Transaction control via the autocommit attribute").
The lab's statement trace shows exactly that: `SAVEPOINT`, `INSERT`, `RELEASE`, then the outer `ROLLBACK` followed immediately by the driver's `BEGIN`.
Every savepoint therefore nests inside the driver's transaction, and the outer rollback discards it.
On Python versions without the parameter, SQLAlchemy documents the alternative: set `isolation_level = None` in a `connect` event and emit `BEGIN` yourself in a `begin` event `(sqlalchemy 2.1.8 sqlalchemy/dialects/sqlite/base.py, module docstring)`.
For `:memory:` URLs the aiosqlite dialect already selects `StaticPool` `(sqlalchemy 2.1.8 sqlalchemy/dialects/sqlite/aiosqlite.py, SQLiteDialect_aiosqlite.get_pool_class)`; passing it explicitly documents the requirement that every connection share one in-memory database.

## Numbers and limits

Measured rows ran on Apple M3 Pro, macOS 27.3 arm64 (Darwin 27.3.0), CPython 3.14.6 from the lab virtualenv, with fastapi 0.162.0, starlette 1.10.0, httpx 0.28.1, httpx2 2.16.0, uvicorn 0.54.0, SQLAlchemy 2.1.8, aiosqlite 0.22.1 (SQLite 3.53.1), asyncpg 0.32.0, and a local PostgreSQL 18.7 over loopback TCP `(as of 2026-11)`.
Each request row is 2,000 sequential `GET /health` calls on the lab app after 100 warm-up calls, timed with `time.perf_counter_ns()` from a throwaway script run as `labs/.venv/bin/python bench.py`; database rows are 270 to 500 iterations after warm-up.

| Metric / Parameter | Value / Limit | Scope / Impact | Source / Verification |
|---|---|---|---|
| In-process ASGI request latency | median `101 us`, p99 `333 us` | `httpx.AsyncClient` + `ASGITransport` inside `LifespanManager` | Measured (setup above) |
| `TestClient` request latency, `with` block | median `578 us`, p99 `859 us` | One portal thread for the block | Measured (setup above) |
| `TestClient` request latency, no `with` block | median `914 us`, p99 `6.5 ms` | New portal thread and loop per request (n=270) | Measured (setup above) |
| Loopback TCP request latency | median `525 us`, p99 `872 us` | Uvicorn 0.54.0 on `127.0.0.1:0`, keep-alive `httpx.AsyncClient` | Measured (setup above) |
| Outer rollback, SQLite | median `41 us`, p99 `292 us` | `outer_trans.rollback()` after one savepoint commit, in-memory | Measured (setup above) |
| Outer rollback, PostgreSQL | median `96 us`, p99 `3.1 ms` | Same flow, local PostgreSQL 18.7 via asyncpg | Measured (setup above) |
| DDL reset, SQLite | median `0.98 ms`, p99 `7.7 ms` | `drop_all()` + `create_all()`, one table, in-memory | Measured (setup above) |
| DDL reset, PostgreSQL | median `3.7 ms`, p99 `12.8 ms` | `drop_all()` + `create_all()`, one table | Measured (setup above) |
| `TRUNCATE ... RESTART IDENTITY CASCADE`, PostgreSQL | median `7.9 ms`, p99 `15.6 ms` | 10 tables, one row each | Measured (setup above) |
| Lifespan startup timeout | `5` seconds, raises builtin `TimeoutError` | Max wait for `lifespan.startup.complete` | `(asgi_lifespan 2.1.0 asgi_lifespan/_manager.py, LifespanManager.__init__)` |
| Lifespan shutdown timeout | `5` seconds | Max wait for `lifespan.shutdown.complete`; only on clean exit | `(asgi_lifespan 2.1.0 asgi_lifespan/_manager.py, LifespanManager.__init__)` |
| Callable classification cache size | `4096` entries per `lru_cache` | `_is_gen_callable_cached` and siblings | `(fastapi 0.162.0 fastapi/dependencies/models.py, _CALLABLE_CLASSIFICATION_CACHE_SIZE)` |
| `TestClient` default client address | `("testclient", 50000)` | ASGI scope `client` | `(starlette 1.10.0 starlette/testclient.py, TestClient.__init__)` |
| `ASGITransport` default client address | `("127.0.0.1", 123)` | ASGI scope `client` | `(httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.__init__)` |
| AnyIO default threadpool capacity | `40` tokens | `CapacityLimiter` used by `run_in_threadpool` for `def` dependencies and endpoints | `(anyio 4.16.0 anyio/_backends/_asyncio.py, AsyncIOBackend.current_default_thread_limiter)` |
| `StaticPool` size | exactly 1 connection | "A Pool of exactly one connection, used for all requests" | `(sqlalchemy 2.1.8 sqlalchemy/pool/impl.py, StaticPool)` |
| `QueuePool` defaults | `pool_size=5`, `max_overflow=10`, `timeout=30.0` | Checkout limit for file and server databases | `(sqlalchemy 2.1.8 sqlalchemy/pool/impl.py, QueuePool.__init__)` |

## Portability

Only CPython 3.14.6 was run in this lab; the matrix legs (3.12.16, 3.13.18, 3.14.10, 3.15.2, 3.15.2t) were not run because the user paused lab matrix checks on 2026-11-11 `(as of 2026-11)`.
Cells for unrun legs therefore state only what a cited source establishes, and say "not run" where nothing does.
The lab workspace declares `requires-python = ">=3.12,<3.16"` (`labs/pyproject.toml`), and the pinned releases declare these minimums in their wheel metadata: fastapi 0.162.0 `>=3.10`, starlette 1.10.0 `>=3.11`, SQLAlchemy 2.1.8 `>=3.11`, anyio 4.16.0 `>=3.10`, httpx 0.28.1 `>=3.8`, asgi-lifespan 2.1.0 `>=3.7`, aiosqlite 0.22.1 `>=3.9`, asyncpg 0.32.0 `>=3.9` (`Requires-Python` in each `*.dist-info/METADATA`).
Reading the pinned source, `httpx/_transports/asgi.py` and `asgi_lifespan` contain no `sys.version_info` branch, and the only one on the FastAPI dependency path is in `_get_signature`: when `inspect.signature(call, eval_str=True)` raises `NameError`, Python 3.14 and later retry with `annotationlib.Format.FORWARDREF` and earlier versions retry without it `(fastapi 0.162.0 fastapi/dependencies/utils.py, _get_signature)`.
That branch matters to overrides because `get_dependant()` analyses the replacement's signature, so a replacement whose annotations name a `TYPE_CHECKING`-only import is analysed differently across versions; this is a source reading, not a run.

| Runtime / Engine | ASGI In-Process (`httpx.ASGITransport`) | Lifespan Support (`asgi-lifespan`) | Dependency Overrides | Transactional SAVEPOINT Rollback |
|---|---|---|---|---|
| CPython 3.12.16 | Not run; no version branch in the transport source | Not run; no version branch in the library source | Not run; `_get_signature` takes the non-`FORWARDREF` retry path | Not run; `autocommit` exists since 3.12 ("Changed in version 3.12: Added the autocommit parameter"), default legacy, so `connect_args={"autocommit": False}` is required |
| CPython 3.13.18 | Not run; same source as 3.12 | Not run; same source as 3.12 | Not run; same `_get_signature` path as 3.12 | Not run; same documented `sqlite3` behavior as 3.12 |
| CPython 3.14.10 | Ran on 3.14.6, 10 tests passed; 3.14.10 not run | Ran on 3.14.6 | Ran on 3.14.6; `_get_signature` takes the `FORWARDREF` retry path | Ran on 3.14.6: legacy mode leaves 1 row, `autocommit=False` leaves 0 |
| CPython 3.15.2 | Not run; same source | Not run; same source | Not run; same `_get_signature` path as 3.14 | Not run; `sqlite3.connect()` parameters other than `database` are keyword-only from 3.15, which `connect_args` already satisfies, and the 3.15 docs keep the legacy default and say it "will change to `False` in a future Python release" |
| CPython 3.15.2t (free-threaded) | Not run | Not run | Not run; PEP 703 gives `dict` a per-object lock, so concurrent `pop()` and item assignment are mutually excluded, while test-to-test leakage is a sequencing problem and unchanged | Not run |
| Uvicorn 0.54.0 | Not used by in-process tests; loopback latency measured above | Sends the `lifespan` scope with `state` `(uvicorn 0.54.0 uvicorn/lifespan/on.py, LifespanOn.main)` | Same `FastAPI` object, no server involvement | N/A |
| Hypercorn 0.18.0, Granian 2.8.7 | Not run; in-process tests bypass the server | Not run | Not run | N/A |
| HTTP/1.1, HTTP/2, HTTP/3 | Scope is always `"http_version": "1.1"`; HTTP/2 and HTTP/3 need a real server | N/A | N/A | N/A |
| SQLite (`aiosqlite`) | N/A (database transport) | N/A | N/A | Ran: works with `autocommit=False` (3.12+) or the SQLAlchemy `BEGIN` event recipe; `:memory:` needs one shared connection (`StaticPool`, the dialect default) |
| PostgreSQL (`asyncpg`) | N/A (database transport) | N/A | N/A | Ran: native `SAVEPOINT`; the rollback flow ran against local PostgreSQL 18.7 with asyncpg 0.32.0 for the Numbers table; asyncpg connections are loop-bound (reproduced above) |
| MySQL (`asyncmy`) | N/A (database transport) | N/A | N/A | Not run; InnoDB supports `SAVEPOINT`, `ROLLBACK TO SAVEPOINT`, and `RELEASE SAVEPOINT` (MySQL 8.4 manual) |

## Worked example

The worked example consists of a testing harness and a test suite covering lifespan orchestration, dependency overrides, and transactional rollback fixtures.
The implementation lives in [fastapi_testing_harness.py](../labs/src/api_labs/f11_fastapi_internals_and_patterns/fastapi_testing_harness.py) and the test suite lives in [test_testing_fastapi_applications_asyncclient_lifespan_and_dependency_overrides.py](../labs/tests/f11_fastapi_internals_and_patterns/test_testing_fastapi_applications_asyncclient_lifespan_and_dependency_overrides.py).
Production wiring goes through `app.state.session_factory`, so `create_app()` returns an app with an empty `dependency_overrides` map, and only tests install overrides.

Here is the implementation of the application factory, lifecycle state tracker, and transactional session provider from `fastapi_testing_harness.py`:

```python
"""FastAPI testing harness: Lifespan, dependency overrides, and transactional rollback."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

EXPECTED_AUTH_CREDENTIAL = "valid-test-credential"


class Base(DeclarativeBase):
    """Declarative base class for SQLAlchemy models."""


class LedgerAccount(Base):
    """Transactional ledger account table for testing state isolation and rollback."""

    __tablename__ = "ledger_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    holder_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    balance: Mapped[int] = mapped_column(nullable=False, default=0)


@dataclass
class AppLifecycleState:
    """Explicit state tracker for lifespan startup and shutdown verification."""

    startup_called: bool = False
    shutdown_called: bool = False
    active_connections: int = 0


def create_test_engine(
    url: str = "sqlite+aiosqlite:///:memory:",
) -> AsyncEngine:
    """Create an asynchronous SQLite test engine with sqlite3 autocommit=False (Python 3.12+).

    With autocommit=False the sqlite3 driver keeps a transaction open at all times
    (BEGIN DEFERRED on connect and after every commit or rollback), so SAVEPOINTs nest
    inside it and the outer rollback also discards released SAVEPOINTs.
    StaticPool ensures all async connections within the process share the same in-memory DB.
    """
    return create_async_engine(
        url,
        connect_args={"autocommit": False, "check_same_thread": False},
        poolclass=StaticPool,
    )


async def init_db(engine: AsyncEngine) -> None:
    """Create all declarative tables on the test engine."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Production database session dependency; tests replace it via dependency_overrides."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_current_user(request: Request) -> str:
    """Default authentication dependency requiring a valid Bearer token."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    bearer_credential = auth_header.removeprefix("Bearer ").strip()
    if bearer_credential != EXPECTED_AUTH_CREDENTIAL:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    return "prod_user"


def create_app(
    engine: AsyncEngine,
    lifecycle_state: AppLifecycleState | None = None,
) -> FastAPI:
    """Application factory configuring lifespan state and route endpoints."""
    tracker = lifecycle_state or AppLifecycleState()

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncGenerator[dict[str, Any], None]:
        tracker.startup_called = True
        tracker.active_connections = 1
        app.state.ready = True
        app.state.tracker = tracker
        try:
            yield {"ready": True, "tracker": tracker}
        finally:
            tracker.shutdown_called = True
            tracker.active_connections = 0
            app.state.ready = False

    app = FastAPI(title="FastAPI-Testing-Harness", lifespan=app_lifespan)

    # Production wiring goes through app.state, never dependency_overrides: any non-empty
    # overrides dict makes solve_dependencies() rebuild every sub-dependant per request.
    app.state.session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    @app.get("/health")
    async def health_check(request: Request) -> dict[str, Any]:
        if not getattr(request.app.state, "ready", False):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Application lifespan uninitialized",
            )
        return {
            "status": "healthy",
            "ready": request.app.state.ready,
            "startup_called": request.app.state.tracker.startup_called,
        }

    @app.get("/me")
    async def get_me(user: Annotated[str, Depends(get_current_user)]) -> dict[str, str]:
        return {"user": user}

    @app.get("/me-cached")
    async def get_me_cached(
        user1: Annotated[str, Depends(get_current_user, use_cache=True)],
        user2: Annotated[str, Depends(get_current_user, use_cache=True)],
    ) -> dict[str, Any]:
        return {"user1": user1, "user2": user2, "identical": user1 is user2}

    @app.post("/accounts", status_code=status.HTTP_201_CREATED)
    async def create_account(
        holder_name: str,
        balance: int,
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> dict[str, Any]:
        account = LedgerAccount(holder_name=holder_name, balance=balance)
        session.add(account)
        await session.commit()
        await session.refresh(account)
        return {
            "id": account.id,
            "holder_name": account.holder_name,
            "balance": account.balance,
        }

    @app.get("/accounts")
    async def list_accounts(
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> list[dict[str, Any]]:
        query = select(LedgerAccount).order_by(LedgerAccount.id)
        result = await session.execute(query)
        accounts = result.scalars().all()
        return [
            {"id": acc.id, "holder_name": acc.holder_name, "balance": acc.balance}
            for acc in accounts
        ]

    @app.post("/transfer")
    async def transfer_balance(
        source_id: int,
        target_id: int,
        amount: int,
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> dict[str, Any]:
        if amount <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer amount must be strictly positive",
            )
        src_res = await session.execute(select(LedgerAccount).where(LedgerAccount.id == source_id))
        source = src_res.scalar_one_or_none()
        dst_res = await session.execute(select(LedgerAccount).where(LedgerAccount.id == target_id))
        target = dst_res.scalar_one_or_none()
        if source is None or target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or both accounts not found",
            )
        if source.balance < amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insufficient funds",
            )
        source.balance -= amount
        target.balance += amount
        await session.commit()
        return {
            "source_balance": source.balance,
            "target_balance": target.balance,
            "transferred": amount,
        }

    return app


def create_transactional_session(connection: AsyncConnection) -> AsyncSession:
    """Create an AsyncSession bound to an external transaction using SAVEPOINTs.

    join_transaction_mode='create_savepoint' forces the session to execute begin_nested()
    for all transactional operations. Calling commit() inside route handlers releases only
    the savepoint without committing the enclosing outer transaction.
    """
    return AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
```

The corresponding test module validates state isolation, nested dependency overrides, and transactional rollback:

```python
# Simplified excerpt from test_testing_fastapi_applications_asyncclient_lifespan_and_dependency_overrides.py
@pytest.mark.unit
@pytest.mark.asyncio
async def test_transactional_rollback_isolates_db_mutations() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)

        async with engine.connect() as connection:
            outer_trans = await connection.begin()
            test_session = create_transactional_session(connection)

            async def override_db() -> AsyncIterator[AsyncSession]:
                yield test_session

            app.dependency_overrides[get_db] = override_db
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    create_resp = await client.post(
                        "/accounts",
                        params={"holder_name": "alice_vault", "balance": 1000},
                    )
                    assert create_resp.status_code == status.HTTP_201_CREATED
                    account_id = create_resp.json()["id"]

                    list_resp = await client.get("/accounts")
                    assert list_resp.status_code == status.HTTP_200_OK
                    accounts = list_resp.json()
                    assert len(accounts) == 1
                    assert accounts[0]["id"] == account_id
            finally:
                app.dependency_overrides.pop(get_db, None)
                await test_session.close()
                await outer_trans.rollback()

        async with engine.connect() as verify_conn:
            verify_session = AsyncSession(bind=verify_conn)
            result = await verify_session.execute(select(LedgerAccount))
            persisted = result.scalars().all()
            assert len(persisted) == 0
            await verify_session.close()
    finally:
        await engine.dispose()
```

Executing the test suite produces the following pytest output:

```text
$ uv run --frozen pytest tests/f11_fastapi_internals_and_patterns/test_testing_fastapi_applications_asyncclient_lifespan_and_dependency_overrides.py -q
..........                                                               [100%]
10 passed in 2.06s
```
*(Captured on CPython 3.14.6, Darwin 27.3.0 arm64, macOS 27.3, Apple M3 Pro)*.

## Pitfalls

> [!warning] Pitfall 1: Event loop mismatch and deadlocks with Starlette's `TestClient`
> Trigger: Calling Starlette's synchronous `TestClient` from an `async def` test, especially while the app uses objects created on the test's event loop.
> Symptom: `RuntimeError: Task <...> got Future <...> attached to a different loop`, `RuntimeError: <Queue ...> is bound to a different event loop`, or a hang when the handler waits on the blocked test loop (all three reproduced in this lab's experiments; the same `different loop` error is the subject of upstream issue `fastapi/fastapi#4100`, where a Tortoise ORM suite on `TestClient` hit it after 0.69 and moved to an async client).
> Fix: In async tests, use `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` so the app runs on the test's loop; keep `TestClient` for synchronous tests, inside a `with` block so lifespan runs and one portal serves all requests.

> [!warning] Pitfall 2: Silent omission of application lifespan in raw `AsyncClient`
> Trigger: Executing requests via `httpx.AsyncClient(transport=ASGITransport(app=app))` without wrapping the application in a lifespan context manager.
> Symptom: `await client.get(...)` raises `AttributeError: 'State' object has no attribute '...'` (or returns `500` with `raise_app_exceptions=False`), or pools and background workers created at startup are missing.
> Incident: upstream issue `fastapi/fastapi#2003`, "AsyncClient ignores the startup and shutdown events", where a startup-registered middleware was missing in tests; maintainers answered with `LifespanManager` and no code change.
> Fix: Wrap client execution inside `async with LifespanManager(app) as manager:` and build the transport on `manager.app` when handlers read lifespan-yielded state from `request.state`.

> [!warning] Pitfall 3: State leakage from a shared `app.dependency_overrides`
> Trigger: Mutating `app.dependency_overrides` on a module-level application object and not restoring it, so a later test in the same worker process runs with the earlier test's override.
> Symptom: Order-dependent failures where test B passes or fails depending on whether test A ran first in the same worker, for example an endpoint that should return `401 Unauthorized` returning `200 OK` with a mocked user.
> Fix: Restore in a fixture `finally` block with `app.dependency_overrides.pop(key, None)` (the FastAPI testing guide resets with `app.dependency_overrides = {}`), or build an independent app per test with a factory such as `create_app()`.

> [!warning] Pitfall 4: Default-argument closures in override functions become request parameters
> Trigger: Binding a test object through a default argument, for example `async def override_db(current_session: AsyncSession = session): yield current_session`.
> Symptom: `get_dependant()` treats `current_session` as a query parameter; for a non-Pydantic type such as `AsyncSession` the request fails with `fastapi.exceptions.FastAPIError: Invalid args for response field! Hint: check that <class '...'> is a valid Pydantic field type`, and for a `str` default the caller can replace the value (in the lab, `?current=attacker` made the override return `"attacker"`).
> Fix: Use a factory function returning a zero-argument closure:
> ```python
> def make_override(s: AsyncSession) -> Callable[[], AsyncIterator[AsyncSession]]:
>     async def _override() -> AsyncIterator[AsyncSession]:
>         yield s
>     return _override
> app.dependency_overrides[get_db] = make_override(session)
> ```

> [!warning] Pitfall 5: SQLite legacy transaction control bypassing SAVEPOINT rollback
> Trigger: Using `sqlite+aiosqlite:///:memory:` with the default `sqlite3` legacy transaction control.
> Symptom: The first `SAVEPOINT` opens the transaction itself, its `RELEASE` on `session.commit()` commits the row, and `outer_trans.rollback()` silently does nothing, so rows leak into later tests that share the database.
> Fix: On Python 3.12+, configure `create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"autocommit": False, "check_same_thread": False}, poolclass=StaticPool)`; on older versions, use SQLAlchemy's `isolation_level = None` plus `BEGIN` event recipe.

> [!warning] Pitfall 6: Dependency overrides failing on mounted sub-applications
> Trigger: Registering a sub-application via `app.mount("/sub", sub_app)` and setting overrides on `app.dependency_overrides`.
> Symptom: Route handlers mounted under `/sub` run their original dependencies, because the mounted `FastAPI` instance is its own `dependency_overrides_provider` (asserted by `test_parent_overrides_do_not_reach_mounted_sub_application`).
> Reference: the FastAPI sub-applications guide describes a mounted application as "completely independent".
> Fix: Mutate `sub_app.dependency_overrides` on the child application, or refactor mounted sub-applications into `APIRouter` instances included via `app.include_router()`.

## Security and robustness

Testing harnesses require strict boundary enforcement to prevent security vulnerabilities and test integrity failures:

### Guarding against mock leaks in production environments

`app.dependency_overrides` is a test-only substitution hook that can replace authentication, tenant authorization, and signature checks with anything.
Production entry points must not ship with overrides installed, which also keeps `solve_dependencies()` off its per-request rebuild path.
During production startup, applications should assert that overrides are empty:
```python
if os.getenv("ENVIRONMENT") == "production" and app.dependency_overrides:
    raise RuntimeError("Security violation: app.dependency_overrides must be empty in production.")
```
The lab's `create_app()` follows this rule and `test_factory_leaves_overrides_empty_and_production_get_db_commits` asserts it.

### Negative security boundary testing

A common testing antipattern replaces authentication everywhere via `app.dependency_overrides[get_current_user] = mock_user`.
While convenient for testing domain logic, this practice leaves the real security perimeter untested.
Test suites must include dedicated modules that exercise the un-overridden production dependency against hostile inputs: expired JWTs, invalid signatures, missing `Bearer` prefixes, injection strings in tenant identifiers, and insufficient OAuth2 scopes; the lab's `/me` tests check the missing-header and restored-dependency `401` paths.

### In-memory database isolation and credential protection

When tests run in parallel, sharing one SQLite file path (for example `/tmp/test.db`) across workers creates cross-test races and leaves fixture data on the filesystem.
Test engines should use per-process in-memory databases (`sqlite+aiosqlite:///:memory:`) with a single shared connection (`StaticPool`), or derive isolated database names per worker from the `PYTEST_XDIST_WORKER` environment variable that `pytest-xdist` sets in each worker:
```python
worker_id = os.getenv("PYTEST_XDIST_WORKER", "master")
db_url = f"postgresql+asyncpg://user:pass@localhost:5432/test_db_{worker_id}"
```

### Connection pool saturation defense

Transactional rollback fixtures hold one connection checked out for the entire test.
Code under test that checks out additional connections from a `QueuePool` competes with it, and once `pool_size` plus `max_overflow` connections (default `5 + 10`) are in use, further checkouts wait up to `timeout=30.0` seconds and then raise `TimeoutError`.
Work that opens its own connections also does not see the test's uncommitted rows, so either route all of it through the single test connection or size the pool explicitly and accept that those paths are not isolated by the rollback.
Do not share one `AsyncSession` across concurrently running tasks; SQLAlchemy documents that a session is not safe for concurrent use (SQLAlchemy session basics FAQ).

## The principal's view

From the perspective of a principal or staff engineer, testing architecture is an economic and organizational optimization problem, not merely a quality check.
The primary metric governing test suite design is developer feedback loop latency.
Per-test cost decides that latency: from the Numbers table, a PostgreSQL `TRUNCATE` reset of 10 tables costs about `7.9 ms` against about `0.1 ms` for an outer rollback, and an in-process request costs about `0.1 ms` against `0.5 ms` to `0.9 ms` for loopback Uvicorn or `TestClient`.
For 2,000 tests with five requests each, that is roughly 21 seconds of reset and loopback overhead (`7.9 ms + 5 x 0.53 ms` per test) versus about 1.2 seconds (`0.1 ms + 5 x 0.1 ms`) for the in-process, rollback-based design on this machine; container startup per test, if used, adds far more (not measured here).
Faster suites change behavior: engineers run the whole suite before every commit instead of relying on CI.

To get that speed without losing production fidelity, a principal engineer sets a tiered allocation; the split below is a judgment call, not a measured optimum:
- About 80% Tier 1 in-process ASGI tests: built with `httpx.AsyncClient`, `LifespanManager`, scoped `dependency_overrides`, and SAVEPOINT fixtures.
  These test business logic, validation errors, serialization, authentication gates, and multi-step transaction workflows.
- About 15% Tier 2 real-database integration tests: run against ephemeral PostgreSQL or MySQL instances (Testcontainers or pre-warmed databases).
  These validate complex SQL, dialect-specific operators such as JSONB, triggers, advisory locks, and migration scripts.
- About 5% Tier 3 end-to-end black-box tests: run over real sockets against running server processes.
  These validate TLS termination, HTTP/2 multiplexing, reverse proxy header forwarding (`X-Forwarded-For`), and graceful shutdown on `SIGTERM`.

When adopting this architecture across large organizations, principal engineers prohibit direct mutation of a global `app.dependency_overrides` in test files.
Instead, platform teams provide standard pytest fixtures that wrap application factories (`create_app()`) and database session fixtures with guaranteed cleanup, removing a whole class of order-dependent failures.

## Interview questions

> [!question] L5: Why does `httpx.AsyncClient` with `httpx.ASGITransport` execute tests faster than Starlette's `TestClient`?
>
> > [!success]- Answer
> > `httpx.AsyncClient` with `httpx.ASGITransport` awaits the ASGI application callable directly in the caller's task on the current event loop.
> > Starlette's `TestClient` is a synchronous client that bridges to the async app through an AnyIO `BlockingPortal` running on another thread, creating a new portal thread per request when used outside a `with` block.
> > The in-process client avoids thread hand-offs, portal synchronization, and thread creation; measured on this lab's `/health`, the median was `101 us` in-process versus `578 us` for `TestClient` in a `with` block and `914 us` without one.
> >
> > Follow-up: Does `httpx.ASGITransport` trigger ASGI application lifespan startup and shutdown events by default?

> [!question] L5: What happens internally when a dependency is added to `app.dependency_overrides`?
>
> > [!success]- Answer
> > `app.dependency_overrides` is a plain dictionary stored on the `FastAPI` application instance and passed to the router as the overrides provider.
> > On each request, as long as the dictionary is non-empty, `solve_dependencies()` looks up every sub-dependant's callable with `.get(original, original)` and rebuilds each one with `get_dependant()`, overridden or not.
> > Because the replacement's signature is analyzed, it can declare its own `Depends()`, headers, or query parameters.
> > The solved value is cached under the original dependency's cache key, so all `use_cache=True` consumers of the original share the override's value within a request.
> >
> > Follow-up: How do you prevent dependency overrides set in one test from leaking into subsequent tests?

> [!question] L6: Predict the output when an endpoint accessing `request.app.state` is requested via raw `AsyncClient` vs `LifespanManager`.
>
> > [!success]- Answer
> > Given an endpoint `GET /state` returning `{"ready": request.app.state.ready}` where `ready` is set to `True` inside `@asynccontextmanager async def lifespan(app)`:
> > With raw `httpx.AsyncClient(transport=ASGITransport(app=app))`, `await client.get("/state")` raises `AttributeError: 'State' object has no attribute 'ready'`, because `raise_app_exceptions=True` re-raises the app's exception; with `raise_app_exceptions=False` it returns `500`.
> > Inside `async with LifespanManager(app):` it returns `200` with `{"ready": true}`.
> > Raw `ASGITransport` dispatches only `type: "http"` scopes and never sends the `type: "lifespan"` scope.
> >
> > Follow-up: What happens if an exception is raised inside the application's lifespan startup context manager when using `LifespanManager`?

> [!question] L6: Debug an API test where binding a mock session via default argument causes `FastAPIError`.
>
> > [!success]- Answer
> > An engineer writes `async def override_db(s: AsyncSession = mock_session): yield s` and registers it in `app.dependency_overrides[get_db]`.
> > When the test runs, FastAPI raises `FastAPIError: Invalid args for response field! Hint: check that <class '...AsyncSession'> is a valid Pydantic field type`.
> > `get_dependant()` inspects every parameter of the override; a parameter without a `Depends` marker or special type is classified as a query parameter, and FastAPI cannot build a Pydantic field for `AsyncSession`.
> > The fix is a zero-argument closure produced by a factory function: `def make_override(s): async def _override(): yield s; return _override`.
> >
> > Follow-up: Can a dependency override itself define dependencies via `Depends()`?

> [!question] L6: Why does SQLite fail to roll back changes made inside a `SAVEPOINT` unless `autocommit=False` is set?
>
> > [!success]- Answer
> > By default `sqlite3` uses legacy transaction control, which implicitly issues `BEGIN` only before `INSERT`, `UPDATE`, `DELETE`, or `REPLACE`, and SQLAlchemy's pysqlite dialect emits no `BEGIN` itself.
> > SQLite treats an outermost `SAVEPOINT` outside a transaction like `BEGIN DEFERRED`, and a `RELEASE` that empties the savepoint stack like `COMMIT`.
> > So when the handler commits and SQLAlchemy emits `RELEASE SAVEPOINT`, the row is committed, and the fixture's outer `ROLLBACK` has nothing to undo; it does not raise.
> > Setting `connect_args={"autocommit": False}` (Python 3.12+) makes `sqlite3` keep a `BEGIN DEFERRED` transaction open from connect and after every commit or rollback, so savepoints nest inside it.
> >
> > Follow-up: Does this SQLite behavior affect PostgreSQL or MySQL test suites?

> [!question] L6: How does SQLAlchemy's `join_transaction_mode="create_savepoint"` intercept `session.commit()` calls in route handlers?
>
> > [!success]- Answer
> > When an `AsyncSession` is constructed with `join_transaction_mode="create_savepoint"` on a connection that already has a transaction, the session's transaction calls `connection.begin_nested()` when it first needs the connection.
> > That emits `SAVEPOINT sa_savepoint_1` at the first flush or query.
> > `await session.commit()` commits only the nested transaction, emitting `RELEASE SAVEPOINT sa_savepoint_1`, and leaves the outer transaction untouched.
> > The next savepoint is created lazily at the next flush or query, not immediately after the commit.
> > With the default `"conditional_savepoint"` and no existing savepoint, the session would instead join in `"rollback_only"` mode.
> >
> > Follow-up: What happens if the route handler calls `await session.rollback()` instead of commit?

> [!question] L7: How do you design a hermetic test architecture supporting `pytest-xdist -n auto` on 32 CPU cores against PostgreSQL?
>
> > [!success]- Answer
> > Each xdist worker reads `PYTEST_XDIST_WORKER` (`gw0` through `gw31`) and creates or reuses its own database (`test_db_gw0` and so on), for example by cloning a migrated template database with `CREATE DATABASE ... TEMPLATE`.
> > Migrations run once into the template (or once per worker database) to establish tables, constraints, and indexes.
> > Within each worker, every test checks out one `AsyncConnection`, begins an outer transaction, and binds sessions with `join_transaction_mode="create_savepoint"`.
> > Each test rolls back its outer transaction at teardown, which measured about `0.1 ms` median on a local PostgreSQL 18.7, with no cross-worker lock contention because workers use different databases.
> > Each test builds its app with an application factory (`create_app()`), so no override map is shared between tests.
> >
> > Follow-up: How do you handle database migration integration tests within this architecture without dropping tables used by other tests?

> [!question] L7: Why do dependency overrides on a root FastAPI app fail to affect routes in mounted sub-applications?
>
> > [!success]- Answer
> > Sub-applications mounted via `app.mount("/sub", sub_app)` are independent ASGI applications with their own `FastAPI` instances.
> > When a request enters `/sub/endpoint`, Starlette's `Mount` extends the scope's `root_path` with the mount prefix and calls `sub_app` `(starlette 1.10.0 starlette/routing.py, Mount.matches)`.
> > `sub_app`'s routes were registered with `sub_app` as their `dependency_overrides_provider`, so `solve_dependencies()` consults `sub_app.dependency_overrides` and never the parent's dictionary.
> > To fix this, platforms either apply overrides directly to `sub_app.dependency_overrides` or replace mounts with `APIRouter` instances via `app.include_router()`.
> >
> > Follow-up: How would you design a request-scoped dependency override system using `contextvars` to eliminate mutable dictionary state entirely?

## Related

Prerequisites: [[Structuring-Large-FastAPI-Applications-APIRouter-Lifespan-and-Domain-Modules]]

Next: [[Class-Based-Dependencies-Callable-Objects-and-Parameterized-Guards]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[Dependency-Injection-Engine-DAG-Resolution-and-Use-Cache-Mechanics]], [[Yield-Dependencies-AsyncExitStack-Cleanup-Timing-and-Scope]]

- Lab: `labs/tests/f11_fastapi_internals_and_patterns/test_testing_fastapi_applications_asyncclient_lifespan_and_dependency_overrides.py`

## Further reading

- [ASGI 3.0 Specification](https://asgi.readthedocs.io/en/latest/specs/main.html): The authoritative specification defining the ASGI callable interface, HTTP scope dictionaries, and event lifecycles.
- [ASGI Lifespan Protocol](https://asgi.readthedocs.io/en/latest/specs/lifespan.html): Specification for the lifespan state machine governing application startup, state propagation, and shutdown completion.
- [Python sqlite3: Transaction control via the autocommit attribute](https://docs.python.org/3/library/sqlite3.html#transaction-control-via-the-autocommit-attribute): The Python 3.12+ `autocommit` model that keeps a transaction open so savepoints nest correctly.
- [SQLite SAVEPOINT](https://www.sqlite.org/lang_savepoint.html): Defines why an outermost savepoint acts as `BEGIN DEFERRED` and its `RELEASE` as `COMMIT`.
- [SQLAlchemy Joining a Session into an External Transaction](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites): Official documentation on `join_transaction_mode="create_savepoint"` for test suite integration.
- [HTTPX Async Client and ASGI Transport](https://www.encode.io/httpx/async/#calling-into-python-web-apps): Maintainer documentation on dispatching in-process requests directly to ASGI applications.
- [FastAPI Advanced Testing with AsyncClient](https://fastapi.tiangolo.com/advanced/async-tests/): Official guide on writing asynchronous tests for FastAPI applications, including the lifespan warning.
- [FastAPI: Testing Dependencies with Overrides](https://fastapi.tiangolo.com/advanced/testing-dependencies/): Maintainer documentation of `app.dependency_overrides` as a plain `dict` and how to reset it.
- [asgi-lifespan README](https://github.com/florimondmanca/asgi-lifespan): Documents `LifespanManager`, its 5-second timeouts, and the rule to build the client on `manager.app`.
