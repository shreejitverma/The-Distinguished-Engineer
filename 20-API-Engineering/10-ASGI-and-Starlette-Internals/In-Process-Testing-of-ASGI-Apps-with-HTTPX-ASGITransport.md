---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Testing ASGI Applications Without Sockets: httpx.ASGITransport (0.28.1) vs Starlette TestClient (AnyIO Portal Thread Bridging)

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

Prerequisites: [[Starlette-AuthenticationMiddleware-SessionMiddleware-and-TrustedHost]]

Next: [[ASGI-State-Isolation-and-ContextVars-Across-Request-Lifecycle]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Proves how Starlette's synchronous TestClient spins up a background BlockingPortal event loop thread (which can hide cross-loop state bugs), whereas httpx.AsyncClient + ASGITransport runs in the same asyncio loop.

## Further reading
