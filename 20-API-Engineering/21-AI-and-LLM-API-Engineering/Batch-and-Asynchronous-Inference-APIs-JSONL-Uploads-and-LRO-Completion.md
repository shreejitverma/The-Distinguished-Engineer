---
type: pattern
track: [sde, distinguished, ai-eng]
level:
status: seed
last_reviewed:
sources: []
---

# High-Throughput Batch Inference APIs: NDJSON/JSONL Request Files, 24-Hour SLO Queueing, Partial Failure Output Files, and Webhook Notification

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [AI and LLM API Engineering](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Streaming-Partial-JSON-Validation-in-Pydantic-v2-Experimental-Allow-Partial]]

Next: [[Multimodal-API-Design-Chunked-Audio-WebSockets-and-Content-Addressed-Blobs]]

See also: [[jq-Streaming-Large-Inputs]]

Lab plan: Implements a batch job API accepting an NDJSON input stream of custom_id-keyed requests, executing them through a bounded worker pool, and writing an NDJSON result stream with per-item response or error objects.

## Further reading
