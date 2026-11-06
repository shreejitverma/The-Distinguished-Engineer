---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Starlette Request Internals: request.stream() vs request.body(), _body Caching, and Why Reading the Stream in Middleware Starves Downstream Handlers

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

Prerequisites: [[Starlette-Routing-Internals-Regex-Compilation-and-Mount-Dispatch]]

Next: [[Starlette-Responses-StreamingResponse-FileResponse-and-Background-Execution]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Reproduces RuntimeError: Stream consumed when a naive logging middleware iterates request.stream() before the route handler reads the body, and fixes it using pure ASGI receive wrapper replay.

## Further reading
