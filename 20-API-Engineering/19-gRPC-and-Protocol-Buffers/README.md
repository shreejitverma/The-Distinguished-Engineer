---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# gRPC and Protocol Buffers

Covers Protocol Buffers (`protobuf 7.36.2`, released 2026-09-17) and gRPC (`grpcio 1.85.1`, released 2026-10-30) down to the byte and HTTP/2 frame level.
Details TLV tag-wire encoding, Base-128 Varints, ZigZag signed integers, Protobuf Editions (`2023`/`2024`), wire-safe schema evolution (`reserved` tags/names), and `buf` (`v1.75.0`) linting and breaking-change enforcement.
Examines gRPC's 5-byte length-prefixed framing over HTTP/2, the four RPC streaming modes, `grpc-timeout` deadline propagation, rich status details (`google.rpc.Status`), HTTP/JSON-to-gRPC transcoding (AIP-127), and async `grpc.aio` interceptors.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [Protocol Buffers (7.36.2) Binary Wire Format: Tag-Length-Value (TLV), Base-128 Varints, ZigZag Signed Encoding, and Packed Repeated Fields](Protobuf-Wire-Format-TLV-Tags-Varints-and-ZigZag-Encoding.md)
2. [Protobuf Editions (2023 and 2024) vs proto2 and proto3: Explicit vs Implicit Field Presence, Open vs Closed Enums, and Feature Resolution](Protobuf-Editions-2023-2024-vs-Proto2-and-Proto3-Presence-Semantics.md)
3. [Wire-Safe Protobuf Schema Evolution: Why Field Numbers Can Never Be Reused, reserved Tags and Names, Wire-Compatible Type Promotions, and Unknown Field Preservation](Protobuf-Schema-Evolution-Invariants-Reserved-Tags-and-Unknown-Fields.md)
4. [Schema Governance for Protocol Buffers with Buf (v1.75.0): Package Layout, Linting Categories, and Wire vs Source Breaking-Change Detection](Protobuf-Governance-Linting-and-Breaking-Change-Gates-with-Buf.md)
5. [How gRPC Maps to HTTP/2: Pseudo-Headers, 5-Byte Length-Prefixed Message Envelopes, and Trailers-Only grpc-status Responses](gRPC-Over-HTTP-2-Framing-Length-Prefixed-Messages-and-Trailers.md)
6. [gRPC's Four Streaming Modes in Python grpc.aio (1.85.1): Unary, Server Streaming, Client Streaming, and Full-Duplex Bidirectional Streaming](The-Four-gRPC-Communication-Modes-Unary-Server-Client-and-Bidi-Streaming.md)
7. [Deadlines and Cancellation Propagation in gRPC: The grpc-timeout Wire Header and Automatic Downstream RPC Abortion](gRPC-Deadlines-Timeout-Header-and-Context-Cancellation-Cascade.md)
8. [gRPC Error Handling: The 16 Canonical Status Codes, grpc-status-details-bin Trailers, and Unpacking google.rpc.Status Protobuf Details](gRPC-Status-Codes-and-Google-RPC-Status-Rich-Error-Payloads.md)
9. [Building Async Server and Client Interceptors in grpc.aio (1.85.1) for JWT Verification, OpenTelemetry Tracing, and Service-Config Retries](gRPC-Interceptors-in-AsyncIO-Auth-Logging-Metrics-and-Retries.md)
10. [Production gRPC Tuning: HTTP/2 PING Keepalives (ENHANCE_YOUR_CALM / too_many_pings), Max Receive/Send Message Limits, and Stream Flow Control](gRPC-Keepalive-Flow-Control-and-Max-Message-Size-Tuning.md)
11. [Solving the gRPC Load Balancing Dilemma: Headless DNS Service Discovery, Client-Side round_robin Subchannels, and Proxyless xDS](gRPC-Load-Balancing-Subchannels-Pick-First-vs-Round-Robin-and-xDS.md)
12. [Serving Both REST/JSON and gRPC from One Protobuf Contract: google.api.http Annotations and Envoy gRPC-JSON Transcoder (AIP-127)](HTTP-JSON-to-gRPC-Transcoding-and-Google-API-HTTP-Annotations-AIP-127.md)
13. [Operating gRPC in Production: The Standard gRPC Health Checking Protocol (grpc.health.v1.Health), Server Reflection, and server.stop(grace)](gRPC-Health-Checking-Protocol-Server-Reflection-and-Graceful-Stop.md)
