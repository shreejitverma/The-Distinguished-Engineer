---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: Cascading Retry Storms and Metastable Failure After a Transient Database Stall (AWS and Google SRE Overload Analyses)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-HTTP-2-Rapid-Reset-DDoS-CVE-2023-44487]]

Next: [[Case-Study-Blocking-Call-Inside-Async-Def-Freezing-the-Entire-Worker-Fleet]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Reproduces a metastable state in an async service simulation where a 2-second downstream stall triggers client retries that keep queue latency above the client timeout even after the database recovers, and proves recovery via LIFO load shedding + retry budgets.

## Further reading
