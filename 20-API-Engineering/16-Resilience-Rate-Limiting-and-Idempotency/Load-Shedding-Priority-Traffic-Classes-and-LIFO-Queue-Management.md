---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Load Shedding Under Overload: Prioritized Admission Control (Critical vs Background Traffic), CoDel Queue Timeout, and LIFO vs FIFO Under Saturation

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Resilience Rate Limiting and Idempotency](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Adaptive-Concurrency-Limits-Littles-Law-Vegas-and-Gradient2]]

Next: [[Bulkhead-Isolation-and-Per-Dependency-Resource-Partitioning]]

Lab plan: Proves in a simulated overload test that shedding low-priority requests early and switching queued work from FIFO to LIFO with a short sojourn timeout keeps p95 latency below the client timeout.

## Further reading
