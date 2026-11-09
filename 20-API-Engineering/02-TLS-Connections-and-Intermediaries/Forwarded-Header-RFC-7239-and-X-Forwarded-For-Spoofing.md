---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Proxy Identity Propagation: RFC 7239 Forwarded vs X-Forwarded-For Right-to-Left Trusted Hop Parsing

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

Prerequisites: [[Hop-by-Hop-vs-End-to-End-Headers-and-Connection-Header-Abuse]]

Next: [[100-Continue-Expect-Header-and-Large-Upload-Admission]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Proves that reading X-Forwarded-For[0] allows trivial IP rate-limit bypass via client header spoofing, whereas right-to-left trusted-proxy CIDR peeling extracts the true client IP.

## Further reading
