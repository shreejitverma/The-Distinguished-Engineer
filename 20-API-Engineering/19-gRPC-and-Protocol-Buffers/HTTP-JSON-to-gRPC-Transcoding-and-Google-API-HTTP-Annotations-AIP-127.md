---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Serving Both REST/JSON and gRPC from One Protobuf Contract: google.api.http Annotations and Envoy gRPC-JSON Transcoder (AIP-127)

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

Prerequisites: [[gRPC-Load-Balancing-Subchannels-Pick-First-vs-Round-Robin-and-xDS]]

Next: [[gRPC-Health-Checking-Protocol-Server-Reflection-and-Graceful-Stop]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Maps an HTTP/JSON request (PATCH /v1/books/42?updateMask=title) into a Protobuf UpdateBookRequest message with FieldMask per AIP-127 transcoding rules.

## Further reading
