---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Cancellation Mechanics in Python APIs: Task.cancel(), Task.cancelling(), Why BaseException Includes CancelledError, and Shielded Finalizers

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [ASGI Servers and Async Runtime](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[AnyIO-Structured-Concurrency-TaskGroups-CancelScopes-and-ExceptionGroups]]

Next: [[End-to-End-Backpressure-TCP-Windows-ASGI-Send-Await-and-Bounded-Queues]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Reproduces the deadly bug where a broad except Exception or bare except swallows CancelledError without re-raising, breaking asyncio.timeout(), and demonstrates AnyIO CancelScope(shield=True) for asynchronous DB rollback during cancellation.

## Further reading
