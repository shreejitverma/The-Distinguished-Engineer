---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/"
  - "https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/tutorial/dependencies/dependencies-with-yield.md"
  - "https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/advanced/advanced-dependencies.md"
  - "https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/release-notes.md"
  - "https://docs.python.org/3/library/contextlib.html"
  - "https://peps.python.org/pep-0343/"
  - "https://peps.python.org/pep-0492/"
  - "https://peps.python.org/pep-0525/"
  - "https://peps.python.org/pep-0789/"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc9112.html"
  - "https://asgi.readthedocs.io/en/latest/specs/www.html"
  - "https://github.com/fastapi/fastapi/pull/5122"
  - "https://github.com/fastapi/fastapi/pull/10831"
  - "https://github.com/fastapi/fastapi/pull/11191"
  - "https://github.com/fastapi/fastapi/pull/14099"
  - "https://github.com/fastapi/fastapi/pull/14262"
  - "https://github.com/fastapi/fastapi/pull/14301"
  - "https://github.com/encode/starlette"
  - "https://github.com/litestar-org/litestar"
  - "https://peps.python.org/pep-0255/"
  - "https://github.com/fastapi/fastapi/pull/4575"
  - "https://raw.githubusercontent.com/emmett-framework/granian/v2.8.7/README.md"
  - "https://github.com/pgjones/hypercorn"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.73.0/fastapi/applications.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.105.0/fastapi/middleware/asyncexitstack.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.110.0/fastapi/routing.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.117.1/fastapi/routing.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.118.0/fastapi/routing.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.121.0/fastapi/dependencies/utils.py"
  - "https://raw.githubusercontent.com/fastapi/fastapi/0.121.1/fastapi/dependencies/utils.py"
---

# Generator (yield) Dependencies in Depth: AsyncExitStack Teardown, Exception Handling, and scope='function' vs scope='request'

> [!summary] Claim
> FastAPI (`0.162.0`) wraps every generator dependency (`def` or `async def` with `yield`) in a context manager entered onto one of two nested per-request `contextlib.AsyncExitStack` instances inside `request_response` - `scope["fastapi_function_astack"]` (`scope="function"`, which unwinds before `await response(scope, receive, send)` sends `http.response.start`) and `scope["fastapi_inner_astack"]` (`scope="request"`, the default, which unwinds after `StreamingResponse` body iteration and `BackgroundTasks` complete).

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: `solve_dependencies()` converts `async def` generator dependencies via `contextlib.asynccontextmanager` and synchronous `def` generator dependencies via `contextmanager_in_threadpool(contextlib.contextmanager(call)(**sub_values))`, pushing their `__aexit__` callbacks onto `scope["fastapi_function_astack"]` (`scope="function"`) or `scope["fastapi_inner_astack"]` (`scope="request"`).
- Contract rule: In `request_response`, `fastapi_function_astack` exits immediately after the endpoint returns its `Response` object (before `http.response.start`, body streaming, and `BackgroundTasks`), whereas `fastapi_inner_astack` exits only after `await response(scope, receive, send)` finishes both body transmission and `BackgroundTasks`.
- The trap: Raising a handled exception (such as `HTTPException`) after `yield` in a `scope="request"` dependency on a successful endpoint return triggers `RuntimeError("Caught handled exception, but response already started.")` in Starlette's `wrap_app_handling_exceptions`, while swallowing an endpoint exception in `except` without re-raising triggers `FastAPIError("Response not awaited...")`.
- Portability fact: FastAPI moved `yield` cleanup before response sending in `0.106.0` (breaking use of those resources in `BackgroundTasks` and `StreamingResponse`), reverted default `yield` cleanup to post-response in `0.118.0`, and introduced `Depends(..., scope="function" | "request")` in `0.121.0` (`(as of 2026-11)`).
- When not to use it: Do not use `scope="request"` (the default) for database sessions or locks that are only needed inside the endpoint function when the endpoint returns a slow `StreamingResponse` or long `BackgroundTasks`, and never rely on post-`yield` code in `scope="request"` to commit transactions that must turn commit failures into `HTTPException` responses.

## Mental model

```mermaid
sequenceDiagram
    participant Client
    participant ExcWrap as "wrap_app_handling_exceptions<br/>(response_started flag)"
    participant ReqStack as "request_stack<br/>(scope['fastapi_inner_astack'])"
    participant FuncStack as "function_stack<br/>(scope['fastapi_function_astack'])"
    participant Endpoint as "f(request)<br/>solve_dependencies + endpoint"
    participant Resp as "response(scope, receive, send)<br/>Stream + BackgroundTasks"

    Client->>ExcWrap: ASGI http scope
    ExcWrap->>ReqStack: __aenter__()
    ReqStack->>FuncStack: __aenter__()
    FuncStack->>Endpoint: await f(request)
    Note over Endpoint: Enter scope='request' on ReqStack<br/>Enter scope='function' on FuncStack<br/>Run endpoint & serialize_response
    Endpoint-->>FuncStack: return Response
    Note over FuncStack: __aexit__() LIFO teardown<br/>(scope='function' exits here;<br/>response_started is False)
    FuncStack->>Resp: await response(scope, receive, send)
    Note over Resp,ExcWrap: send http.response.start (response_started = True)<br/>stream http.response.body chunks<br/>await response.background()
    Resp-->>ReqStack: response_awaited = True
    Note over ReqStack: __aexit__() LIFO teardown<br/>(scope='request' exits here;<br/>response_started is True)
    ReqStack-->>Client: ASGI task complete
```

Every HTTP request entering a FastAPI route passes through three nested `contextlib.AsyncExitStack` contexts before reaching the path operation function.
First, `AsyncExitStackMiddleware` enters `scope["fastapi_middleware_astack"]` inside `ExceptionMiddleware` exclusively to close multipart `FormData` (`UploadFile` handles) after the route completes.
Second, `request_response` in `fastapi/routing.py` wraps the route inside Starlette's `wrap_app_handling_exceptions` and enters `request_stack` (`scope["fastapi_inner_astack"]`) followed immediately by `function_stack` (`scope["fastapi_function_astack"]`).
During `await f(request)`, `solve_dependencies()` resolves the dependency DAG depth-first and enters each generator dependency onto either `function_stack` (when `scope="function"`) or `request_stack` (when `scope="request"` or omitted).
When `f(request)` returns the `Response` object, `function_stack.__aexit__()` runs immediately in last-in, first-out (LIFO) order while `response_started` is still `False`, after which `await response(scope, receive, send)` flushes `http.response.start`, streams all `http.response.body` chunks, and awaits `BackgroundTasks`, and finally `request_stack.__aexit__()` unwinds all `scope="request"` dependencies in LIFO order.

## How it works

### Generator detection and startup scope validation

When an `APIRoute` is registered, `get_dependant()` in `fastapi/dependencies/utils.py` recursively inspects callable signatures to construct the `Dependant` tree `(fastapi 0.162.0 fastapi/dependencies/utils.py, get_dependant)`.
FastAPI classifies callables using `inspect.isgeneratorfunction` and `inspect.isasyncgenfunction` after unwrapping `functools.partial` chains via `_impartial()` and decorator chains via `inspect.unwrap()` `(fastapi 0.162.0 fastapi/dependencies/models.py, _is_gen_callable_cached)` and `(fastapi 0.162.0 fastapi/dependencies/models.py, _is_async_gen_callable_cached)`.
If the target is a callable class instance rather than a function or class, both helpers also inspect `getattr(call, "__call__", None)` so generator `__call__` methods on parameterized dependency objects are detected identically.
Each classification result is memoized in an `@lru_cache(maxsize=4096)` keyed by `_CallIdentity`, which hashes `id(self.call)` and compares object identity via `is` `(fastapi 0.162.0 fastapi/dependencies/models.py, _CallIdentity)`.

Every `Depends` marker carries `scope: Literal["function", "request"] | None = None` `(fastapi 0.162.0 fastapi/params.py, Depends)`.
The helper `_get_computed_scope(*, dependant: Dependant) -> str | None` returns `dependant.scope` if explicitly set, or defaults to `"request"` whenever `_is_gen_callable(dependant.call)` or `_is_async_gen_callable(dependant.call)` is `True`, and `None` for non-generator dependencies `(fastapi 0.162.0 fastapi/dependencies/models.py, _get_computed_scope)`.

Because a parent generator dependency's post-`yield` cleanup code may use values yielded by its sub-dependencies, a sub-dependency must never exit *before* the parent dependency that consumes it.
In `get_dependant()`, FastAPI enforces this lifetime hierarchy at route registration time (simplified excerpt from `(fastapi 0.162.0 fastapi/dependencies/utils.py, get_dependant)`):

```python
if (
    (
        _is_gen_callable(dependant.call)
        or _is_async_gen_callable(dependant.call)
    )
    and _get_computed_scope(dependant=dependant) == "request"
    and param_details.depends.scope == "function"
):
    assert dependant.call
    call_name = getattr(dependant.call, "__name__", "<unnamed_callable>")
    raise DependencyScopeError(
        f'The dependency "{call_name}" has a scope of '
        '"request", it cannot depend on dependencies with scope "function".'
    )
```

