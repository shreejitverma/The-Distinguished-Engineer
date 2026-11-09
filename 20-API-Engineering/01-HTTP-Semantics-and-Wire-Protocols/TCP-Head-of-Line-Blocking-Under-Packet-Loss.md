---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why HTTP/2 Suffers TCP Head-of-Line Blocking Under Packet Loss

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

Prerequisites: [[HTTP-2-Prioritization-and-Extensible-Priorities-RFC-9218]]

Next: [[QUIC-Transport-Streams-and-Connection-Migration-RFC-9000]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Models packet loss across multiplexed streams on a single ordered byte stream versus independent QUIC streams, proving how a single lost TCP segment stalls all active HTTP/2 streams.

## Further reading
