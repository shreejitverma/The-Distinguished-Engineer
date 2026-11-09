---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# TLS 1.3 Session Tickets, 0-RTT Early Data Replay Hazards, and HTTP 425 Too Early (RFC 8470)

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

Prerequisites: [[TLS-1-3-Handshake-Cipher-Suites-and-ALPN-RFC-9846]]

Next: [[X509-Certificates-Chain-Validation-SAN-and-OCSP-Stapling]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Simulates a 0-RTT Early-Data request containing a non-idempotent POST and proves that an RFC 8470 middleware rejects unsafe early data with 425 Too Early while allowing safe GET requests.

## Further reading