A `scope="request"` dependency exits after the response is sent, so if its sub-dependency had `scope="function"`, that sub-dependency would already be closed before the parent ran its post-`yield` cleanup.
Conversely, a `scope="function"` dependency *is* permitted to depend on a `scope="request"` sub-dependency because `fastapi_function_astack` unwinds before `fastapi_inner_astack`, guaranteeing the `scope="request"` resource remains alive throughout the `scope="function"` dependency's teardown.

### Cache key isolation by computed scope

Per-request dependency deduplication uses the 3-tuple `DependencyCacheKey` returned by `_get_cache_key()` (simplified excerpt from `(fastapi 0.162.0 fastapi/dependencies/models.py, _get_cache_key)`):

```python
return (
    dependant.call,
    scopes_for_cache,
    _get_computed_scope(dependant=dependant) or "",
)
```

Including `_get_computed_scope(dependant=dependant) or ""` as the third element prevents a subtle lifetime bug: if the same generator function `get_session` is injected once with `Depends(get_session, scope="function")` and once with `Depends(get_session, scope="request")` within the same request, a 2-tuple key `(call, scopes)` would alias the `"request"` injection to the `"function"` instance, which would then close before `StreamingResponse` or `BackgroundTasks` could use it.
With the 3-tuple key, `(get_session, (), "function")` and `(get_session, (), "request")` occupy separate slots in `dependency_cache` and enter separate context managers on `fastapi_function_astack` and `fastapi_inner_astack`.
See [[Dependency-Injection-Engine-DAG-Resolution-and-Use-Cache-Mechanics]] for the complete DAG traversal and `use_cache` mechanics.

### Wrapping generators: `asynccontextmanager` and `contextmanager_in_threadpool`

When `solve_dependencies()` encounters an uncached generator dependency, it selects the target stack and delegates to `_solve_generator()` (simplified excerpt from `(fastapi 0.162.0 fastapi/dependencies/utils.py, _solve_generator)`):

```python
async def _solve_generator(
    *, dependant: Dependant, stack: AsyncExitStack, sub_values: dict[str, Any]
) -> Any:
    assert dependant.call
    if _is_async_gen_callable(dependant.call):
        cm = asynccontextmanager(dependant.call)(**sub_values)
    elif _is_gen_callable(dependant.call):
        cm = contextmanager_in_threadpool(contextmanager(dependant.call)(**sub_values))
    return await stack.enter_async_context(cm)
```

For an `async def` generator dependency (defined by `(PEP 525)`), `contextlib.asynccontextmanager` wraps the async generator in `_AsyncGeneratorContextManager` `(CPython 3.14.10 Lib/contextlib.py, _AsyncGeneratorContextManager)`.
Calling `await stack.enter_async_context(cm)` invokes `await cm.__aenter__()` (which runs `await anext(self.gen)` to advance the coroutine to its `yield` expression and returns the yielded value) and, via `_push_async_cm_exit()`, appends `(False, MethodType(type(cm).__aexit__, cm))` to `stack._exit_callbacks`, a `collections.deque` `(CPython 3.14.10 Lib/contextlib.py, AsyncExitStack.enter_async_context)`.

For a synchronous `def` generator dependency (generators are defined by `(PEP 255)`; the generator-based context manager template comes from `(PEP 343)`), `contextlib.contextmanager` produces a synchronous `_GeneratorContextManager`, which FastAPI lifts into an async context manager via `contextmanager_in_threadpool()` (simplified excerpt from `(fastapi 0.162.0 fastapi/concurrency.py, contextmanager_in_threadpool)`):

```python
@asynccontextmanager
async def contextmanager_in_threadpool(
    cm: AbstractContextManager[_T],
) -> AsyncGenerator[_T, None]:
    exit_limiter = CapacityLimiter(1)
    try:
        yield await run_in_threadpool(cm.__enter__)
    except Exception as e:
        ok = bool(
            await anyio.to_thread.run_sync(
                cm.__exit__, type(e), e, e.__traceback__, limiter=exit_limiter
            )
        )
        if not ok:
            raise e
    else:
        await anyio.to_thread.run_sync(
            cm.__exit__, None, None, None, limiter=exit_limiter
        )
```

Notice two critical details in `contextmanager_in_threadpool`:
1. `cm.__enter__` runs via `starlette.concurrency.run_in_threadpool`, which calls `anyio.to_thread.run_sync(func)` with no limiter and therefore uses AnyIO's default threadpool `CapacityLimiter(40)` `(starlette 1.10.0 starlette/concurrency.py, run_in_threadpool)` `(anyio 4.16.0 anyio/_backends/_asyncio.py, AsyncIOBackend.current_default_thread_limiter)`.
   However, `cm.__exit__` runs with a freshly allocated `exit_limiter = CapacityLimiter(1)` (introduced in FastAPI `0.82.0`, PR `#5122`).
   If `cm.__exit__` shared the default 40-thread limiter, a burst of 40 concurrent requests waiting to acquire a database connection in `cm.__enter__` could starve the `cm.__exit__` calls of in-flight requests trying to *release* connections back to the pool, causing a classic two-pool lock-order deadlock.
   See [[Async-Def-vs-Def-Endpoints-and-AnyIO-Threadpool-Limiter-Semantics]] for threadpool limiter mechanics.
2. Line 30 catches `except Exception as e:` rather than `except BaseException as e:`.
   Because `asyncio.CancelledError` inherits from `BaseException` rather than `Exception` (since Python 3.8), if a task is cancelled while inside `AsyncExitStack`, `contextmanager_in_threadpool` does not catch `CancelledError` and never schedules `cm.__exit__` in the worker threadpool.
   Instead, when the `contextmanager_in_threadpool` frame unwinds and drops the last reference to the `_GeneratorContextManager`, CPython finalizes the suspended synchronous generator, whose finalizer calls `close()` and throws `GeneratorExit` into it directly on the main event-loop thread.
   Observed on CPython `3.14.6` (`fastapi 0.162.0`, `anyio 4.16.0`): cancelling a task holding such a dependency on an `AsyncExitStack` logged `GeneratorExit` in the generator on `threading.main_thread()`, followed by its `finally` on the same thread, before the cancelled task finished, and `cm.__exit__` never ran in a worker thread.

### The two dependency stacks in `request_response` and wire framing constraints

The exact teardown timing of `scope="function"` versus `scope="request"` is governed by the inner `app` closure of `request_response` in `fastapi/routing.py` (simplified excerpt from `(fastapi 0.162.0 fastapi/routing.py, request_response)`):

```python
async def app(scope: Scope, receive: Receive, send: Send) -> None:
    request = Request(scope, receive, send)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        # Starts customization
        response_awaited = False
        async with AsyncExitStack() as request_stack:
            scope["fastapi_inner_astack"] = request_stack
            async with AsyncExitStack() as function_stack:
                scope["fastapi_function_astack"] = function_stack
                response = await f(request)
            await response(scope, receive, send)
            # Continues customization
            response_awaited = True
        if not response_awaited:
            raise FastAPIError(
                "Response not awaited. There's a high chance that the "
                "application code is raising an exception and a dependency with yield "
                "has a block with a bare except, or a block with except Exception, "
                "and is not raising the exception again..."
            )

    # Same as in Starlette
    await wrap_app_handling_exceptions(app, request)(scope, receive, send)
```

And inside `Response.__call__` (or `StreamingResponse.__call__`) in `starlette/responses.py` (simplified excerpt from `(starlette 1.10.0 starlette/responses.py, Response.__call__)`):

```python
await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
await send({"type": "http.response.body", "body": self.body})

if self.background is not None:
    await self.background()
```

Tracing the wire and ASGI state machine across this boundary explains every timing and exception rule:

1. **Before `await response(scope, receive, send)` (`scope="function"` teardown)**:
   When `await f(request)` returns the `Response` instance, the `async with AsyncExitStack() as function_stack:` block exits immediately.
   `AsyncExitStack.__aexit__` pops callbacks from `function_stack._exit_callbacks` in LIFO order and awaits `cb(None, None, None)` `(CPython 3.14.10 Lib/contextlib.py, AsyncExitStack.__aexit__)`.
   At this moment, `await response(scope, receive, send)` has not been called, no `http.response.start` ASGI message has been emitted, and on an HTTP/1.1 connection the status line and header section (`HTTP-message = start-line CRLF *( field-line CRLF ) CRLF [ message-body ]` per `(RFC 9112 Section 2.1)` and `(RFC 9112 Section 4)`) have not been written to the socket.
   Therefore, if a `scope="function"` dependency raises an `HTTPException` or other handled exception in its post-`yield` block, `response_started` inside `wrap_app_handling_exceptions` `(starlette 1.10.0 starlette/_exception_handler.py, wrap_app_handling_exceptions)` is still `False`; the exception propagates through `request_stack.__aexit__` and is converted by `wrap_app_handling_exceptions` into a valid HTTP error response (for example, `503 Service Unavailable`).
