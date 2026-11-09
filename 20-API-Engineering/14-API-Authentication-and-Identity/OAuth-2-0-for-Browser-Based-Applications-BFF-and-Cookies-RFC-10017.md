---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# OAuth 2.0 for Browser-Based Applications (RFC 10017, August 2026): Backend-for-Frontend (BFF) Pattern vs In-Browser Token Storage

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Authentication and Identity](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[OAuth-2-0-to-OAuth-2-1-and-RFC-9700-Security-BCP]]

Next: [[OpenID-Connect-OIDC-ID-Tokens-vs-Access-Tokens-and-Metadata-Discovery]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements a BFF token-mediating proxy in FastAPI that keeps OAuth access and refresh tokens exclusively in encrypted server-side session storage while issuing __Host- HttpOnly Secure SameSite=Lax cookies to the browser.

## Further reading
