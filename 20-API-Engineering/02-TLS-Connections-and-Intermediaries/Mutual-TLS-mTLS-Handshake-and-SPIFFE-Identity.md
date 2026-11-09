---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Mutual TLS (mTLS) Client Authentication, Certificate Pinning, and SPIFFE SVID URI Identities

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

Prerequisites: [[X509-Certificates-Chain-Validation-SAN-and-OCSP-Stapling]]

Next: [[Client-Cert-Header-and-TLS-Terminating-Proxies-RFC-9440]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Configures an ssl.SSLContext with CERT_REQUIRED over loopback, extracts the client certificate subject and SAN URI (spiffe://domain/ns/default/sa/orders), and rejects unauthenticated TLS peers.

## Further reading
