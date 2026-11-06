---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Cryptographic Request Non-Repudiation: Signing HTTP Requests with HTTP Message Signatures (RFC 9421) and Digest Fields (RFC 9530)

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

Prerequisites: [[Pushed-Authorization-Requests-PAR-RFC-9126-and-Rich-Authorization-Requests-RAR-RFC-9396]]

Next: [[Selective-Disclosure-JWTs-SD-JWT-RFC-9901]]

Lab plan: Constructs and verifies an RFC 9421 Signature-Input and Signature header covering (@method @target-uri @authority content-digest) with an Ed25519 key and RFC 9530 Content-Digest: sha-256=:...:.

## Further reading
