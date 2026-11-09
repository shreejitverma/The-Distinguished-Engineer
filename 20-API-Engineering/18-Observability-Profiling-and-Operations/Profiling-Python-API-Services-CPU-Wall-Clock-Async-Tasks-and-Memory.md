---
type: playbook
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Profiling Python APIs in Production: sys.monitoring, cProfile vs Wall-Clock Async Profiling, SQLAlchemy Query Attribution, and Memory Flamegraphs

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Observability Profiling and Operations](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Capacity-Planning-with-Littles-Law-and-the-Universal-Scalability-Law]]

Next: [[SLIs-SLOs-Error-Budgets-and-Multi-Window-Burn-Rate-Alerting]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Uses Python 3.12+ sys.monitoring and SQLAlchemy Engine event hooks to build a lightweight per-request profiler that breaks down wall-clock time into CPU time, async I/O wait, and per-query SQL time.

## Further reading
