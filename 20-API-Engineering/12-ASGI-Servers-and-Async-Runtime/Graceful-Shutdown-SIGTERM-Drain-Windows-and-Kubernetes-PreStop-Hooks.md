---
type: playbook
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Zero-Dropped-Request Deploys: SIGTERM Handling in Uvicorn, Kubernetes Endpoint Removal Races, preStop Delays, and In-Flight Drain Timeouts

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

Prerequisites: [[Async-Synchronization-Primitives-Locks-Semaphores-and-Reentrant-Deadlocks]]

Next: [[Memory-Leaks-in-Long-Running-Python-API-Workers]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Spawns a live Uvicorn worker with an in-flight 500ms request, sends SIGTERM, and proves that Uvicorn stops accepting new connections, completes the in-flight request with 200 OK, and runs ASGI lifespan.shutdown before exiting 0.

## Further reading
