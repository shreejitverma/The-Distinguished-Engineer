---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Architectural Placement of Authorization: Why Coarse Scopes Belong at the Edge and Object-Level Checks Must Bind to the Database Query

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

Prerequisites: [[Authorization-Models-Scopes-RBAC-ABAC-and-Zanzibar-ReBAC]]

Next: [[OWASP-API-Security-Top-10-2023-Complete-Threat-Model]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Proves how checking permissions in middleware before loading the resource misses tenant ownership (BOLA), while fetching the resource before checking permissions without scoping the WHERE clause leaks timing and existence.

## Further reading
