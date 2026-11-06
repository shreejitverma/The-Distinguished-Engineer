---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# asyncio.eager_task_factory (Python 3.12-3.15): Synchronous First-Step Coroutine Execution, Throughput Gains, and Ordering Surprises

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

Prerequisites: [[CPython-Asyncio-Event-Loop-Internals-Selectors-Ready-Queue-and-Timers]]

Next: [[Uvloop-and-Httptools-Internals-Libuv-and-C-HTTP-Parsing]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Proves that enabling loop.set_task_factory(asyncio.eager_task_factory) executes a coroutine synchronously up to its first blocking await, eliminating Task allocation when a cache hit returns immediately.

## Further reading
