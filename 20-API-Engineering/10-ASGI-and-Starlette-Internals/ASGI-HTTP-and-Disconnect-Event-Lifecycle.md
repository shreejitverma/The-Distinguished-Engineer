---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# ASGI HTTP Message Format (v2.4): Request Streaming, Response Chunks, Early Client Disconnect (http.disconnect), and Zero-Copy Extensions

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

Prerequisites: [[ASGI-3-0-Callable-Contract-Scope-Receive-and-Send]]

Next: [[ASGI-Lifespan-Protocol-Startup-Shutdown-and-Scope-State]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Simulates an abrupt client disconnect mid-request and proves how an ASGI app awaiting receive() observes {'type': 'http.disconnect'} and cancels downstream work.

## Further reading
