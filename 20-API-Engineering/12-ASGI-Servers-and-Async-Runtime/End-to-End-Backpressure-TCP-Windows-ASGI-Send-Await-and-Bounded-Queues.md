---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Backpressure Across the Stack: How a Slow Client Stalls TCP Send Buffers, Pauses Uvicorn Transport Writing, and Suspends ASGI send()

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

Prerequisites: [[Cancellation-Semantics-CancelledError-Swallowing-and-Shielded-Cleanup]]

Next: [[Detecting-Event-Loop-Blocking-Loop-Lag-Monitors-and-Debug-Callbacks]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/17-Concurrency-in-Web-Services-and-Coding]]

Lab plan: Simulates a slow consumer reading 1 KB/s from a fast producer and proves that awaiting send() in an ASGI StreamingResponse naturally throttles the producer when transport write buffers hit the high watermark, whereas unbounded asyncio.Queue causes OOM.

## Further reading
