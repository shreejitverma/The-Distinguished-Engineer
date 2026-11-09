---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# How gRPC Maps to HTTP/2: Pseudo-Headers, 5-Byte Length-Prefixed Message Envelopes, and Trailers-Only grpc-status Responses

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [gRPC and Protocol Buffers](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Protobuf-Governance-Linting-and-Breaking-Change-Gates-with-Buf]]

Next: [[The-Four-gRPC-Communication-Modes-Unary-Server-Client-and-Bidi-Streaming]]

See also: [[gRPC-and-Protocol-Buffers]], [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Dissects the 5-byte gRPC DATA frame header (1-byte compressed flag + 4-byte big-endian uint32 message length) over h2 4.4.1 and parses grpc-status and grpc-message HTTP/2 trailers.

## Further reading
