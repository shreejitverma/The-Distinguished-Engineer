---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Receiving Webhooks Safely: Raw-Bytes HMAC Verification Before JSON Parsing, 5-Minute Timestamp Replay Windows, and Event-ID Deduplication

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

Prerequisites: [[Webhook-Sender-Architecture-Standard-Webhooks-Signatures-and-Fan-Out]]

Next: [[L4-vs-L7-Load-Balancing-for-HTTP-1-1-HTTP-2-and-gRPC]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Tests a FastAPI webhook receiver that verifies HMAC signatures against raw request.body() bytes, rejects timestamps older than 300s, deduplicates webhook-id in Redis/DB, and acknowledges with 202 before slow processing.

## Further reading
