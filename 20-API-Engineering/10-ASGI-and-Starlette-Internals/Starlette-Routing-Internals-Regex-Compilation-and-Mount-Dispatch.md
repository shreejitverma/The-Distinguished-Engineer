---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Starlette (1.10.0) Routing Internals: Route Path Regex Compilation, Convertors, Mount Sub-Apps, Host Routing, and 405 vs 404 Resolution

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

Prerequisites: [[ASGI-Lifespan-Protocol-Startup-Shutdown-and-Scope-State]]

Next: [[Starlette-Request-Internals-Stream-Consumption-and-Body-Caching]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Traces Router.handle() and Route.matches() in Starlette 1.10.0 to show how partial matches across routes with identical paths but different HTTP methods accumulate to emit 405 Method Not Allowed with an Allow header.

## Further reading
