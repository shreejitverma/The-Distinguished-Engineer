---
type: concept
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc9112.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://github.com/MagicStack/uvloop"
  - "https://github.com/MagicStack/httptools"
  - "https://github.com/nodejs/llhttp"
  - "https://github.com/libuv/libuv"
  - "https://github.com/encode/uvicorn"
  - "https://github.com/python-hyper/h11"
  - "https://nvd.nist.gov/vuln/detail/CVE-2023-30589"
  - "https://nvd.nist.gov/vuln/detail/CVE-2022-32214"
  - "https://nvd.nist.gov/vuln/detail/CVE-2022-32213"
  - "https://www.rfc-editor.org/rfc/rfc9112.txt"
  - "https://github.com/advisories/GHSA-cggh-pq45-6h9x"
  - "https://github.com/advisories/GHSA-q5vx-44v4-gch4"
  - "https://github.com/advisories/GHSA-5689-v88g-g6rv"
  - "https://github.com/advisories/GHSA-hg6j-8h7m-3w3j"
  - "https://github.com/MagicStack/httptools/releases"
  - "https://github.com/MagicStack/httptools/blob/v0.9.0/setup.py"
  - "https://github.com/MagicStack/uvloop/blob/v0.23.0/setup.py"
  - "https://github.com/MagicStack/uvloop/blob/v0.23.0/README.rst"
  - "https://github.com/libuv/libuv/blob/1cfa32ff59c076ffb6ed735bbc8c18361558661f/src/unix/core.c"
  - "https://github.com/libuv/libuv/blob/1cfa32ff59c076ffb6ed735bbc8c18361558661f/include/uv/version.h"
  - "https://github.com/nodejs/llhttp/blob/0e815792b167a9bd8ace259b95b7da953776c288/include/llhttp.h"
  - "https://github.com/nodejs/llhttp/blob/0e815792b167a9bd8ace259b95b7da953776c288/src/llhttp.c"
  - "https://github.com/nodejs/llhttp/blob/0e815792b167a9bd8ace259b95b7da953776c288/README.md"
  - "https://pypi.org/project/uvloop/0.23.0/#files"
  - "https://pypi.org/project/httptools/0.9.0/#files"
---

# uvloop (0.23.0) and httptools (0.9.0) Internals: Cython libuv Loop Replacement and llhttp C Callback Framing

> [!summary] Claim
> Replacing CPython's pure-Python selector event loop with uvloop 0.23.0 (Cython over libuv 1.52.1) and pure-Python `h11` parsing with httptools 0.9.0 (llhttp 9.4.3) moves polling, timers, and HTTP framing into C; in this note's lab the llhttp parser alone is about 20x faster than `h11` on one 76-byte request, while every socket read and every parser span still costs one Python `bytes` copy.

Part of [ASGI Servers and Async Runtime](README.md) in [API Engineering](../README.md).

## TL;DR

`uvloop` wraps libuv's C event loop in a Cython `Loop` class that releases the GIL around `uv_run`, while `httptools` runs Node's `llhttp` state machine over the raw byte buffer and calls Cython C callbacks for each parsed span.
`httptools` stitches split header names and values itself and hands `on_header(name, value)` complete pairs, but it passes `on_url` and `on_body` fragments through unbuffered, so protocols must concatenate URL and body pieces across `data_received` calls.
Enabling dangerous parser leniencies, or running a front-end proxy that frames messages differently from `llhttp`, exposes backend ASGI workers to HTTP request smuggling (RFC 9112 Section 11.2, CVE-2023-30589, CVE-2022-32214).
`uvloop` 0.23.0 is POSIX only (`epoll` on Linux, `kqueue` on macOS) and refuses to build on Windows; servers such as Uvicorn then use `asyncio.ProactorEventLoop` there `(as of 2026-11)`.
Do not choose `uvloop` and `httptools` when native HTTP/2 multiplexing, HTTP/3 QUIC transport, or custom thread-per-core multi-reactor architectures are required; choose [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers]] or [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI]] instead.

## Mental model

```mermaid
flowchart TD
    subgraph KernelSpace["Kernel Space (OS)"]
        Socket["TCP Client Socket<br/>(File Descriptor)"]
    end

    subgraph LibuvLoop["uvloop (libuv 1.52.1 Engine)"]
        IOPoll["uv__io_poll (epoll_wait / kevent)<br/>Executed with nogil"]
        RecvBuf["Loop._recv_buffer<br/>(Fixed 256000-byte buffer per loop)"]
        OnRead["__uv_stream_on_read<br/>Acquires GIL: noexcept with gil"]
    end

    subgraph LLHttpParser["httptools (llhttp 9.4.3 Engine)"]
        FeedData["HttpParser.feed_data<br/>Buffer pointer of the bytes object"]
        CExecute["llhttp_execute<br/>Generated goto state machine"]
        CCallbacks["C Callbacks<br/>(cb_on_url, cb_on_header_field,<br/>cb_on_header_value, cb_on_body)"]
    end

    subgraph UvicornProtocol["Uvicorn ASGI Protocol"]
        ProtoHandler["HttpToolsProtocol<br/>(on_url, on_header, on_headers_complete)"]
        CycleQueue["RequestResponseCycle<br/>(Pipelined FIFO Queue)"]
        ASGITask["loop.create_task(cycle.run_asgi)<br/>Dispatches ASGI Application"]
    end

    Socket -->|Kernel Readiness| IOPoll
    IOPoll -->|read() syscall into buffer| RecvBuf
    RecvBuf -->|loop._recv_buffer[:nread] copies to bytes| OnRead
    OnRead -->|protocol.data_received| FeedData
    FeedData -->|char* pointer, no copy| CExecute
    CExecute -->|Span callbacks| CCallbacks
    CCallbacks -->|at[:length] bytes per span; headers stitched| ProtoHandler
    ProtoHandler -->|Headers complete| CycleQueue
    CycleQueue -->|Spawn coroutine| ASGITask
```

The execution flow begins when network packets land on a TCP client socket file descriptor in kernel space.
libuv's polling routine `uv__io_poll` waits on socket readiness using `epoll` or `kqueue` inside `Loop.__run` with the Python GIL released.
Upon socket readability, libuv calls `read(2)` into a preallocated 256000-byte loop-level buffer (`_recv_buffer`), so no C receive buffer is allocated per read.
Control transitions into Cython's `__uv_stream_on_read` callback, which holds the GIL, copies the `nread` bytes into a new Python `bytes` object with `loop._recv_buffer[:nread]`, and passes it to `HttpToolsProtocol.data_received`.
`HttpRequestParser.feed_data` takes the buffer pointer of that object and passes it to `llhttp_execute` without a further copy.
The `llhttp` state machine reports each span (URL, header field, header value, body) through C callbacks, and each callback copies its span into a new `bytes` object (`at[:length]`) before calling Python; header fragments are stitched in Cython, URL and body fragments are not.
Finally, `HttpToolsProtocol.on_headers_complete` populates the ASGI `scope` dictionary and dispatches an ASGI task to the event loop.

## How it works

