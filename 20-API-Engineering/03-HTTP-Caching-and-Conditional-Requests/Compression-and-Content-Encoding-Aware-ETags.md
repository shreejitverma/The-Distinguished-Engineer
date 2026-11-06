---
type: problem
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Why Content-Encoding Must Change the Strong ETag: Avoiding Proxy Cache Corruption Across Gzip and Identity

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [HTTP Caching and Conditional Requests](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Strong-vs-Weak-Validators-ETag-and-Last-Modified]]

Next: [[Conditional-GET-Revalidation-and-304-Not-Modified]]

Lab plan: Reproduces the bug where a middleware compresses a response without modifying its strong ETag, causing a byte-range client or conditional cache to mix gzip and identity representations.

## Further reading
