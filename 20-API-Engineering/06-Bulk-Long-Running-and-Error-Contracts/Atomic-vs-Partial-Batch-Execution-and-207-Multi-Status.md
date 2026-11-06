---
type: pattern
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Atomic All-or-Nothing Batch Transactions vs Partial Success with per-Item Status (207 Multi-Status)

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

Prerequisites: [[Batch-Get-and-Bulk-Mutation-Design-AIP-231-to-AIP-235]]

Next: [[Criteria-Based-Bulk-Delete-and-Purge-Operations-AIP-165]]

See also: [[02-Programming-Languages/Python/Python-Backend-Interview-Prep/05-REST-API-Design]]

Lab plan: Tests both atomic batch mode (rolling back the entire SQL transaction if any item fails validation) and partial-failure mode (isolating item failures with per-index status codes and error objects).

## Further reading