Standard CPython `asyncio` implements its event loop in pure Python within `asyncio/base_events.py` and `asyncio/selector_events.py`.
In default `asyncio`, each event loop tick calls `selectors.DefaultSelector.select()`, which invokes the C-level `epoll_wait` or `kevent` system call but immediately wraps results in a list of Python `(key, mask)` tuples.
When processing socket I/O for a plain `Protocol`, default `asyncio` calls `self._sock.recv(self.max_size)` with `max_size = 256 * 1024`, allocating a new Python `bytes` object on every read (CPython 3.14.6 `Lib/asyncio/selector_events.py`, `_SelectorSocketTransport._read_ready__data_received`).
Timers are maintained in a Python binary heap using `heapq`, ready callbacks are held in a `collections.deque` of `asyncio.Handle` instances, and reader and writer registrations go through the selector's registration map.
Under heavy I/O concurrency, Python bytecode evaluation, object allocation churn, and Garbage Collector (GC) tracking create significant CPU overhead.

### uvloop: Cython and libuv architecture

`uvloop` eliminates most of this pure-Python loop overhead by replacing CPython's `asyncio.AbstractEventLoop` implementation with a compiled Cython extension wrapping libuv 1.52.1.
When an application invokes `uvloop.new_event_loop()`, the Cython `Loop` class allocates its internal `uv_loop_t` struct via `PyMem_RawMalloc(sizeof(uv.uv_loop_t))` `(uvloop 0.23.0 uvloop/loop.pyx, Loop.__cinit__)`.
The loop installs Python memory allocators into libuv using `__install_pymem()` (which calls `uv_replace_allocator`) and registers fork hooks with `pthread_atfork` via `__install_atfork()`.
When the loop starts, `Loop._run` records the running OS thread in `self._thread_id = PyThread_get_thread_ident()`; `_check_thread` compares against it, but `call_soon` and `call_later` only call `_check_thread` when debug mode is on `(uvloop 0.23.0 uvloop/loop.pyx, Loop._run)`.

```
+-----------------------------------------------------------------------------------+
| uvloop Event Loop Iteration Cycle (libuv 1.52.1 uv_run, src/unix/core.c)          |
+-----------------------------------------------------------------------------------+
| 0. UV_RUN_DEFAULT only, once before the loop: uv__update_time + uv__run_timers    |
| 1. uv__run_pending(loop)                                                          |
|    Fires pending I/O callbacks deferred from the previous iteration.              |
| 2. uv__run_idle(loop) -> uvloop handler_idle (Loop._on_idle)                      |
|    Drains self._ready up to ntodo = len(self._ready); flushes queued writes.      |
| 3. uv__run_prepare(loop)                                                          |
|    Executes pre-polling preparatory handles.                                      |
| 4. uv__io_poll(loop, timeout)                                                     |
|    Executes epoll_wait(2) / kevent(2) with nogil. GIL released during sleep.      |
|    Then up to 8 more uv__run_pending passes for immediate callbacks (write_cb).   |
| 5. uv__run_check(loop) -> uvloop handler_check__exec_writes                       |
|    Flushes stream writes queued with Loop._queue_write.                           |
| 6. uv__run_closing_handles(loop)                                                  |
|    Reclaims memory for closed libuv streams, timers, and handles.                 |
| 7. uv__update_time(loop) + uv__run_timers(loop)                                   |
|    Refreshes the cached clock, then drains expired timers from the C min-heap.    |
+-----------------------------------------------------------------------------------+
```

The phase order is the `uv_run` body in libuv 1.52.1 `src/unix/core.c`: timers run after the check and closing phases, with one extra timer pass before the loop in `UV_RUN_DEFAULT` mode.
When the loop runs via `run_forever()` or `run_until_complete()`, it invokes the private Cython method `__run` `(uvloop 0.23.0 uvloop/loop.pyx, Loop.__run)`:

```cython
cdef __run(self, uv.uv_run_mode mode):
    Py_INCREF(self)
    with nogil:
        err = uv.uv_run(self.uvloop, mode)
    Py_DECREF(self)

    if err < 0:
        raise convert_error(err)
```

This is a simplified excerpt: the source also carries a comment block on manual reference counting.
The `with nogil:` context releases the Python Global Interpreter Lock (GIL) before entering `uv_run`.
While libuv blocks inside the kernel waiting for socket descriptors in `uv__io_poll`, other Python threads can run without contending for the GIL.
When kernel readiness events occur, libuv calls registered C callbacks, which re-acquire the GIL because Cython declares them `noexcept with gil`.

### Zero-heap receive buffer management

A major performance optimization in `uvloop` is its socket receive buffer strategy.
In `uvloop/loop.pxd`, the `Loop` extension type embeds a fixed char array as a member `(uvloop 0.23.0 uvloop/loop.pxd, Loop._recv_buffer)`:

```cython
char _recv_buffer[UV_STREAM_RECV_BUF_SIZE]
bint _recv_buffer_in_use
```

The constant `UV_STREAM_RECV_BUF_SIZE` is defined as `256000` bytes (250 KiB, commented `# 250kb` in the source) `(uvloop 0.23.0 uvloop/includes/consts.pxi, UV_STREAM_RECV_BUF_SIZE)`.
When libuv prepares to read data from an active TCP stream (`uv_stream_t`), it invokes the buffer allocation callback `__loop_alloc_buffer` `(uvloop 0.23.0 uvloop/loop.pyx, __loop_alloc_buffer)`:

```cython
cdef void __loop_alloc_buffer(
    uv.uv_handle_t* uvhandle,
    size_t suggested_size,
    uv.uv_buf_t* buf
) noexcept with gil:
    cdef:
        Loop loop = (<UVHandle>uvhandle.data)._loop

    if loop._recv_buffer_in_use == 1:
        buf.len = 0
        exc = RuntimeError('concurrent allocations')
        loop._handle_exception(exc)
        return

    loop._recv_buffer_in_use = 1
    buf.base = loop._recv_buffer
    buf.len = sizeof(loop._recv_buffer)
```

libuv fills `loop._recv_buffer` with a `read(2)` call on the socket.
Once bytes are received, libuv invokes `__uv_stream_on_read`, which delegates to `__uv_stream_on_read_impl` `(uvloop 0.23.0 uvloop/handles/stream.pyx, __uv_stream_on_read_impl)`.
The callback marks `loop._recv_buffer_in_use = 0` via `__loop_free_buffer(loop)` and slices the buffer:

```cython
run_in_context1(
    sc.context,
    sc._protocol_data_received,
    loop._recv_buffer[:nread],
)
```

In Cython, slicing a `char` array with `[:nread]` builds a new Python `bytes` object and copies `nread` bytes into it, so each read still costs one Python allocation and one `memcpy`.
What the shared buffer saves is the per-read allocation and free of the C receive buffer, and a 256000-byte buffer per idle connection.
For zero-copy reads, `uvloop` also supports `asyncio.BufferedProtocol`: `__uv_stream_buffered_alloc` asks the protocol's `get_buffer()` for a writable buffer and libuv reads straight into it `(uvloop 0.23.0 uvloop/handles/stream.pyx, __uv_stream_buffered_alloc)`.

### Socket write fast-path and check-phase flushing

