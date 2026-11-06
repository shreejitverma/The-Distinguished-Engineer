---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The ASGI 3.0 Callable Contract: scope Dictionary, receive() Awaitable, and send() Event State Machine

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

Prerequisites: [[WSGI-PEP-3333-vs-ASGI-3-0-Architectural-Shift]]

Next: [[ASGI-HTTP-and-Disconnect-Event-Lifecycle]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Writes a raw ASGI 3.0 application without any framework that inspects scope, reads chunked http.request events with more_body=True, and emits http.response.start and http.response.body events.

## Further reading
