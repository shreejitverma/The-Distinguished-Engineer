---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# uvloop (0.23.0) and httptools (0.9.0) Internals: Cython libuv Loop Replacement and llhttp C Callback Framing

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [ASGI Servers and Async Runtime](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Eager-Task-Factory-in-Python-3-12-to-3-15-and-Execution-Order-Shifts]]

Next: [[Uvicorn-Internals-Server-Lifecycle-Protocols-and-Flow-Control]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Feeds raw HTTP/1.1 byte chunks into both pure-Python h11 0.16.0 and C-extension httptools 0.9.0 HttpRequestParser, benchmarking parser throughput and callback invocation.

## Further reading
