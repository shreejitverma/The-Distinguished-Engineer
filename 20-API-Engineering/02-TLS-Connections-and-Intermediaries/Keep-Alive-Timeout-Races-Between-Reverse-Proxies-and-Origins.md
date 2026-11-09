---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# The Keep-Alive Timeout Race Condition: Why Origin Idle Timeouts Must Exceed Proxy Idle Timeouts

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

Prerequisites: [[TCP-Connection-Lifecycle-Keep-Alive-and-TIME-WAIT]]

Next: [[Outbound-Connection-Pools-Limits-and-DNS-TTL-Staleness]]

See also: [[HTTP-Retries-and-Timeouts-with-curl]]

Lab plan: Reproduces the race where an origin closes an idle keep-alive socket at the exact instant a reverse proxy dispatches a new request on it, resulting in 502 Bad Gateway on non-idempotent POSTs.

## Further reading
