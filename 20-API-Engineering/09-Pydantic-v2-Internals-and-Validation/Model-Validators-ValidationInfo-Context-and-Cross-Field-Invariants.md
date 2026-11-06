---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Model-Level Validators (@model_validator mode='before'|'after'|'wrap') and Passing Runtime State via ValidationInfo.context

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

Prerequisites: [[Functional-Validators-Before-After-Wrap-and-Plain]]

Next: [[Aliases-AliasPath-AliasChoices-and-Bidirectional-Field-Mapping]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Passes tenant permissions and feature flags through model_validate(data, context={...}) into ValidationInfo.context to enforce context-dependent cross-field invariants.

## Further reading