2. **During `await response(scope, receive, send)` (`StreamingResponse` and `BackgroundTasks`)**:
   Once `function_stack.__aexit__` completes without error, `await response(scope, receive, send)` executes.
   Starlette's `sender` wrapper in `wrap_app_handling_exceptions` intercepts `message["type"] == "http.response.start"` and sets `response_started = True` `(starlette 1.10.0 starlette/_exception_handler.py, wrap_app_handling_exceptions)`.
   For a `StreamingResponse`, `stream_response()` iterates `self.body_iterator` and sends `http.response.body` chunks with `more_body: True` followed by a terminal `b""` chunk with `more_body: False` `(starlette 1.10.0 starlette/responses.py, StreamingResponse.stream_response)`.
   Immediately after the body finishes, `Response.__call__` and `StreamingResponse.__call__` execute `if self.background is not None: await self.background()`, running each `BackgroundTask` sequentially inside `await response(scope, receive, send)` `(starlette 1.10.0 starlette/background.py, BackgroundTasks.__call__)`.
   Throughout both body streaming and `BackgroundTasks`, every `scope="function"` dependency is already closed, whereas every `scope="request"` dependency is still active!
   See [[Yield-Dependencies-Inside-StreamingResponse-and-Resource-Lifecycle-Traps]] for how `StreamingResponse` and `EventSourceResponse` interact with `fastapi_inner_astack`.
3. **After `await response(scope, receive, send)` (`scope="request"` teardown)**:
   After `await response(scope, receive, send)` returns, `response_awaited = True` is set and `request_stack.__aexit__(None, None, None)` unwinds all `scope="request"` dependencies in LIFO order.
   Because `http.response.start` and `http.response.body` have already been sent (`response_started == True`) and final HTTP status codes (`200` through `599` per `(RFC 9110 Section 15)`) cannot be retroactively rewritten on the wire after the header section is flushed, raising any exception that matches a Starlette exception handler (such as `HTTPException`) inside `scope="request"` post-`yield` cleanup hits lines 55-56 of `starlette/_exception_handler.py` (simplified excerpt from `(starlette 1.10.0 starlette/_exception_handler.py, wrap_app_handling_exceptions)`):
   ```python
   if response_started:
       raise RuntimeError("Caught handled exception, but response already started.") from exc
   ```
4. **When `f(request)` raises an exception**:
   If a sub-dependency, request validation, or the endpoint function raises an exception `exc`, `await f(request)` aborts *before* `await response(scope, receive, send)` is reached.
   First, `function_stack.__aexit__(type(exc), exc, exc.__traceback__)` throws `exc` into every active `scope="function"` generator via `gen.athrow(value)` (or `cm.__exit__` in a worker thread).
   Next, the uncaught (or replaced) exception propagates into `request_stack.__aexit__(type(exc), exc, exc.__traceback__)`, throwing it into every active `scope="request"` generator *before* `http.response.start` is ever sent (`response_started == False`).
   If multiple dependencies in a chain raise during teardown, `AsyncExitStack.__aexit__` chains them via `_fix_exception_context(new_exc, exc)`, which walks `new_exc.__context__` to the end of its chain and sets that link to the previous exception so the full failure chain is preserved `(CPython 3.14.10 Lib/contextlib.py, AsyncExitStack.__aexit__)`.
   However, if a `yield` dependency catches `exc` in an `except` block and neither re-raises nor raises a replacement exception, `_AsyncGeneratorContextManager.__aexit__` catches the resulting `StopAsyncIteration` and returns `True` (suppressing the exception per `(PEP 343)` and `(PEP 492)`).
   `AsyncExitStack.__aexit__` then suppresses the exception, execution resumes after `async with AsyncExitStack() as request_stack:` with `response_awaited == False`, and FastAPI raises `FastAPIError("Response not awaited...")`.
   The guard first appeared in FastAPI `0.110.0` (PR `#11191`) as `FastAPIError("No response object was returned...")` inside `get_request_handler()`; the current `response_awaited` check and message arrived with the `request_response` copy in `0.118.0` (PR `#14099`) `(fastapi 0.110.0 fastapi/routing.py, get_request_handler)` `(fastapi 0.118.0 fastapi/routing.py, request_response)`.

## Numbers and limits

All runtime measurements below were re-measured on an Apple M3 Pro (`Darwin 27.2.0 arm64`, macOS `27.2`) under the lab workspace interpreter CPython `3.14.6` (`fastapi 0.162.0`, `starlette 1.10.0`, `anyio 4.16.0`) with a throwaway script run from `labs/` as `uv run --frozen python measure.py`; CPython `3.14.6` and `3.14.10` ship a byte-identical `Lib/contextlib.py`.

| Metric or parameter | Value | Source / measurement | Operational implication |
|---|---:|---|---|
| Per-request `AsyncExitStack` instances | `3` (`fastapi_middleware_astack`, `fastapi_inner_astack`, `fastapi_function_astack`) | `(fastapi 0.162.0 fastapi/middleware/asyncexitstack.py, AsyncExitStackMiddleware.__call__)`; `(fastapi 0.162.0 fastapi/routing.py, request_response)` | Allocated on every HTTP and WebSocket request even when an endpoint declares zero `yield` dependencies |
| Shallow memory per `AsyncExitStack` + `deque` | `808 B` (`48 B` instance + `760 B` `collections.deque`) | Measured via `sys.getsizeof(stack) + sys.getsizeof(stack._exit_callbacks)` on CPython `3.14.6` (`arm64`) | `2,424 B` baseline `AsyncExitStack` allocation per in-flight request across the 3 stacks |
| Shallow memory per `_AsyncGeneratorContextManager` / `_GeneratorContextManager` | `48 B` | Measured via `sys.getsizeof` on CPython `3.14.6` (`arm64`) | Minimal wrapper overhead; `__aenter__` deletes `self.args`, `self.kwds`, and `self.func` immediately upon entry `(CPython 3.14.10 Lib/contextlib.py, _AsyncGeneratorContextManager.__aenter__)` |
| Shallow memory per `Dependant` (`slots=True`) | `184 B` | `(fastapi 0.162.0 fastapi/dependencies/models.py, Dependant)`, measured via `sys.getsizeof(Dependant())` on CPython `3.14.6` | `19` slot fields; the tree is built at route registration and reused across requests, except that `solve_dependencies()` rebuilds overridden sub-dependants via `get_dependant()` on every request while `dependency_overrides` is non-empty `(fastapi 0.162.0 fastapi/dependencies/utils.py, solve_dependencies)` |
| `_CALLABLE_CLASSIFICATION_CACHE_SIZE` | `4096` entries | `(fastapi 0.162.0 fastapi/dependencies/models.py, _CALLABLE_CLASSIFICATION_CACHE_SIZE)` | Max `@lru_cache` size for `_is_gen_callable_cached`, `_is_async_gen_callable_cached`, and `_is_coroutine_callable_cached`; dynamic per-request callables that exceed `4096` unique object IDs thrash the cache |
| Default threadpool `CapacityLimiter` (`__enter__`) | `40` tokens | `(anyio 4.16.0 anyio/_backends/_asyncio.py, AsyncIOBackend.current_default_thread_limiter)`, used by `run_in_threadpool(cm.__enter__)`; `anyio.to_thread.current_default_thread_limiter().total_tokens == 40` measured on CPython `3.14.6` | At most 40 synchronous `def` dependency `__enter__` calls execute concurrently per event loop unless overridden |
| Dedicated teardown `exit_limiter` (`__exit__`) | `1` token per call (`CapacityLimiter(1)`, `64 B` by `sys.getsizeof` on CPython `3.14.6`) | `(fastapi 0.162.0 fastapi/concurrency.py, contextmanager_in_threadpool)` | Every sync `def` generator dependency allocates a fresh 1-token limiter so `__exit__` never waits on the shared 40-token limiter and cannot deadlock behind `__enter__` calls |
| `async def` generator dependency enter + exit latency | `2.27 us/op` (second run `2.34 us/op`) | Mean of 10 trials x `2,000` iterations, each iteration `async with AsyncExitStack() as st: await st.enter_async_context(asynccontextmanager(async_dep)())`, Apple M3 Pro, CPython `3.14.6` | Pure coroutine suspension on the event loop with zero thread switches |
| `def` sync generator dependency enter + exit latency | `137.67 us/op` (`60.8x` slower; second run `142.87 us/op`, `61.1x`) | Same loop with `contextmanager_in_threadpool(contextmanager(sync_dep)())`, Apple M3 Pro, CPython `3.14.6` | Pays two `anyio.to_thread.run_sync` worker-thread round trips (`__enter__` and `__exit__`) plus `CapacityLimiter(1)` creation |

## Portability

