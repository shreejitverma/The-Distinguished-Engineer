---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# W3C Trace Context (traceparent, tracestate) and Baggage Propagation Across FastAPI, httpx (0.28.1), and SQLAlchemy (2.1.8)

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

Prerequisites: [[RED-and-USE-Metrics-and-OpenTelemetry-HTTP-Semantic-Conventions]]

Next: [[Trace-Sampling-Strategies-Head-Based-vs-Tail-Based-Sampling]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Traces a request entering FastAPI with a W3C traceparent header, executing an async SQLAlchemy query, and making an outbound httpx call, asserting a single parent-child span tree in InMemorySpanExporter.

## Further reading
