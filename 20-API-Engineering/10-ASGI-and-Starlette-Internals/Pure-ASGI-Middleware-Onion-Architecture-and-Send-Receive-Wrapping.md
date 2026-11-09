---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Writing Pure ASGI Middleware: Wrapping scope, receive, and send to Inspect Headers, Mutate Status, and Enforce Limits Zero-Copy

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

Prerequisites: [[Starlette-Responses-StreamingResponse-FileResponse-and-Background-Execution]]

Next: [[Why-BaseHTTPMiddleware-Hurts-Performance-Streaming-and-ContextVars]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Implements a pure ASGI middleware that injects X-Request-ID, measures wall-clock latency in a Server-Timing header on http.response.start, and rejects oversized request bodies in flight.

## Further reading
