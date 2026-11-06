---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# When FastAPI Becomes the Bottleneck: Per-Request DI Overhead, Startup Cold-Start Cost, and Comparison with Litestar (2.25.0) and Raw Starlette

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Mounting-Sub-Applications-WSGI-Legacy-Strangler-and-Static-Assets]]

Next: [[CPython-Asyncio-Event-Loop-Internals-Selectors-Ready-Queue-and-Timers]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Measures cold-start import/route-compilation time on a 500-route application and per-request latency across FastAPI 0.162.0, Litestar 2.25.0 (msgspec-native), and raw Starlette 1.10.0.

## Further reading
