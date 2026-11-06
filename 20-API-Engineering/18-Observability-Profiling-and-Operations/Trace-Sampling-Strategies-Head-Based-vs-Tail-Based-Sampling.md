---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Trace Sampling at High QPS: ParentBased Head Sampling vs Collector Tail-Based Sampling (Always Keeping 5xx Errors and High-Latency Outliers)

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

Prerequisites: [[Distributed-Tracing-W3C-Trace-Context-and-Baggage-Propagation]]

Next: [[Structured-Logging-with-Structlog-and-Stripe-Canonical-Log-Lines]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Implements a custom OpenTelemetry sampler and tail-buffer processor that samples 1% of healthy 200 OK traces while retaining 100% of traces containing a 5xx status or exceeding 500ms latency.

## Further reading
