---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# HTTP Request Smuggling: CL.TE, TE.CL, and TE.TE Framing Desynchronization (RFC 9112)

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [HTTP Semantics and Wire Protocols](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[HTTP-1-1-Pipelining-and-Head-of-Line-Blocking]]

Next: [[HTTP-2-Binary-Framing-and-Stream-State-Machine]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]]

Lab plan: Proves that RFC 9112 Section 6.3 strict parser rejection stops ambiguous dual Content-Length and Transfer-Encoding payloads that desynchronize a naive front-end proxy from a back-end parser.

## Further reading
