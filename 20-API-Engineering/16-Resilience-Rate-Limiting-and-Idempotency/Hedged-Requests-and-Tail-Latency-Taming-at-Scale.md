---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Taming p99.9 Tail Latency with Hedged Requests and Cross-Zone Backup Calls Without Doubling Cluster Load

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

Prerequisites: [[Bulkhead-Isolation-and-Per-Dependency-Resource-Partitioning]]

Next: [[Webhook-Sender-Architecture-Standard-Webhooks-Signatures-and-Fan-Out]]

Lab plan: Implements a hedged async read client that fires a secondary request only if the primary exceeds the 95th-percentile latency threshold, cancelling the slower leg via AnyIO TaskGroup.

## Further reading
