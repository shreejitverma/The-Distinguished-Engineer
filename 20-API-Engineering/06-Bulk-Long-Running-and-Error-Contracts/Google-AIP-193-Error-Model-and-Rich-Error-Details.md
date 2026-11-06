---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Google AIP-193 Error Model: google.rpc.Status, ErrorInfo, RetryInfo, QuotaFailure, and BadRequest

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

Prerequisites: [[Problem-Details-for-HTTP-APIs-RFC-9457]]

Next: [[Error-Taxonomy-Retryability-and-Client-Contract-Design]]

See also: [[gRPC-and-Protocol-Buffers]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Constructs and serializes AIP-193 error payloads with domain-scoped ErrorInfo reason enums, localized messages, and structured field violations that map 1:1 between HTTP/JSON and gRPC status trailers.

## Further reading
