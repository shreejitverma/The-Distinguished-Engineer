---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/starlette/responses.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/starlette/background.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/starlette/concurrency.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/starlette/_utils.py"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/docs/responses.md"
  - "https://raw.githubusercontent.com/Kludex/starlette/1.10.0/docs/background.md"
  - "https://raw.githubusercontent.com/django/asgiref/main/specs/www.rst"
  - "https://raw.githubusercontent.com/django/asgiref/main/specs/asgi.rst"
  - "https://raw.githubusercontent.com/django/asgiref/main/docs/extensions.rst"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.rfc-editor.org/rfc/rfc9112.txt"
  - "https://www.rfc-editor.org/rfc/rfc9113.txt"
  - "https://www.rfc-editor.org/rfc/rfc9114.txt"
  - "https://www.rfc-editor.org/rfc/rfc8259.txt"
  - "https://www.rfc-editor.org/rfc/rfc6266.txt"
  - "https://www.rfc-editor.org/rfc/rfc8187.txt"
  - "https://docs.python.org/3/library/io.html"
  - "https://api.github.com/advisories/GHSA-7f5h-v6xp-fcq8"
  - "https://api.github.com/advisories/GHSA-v5gw-mw7f-84px"
  - "https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/"
  - "https://github.com/agronholm/anyio/pull/1314"
  - "https://github.com/advisories/GHSA-7f5h-v6xp-fcq8"
  - "https://github.com/advisories/GHSA-v5gw-mw7f-84px"
---

# Starlette Response Internals: JSONResponse, StreamingResponse Client-Disconnect Listening, and FileResponse Chunked File I/O

> [!summary] Claim
> A Starlette response is an ASGI app that renders its headers once and then pushes `http.response.body` messages; `StreamingResponse` and `FileResponse` learn that the client left either by racing a `receive()` listener against the sender in one task group (ASGI HTTP spec below 2.4, which is every pinned server as of 2026-11) or by catching `OSError` from `send()` (spec 2.4 and later), and that one branch decides whether your generator is cancelled, whether it is ever closed, and whether `background` runs at all.

