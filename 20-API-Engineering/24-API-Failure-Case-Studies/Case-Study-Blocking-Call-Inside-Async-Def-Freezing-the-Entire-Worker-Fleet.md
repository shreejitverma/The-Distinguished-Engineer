---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: How One Synchronous HTTP or File Call Inside an async def Endpoint Froze Event Loops, Tripped Liveness Probes, and Caused a Fleet-Wide Outage

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-Cascading-Retry-Storms-and-Metastable-Overload-Collapse]]

Next: [[Case-Study-Connection-Pool-Exhaustion-from-Holding-DB-Sessions-Across-Slow-Outbound-HTTP-Calls]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Reproduces how a 200ms synchronous blocking call inside an async def route delays the /healthz response past the probe timeout under 20 RPS, and proves the fix using async I/O + loop-lag watchdogs + shallow liveness probes.

## Further reading
