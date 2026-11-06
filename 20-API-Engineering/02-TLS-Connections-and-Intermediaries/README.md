---
type: moc
track: [sde, distinguished]
level:
status: draft
last_reviewed:
sources: []
---

# TLS Connections and Intermediaries

Examines the transport and intermediary layer beneath HTTP APIs: TLS 1.3 (RFC 9846 and RFC 9325), 0-RTT replay hazards (RFC 8470), X.509 certificate validation, and Mutual TLS (mTLS) client authentication (RFC 8705 and RFC 9440).
Details TCP socket lifecycle, connection pooling, keep-alive race conditions between proxies and origins, connection coalescing, and hop-by-hop versus end-to-end header semantics.
Shows how reverse proxies and load balancers propagate client identity via Forwarded (RFC 7239) and how misconfigured trust boundaries lead to IP spoofing and header injection.

Part of [API Engineering](../README.md).

## Learning order

Read in this order; each note links its prerequisite and the next note.

1. [TLS 1.3 Handshake Mechanics, Key Schedule, Cipher Suites, and ALPN Negotiation (RFC 9846)](TLS-1-3-Handshake-Cipher-Suites-and-ALPN-RFC-9846.md)
2. [TLS 1.3 Session Tickets, 0-RTT Early Data Replay Hazards, and HTTP 425 Too Early (RFC 8470)](TLS-Session-Resumption-0-RTT-Early-Data-and-425-Too-Early.md)
3. [X.509 Certificate Chains, SAN Matching, Expiration, Revocation, and OCSP Stapling for APIs](X509-Certificates-Chain-Validation-SAN-and-OCSP-Stapling.md)
4. [Mutual TLS (mTLS) Client Authentication, Certificate Pinning, and SPIFFE SVID URI Identities](Mutual-TLS-mTLS-Handshake-and-SPIFFE-Identity.md)
5. [Propagating mTLS Identity Behind Terminating Proxies with the Client-Cert Header (RFC 9440)](Client-Cert-Header-and-TLS-Terminating-Proxies-RFC-9440.md)
6. [TCP Socket Lifecycle for API Servers: Backlog Queues, Keep-Alive Probes, Nagle, and TIME_WAIT](TCP-Connection-Lifecycle-Keep-Alive-and-TIME-WAIT.md)
7. [The Keep-Alive Timeout Race Condition: Why Origin Idle Timeouts Must Exceed Proxy Idle Timeouts](Keep-Alive-Timeout-Races-Between-Reverse-Proxies-and-Origins.md)
8. [Outbound HTTP Connection Pooling in httpx and httpcore: Pool Limits, Idle Expiry, and DNS Caching](Outbound-Connection-Pools-Limits-and-DNS-TTL-Staleness.md)
9. [HTTP/2 and HTTP/3 Connection Coalescing Across Wildcard Certificates and 421 Misdirected Request](HTTP-2-Connection-Coalescing-and-421-Misdirected-Request.md)
10. [Hop-by-Hop vs End-to-End Headers and the Connection Header Stripping Vulnerability (RFC 9110)](Hop-by-Hop-vs-End-to-End-Headers-and-Connection-Header-Abuse.md)
11. [Proxy Identity Propagation: RFC 7239 Forwarded vs X-Forwarded-For Right-to-Left Trusted Hop Parsing](Forwarded-Header-RFC-7239-and-X-Forwarded-For-Spoofing.md)
12. [Expect: 100-continue Mechanics: Rejecting Unauthorized or Oversized Uploads Before Body Transmission](100-Continue-Expect-Header-and-Large-Upload-Admission.md)
13. [Informational 1xx Responses and 103 Early Hints for Link Preloading (RFC 8297 and RFC 9110)](103-Early-Hints-and-Informational-Responses.md)
14. [HTTP Trailer Fields: Streaming Digest Integrity (RFC 9530) and Trailers-Only Responses](HTTP-Trailers-Streaming-Checksums-and-gRPC-Status.md)
