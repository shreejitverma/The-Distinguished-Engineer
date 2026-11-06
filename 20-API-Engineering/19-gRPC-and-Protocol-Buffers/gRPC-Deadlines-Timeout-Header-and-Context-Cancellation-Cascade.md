---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Deadlines and Cancellation Propagation in gRPC: The grpc-timeout Wire Header and Automatic Downstream RPC Abortion

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

Prerequisites: [[The-Four-gRPC-Communication-Modes-Unary-Server-Client-and-Bidi-Streaming]]

Next: [[gRPC-Status-Codes-and-Google-RPC-Status-Rich-Error-Payloads]]

See also: [[gRPC-and-Protocol-Buffers]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Starts a 2-hop gRPC call chain with a 100ms deadline, stalls Hop 2 for 200ms, and proves that Hop 1 and Hop 2 both abort with StatusCode.DEADLINE_EXCEEDED and cancel their running asyncio tasks.

## Further reading
