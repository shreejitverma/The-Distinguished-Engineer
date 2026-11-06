---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# CPython asyncio Event Loop Internals: BaseEventLoop._run_once, epoll/kqueue Selector Polling, _ready Deque, and _scheduled Min-Heap

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

Prerequisites: [[FastAPI-Architectural-Limits-and-Comparison-with-Litestar-and-Raw-Starlette]]

Next: [[Eager-Task-Factory-in-Python-3-12-to-3-15-and-Execution-Order-Shifts]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Instruments BaseEventLoop._run_once to trace exact tick behavior across I/O readiness callbacks, call_soon(_ready) FIFO execution, and call_later(_scheduled) timer heap popping.

## Further reading
