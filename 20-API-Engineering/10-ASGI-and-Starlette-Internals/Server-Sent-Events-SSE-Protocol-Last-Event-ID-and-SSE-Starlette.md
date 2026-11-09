---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Server-Sent Events (WHATWG SSE) Internals: text/event-stream Framing, retry:, Last-Event-ID Resumption, Ping Keep-Alives, and sse-starlette (3.6.0)

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

Prerequisites: [[ASGI-WebSockets-Lifecycle-Subprotocols-and-Backpressure]]

Next: [[Starlette-AuthenticationMiddleware-SessionMiddleware-and-TrustedHost]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Streams numbered SSE events via EventSourceResponse (sse-starlette 3.6.0), simulates a dropped connection, and verifies gapless replay when the client reconnects with the Last-Event-ID header.

## Further reading
