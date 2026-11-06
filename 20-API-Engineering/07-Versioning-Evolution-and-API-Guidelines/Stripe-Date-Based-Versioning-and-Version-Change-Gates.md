---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Stripe's Date-Based API Versioning Engine: Account Pinning, Stripe-Version Overrides, and Bidirectional Transformers

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Versioning Evolution and API Guidelines](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[API-Versioning-Strategies-URI-Header-Media-Type-and-Channel-AIP-185]]

Next: [[Deprecation-Header-RFC-9745-and-Sunset-Header-RFC-8594]]

See also: [[API-Fundamentals]]

Lab plan: Builds a complete Stripe-style version transformation pipeline where core domain handlers speak only the latest schema and declarative VersionChange modules downgrade responses and upgrade requests across three dated versions.

## Further reading
