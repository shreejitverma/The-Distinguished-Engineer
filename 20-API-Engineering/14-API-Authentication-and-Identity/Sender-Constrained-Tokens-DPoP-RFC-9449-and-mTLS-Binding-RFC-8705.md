---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Stopping Stolen Bearer Token Replay: Demonstrating Proof of Possession (DPoP, RFC 9449) and mTLS Certificate-Bound Tokens (RFC 8705)

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

Prerequisites: [[Refresh-Token-Rotation-Reuse-Detection-and-Token-Revocation-Architectures]]

Next: [[Service-to-Service-Identity-Client-Credentials-and-Token-Exchange-RFC-8693]]

See also: [[API-Authentication-and-Authorization]]

Lab plan: Implements an RFC 9449 DPoP proof verifier in FastAPI that validates the DPoP header JWS signature, htm (method), htu (URI), ath (access token hash), jti replay cache, and cnf.jkt thumbprint binding.

## Further reading
