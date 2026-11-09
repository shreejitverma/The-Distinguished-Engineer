---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# async def vs def in FastAPI: Event Loop Execution vs anyio.to_thread.run_sync and the 40-Thread CapacityLimiter

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Yield-Dependencies-Inside-StreamingResponse-and-Resource-Lifecycle-Traps]]

Next: [[Response-Serialization-Pipeline-Response-Model-and-Double-Validation-Cost]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Demonstrates how 41 concurrent requests to a blocking def endpoint queue behind AnyIO's default 40-token CapacityLimiter while async def endpoints continue serving on the event loop, and how adjusting total_tokens changes queueing.

## Further reading
