---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The ASGI Lifespan Protocol (v2.0): lifespan.startup, lifespan.shutdown, Startup Failure Aborts, and Typed scope['state'] Sharing

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

Prerequisites: [[ASGI-HTTP-and-Disconnect-Event-Lifecycle]]

Next: [[Starlette-Routing-Internals-Regex-Compilation-and-Mount-Dispatch]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Tests lifespan startup and shutdown state transitions with asgi-lifespan 2.1.0, proving that yielding a typed state dict populates request.state and that raising during startup emits lifespan.startup.failed.

## Further reading
