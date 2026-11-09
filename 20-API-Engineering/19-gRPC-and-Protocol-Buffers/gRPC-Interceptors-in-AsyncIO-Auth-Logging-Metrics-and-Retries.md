---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Building Async Server and Client Interceptors in grpc.aio (1.85.1) for JWT Verification, OpenTelemetry Tracing, and Service-Config Retries

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

Prerequisites: [[gRPC-Status-Codes-and-Google-RPC-Status-Rich-Error-Payloads]]

Next: [[gRPC-Keepalive-Flow-Control-and-Max-Message-Size-Tuning]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Implements a grpc.aio.ServerInterceptor that validates Bearer metadata tokens and a service-config JSON retry policy that automatically retries UNAVAILABLE status codes with backoff.

## Further reading
