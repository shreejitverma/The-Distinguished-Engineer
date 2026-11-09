---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# TCP Socket Lifecycle for API Servers: Backlog Queues, Keep-Alive Probes, Nagle, and TIME_WAIT

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

Prerequisites: [[Client-Cert-Header-and-TLS-Terminating-Proxies-RFC-9440]]

Next: [[Keep-Alive-Timeout-Races-Between-Reverse-Proxies-and-Origins]]

Lab plan: Demonstrates ephemeral port exhaustion and latency spikes when an HTTP client disables connection pooling, vs reusing keep-alive sockets with TCP_NODELAY enabled.

## Further reading
