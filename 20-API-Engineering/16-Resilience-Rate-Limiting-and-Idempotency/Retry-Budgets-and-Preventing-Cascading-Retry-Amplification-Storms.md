---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why Multi-Hop Retries Multiply Traffic by K^N and How Per-Client Retry Token Ratios (Google SRE Retry Budgets) Stop Retry Storms

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

Prerequisites: [[Retries-Exponential-Backoff-Full-vs-Decorrelated-Jitter-and-Stamina]]

Next: [[Circuit-Breakers-State-Machines-Half-Open-Probing-and-When-Not-to-Use-Them]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Models a 3-tier service call chain with 3 retries per hop (amplifying a downstream overload by 27x) and proves that a Google SRE retry budget (max 10% retry ratio + retry only at the outermost caller) caps amplification to 1.1x.

## Further reading