| Runtime / component | Version / target | Behavior and verification status |
|---|---|---|
| CPython `3.12` | `3.12.13` (`macos-aarch64`) | Proven by lab (`PYTHONPATH=src uv run --no-project --python 3.12.13 --with fastapi==0.162.0 --with httpx==0.28.1 --with pytest==9.2.0 --with pytest-asyncio==1.4.1.post1 pytest <test> -q`: `8 passed in 0.19s`); the contract pin `3.12.16` is not installed on this machine and was not run |
| CPython `3.13` | `3.13.18` (`macos-aarch64`) | Proven by lab (same command with `--python 3.13.18`: `8 passed in 0.27s`); `fastapi.dependencies.models` switches to `from inspect import iscoroutinefunction` at `sys.version_info >= (3, 13)` `(fastapi 0.162.0 fastapi/dependencies/models.py, iscoroutinefunction)` |
| CPython `3.14` | `3.14.6` (workspace venv) and `3.14.10` (Homebrew) (`macos-aarch64`) | Proven by lab (`uv run --frozen pytest <test> -v` on `3.14.6`: `8 passed in 0.14s`; same `--no-project` command with `--python /opt/homebrew/bin/python3.14` on `3.14.10`: `8 passed in 0.20s`); `fastapi.dependencies.utils._get_signature` uses the `annotationlib.Format.FORWARDREF` fallback at `sys.version_info >= (3, 14)` `(fastapi 0.162.0 fastapi/dependencies/utils.py, _get_signature)` |
| CPython `3.15` (default GIL) | `3.15.2` (`macos-aarch64`) | Proven by lab (same `--no-project` command with `--python /opt/homebrew/bin/python3.15`: `8 passed in 0.19s`) |
| CPython `3.15t` (free-threaded) | `3.15.0b3+freethreaded` (`macos-aarch64`; `sys._is_gil_enabled()` is `False`) | Proven by lab (same `--no-project` command with `--python 3.15t`: `8 passed in 0.27s`); the contract pin `3.15.2t` is not installed on this machine and was not run; sync `def` generator dependencies running in `anyio.to_thread` execute without the GIL, requiring thread-safe state in shared objects |
| FastAPI `< 0.106.0` | `0.42.0` - `0.105.0` | One per-request `AsyncExitStack` in `scope["fastapi_astack"]`, entered in `FastAPI.__call__` through `0.73.x` and in `AsyncExitStackMiddleware` from `0.74.0` (PR `#4575`) `(fastapi 0.73.0 fastapi/applications.py, FastAPI.__call__)` `(fastapi 0.105.0 fastapi/middleware/asyncexitstack.py, AsyncExitStackMiddleware.__call__)`; `yield` teardown ran *after* the response and `BackgroundTasks`, so an `HTTPException` raised after `yield` could not become a response (fixed by PR `#10831`) |
| FastAPI `0.106.0` - `0.117.1` | `0.106.0` (PR `#10831`), `0.110.0` (PR `#11191`) | `AsyncExitStackMiddleware` removed; dependencies entered a local `async_exit_stack` inside `get_request_handler()` that exits before the `Response` is returned, so before `await response(scope, receive, send)` `(fastapi 0.110.0 fastapi/routing.py, get_request_handler)`; enabled `HTTPException` after `yield` but closed DB sessions before `StreamingResponse` streamed chunks or `BackgroundTasks` ran |
| FastAPI `0.118.0` - `0.120.4` | `0.118.0` (PR `#14099`) | Added FastAPI's own `request_response` with `scope["fastapi_inner_astack"]` wrapping `await response(scope, receive, send)` and restored `AsyncExitStackMiddleware` as `fastapi_middleware_astack` for files, so `StreamingResponse` kept resources open until streaming completed `(fastapi 0.118.0 fastapi/routing.py, request_response)`; no `scope="function"` option yet |
| FastAPI `0.121.0` - `0.162.0` | `0.121.0` (PR `#14262`), `0.121.1` (PR `#14301`), `0.162.0` (lab pin); the newest release is `0.162.2` (2026-11-07) `(as of 2026-11)` and was not run | Dual-stack architecture (`fastapi_function_astack` for `scope="function"` before response, `fastapi_inner_astack` for `scope="request"` after response and `BackgroundTasks`), proven in lab on `0.162.0` |
| ASGI servers (`Uvicorn`, `Hypercorn`, `Granian`) | `uvicorn 0.54.0`, `hypercorn 0.18.0`, `granian 2.8.7` | Both dependency `AsyncExitStack` instances live inside FastAPI's `request_response` ASGI callable, so the order does not depend on the server; the lab app's `/stream-order` produced the exact expected event list over loopback under `uvicorn 0.54.0` (HTTP/1.1) and `hypercorn 0.18.0` (HTTP/1.1 and cleartext HTTP/2 via `httpx` `http2=True`) on CPython `3.14.6`. `granian 2.8.7` (`granian --interface asgi --http 1` and `--http 2`, run via `uv run --frozen` from `labs/` on CPython `3.14.6`, macOS `27.2`) produced the same event list over HTTP/1.1 and over cleartext HTTP/2 (`httpx` `http2=True`, `http1=False`, `response.http_version == "HTTP/2"`); Granian serves only HTTP/1 and HTTP/2 `(granian 2.8.7 README.md, Features)`, so it has no HTTP/3 leg. HTTP/3 over QUIC was proven on `hypercorn 0.18.0` (`--quic-bind` with a self-signed certificate and `aioquic` installed) using an `aioquic 1.7.2` `H3Connection` client on CPython `3.14.6`: `/stream-order` returned `200` and the event list was identical `(hypercorn 0.18.0 hypercorn/protocol/h3.py)`. On client disconnect, `StreamingResponse.__call__` converts an `OSError` from `send()` into `ClientDisconnect` when ASGI `spec_version >= 2.4`, and below `2.4` races `listen_for_disconnect()` against the stream in a task group `(starlette 1.10.0 starlette/responses.py, StreamingResponse.__call__)` |
| Container environments | Debian Trixie, Alpine musl libc | Not run: no container runtime leg was executed for this note, so no behavior is claimed for musl or Debian beyond the macOS results above; nothing in the dependency teardown order depends on libc, but that is a design inference and not a measurement |

## Worked example

The lab [implementation](../labs/src/api_labs/f11_fastapi_internals_and_patterns/yield_dependencies_scope.py) and [test](../labs/tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py) verify the exact execution order of `scope="function"` and `scope="request"` dependencies relative to `StreamingResponse` chunks and `BackgroundTasks`, inspect the live `AsyncExitStack` callback counts in `request.scope`, and prove the exception-handling invariants for post-`yield` errors, swallowed exceptions, `DependencyScopeError`, cache-key scope isolation, and the request-time crash of a `Depends` callable decorated with `@contextmanager` or `@asynccontextmanager`.

### Implementation (`labs/src/api_labs/f11_fastapi_internals_and_patterns/yield_dependencies_scope.py`)

