---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# HTTP Trailer Fields: Streaming Digest Integrity (RFC 9530) and Trailers-Only Responses

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [TLS Connections and Intermediaries](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[103-Early-Hints-and-Informational-Responses]]

Next: [[HTTP-Caching-Architecture-Private-Shared-and-Gateway-Caches]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Streams a chunked HTTP response computing a running SHA-256 hash and emits a Repr-Digest trailer field per RFC 9530, verifying end-of-stream integrity at the receiver.

## Further reading
