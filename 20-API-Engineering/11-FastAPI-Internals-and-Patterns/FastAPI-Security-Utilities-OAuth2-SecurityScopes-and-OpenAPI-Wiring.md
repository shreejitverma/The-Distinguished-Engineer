---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# FastAPI Security Primitives: Security(), SecurityScopes, OAuth2PasswordBearer, HTTPBearer, and APIKeyHeader Internals

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [FastAPI Internals and Patterns](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Custom-Response-Classes-ORJSONResponse-and-MsgSpec-Integration]]

Next: [[Exception-Handling-in-FastAPI-HTTPException-vs-RequestValidationError]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/07-Auth-and-Security]]

Lab plan: Traces how Security(get_current_user, scopes=['orders:write']) populates SecurityScopes.scopes across chained sub-dependencies and automatically emits securitySchemes and per-operation security requirements in OpenAPI.

## Further reading