`transport.write(data)` appends `data` to the stream's `_buffer` list and calls `_initiate_write` `(uvloop 0.23.0 uvloop/handles/stream.pyx, UVStream._initiate_write)`.
If the protocol is not paused and `(<uv.uv_stream_t*>self._handle).write_queue_size == 0`, `_initiate_write` calls `_exec_write` immediately.
With exactly one buffered item, `_exec_write` calls `_try_write`, which issues a direct POSIX `system.write(fd, buf, blen)` instead of `uv_try_write`; for an exact `bytes` object it passes `PyBytes_AS_STRING(data)` without taking a buffer view `(uvloop 0.23.0 uvloop/handles/stream.pyx, UVStream._try_write)`.
With several buffered items it calls `uv_try_write` over a `_StreamWriteContext` of `uv_buf_t` entries.
If the write is partial or fails with `EAGAIN` or `EWOULDBLOCK`, `_exec_write` hands the remainder to libuv with `uv_write`, which queues it on the stream and completes it in `__uv_stream_on_write` `(uvloop 0.23.0 uvloop/handles/stream.pyx, UVStream._exec_write)`.
Only when the fast path is not allowed (protocol paused or libuv's write queue non-empty) does `_initiate_write` call `Loop._queue_write`, which adds the stream to `self._queued_streams`; those streams are flushed by `_exec_queued_writes`, run from the `UVCheck` handle `handler_check__exec_writes` and also at the end of `Loop._on_idle` `(uvloop 0.23.0 uvloop/loop.pyx, Loop._exec_queued_writes)`.

### httptools and llhttp: C callback framing

`httptools` 0.9.0 wraps Node.js `llhttp` 9.4.3 (vendored submodule at commit `0e815792`, `LLHTTP_VERSION_MINOR 4`, `LLHTTP_VERSION_PATCH 3` in `include/llhttp.h`).
`llhttp` is written in TypeScript and compiled by `llparse` into C: the release `src/llhttp.c` is a goto-based state machine with byte lookup tables and SSE4.2 or NEON range scans for some spans.
`httptools` switched its message parser from Ryan Dahl's `http-parser` to `llhttp` in 0.2.0, but 0.9.0 still vendors `http-parser` for `httptools.parse_url` (`httptools/parser/url_cparser.pxd` declares `cdef extern from "http_parser.h"`).
`httptools.HttpRequestParser` subclasses the internal `HttpParser`, which owns a `cparser.llhttp_t*` parser struct and a `cparser.llhttp_settings_t*` callback table, both allocated with `PyMem_Malloc` `(httptools 0.9.0 httptools/parser/parser.pyx, HttpParser)`.

When bytes arrive from the transport, Uvicorn passes them to `HttpRequestParser.feed_data(data)`.
`feed_data` inspects the input argument:
If the argument is a `memoryview`, it retrieves the underlying buffer via `PyMemoryView_GET_BUFFER(data)`.
Otherwise, it binds `self.py_buf` via `PyObject_GetBuffer(data, buf, PyBUF_SIMPLE)`.
It then executes the C parser `(httptools 0.9.0 httptools/parser/parser.pyx, HttpParser.feed_data)`:

```cython
err = cparser.llhttp_execute(
    self._cparser,
    <char*>buf.buf,
    data_len)
```

The C function `llhttp_execute` scans the byte buffer state by state.
As it recognizes the message elements of RFC 9112 Section 2.1, `llhttp` invokes the function pointers configured on `llhttp_settings_t`, and `httptools` registers `cb_on_header_field` and `cb_on_header_value` only if the protocol defines `on_header`, `cb_on_url` only if it defines `on_url`, and `cb_on_body` only if it defines `on_body`.
For the request below, `llhttp` makes these C-level calls; the Python protocol sees `on_header(name, value)` pairs instead of the field and value calls:

```
+-----------------------------------------------------------------------------------+
| llhttp 9.4.3 Streaming Callback Invocations (RFC 9112 Framing)                    |
+-----------------------------------------------------------------------------------+
| Wire Octets:                                                                      |
| POST /api/submit HTTP/1.1\r\nHost: example.com\r\nContent-Length: 5\r\n\r\nhello   |
+-----------------------------------------------------------------------------------+
| Callback Invocations:                                                             |
| 1. on_message_begin()                                                             |
| 2. on_url(b"/api/submit")                                                         |
| 3. on_header_field(b"Host")                                                       |
| 4. on_header_value(b"example.com")                                                |
| 5. on_header_field(b"Content-Length")                                             |
| 6. on_header_value(b"5")                                                          |
| 7. on_headers_complete()                                                          |
| 8. on_body(b"hello")                                                              |
| 9. on_message_complete()                                                          |
+-----------------------------------------------------------------------------------+
```

Each span callback copies its span into a new Python `bytes` object before calling into Python, for example `pyparser._on_header_field(at[:length])` in `cb_on_header_field` `(httptools 0.9.0 httptools/parser/parser.pyx, cb_on_header_field)`.

### The chunk boundary header stitching trap

Because TCP streams arrive in variable-sized packets, an HTTP header field or value can be fragmented across two or more `data_received` calls.
For example, the octets `Host: example.com\r\n` might arrive as chunk 1 `b"Ho"` and chunk 2 `b"st: example.com\r\n"`.
`llhttp` does not buffer spans across `llhttp_execute` calls; at the end of each call it flushes any open span to its callback, so it invokes `cb_on_header_field` for `b"Ho"` and then again for `b"st"` (llhttp 9.4.3 `src/llhttp.c`, `llhttp__internal_execute`).
`httptools` handles this in Cython through intermediate buffering `(httptools 0.9.0 httptools/parser/parser.pyx, HttpParser._on_header_field)`:

```cython
cdef _maybe_call_on_header(self):
    if self._current_header_value is not None:
        current_header_name = self._current_header_name
        current_header_value = self._current_header_value

        self._current_header_name = self._current_header_value = None

        if self._proto_on_header is not None:
            self._proto_on_header(current_header_name,
                                  current_header_value)

cdef _on_header_field(self, bytes field):
    self._maybe_call_on_header()
    if self._current_header_name is None:
        self._current_header_name = field
    else:
        self._current_header_name += field
```

When a new header field begins, if a previous header value was buffered, `_maybe_call_on_header()` emits the completed `(name, value)` pair to Python protocol callbacks (`protocol.on_header`).
If `field` fragments arrive across chunk boundaries, they are concatenated in `_current_header_name += field`, and `_on_header_value` concatenates value fragments the same way.
When all headers conclude, `_on_headers_complete` calls `_maybe_call_on_header()` to flush the final pending header before dispatching `on_headers_complete` to the application protocol.
The stitching stops at headers: `cb_on_url` and `cb_on_body` pass each fragment straight to `protocol.on_url` and `protocol.on_body`.
Feeding `b"GET /api/v"` then `b"1/items HTTP/1.1\r\nContent-Length: 6\r\n\r\nab"` then `b"cdef"` to httptools 0.9.0 produced `on_url(b"/api/v")`, `on_url(b"1/items")`, `on_body(b"ab")`, and `on_body(b"cdef")`, which is why Uvicorn's `on_url` does `self.url += url` (uvicorn 0.54.0 `uvicorn/protocols/http/httptools_impl.py`, `HttpToolsProtocol.on_url`).

### Push callbacks vs Sans-I/O pull: httptools vs h11

In contrast to `httptools`, `h11` 0.16.0 is written in pure Python using a Sans-I/O pull architecture.
Incoming data is fed to `h11.Connection.receive_data(data)`, which appends bytes to an internal `bytearray` inside `ReceiveBuffer` (`h11/_receivebuffer.py`).
The application must loop over `conn.next_event()` until `h11.NEED_DATA` is returned.
For every message component, `h11` matches regular expressions built from the ABNF strings in `_abnf.py` and compiled in `_readers.py` (for example `header_field_re`, `request_line_re`), computes substring slices, and instantiates frozen dataclass events: `h11.Request`, `h11.Data`, and `h11.EndOfMessage` (`h11/_events.py`).
Each request produces several short-lived Python event objects, whereas `httptools` produces only the `bytes` objects for each span and calls the protocol's bound methods directly from C.

## Numbers and limits

Measured rows were run on 2026-11-15 on Apple Silicon arm64, macOS 27.3 (`uname -r` = `27.3.0`), CPython 3.14.6 from uv, with `uv run --frozen python` from `labs/`; they are medians of five runs on a shared, unpinned machine, so expect run-to-run spread `(as of 2026-11)`.

| Metric / Parameter | Value / Limit | Origin / Source | Production Implication |
|---|---|---|---|
| libuv embedded version | `1.52.1` (`0x13401`) | `(uvloop 0.23.0 uvloop/loop.pyx, libuv_get_version)`, returned at runtime in the lab test; vendored commit `1cfa32ff` `include/uv/version.h` | Fixes the `uv_run` phase order shown above and the `epoll` / `kqueue` backends. |
| llhttp embedded version | `9.4.3` | `(httptools 0.9.0 vendor/llhttp @ 0e815792)`, `include/llhttp.h` version macros | Includes the fixes for CVE-2022-32214 (llhttp 6.0.9 in httptools 0.5.0) and CVE-2023-30589 (llhttp 8.1.1 in httptools 0.6.0). |
| uvloop stream receive buffer | `256,000` bytes (250 KiB) | `(uvloop 0.23.0 uvloop/includes/consts.pxi, UV_STREAM_RECV_BUF_SIZE)` | One buffer per event loop; no C buffer allocation per read, but one `bytes` copy of `nread` bytes per read. |
| Slow callback threshold | `0.1` seconds (100 ms) | `(uvloop 0.23.0 uvloop/loop.pyx, Loop.__cinit__)` sets `slow_callback_duration` | In debug mode `Loop._on_idle` logs `Executing %s took %.3f seconds` above this threshold. |
| Uvicorn request-body high-water mark | `65,536` bytes (64 KiB) | `(uvicorn 0.54.0 uvicorn/protocols/http/flow_control.py, HIGH_WATER_LIMIT)` | `HttpToolsProtocol.on_body` calls `flow.pause_reading()` when the buffered request body exceeds it. |
| Maximum timer delay | `3,153,600,000` s (`3600 * 24 * 365 * 100`, about 100 years) | `(uvloop 0.23.0 uvloop/loop.pyx, MAX_SLEEP)` | `call_later` clamps larger or infinite delays to this value, which also bounds `asyncio.sleep`. |
| `httptools` parsing throughput | `1,239,025` req/s (runs: 0.88M to 1.84M) | Measured: `benchmark_parser_throughput(payload, iterations=100_000)` from the lab | New `HttpRequestParser` plus `feed_data` per request on the 76-byte `GET /health` payload from `test_parser_throughput_speedup_differential`. |
| `h11` parsing throughput | `61,103` req/s (runs: 41k to 68k) | Measured: same call, same payload | New `h11.Connection(h11.SERVER)` plus `receive_data` and `next_event` until `NEED_DATA` per request. |
| Throughput ratio | `21.5x` median per-run ratio (runs: 14.2x to 35.5x) | Measured: `speedup_ratio` from the same five runs | Parser-only microbenchmark; it says nothing about end-to-end server throughput, which also pays for the loop, the ASGI app, and the network. |
| 1-byte chunk feeding | `107,332` req/s, vs `249,506` req/s for one `feed()` | Measured: `HttpToolsParserCollector().feed_chunks([bytes([b]) for b in payload])` 20,000 times per run, vs `HttpToolsParserCollector().feed(payload)` | Pathological 1-byte segmentation costs about 2.3x here, because every byte becomes a `feed_data` call plus span callbacks. |

## Portability

The portability matrix reflects upstream release artifacts and source checks; only the CPython 3.14.6 macOS arm64 leg (the lab) and a 3.15.0b3 free-threaded import check were executed for this note, and the other CPython legs and container environments were not run (lab checks paused by the user on 2026-11-11) `(as of 2026-11)`.

| Runtime / Platform | uvloop 0.23.0 Support | httptools 0.9.0 Support | HTTP/1.1 Support | HTTP/2 Support | HTTP/3 (QUIC) Support | Verified Production Status |
|---|---|---|---|---|---|---|
| CPython 3.12.16 (Linux x86_64 / arm64) | `cp312` manylinux wheels (`epoll`) | `cp312` manylinux wheels | Supported | Via [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers\|Hypercorn]] / [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI\|Granian]] | Via Hypercorn with `aioquic` | Wheel tags on PyPI; lab not run on this leg. |
| CPython 3.13.18 (Linux x86_64 / arm64) | `cp313` manylinux wheels (`epoll`) | `cp313` manylinux wheels | Supported | Via [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers\|Hypercorn]] / [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI\|Granian]] | Via Hypercorn with `aioquic` | Wheel tags on PyPI; lab not run on this leg. |
| CPython 3.14.6 (macOS 27.3 arm64) | `kqueue`; lab reports libuv `1.52.1` | llhttp 9.4.3 | Supported | Via [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers\|Hypercorn]] / [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI\|Granian]] | Via Hypercorn with `aioquic` | Lab run: 8 tests passed; 3.14.10 itself was not run. |
| CPython 3.15.2 (Linux musl / Alpine) | `cp315` `musllinux_1_2` wheels for x86_64 and aarch64 | `cp315` `musllinux_1_2` wheels for x86_64, aarch64, ppc64le, riscv64 | Supported | Via [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers\|Hypercorn]] / [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI\|Granian]] | Via Hypercorn with `aioquic` | Prebuilt wheels, no compiler needed on those arches; source builds only elsewhere; lab not run. |
| CPython 3.15.2t (Free-Threaded, PEP 703) | `cp315t` wheels; `# cython: freethreading_compatible=True` in `uvloop/loop.pyx` | `cp315t` wheels; `-DCYTHON_FREETHREADING_COMPATIBLE=1` in `setup.py` `CFLAGS` | Supported | Via [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers\|Hypercorn]] / [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI\|Granian]] | Via Hypercorn with `aioquic` | Run 2026-11-15 on macOS 27.3 arm64 with CPython 3.15.0b3 free-threaded (3.15.2t itself was not run): `import httptools` and `import uvloop` left `sys._is_gil_enabled()` False, and `uvloop.new_event_loop()` worked; each loop stays single-threaded per instance. |
| Windows 11 / Server 2022 (x86_64) | Unsupported: no Windows wheels, `setup.py` raises `RuntimeError('uvloop does not support Windows at the moment')` | `win32`, `win_amd64`, and `win_arm64` wheels | Supported | Via [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers\|Hypercorn]] / [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI\|Granian]] | Via Hypercorn with `aioquic` | Uvicorn uses `asyncio.ProactorEventLoop`, or `SelectorEventLoop` with reload or workers (uvicorn 0.54.0 `uvicorn/loops/asyncio.py`). |
| Uvicorn 0.54.0 | `uvicorn[standard]` installs it except on win32, cygwin, and PyPy; `loop="auto"` uses it when importable | `uvicorn[standard]` requires `httptools>=0.8.0`; `http="auto"` prefers it | Supported | Opt-in: `http2=True` requires `http="zttp"` (`uvicorn/config.py`, `Config.load`) | Unsupported | Default high-throughput Python API configuration. |
| Hypercorn 0.18.0 | `--worker-class uvloop` (`hypercorn/asyncio/run.py`, `uvloop_worker`) | Not used (`h11` for HTTP/1.1, `h2` for HTTP/2) | Supported | Supported (native `h2`) | Supported (`aioquic`, `hypercorn/protocol/h3.py`) | Prefers native HTTP/2 and HTTP/3 multiplexing. |
| Granian 2.8.7 | Optional: `loop="auto"` picks `rloop`, then `uvloop`, then `winloop`, then `asyncio` (`granian/_loops.py`, `build_auto_loop`) | Not used: HTTP parsing in Rust Hyper | Supported | Supported (native Hyper) | Unsupported: `HTTPModes` offers only `auto`, `1`, `2` (`granian/constants.py`) | Sockets and HTTP framing in Rust; ASGI coroutines still run on a Python event loop. |

