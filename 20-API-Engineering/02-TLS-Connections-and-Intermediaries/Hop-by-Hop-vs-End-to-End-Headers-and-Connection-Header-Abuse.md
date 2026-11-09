---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Hop-by-Hop vs End-to-End Headers and the Connection Header Stripping Vulnerability (RFC 9110)

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

Prerequisites: [[HTTP-2-Connection-Coalescing-and-421-Misdirected-Request]]

Next: [[Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing]]

Lab plan: Proves how a malicious client sending Connection: keep-alive, X-Internal-Auth tricks a naive proxy into stripping a security-critical header before forwarding the request to the backend.

## Further reading
