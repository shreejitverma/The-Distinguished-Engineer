---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The BaseHTTPMiddleware Trap: Task Spawning Overhead, Memory Buffering, ExceptionGroup Wrapping, and ContextVar Isolation

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [ASGI and Starlette Internals](README.md) in [API Engineering](../README.md).

## TL;DR

## Mental model

## How it works

## Numbers and limits

## Portability

## Worked example

## Pitfalls

## Security and robustness

## The principal's view

## Interview questions

## Related

Prerequisites: [[Pure-ASGI-Middleware-Onion-Architecture-and-Send-Receive-Wrapping]]

Next: [[Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Proves in a pytest lab that mutating a ContextVar inside call_next in BaseHTTPMiddleware fails to propagate back to outer scope due to child task copying, and measures the throughput gap against pure ASGI middleware.

## Further reading
