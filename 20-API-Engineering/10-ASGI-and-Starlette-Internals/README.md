---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# ASGI and Starlette Internals

Traces the evolution of Python web server interfaces from WSGI (PEP 3333) to ASGI 3.0 and examines Starlette (`1.10.0`, released 2026-10-24) down to its source modules (`routing.py`, `requests.py`, `responses.py`, `middleware/base.py`, `middleware/exceptions.py`).
Covers the ASGI `scope`, `receive`, and `send` callable contract across HTTP, WebSocket, and Lifespan protocols.
Explains why `BaseHTTPMiddleware` breaks `contextvars` changes and buffers streaming responses compared to pure ASGI middleware, and how Starlette handles disconnects, streaming, and Server-Sent Events (`sse-starlette 3.6.0`).

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [From WSGI (PEP 3333) to ASGI 3.0: Why Synchronous Single-Callable Interfaces Cannot Express Multiplexing, WebSockets, or Lifespan](WSGI-PEP-3333-vs-ASGI-3-0-Architectural-Shift.md)
2. [The ASGI 3.0 Callable Contract: scope Dictionary, receive() Awaitable, and send() Event State Machine](ASGI-3-0-Callable-Contract-Scope-Receive-and-Send.md)
3. [ASGI HTTP Message Format (v2.4): Request Streaming, Response Chunks, Early Client Disconnect (http.disconnect), and Zero-Copy Extensions](ASGI-HTTP-and-Disconnect-Event-Lifecycle.md)
4. [The ASGI Lifespan Protocol (v2.0): lifespan.startup, lifespan.shutdown, Startup Failure Aborts, and Typed scope['state'] Sharing](ASGI-Lifespan-Protocol-Startup-Shutdown-and-Scope-State.md)
5. [Starlette (1.10.0) Routing Internals: Route Path Regex Compilation, Convertors, Mount Sub-Apps, Host Routing, and 405 vs 404 Resolution](Starlette-Routing-Internals-Regex-Compilation-and-Mount-Dispatch.md)
6. [Starlette Request Internals: request.stream() vs request.body(), _body Caching, and Why Reading the Stream in Middleware Starves Downstream Handlers](Starlette-Request-Internals-Stream-Consumption-and-Body-Caching.md)
7. [Starlette Response Internals: JSONResponse, StreamingResponse Client-Disconnect Listening, and FileResponse Chunked File I/O](Starlette-Responses-StreamingResponse-FileResponse-and-Background-Execution.md)
8. [Writing Pure ASGI Middleware: Wrapping scope, receive, and send to Inspect Headers, Mutate Status, and Enforce Limits Zero-Copy](Pure-ASGI-Middleware-Onion-Architecture-and-Send-Receive-Wrapping.md)
9. [The BaseHTTPMiddleware Trap: Task Spawning Overhead, Memory Buffering, ExceptionGroup Wrapping, and ContextVar Isolation](Why-BaseHTTPMiddleware-Hurts-Performance-Streaming-and-ContextVars.md)
10. [Starlette Exception Handling Onion: Why ServerErrorMiddleware Wraps the Outermost Layer and Why Mid-Stream Exceptions Cannot Return 500](Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware.md)
11. [WebSockets in ASGI and Starlette: Handshake, Subprotocol Negotiation, Close Codes (RFC 6455), and Concurrent Send/Receive Loops](ASGI-WebSockets-Lifecycle-Subprotocols-and-Backpressure.md)
12. [Server-Sent Events (WHATWG SSE) Internals: text/event-stream Framing, retry:, Last-Event-ID Resumption, Ping Keep-Alives, and sse-starlette (3.6.0)](Server-Sent-Events-SSE-Protocol-Last-Event-ID-and-SSE-Starlette.md)
13. [Starlette Security Middlewares: AuthenticationMiddleware, Signed Cookie SessionMiddleware (itsdangerous), HTTPSRedirect, and TrustedHostMiddleware](Starlette-AuthenticationMiddleware-SessionMiddleware-and-TrustedHost.md)
14. [Testing ASGI Applications Without Sockets: httpx.ASGITransport (0.28.1) vs Starlette TestClient (AnyIO Portal Thread Bridging)](In-Process-Testing-of-ASGI-Apps-with-HTTPX-ASGITransport.md)
15. [Request-Scoped State Isolation: scope['state'] vs contextvars.ContextVar Across Middleware, Dependencies, and Background Tasks](ASGI-State-Isolation-and-ContextVars-Across-Request-Lifecycle.md)