## Worked example

The executable lab implementation is located at [implementation](../labs/src/api_labs/f12_asgi_servers_and_async_runtime/c_http_framing.py) and its test suite is at [test](../labs/tests/f12_asgi_servers_and_async_runtime/test_uvloop_and_httptools_internals_libuv_and_c_http_parsing.py).
The following excerpt illustrates the streaming push collector bridging `httptools` C callbacks across chunk boundaries and benchmarking against `h11`:

```python
# Simplified excerpt from labs/src/api_labs/f12_asgi_servers_and_async_runtime/c_http_framing.py
from dataclasses import dataclass, field
import httptools
import h11


@dataclass(slots=True)
class ParsedHttpRequest:
    method: str = ""
    url: bytes = b""
    http_version: str = "1.1"
    headers: list[tuple[bytes, bytes]] = field(
        default_factory=lambda: list[tuple[bytes, bytes]]()
    )
    body: bytes = b""
    chunk_count: int = 0
    completed: bool = False
    upgrade: bool = False


class HttpToolsParserCollector:
    """Protocol collector bridging httptools C callbacks to structured messages."""

    def __init__(self) -> None:
        self.parser: httptools.HttpRequestParser = httptools.HttpRequestParser(self)
        self.messages: list[ParsedHttpRequest] = []
        self.callback_log: list[str] = []
        self._current_msg: ParsedHttpRequest | None = None
        self._current_url: bytes = b""

    def on_message_begin(self) -> None:
        self.callback_log.append("on_message_begin")
        self._current_msg = ParsedHttpRequest()
        self._current_url = b""

    def on_url(self, url: bytes) -> None:
        self.callback_log.append(f"on_url:{url.decode('latin-1')}")
        self._current_url += url

    def on_header(self, name: bytes, value: bytes) -> None:
        name_lower = name.lower()
        self.callback_log.append(
            f"on_header:{name_lower.decode('latin-1')}={value.decode('latin-1')}"
        )
        if self._current_msg is not None:
            self._current_msg.headers.append((name_lower, value))

    def on_headers_complete(self) -> None:
        self.callback_log.append("on_headers_complete")
        if self._current_msg is not None:
            raw_method = self.parser.get_method()
            self._current_msg.method = raw_method.decode("ascii")
            self._current_msg.url = self._current_url
            self._current_msg.http_version = self.parser.get_http_version()
            self._current_msg.upgrade = bool(self.parser.should_upgrade())

    def on_body(self, body: bytes) -> None:
        self.callback_log.append(f"on_body:{len(body)}b")
        if self._current_msg is not None:
            self._current_msg.body += body

    def on_chunk_header(self) -> None:
        self.callback_log.append("on_chunk_header")
        if self._current_msg is not None:
            self._current_msg.chunk_count += 1

    def on_chunk_complete(self) -> None:
        self.callback_log.append("on_chunk_complete")

    def on_message_complete(self) -> None:
        self.callback_log.append("on_message_complete")
        if self._current_msg is not None:
            self._current_msg.completed = True
            self.messages.append(self._current_msg)
            self._current_msg = None

    def feed_chunks(self, chunks: list[bytes]) -> None:
        for chunk in chunks:
            if chunk:
                self.parser.feed_data(chunk)
```

