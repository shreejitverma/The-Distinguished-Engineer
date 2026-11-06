---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Measuring API Latency Correctly: Prometheus Classic vs Native Histograms (prometheus-client 0.26.0) and Why Averaging p99 Across Pods Is Mathematically Invalid

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

Prerequisites: [[Structured-Logging-with-Structlog-and-Stripe-Canonical-Log-Lines]]

Next: [[Health-Check-Engineering-Liveness-Readiness-Startup-and-Cascading-Restarts]]

Lab plan: Demonstrates with synthetic pod distributions how averaging per-pod p99 values produces a 4x error when one pod receives 90% of traffic, whereas aggregating histogram buckets yields the true fleet-wide p99.

## Further reading
