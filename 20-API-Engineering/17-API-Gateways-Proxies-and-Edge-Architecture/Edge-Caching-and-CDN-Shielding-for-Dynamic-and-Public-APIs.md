---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Putting a CDN in Front of an API: Origin Shielding, Collapse-Forwarding, Signed Edge URLs, and Cache-Control: s-maxage

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Gateways Proxies and Edge Architecture](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[HTTP-Desync-and-Header-Normalization-Across-Multi-Tier-Proxies]]

Next: [[Shadow-Traffic-Dark-Launches-and-Request-Mirroring-at-the-Gateway]]

See also: [[REST-APIs]]

Lab plan: Tests an origin-shielded API caching setup using s-maxage=60, stale-while-revalidate=300, and max-age=0 so CDNs cache for 60s with instant surrogate-key purge while browsers always revalidate at the edge.

## Further reading
