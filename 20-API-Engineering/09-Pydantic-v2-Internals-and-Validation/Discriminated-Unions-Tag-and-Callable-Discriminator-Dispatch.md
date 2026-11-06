---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# High-Performance Polymorphism: Discriminated Unions with String Discriminators, Tag(), and Callable Discriminator()

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

Prerequisites: [[Aliases-AliasPath-AliasChoices-and-Bidirectional-Field-Mapping]]

Next: [[Generic-Models-PEP-695-Syntax-and-Variance]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Benchmarks O(1) tagged union dispatch against O(K) untagged Union[A, B, C, ...] left-to-right trial validation and tests a callable Discriminator routing payloads by structural shape.

## Further reading
