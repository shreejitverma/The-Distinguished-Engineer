---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: Authentication Bypass via RS256-to-HS256 Key Confusion, alg: none, and Unvalidated jku / kid Header Exploits (RFC 8725)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-Connection-Pool-Exhaustion-from-Holding-DB-Sessions-Across-Slow-Outbound-HTTP-Calls]]

Next: [[Case-Study-Multi-Tenant-BOLA-and-Mass-Assignment-Privilege-Escalation]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Reproduces a vulnerable JWT verification helper that trusts header['alg'] and fetches header['jku'] dynamically, compromises it with a forged token, and replaces it with a strict RFC 8725 verifier.

## Further reading
