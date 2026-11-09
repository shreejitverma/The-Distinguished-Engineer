---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Engineering Outbound Webhooks: Standard Webhooks Specification (webhook-id, webhook-timestamp, webhook-signature), Retries, and Dead-Endpoint Circuit Breaking

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Resilience Rate Limiting and Idempotency](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Hedged-Requests-and-Tail-Latency-Taming-at-Scale]]

Next: [[Webhook-Consumer-Engineering-Signature-Verification-Replay-Windows-and-Idempotency]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]], [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/08-Microservices-and-Messaging]]

Lab plan: Implements a Standard Webhooks v1 signer and delivery worker with HMAC-SHA256 v1,<base64> signatures, dual secret rotation, exponential backoff schedule, and SSRF-safe outbound delivery.

## Further reading
