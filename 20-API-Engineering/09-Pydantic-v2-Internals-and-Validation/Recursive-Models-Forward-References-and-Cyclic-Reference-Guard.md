---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Recursive Schemas, Forward References, model_rebuild(), and Cyclic Reference Detection in pydantic-core

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

Prerequisites: [[Generic-Models-PEP-695-Syntax-and-Variance]]

Next: [[Custom-Serialization-Field-Model-and-Wrap-Serializers]]

Lab plan: Builds a self-referential AST/Comment tree model, resolves deferred annotations with model_rebuild(), and proves that pydantic-core detects and rejects cyclic Python object graphs instead of hitting RecursionError.

## Further reading
