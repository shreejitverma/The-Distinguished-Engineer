---
type: pattern
track: [sde, distinguished, ai-eng]
level:
status: seed
last_reviewed:
sources: []
---

# AI API Gateways: Multi-Provider Routing, TTFT (Time-to-First-Token) Hedging, Prompt-Injection Guardrail Filters, and Token Cost Attribution

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

Prerequisites: [[Designing-Agent-Friendly-APIs-AXI-Principles-Token-Efficiency-and-Next-Step-Hints]]

Next: [[API-Estate-Inventory-Cataloging-RFC-9727-and-Shadow-API-Discovery]]

Lab plan: Implements an async AI gateway proxy that enforces a time-to-first-token (TTFT) deadline before the first SSE chunk, automatically failing over to a secondary provider if the primary stalls before streaming starts.

## Further reading
