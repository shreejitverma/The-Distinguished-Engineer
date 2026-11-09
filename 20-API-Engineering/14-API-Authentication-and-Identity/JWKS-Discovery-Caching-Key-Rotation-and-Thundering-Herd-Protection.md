---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# JWKS Endpoint Engineering: Public Key Caching, Graceful Signing Key Rotation (kid), and Preventing IDP DoS on Unknown kid Attacks

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

Prerequisites: [[JWT-Cryptographic-Pitfalls-and-RFC-8725-Best-Current-Practices]]

Next: [[Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Builds an async JWKS verifier with background refresh, overlap rotation of old and new kid keys, and a rate-limited negative cache so forged tokens with random kid values cannot trigger an outbound HTTP storm to the IDP.

## Further reading