```python
"""FastAPI yield dependency AsyncExitStack teardown timing, exception handling, and scopes."""

from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse


class DomainConflictError(Exception):
    """Domain error translated into an HTTP 409 response by a yield dependency."""


@dataclass
class LifecycleRecorder:
    """Deterministic event log and resource state tracker for yield dependency tests."""

    events: list[str] = field(default_factory=list[str])
    active_resources: set[str] = field(default_factory=set[str])
    stack_callback_counts: dict[str, int] = field(default_factory=dict[str, int])

    def record(self, event: str) -> None:
        self.events.append(event)


def build_lifecycle_app(recorder: LifecycleRecorder) -> FastAPI:
    """Build a FastAPI app proving scope='function' vs scope='request' teardown order."""
    app = FastAPI()

    async def request_root_dep() -> AsyncIterator[str]:
        recorder.record("req_root:enter")
        recorder.active_resources.add("req_root")
        try:
            yield "req_root"
        finally:
            recorder.active_resources.discard("req_root")
            recorder.record("req_root:exit")

    async def request_child_dep(
        root: Annotated[str, Depends(request_root_dep, scope="request")],
    ) -> AsyncIterator[str]:
        recorder.record(f"req_child({root}):enter")
        recorder.active_resources.add("req_child")
        try:
            yield f"child_of_{root}"
        finally:
            # Parent resource must still be active during child LIFO teardown.
            parent_alive = "req_root" in recorder.active_resources
            recorder.active_resources.discard("req_child")
            recorder.record(f"req_child:exit(root_alive={parent_alive})")

    def function_guard_dep(
        root: Annotated[str, Depends(request_root_dep, scope="request")],
    ) -> Iterator[str]:
        recorder.record(f"func_guard({root}):enter")
        recorder.active_resources.add("func_guard")
        try:
            yield "func_guard"
        except DomainConflictError as exc:
            recorder.record("func_guard:caught_conflict")
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            recorder.active_resources.discard("func_guard")
            recorder.record("func_guard:exit")

    @app.get("/stream-order")
    async def stream_order(
        request: Request,
        background_tasks: BackgroundTasks,
        req_val: Annotated[str, Depends(request_child_dep, scope="request")],
        func_val: Annotated[str, Depends(function_guard_dep, scope="function")],
    ) -> StreamingResponse:
        for key in (
            "fastapi_middleware_astack",
            "fastapi_inner_astack",
            "fastapi_function_astack",
        ):
            stack: object = request.scope.get(key)
            callbacks: tuple[object, ...] = getattr(stack, "_exit_callbacks", ())
            if isinstance(stack, AsyncExitStack):
                recorder.stack_callback_counts[key] = len(callbacks)

        recorder.record(f"endpoint:return({req_val},{func_val})")
        background_tasks.add_task(
            lambda: recorder.record(
                f"background:run(req_alive={'req_child' in recorder.active_resources},"
                f"func_alive={'func_guard' in recorder.active_resources})"
            )
        )

        async def chunk_stream() -> AsyncIterator[bytes]:
            recorder.record(
                f"stream:chunk_1(req_alive={'req_child' in recorder.active_resources},"
                f"func_alive={'func_guard' in recorder.active_resources})"
            )
            yield b"chunk-1\n"
            recorder.record("stream:chunk_2")
            yield b"chunk-2\n"

        return StreamingResponse(chunk_stream(), media_type="text/plain")

    @app.get("/conflict")
    async def conflict_endpoint(
        _guard: Annotated[str, Depends(function_guard_dep, scope="function")],
    ) -> dict[str, str]:
        recorder.record("endpoint:raise_conflict")
        raise DomainConflictError("duplicate ledger key")

    return app


def build_post_yield_error_app(swallowed_log: list[str] | None = None) -> FastAPI:
    """Build an app contrasting post-yield HTTPException in function vs request scope."""
    app = FastAPI()

    async def function_commit_dep() -> AsyncIterator[str]:
        yield "tx_func"
        raise HTTPException(status_code=503, detail="function scope commit failed")

    async def request_commit_dep() -> AsyncIterator[str]:
        yield "tx_req"
        raise HTTPException(status_code=503, detail="request scope commit failed")

    async def swallowing_dep() -> AsyncIterator[str]:
        try:
            yield "swallow"
        except ValueError as exc:
            # Anti-pattern: catching and logging without re-raising suppresses the error.
            if swallowed_log is not None:
                swallowed_log.append(f"swallowed:{exc}")

    @app.get("/post-yield-function-error")
    async def post_yield_func(
        tx: Annotated[str, Depends(function_commit_dep, scope="function")],
    ) -> dict[str, str]:
        return {"tx": tx}

    @app.get("/post-yield-request-error")
    async def post_yield_req(
        tx: Annotated[str, Depends(request_commit_dep, scope="request")],
    ) -> dict[str, str]:
        return {"tx": tx}

    @app.get("/swallowed-error")
    async def swallowed_error(
        _s: Annotated[str, Depends(swallowing_dep, scope="request")],
    ) -> dict[str, str]:
        raise ValueError("unhandled failure")

    return app
```

### Test suite (`labs/tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py`)

```python
"""Unit tests for FastAPI yield dependency AsyncExitStack teardown timing and scopes."""

from collections.abc import AsyncGenerator, AsyncIterator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import DependencyScopeError, FastAPIError

from api_labs.f11_fastapi_internals_and_patterns.yield_dependencies_scope import (
    LifecycleRecorder,
    build_lifecycle_app,
    build_post_yield_error_app,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_yield_cleanup_ordering_across_function_and_request_scopes() -> None:
    recorder = LifecycleRecorder()
    app = build_lifecycle_app(recorder)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/stream-order")

    assert response.status_code == 200
    assert response.text == "chunk-1\nchunk-2\n"
    assert recorder.stack_callback_counts == {
        "fastapi_middleware_astack": 0,
        "fastapi_inner_astack": 2,
        "fastapi_function_astack": 1,
    }
    assert recorder.active_resources == set()
    assert recorder.events == [
        "req_root:enter",
        "req_child(req_root):enter",
        "func_guard(req_root):enter",
        "endpoint:return(child_of_req_root,func_guard)",
        "func_guard:exit",
        "stream:chunk_1(req_alive=True,func_alive=False)",
        "stream:chunk_2",
        "background:run(req_alive=True,func_alive=False)",
        "req_child:exit(root_alive=True)",
        "req_root:exit",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_yield_dependency_catches_endpoint_exception_and_translates_to_409() -> None:
    recorder = LifecycleRecorder()
    app = build_lifecycle_app(recorder)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/conflict")

    assert response.status_code == 409
    assert response.json() == {"detail": "duplicate ledger key"}
    assert recorder.active_resources == set()
    assert recorder.events == [
        "req_root:enter",
        "func_guard(req_root):enter",
        "endpoint:raise_conflict",
        "func_guard:caught_conflict",
        "func_guard:exit",
        "req_root:exit",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_yield_http_exception_in_function_scope_returns_503() -> None:
    app = build_post_yield_error_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/post-yield-function-error")

    assert response.status_code == 503
    assert response.json() == {"detail": "function scope commit failed"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_yield_http_exception_in_request_scope_fails_after_response_start() -> None:
    app = build_post_yield_error_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        with pytest.raises(
            RuntimeError,
            match=r"Caught handled exception, but response already started\.",
        ):
            await client.get("/post-yield-request-error")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_swallowing_exception_in_yield_dependency_raises_fastapi_error() -> None:
    swallowed_log: list[str] = []
    app = build_post_yield_error_app(swallowed_log=swallowed_log)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        with pytest.raises(FastAPIError, match=r"Response not awaited\."):
            await client.get("/swallowed-error")

    assert swallowed_log == ["swallowed:unhandled failure"]


@pytest.mark.unit
def test_request_scoped_dependency_rejects_function_scoped_sub_dependency() -> None:
    app = FastAPI()

    async def narrow_dep() -> AsyncIterator[str]:
        yield "narrow"

    async def wide_dep(
        sub: Annotated[str, Depends(narrow_dep, scope="function")],
    ) -> AsyncIterator[str]:
        yield sub

    with pytest.raises(
        DependencyScopeError,
        match=r'The dependency "wide_dep" has a scope of "request", '
        r'it cannot depend on dependencies with scope "function"\.',
    ):

        @app.get("/invalid-scope")
        async def invalid_endpoint(
            val: Annotated[str, Depends(wide_dep, scope="request")],
        ) -> dict[str, str]:
            return {"val": val}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_computed_scope_is_part_of_dependency_cache_key() -> None:
    app = FastAPI()
    events: list[str] = []
    counter = 0

    async def shared_gen() -> AsyncIterator[int]:
        nonlocal counter
        counter += 1
        instance_id = counter
        events.append(f"enter:{instance_id}")
        try:
            yield instance_id
        finally:
            events.append(f"exit:{instance_id}")

    @app.get("/dual-scope")
    async def dual_scope(
        func_scoped: Annotated[int, Depends(shared_gen, scope="function")],
        req_scoped: Annotated[int, Depends(shared_gen, scope="request")],
    ) -> dict[str, int]:
        events.append(f"endpoint:{func_scoped}:{req_scoped}")
        return {"func": func_scoped, "req": req_scoped}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/dual-scope")

    assert response.status_code == 200
    assert response.json() == {"func": 1, "req": 2}
    assert events == [
        "enter:1",
        "enter:2",
        "endpoint:1:2",
        "exit:1",
        "exit:2",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contextmanager_decorated_dependency_crashes_at_request_time() -> None:
    app = FastAPI()
    entered: list[str] = []

    @contextmanager
    def decorated_sync() -> Generator[str]:
        entered.append("sync")
        yield "never-injected"

    @asynccontextmanager
    async def decorated_async() -> AsyncGenerator[str]:
        entered.append("async")
        yield "never-injected"

    @app.get("/decorated-sync")
    async def sync_endpoint(
        val: Annotated[str, Depends(decorated_sync)],
    ) -> dict[str, str]:
        return {"val": val}

    @app.get("/decorated-async")
    async def async_endpoint(
        val: Annotated[str, Depends(decorated_async)],
    ) -> dict[str, str]:
        return {"val": val}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # inspect.unwrap() follows __wrapped__, so FastAPI classifies the helper as
        # a generator and wraps it a second time; the request then crashes.
        with pytest.raises(
            AttributeError,
            match=r"'_GeneratorContextManager' object has no attribute 'throw'",
        ) as sync_info:
            await client.get("/decorated-sync")
        with pytest.raises(
            TypeError,
            match=r"'_AsyncGeneratorContextManager' object is not an async iterator",
        ):
            await client.get("/decorated-async")

    assert isinstance(sync_info.value.__context__, TypeError)
    assert "is not an iterator" in str(sync_info.value.__context__)
    assert entered == []
```

### Captured execution output

Executed from `labs/` with `uv run --frozen pytest tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py -v -p no:cacheprovider` on `Darwin 27.2.0 arm64` (macOS `27.2`, Apple M3 Pro, CPython `3.14.6`, the workspace interpreter):

