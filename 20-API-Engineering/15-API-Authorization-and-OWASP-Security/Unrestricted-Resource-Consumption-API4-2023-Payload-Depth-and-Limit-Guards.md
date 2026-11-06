---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Defending Against Unrestricted Resource Consumption (API4:2023): Max Body Bytes, JSON Nesting Depth, Array Caps, and ReDoS-Safe Regexes

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Authorization and OWASP Security](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Defeating-BFLA-API5-2023-Function-Level-Guards-and-Deny-By-Default-Routers]]

Next: [[Preventing-SSRF-API7-2023-DNS-Pinning-and-Private-CIDR-Socket-Guards]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Tests an ASGI body-size limiter that aborts streaming uploads exceeding 1 MB even when Content-Length is omitted (chunked encoding) and verifies that deeply nested JSON payloads are rejected before memory exhaustion.

## Further reading
