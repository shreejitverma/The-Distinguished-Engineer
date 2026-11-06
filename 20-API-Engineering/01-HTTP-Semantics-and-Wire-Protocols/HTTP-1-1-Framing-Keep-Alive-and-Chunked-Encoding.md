---
type: concept
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# HTTP/1.1 Wire Framing, Persistent Connections, and Chunked Transfer Coding (RFC 9112)

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

Prerequisites: [[URI-Syntax-Resolution-and-Templates-RFC-6570]]

Next: [[HTTP-1-1-Pipelining-and-Head-of-Line-Blocking]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]], [[curl-in-Depth]]

Lab plan: Feeds raw byte streams into h11 0.16.0 to prove Content-Length versus Transfer-Encoding: chunked framing, chunk extensions, trailer parsing, and connection reuse state transitions.

## Further reading
