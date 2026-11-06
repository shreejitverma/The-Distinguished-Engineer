---
type: pattern
track: [sde, distinguished, ai-eng]
level:
status: seed
last_reviewed:
sources: []
---

# Token-Per-Minute (TPM) Rate Limiting for AI APIs: Pre-Flight max_tokens Reservation and Post-Stream Actual Usage Reconciliation in Redis/Valkey

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

Prerequisites: [[Propagating-Client-Disconnects-to-Cancel-Upstream-GPU-Inference]]

Next: [[Structured-Output-Contracts-JSON-Schema-Subsets-for-Constrained-Decoding]]

See also: [[API-Fundamentals]]

Lab plan: Implements a two-phase atomic Lua token bucket that reserves (prompt_tokens + max_completion_tokens) before dispatching inference and refunds (max_completion_tokens - actual_completion_tokens) when the stream finishes or aborts.

## Further reading
