---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Generator (yield) Dependencies in Depth: AsyncExitStack Teardown, Exception Handling, and scope='function' vs scope='request'

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

Prerequisites: [[Dependency-Injection-Engine-DAG-Resolution-and-Use-Cache-Mechanics]]

Next: [[Yield-Dependencies-Inside-StreamingResponse-and-Resource-Lifecycle-Traps]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Proves the exact execution order of code after yield relative to response streaming and BackgroundTasks under both scope='function' (before response completes) and scope='request' (after response finishes).

## Further reading
