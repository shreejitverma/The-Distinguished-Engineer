---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# When Pagination Is the Wrong Tool: Streaming Large Collections with application/x-ndjson and Server-Side Cursors

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Pagination Filtering and Query Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Search-Endpoints-vs-List-Endpoints-and-Full-Text-Ranking]]

Next: [[Problem-Details-for-HTTP-APIs-RFC-9457]]

See also: [[jq-Streaming-Large-Inputs]], [[CSV-Quoting-and-Edge-Cases]], [[Miller]], [[xsv-and-qsv]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Streams 50,000 rows from an async SQLAlchemy stream_scalars() cursor as newline-delimited JSON (NDJSON) through Starlette StreamingResponse with O(1) memory footprint.

## Further reading
