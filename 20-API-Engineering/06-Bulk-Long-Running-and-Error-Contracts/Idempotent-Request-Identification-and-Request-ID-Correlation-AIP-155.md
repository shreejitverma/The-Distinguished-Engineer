---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Request Identification: Client request_id (AIP-155) vs Server X-Request-ID and Trace Correlation

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Resumable-HTTP-Uploads-IETF-Draft-and-Tus-Protocol]]

Next: [[Compatibility-Taxonomy-Wire-Source-and-Semantic-Compatibility-AIP-180]]

See also: [[API-Fundamentals]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Validates UUIDv4/v7 request_id parameters per AIP-155 (rejecting zero UUID 00000000-0000-0000-0000-000000000000) and binds server correlation IDs to response headers and RFC 9457 error instances.

## Further reading
