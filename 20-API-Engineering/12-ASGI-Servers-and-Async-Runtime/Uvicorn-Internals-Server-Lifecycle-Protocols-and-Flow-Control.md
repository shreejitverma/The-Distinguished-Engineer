---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Uvicorn (0.54.0) Internals: Server.serve(), H11Protocol vs HttpToolsProtocol, FlowControl High/Low Watermarks, and --limit-concurrency

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [ASGI Servers and Async Runtime](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Uvloop-and-Httptools-Internals-Libuv-and-C-HTTP-Parsing]]

Next: [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Instantiates Uvicorn's Config and HTTP protocol classes in a pytest lab to prove how --limit-concurrency immediately returns 503 Service Unavailable when active connections or tasks exceed the threshold.

## Further reading
