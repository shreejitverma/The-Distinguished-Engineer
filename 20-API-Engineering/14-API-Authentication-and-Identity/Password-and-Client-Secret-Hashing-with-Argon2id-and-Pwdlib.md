---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Hashing User Passwords and High-Entropy vs Low-Entropy Secrets: Argon2id Parameters (pwdlib 0.3.1 / argon2-cffi 25.1.0) and Timing-Safe Dummy Verifies

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

Prerequisites: [[API-Key-Engineering-Prefixed-Entropy-Hashed-Storage-and-Zero-Downtime-Rotation]]

Next: [[OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Tests Argon2id hashing, automatic parameter upgrade needs_rehash detection, and constant-time dummy hash verification on non-existent usernames to prevent user-enumeration timing side channels.

## Further reading
