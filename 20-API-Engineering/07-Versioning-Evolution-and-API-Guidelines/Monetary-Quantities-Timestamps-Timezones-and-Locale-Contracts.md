---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Representing Money, Decimals, Timestamps (RFC 3339 / ISO 8601), Durations, and IANA Timezones in JSON APIs (AIP-142)

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

Prerequisites: [[Synthesizing-an-Organizational-API-Style-Guide]]

Next: [[Multi-Tenancy-and-Region-Routing-in-API-Contracts]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Demonstrates IEEE 754 float precision corruption on financial JSON payloads (0.1 + 0.2 != 0.30) and enforces integer minor-units / string-encoded Decimal + ISO 4217 currency and UTC-normalized timestamps in Pydantic v2.

## Further reading
