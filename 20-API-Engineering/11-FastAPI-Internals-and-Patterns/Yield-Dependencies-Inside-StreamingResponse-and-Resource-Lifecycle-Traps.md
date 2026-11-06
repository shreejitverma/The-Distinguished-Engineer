---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why a Database Session Closed Before Streaming Starts Breaks StreamingResponse and How to Scope Resources Correctly

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Yield-Dependencies-AsyncExitStack-Cleanup-Timing-and-Scope]]

Next: [[Async-Def-vs-Def-Endpoints-and-AnyIO-Threadpool-Limiter-Semantics]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Reproduces the bug where a streaming generator accesses a database session after a function-scoped yield dependency has already exited, and fixes it with explicit dependency scope and context management.

## Further reading
