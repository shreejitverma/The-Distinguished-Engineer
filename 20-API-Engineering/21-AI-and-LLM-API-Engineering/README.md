---
type: moc
track: [sde, distinguished, ai-eng]
level:
status: draft
last_reviewed:
sources: []
---

# AI and LLM API Engineering

Extends the book to the engineering challenges of AI, LLM, and agent-facing APIs `(as of 2026-11)`.
Covers streaming token generation over Server-Sent Events (SSE), mid-stream error reporting after `200 OK` headers are committed, aborting expensive GPU inference immediately upon `http.disconnect`, and two-phase token-bucket rate limiting (prompt estimation plus post-stream reconciliation).
Details constrained JSON Schema subsets for structured outputs, async batch inference APIs, and the Model Context Protocol (`MCP 2026-07-28`, `mcp 2.3.0`) alongside AXI (Agent eXperience Interface) design principles.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Designing Streaming Token Generation APIs over SSE: Delta Chunk Framing, Proxy Buffering Disabling (X-Accel-Buffering: no), Heartbeat Comments, and Error Events After 200 OK](Streaming-LLM-Token-APIs-SSE-Framing-Heartbeats-and-Mid-Stream-Errors.md)
2. [Saving Compute on Abandoned Requests: Detecting ASGI http.disconnect During SSE Streaming and Cancelling Upstream httpx / GPU Inference Streams Immediately](Propagating-Client-Disconnects-to-Cancel-Upstream-GPU-Inference.md)
3. [Token-Per-Minute (TPM) Rate Limiting for AI APIs: Pre-Flight max_tokens Reservation and Post-Stream Actual Usage Reconciliation in Redis/Valkey](Token-Aware-Rate-Limiting-Pre-Flight-Reservation-and-Post-Stream-Reconciliation.md)
4. [Structured Output Contracts for LLM APIs: Compiling Pydantic v2 Models into Strict Constrained-Decoding JSON Schemas (additionalProperties: false, Supported Keywords)](Structured-Output-Contracts-JSON-Schema-Subsets-for-Constrained-Decoding.md)
5. [Validating Incomplete Streaming JSON Payloads on the Fly with pydantic-core (2.50.4) experimental_allow_partial](Streaming-Partial-JSON-Validation-in-Pydantic-v2-Experimental-Allow-Partial.md)
6. [High-Throughput Batch Inference APIs: NDJSON/JSONL Request Files, 24-Hour SLO Queueing, Partial Failure Output Files, and Webhook Notification](Batch-and-Asynchronous-Inference-APIs-JSONL-Uploads-and-LRO-Completion.md)
7. [Multimodal API Engineering: Content-Addressed Media References vs Base64 JSON Bloat and Bidirectional Audio/Video Streaming](Multimodal-API-Design-Chunked-Audio-WebSockets-and-Content-Addressed-Blobs.md)
8. [The Model Context Protocol (MCP 2026-07-28, mcp 2.3.0): JSON-RPC 2.0 Lifecycle, Streamable HTTP vs stdio Transports, Tools, Resources, and Prompts](Model-Context-Protocol-MCP-Architecture-JSON-RPC-Transports-and-Tool-Contracts.md)
9. [Designing APIs for AI Agents (AXI Principles): Minimal Default Schemas, Content Truncation with --full Escape Hatches, Pre-Computed Aggregates, and Actionable Error Recovery](Designing-Agent-Friendly-APIs-AXI-Principles-Token-Efficiency-and-Next-Step-Hints.md)
10. [AI API Gateways: Multi-Provider Routing, TTFT (Time-to-First-Token) Hedging, Prompt-Injection Guardrail Filters, and Token Cost Attribution](AI-Gateway-Engineering-Semantic-Caching-Provider-Fallbacks-and-Guardrail-Pipelines.md)
