---
type: playbook
track: [sde, distinguished]
level:
status: seed
last_reviewed:
sources: []
---

# Sixty-Second API Incident Triage: Differentiating Traffic Spikes, Downstream Stalls, DB Lock Convoys, Event-Loop Blocking, and Bad Deploys

> [!summary] Claim
> Seed note: the claim, mechanism, and proof are written when this note is drafted.

Part of [Observability Profiling and Operations](README.md) in [API Engineering](../README.md).

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

Prerequisites: [[Feature-Flags-Dynamic-Configuration-and-Safe-Kill-Switches-in-APIs]]

Next: [[Protobuf-Wire-Format-TLV-Tags-Varints-and-ZigZag-Encoding]]

Lab plan: Runs an automated diagnostic decision tree against simulated telemetry snapshots (high latency + low CPU + full DB pool vs high CPU + high loop lag vs rising 429s) to classify the root cause deterministically.

## Further reading
