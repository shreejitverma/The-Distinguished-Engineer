---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Design-First vs Code-First OpenAPI Workflows: Preventing Spec-Implementation Drift with Snapshot Gates

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

Prerequisites: [[Polymorphism-and-Discriminators-in-OpenAPI-and-JSON-Schema]]

Next: [[OpenAPI-Linting-and-Automated-Style-Governance-with-Vacuum-and-Spectral]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Implements a pytest snapshot gate that exports app.openapi() from FastAPI 0.162.0, normalizes component key ordering, and fails CI if an unreviewed schema change occurs.

## Further reading
