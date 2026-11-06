---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# L4 (TCP/UDP) vs L7 (Application) Load Balancing: Why Multiplexed HTTP/2 and gRPC Connections Pin Traffic to a Single Backend Under L4 Balancing

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Gateways Proxies and Edge Architecture](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]]

Next: [[Load-Balancing-Algorithms-Round-Robin-Least-Request-P2C-and-Consistent-Hashing]]

See also: [[gRPC-and-Protocol-Buffers]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Simulates an L4 connection-level balancer vs an L7 stream-level balancer with 2 long-lived HTTP/2 clients and 10 backend pods, proving severe load skew under L4.

## Further reading
