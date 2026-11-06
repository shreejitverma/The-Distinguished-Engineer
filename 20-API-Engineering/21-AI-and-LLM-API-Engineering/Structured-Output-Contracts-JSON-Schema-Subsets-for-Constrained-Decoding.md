---
type: pattern
track: [sde, distinguished, ai-eng]
level:
status: seed
last_reviewed:
sources: []
---

# Structured Output Contracts for LLM APIs: Compiling Pydantic v2 Models into Strict Constrained-Decoding JSON Schemas (additionalProperties: false, Supported Keywords)

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

Prerequisites: [[Token-Aware-Rate-Limiting-Pre-Flight-Reservation-and-Post-Stream-Reconciliation]]

Next: [[Streaming-Partial-JSON-Validation-in-Pydantic-v2-Experimental-Allow-Partial]]

Lab plan: Builds a Pydantic v2 GenerateJsonSchema subclass that recursively enforces additionalProperties: false, populates all properties into required, and rejects recursive or unsupported regex keywords that break CFG/FSM token masks.

## Further reading
