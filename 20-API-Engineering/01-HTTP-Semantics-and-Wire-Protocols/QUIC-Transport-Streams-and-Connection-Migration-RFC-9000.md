---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# QUIC Transport Internals: UDP Packets, Connection IDs, Independent Streams, and Migration (RFC 9000)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [HTTP Semantics and Wire Protocols](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[TCP-Head-of-Line-Blocking-Under-Packet-Loss]]

Next: [[HTTP-3-Framing-and-QPACK-Header-Compression-RFC-9114]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Uses aioquic 1.7.2 in memory to establish a QUIC connection, multiplex bidirectional streams, and migrate the client UDP source address while preserving connection state via Connection ID.

## Further reading
