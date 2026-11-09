---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why model_validate_json Is Faster and Safer Than model_validate(json.loads()): Zero Intermediate Python Dicts in Rust

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

Prerequisites: [[Lax-vs-Strict-Validation-Modes-and-Coercion-Tables]]

Next: [[TypeAdapter-for-Validating-and-Serializing-Arbitrary-Types]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Measures memory allocations (tracemalloc) and throughput between model_validate(json.loads(raw)) and model_validate_json(raw), proving direct Rust jiter tape parsing directly into model fields.

## Further reading
