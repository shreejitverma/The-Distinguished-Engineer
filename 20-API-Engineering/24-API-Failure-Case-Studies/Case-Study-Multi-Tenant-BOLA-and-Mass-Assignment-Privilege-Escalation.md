---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: Cross-Tenant Data Exfiltration via BOLA (API1:2023) and Admin Privilege Escalation via Mass Assignment (API3:2023)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-JWT-Key-Confusion-and-Unverified-Header-KID-SSRF-Breaches]]

Next: [[Case-Study-HTTP-Request-Smuggling-and-H2C-H2-Downgrade-Desync]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Reproduces a multi-tenant breach where GET /v1/invoices/{id} looks up by primary key without tenant_id scoping and PATCH /v1/users/me accepts ORM attributes directly, then proves complete remediation.

## Further reading
