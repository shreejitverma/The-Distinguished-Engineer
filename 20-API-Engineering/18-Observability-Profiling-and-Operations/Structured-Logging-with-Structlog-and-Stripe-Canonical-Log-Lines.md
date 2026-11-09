---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Structured JSON Logging with structlog (26.1.0) and Canonical Log Lines: Emitting One Rich Summary Event Per HTTP Request

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

Prerequisites: [[Trace-Sampling-Strategies-Head-Based-vs-Tail-Based-Sampling]]

Next: [[Latency-Histograms-Percentiles-DDSketch-and-Why-Averaging-Percentiles-Is-Wrong]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Builds a canonical-log-line ASGI middleware with structlog.contextvars that accumulates auth_user_id, db_query_count, db_duration_ms, rate_limit_remaining, and trace_id throughout the request and emits one structured JSON line on completion.

## Further reading
