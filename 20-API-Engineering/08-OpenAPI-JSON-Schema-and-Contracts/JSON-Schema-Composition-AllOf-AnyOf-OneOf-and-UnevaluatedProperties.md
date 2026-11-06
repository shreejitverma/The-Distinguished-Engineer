---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# JSON Schema Composition Traps: Why additionalProperties: false Breaks allOf and How unevaluatedProperties Fixes It

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [OpenAPI JSON Schema and Contracts](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[JSON-Schema-2020-12-Core-and-Validation-Vocabularies]]

Next: [[OpenAPI-3-1-Architecture-and-JSON-Schema-Alignment]]

Lab plan: Proves with jsonschema 4.26.0 why combining two subschemas via allOf with additionalProperties: false rejects all valid instances, whereas unevaluatedProperties: false succeeds.

## Further reading
