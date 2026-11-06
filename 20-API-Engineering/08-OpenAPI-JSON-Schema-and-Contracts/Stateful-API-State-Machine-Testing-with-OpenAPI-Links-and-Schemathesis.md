---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Stateful Multi-Step API Fuzzing: Chaining POST -> GET -> PATCH -> DELETE via OpenAPI Links in Schemathesis

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

Prerequisites: [[Property-Based-API-Testing-with-Schemathesis-and-Hypothesis]]

Next: [[Response-Schema-Conformance-Enforcement-in-Test-Suites]]

Lab plan: Defines OpenAPI 3.1.2 Link objects connecting POST /items (201) to GET/PATCH/DELETE /items/{item_id} and runs a Hypothesis RuleBasedStateMachine via Schemathesis to verify lifecycle invariants.

## Further reading
