---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Production gRPC Tuning: HTTP/2 PING Keepalives (ENHANCE_YOUR_CALM / too_many_pings), Max Receive/Send Message Limits, and Stream Flow Control

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

Prerequisites: [[gRPC-Interceptors-in-AsyncIO-Auth-Logging-Metrics-and-Retries]]

Next: [[gRPC-Load-Balancing-Subchannels-Pick-First-vs-Round-Robin-and-xDS]]

See also: [[gRPC-and-Protocol-Buffers]]

Lab plan: Reproduces ResourceExhausted on messages exceeding the 4 MB default gRPC receive limit and demonstrates how aggressive client keepalive pings trigger GOAWAY ENHANCE_YOUR_CALM unless server permit_without_calls matches.

## Further reading