The corresponding verification test verifies 1-byte chunk boundary reconstruction, RFC 9112 Section 7.1 chunked encoding, pipelined request handling, syntax rejection, and throughput differentials:

```python
# Simplified excerpt from labs/tests/f12_asgi_servers_and_async_runtime/test_uvloop_and_httptools_internals_libuv_and_c_http_parsing.py
@pytest.mark.unit
def test_httptools_chunk_boundary_header_stitching() -> None:
    collector = HttpToolsParserCollector()
    raw = (
        b"GET /api/v1/stream HTTP/1.1\r\n"
        b"Host: internal.service\r\n"
        b"X-Custom-Split-Token: SecretPayloadValue1234\r\n"
        b"\r\n"
    )
    # Feed 1 byte at a time to force fragmentation across every token boundary
    collector.feed_chunks([bytes([b]) for b in raw])

    assert len(collector.messages) == 1
    msg = collector.messages[0]
    assert msg.completed is True
    assert msg.method == "GET"
    headers_dict = dict(msg.headers)
    assert headers_dict[b"x-custom-split-token"] == b"SecretPayloadValue1234"
```

The test execution output captured on 2026-11-15 on macOS 27.3 arm64 under CPython 3.14.6:

```text
$ uv run --frozen pytest tests/f12_asgi_servers_and_async_runtime/test_uvloop_and_httptools_internals_libuv_and_c_http_parsing.py -q
........                                                                 [100%]
8 passed in 0.18s
```

## Pitfalls

> [!warning] Pitfall 1: Premature Assembly of Split URL, Body, or Raw llhttp Spans Across TCP Chunks
> **Trigger:** Writing an `httptools` protocol whose `on_url` or `on_body` assigns instead of appending, or binding `llhttp` directly (C, `cffi`, `ctypes`) and treating each `on_header_field` or `on_header_value` call as a complete token.
> **Symptom:** Truncated request targets, partial bodies, or truncated header values and missing authentication tokens when clients or proxies split a request across TCP segments.
> **Fix:** With `httptools`, rely on `on_header(name, value)`, which `_maybe_call_on_header` emits only after the whole pair is buffered, and accumulate `on_url` and `on_body` fragments as Uvicorn does (`self.url += url`); with raw `llhttp`, accumulate span fragments until the next field or `on_headers_complete`.

> [!warning] Pitfall 2: HTTP Request Smuggling via Lenient Header Delimiters (CVE-2023-30589, CVE-2022-32214)
> **Trigger:** Running a backend `llhttp` that accepts bare CR (CVE-2023-30589) or bare LF (CVE-2022-32214) as a header line terminator behind a front-end proxy that forwards such bytes as part of a field value, or turning those leniencies back on with `set_dangerous_leniencies`.
> **Symptom:** HTTP request smuggling (HRS) where an attacker smuggles unauthorized backend requests inside what the front-end proxy parsed as an innocent single message body.
> **Fix:** Use `httptools >= 0.6.0` (bundles `llhttp` 8.1.1, fixes CVE-2023-30589) and preferably `>= 0.6.3` (bundles `llhttp` 9.2.1, fixes CVE-2024-27982); never enable `lenient_optional_cr_before_lf`, `lenient_optional_lf_after_cr`, or `lenient_spaces_after_chunk_size` in production, and audit what your server enables itself: Uvicorn 0.54.0 calls `set_dangerous_leniencies(lenient_data_after_close=True)` in `HttpToolsProtocol.__init__`.

