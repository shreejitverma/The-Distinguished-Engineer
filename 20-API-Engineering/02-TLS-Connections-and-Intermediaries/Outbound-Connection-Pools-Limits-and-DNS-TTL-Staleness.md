---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Outbound HTTP Connection Pooling in httpx and httpcore: Pool Limits, Idle Expiry, and DNS Caching

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [TLS Connections and Intermediaries](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Keep-Alive-Timeout-Races-Between-Reverse-Proxies-and-Origins]]

Next: [[HTTP-2-Connection-Coalescing-and-421-Misdirected-Request]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/04-FastAPI]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/16-Asyncio-Deep-Dive]]

Lab plan: Configures httpx.Limits(max_connections, max_keepalive_connections, keepalive_expiry) and proves PoolTimeout behavior under concurrency bursts when callers create per-request clients vs a shared lifespan client.

## Further reading
