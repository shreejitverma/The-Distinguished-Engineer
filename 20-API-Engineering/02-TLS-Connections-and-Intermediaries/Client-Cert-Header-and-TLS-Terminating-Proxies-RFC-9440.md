---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Propagating mTLS Identity Behind Terminating Proxies with the Client-Cert Header (RFC 9440)

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

Prerequisites: [[Mutual-TLS-mTLS-Handshake-and-SPIFFE-Identity]]

Next: [[TCP-Connection-Lifecycle-Keep-Alive-and-TIME-WAIT]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Serializes and parses RFC 9651 Byte Sequence Client-Cert and Client-Cert-Chain headers, and proves how an origin strips untrusted external Client-Cert headers when the peer IP is outside the proxy CIDR.

## Further reading
