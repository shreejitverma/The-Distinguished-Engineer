---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# X.509 Certificate Chains, SAN Matching, Expiration, Revocation, and OCSP Stapling for APIs

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [TLS Connections and Intermediaries](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[TLS-Session-Resumption-0-RTT-Early-Data-and-425-Too-Early]]

Next: [[Mutual-TLS-mTLS-Handshake-and-SPIFFE-Identity]]

See also: [[API-Authentication-and-Authorization]]

Lab plan: Generates a root CA, intermediate CA, and leaf certificate with cryptography 50.0.2 and tests hostname/IP SAN verification, expired leaf rejection, and untrusted intermediate failures.

## Further reading