> [!warning] Pitfall 3: Blocking the Single-Threaded libuv Event Loop with Synchronous CPU Work
> **Trigger:** Calling blocking cryptographic routines, synchronous database drivers, or `time.sleep()` directly inside an `async def` route on a `uvloop` worker.
> **Symptom:** Global server lockup; all active connections stall, health probes fail, and in debug mode the loop logs warnings such as `Executing <Handle ...> took 0.450 seconds` (format `'Executing %s took %.3f seconds'` in `Loop._on_idle`).
> **Fix:** Offload CPU-heavy or blocking synchronous calls to worker threads via `asyncio.to_thread` or an external `ProcessPoolExecutor`, preserving libuv's I/O polling reactivity.

> [!warning] Pitfall 4: Windows Deployment Failure and Import Crashes
> **Trigger:** Including `uvloop` unconditionally in a production `requirements.txt` or `pyproject.toml` file deployed to Windows developer machines or Windows Server containers.
> **Symptom:** `pip install` finds no Windows wheel, falls back to the sdist, and its `setup.py` raises `RuntimeError: uvloop does not support Windows at the moment`; with the dependency excluded but the import unguarded, the runtime fails with `ModuleNotFoundError: No module named 'uvloop'`.
> **Fix:** Use an environment marker, for example `uvloop==0.23.0; sys_platform != "win32"`, and select the loop conditionally: `uvloop.run(main())` or `asyncio.run(main(), loop_factory=uvloop.new_event_loop)` under `if sys.platform != "win32":`; avoid `uvloop.EventLoopPolicy` and `uvloop.install()`, which uvloop 0.23.0 marks deprecated and removes on Python 3.16.

## Security and robustness

### HTTP request smuggling threat model (RFC 9112 Section 11.2)

HTTP/1.1 message boundaries are governed by two distinct headers: `Content-Length` (RFC 9112 Section 6.2) and `Transfer-Encoding: chunked` (RFC 9112 Section 6.1 and Section 7.1).
When an intermediary reverse proxy and a backend ASGI server disagree on which header takes precedence, or disagree on how invalid delimiters are handled, an attacker can manipulate message framing.
In a CL.TE smuggling attack, the front-end proxy uses `Content-Length` while the backend `llhttp` parser uses `Transfer-Encoding`.
RFC 9112 Section 6.3 (rule 3) says:
If a message is received with both `Transfer-Encoding` and `Content-Length`, `Transfer-Encoding` overrides `Content-Length`, such a message "might indicate an attempt to perform request smuggling" and "ought to be handled as an error"; Section 6.1 adds that a server MAY reject such a request and MUST close the connection after responding.
`llhttp` 9.4.3 rejects that combination outright: with `Content-Length` first it fails with `HPE_INVALID_TRANSFER_ENCODING` ("Transfer-Encoding can't be present with Content-Length"), and with `Transfer-Encoding` first with `HPE_INVALID_CONTENT_LENGTH` ("Content-Length can't be present with Transfer-Encoding").
It rejects duplicate `Content-Length` headers with `HPE_UNEXPECTED_CONTENT_LENGTH` ("Duplicate Content-Length"), a request whose final transfer coding is not `chunked` with "Request has invalid `Transfer-Encoding`" (RFC 9112 Section 6.3 rule 4), and whitespace before the colon with `HPE_INVALID_HEADER_TOKEN` (RFC 9112 Section 5.1).
It accepts `Transfer-Encoding: <tab>chunked`, because a leading tab is optional whitespace that RFC 9112 Section 5.1 tells parsers to strip; these behaviors were all observed by feeding the bytes to httptools 0.9.0 on CPython 3.14.6.

### CVE-2023-30589 and CVE-2022-32214 delimiter vulnerability analysis

Historically, `llhttp` accepted a bare CR (`\r`) without a following LF (`\n`) as a header field delimiter (CVE-2023-30589, GHSA-cggh-pq45-6h9x, fixed in `llhttp` 8.1.1 and httptools 0.6.0).
Earlier, `llhttp` accepted a bare LF (`\n`) without a preceding CR (`\r`) as a header field delimiter (CVE-2022-32214, GHSA-q5vx-44v4-gch4, fixed in the `llhttp` 6.0.9 bundled by httptools 0.5.0).
RFC 9112 Section 2.2 says a recipient of a bare CR "MUST consider that element to be invalid or replace each bare CR with SP", while it "MAY recognize a single LF as a line terminator"; the danger is two parsers in one chain making different choices.
If a reverse proxy forwarded bare CR characters verbatim inside a header value, the proxy treated an inner smuggled header as part of that value, while a vulnerable `llhttp` parsed it as an independent header.
The following is a simplified illustration, not a tested exploit; `Content-Length: 47` covers every byte after the blank line:

```http
POST / HTTP/1.1\r\n
Host: example.com\r\n
Content-Length: 47\r\n
Foo: bar\rTransfer-Encoding: chunked\r\n
\r\n
0\r\n\r\n
GET /admin HTTP/1.1\r\n
Host: example.com\r\n
\r\n
```

The proxy sees `Foo` with value `bar\rTransfer-Encoding: chunked` and frames the message by `Content-Length`, forwarding all 47 body bytes as one request.
A backend that splits the line at the bare CR and prefers `Transfer-Encoding` stops at the `0\r\n\r\n` chunk and treats `GET /admin` as the next pipelined request; whether a given vulnerable `llhttp` release accepted the request also depended on its `Content-Length` plus `Transfer-Encoding` handling, which this note did not test on old releases.
httptools 0.9.0 with `llhttp` 9.4.3 rejects this exact payload (without the `Content-Length` line) at the bare CR with `HPE_LF_EXPECTED` ("Missing expected LF after header value"), and rejects a bare LF in the header section with "Missing expected CR after header value", in its default strict mode.

### Pipelining memory exhaustion and flow control

Under HTTP/1.1 pipelining, a client can transmit multiple HTTP requests over a single connection without waiting for individual responses, and the server MUST send the responses in request order (RFC 9112 Section 9.3.2).
If an attacker sends 10,000 pipelined requests in rapid succession, a naive server that spawns ASGI coroutines for all 10,000 requests simultaneously will suffer out-of-memory (OOM) termination.
Uvicorn's `HttpToolsProtocol` defends against this through flow control (uvicorn 0.54.0 `uvicorn/protocols/http/httptools_impl.py`, `HttpToolsProtocol.on_headers_complete`):
When a request's headers complete while the previous `RequestResponseCycle` has not finished its response, Uvicorn calls `self.flow.pause_reading()` and appends the cycle to `self.pipeline`, an unbounded `collections.deque`.
Pausing reads stops libuv from reading from the socket, so the kernel receive buffer fills and the advertised TCP window shrinks toward the client.
The queue is bounded in practice only by what was already read: every pipelined request inside the current `data_received` buffer (up to 256000 bytes per uvloop read) is parsed before the pause takes effect.
Only when `on_response_complete()` finishes the active response does Uvicorn call `self.flow.resume_reading()` and pop the next cycle from the pipeline to start it.

## The principal's view

