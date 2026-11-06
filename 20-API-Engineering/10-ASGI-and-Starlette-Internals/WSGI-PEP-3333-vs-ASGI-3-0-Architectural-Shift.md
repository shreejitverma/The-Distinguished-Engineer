---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# From WSGI (PEP 3333) to ASGI 3.0: Why Synchronous Single-Callable Interfaces Cannot Express Multiplexing, WebSockets, or Lifespan

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

Prerequisites: [[Migrating-Large-Codebases-from-Pydantic-v1-to-v2]]

Next: [[ASGI-3-0-Callable-Contract-Scope-Receive-and-Send]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Wraps a legacy synchronous WSGI callable inside an ASGI-to-WSGI threadpool adapter and demonstrates how long-held streaming connections exhaust worker threads under WSGI while multiplexing cleanly under ASGI.

## Further reading
