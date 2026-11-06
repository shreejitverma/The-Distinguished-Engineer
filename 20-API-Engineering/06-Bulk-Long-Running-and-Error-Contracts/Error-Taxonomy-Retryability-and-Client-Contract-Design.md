---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Designing an Error Taxonomy: Separating Transient vs Permanent Failures and Preventing Information Disclosure

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Google-AIP-193-Error-Model-and-Rich-Error-Details]]

Next: [[Mapping-Pydantic-Validation-Errors-to-RFC-9457-Problem-Details]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Tests an exception-to-HTTP mapper that classifies domain exceptions into retryable vs non-retryable categories, attaches Retry-After headers when appropriate, and scrubs stack traces and SQL internals.

## Further reading