From an enterprise architectural perspective, choosing between `uvloop` + `httptools`, pure-Python `asyncio` + `h11`, and native Rust runtimes like `Granian` represents a fundamental engineering tradeoff between runtime simplicity, operational overhead, and raw throughput.

```
+-----------------------------------------------------------------------------------+
| Runtime Architecture Comparison: Python vs Cython vs Rust                         |
+-----------------------------------------------------------------------------------+
| Metric / Characteristic    | asyncio + h11      | uvloop + httptools | Granian (Rust)     |
+----------------------------+--------------------+--------------------+--------------------+
| Loop Engine                | Python Selectors   | C libuv 1.52.1     | Rust Tokio I/O     |
| HTTP Parser                | Pure Python Regex  | C llhttp 9.4.3     | Rust Hyper         |
| Multi-threading Model      | 1 GIL Loop / Proc  | 1 GIL Loop / Proc  | Multi-thread Tokio |
| Cross-Platform (Windows)   | Native Support     | uvloop POSIX Only  | Native Support     |
| Build Risk                 | None (Pure Python) | Wheels; no Windows | Pre-built Wheels   |
| Parser Microbenchmark      | ~61k req/s         | ~1.24M req/s       | Not measured       |
+----------------------------+--------------------+--------------------+--------------------+
```

The parser row is this note's measured lab microbenchmark (median of five runs, CPython 3.14.6, macOS 27.3 arm64); memory per worker and Granian throughput were not measured, so they are left out.
For most standard microservices, `uvicorn[standard]` (bundling `uvloop` and `httptools`) is a sound default; the uvloop project's own README claims "uvloop makes asyncio 2-4x faster", a figure from its own benchmarks that this note did not re-measure end to end.
However, principal engineers must recognize the operational boundaries:
1. uvloop 0.23.0 and httptools 0.9.0 ship `musllinux_1_2` wheels for Alpine on x86_64 and aarch64, so container builds on those arches need no compiler; on other arches, or when a new CPython ships before wheels do, the sdist build needs a C toolchain and can be flaky.
2. In multi-tenant environments with strict zero-CVE policies, embedded C libraries like `llhttp` and `libuv` introduce third-party vulnerability vectors that cannot be remediated simply by upgrading Python standard library packages; they require tracking Cython wheel point releases.
3. For gateway workloads with very high connection counts or strict tail-latency budgets, even `uvloop` remains limited by one GIL-holding loop per process and by per-request Python object allocation (the `bytes` copies, the ASGI `scope` dictionary, and the task).
In those specific architectural domains, migrating the server tier to Rust-based engines ([[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI]]) or dedicated reverse proxies (Envoy, Cloudflare Pingora) is the superior architectural decision.

## Interview questions

> [!question] L5: Explain the difference between httptools and pure-Python h11 in terms of memory allocation and execution model.
>
> > [!success]- Answer
> > `httptools` compiles Node's `llhttp` state machine into a C extension that parses directly over the buffer of the `bytes` object passed to `feed_data`, with no copy on entry.
> > It pushes parsing events to the application by calling the protocol's bound methods from C callbacks; each span callback allocates one `bytes` object for its span (`at[:length]`), but no event objects are created.
> > In contrast, `h11` is a pure-Python Sans-I/O library that appends received bytes to an internal `bytearray` and requires the application to pull events in a while loop.
> > Every event in `h11` instantiates a Python dataclass such as `Request`, `Data`, or `EndOfMessage`, on top of regex matching and slicing, which adds allocation churn and garbage collector pressure.
> >
> > Follow-up: Why does `httptools` require an adapter protocol to handle fragmented headers across TCP packet boundaries?

> [!question] L5: Predict the output: Given an HTTP parser receiving b"POST /v1 HTTP/1.1\r\nContent-" in chunk 1 and b"Length: 4\r\n\r\ntest" in chunk 2, what callback sequence does httptools emit?
>
> > [!success]- Answer
> > Chunk 1 causes `llhttp` to invoke `on_message_begin`, `on_url` with `b"/v1"`, and, when it flushes the open span at the end of the buffer, `cb_on_header_field` with `b"Content-"`.
> > Because the header field is incomplete, no `on_header` event reaches the protocol and `_current_header_name` buffers `b"Content-"`.
> > Chunk 2 causes `llhttp` to invoke `cb_on_header_field` with `b"Length"`, concatenating the name to `b"Content-Length"`, followed by `cb_on_header_value` with `b"4"`.
> > `cb_on_headers_complete` then runs `_on_headers_complete`, whose `_maybe_call_on_header` emits `on_header(b"Content-Length", b"4")` with the name's case preserved, followed by `on_headers_complete`, `on_body(b"test")`, and `on_message_complete`.
> > Running exactly these two chunks through httptools 0.9.0 produced `on_message_begin` and `on_url(b"/v1")` during chunk 1 and all the remaining protocol callbacks during chunk 2.
> >
> > Follow-up: What happens if chunk 2 contains an illegal character such as a space before the colon?

> [!question] L6: Why does uvloop achieve substantially higher I/O throughput than CPython's default asyncio event loop on Linux?
> Detail the role of uv_run and the GIL.
>
> > [!success]- Answer
> > Default `asyncio` executes its loop logic in pure Python: `selectors.select()` returns a list of Python tuples, ready handles go through a Python `deque`, and timers through a `heapq`.
> > `uvloop` replaces this loop with libuv 1.52.1 written in C, calling `uv_run` inside a `with nogil:` block that releases the Python GIL during kernel polling in `epoll_wait`, so readiness dispatch, timers, and handle bookkeeping run in C.
> > Socket reads land in one 256000-byte buffer per loop (`Loop._recv_buffer`), so no C buffer is allocated per read; the per-read `bytes` copy passed to `data_received` remains, just as `sock.recv()` creates one in `asyncio`.
> > Socket writes try an immediate `write(2)` (or `uv_try_write` for several buffers) when libuv's write queue is empty, and only fall back to a queued `uv_write` request when the kernel cannot take all the data.
> >
> > Follow-up: How does `uvloop` schedule and execute Python callbacks queued via `call_soon`?

> [!question] L6: Debug this API: An engineer wraps llhttp directly (through `cffi`) instead of using httptools, and under high concurrency the service occasionally corrupts the first header of incoming requests.
> Inspect the following snippet and identify the defect:
> ```python
> def on_header_field(self, name: bytes) -> None:
>     self.current_name = name
>
>
> def on_header_value(self, value: bytes) -> None:
>     self.headers[self.current_name] = value
> ```
>
> > [!success]- Answer
> > The implementation assumes that `on_header_field` and `on_header_value` are each called exactly once per header with the complete byte sequence.
> > In reality, `llhttp` is a streaming parser that invokes span callbacks incrementally whenever TCP packet boundaries split a field name or value across `llhttp_execute` calls.
> > If a header field name is split (for example, `b"Authori"` and `b"zation"`), `self.current_name` is overwritten with the trailing fragment `b"zation"`, and a split value likewise overwrites the earlier part, corrupting the header map.
> > The fix requires concatenating fragments into an accumulator (`self.current_name += name`) and only finalizing the header pair when transitioning to the next header or inside `on_headers_complete`, which is exactly what `httptools` does before calling `on_header`.
> >
> > Follow-up: Why must header field names be converted to lowercase when constructing an ASGI scope dictionary?

