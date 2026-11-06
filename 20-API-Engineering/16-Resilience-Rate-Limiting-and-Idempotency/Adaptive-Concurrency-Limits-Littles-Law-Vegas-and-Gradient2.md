---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Self-Tuning Capacity Protection: Adaptive Concurrency Limits Using Little's Law, TCP Vegas, and Gradient2 Queue-Delay Detection

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

Prerequisites: [[Circuit-Breakers-State-Machines-Half-Open-Probing-and-When-Not-to-Use-Them]]

Next: [[Load-Shedding-Priority-Traffic-Classes-and-LIFO-Queue-Management]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Implements a Gradient2 adaptive concurrency limiter in ASGI middleware that dynamically expands its in-flight limit when rtt_noload / rtt_actual ~= 1.0 and contracts it when queueing latency rises.

## Further reading
