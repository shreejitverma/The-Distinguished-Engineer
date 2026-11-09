---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# TypeAdapter: Validating and Serializing Lists, TypedDicts, Unions, and Primitives Without a BaseModel Wrapper

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Pydantic v2 Internals and Validation](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Single-Pass-JSON-Validation-Model-Validate-JSON-vs-JSON-Loads]]

Next: [[Annotated-Constraints-Field-Metadata-and-Custom-Types]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Benchmarks a module-level singleton TypeAdapter(list[OrderItem]) against repeated instantiation inside a request handler, proving why TypeAdapter construction must be cached at import time.

## Further reading
