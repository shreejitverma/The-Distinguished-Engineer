---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Defending Auth and Token Endpoints Against Brute Force and Credential Stuffing Without Enabling Account-Lockout Denial of Service

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

Prerequisites: [[Security-Headers-for-HTTP-APIs-HSTS-Nosniff-Cache-Control-and-Security-Txt]]

Next: [[Structured-Log-Redaction-and-Preventing-PII-and-Token-Leaks]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements a dual-key rate limiter (per-IP and per-(username, IP) with exponential backoff delays) that throttles credential stuffing while allowing the legitimate user to log in from their known IP.

## Further reading