```text
============================= test session starts ==============================
platform darwin -- Python 3.14.6, pytest-9.2.0, pluggy-1.7.0 -- /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Yield-Dependencies-AsyncExitStack-Cleanup-Timing-and-Scope/verify/labs/.venv/bin/python
hypothesis profile 'default'
rootdir: /Users/shreejitverma/github/The-Distinguished-Engineer/build/api_notes/jobs/Yield-Dependencies-AsyncExitStack-Cleanup-Timing-and-Scope/verify/labs
configfile: pyproject.toml
plugins: platformdirs-4.14.5, schemathesis-4.33.1, cov-7.1.0, hypothesis-6.170.14, inline-snapshot-0.36.1, Faker-40.44.0, locust-2.47.0, typeguard-4.6.0, anyio-4.16.0, asyncio-1.4.1.post1, respx-0.23.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 8 items

tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_yield_cleanup_ordering_across_function_and_request_scopes PASSED [ 12%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_yield_dependency_catches_endpoint_exception_and_translates_to_409 PASSED [ 25%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_post_yield_http_exception_in_function_scope_returns_503 PASSED [ 37%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_post_yield_http_exception_in_request_scope_fails_after_response_start PASSED [ 50%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_swallowing_exception_in_yield_dependency_raises_fastapi_error PASSED [ 62%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_request_scoped_dependency_rejects_function_scoped_sub_dependency PASSED [ 75%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_computed_scope_is_part_of_dependency_cache_key PASSED [ 87%]
tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py::test_contextmanager_decorated_dependency_crashes_at_request_time PASSED [100%]

============================== 8 passed in 0.14s ===============================
```

## Pitfalls

> [!warning] Raising `HTTPException` or committing transactions after `yield` in `scope="request"`
> - Trigger: A generator dependency using the default `scope="request"` performs `await session.commit()` after `yield` and catches `IntegrityError` to raise `HTTPException(status_code=409)`.
> - Symptom: When the endpoint returns normally, `await response(scope, receive, send)` flushes `http.response.start` (`200 OK`) and `http.response.body` *before* `fastapi_inner_astack.__aexit__` executes the code after `yield`; when `HTTPException(409)` is then raised, Starlette's `wrap_app_handling_exceptions` sees `response_started == True` and crashes the ASGI task with `RuntimeError: Caught handled exception, but response already started.` `(starlette 1.10.0 starlette/_exception_handler.py, wrap_app_handling_exceptions)`.
> - Fix: Commit explicitly inside the endpoint or service layer before returning the response, or if commit-on-exit is mandatory and the resource is never used by `StreamingResponse` or `BackgroundTasks`, declare `Depends(get_db, scope="function")` so teardown runs inside `fastapi_function_astack` before `http.response.start`.
> - Source: the maintainers moved the exit code back and forth because of exactly this trade-off.
>   FastAPI `0.106.0` (2023-12-25, PR `#10831`) made `HTTPException` after `yield` work by running the exit code before the response is sent, and its release notes list "Using resources from dependencies with `yield` in background tasks is no longer supported" as a breaking change.
>   FastAPI `0.118.0` (2025-09-29, PR `#14099`) reverted to after-the-response teardown because, in the release notes' words, a `StreamingResponse` "would not be able to use that session while streaming data because the session would have already been closed" `(https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/release-notes.md)`.

> [!warning] Swallowing exceptions in `except` inside a `yield` dependency without re-raising
> - Trigger: Writing `try: yield session except Exception as exc: logger.error(exc)` in a `yield` dependency without a trailing `raise` or replacement `raise HTTPException(...)` (FastAPI PR `#11191`).
> - Symptom: `_AsyncGeneratorContextManager.__aexit__` or `_GeneratorContextManager.__exit__` returns `True`, causing `AsyncExitStack.__aexit__` to suppress the exception even though `await response(scope, receive, send)` was skipped; `request_response` detects `not response_awaited` and raises `FastAPIError: Response not awaited` (`500 Internal Server Error`), hiding the original traceback from error-reporting middleware `(fastapi 0.162.0 fastapi/routing.py, request_response)`.
> - Source: PR `#11191` (merged 2024-02-24) describes the underlying production defect as "a bug of growing unhandled memory/memory leak" for internal server errors, which is why `0.110.0` required a `yield` dependency that catches an exception to raise again.
> - Fix: Use `try: yield resource finally: cleanup()` for unconditional teardown, and whenever an `except` clause is present around `yield`, always re-raise the original exception (`raise`) or raise a domain/HTTP exception (`raise HTTPException(...) from exc`).

> [!warning] Decorating a FastAPI `yield` dependency with `@contextmanager` or `@asynccontextmanager`
> - Trigger: Applying `@contextlib.contextmanager` or `@contextlib.asynccontextmanager` directly to a function passed to `Depends(...)`.
> - Symptom: `@contextmanager` replaces the generator function with a `helper(*args, **kwds)` that *returns* a `_GeneratorContextManager`, so `inspect.isgeneratorfunction(helper)` is `False`, but `functools.wraps` sets `helper.__wrapped__`, and FastAPI's `_unwrapped_call()` follows it with `inspect.unwrap()`, so `_is_gen_callable()` (or `_is_async_gen_callable()`) still returns `True` `(fastapi 0.162.0 fastapi/dependencies/models.py, _is_gen_callable_cached)`.
>   `_solve_generator()` then wraps `helper` a second time, and the request crashes before the endpoint runs: the sync form raises `TypeError: '_GeneratorContextManager' object is not an iterator` in `__enter__`, which `contextmanager_in_threadpool` passes to `cm.__exit__`, which in turn raises `AttributeError: '_GeneratorContextManager' object has no attribute 'throw'`; the async form raises `TypeError: '_AsyncGeneratorContextManager' object is not an async iterator`.
>   The client receives `500 Internal Server Error`, and the decorated body never runs (proven by `test_contextmanager_decorated_dependency_crashes_at_request_time` on CPython `3.12.13` through `3.15.2`).
> - Fix: Never decorate `Depends` callables with `@contextmanager` or `@asynccontextmanager`; write a plain `def` or `async def` function containing a single `yield` statement and let `_solve_generator()` apply the decorator internally.

> [!warning] Blocking I/O in a synchronous `def` generator's `finally` block during task cancellation
> - Trigger: A client disconnects or an `anyio.CancelScope` cancels the request task while a synchronous `def` generator dependency (wrapped via `contextmanager_in_threadpool`) is active on `AsyncExitStack`.
> - Symptom: Because `contextmanager_in_threadpool` catches `except Exception as e:` rather than `except BaseException as e:` `(fastapi 0.162.0 fastapi/concurrency.py, contextmanager_in_threadpool)`, `asyncio.CancelledError` (`BaseException`) bypasses `anyio.to_thread.run_sync(cm.__exit__, ...)`; when CPython garbage-collects the generator frame, `gen.close()` throws `GeneratorExit` into the synchronous generator **on the main event-loop thread**, stalling the event loop if the `finally` block performs blocking network or disk I/O.
> - Fix: Prefer `async def` generator dependencies with async drivers (`asyncpg`, `httpx.AsyncClient`), or keep synchronous `finally` cleanup non-blocking (for example, returning a connection handle to an in-memory queue rather than executing synchronous socket writes).

> [!warning] Parameterless top-level `dependencies=[Depends(dep, scope="function")]` prior to FastAPI `0.121.1`
> - Trigger: Declaring `scope="function"` inside `dependencies=[Depends(dep, scope="function")]` on `@app.get(...)`, `APIRouter(...)`, or `FastAPI(...)` in FastAPI `0.121.0`.
> - Symptom: In `0.121.0`, `get_parameterless_sub_dependant()` omitted `scope=depends.scope` when calling `get_dependant()`, silently dropping `scope="function"` back to `scope="request"`; fixed in `0.121.1` (PR `#14301`) `(fastapi 0.162.0 fastapi/dependencies/utils.py, get_parameterless_sub_dependant)`.
> - Fix: Pin `fastapi >= 0.121.1` (`0.162.0` in this workspace) whenever using `scope="function"` on route-level, router-level, or app-level `dependencies=[...]`.

## Security and robustness

### Connection pool exhaustion via default `scope="request"` (`OWASP API4:2023 Unrestricted Resource Consumption`)

Because `scope="request"` is the default for all `yield` dependencies in FastAPI `>= 0.118.0`, every generator dependency held by an endpoint stays open in `scope["fastapi_inner_astack"]` until the entire HTTP response body and all attached `BackgroundTasks` finish executing `(fastapi 0.162.0 fastapi/routing.py, request_response)`.
If an endpoint uses a database session dependency (`yield session`) solely to authenticate a caller or load metadata, and then returns a slow `StreamingResponse`, a Server-Sent Events (`EventSourceResponse`) stream, or an in-process `BackgroundTasks` chain that calls an external webhook, that database connection remains checked out from SQLAlchemy's `QueuePool` for the entire duration of the stream or background task.
An attacker opening `N` slow-reading connections (a Slowloris-style response drain where `N` equals `pool_size + max_overflow`; the SQLAlchemy defaults give `5 + 10 = 15`) exhausts the database connection pool and causes all other endpoints on the service to fail with `QueuePool limit of size ... overflow ... reached` `(sqlalchemy 2.1.8 sqlalchemy/pool/impl.py, QueuePool)`.

