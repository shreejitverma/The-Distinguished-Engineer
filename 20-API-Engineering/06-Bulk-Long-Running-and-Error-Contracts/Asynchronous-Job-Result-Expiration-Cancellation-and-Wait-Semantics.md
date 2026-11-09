---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# LRO Engineering Details: Waiter Long-Polling (:wait), Operation TTL Garbage Collection, and Idempotent Replay

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Long-Running-Operations-202-Accepted-and-Operation-Resources-AIP-151]]

Next: [[Dry-Run-and-Validate-Only-Requests-AIP-163]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Tests an LRO :wait endpoint that blocks up to a bounded timeout on an asyncio.Event before returning the latest operation state or the completed result.

## Further reading
