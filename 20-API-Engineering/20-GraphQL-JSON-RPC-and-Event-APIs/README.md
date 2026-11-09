---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# GraphQL JSON RPC and Event APIs

Examines the major non-REST, non-gRPC API paradigms: GraphQL (`September2025` specification and `strawberry-graphql 0.340.0`), JSON-RPC 2.0, Event-Driven APIs (`AsyncAPI 3.1.0` and `CloudEvents 1.0.2`), and real-time push protocols.
Details GraphQL AST parsing, resolver execution, `DataLoader` batching to eliminate N+1 queries, query depth/cost DoS defenses, Persisted Queries, and Apollo Federation.
Concludes with a quantitative architectural decision framework for choosing between REST, gRPC, GraphQL, JSON-RPC, WebSockets, SSE, and Event-Driven APIs.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [GraphQL (September 2025 Specification) Internals: Schema Definition Language (SDL), Document AST Validation, and Breadth-First Resolver Execution](GraphQL-Specification-September-2025-SDL-AST-and-Execution-Engine.md)
2. [Building Type-Safe GraphQL APIs in Python with Strawberry (0.340.0) and FastAPI: @strawberry.type, Input Mutations, Union Errors, and WebSocket Subscriptions](Strawberry-GraphQL-with-FastAPI-Types-Context-Mutations-and-Subscriptions.md)
3. [How GraphQL Resolvers Trigger N+1 Query Cascades and How DataLoader Event-Loop Tick Coalescing Collapses Them to O(1) Queries](The-GraphQL-N-Plus-1-Problem-and-DataLoader-Tick-Batching-Internals.md)
4. [Hardening GraphQL in Production: AST Depth Limits, Weighted Query Complexity Cost Analysis, Alias/Batching Brute-Force Caps, and Disabling Introspection](GraphQL-Security-Query-Depth-Complexity-Cost-Analysis-and-Batch-Limits.md)
5. [Field-Level Authorization in GraphQL, Non-Null (!) Error Bubbling Traps, and Why Partial Success Returns HTTP 200](GraphQL-Field-Level-Authorization-and-Partial-Error-Null-Bubbling.md)
6. [Overcoming GraphQL's POST Caching Problem: Automatic Persisted Queries (APQ), Trusted Documents (Allowlisted Query Hashes), and GET CDN Caching](Automatic-Persisted-Queries-APQ-and-Edge-CDN-Caching-for-GraphQL.md)
7. [Distributed GraphQL at Enterprise Scale: Apollo Federation v2 (@key, @external, @requires, _entities Resolver) and Router Query Plan Execution](Distributed-GraphQL-Apollo-Federation-v2-Entities-and-Subgraph-Planning.md)
8. [JSON-RPC 2.0 in Depth: Request/Response Envelope, Positional vs Named params, Notifications (No id), Batch Arrays, and Standard Error Codes (-32700 to -32603)](JSON-RPC-2-0-Specification-Batching-Notifications-and-Error-Codes.md)
9. [Event-Driven API Contracts with AsyncAPI 3.1.0 (January 2026): Channels, Operations (send/receive), Messages, and Multi-Protocol Bindings (Kafka, AMQP, WebSockets)](Event-Driven-APIs-and-AsyncAPI-3-1-0-Specification.md)
10. [Standardizing Event Payloads with CloudEvents (v1.0.2): Context Attributes (id, source, type, specversion), Binary vs Structured HTTP Binding, and Schema Evolution](CloudEvents-1-0-2-Envelope-Structured-vs-Binary-Content-Modes-and-Schema-Registries.md)
11. [Consuming Asynchronous Events Safely: The Transactional Inbox Pattern, Out-of-Order Version Guards, and Dead-Letter Queue (DLQ) Escalation](Idempotent-Event-Consumers-Inbox-Pattern-and-Poison-Message-DLQs.md)
12. [Real-Time API Transport Selection: WebSockets (RFC 6455) vs Server-Sent Events (SSE) vs HTTP/2-3 Streaming vs Long Polling Across Proxies and Firewalls](Real-Time-Push-Protocols-Compared-WebSockets-vs-SSE-vs-HTTP-2-3-Streaming-vs-Long-Polling.md)
13. [Asynchronous Request-Reply Topologies: Polling vs Webhooks vs Callback URLs vs SSE Notification Channels](HTTP-Callback-Subscriptions-WebSub-and-Async-Request-Reply-Patterns.md)
14. [The Principal Engineer's Paradigm Decision Matrix: Choosing REST, gRPC, GraphQL, JSON-RPC, or Event Streams by Boundary, Team Topology, and Latency](Architectural-Paradigm-Selection-REST-vs-gRPC-vs-GraphQL-vs-JSON-RPC-vs-Events.md)
