---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Diagnosing Memory Leaks in Long-Running ASGI Workers: Unbounded lru_cache, Reference Cycles, Arena Fragmentation, and tracemalloc Diffs

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

Prerequisites: [[Graceful-Shutdown-SIGTERM-Drain-Windows-and-Kubernetes-PreStop-Hooks]]

Next: [[SQLAlchemy-2-x-Architecture-Core-vs-ORM-Engine-and-Unified-Select]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Uses tracemalloc snapshot diffs in pytest to detect and fix a per-request memory leak caused by @functools.lru_cache(maxsize=None) on an instance method and unclosed async generators.

## Further reading
