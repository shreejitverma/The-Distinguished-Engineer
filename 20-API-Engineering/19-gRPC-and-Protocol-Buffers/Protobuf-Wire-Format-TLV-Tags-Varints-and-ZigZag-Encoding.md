---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Protocol Buffers (7.36.2) Binary Wire Format: Tag-Length-Value (TLV), Base-128 Varints, ZigZag Signed Encoding, and Packed Repeated Fields

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

Prerequisites: [[The-Sixty-Second-API-Incident-Triage-Playbook]]

Next: [[Protobuf-Editions-2023-2024-vs-Proto2-and-Proto3-Presence-Semantics]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Encodes and decodes Protobuf wire bytes manually in Python and compares them byte-for-byte against protobuf 7.36.2 SerializeToString(), proving why negative int32 takes 10 bytes while sint32 ZigZag takes 1 byte.

## Further reading
