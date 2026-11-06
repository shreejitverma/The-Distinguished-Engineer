---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Operating gRPC in Production: The Standard gRPC Health Checking Protocol (grpc.health.v1.Health), Server Reflection, and server.stop(grace)

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

Prerequisites: [[HTTP-JSON-to-gRPC-Transcoding-and-Google-API-HTTP-Annotations-AIP-127]]

Next: [[GraphQL-Specification-September-2025-SDL-AST-and-Execution-Engine]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Tests per-service health status transitions (SERVING -> NOT_SERVING) via Health.Check and Health.Watch streaming RPCs prior to graceful server drain.

## Further reading