> [!question] L6: How does Uvicorn prevent HTTP/1.1 pipelining from causing out-of-memory exhaustion when using httptools?
>
> > [!success]- Answer
> > When multiple pipelined HTTP requests arrive in a single TCP read buffer, `llhttp` fires `on_headers_complete` sequentially for each message.
> > If Uvicorn detects that the current `RequestResponseCycle` has not completed its response, it calls `self.flow.pause_reading()` on the transport and enqueues the subsequent request into `self.pipeline`.
> > Pausing the transport tells libuv to stop reading from the socket descriptor, so the kernel receive buffer fills, the TCP receive window closes, and the client stops sending.
> > When the active response completes, Uvicorn calls `self.flow.resume_reading()`, pops the next cycle from the pipeline, and starts it, processing pipelined requests strictly in order without unbounded concurrency.
> >
> > Follow-up: What error response does Uvicorn return if `limit_concurrency` is exceeded while processing pipelined requests?

> [!question] L7: Detail the memory architecture of uvloop's socket read path.
> Why does uvloop use a fixed 250 KiB buffer instead of dynamically allocating buffers per socket connection?
>
> > [!success]- Answer
> > In `uvloop/loop.pxd`, the `Loop` extension type embeds an array `char _recv_buffer[UV_STREAM_RECV_BUF_SIZE]` where `UV_STREAM_RECV_BUF_SIZE = 256000` (250 KiB).
> > Because libuv runs on a single thread and processes I/O events serially, only one socket read callback executes at any given point in time, and `_recv_buffer_in_use` turns a violation into a `RuntimeError('concurrent allocations')`.
> > By pointing `uv_buf_t.base` at this shared loop buffer in `__loop_alloc_buffer`, `uvloop` avoids allocating and freeing a C receive buffer on every read; `__uv_stream_on_read_impl` then copies the `nread` bytes into one `bytes` object for `data_received`.
> > Dynamically allocating a 256000-byte buffer per socket connection would multiply resident memory across tens of thousands of idle connections and churn the allocator; protocols that need zero-copy reads use `asyncio.BufferedProtocol`, which `uvloop` serves through `__uv_stream_buffered_alloc` and `get_buffer()`.
> >
> > Follow-up: How does `uvloop` handle SSL/TLS streams where OpenSSL requires dedicated memory buffers for decryption?

> [!question] L7: Trace the root cause of HTTP Request Smuggling vulnerabilities (such as CVE-2023-30589 and CVE-2022-32214) in llhttp.
> How must an infrastructure architect configure reverse proxies and ASGI servers to prevent framing desynchronization?
>
> > [!success]- Answer
> > The vulnerabilities arose from lenient parsing in `llhttp` where bare CR (`\r`, CVE-2023-30589) or bare LF (`\n`, CVE-2022-32214) octets were accepted as valid line delimiters for HTTP header fields.
> > Front-end reverse proxies that kept such octets inside a field value, instead of rejecting or replacing a bare CR as RFC 9112 Section 2.2 requires, forwarded them as opaque value bytes.
> > This allowed attackers to hide smuggled `Content-Length` or `Transfer-Encoding` headers that were ignored by the front-end proxy but interpreted as framing boundaries by `llhttp`.
> > To eliminate this attack vector, architects must ensure reverse proxies enforce strict CRLF framing, normalize or reject ambiguous headers, and keep `httptools` at `>= 0.6.0` (`llhttp` 8.1.1), preferably `>= 0.6.3` (`llhttp` 9.2.1, CVE-2024-27982), with the dangerous lenient flags disabled.
> >
> > Follow-up: How does RFC 9112 Section 6.3 resolve ambiguity when both `Content-Length` and `Transfer-Encoding: chunked` are present in a request?

> [!question] L7: How does uvloop 0.23.0 interact with free-threaded CPython (PEP 703) in Python 3.13t-3.15t?
> What invariants must be preserved when running multiple event loops across operating system threads?
>
> > [!success]- Answer
> > `uvloop` 0.23.0 compiles `loop.pyx` with the Cython directive `# cython: freethreading_compatible=True` and ships `cp314t` and `cp315t` wheels, so the extension declares that it does not need the GIL, and on CPython 3.15.0b3 free-threaded (macOS arm64, 2026-11-15) importing `httptools` and `uvloop` left `sys._is_gil_enabled()` False; the `-DCYTHON_FREETHREADING_COMPATIBLE=1` compiler flag belongs to httptools' `setup.py`, not uvloop's.
> > However, libuv's internal data structures (`uv_loop_t`, handles, and queues) are not thread-safe.
> > `uvloop` records `self._thread_id = PyThread_get_thread_ident()` in `Loop._run`, but `call_soon` and `call_later` only check it through `_check_thread` in debug mode, so cross-thread misuse in production goes undetected rather than raising.
> > To scale across multiple CPU cores in free-threaded Python, an application must run one distinct `uvloop.Loop` instance per OS thread, communicating strictly via thread-safe queues or `call_soon_threadsafe`.
> >
> > Follow-up: What mechanism does `uvloop` use in `call_soon_threadsafe` to wake up libuv's polling loop from another operating system thread?

## Related

Prerequisites: [[Eager-Task-Factory-in-Python-3-12-to-3-15-and-Execution-Order-Shifts]]

Next: [[Uvicorn-Internals-Server-Lifecycle-Protocols-and-Flow-Control]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[CPython-Asyncio-Event-Loop-Internals-Selectors-Ready-Queue-and-Timers]], [[End-to-End-Backpressure-TCP-Windows-ASGI-Send-Await-and-Bounded-Queues]], [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI]], [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers]]

- Lab: `labs/tests/f12_asgi_servers_and_async_runtime/test_uvloop_and_httptools_internals_libuv_and_c_http_parsing.py`

## Further reading

- [RFC 9112: HTTP/1.1 Specification](https://www.rfc-editor.org/rfc/rfc9112.html)
  Defines formal HTTP/1.1 message framing, bare CR and LF handling (Section 2.2), chunked transfer coding (Section 7.1), message body length precedence (Section 6.3), and request smuggling security considerations (Section 11.2).
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
  Standardizes HTTP architecture, method semantics, connection management (Section 7.6.1), and protocol upgrade rules (Section 7.8).
- [MagicStack uvloop Repository](https://github.com/MagicStack/uvloop)
  Official source code for the Cython libuv replacement loop, detailing `Loop.__run`, memory buffer layouts, and POSIX stream bindings.
- [MagicStack httptools Repository](https://github.com/MagicStack/httptools)
  Official source code for Cython bindings to Node.js `llhttp`, documenting C callback mappings and streaming parser state machines.
- [Node.js llhttp Parser Repository](https://github.com/nodejs/llhttp)
  High-performance C HTTP parser generated by `llparse` from TypeScript, succeeding Ryan Dahl's http-parser; its README documents every dangerous leniency flag.
- [libuv Design and Architecture Guide](https://github.com/libuv/libuv)
  Comprehensive architectural overview of libuv's multiplatform asynchronous I/O event loop, handle hierarchies, and polling phases.
- [Encode Uvicorn Server Implementation](https://github.com/encode/uvicorn)
  Reference ASGI web server implementation illustrating `HttpToolsProtocol` integration, keep-alive timers, and backpressure flow control.
- [Python Sans-I/O h11 Documentation](https://github.com/python-hyper/h11)
  Pure-Python reference HTTP/1.1 state machine implementation demonstrating pull-based event generation and receive buffer management.
