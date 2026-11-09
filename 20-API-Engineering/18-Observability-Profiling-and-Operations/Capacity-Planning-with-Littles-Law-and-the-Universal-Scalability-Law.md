---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# API Capacity Planning: Little's Law (L = lambda * W), Amdahl's Law, and Gunther's Universal Scalability Law (Contention and Coherency Retrograde)

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

Prerequisites: [[Load-Testing-APIs-with-Locust-and-Open-vs-Closed-Workload-Models]]

Next: [[Profiling-Python-API-Services-CPU-Wall-Clock-Async-Tasks-and-Memory]]

Lab plan: Fits throughput measurements across concurrency levels to the Universal Scalability Law C(N) = N / (1 + alpha*(N-1) + beta*N*(N-1)) to pinpoint the exact concurrency ceiling where lock/pool contention causes retrograde throughput collapse.

## Further reading
