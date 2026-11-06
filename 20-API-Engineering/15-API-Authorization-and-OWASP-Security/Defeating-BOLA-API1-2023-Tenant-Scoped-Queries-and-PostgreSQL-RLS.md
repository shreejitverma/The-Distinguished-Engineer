---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Preventing Broken Object Level Authorization (BOLA / IDOR, API1:2023): Tenant-Scoped Repository Types and PostgreSQL Row-Level Security (RLS)

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

Prerequisites: [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]]

Next: [[Defeating-BOPLA-and-Mass-Assignment-API3-2023-Strict-Input-Output-Schemas]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements a TenantSession wrapper in SQLAlchemy that automatically injects WHERE tenant_id = :current_tenant via do_orm_execute events and verifies defense-in-depth with PostgreSQL SET LOCAL app.tenant_id RLS policies.

## Further reading