Defense in depth requires three controls:
1. Explicitly set `Depends(get_db, scope="function")` whenever a `yield` dependency is not consumed by a `StreamingResponse` generator so the resource is returned to the pool inside `fastapi_function_astack` before the first byte of the response is sent.
2. Never run slow network I/O inside Starlette `BackgroundTasks` on routes that hold `scope="request"` resources; offload asynchronous post-response work to durable task queues (see [[BackgroundTasks-vs-Durable-Task-Queues-Celery-and-Dramatiq]]).
3. Enforce strict stream and response write timeouts at the reverse proxy and ASGI server layer.

### ContextVars propagation and tenant isolation across `AsyncExitStack` teardown (`OWASP API8:2023 Security Misconfiguration`)

Multi-tenant FastAPI services frequently use a `yield` dependency to bind a `contextvars.ContextVar` (such as `current_tenant_id` or a RLS session variable) via `token = tenant_var.set(tenant_id)` before `yield` and reset it via `tenant_var.reset(token)` in `finally:`.
If that tenant-context dependency is declared with `scope="function"`, `tenant_var.reset(token)` runs *before* `StreamingResponse` body iteration and `BackgroundTasks` execute, causing any streaming chunk generator or `BackgroundTask` that reads `tenant_var.get()` to see the default (unauthenticated or wrong-tenant) value.
Conversely, if a synchronous `def` generator dependency calls `tenant_var.set()` before `yield`, that mutation occurs inside a worker thread's copied `Context` inside `run_in_threadpool(cm.__enter__)` and is discarded when `cm.__enter__` returns, never propagating back to the event-loop task!
Both effects were observed with a throwaway script (not part of the lab) run as `uv run --frozen python cv.py` from `labs/` through `httpx.ASGITransport` on CPython `3.14.6`, macOS `27.2` (`fastapi 0.162.0`): a `scope="function"` `async def` dependency that set and reset a `ContextVar` showed the set value in the endpoint and the default in the `StreamingResponse` iterator and the `BackgroundTask`, and a `def` generator dependency's `set()` was invisible to the endpoint.
Any dependency that mutates a `ContextVar` must be an `async def` generator (so `__aenter__` and `__aexit__` execute directly on the request's event-loop task context) and must use `scope="request"` if `StreamingResponse` or `BackgroundTasks` read that `ContextVar`.

## The principal's view

FastAPI's `yield` dependency mechanism conflates two distinct architectural responsibilities into one syntax: **Dependency Injection (DI)** (wiring object graphs per request) and **Unit of Work / Resource Lifecycle Management** (bounding database transactions, locks, and network sessions).
The oscillation of FastAPI's teardown semantics across `0.105.0` (after response), `0.106.0` (before response, PR `#10831`), `0.118.0` (after response again, PR `#14099`), and `0.121.0` (dual `scope="function"` vs `scope="request"` stacks, PR `#14262`) is the direct symptom of that conflation:
- A transaction boundary (Unit of Work) wants to end **before** `http.response.start` is sent (`scope="function"`), because if `COMMIT` fails with a serialization or integrity error, the API must return `409 Conflict` or `503 Service Unavailable` rather than crashing after already flushing `200 OK` headers on the wire.
- A streaming cursor or temp-file handle wants to stay alive **until** the last `http.response.body` chunk is flushed (`scope="request"`), because closing it before `await response(scope, receive, send)` breaks `StreamingResponse`.

At organizational scale, a principal engineer sets three explicit standards for FastAPI services:
1. **Separate connection acquisition from transaction commit**: Use `yield` dependencies only to acquire and close/rollback sessions (`try: yield session finally: await session.close()`), never to auto-commit after `yield`, and require explicit `async with session.begin():` or `await uow.commit()` inside the endpoint or application service before returning the response payload.
2. **Mandate `async def` for all `yield` dependencies**: Synchronous `def` generator dependencies pay a roughly `61x` thread-switching tax (`137.7 us` vs `2.3 us` per enter and exit, measured on CPython `3.14.6`, Apple M3 Pro), allocate a `CapacityLimiter(1)` per invocation, cannot propagate `ContextVar` mutations to the event loop, and fall back to main-thread `GeneratorExit` finalization on task cancellation.
3. **Evaluate framework trade-offs**: When evaluating frameworks for new services, compare FastAPI's dual-`AsyncExitStack` model with Litestar (`2.25.0`), which separates handler-scoped dependency generators (`DependencyCleanupGroup`) from explicit lifecycle hooks (`after_request`, `after_response`) `(litestar 2.25.0 litestar/_kwargs/cleanup.py, DependencyCleanupGroup)` `(litestar 2.25.0 litestar/app.py, Litestar.__init__)`, or raw Starlette (`1.10.0`), where resource lifetimes are scoped explicitly with `async with` blocks inside the endpoint or pure ASGI middleware (see [[FastAPI-Architectural-Limits-and-Comparison-with-Litestar-and-Raw-Starlette]]).

## Interview questions

> [!question] L5: What is the difference between `Depends(dep, scope="function")` and `Depends(dep, scope="request")` for a `yield` dependency in FastAPI (`0.162.0`), and which one is the default?
>
> > [!success]- Answer
> > In FastAPI `0.162.0`, `scope="request"` is the default for any generator (`yield`) dependency when `scope` is omitted (`None`), as computed by `_get_computed_scope()` in `fastapi/dependencies/models.py`.
> > A `scope="function"` dependency is entered onto `scope["fastapi_function_astack"]`, whose `__aexit__` runs inside `request_response` immediately after the path operation function and response serialization return the `Response` object, before `await response(scope, receive, send)` sends `http.response.start`.
> > A `scope="request"` dependency is entered onto `scope["fastapi_inner_astack"]`, whose `__aexit__` runs only after `await response(scope, receive, send)` has finished sending the response body (including `StreamingResponse` chunks) and running all `BackgroundTasks`.
> >
> > Follow-up: Why did FastAPI change the default teardown timing in `0.106.0` and then revert it in `0.118.0`?

> [!question] L5: Why must you never decorate a FastAPI `yield` dependency function with `@contextlib.contextmanager` or `@contextlib.asynccontextmanager`?
>
> > [!success]- Answer
> > FastAPI identifies `yield` dependencies by calling `inspect.isgeneratorfunction` and `inspect.isasyncgenfunction` in `_is_gen_callable_cached()` and `_is_async_gen_callable_cached()`, on the callable itself and on `inspect.unwrap()` of it.
> > Decorating a generator with `@contextmanager` or `@asynccontextmanager` replaces it with a wrapper that returns a `_GeneratorContextManager` or `_AsyncGeneratorContextManager` instance, but `functools.wraps` leaves `__wrapped__` pointing at the original generator function, so FastAPI still classifies the wrapper as a generator dependency.
> > `_solve_generator()` then applies `contextmanager()` or `asynccontextmanager()` to the wrapper a second time, so entering it calls `next()` or `anext()` on a context manager object: the request fails with `TypeError` (sync: `'_GeneratorContextManager' object is not an iterator`, then `AttributeError: ... has no attribute 'throw'` from `cm.__exit__`; async: `'_AsyncGeneratorContextManager' object is not an async iterator`) and returns `500` before the endpoint or the dependency body runs.
> >
> > Follow-up: How can you use an existing class-based context manager inside a FastAPI `yield` dependency?

> [!question] L6 (Predict-the-output): Given an endpoint `/items` that injects `dep_req = Depends(gen_req, scope="request")` and `dep_func = Depends(gen_func, scope="function")` (where `gen_func` also depends on `gen_req`), adds a `BackgroundTask` logging `"bg"`, and returns a `StreamingResponse` yielding two chunks (`"c1"`, `"c2"`), what is the exact sequence of enter, endpoint, stream, background, and exit events?
>
> > [!success]- Answer
> > First, `solve_dependencies()` resolves `dep_req` and enters `gen_req` onto `scope["fastapi_inner_astack"]` (`req:enter`).
> > Second, `solve_dependencies()` resolves `dep_func`, reuses the cached `dep_req` value from `dependency_cache` (because both request `(gen_req, (), "request")`), and enters `gen_func` onto `scope["fastapi_function_astack"]` (`func:enter`).
> > Third, the endpoint runs, registers `"bg"`, and returns the `StreamingResponse` object (`endpoint:return`).
> > Fourth, `fastapi_function_astack.__aexit__` runs before `await response(scope, receive, send)`, executing `func:exit`.
> > Fifth, `StreamingResponse.__call__` streams `"c1"` and `"c2"` (`stream:c1`, `stream:c2`) and then awaits `self.background()` (`bg`).
> > Finally, `fastapi_inner_astack.__aexit__` unwinds `gen_req` (`req:exit`), giving the exact sequence: `["req:enter", "func:enter", "endpoint:return", "func:exit", "stream:c1", "stream:c2", "bg", "req:exit"]`.
> >
> > Follow-up: What would happen at startup if `gen_req` (`scope="request"`) instead depended on `gen_func` (`scope="function"`)?

> [!question] L6 (Debug-this-API): An engineer writes a `yield` dependency with default scope that runs `await db.commit()` after `yield` and converts `IntegrityError` into `raise HTTPException(status_code=409, detail="Conflict")`, yet whenever a unique constraint fails on commit in production, the client receives `200 OK` and Uvicorn logs `RuntimeError: Caught handled exception, but response already started.` - why does this happen, and how do you fix it?
>
> > [!success]- Answer
> > Because the dependency uses the default `scope="request"`, its post-`yield` `await db.commit()` runs inside `fastapi_inner_astack.__aexit__` *after* `await response(scope, receive, send)` has already sent the `http.response.start` (`200 OK`) and `http.response.body` ASGI messages to the client.
> > When `IntegrityError` is caught and re-raised as `HTTPException(409)`, Starlette's `wrap_app_handling_exceptions` catches the `HTTPException`, sees `response_started == True`, and raises `RuntimeError("Caught handled exception, but response already started.")` because HTTP status headers cannot be altered once flushed on the wire.
> > The preferred fix is to call `await db.commit()` explicitly inside the endpoint or service layer before returning the response so serialization and status selection happen after commit succeeds; alternatively, if the session is not needed during response streaming or `BackgroundTasks`, set `Depends(get_db, scope="function")` so `fastapi_function_astack.__aexit__` runs before `http.response.start`.
> >
> > Follow-up: If the endpoint itself raises `IntegrityError` before returning, does a `scope="request"` dependency catch it before `response_started` becomes `True`?

> [!question] L6: What happens in FastAPI (`0.162.0`) when an endpoint raises a `ValueError`, and a `yield` dependency catches `except ValueError:` and logs it without re-raising?
>
> > [!success]- Answer
> > In Python's context manager protocol (`PEP 343` and `PEP 492`), when a generator wrapped by `@contextmanager` or `@asynccontextmanager` catches an exception thrown into its `yield` point via `throw()` / `athrow()` and returns normally, `__exit__` / `__aexit__` catches `StopIteration` / `StopAsyncIteration` and returns `True`, suppressing the exception.
> > `AsyncExitStack.__aexit__` therefore suppresses the `ValueError`, and execution in `request_response` continues past `async with AsyncExitStack() as request_stack:` even though `await response(scope, receive, send)` was skipped.
> > FastAPI checks the boolean guard `if not response_awaited:` at the end of `request_response` and raises `FastAPIError("Response not awaited...")`, resulting in an unhandled `500 Internal Server Error`.
> >
> > Follow-up: Why did FastAPI `0.110.0` (PR `#11191`) remove automatic re-raising of swallowed exceptions in `yield` dependencies?

> [!question] L6: How does FastAPI execute synchronous `def` generator dependencies without deadlocking when AnyIO's 40-thread pool is completely saturated?
>
> > [!success]- Answer
> > In `fastapi/concurrency.py`, `contextmanager_in_threadpool()` executes `cm.__enter__` via `run_in_threadpool(cm.__enter__)`, which acquires a token from AnyIO's default 40-thread `CapacityLimiter`.
> > However, before entering the `try` block, `contextmanager_in_threadpool()` instantiates a dedicated `exit_limiter = CapacityLimiter(1)` and passes `limiter=exit_limiter` to `anyio.to_thread.run_sync(cm.__exit__, ...)`.
> > Because AnyIO limits concurrency per `CapacityLimiter` instance, giving `cm.__exit__` its own 1-token limiter guarantees that teardown (such as returning a database connection to `QueuePool`) always runs immediately in a worker thread even when all 40 default limiter tokens are blocked waiting for connections in `cm.__enter__`.
> >
> > Follow-up: What is the latency penalty of a synchronous `def` generator dependency compared to an `async def` generator dependency?

> [!question] L7+: What happens at the CPython and event-loop level when an in-flight FastAPI request using a synchronous `def` generator dependency is cancelled via `asyncio.CancelledError`?
>
> > [!success]- Answer
> > Since Python 3.8, `asyncio.CancelledError` inherits from `BaseException` rather than `Exception`.
> > In `fastapi/concurrency.py`, `contextmanager_in_threadpool()` wraps `yield await run_in_threadpool(cm.__enter__)` in `try ... except Exception as e: ... else: ...` without a `BaseException` handler or `finally` block.
> > When `AsyncExitStack.__aexit__` throws `CancelledError` into `contextmanager_in_threadpool` via `athrow()`, the `except Exception` clause does not match, so `cm.__exit__` is never dispatched to the worker threadpool via `anyio.to_thread.run_sync`.
> > Instead, when the `contextmanager_in_threadpool` frame unwinds and drops the last reference to the `_GeneratorContextManager`, CPython finalizes the suspended generator, whose finalizer calls `close()` and synchronously throws `GeneratorExit` into the `def` generator on the **main event-loop thread**, risking event-loop stalls if the `finally` block performs blocking I/O.
> >
> > Follow-up: How would you patch `contextmanager_in_threadpool` to prevent main-thread `GeneratorExit` finalization while still respecting task cancellation?

> [!question] L7+: If an endpoint injects the same generator callable `get_resource` twice in the same request - once with `Depends(get_resource, scope="function")` and once with `Depends(get_resource, scope="request")` - how many times does `get_resource` run, and how does FastAPI prevent a use-after-close bug?
>
> > [!success]- Answer
> > `get_resource` runs **twice**, creating two independent generator instances entered onto `fastapi_function_astack` and `fastapi_inner_astack` respectively.
> > FastAPI prevents aliasing between the two scopes in `_get_cache_key()` (`fastapi/dependencies/models.py`), which constructs the 3-tuple `(dependant.call, scopes_for_cache, _get_computed_scope(dependant=dependant) or "")`.
> > Because `(get_resource, (), "function")` and `(get_resource, (), "request")` produce distinct keys in `dependency_cache`, the `scope="request"` consumer never receives the `scope="function"` instance that closes before `StreamingResponse` and `BackgroundTasks` execute.
> >
> > Follow-up: Under what condition do two `Depends(get_resource)` declarations in the same request share a single cached instance?

## Related

Prerequisites: [[Dependency-Injection-Engine-DAG-Resolution-and-Use-Cache-Mechanics]]

Next: [[Yield-Dependencies-Inside-StreamingResponse-and-Resource-Lifecycle-Traps]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]], [[Async-Def-vs-Def-Endpoints-and-AnyIO-Threadpool-Limiter-Semantics]], [[Exception-Handling-in-FastAPI-HTTPException-vs-RequestValidationError]], [[BackgroundTasks-vs-Durable-Task-Queues-Celery-and-Dramatiq]], [[FastAPI-Architectural-Limits-and-Comparison-with-Litestar-and-Raw-Starlette]]

