---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# OpenID Connect (OIDC Core 1.0) vs OAuth 2.0: Why an ID Token Must Never Be Accepted as an API Access Token, Plus RFC 8414 and RFC 9728 Metadata

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

Prerequisites: [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]

Next: [[JWT-JWS-JWK-Structure-and-RFC-9068-Access-Token-Profile]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Proves how accepting an OIDC ID Token (whose audience is the client_id) at an API resource server enables confused-deputy token forwarding attacks, and enforces RFC 9068 typ: at+jwt and resource-specific aud checks.

## Further reading
