---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# ASGI Servers and Async Runtime

Descends into the ASGI server and CPython `asyncio` runtime layer: Uvicorn (`0.54.0`), `uvloop` (`0.23.0`), `httptools` (`0.9.0`), Hypercorn (`0.18.0`), Granian (`2.8.7` in Rust/Tokio), and Gunicorn (`26.2.0`) process management.
Covers CPython `3.12` through `3.15` event loop internals (including `eager_task_factory` and `3.15t` free-threaded execution), AnyIO (`4.16.0`) structured concurrency, cancellation and shield semantics, TCP-to-ASGI backpressure watermarks, event-loop blocking detection, and zero-dropped-request graceful shutdown on `SIGTERM`.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [CPython asyncio Event Loop Internals: BaseEventLoop._run_once, epoll/kqueue Selector Polling, _ready Deque, and _scheduled Min-Heap](CPython-Asyncio-Event-Loop-Internals-Selectors-Ready-Queue-and-Timers.md)
2. [asyncio.eager_task_factory (Python 3.12-3.15): Synchronous First-Step Coroutine Execution, Throughput Gains, and Ordering Surprises](Eager-Task-Factory-in-Python-3-12-to-3-15-and-Execution-Order-Shifts.md)
3. [uvloop (0.23.0) and httptools (0.9.0) Internals: Cython libuv Loop Replacement and llhttp C Callback Framing](Uvloop-and-Httptools-Internals-Libuv-and-C-HTTP-Parsing.md)
4. [Uvicorn (0.54.0) Internals: Server.serve(), H11Protocol vs HttpToolsProtocol, FlowControl High/Low Watermarks, and --limit-concurrency](Uvicorn-Internals-Server-Lifecycle-Protocols-and-Flow-Control.md)
5. [Hypercorn (0.18.0) Internals: Multiplexed HTTP/2 (h2 4.4.1) and HTTP/3 QUIC (aioquic 1.7.2) ASGI Worker Architecture](Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers.md)
6. [Granian (2.8.7) Internals: Rust Tokio/Hyper HTTP/1 and HTTP/2 Engine, PyO3 Bridging, and ASGI vs RSGI Protocol Overhead](Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI.md)
7. [Process Management in Production: Gunicorn (26.2.0) vs uvicorn --workers vs Single-Process Kubernetes Pods and Memory Recycling](Multi-Process-Worker-Topologies-Gunicorn-Uvicorn-Workers-vs-Kubernetes-Replicas.md)
8. [Structured Concurrency with AnyIO (4.16.0) and asyncio.TaskGroup: CancelScopes, Sibling Cancellation, and except* ExceptionGroup Handling](AnyIO-Structured-Concurrency-TaskGroups-CancelScopes-and-ExceptionGroups.md)
9. [Cancellation Mechanics in Python APIs: Task.cancel(), Task.cancelling(), Why BaseException Includes CancelledError, and Shielded Finalizers](Cancellation-Semantics-CancelledError-Swallowing-and-Shielded-Cleanup.md)
10. [Backpressure Across the Stack: How a Slow Client Stalls TCP Send Buffers, Pauses Uvicorn Transport Writing, and Suspends ASGI send()](End-to-End-Backpressure-TCP-Windows-ASGI-Send-Await-and-Bounded-Queues.md)
11. [Detecting Event-Loop Blocking in Production: Loop-Lag Canary Tasks, loop.slow_callback_duration, and Stack-Sampling Watchdogs](Detecting-Event-Loop-Blocking-Loop-Lag-Monitors-and-Debug-Callbacks.md)
12. [Handling CPU-Bound Work Behind an Async API: ProcessPoolExecutor vs ThreadPool on GIL Builds vs Free-Threaded CPython (3.13t-3.15t)](Offloading-CPU-Bound-Work-ProcessPoolExecutor-vs-Free-Threaded-Python-3-15t.md)
13. [Why Unreferenced asyncio.create_task() Calls Disappear Under Garbage Collection and How to Build a Lifespan-Scoped Task Pool](Fire-and-Forget-Task-Hazards-GC-Disappearance-and-Managed-Task-Nurseries.md)
14. [Why Single-Threaded Asyncio Still Needs Locks: Yield-Point Interleaving Races, Semaphore Concurrency Caps, and Non-Reentrant Lock Deadlocks](Async-Synchronization-Primitives-Locks-Semaphores-and-Reentrant-Deadlocks.md)
15. [Zero-Dropped-Request Deploys: SIGTERM Handling in Uvicorn, Kubernetes Endpoint Removal Races, preStop Delays, and In-Flight Drain Timeouts](Graceful-Shutdown-SIGTERM-Drain-Windows-and-Kubernetes-PreStop-Hooks.md)
16. [Diagnosing Memory Leaks in Long-Running ASGI Workers: Unbounded lru_cache, Reference Cycles, Arena Fragmentation, and tracemalloc Diffs](Memory-Leaks-in-Long-Running-Python-API-Workers.md)
