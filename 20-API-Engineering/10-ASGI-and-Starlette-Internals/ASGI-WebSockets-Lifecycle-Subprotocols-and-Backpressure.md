---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# WebSockets in ASGI and Starlette: Handshake, Subprotocol Negotiation, Close Codes (RFC 6455), and Concurrent Send/Receive Loops

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

Prerequisites: [[Starlette-Exception-Hierarchy-ServerErrorMiddleware-vs-ExceptionMiddleware]]

Next: [[Server-Sent-Events-SSE-Protocol-Last-Event-ID-and-SSE-Starlette]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Implements a full-duplex WebSocket endpoint in Starlette 1.10.0 using an anyio TaskGroup to multiplex inbound client commands and outbound broadcast messages with bounded queue backpressure.

## Further reading
