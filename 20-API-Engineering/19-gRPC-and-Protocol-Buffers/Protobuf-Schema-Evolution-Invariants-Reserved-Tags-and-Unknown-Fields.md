---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Wire-Safe Protobuf Schema Evolution: Why Field Numbers Can Never Be Reused, reserved Tags and Names, Wire-Compatible Type Promotions, and Unknown Field Preservation

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

Prerequisites: [[Protobuf-Editions-2023-2024-vs-Proto2-and-Proto3-Presence-Semantics]]

Next: [[Protobuf-Governance-Linting-and-Breaking-Change-Gates-with-Buf]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Serializes a v2 message containing new fields, parses and re-serializes it through an older v1 service, and proves that unknown fields are preserved intact whereas reusing a deleted field number with a different type corrupts decoding.

## Further reading
