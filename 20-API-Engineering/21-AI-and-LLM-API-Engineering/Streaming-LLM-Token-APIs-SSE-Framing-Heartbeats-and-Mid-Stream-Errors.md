---
type: pattern
track: [sde, distinguished, ai-eng]
level:
status: seed
last_reviewed:
sources: []
---

# Designing Streaming Token Generation APIs over SSE: Delta Chunk Framing, Proxy Buffering Disabling (X-Accel-Buffering: no), Heartbeat Comments, and Error Events After 200 OK

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [AI and LLM API Engineering](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Architectural-Paradigm-Selection-REST-vs-gRPC-vs-GraphQL-vs-JSON-RPC-vs-Events]]

Next: [[Propagating-Client-Disconnects-to-Cancel-Upstream-GPU-Inference]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]]

Lab plan: Implements a streaming completion endpoint in FastAPI that emits typed SSE delta events, sends ': ping' keepalives every 15s, and emits a terminal event: error frame with RFC 9457 JSON if generation fails after 200 OK.

## Further reading
