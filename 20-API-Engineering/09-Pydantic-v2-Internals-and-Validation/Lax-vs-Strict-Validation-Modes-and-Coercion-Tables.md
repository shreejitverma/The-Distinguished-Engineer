---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Validation Modes in Pydantic v2: Lax Coercion vs Strict Mode (strict=True, StrictInt, StrictStr) and ConfigDict

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

Prerequisites: [[Pydantic-v2-Architecture-CoreSchema-and-Rust-Pydantic-Core]]

Next: [[Single-Pass-JSON-Validation-Model-Validate-JSON-vs-JSON-Loads]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Proves the exact coercion matrix difference between model_validate(dict) (Python input) and model_validate_json(bytes) (JSON input) under both lax and strict=True configurations.

## Further reading
