---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# HTTP/2 Stream and Connection Flow Control: WINDOW_UPDATE and Backpressure (RFC 9113)

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

Prerequisites: [[HTTP-2-Binary-Framing-and-Stream-State-Machine]]

Next: [[HPACK-Header-Compression-and-Dynamic-Table-Security]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]], [[gRPC-and-Protocol-Buffers]]

Lab plan: Simulates a slow receiver exhausting the 65,535-byte default HTTP/2 flow-control window in h2 4.4.1 and proves that one stalled stream does not block sibling streams until the connection window saturates.

## Further reading
