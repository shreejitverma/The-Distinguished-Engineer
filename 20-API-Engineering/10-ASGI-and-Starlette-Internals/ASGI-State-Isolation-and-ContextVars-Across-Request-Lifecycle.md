---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Request-Scoped State Isolation: scope['state'] vs contextvars.ContextVar Across Middleware, Dependencies, and Background Tasks

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

Prerequisites: [[In-Process-Testing-of-ASGI-Apps-with-HTTPX-ASGITransport]]

Next: [[FastAPI-Architecture-What-FastAPI-Adds-Over-Starlette-and-Pydantic]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Traces a ContextVar correlation ID set in pure ASGI middleware through async route handlers, to_thread sync worker functions, and post-response BackgroundTasks.

## Further reading
