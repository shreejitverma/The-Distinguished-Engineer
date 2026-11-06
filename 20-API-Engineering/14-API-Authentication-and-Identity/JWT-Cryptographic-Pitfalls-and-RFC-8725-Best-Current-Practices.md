---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# JWT Vulnerability Anatomy and RFC 8725 Defenses: alg: none, RS256-to-HS256 Key Confusion, Unvalidated kid Path Traversal, and jku/x5u SSRF

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

Prerequisites: [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]]

Next: [[JWKS-Discovery-Caching-Key-Rotation-and-Thundering-Herd-Protection]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Constructs exploit payloads for alg: none, RS256-public-key-as-HS256-HMAC-secret, and unverified claim decoding, and proves how joserfc 1.8.0 and PyJWT 2.15.1 with explicit algorithm allowlists and key-type checks block every vector.

## Further reading
