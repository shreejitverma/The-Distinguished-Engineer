---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Starlette Exception Handling Onion: Why ServerErrorMiddleware Wraps the Outermost Layer and Why Mid-Stream Exceptions Cannot Return 500

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

Prerequisites: [[Why-BaseHTTPMiddleware-Hurts-Performance-Streaming-and-ContextVars]]

Next: [[ASGI-WebSockets-Lifecycle-Subprotocols-and-Backpressure]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Demonstrates the exact middleware onion order (ServerErrorMiddleware -> user middlewares -> ExceptionMiddleware -> Router) and proves that raising after http.response.start is sent raises RuntimeError instead of a 500 response.

## Further reading
