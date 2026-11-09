---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Handling CPU-Bound Work Behind an Async API: ProcessPoolExecutor vs ThreadPool on GIL Builds vs Free-Threaded CPython (3.13t-3.15t)

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

Prerequisites: [[Detecting-Event-Loop-Blocking-Loop-Lag-Monitors-and-Debug-Callbacks]]

Next: [[Fire-and-Forget-Task-Hazards-GC-Disappearance-and-Managed-Task-Nurseries]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Benchmarks a CPU-intensive JSON transformation + hashing endpoint under concurrent load using asyncio.to_thread (GIL-contended), ProcessPoolExecutor (IPC serialization cost), and free-threaded CPython.

## Further reading
