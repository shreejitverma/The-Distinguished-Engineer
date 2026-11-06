---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Granian (2.8.7) Internals: Rust Tokio/Hyper HTTP/1 and HTTP/2 Engine, PyO3 Bridging, and ASGI vs RSGI Protocol Overhead

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

Prerequisites: [[Hypercorn-Internals-HTTP-2-HTTP-3-QUIC-and-AnyIO-Trio-Workers]]

Next: [[Multi-Process-Worker-Topologies-Gunicorn-Uvicorn-Workers-vs-Kubernetes-Replicas]]

Lab plan: Tests Granian 2.8.7 ASGI scope construction and compares the dictionary allocation overhead of standard ASGI against Granian's direct Rust-to-Python event dispatch.

## Further reading
