---
type: case-study
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Case Study: HTTP/2-to-HTTP/1.1 Downgrade Request Smuggling (H2.CL and H2.TE) and Unauthorized H2C Upgrade Tunneling Across Reverse Proxies

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [API Failure Case Studies](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Case-Study-Multi-Tenant-BOLA-and-Mass-Assignment-Privilege-Escalation]]

Next: [[Case-Study-CDN-Cache-Leak-of-Authenticated-PII-Responses]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Demonstrates how an HTTP/2 request containing a mismatched content-length header or an injected transfer-encoding header desynchronizes a backend HTTP/1.1 connection when a naive proxy downgrades without strict RFC 9113 Section 8.1 validation.

## Further reading
