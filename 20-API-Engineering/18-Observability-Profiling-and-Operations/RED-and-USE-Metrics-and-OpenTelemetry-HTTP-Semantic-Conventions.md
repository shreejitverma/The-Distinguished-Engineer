---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# RED (Rate, Errors, Duration) and USE Metrics for APIs, and OpenTelemetry HTTP Semantic Conventions (http.request.method, http.route, http.response.status_code)

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

Prerequisites: [[Zero-Downtime-Blue-Green-Canary-and-Cell-Based-API-Deployments]]

Next: [[Distributed-Tracing-W3C-Trace-Context-and-Baggage-Propagation]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Instruments a FastAPI app with OpenTelemetry 1.45.2 InMemoryMetricReader and verifies that metrics label http.route with the route template (/orders/{order_id}) rather than the raw URL path (/orders/98412) to prevent cardinality explosion.

## Further reading
