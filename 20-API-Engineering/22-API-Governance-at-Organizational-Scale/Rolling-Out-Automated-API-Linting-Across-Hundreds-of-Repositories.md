---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Rolling Out Automated OpenAPI and Protobuf Linting Across an Existing Estate: Baseline Suppression Ratchets, Severity Tiers, and Zero-Regression CI Gates

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Governance at Organizational Scale](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Running-an-API-Design-Review-Board-Lightweight-AIPs-and-Review-Checklists]]

Next: [[Golden-Path-API-Service-Templates-and-Internal-Platform-Chassis]]

Lab plan: Implements a lint-ratchet runner that compares current OpenAPI lint violations against a committed baseline.json, allowing legacy violations on untouched endpoints while failing CI on any new violation.

## Further reading
