---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# From OAuth 2.0 (RFC 6749) to OAuth 2.1 (draft-ietf-oauth-v2-1-17) and OAuth Security BCP (RFC 9700): Mandatory PKCE and Removed Implicit/ROPC Grants

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

Prerequisites: [[Password-and-Client-Secret-Hashing-with-Argon2id-and-Pwdlib]]

Next: [[OAuth-2-0-for-Browser-Based-Applications-BFF-and-Cookies-RFC-10017]]

See also: [[API-Authentication-and-Authorization]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Implements an OAuth 2.1 authorization server state machine enforcing S256 PKCE (rejecting plain or missing code_challenge), exact redirect_uri string matching, and single-use authorization code replay detection (revoking issued tokens on code reuse per RFC 9700).

## Further reading
