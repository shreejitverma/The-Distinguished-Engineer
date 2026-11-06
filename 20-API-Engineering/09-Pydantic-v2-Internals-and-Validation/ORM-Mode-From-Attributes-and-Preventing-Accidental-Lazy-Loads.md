---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Validating from ORM Objects (from_attributes=True) and Preventing Accidental SQLAlchemy Lazy-Load Queries During Serialization

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

Prerequisites: [[Custom-Serialization-Field-Model-and-Wrap-Serializers]]

Next: [[JSON-Schema-Generation-Internals-and-GenerateJsonSchema]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/06-SQL-and-SQLAlchemy]]

Lab plan: Reproduces how model_validate(orm_obj, from_attributes=True) triggers an implicit database query (or MissingGreenlet in async SQLAlchemy) if a relationship attribute exists on the Pydantic schema without eager loading.

## Further reading
