---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Hypercorn (0.18.0) Internals: Multiplexed HTTP/2 (h2 4.4.1) and HTTP/3 QUIC (aioquic 1.7.2) ASGI Worker Architecture

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [ASGI Servers and Async Runtime](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Uvicorn-Internals-Server-Lifecycle-Protocols-and-Flow-Control]]

Next: [[Granian-Internals-Rust-Tokio-Hyper-Engine-and-RSGI-vs-ASGI]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Runs an ASGI application under Hypercorn's HTTP/2 protocol handler, verifying concurrent stream multiplexing over a single socket where Uvicorn only supports HTTP/1.1.

## Further reading
