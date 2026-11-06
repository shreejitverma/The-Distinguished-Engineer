---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Offloading Large Payloads from API Servers with Presigned Upload URLs and Cryptographic Upload Policies

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Bulk Long Running and Error Contracts](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Multipart-Form-Streaming-and-Upload-Memory-Bounds]]

Next: [[Resumable-HTTP-Uploads-IETF-Draft-and-Tus-Protocol]]

Lab plan: Implements a two-phase upload contract (1: API mints a short-lived HMAC-signed upload URL with Content-Length and Content-Type constraints; 2: storage callback finalizes the metadata record).

## Further reading