- Lab: `labs/tests/f11_fastapi_internals_and_patterns/test_yield_dependencies_asyncexitstack_cleanup_timing_and_scope.py`

## Further reading

- [FastAPI Documentation: Dependencies with yield](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/) - Official specification of `yield` dependencies, `try`/`except`/`finally` rules, and `scope="function"` versus `scope="request"` lifecycle diagrams.
- [FastAPI Pull Request #14262: Add support for dependencies with scopes](https://github.com/fastapi/fastapi/pull/14262) - Introduces `scope="function"` and `scope="request"`, `fastapi_function_astack`, `_get_computed_scope()`, and `DependencyScopeError` in FastAPI `0.121.0`.
- [FastAPI Pull Request #14099: Fix support for StreamingResponses with dependencies with yield](https://github.com/fastapi/fastapi/pull/14099) - Moves `await response(scope, receive, send)` back inside `fastapi_inner_astack` in FastAPI `0.118.0`.
- [FastAPI Pull Request #11191: Refactor dependencies with yield and except to require raising again](https://github.com/fastapi/fastapi/pull/11191) - Fixes memory leaks from unreleased exception tracebacks and adds the `FastAPIError("No response object was returned...")` guard in FastAPI `0.110.0`; the `response_awaited` form arrived in `0.118.0`.
- [FastAPI Pull Request #5122: Allow exit code for dependencies with yield to always execute](https://github.com/fastapi/fastapi/pull/5122) - Introduces `exit_limiter = CapacityLimiter(1)` in `contextmanager_in_threadpool` to prevent threadpool starvation deadlocks during teardown in FastAPI `0.82.0`.
- [Python Standard Library: contextlib - Utilities for with-statement contexts](https://docs.python.org/3/library/contextlib.html) - Reference implementation semantics for `AsyncExitStack`, `@contextmanager`, and `@asynccontextmanager`.
- [PEP 343: The "with" Statement](https://peps.python.org/pep-0343/) and [PEP 525: Asynchronous Generators](https://peps.python.org/pep-0525/) - Define the synchronous and asynchronous generator context management protocols (`throw()`, `athrow()`, `aclose()`, and boolean exception suppression).
- [RFC 9112 Section 2.1: HTTP/1.1 Message Format](https://www.rfc-editor.org/rfc/rfc9112.html#section-2.1) - Explains why the HTTP status line and header section must precede the message body on the wire, making status codes immutable once `http.response.start` is sent.
