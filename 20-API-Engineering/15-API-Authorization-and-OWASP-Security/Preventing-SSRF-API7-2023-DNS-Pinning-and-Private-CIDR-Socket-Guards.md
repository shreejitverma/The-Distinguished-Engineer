---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Defending Against Server-Side Request Forgery (SSRF, API7:2023): Why URL String Validation Fails Against DNS Rebinding and How to Build a Safe httpx Transport

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

Prerequisites: [[Unrestricted-Resource-Consumption-API4-2023-Payload-Depth-and-Limit-Guards]]

Next: [[Protecting-Sensitive-Business-Flows-API6-2023-Anti-Automation-and-Bot-Defense]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Proves how a naive pre-flight socket.getaddrinfo check is bypassed by DNS rebinding or HTTP 302 redirects to 169.254.169.254 / 127.0.0.1, and builds a custom httpx transport that validates resolved IP addresses at connect time with redirects disabled.

## Further reading
