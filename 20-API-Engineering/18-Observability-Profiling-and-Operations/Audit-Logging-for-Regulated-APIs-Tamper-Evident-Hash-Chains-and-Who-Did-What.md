---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Immutable Audit Trails for Financial and Healthcare APIs: Actor/Subject/Resource/Diff Schema and Cryptographic SHA-256 Hash Chaining

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Observability Profiling and Operations](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Client-Side-Telemetry-Network-Error-Logging-NEL-and-Edge-vs-Origin-Gap]]

Next: [[Feature-Flags-Dynamic-Configuration-and-Safe-Kill-Switches-in-APIs]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements an append-only audit log table where each entry hashes (prev_entry_hash || timestamp || actor || action || resource_id || canonical_json_diff) and provides a verifier detecting any row tampering or deletion.

## Further reading
