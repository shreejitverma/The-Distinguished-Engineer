---
type: pattern
track: [sde, distinguished, ai-eng]
level:
status: seed
last_reviewed:
sources: []
---

# Validating Incomplete Streaming JSON Payloads on the Fly with pydantic-core (2.50.4) experimental_allow_partial

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

Prerequisites: [[Structured-Output-Contracts-JSON-Schema-Subsets-for-Constrained-Decoding]]

Next: [[Batch-and-Asynchronous-Inference-APIs-JSONL-Uploads-and-LRO-Completion]]

Lab plan: Feeds truncated JSON prefixes ('{"title": "Hel', '{"title": "Hello", "items": [{"id": 1') into TypeAdapter.validate_json(..., experimental_allow_partial='trailing-strings') to render progressive structured UI updates.

## Further reading
