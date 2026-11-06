---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Engineering Production API Keys: Secret-Scanning Prefixes (acme_live_...), SHA-256/HMAC Lookup Hashes, Constant-Time Comparison, and Dual-Key Rotation

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[API-Authentication-Architecture-Edge-Verification-vs-Zero-Trust-Propagation]]

Next: [[Password-and-Client-Secret-Hashing-with-Argon2id-and-Pwdlib]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements a complete API key lifecycle: CSPRNG generation with checksum prefix, database storage of key_prefix + SHA-256 digest (never plaintext), constant-time verification, and overlapping grace-period key rotation.

## Further reading
