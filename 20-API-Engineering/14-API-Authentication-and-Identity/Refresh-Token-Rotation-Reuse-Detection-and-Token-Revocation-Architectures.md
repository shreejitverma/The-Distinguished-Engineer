---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Access and Refresh Token Lifecycles: Refresh Token Rotation with Reuse Family Revocation (RFC 9700), Token Introspection (RFC 7662), and Phantom Tokens

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

Prerequisites: [[JWKS-Discovery-Caching-Key-Rotation-and-Thundering-Herd-Protection]]

Next: [[Sender-Constrained-Tokens-DPoP-RFC-9449-and-mTLS-Binding-RFC-8705]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements refresh token family tracking where replaying an already-rotated refresh token immediately invalidates the entire token family, alerting on token theft.

## Further reading
