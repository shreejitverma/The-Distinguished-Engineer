---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why Unreferenced asyncio.create_task() Calls Disappear Under Garbage Collection and How to Build a Lifespan-Scoped Task Pool

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

Prerequisites: [[Offloading-CPU-Bound-Work-ProcessPoolExecutor-vs-Free-Threaded-Python-3-15t]]

Next: [[Async-Synchronization-Primitives-Locks-Semaphores-and-Reentrant-Deadlocks]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Proves that calling asyncio.create_task() without storing a strong reference allows gc.collect() to destroy a suspended task mid-await, and builds a bounded lifespan TaskGroup nursery with backpressure and shutdown drain.

## Further reading