Part of [ASGI and Starlette Internals](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: `Response.__call__` sends `http.response.start` then one `http.response.body` and awaits `background`; `StreamingResponse` sends one body message per chunk plus an empty final one, and `FileResponse` reads 65536-byte chunks through worker threads (starlette 1.10.0 starlette/responses.py).
- Contract rule: when `scope["asgi"]["spec_version"]` is below `2.4` (or missing, which means `2.0`), Starlette runs `listen_for_disconnect(receive)` and `stream_response(send)` in one AnyIO task group and cancels the other when either finishes; at `2.4` and above it skips the listener and converts `OSError` from `send()` into `ClientDisconnect`.
- Trap: cancellation only reaches your async generator if it is awaiting inside its own body; if it is parked at `yield` while `send()` blocks, Starlette never calls `aclose()`, so its `finally` waits for garbage collection, and on the spec 2.4 path `background` never runs.
- Portability: Uvicorn 0.54.0 and Granian 2.8.7 advertise spec `2.3` and Hypercorn 0.18.0 advertises `2.1`, so all three take the listener path, and only Granian offers `http.response.pathsend`, which turns `FileResponse` into a single zero-copy hand-off (measured here).
- When not to use it: `BackgroundTask` is not a job queue (it runs in the request task, after the bytes left, with no retry), and `StreamingResponse` over a sync iterator or an `open()` file costs one thread hop per chunk from a 40-token pool.

## Mental model

```mermaid
sequenceDiagram
    participant S as Server (spec 2.3)
    participant L as listen_for_disconnect
    participant W as stream_response
    participant G as body_iterator
    W->>S: http.response.start (status, raw_headers)
    loop each chunk
        W->>G: __anext__()
        G-->>W: chunk (str encoded with charset)
        W->>S: http.response.body more_body=True
    end
    L->>S: receive()
    S-->>L: http.request (discarded)
    L->>S: receive()
    Note over S: client closes TCP
    S-->>L: http.disconnect
    L->>W: task_group.cancel_scope.cancel()
    Note over W,G: CancelledError lands where W is suspended:<br/>inside G (G's finally runs) or inside send (G stays parked)
    W-->>S: return; then await background()
```

Two coroutines share one task group: the writer pulls chunks from the iterator and pushes them to `send`, while the listener drains `receive()` and throws away everything except `http.disconnect`.
Whichever returns first cancels the group's scope, so a finished stream stops the listener and a vanished client stops the stream.
The cancellation is delivered at the writer's current suspension point, which is the detail that decides cleanup: a generator awaiting a database or an upstream API receives `CancelledError` in its own frame and unwinds, but a generator parked at `yield` while the writer is stuck in `send()` is never resumed and never closed.
After the group exits without error, `__call__` awaits `background`, so on this path background work runs even for a client that left.

## How it works

### The base `Response`: render once, frame once

`Response.__init__` calls `render(content)` and then `init_headers(headers)`, so the body bytes and the header list both exist before any ASGI message is sent (starlette 1.10.0 starlette/responses.py, Response.__init__).
`render` maps `None` to `b""`, returns `bytes` unchanged, casts a C-contiguous `memoryview` to format `B` without copying and copies a non-contiguous one with `tobytes()`, and encodes anything else with `self.charset`, which defaults to `utf-8` (starlette 1.10.0 starlette/responses.py, Response.render).
`init_headers` lowercases and latin-1 encodes caller headers, then appends `content-length` only if the caller did not supply one and the status is not `1xx`, `204`, or `304` (starlette 1.10.0 starlette/responses.py, Response.init_headers).
That rule tracks the specification: a server MUST NOT send `Content-Length` in a `1xx` or `204` response, and MAY send it in a `304` only if it equals the length of the `200` body it stands for (RFC 9110 Section 8.6), so omitting it on `304` is the safe choice.
A caller-supplied `Content-Length` wins without any check, so `Response(b"abc", headers={"Content-Length": "999"})` produces a header that lies about the framing; the lab asserts exactly that header list.
`init_headers` appends `; charset=utf-8` only when the media type starts with `text/` and has no `charset=` already, so `application/json` stays bare, which matches the JSON media type registration, where no `charset` parameter is defined (RFC 8259 Section 11).
`__call__` sends `http.response.start` with `self.raw_headers`, one `http.response.body` with the whole body and no `more_body` key (which the ASGI spec defaults to `False`), and then awaits `self.background` (starlette 1.10.0 starlette/responses.py, Response.__call__).
For a `websocket` scope it first wraps `send` so the two messages go out as `websocket.http.response.start` and `websocket.http.response.body`, the WebSocket denial response; the handshake side is in [[ASGI-WebSockets-Lifecycle-Subprotocols-and-Backpressure]].

### `JSONResponse`: `json.dumps` with four fixed choices

`JSONResponse.render` is `json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")).encode("utf-8")` (starlette 1.10.0 starlette/responses.py, JSONResponse.render).
`separators=(",", ":")` removes every optional space, so `{"name": "Zo\u00eb", "tags": ["a", "b"]}` renders to the 32-byte body `{"name":"Zo\xc3\xab","tags":["a","b"]}`, 31 characters long; the lab asserts both numbers, and `Content-Length` counts the bytes.
`ensure_ascii=False` emits UTF-8 directly instead of `\u00eb` escapes, which RFC 8259 requires for JSON exchanged between systems that are not a closed ecosystem (RFC 8259 Section 8.1).
`allow_nan=False` makes `json.dumps` raise `ValueError: Out of range float values are not JSON compliant: nan`, because "Numeric values that cannot be represented in the grammar below (such as Infinity and NaN) are not permitted" (RFC 8259 Section 6).
Because rendering happens in `__init__`, the error surfaces in the endpoint before `http.response.start`, so the exception middleware can still answer `500`; contrast this with a streaming error, which arrives after the headers (see [[Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware]]).
FastAPI 0.162.0 bypasses `JSONResponse` entirely on its fast path: when a route has a response field and no custom `response_class`, it serializes with Pydantic's `TypeAdapter.dump_json` (fastapi 0.162.0 fastapi/_compat/v2.py, ModelField.serialize_json) and wraps the bytes in a plain `Response(media_type="application/json")` (fastapi 0.162.0 fastapi/routing.py, get_request_handler).
The two paths disagree on `NaN`: the fast path answers `200` with `{"x":null}`, while returning `JSONResponse({"x": nan})` from the same app raises `ValueError`; the lab asserts both.

### `StreamingResponse`: one message per chunk, no length

`StreamingResponse.__init__` never calls `render`, so `self.body` does not exist and `init_headers` adds no `content-length` (starlette 1.10.0 starlette/responses.py, StreamingResponse.__init__).
The ASGI spec lets the server choose framing for that case: servers "may opt to use `Transfer-Encoding: chunked` if the application presents a response that has no `Content-Length` set" (ASGI HTTP spec, Response Start), and on HTTP/1.1 a message without `Content-Length` or `Transfer-Encoding` would be close-delimited (RFC 9112 Section 6.3); the chunk grammar is in [[HTTP-1-1-Framing-Keep-Alive-and-Chunked-Encoding]].
All three servers measured here answered with `transfer-encoding: chunked` on HTTP/1.1.
An `AsyncIterable` is used as is; anything else is wrapped in `iterate_in_threadpool`, which calls `next()` through `anyio.to_thread.run_sync` once per item and converts `StopIteration` into a private `_StopIteration` because `StopIteration` cannot cross the thread boundary (starlette 1.10.0 starlette/concurrency.py, iterate_in_threadpool and _next).
Each of those hops borrows a token from AnyIO's default thread limiter, a `CapacityLimiter(40)` shared with every other `run_in_threadpool` call, sync endpoint, sync dependency, and `FileResponse` read in the process (anyio 4.16.0 anyio/_backends/_asyncio.py, AsyncIOBackend.current_default_thread_limiter).
`stream_response` sends `http.response.start`, then for each chunk encodes non-`bytes`, non-`memoryview` values with `self.charset` and sends them with `more_body: True`, then sends `{"body": b"", "more_body": False}` (starlette 1.10.0 starlette/responses.py, StreamingResponse.stream_response).
So a two-chunk stream is three body messages, `[(1, True), (2, True), (0, False)]` in the lab, and a `bytearray` chunk fails with `AttributeError: 'bytearray' object has no attribute 'encode'` because the `isinstance` check names `bytes | memoryview` only.
Each `send()` is a backpressure point: Uvicorn's `send` awaits `self.flow.drain()` while the transport has paused writing (uvicorn 0.54.0 uvicorn/protocols/http/h11_impl.py, RequestResponseCycle.send), so a slow reader slows the generator rather than filling memory.

### The disconnect branch, verbatim

This is `StreamingResponse.__call__` at lines 262 to 288 of the installed release, byte-identical to the 1.10.0 tag:

```python
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            send = self._wrap_websocket_denial_send(send)
            await self.stream_response(send)
            if self.background is not None:
                await self.background()
            return

        spec_version = tuple(map(int, scope.get("asgi", {}).get("spec_version", "2.0").split(".")))

        if spec_version >= (2, 4):
            try:
                await self.stream_response(send)
            except OSError:
                raise ClientDisconnect()
        else:
            async with create_collapsing_task_group() as task_group:

                async def wrap(func: Callable[[], Awaitable[None]]) -> None:
                    await func()
                    task_group.cancel_scope.cancel()

                task_group.start_soon(wrap, partial(self.stream_response, send))
                await wrap(partial(self.listen_for_disconnect, receive))

        if self.background is not None:
            await self.background()
```

The switch exists because the ASGI HTTP spec changed the disconnect contract at version 2.4: "If `send()` is called on a closed connection the server should raise a server-specific subclass of `OSError`. This is not guaranteed, however, especially on older ASGI server implementations (it was introduced in spec version 2.4)" (ASGI HTTP spec, Disconnected Client - send exception).
Before 2.4 the only reliable signal is the `http.disconnect` event, which is "Sent to the application if receive is called after a response has been sent or after the HTTP connection has been closed" (ASGI HTTP spec, Disconnect - receive event), so Starlette must keep a `receive()` call outstanding for the whole stream.
That is what the listener costs: a second task, and a loop that discards every `http.request` message it reads (starlette 1.10.0 starlette/responses.py, StreamingResponse.listen_for_disconnect).
A generator that reads `request.stream()` while the response is streaming therefore competes with the listener for the same `receive()` messages, and whatever the listener wins is discarded; read the request body before returning the response.
Uvicorn needs the listener because its `send()` does not raise after a disconnect: `connection_lost` sets `cycle.disconnected` and wakes `message_event`, and `send` then returns silently (uvicorn 0.54.0 uvicorn/protocols/http/h11_impl.py, H11Protocol.connection_lost and RequestResponseCycle.send; the same `if self.disconnected: return` is in httptools_impl.py).
Without the listener, a generator streaming into Uvicorn after the client left would keep producing into a black hole until it ended on its own.
`create_collapsing_task_group` unwraps a single-exception `BaseExceptionGroup` and re-raises the inner exception with its cause, so an error inside the stream surfaces as itself rather than as an `ExceptionGroup` (starlette 1.10.0 starlette/_utils.py, create_collapsing_task_group).
The spec 2.4 branch has no listener at all (the lab asserts `receive_calls == 0`), and a disconnect turns into a raised `ClientDisconnect` that skips the `background` call below it.

### Where the cancellation lands

AnyIO cancellation is delivered at the next `await` of the cancelled task, so the outcome depends on what `stream_response` is awaiting when the listener fires.
If it is inside `self.body_iterator.__anext__()` and the generator is awaiting I/O, `CancelledError` is raised in the generator frame, its `finally` runs at once, and the lab sees exactly three chunks produced, three sent, and zero dropped for a client that left after the third.
If it is inside `send()` (a full socket buffer, a proxy that stopped reading), the generator is suspended at `yield` and is never resumed; Starlette does not call `aclose()`, so the `finally` runs only when the response object is collected and the event loop's async-generator finalizer schedules `aclose()`.
The lab's `ClosingStreamingResponse` closes the iterator in a `finally` around `stream_response`, under `anyio.CancelScope(shield=True)` so the `await` can complete inside the cancelled scope, and the lab shows `["finally", "background"]` for it against `"finally" not in events` for the stock class.
A sync iterator adds a second layer: a worker thread cannot be interrupted, and `anyio.to_thread.run_sync` defaults to `abandon_on_cancel=False`, which ignores host-task cancellation "until the operation has completed in the worker thread" (anyio 4.16.0 anyio/to_thread.py, run_sync), so one slow `next()` delays disconnect handling by its full duration.
When that sync generator is finally collected, its `finally` runs on whichever thread drops the last reference, which here is the event-loop thread, not a worker, as the lab proves with `threading.get_ident()`; blocking cleanup there stalls every other request on the loop.

### `FileResponse`: stat, then 64 KiB reads through threads

`FileResponse.__init__` guesses the media type from `filename` or `path` with `mimetypes.guess_type` (falling back to `application/octet-stream`), sets `accept-ranges: bytes`, builds `Content-Disposition` when `filename` is given, and sets stat headers only if a `stat_result` was passed in (starlette 1.10.0 starlette/responses.py, FileResponse.__init__).
`Content-Disposition` uses `filename="..."` when `urllib.parse.quote(filename)` leaves the name unchanged and `filename*=utf-8''<percent-encoded>` otherwise, so `r\u00e9sum\u00e9.pdf` (Python escapes) becomes `filename*=utf-8''r%C3%A9sum%C3%A9.pdf` and `a"b.txt` becomes `filename*=utf-8''a%22b.txt`; that is the `filename*` encoding that RFC 6266 Section 4.3 takes from RFC 5987 (since obsoleted by RFC 8187, whose `ext-value` is the same form) for names outside ISO-8859-1, although Starlette sends it alone rather than alongside an ASCII `filename` fallback as RFC 6266 Section 4.3 recommends.
At call time, if no `stat_result` was given, `os.stat` runs in a worker thread, a missing file raises `RuntimeError("File at path ... does not exist.")`, and a non-regular file raises `RuntimeError("... is not a file.")` (starlette 1.10.0 starlette/responses.py, FileResponse.__call__).
`set_stat_headers` uses `setdefault` for `content-length` (`st_size`), `last-modified` (`email.utils.formatdate(st_mtime, usegmt=True)`), and `etag`, which is `'"' + md5(f"{st_mtime}-{st_size}") + '"'`: a strong-looking validator derived from metadata, not from content (starlette 1.10.0 starlette/responses.py, FileResponse.set_stat_headers); the lab recomputes it with `starlette_etag`.
`_handle_simple` opens the file with `anyio.open_file` and no `limiter`, whose `read` is `to_thread.run_sync(self._fp.read, size, limiter=self._limiter)` with `_limiter` left `None`, so every read uses the default limiter (anyio 4.16.0 anyio/_core/_fileio.py, AsyncFile.read), and loops `chunk = await file.read(self.chunk_size)` with `more_body = len(chunk) == self.chunk_size` (starlette 1.10.0 starlette/responses.py, FileResponse._handle_simple).
That termination test means a file whose size is an exact multiple of 65536 ends with an extra empty message: 131072 bytes go out as `(65536, True), (65536, True), (0, False)`, 65537 bytes as `(65536, True), (1, False)`, and an empty file as `(0, False)`; the lab checks six sizes against `file_chunk_plan`.
It also means the loop reads until a short read, not until `Content-Length` bytes, so a file that grew after `stat` sends more bytes than the header promised; the lab passes a stale `stat_result` and gets `content-length: 10` with 70000 body bytes.
The file is closed in a `finally` under `anyio.CancelScope(shield=True)`, with the source comment "Closing must finish even when the transfer is cancelled" (starlette 1.10.0 starlette/responses.py, FileResponse._open_file).
A `TODO` above that wrapper says to remove it "once minimum AnyIO includes https://github.com/agronholm/anyio/pull/1314"; the pinned AnyIO 4.16.0 already shields inside `AsyncFile.aclose`, which runs `self._fp.close` in a worker thread under `CancelScope(shield=True)` (anyio 4.16.0 anyio/_core/_fileio.py, AsyncFile.aclose), so the outer shield matters only for older AnyIO releases that Starlette still supports.
The disconnect logic mirrors `StreamingResponse` but is inlined: for an `http` scope that is not `HEAD`, not using `pathsend`, and below spec 2.4, the send runs in a task group while the parent loops on `receive()` until `http.disconnect`, then cancels (starlette 1.10.0 starlette/responses.py, FileResponse.__call__); in the lab a client that leaves after two chunks of a 4 MiB file receives two body messages and no further `send()` is attempted, although the read already in flight in a worker thread is allowed to finish first.
Unlike `StreamingResponse`, the spec 2.4 path of `FileResponse` does not catch `OSError`, so a server that raises it propagates `OSError` itself.
`HEAD` sends the headers, including the full `content-length`, and one empty body message, with no file open and no listener, which is the RFC 9110 Section 8.6 rule that a `HEAD` `Content-Length` must equal what `GET` would send.
When the scope's `extensions` contain `http.response.pathsend`, Starlette sends `{"type": "http.response.pathsend", "path": str(self.path)}` instead of body messages and skips the listener; the extension "cannot be mixed with `http.response.body`" and leaves `Content-Type` and `Content-Length` to the application (ASGI extensions, Path Send).
The extension asks for "the absolute file path", but Starlette sends `str(self.path)` unchanged, so a relative path is resolved by the server against its own working directory.
`Range` requests take separate single-range and multipart paths that never use `pathsend`; their parsing, merging, and `If-Range` rules are in [[Range-Requests-Partial-Content-and-206-Semantics]].

### `BackgroundTask` and `BackgroundTasks`

`BackgroundTask.__init__` stores the callable and its arguments and records `is_async_callable(func)`, which unwraps `functools.partial` and accepts objects whose `__call__` is a coroutine function (starlette 1.10.0 starlette/background.py and starlette/_utils.py, is_async_callable).
Calling it awaits the coroutine directly or runs a sync function through `run_in_threadpool`, the same 40-token limiter (starlette 1.10.0 starlette/background.py, BackgroundTask.__call__).
`BackgroundTasks.__call__` awaits each task in list order, and the documentation states the consequence: "In case one of the tasks raises an exception, the following tasks will not get the opportunity to be executed" (Starlette 1.10.0 docs/background.md); the lab records `["final-body", "first", "boom"]` and no `"never"`.
"Background" means after the last body message, inside the same ASGI call: the server has the complete response, but the application coroutine has not returned, so middleware `finally` blocks, request-scoped context, and server shutdown accounting all wait for it.
Uvicorn's shutdown waits for those tasks up to `timeout_graceful_shutdown` (default `None`, which waits indefinitely) and then logs `Cancel %s running task(s), timeout graceful shutdown exceeded` (uvicorn 0.54.0 uvicorn/server.py, Server.shutdown), so a slow background task is a deploy-time drain cost.
FastAPI attaches its injected `BackgroundTasks` to a returned `Response` only if that response's `background` is `None` (fastapi 0.162.0 fastapi/routing.py, get_request_handler), so returning `Response(background=...)` silently replaces the tasks added through dependency injection.

## Numbers and limits

| Quantity | Value | Source |
| --- | --- | --- |
| `FileResponse.chunk_size` | 65536 bytes (`64 * 1024`) | starlette 1.10.0 starlette/responses.py |
| `FileResponse` body messages for 131072 bytes | 3, the last empty | lab `test_file_chunk_plan_adds_an_empty_tail_on_exact_multiples` |
| `FileResponse.max_ranges` | 100 | starlette 1.10.0 starlette/responses.py; see the Range note |
| AnyIO default thread limiter | 40 tokens, shared by every `to_thread.run_sync` caller that passes no `limiter` | anyio 4.16.0 anyio/_backends/_asyncio.py, AsyncIOBackend.current_default_thread_limiter |
| Thread hops for a sync iterator of N items | N + 1 (one per `next()`, including the one that ends it) | starlette 1.10.0 starlette/concurrency.py, iterate_in_threadpool |
| `JSONResponse` for `{"name": "Zo\u00eb", "tags": ["a", "b"]}` | 32 bytes, 31 characters, `content-length: 32` | lab |
| `ETag` length | 34 characters: 32 hex digits of MD5 plus two quotes | starlette 1.10.0 source; lab |
| Chunks produced after a client leaves (in-process, spec 2.3, async producer) | 0 beyond the last one sent: 3 produced, 3 sent, 0 dropped | lab `test_pre_2_4_disconnect_cancels_generator_and_still_runs_background` |
| Body sends after a client leaves (in-process, spec 2.3, 4 MiB file) | 0: 2 sent, 0 dropped | lab `test_file_response_stops_reading_after_disconnect` |
| Chunks produced before `finally` over a real socket (1 KiB chunks every 10 ms, client reads 4096 bytes and closes) | 4 on Uvicorn 0.54.0, Hypercorn 0.18.0, and Granian 2.8.7 | server check below |
| `asgi.spec_version` advertised for HTTP | Uvicorn 0.54.0 `2.3`, Hypercorn 0.18.0 `2.1`, Granian 2.8.7 `2.3` | server check below; uvicorn 0.54.0 h11_impl.py and hypercorn 0.18.0 protocol/http_stream.py source |
| `httpx.ASGITransport` latency with a 0.3 s background task | 0.303 s, because the transport awaits the whole app call | measured once, script below |
| Uvicorn flow-control constant | `HIGH_WATER_LIMIT = 65536`, used for request-body buffering | uvicorn 0.54.0 uvicorn/protocols/http/flow_control.py |

The server check ran on macOS 27.3 (arm64, Darwin 27.3.0) with CPython 3.14.6 from the lab virtual environment, run with `labs/.venv/bin/python srvcheck.py` (2026-11-14); it is not part of the lab because it binds sockets and spawns processes.
`srvapp.py` streams `b"x" * 1024` every 10 ms from an endless generator, logs `scope["asgi"]["spec_version"]`, the generator's `finally`, the background task, and how the app returned; `srvcheck.py` starts each server on a free loopback port, sends `GET / HTTP/1.1`, reads at least 4096 bytes, closes the socket, waits one second, requests `/file`, and stops the server.
Its output, one header summary and one log line per server:

```text
uvicorn ['HTTP/1.1 200 OK', 'transfer-encoding: chunked']
    spec 2.3 http 1.1 | finally after 4 chunks | bg ran | app returned normally
hypercorn ['HTTP/1.1 200 ', 'Transfer-Encoding: chunked']
    spec 2.1 http 1.1 | finally after 4 chunks | bg ran | app returned normally
granian ['HTTP/1.1 200 OK', 'transfer-encoding: chunked']
    spec 2.3 http 1.1 | finally after 4 chunks | bg ran | app returned normally
```

The `/file` request of the same run served an 856-byte file through `FileResponse` and logged the scope's extension names and the message types the app sent:

```text
uvicorn ['HTTP/1.1 200 OK', 'content-length: 856'] body 856
    [] | http.response.start | http.response.body
hypercorn ['HTTP/1.1 200 ', 'content-length: 856'] body 856
    [] | http.response.start | http.response.body
granian ['HTTP/1.1 200 OK', 'content-length: 856'] body 856
    ["http.response.pathsend", "websocket.http.response"] | http.response.start | http.response.pathsend
```

The `ASGITransport` timing came from one run of an endpoint returning `PlainTextResponse("ok", background=BackgroundTask(slow_bg))` with `await asyncio.sleep(0.3)` in `slow_bg`, timed around `client.get("/")` with `time.perf_counter()` on the same machine and interpreter; it printed `asgitransport waited for bg ok 0.303`.

## Portability

| Dimension | Behavior | Evidence |
| --- | --- | --- |
| CPython 3.12 | 31 passed on 3.12.13 (macOS); 3.12.16 was not available here | `uv run --frozen --isolated --python 3.12 pytest <lab>` |
| CPython 3.13.18 | 31 passed | `uv run --frozen --isolated --python 3.13 pytest <lab>` |
| CPython 3.14 | 31 passed on 3.14.6 (lab virtual environment) and on 3.14.10 | `uv run --frozen pytest <lab>`; `uv run --frozen --isolated --python 3.14.10 pytest <lab>` |
| CPython 3.15.2 | 31 passed | `uv run --frozen --isolated --python 3.15.2 pytest <lab>`; plain `--python 3.15` picked 3.15.0b3, where `hypothesis` failed to import `hypothesis._native` before collection |
| CPython 3.15 free-threaded | 31 passed on 3.15.0b3t with `sys._is_gil_enabled()` `False`, in a reduced environment; 3.15.2t was not available, and the full sync fails because `psycopg-binary==3.3.8` has no free-threaded wheel | `uv run --no-project --python 3.15t --with starlette==1.10.0 --with fastapi==0.162.0 ...` with `PYTHONPATH=src` |
| Free-threading and the listener race | the lab's event order did not change without the GIL, as expected: both coroutines run on one event-loop thread, and only the worker-thread hops run in parallel | same run |
| ASGI HTTP spec `2.0` to `2.3` (or key missing) | listener path; `background` runs after a disconnect | lab, parametrized over `None`, `"2.0"`, `"2.3"` |
| ASGI HTTP spec `2.4`, `2.5` | no listener; `OSError` -> `ClientDisconnect`; `background` skipped | lab, parametrized over `"2.4"`, `"2.5"` |
| Uvicorn 0.54.0 | spec `2.3`; `send` after disconnect returns silently; no `pathsend` | server check; uvicorn 0.54.0 h11_impl.py and httptools_impl.py, RequestResponseCycle.send |
| Hypercorn 0.18.0 | spec `2.1` for HTTP; no `pathsend`; empty reason phrase in the status line (`HTTP/1.1 200 `) | server check; hypercorn 0.18.0 hypercorn/protocol/http_stream.py, HTTPStream.handle (spec); hypercorn/protocol/h11.py, H11Protocol.stream_send (`h11.Response` built without `reason`) |
| Granian 2.8.7 | spec `2.3`; offers `http.response.pathsend`, so `FileResponse` sends one `pathsend` message | server check |
| FastAPI 0.162.0 | `response_model` fast path uses Pydantic `dump_json` and `Response`, so `NaN` -> `null`; explicit `JSONResponse` raises | lab `test_fastapi_response_model_fast_path_bypasses_jsonresponse`; fastapi 0.162.0 fastapi/routing.py |
| HTTP/1.1 | no `Content-Length` -> `Transfer-Encoding: chunked` on all three servers | server check |
| HTTP/2 | no chunked coding: an endpoint MUST NOT generate `Transfer-Encoding` in HTTP/2 (RFC 9113 Section 8.2.2), so a length-less stream is framed by `DATA` frames and `END_STREAM`; over h2c prior knowledge, Hypercorn 0.18.0 (spec `2.1`) and Granian 2.8.7 with `--http 2` (spec `2.3`) sent `:status: 200` with no `transfer-encoding` or `content-length`, and after the client read 4096 `DATA` bytes and closed, both logged `http 2`, `finally after 4 chunks`, `bg ran`, and a normal return; Uvicorn 0.54.0 serves HTTP/2 only with `http="zttp"` and `http2=True`, and its HTTP/2 `send` also advertises spec `2.3` and returns silently after a disconnect | RFC 9113 Section 8.2.2; verifier h2c probe with `h2` 4.4.1 (2026-11-14, same machine); uvicorn 0.54.0 uvicorn/config.py, Config.load, and uvicorn/protocols/http/zttp_h2_impl.py, RequestResponseCycle.send; the Uvicorn HTTP/2 path was read, not run, because `zttp` is not installed in the lab environment, so its disconnect behavior is stated from source only |
| HTTP/3 over QUIC | no HTTP/3 server was run for this note; by specification the framing is length-less too: transfer codings are not defined for HTTP/3 and `Transfer-Encoding` MUST NOT be used (RFC 9114 Section 4.1), so the content is a series of `DATA` frames inside the message and needs no length prefix (RFC 9114 Section 4.1); a client that loses interest cancels by abruptly terminating the stream (RFC 9114 Section 4.1.1), which a server surfaces to ASGI through whatever disconnect signal it implements; ASGI `http_version` may be `"1.1"`, `"2"`, or `"3"` (ASGI HTTP spec, `www.rst`) | RFC 9114 Section 4.1 and Section 4.1.1; ASGI HTTP spec (asgiref main `specs/www.rst`, `http_version`, fetched 2026-11-14) |
| Debian Trixie and Alpine containers | not run; container legs are paused for this book (2026-11-11) | none |

## Worked example

The [implementation](../labs/src/api_labs/f10_asgi_and_starlette_internals/starlette_responses.py) provides `ScriptedPeer`, an in-process stand-in for the server side of one request, and `ClosingStreamingResponse`; the [test](../labs/tests/f10_asgi_and_starlette_internals/test_starlette_responses_streamingresponse_fileresponse_and_background_execution.py) drives Starlette's own response classes through it, and uses `httpx.ASGITransport` for the paths where the client never leaves.
`ASGITransport` cannot model a disconnect: its `receive()` returns `http.disconnect` only after `response_complete` is set, and it buffers the whole body before returning (httpx 0.28.1 httpx/_transports/asgi.py, ASGITransport.handle_async_request), which is why the disconnect tests call the response directly.

`ScriptedPeer.send`, verbatim from lines 88 to 99 of `starlette_responses.py`: after the chosen body message it marks the client gone, and from then on it drops (Uvicorn), raises `OSError` (spec 2.4), or hangs in the send that saw the disconnect.

```python
    async def send(self, message: Message) -> None:
        if self._gone.is_set():
            if self.after_disconnect == "raise":
                raise OSError("client disconnected")
            self.dropped += 1
            return
        self.sent.append(message)
        bodies = sum(1 for m in self.sent if m["type"] == "http.response.body")
        if self.disconnect_after_bodies is not None and bodies >= self.disconnect_after_bodies:
            self._gone.set()
            if self.after_disconnect == "hang":
                await asyncio.Event().wait()
```

The fix for the parked-generator leak, verbatim from lines 110 to 127:

```python
class ClosingStreamingResponse(StreamingResponse):
    """``StreamingResponse`` whose async generator is closed before ``background`` runs.

    ``stream_response`` is the coroutine Starlette cancels on ``http.disconnect`` (spec < 2.4)
    and the one that sees ``OSError`` from ``send`` (spec >= 2.4), so closing in its
    ``finally`` covers both paths; the shield lets ``aclose()`` finish inside a cancelled scope.
    """

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            aclose: Callable[[], Awaitable[object]] | None = getattr(
                self.body_iterator, "aclose", None
            )
            if aclose is not None:
                with anyio.CancelScope(shield=True):
                    await aclose()
```

The three tests that pin the version switch and the leak, verbatim from lines 123 to 175 of the test module; `_endless` appends `"chunk"` after each `await asyncio.sleep(0)` and `"finally"` in its `finally`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", [None, "2.0", "2.3"])
async def test_pre_2_4_disconnect_cancels_generator_and_still_runs_background(
    spec_version: str | None,
) -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=3, after_disconnect="drop")
    response = StreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    await response(http_scope(spec_version=spec_version), peer.receive, peer.send)
    assert peer.receive_calls == 2
    assert (events.count("chunk"), len(peer.body_messages()), peer.dropped) == (3, 3, 0)
    assert events[-2:] == ["finally", "background"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.4", "2.5"])
async def test_spec_2_4_send_oserror_becomes_client_disconnect_and_skips_background(
    spec_version: str,
) -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=3, after_disconnect="raise")
    response = StreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    with pytest.raises(ClientDisconnect):
        await response(http_scope(spec_version=spec_version), peer.receive, peer.send)
    assert peer.receive_calls == 0
    assert "background" not in events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_suspended_in_send_is_not_closed_by_starlette() -> None:
    events: list[str] = []
    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="hang")
    response = StreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    await response(http_scope(), peer.receive, peer.send)
    assert events[-1] == "background"
    assert "finally" not in events

    events.clear()
    peer = ScriptedPeer(disconnect_after_bodies=2, after_disconnect="hang")
    closing = ClosingStreamingResponse(
        _endless(events), background=BackgroundTask(events.append, "background")
    )
    await closing(http_scope(), peer.receive, peer.send)
    assert events[-2:] == ["finally", "background"]
