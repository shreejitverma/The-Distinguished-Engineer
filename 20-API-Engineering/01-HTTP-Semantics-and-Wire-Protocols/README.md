---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# HTTP Semantics and Wire Protocols

Covers the abstract HTTP message model in RFC 9110 and its concrete wire serializations across HTTP/1.1 (RFC 9112), HTTP/2 (RFC 9113), and HTTP/3 over QUIC (RFC 9114 and RFC 9000).
Explains how semantics such as methods, header fields, content negotiation, range requests, and structured field values (RFC 9651) remain invariant while framing evolves from text delimiters to binary multiplexed streams and UDP transport.
Equips the reader to diagnose head-of-line blocking at both the application and transport layers, inspect HPACK and QPACK header compression state machines, and prevent framing desynchronization.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [The HTTP Message Abstraction: Control Data, Headers, Content, and Trailers (RFC 9110)](HTTP-Message-Abstraction-RFC-9110.md)
2. [URI Syntax, Percent-Encoding Normalization, and URI Templates (RFC 3986 and RFC 6570)](URI-Syntax-Resolution-and-Templates-RFC-6570.md)
3. [HTTP/1.1 Wire Framing, Persistent Connections, and Chunked Transfer Coding (RFC 9112)](HTTP-1-1-Framing-Keep-Alive-and-Chunked-Encoding.md)
4. [HTTP/1.1 Pipelining Hazards and Application-Layer Head-of-Line Blocking](HTTP-1-1-Pipelining-and-Head-of-Line-Blocking.md)
5. [HTTP Request Smuggling: CL.TE, TE.CL, and TE.TE Framing Desynchronization (RFC 9112)](HTTP-Request-Smuggling-CL-TE-and-Desync-Mechanics.md)
6. [HTTP/2 Binary Framing Layer, Frame Types, and the Stream Lifecycle State Machine (RFC 9113)](HTTP-2-Binary-Framing-and-Stream-State-Machine.md)
7. [HTTP/2 Stream and Connection Flow Control: WINDOW_UPDATE and Backpressure (RFC 9113)](HTTP-2-Flow-Control-and-Window-Update-Mechanics.md)
8. [HPACK Header Compression: Static Table, Dynamic Table Eviction, and CRIME/BREACH Defenses (RFC 7541)](HPACK-Header-Compression-and-Dynamic-Table-Security.md)
9. [Why RFC 7540 Stream Priority Trees Were Deprecated and Replaced by Extensible Priorities (RFC 9218)](HTTP-2-Prioritization-and-Extensible-Priorities-RFC-9218.md)
10. [Why HTTP/2 Suffers TCP Head-of-Line Blocking Under Packet Loss](TCP-Head-of-Line-Blocking-Under-Packet-Loss.md)
11. [QUIC Transport Internals: UDP Packets, Connection IDs, Independent Streams, and Migration (RFC 9000)](QUIC-Transport-Streams-and-Connection-Migration-RFC-9000.md)
12. [HTTP/3 Frame Mapping over QUIC and QPACK Out-of-Order Header Compression (RFC 9114)](HTTP-3-Framing-and-QPACK-Header-Compression-RFC-9114.md)
13. [Protocol Discovery and Upgrade: ALPN, Alt-Svc, and HTTPS DNS Records](Alt-Svc-and-HTTP-Version-Negotiation.md)
14. [Structured Field Values for HTTP: Lists, Dictionaries, Items, and Parameters (RFC 9651)](Structured-Field-Values-for-HTTP-RFC-9651.md)
15. [Proactive vs Reactive Content Negotiation, Media Type Parameters, and Quality Values (RFC 9110)](Content-Negotiation-Media-Types-and-Charsets.md)
16. [HTTP Content-Encoding: Gzip, Brotli, Zstandard, Streaming Compression, and BREACH Mitigations](HTTP-Compression-Gzip-Brotli-Zstandard-and-BREACH.md)
17. [Byte-Range Requests, 206 Partial Content, 416 Range Not Satisfiable, and If-Range (RFC 9110)](Range-Requests-Partial-Content-and-206-Semantics.md)
18. [Building Application Protocols with HTTP: BCP 56 Rules and Anti-Patterns (RFC 9205)](Protocol-Design-Over-HTTP-BCP-56-RFC-9205.md)
