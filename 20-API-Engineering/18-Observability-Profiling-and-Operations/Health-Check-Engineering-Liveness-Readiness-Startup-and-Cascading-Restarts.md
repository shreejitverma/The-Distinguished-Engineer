---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Health Check Architecture: Liveness (/healthz) vs Readiness (/readyz) vs Startup Probes and Why Checking Downstream DBs in Liveness Causes Cascading Outages

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Observability Profiling and Operations](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Latency-Histograms-Percentiles-DDSketch-and-Why-Averaging-Percentiles-Is-Wrong]]

Next: [[Load-Testing-APIs-with-Locust-and-Open-vs-Closed-Workload-Models]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Simulates a transient 5-second database blip and proves how wiring a DB ping into a Kubernetes liveness probe triggers simultaneous pod restarts across the entire fleet, whereas readiness shedding + shallow liveness recovers cleanly.

## Further reading