```

`receive_calls == 2` is the listener at work: the first call returns the request's `http.request` message, which it discards, and the second blocks until `http.disconnect`.
The rest of the module covers `JSONResponse` bytes and `NaN`, the `Content-Length` rules, the chunk plan for six file sizes, the stat headers and `ETag` formula, `HEAD`, `pathsend`, the stale-stat framing break, `Content-Disposition`, background ordering, and the `ASGITransport` wait.

Captured from `labs/` on macOS 27.3 (arm64, Darwin) with CPython 3.14.6 and pytest 9.2.0:

```sh
uv run --frozen pytest tests/f10_asgi_and_starlette_internals/test_starlette_responses_streamingresponse_fileresponse_and_background_execution.py -q
```

```text
...............................                                          [100%]
31 passed in 1.05s
```

## Pitfalls

> [!warning] A parked generator is never closed
> Trigger: an async generator that holds a database cursor, a pooled connection, or an upstream HTTP stream, served to a client that stops reading and then disconnects while `send()` is blocked.
> Symptom: the request finishes and `background` runs, but the generator's `finally` (and any `async with` inside it) has not run; the connection returns to the pool only when garbage collection finalizes the generator, so pools drain under slow-client churn.
> Fix: close the iterator yourself in a `finally` around `stream_response`, as `ClosingStreamingResponse` does, or own the resource outside the generator and release it in middleware; the lab shows the stock class leaving `"finally"` unrun and the subclass running it before `"background"`.

> [!warning] On a spec 2.4 server, a disconnect skips `background` and raises
> Trigger: a future server that advertises `spec_version` `2.4` and raises `OSError` from `send()` after the client leaves.
> Symptom: `StreamingResponse` raises `ClientDisconnect` out of the app, so the `background` call after it never runs, while the same code on Uvicorn 0.54.0 (spec `2.3`) runs it; audit writes or usage metering attached as background work silently disappear for aborted streams.
> Fix: do not attach must-run work to a streaming response's `background`; record it in a `finally` or in middleware, and test both branches as the lab does with `spec_version` `2.3` and `2.4`.

> [!warning] A sync iterator or `open()` file costs a thread hop per item
> Trigger: `StreamingResponse(open(path, "rb"))` (which the Starlette docs suggest for file-like objects) or `StreamingResponse(sync_generator())`.
> Symptom: every item is a `to_thread.run_sync` call against the shared 40-token limiter; a binary file iterates by lines, because `IOBase` iteration yields "the lines in a stream" (Python `io` docs), so a file with no newline bytes is read into memory as one chunk, and one with many short lines becomes thousands of hops and body messages.
> Fix: use `FileResponse` for files, or an async generator that reads fixed-size blocks; keep sync generators free of blocking cleanup, because their `finally` runs on the event-loop thread when collected (lab `test_sync_iterator_runs_in_worker_threads_and_is_finalized_on_the_loop`).

> [!warning] `FileResponse` trusts `stat` and reads until a short read
> Trigger: a file that is appended to (a log), truncated, or replaced between `stat` and the last `read`, or a `stat_result` computed earlier and passed in.
> Symptom: `Content-Length` disagrees with the bytes sent; a recipient that gets fewer bytes "MUST consider the message to be incomplete and close the connection" (RFC 9112 Section 6.3), and more bytes than declared are a framing error that h11 refuses with `LocalProtocolError("Too much data for declared Content-Length")` (h11 0.16.0 h11/_writers.py, ContentLengthWriter); the lab's application emits 70000 bytes under `content-length: 10`.
> Fix: serve immutable files (write to a temporary name, then `os.replace`), or snapshot the content and serve the snapshot; never pass a cached `stat_result` for a mutable file.

> [!warning] Errors after the last byte look like success
> Trigger: a background task, or a `BackgroundTasks` entry, that raises.
> Symptom: the client already has a `200`; the exception escapes the ASGI call after the response completed, so error middleware cannot change the status and the server only logs it, and every later task in the same `BackgroundTasks` is skipped (Starlette 1.10.0 docs/background.md; lab `test_background_runs_after_final_body_sequentially_and_stops_on_error`).
> Fix: wrap each task in its own error handling and telemetry; move anything that must survive a crash or a deploy to a durable queue.

> [!warning] The in-process client hides the production timing
> Trigger: testing streaming or background behavior only through `httpx.ASGITransport`.
> Symptom: `client.get` returns only after background tasks finish (0.303 s for a 0.3 s task, measured), the body is fully buffered, `receive()` never reports a disconnect while the app is streaming so an endless stream hangs the test, and `HEAD` bodies are discarded by the transport, so a response that wrongly sends a `HEAD` body still passes (httpx 0.28.1 httpx/_transports/asgi.py).
> Fix: assert ASGI messages directly with a scripted peer for framing, `HEAD`, and disconnect behavior, and keep one socket-level test per server you deploy; testing strategy is in [[In-Process-Testing-of-ASGI-Apps-with-HTTPX-ASGITransport]].

> [!warning] Range parsing was quadratic in Starlette 0.39.0 to 0.49.0
> Trigger: an unauthenticated request with a crafted many-part `Range` header to any `FileResponse` or `StaticFiles` route.
> Symptom: "quadratic-time processing in Starlette's `FileResponse` Range parsing/merging logic", CPU exhaustion per request; CVE-2025-62727, rated high, affects `>= 0.39.0, <= 0.49.0`, first patched in 0.49.1 (GHSA-7f5h-v6xp-fcq8).
> Fix: run 0.49.1 or later (the lab pins 1.10.0, which sorts once and caps at `max_ranges = 100`); details in [[Range-Requests-Partial-Content-and-206-Semantics]].

## Security and robustness

- Path containment is not `FileResponse`'s job: it checks only existence and `S_ISREG`, and `os.stat` follows symlinks, so `FileResponse(base / user_input)` serves anything the process can read; resolve the path and check it is inside the root before constructing the response.
  `StaticFiles` does that containment and still got it wrong once: CVE-2023-29159 (GHSA-v5gw-mw7f-84px, medium, `>= 0.13.5, < 0.27.0`) exposed sibling paths that shared a prefix with the static directory because the check used `os.path.commonprefix()`.
- Header injection through download names is closed by construction: `Content-Disposition` routes any name that `quote()` changes, including `"`, CR, and LF, into the percent-encoded `filename*` form, which the lab shows for `a"b.txt`.
- Response bodies are an OWASP API4:2023 Unrestricted Resource Consumption surface: an API is vulnerable if limits such as "Execution timeouts" and "Maximum number of file descriptors" are missing; an endless or slow `StreamingResponse` holds a task, a generator, any pooled resources, and on file paths an open descriptor per client for as long as the client stays connected.
  Bound stream duration and total bytes in the generator, cap concurrent streams per principal, and rely on server backpressure (`send()` blocks when the socket buffer is full) rather than buffering ahead.
- The `ETag` is metadata-derived: two replicas with the same bytes but different `mtime` disagree, and a same-size rewrite within one `mtime` tick keeps the old tag although the bytes changed; for content-addressed caching or safe `If-Range` resumption, set an explicit content hash as `etag`, which `set_stat_headers` respects because it uses `setdefault`.
- `JSONResponse` refuses `NaN` and `Infinity`, which keeps non-JSON tokens off the wire, but the FastAPI fast path maps them to `null`, so a client may see `null` for a field documented as a number; validate at the model boundary rather than relying on the serializer.
- Background work runs with the request's privileges and context after the response is gone; never let it trust request-derived values that were only validated for the response path, and never put secrets into a task's arguments where an exception log will print them.

## The principal's view

The decision is where the lifetime of a response ends, and Starlette gives three different answers depending on the server: the last `send()`, the `http.disconnect` event, or an `OSError`.
Standardize on cleanup that does not depend on which one you get: resources owned by a generator are released in a `finally` that something guarantees to run (an explicit `aclose()`), and work that must happen is recorded outside `background`.
Treat `BackgroundTask` as a latency trick, not a reliability tool: it moves work after the last byte but keeps it in the request task, in the same process, with no retry, no persistence, and a shutdown drain window; the organizational trade-off is that it is trivially easy to adopt and invisible on dashboards until a deploy cancels it.
For file delivery, the best `FileResponse` is the one that never reads the file in Python: prefer object storage with signed URLs or a reverse proxy that serves from disk, then `pathsend` on a server that offers it (Granian here), and only then Starlette's threaded 64 KiB loop.
For long-lived streams, choose the protocol by the failure model: Server-Sent Events adds reconnection and `Last-Event-ID` on top of the same `StreamingResponse` mechanics (see [[Server-Sent-Events-SSE-Protocol-Last-Event-ID-and-SSE-Starlette]]), WebSockets give explicit close codes, and gRPC server streaming gives deadlines and status trailers; plain chunked HTTP gives you only a socket that closes.
When the per-chunk overhead of Python matters (thread hops, a message per chunk, one task per listener), the answer is to batch chunks into larger writes or to move the byte-pushing into a server or proxy written in a systems language, not to tune the event loop.

## Interview questions

> [!question] L5: Why does `JSONResponse({"x": float("nan")})` fail, and when does the failure happen?
>
> > [!success]- Answer
> > `JSONResponse.render` calls `json.dumps` with `allow_nan=False`, which raises `ValueError` for `NaN` and `Infinity`.
> > RFC 8259 Section 6 says those values are not permitted in JSON.
> > Rendering runs in `__init__`, so the error happens in the endpoint before `http.response.start`, and error middleware can still return `500`.
> >
> > Follow-up: Why does a FastAPI route with `response_model=dict[str, float]` return `{"x":null}` for the same value?

> [!question] L5: Predict the output. What `(len(body), more_body)` pairs does `FileResponse` send for a file of exactly 131072 bytes, and why?
>
> > [!success]- Answer
> > `(65536, True), (65536, True), (0, False)`.
> > `_handle_simple` sets `more_body = len(chunk) == self.chunk_size`, so a full second chunk claims more is coming.
> > The third read returns `b""`, which is shorter than the chunk size, so it goes out as the empty final message.
> >
> > Follow-up: What does the same loop do if the file grows by 10 bytes between `stat` and the last read?

> [!question] L6: Walk through what Starlette 1.10.0 does when a client disconnects in the middle of a `StreamingResponse` served by Uvicorn 0.54.0.
>
> > [!success]- Answer
> > Uvicorn advertises spec `2.3`, so Starlette runs `listen_for_disconnect` and `stream_response` in one AnyIO task group.
> > `connection_lost` marks the cycle disconnected and wakes `receive()`, which returns `http.disconnect`.
> > The listener returns and cancels the scope, so `stream_response` gets `CancelledError` at its current `await`.
> > The group exits cleanly and `__call__` awaits `background`.
> > Uvicorn's `send()` returns silently after a disconnect, which is why the listener is needed at all.
> >
> > Follow-up: What changes if the server advertises spec `2.4`?

> [!question] L6: Debug this API. A streaming export endpoint wraps a database cursor in an async generator with `async with pool.acquire()`; under slow mobile clients the pool runs dry, yet every request logs as finished and its background audit task runs. What is happening?
>
> > [!success]- Answer
> > The clients stop reading, so `send()` blocks, and the generator is parked at `yield`.
> > When the disconnect arrives, the listener cancels `stream_response` inside `send()`, not inside the generator.
> > Starlette never calls `aclose()`, so the `async with` exit and the connection release wait for garbage collection.
> > Close the generator explicitly in a `finally` around `stream_response`, as `ClosingStreamingResponse` does, and confirm with a test that hangs the send.
> >
> > Follow-up: Why would the same endpoint release connections promptly if the generator were blocked on a query instead?

> [!question] L6: Why does `FileResponse` close its file under `anyio.CancelScope(shield=True)`?
>
> > [!success]- Answer
> > The transfer is cancelled from outside when the client disconnects, and inside a cancelled scope every unshielded `await` raises `CancelledError` immediately.
> > `AsyncFile.aclose()` is an `await` that runs the close in a worker thread, so without a shield the close itself could be cancelled and the descriptor would leak.
> > The shield lets that one `await` finish before cancellation resumes.
> > AnyIO 4.16.0 already shields inside `AsyncFile.aclose`; Starlette's `TODO` keeps its own shield for older AnyIO releases.
> >
> > Follow-up: What other cleanup in your own streaming code needs the same shield?

> [!question] L6: Your test suite uses `httpx.ASGITransport` and passes, but production shows background tasks delaying nothing and streams that never end on disconnect. What can the in-process tests not see?
>
> > [!success]- Answer
> > `ASGITransport` awaits the whole application call, so background tasks complete before `client.get` returns.
> > It buffers the full body, and its `receive()` returns `http.disconnect` only after the response is complete, so it can never simulate a client that leaves mid-stream.
> > It also drops `HEAD` bodies itself, so a buggy `HEAD` response still looks correct.
> > Drive the ASGI callable with a scripted `receive` and `send` for these cases, and keep a socket-level test per deployed server.
> >
> > Follow-up: How would you write a deterministic test for "the generator's `finally` runs before `background`"?

> [!question] L7+: Your platform runs the same Starlette app behind Uvicorn today and plans to adopt a server that implements ASGI HTTP spec 2.4. What breaks, and how do you make the codebase indifferent to the switch?
>
> > [!success]- Answer
> > On spec 2.4 Starlette stops listening for `http.disconnect` and turns `OSError` from `send()` into `ClientDisconnect`, which propagates out of the app.
> > `background` on streaming responses no longer runs for aborted streams, and `FileResponse` propagates raw `OSError` because it does not convert it.
> > Logging and error middleware may start reporting disconnects as errors unless they treat `ClientDisconnect` and `OSError` as expected.
> > Make cleanup and must-run accounting independent of the branch: explicit `aclose()` in a `finally`, accounting in middleware, and a test matrix over `spec_version` `2.3` and `2.4`.
> >
> > Follow-up: Which metrics would you add before the migration to prove nothing was lost?

> [!question] L7+: A team proposes using `BackgroundTasks` to send order-confirmation emails and to write billing events. Make the call.
>
> > [!success]- Answer
> > Emails can tolerate loss and duplicates if the user can trigger a resend; billing events cannot.
> > `BackgroundTasks` runs in the request task after the last byte, sequentially, with no retry and no persistence, and one failing task skips the rest.
> > A deploy cancels tasks that exceed Uvicorn's `timeout_graceful_shutdown`, and on a spec 2.4 server a disconnected stream skips `background` entirely.
> > Put billing on a durable queue or an outbox written in the request transaction; keep `BackgroundTasks` for best-effort work whose loss is cheap.
> >
> > Follow-up: How does the transactional outbox pattern guarantee the event is written exactly when the order is?

> [!question] L7+: When would you serve files through `FileResponse` at all, rather than object storage or a proxy?
>
> > [!success]- Answer
> > When the file must be authorized per request with application logic, is small, and the traffic is modest.
> > Each `FileResponse` read is a 64 KiB thread hop against a 40-token pool shared with all sync code, plus one ASGI message per chunk.
> > Its `ETag` derives from `mtime` and size, which breaks validator agreement across replicas.
> > For volume, authorize in the app and redirect to a signed URL, or let a proxy or a `pathsend`-capable server push the bytes.
> >
> > Follow-up: How would you set a content-based `ETag` without reading the file on every request?

## Related

Prerequisites: [[Starlette-Request-Internals-Stream-Consumption-and-Body-Caching]]

Next: [[Pure-ASGI-Middleware-Onion-Architecture-and-Send-Receive-Wrapping]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[ASGI-HTTP-and-Disconnect-Event-Lifecycle]], [[ASGI-3-0-Callable-Contract-Scope-Receive-and-Send]], [[Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware]], [[Why-BaseHTTPMiddleware-Hurts-Performance-Streaming-and-ContextVars]], [[Server-Sent-Events-SSE-Protocol-Last-Event-ID-and-SSE-Starlette]], [[ASGI-WebSockets-Lifecycle-Subprotocols-and-Backpressure]], [[In-Process-Testing-of-ASGI-Apps-with-HTTPX-ASGITransport]], [[ASGI-State-Isolation-and-ContextVars-Across-Request-Lifecycle]], [[Range-Requests-Partial-Content-and-206-Semantics]], [[HTTP-1-1-Framing-Keep-Alive-and-Chunked-Encoding]]

- Lab: `labs/tests/f10_asgi_and_starlette_internals/test_starlette_responses_streamingresponse_fileresponse_and_background_execution.py`

## Further reading

- [Starlette 1.10.0 `starlette/responses.py`](https://github.com/Kludex/starlette/blob/1.10.0/starlette/responses.py): every class in this note, including the spec-version switch and the shielded file close.
- [Starlette 1.10.0 `starlette/background.py`](https://github.com/Kludex/starlette/blob/1.10.0/starlette/background.py): the twenty lines that define what "background" means.
- [Starlette 1.10.0 `starlette/concurrency.py`](https://github.com/Kludex/starlette/blob/1.10.0/starlette/concurrency.py): `iterate_in_threadpool` and the `StopIteration` workaround.
- [ASGI HTTP and WebSocket spec](https://github.com/django/asgiref/blob/main/specs/www.rst): spec versions, the `http.disconnect` event, and the 2.4 `OSError` rule.
- [ASGI extensions](https://github.com/django/asgiref/blob/main/docs/extensions.rst): the `http.response.pathsend` contract.
- [RFC 9110 Section 8.6](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.6): when `Content-Length` must, may, and must not be sent.
- [RFC 9112 Section 6.3](https://www.rfc-editor.org/rfc/rfc9112.html#section-6.3): how a recipient decides where a response body ends, and what an early close means.
- [RFC 9113 Section 8.2.2](https://www.rfc-editor.org/rfc/rfc9113.html#section-8.2.2): why `Transfer-Encoding` is forbidden in HTTP/2, so a length-less stream is framed by `DATA` frames.
- [RFC 9114 Section 4.1 and Section 4.1.1](https://www.rfc-editor.org/rfc/rfc9114.html#section-4.1): HTTP/3 message framing without transfer codings, and request cancellation by abrupt stream termination.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259.html): JSON numbers (Section 6), UTF-8 (Section 8.1), and the media type without `charset` (Section 11).
- [RFC 6266 Section 4.3](https://www.rfc-editor.org/rfc/rfc6266.html#section-4.3) and [RFC 8187](https://www.rfc-editor.org/rfc/rfc8187.html): `filename` versus `filename*` in `Content-Disposition`.
- [GHSA-7f5h-v6xp-fcq8](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8): the quadratic `Range` merge in `FileResponse` (CVE-2025-62727).
- [GHSA-v5gw-mw7f-84px](https://github.com/advisories/GHSA-v5gw-mw7f-84px): the `StaticFiles` prefix-match traversal (CVE-2023-29159).
