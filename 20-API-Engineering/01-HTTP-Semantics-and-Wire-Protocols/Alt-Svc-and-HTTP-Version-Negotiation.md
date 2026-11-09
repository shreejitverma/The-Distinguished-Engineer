---
type: pattern
track: [sde, distinguished]
level:
status: solid
last_reviewed:
sources:
  - "https://www.rfc-editor.org/rfc/rfc7301.html"
  - "https://www.rfc-editor.org/rfc/rfc7838.html"
  - "https://www.rfc-editor.org/rfc/rfc9460.html"
  - "https://www.rfc-editor.org/rfc/rfc9848.txt"
  - "https://www.rfc-editor.org/rfc/rfc9849.txt"
  - "https://www.rfc-editor.org/rfc/rfc9113.html"
  - "https://www.rfc-editor.org/rfc/rfc9114.html"
  - "https://www.rfc-editor.org/rfc/rfc9110.html"
  - "https://www.rfc-editor.org/rfc/rfc9111.txt"
  - "https://www.rfc-editor.org/rfc/rfc8446.txt"
  - "https://www.rfc-editor.org/rfc/rfc6066.txt"
  - "https://www.rfc-editor.org/rfc/rfc1035.txt"
  - "https://raw.githubusercontent.com/python/cpython/v3.14.6/Modules/_ssl.c"
  - "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2024-5535"
  - "https://curl.se/docs/vuln.json"
  - "https://api-security.owasp.org/editions/2023/en/0xa8-security-misconfiguration/"
  - "https://www.rfc-editor.org/rfc/rfc7301.txt"
  - "https://www.rfc-editor.org/rfc/rfc7838.txt"
  - "https://www.rfc-editor.org/rfc/rfc9460.txt"
  - "https://www.rfc-editor.org/rfc/rfc9113.txt"
  - "https://www.rfc-editor.org/rfc/rfc9114.txt"
  - "https://www.rfc-editor.org/rfc/rfc9110.txt"
  - "https://www.iana.org/assignments/tls-parameters/tls-parameters-8.csv"
  - "https://raw.githubusercontent.com/emmett-framework/granian/v2.8.7/src/workers.rs"
  - "https://codeload.github.com/emmett-framework/granian/tar.gz/refs/tags/v2.8.7"
---

# Protocol Discovery and Upgrade: ALPN, Alt-Svc, and HTTPS DNS Records

> [!summary] Claim
> A URI never says which HTTP version to speak, so clients learn it from three channels with different trust: the TLS ALPN extension picks the protocol for one connection (server preference wins), `Alt-Svc` advertises other endpoints for a whole origin with a freshness lifetime, and the HTTPS DNS record publishes endpoints and ALPN sets before the first connection; all three are only safe because the alternative must still present a certificate for the origin.

Part of [HTTP Semantics and Wire Protocols](README.md) in [API Engineering](../README.md).

## TL;DR

- Mechanism: the client lists protocols in the ClientHello extension 16 and the server picks one (RFC 7301 Section 3.2); `Alt-Svc: h3=":443"; ma=86400` or an HTTPS record (DNS type 65) tells the client that HTTP/3 exists at all, because HTTP/3 runs over UDP and cannot be negotiated inside a TCP handshake (RFC 9114 Sections 3.1 and 3.1.1).
- Contract rule: the server SHOULD select its own most preferred protocol that the client offered and SHALL send a fatal `no_application_protocol` (120) alert when there is no overlap (RFC 7301 Section 3.2); an `Alt-Svc` value replaces all cached alternatives for the origin, and `clear` removes them (RFC 7838 Sections 3 and 3.1).
- Trap: CPython's `ssl` server does not send that alert; with no overlap the handshake completes and both sides see `selected_alpn_protocol() is None` (CPython 3.14.6 Modules/_ssl.c, do_protocol_selection), so a client that needs `h2` must check the result itself (RFC 7838 Section 2.4).
- Portability: `h2` exists only over TLS and `h2c` Upgrade is deprecated (RFC 9113 Section 3.1); among the pinned Python libraries, Hypercorn 0.18.0 negotiates `h2` and emits `Alt-Svc` for its QUIC listener, while httpx 0.28.1 and httpcore 1.0.9 offer `["http/1.1", "h2"]` and contain no `Alt-Svc` handling (as of 2026-11).
- When not to use it: do not advertise an alternative you cannot keep up for its whole `ma`, do not send `Alt-Svc` from `http://` origins whose apps infer security from the port (RFC 7838 Section 9.5), and do not use protocol discovery to route tenants; use explicit endpoints or a load balancer for that.

## Mental model

```mermaid
sequenceDiagram
    participant C as Client
    participant D as DNS resolver
    participant O as origin.example.test:443 (TCP)
    participant Q as alternative :443 (UDP)
    C->>D: A, AAAA, and HTTPS (type 65) queries
    D-->>C: HTTPS 1 . alpn=h3 (SVCB ALPN set h3 + http/1.1)
    C->>O: ClientHello SNI=origin.example.test<br/>ALPN [h2, http/1.1]
    O-->>C: ServerHello, EncryptedExtensions<br/>ALPN h2 (server preference)
    O-->>C: HEADERS + Alt-Svc: h3=":443"; ma=86400
    Note over C: cache (h3, origin host, 443)<br/>fresh until generated + ma - Age
    C->>Q: QUIC Initial, SNI=origin.example.test<br/>ALPN [h3]
    Q-->>C: certificate valid for origin.example.test
    C->>Q: request with Alt-Used header
    Q-->>C: 421 Misdirected Request
    Note over C: drop that alternative,<br/>retry on origin (RFC 7838 Section 6)
```

The client asks DNS for the HTTPS record in parallel with address records, so it can know the ALPN set before it sends any ClientHello (RFC 9460 Section 3).
The first TCP connection negotiates `h2` through ALPN, and the response carries `Alt-Svc`, which the client caches per origin with a lifetime counted from when the response was generated (RFC 7838 Section 3.1).
The QUIC connection to the alternative still names the origin in SNI and must present a certificate for the origin (RFC 7838 Sections 2.1 and 2.3), which is the only thing that stops an injected `Alt-Svc` from hijacking the origin.
When the alternative answers `421`, the client removes that cache entry and may retry elsewhere, regardless of method idempotency (RFC 7838 Section 6).

## How it works

### ALPN: one connection, server decides

ALPN is TLS extension type 16 carrying `ProtocolNameList`: a 2-octet length, then each name as a 1-octet length and 1 to 255 opaque octets, with a total of 2 to 65535 octets (RFC 7301 Section 3.1).
The ServerHello extension carries exactly one name, and the choice is definitive for the connection (RFC 7301 Sections 3.1 and 3.2).

```text
ALPN extension for ["h2", "http/1.1"] (RFC 7301 Section 3.1), 18 bytes on the wire
00 10 | 00 0e | 00 0c | 02 68 32 | 08 68 74 74 70 2f 31 2e 31
type   ext len  list    "h2"       "http/1.1"
16     14       12
```

The extension sits in the plaintext ClientHello inside a TLS record of type 22 and a handshake message of type 1 (RFC 8446 Sections 4, 4.1.2, 4.2, and 5.1), next to SNI, extension type 0 (RFC 6066 Section 3).
In TLS 1.2 the server's choice is sent in plaintext by design (RFC 7301 Section 4); in TLS 1.3 the extension is allowed only in `ClientHello` and `EncryptedExtensions` (RFC 8446 Section 4.2), so the choice is encrypted while the client's offer stays visible unless Encrypted ClientHello is used (RFC 9849, RFC 9848 Section 5.2).
The lab reads both SNI and ALPN out of a real ClientHello captured from CPython's `ssl.MemoryBIO`, and the measured record lengths show the ALPN extension costs exactly its 4-byte header plus the list.

The registered identifiers that matter for HTTP:

- `http/1.1` is HTTP/1.1 over TLS (RFC 7301 Section 6).
- `h2` is HTTP/2 over TLS, the two octets `0x68 0x32`; `h2c` MUST NOT be sent or selected in ALPN, and the `h2c` Upgrade token and `HTTP2-Settings` header are deprecated (RFC 9113 Sections 3.1 and 3.2).
- `h3` is HTTP/3 over QUIC version 1, which uses TLS 1.3 for its handshake and requires SNI when the server is named by a domain (RFC 9114 Sections 3.1 and 3.2).

Selection belongs to the server: it SHOULD pick "the most highly preferred protocol that it supports and that is also advertised by the client" (RFC 7301 Section 3.2).
OpenSSL's helper does the same; NVD's record for CVE-2024-5535 describes `SSL_select_next_proto` as returning "the first protocol that appears in the server list that also appears in the client list".
CPython wires that helper into the server callback, and returns `SSL_TLSEXT_ERR_NOACK` instead of an alert when it fails (CPython 3.14.6 Modules/_ssl.c, do_protocol_selection and _selectALPN_cb):

```c
    ret = SSL_select_next_proto(out, outlen,
                                server_protocols, server_protocols_len,
                                client_protocols, client_protocols_len);
    if (alpn && ret != OPENSSL_NPN_NEGOTIATED)
        return SSL_TLSEXT_ERR_NOACK;
```

Those lines are copied verbatim from the fetched source, and the lab proves its consequence on five CPython releases and three OpenSSL builds: no overlap yields a completed TLS 1.3 handshake with no protocol, not the alert RFC 7301 requires.
The divergence is CPython-specific: a real Granian 2.8.7 listener (rustls) run here with `--http 2` answered a client offering only `http/1.1` with `SSLError: TLSV1_ALERT_NO_APPLICATION_PROTOCOL`, which is the alert RFC 7301 Section 3.2 requires (CPython 3.14.6 client, macOS 27.2).
CPython's `SSLContext.set_alpn_protocols` encodes each name with `bytes(protocol, 'ascii')` and rejects lengths outside 1 to 255 (CPython 3.14.6 Lib/ssl.py, SSLContext.set_alpn_protocols), so non-ASCII identifiers cannot be offered from Python.

### Prior knowledge and Upgrade: cleartext only

For `http://` URIs there is no TLS, so there is no ALPN.
A client with prior knowledge sends the 24-octet preface `PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n` directly (RFC 9113 Sections 3.3 and 3.4); otherwise HTTP/1.1 `Upgrade` (RFC 9110 Section 7.8) was the only in-band path, and RFC 9113 Section 3.1 deprecates it for HTTP/2.
Hypercorn 0.18.0 still supports both: it answers `Upgrade: h2c` on a request without a body with `101` and switches, and treats a `PRI * HTTP/2.0` request line as prior knowledge (hypercorn 0.18.0 hypercorn/protocol/h11.py, H11Protocol._check_protocol).

### Alt-Svc: a route for the whole origin, with a lifetime

An alternative service is the triple (ALPN name, host, port), it applies to a whole origin, and it changes routing but never the origin: `Host`, SNI, and certificate checks stay those of the origin (RFC 7838 Section 2).
The grammar, as a simplified excerpt of RFC 7838 Section 3 with the comments condensed:

```text
Alt-Svc       = clear / 1#alt-value                  (RFC 7838 Section 3)
alt-value     = alternative *( OWS ";" OWS parameter )
alternative   = protocol-id "=" alt-authority
protocol-id   = token        ; percent-encoded ALPN name, uppercase hex, canonical
alt-authority = quoted-string ; [ uri-host ] ":" port
```

The rules the lab parser enforces, each with its source:

- Values are in server preference order, and an empty host means the origin's host (RFC 7838 Section 3).
- `protocol-id` has exactly one encoding: token characters other than `%` MUST NOT be percent-encoded and hex MUST be uppercase, so `w=x:y#z` is `w%3Dx%3Ay#z` and `x%y` is `x%25y` (RFC 7838 Section 3).
- `ma` is `delta-seconds`, default 86400; it counts from generation, so `Age: 30` with `ma=60` leaves 30 seconds (RFC 7838 Section 3.1); values above the representable range become 2147483648 (RFC 9111 Section 1.2.2).
- `persist=1` is the only meaningful `persist` value; other values MUST be ignored (RFC 7838 Section 3.1), and unknown parameters MUST be ignored (RFC 7838 Section 3).
- `clear` is case-sensitive and wins even when mixed with alternatives in an invalid reply (RFC 7838 Section 3).
- Empty list elements are accepted and ignored, up to a bound (RFC 9110 Section 5.6.1.2); the lab allows 8, a lab policy.
- RFC 7838 does not say what to do with a value that fails to parse; the lab ignores the whole field and keeps the previous cache, which is a lab policy.

Caching rules (RFC 7838 Sections 2.2, 3.1, and 6):

- A new `Alt-Svc` value for the origin replaces every cached alternative; RFC 7838 gives a response without the field no effect, so the lab keeps the cache.
- On a detected network change, the client SHOULD drop alternatives without `persist=1`.
- Existing connections to an alternative may outlive its freshness; `ma` limits only new connections.
- An `Alt-Svc` field in a `421` response MUST be ignored, and a `421` from an alternative MUST remove that entry.
- When using an alternative, the client SHOULD send `Alt-Used: host[:port]` on every request (RFC 7838 Section 5).

Over HTTP/2 the same value can travel in the `ALTSVC` frame, type `0xa`, with a 16-bit `Origin-Len`, an optional ASCII origin, and the field value (RFC 7838 Section 4).
On stream 0 the origin is required; on any other stream it MUST be empty and the stream's origin applies; servers MUST ignore received frames; the frame is hop-by-hop.

```text
ALTSVC on stream 0 for origin "https://origin.example.test", value h3=":443"; ma=3600 (abbreviated; full bytes in the lab test)
00 00 2f | 0a | 00 | 00 00 00 00 | 00 1b | 68 74 74 70 73 3a 2f 2f ... | 68 33 3d 22 ...
length 47  type flags  stream 0     len 27  origin (27 bytes)            field (18 bytes)
```

h2 4.4.1 implements both forms (h2 4.4.1 h2/connection.py, H2Connection.advertise_alternative_service and H2Connection._receive_alt_svc_frame).
It drops a stream-0 frame with an empty origin and drops frames for unknown streams (h2 4.4.1 h2/connection.py, H2Connection._receive_alt_svc_frame), and refuses an implicit advertisement after response headers (h2 4.4.1 h2/stream.py, H2StreamStateMachine.send_alt_svc).
It does not check that a stream-0 origin is authoritative for the connection, which RFC 7838 Section 4 requires, and for the implicit form it reports the stream's `:authority` rather than a serialized origin (h2 4.4.1 h2/stream.py, H2Stream.receive_alt_svc); the lab pins both.

### HTTPS records: discovery before the first byte

SVCB (DNS type 64) and its HTTP form HTTPS (type 65) carry a 2-octet `SvcPriority`, an uncompressed `TargetName`, and `SvcParams` as `key(2) length(2) value` in strictly increasing key order (RFC 9460 Sections 2.2, 14.1, and 14.2).
Priority 0 is AliasMode, which aliases the name and ignores SvcParams; any other value is ServiceMode, and lower values are preferred (RFC 9460 Section 2.4).
A ServiceMode `TargetName` of `.` means the owner name (RFC 9460 Section 2.5.2).

| Key | Name | Wire value | Source |
| --- | --- | --- | --- |
| 0 | `mandatory` | list of 2-octet keys, strictly increasing, never 0 | RFC 9460 Section 8 |
| 1 | `alpn` | 1-octet-length alpn-ids exactly filling the value | RFC 9460 Section 7.1.1 |
| 2 | `no-default-alpn` | empty; requires `alpn` | RFC 9460 Section 7.1.1 |
| 3 | `port` | 2 octets | RFC 9460 Section 7.2 |
| 4 | `ipv4hint` | non-empty list of 4-octet addresses | RFC 9460 Section 7.3 |
| 5 | `ech` | `ECHConfigList` with its length prefix | RFC 9848 Section 3 |
| 6 | `ipv6hint` | non-empty list of 16-octet addresses | RFC 9460 Section 7.3 |

A client MUST treat out-of-order keys, a truncated parameter, or a badly formatted known value as malformed, and then reject the entire RRset (RFC 9460 Section 2.2).
For HTTPS, the default ALPN set is `http/1.1`, added unless `no-default-alpn` is present, and `port` and `no-default-alpn` are automatically mandatory, so a client that does not implement them must skip the record (RFC 9460 Sections 7.1.1, 8, and 9).
ALPN in DNS selects transports, not the final protocol: the client intersects the SVCB ALPN set with what it supports, derives transports (`h3` implies QUIC, `h2` and `http/1.1` imply TLS over TCP), and for each transport sends its own full ProtocolNameList "without regard to the SVCB ALPN set" (RFC 9460 Section 7.1.2).
That last rule is a security property: a DNS attacker can block a transport but cannot steer ALPN selection.

RFC 9460 Section 9.2 lists the deliberate differences from `Alt-Svc`: no `ma` (the DNS TTL is the lifetime), no `persist` (DNS is an untrusted channel), multiple ALPN IDs per record, and no per-client tailoring.
When both are present, a client that implements both SHOULD make only connection attempts consistent with both (RFC 9460 Section 9.3), and if every alternative endpoint carries `ech`, an ECH-capable client MUST become SVCB-reliant and not fall back (RFC 9848 Sections 5.1 and 6).

## Numbers and limits

| Quantity | Value | Source |
| --- | --- | --- |
| ALPN extension type | 16 | RFC 7301 Section 3.1 |
| `ProtocolName` length | 1 to 255 octets | RFC 7301 Section 3.1 |
| `protocol_name_list` length | 2 to 65535 octets | RFC 7301 Section 3.1 |
| `no_application_protocol` alert | 120, fatal | RFC 7301 Section 3.2 |
| `h2` identifier | `0x68 0x32` | RFC 9113 Section 3.1 |
| HTTP/2 client preface | 24 octets, then `SETTINGS` | RFC 9113 Section 3.4 |
| `Alt-Svc` default freshness | 86400 s (24 hours) | RFC 7838 Section 3.1 |
| `delta-seconds` overflow value | 2147483648 (2^31) | RFC 9111 Section 1.2.2 |
| `ALTSVC` frame type | `0xa`, no flags | RFC 7838 Section 4 |
| `ALTSVC` frame, 27-byte origin, 18-byte value | 56 bytes (9 header + 2 + 27 + 18) | lab `test_h2_altsvc_on_stream_zero_wire_layout` |
| DNS RR types | SVCB 64, HTTPS 65 | RFC 9460 Sections 14.1 and 14.2 |
| SvcParamKey private use / invalid | 65280 to 65534 / 65535 | RFC 9460 Section 14.3.2 |
| DNS label / name length | 63 / 255 octets | RFC 1035 Section 2.3.4; lab `_decode_name` |
| Hypercorn automatic `Alt-Svc` | `h3=":<quic port>"; ma=3600`, only when `alt_svc_headers` is empty | hypercorn 0.18.0 hypercorn/config.py, Config.response_headers; lab socket test |
| ClientHello without ALPN | 1526 bytes | measured, see below |
| ClientHello with `["h2", "http/1.1"]` | 1544 bytes (+18) | measured, see below |
| ClientHello with `["h3"]` | 1535 bytes (+9) | measured, see below |

The three ClientHello sizes were re-measured on 2026-11-08 on an Apple M3 Pro, macOS 27.2 (Darwin 27.2.0 arm64), CPython 3.14.6 with OpenSSL 3.5.7, by reading the first flight of `ssl.create_default_context().wrap_bio` 20 times per offer from a throwaway script run as `uv run --frozen python ../tmp_probe/size.py` from `labs/` (removed after the run); every run gave the same size.
The deltas are exactly the 4-byte extension header plus the `ProtocolNameList`; the absolute size depends on the OpenSSL build's defaults and will differ elsewhere.
Most of the 1526-byte baseline is the `key_share` extension (type 51, 1258 bytes): an `X25519MLKEM768` share (group `0x11ec`, 1216 bytes) plus an `x25519` share (group `0x1d`, 32 bytes), decoded by the same script, with group names from the IANA TLS Supported Groups registry.

## Portability

| Dimension | Behavior | Evidence |
| --- | --- | --- |
| CPython 3.12 | 89 lab tests pass on 3.12.13 with OpenSSL 3.5.7 (the matrix patch 3.12.16 is not available to uv here) | lab, `uv run --python 3.12.13`, macOS 27.2 arm64 |
| CPython 3.13 | 89 pass on 3.13.18 with OpenSSL 3.5.9 | lab, `uv run --python 3.13.18` |
| CPython 3.14 | 89 pass on 3.14.6 (OpenSSL 3.5.7, workspace `.venv`) and 3.14.10 (OpenSSL 4.1.0) | lab, `uv run --frozen pytest` and `uv run --python 3.14.10` |
| CPython 3.15 | 89 pass on 3.15.2 with OpenSSL 4.1.0 | lab, `uv run --python 3.15.2` |
| CPython 3.15t | not run on 3.15.2t, which uv cannot download here (as of 2026-11-08); on 3.15.0b3 free-threaded (GIL off, OpenSSL 3.5.7) 88 of 89 pass in a reduced install of the test's imports; the one failure is the Hypercorn QUIC test, because `aioquic==1.7.2` has no wheel and fails to compile there (`Py_TYPE` undeclared) and the full lock fails on `psycopg-binary==3.3.8` | run of `pytest` with `PYTHONPATH=src` in a throwaway env; this leg is recorded as not run on 3.15.2t |
| Debian Trixie container | 89 pass on `python:3.14-trixie` (Debian 13.7, CPython 3.14.10, glibc 2.41, OpenSSL 3.5.7, linux/arm64 under OrbStack) | `docker run` with `labs/` mounted read-only and the test's pinned imports installed with pip |
| Alpine musl container | 89 pass on `python:3.14-alpine` (Alpine 3.24.4, CPython 3.14.10, musl, OpenSSL 3.5.9, linux/arm64) | same command; the no-overlap `None` result also holds on musl |
| CPython `ssl`, no ALPN overlap | handshake completes with `None` on all five releases above | lab `test_cpython_completes_handshake_without_alpn_when_no_overlap`; CPython 3.14.6 Modules/_ssl.c |
| h2 4.4.1 | sends and receives `ALTSVC`; no authority check on stream-0 origins | lab; h2 4.4.1 h2/connection.py and h2/stream.py |
| httpx 0.28.1, httpcore 1.0.9 | ALPN `["http/1.1", "h2"]` with `http2=True`, else `["http/1.1"]`; no `Alt-Svc` code and no HTTP/3 transport | httpcore 1.0.9 httpcore/_async/connection.py, AsyncHTTPConnection._connect; source search of both installed packages |
| Hypercorn 0.18.0 | ALPN `["h2", "http/1.1"]`; `h2c` Upgrade and prior knowledge; automatic `Alt-Svc` for `quic_bind` unless `alt_svc_headers` is set | lab (TLS context and socket test); hypercorn 0.18.0 hypercorn/config.py and hypercorn/protocol/h11.py |
| Uvicorn 0.54.0 | ALPN is set only when the protocol class defines `alpn_protocols`, which only the `zttp` classes do; `http="h11"`, `"httptools"`, and `"auto"` negotiate nothing (client offer `h2, http/1.1` gives `None`), while `http="zttp", http2=True` selects `h2`; `http2=True` with another protocol raises `ValueError` | uvicorn 0.54.0 uvicorn/config.py, Config.load and create_ssl_context; uvicorn/protocols/http/auto_zttp_impl.py, AutoZttpProtocol; run with `zttp==0.0.35` in a throwaway env, CPython 3.14.6, OpenSSL 3.5.7 |
| Granian 2.8.7 | ALPN `["h2", "http/1.1"]` in `auto` mode, `["h2"]` with `--http 2`, `["http/1.1"]` with `--http 1` (rustls); unlike CPython it sends the fatal `no_application_protocol` alert when there is no overlap; no `Alt-Svc` or HTTP/3 code in the tagged source | granian 2.8.7 src/workers.rs, WorkerConfig::tls_cfg (read at the `v2.8.7` tag); run: `granian --interface asgi --http <mode> --ssl-certificate ... app:app` with `ssl` clients, CPython 3.14.6, macOS 27.2 |
| aioquic 1.7.2 | `H3_ALPN = ["h3"]` | aioquic 1.7.2 aioquic/h3/connection.py |
| HTTP/1.1 | `http/1.1` over TLS; `Upgrade` for cleartext switches | RFC 7301 Section 6, RFC 9110 Section 7.8 |
| HTTP/2 | `h2` over TLS via ALPN only; cleartext by prior knowledge; `h2c` Upgrade deprecated | RFC 9113 Sections 3.1 to 3.3 |
| HTTP/3 over QUIC | `h3` in the QUIC TLS handshake; discovered by `Alt-Svc`, `ALTSVC`, or HTTPS record; TCP fallback SHOULD be attempted when UDP fails | RFC 9114 Sections 3.1, 3.1.1, and 3.2 |

## Worked example

The [implementation](../labs/src/api_labs/f01_http_semantics_and_wire_protocols/protocol_discovery.py) has four parts: ALPN encoding and a real TLS handshake over `ssl.MemoryBIO`, an RFC 7838 parser and cache, an h2 `ALTSVC` harness, and an RFC 9460 RDATA decoder and connection planner.
The [test](../labs/tests/f01_http_semantics_and_wire_protocols/test_alt_svc_and_http_version_negotiation.py) has 89 cases, its boundary tests (255-octet names, 63-octet labels, port 65535, the empty-element bound) each fail when their limit is shifted by one, and it checks the RFC 9460 Appendix D.1 and D.2 vectors (Figures 2 to 4 and 6 to 10) byte for byte and pins the real behavior of CPython `ssl`, h2 4.4.1, and Hypercorn 0.18.0.

Server-side selection, the rule CPython delegates to OpenSSL:

```python
def select_alpn(server_preference: Sequence[bytes], client_offer: Sequence[bytes]) -> bytes | None:
    """Return the server's most preferred protocol that the client offered.

    ``None`` means no overlap, where RFC 7301 Section 3.2 says the server SHALL send a
    fatal ``no_application_protocol`` alert.
    """
    offered = set(client_offer)
    for candidate in server_preference:
        if candidate in offered:
            return candidate
    return None
```

The cache update, where `421` handling, replacement, and the `Age` correction each have a mutation that the tests catch:

```python
    def observe_response(
        self,
        origin: Origin,
        *,
        status: int,
        alt_svc_lines: Sequence[str],
        received_at: float,
        age: int = 0,
    ) -> None:
        """Apply the ``Alt-Svc`` lines of one response received at *received_at*."""
        if status == 421 or not alt_svc_lines:
            return  # RFC 7838 Section 6: Alt-Svc in a 421 MUST be ignored
        try:
            parsed = parse_alt_svc(", ".join(alt_svc_lines))
        except AltSvcParseError:
            return  # lab policy: a malformed field is ignored as a whole
        if isinstance(parsed, AltSvcClear):
            self.entries.pop(origin, None)
            return
        generated_at = received_at - age  # ma counts from generation (Section 3.1)
        self.entries[origin] = tuple(
            CachedAlternative(alt, generated_at + alt.max_age) for alt in parsed
        )
```

Two of the tests: the first is the CPython divergence from RFC 7301, the second is the RFC 9460 Section 7.1.2 example.

```python
def test_cpython_completes_handshake_without_alpn_when_no_overlap(
    origin_cert: tuple[Path, Path],
) -> None:
    # RFC 7301 Section 3.2 says the server SHALL send no_application_protocol (120);
    # CPython's _selectALPN_cb returns SSL_TLSEXT_ERR_NOACK, so the handshake succeeds.
    result = memory_handshake(
        _client_ctx(origin_cert, ["http/1.1"]),
        _server_ctx(origin_cert, ["h2"]),
        ORIGIN_HOST,
    )
    assert result.tls_version == "TLSv1.3"
    assert result.client_alpn is None
    assert result.server_alpn is None
```

```python
def test_plan_follows_rfc_9460_section_7_1_2_example() -> None:
    # SVCB ALPN set ["h3"] + default ["http/1.1"]; client supports h1, h2, h3.
    rrset = [_https_rr(1, b"\x00", b"\x00\x01\x00\x03\x02h3")]
    plan = plan_https_connections("example.com.", 443, rrset, CLIENT_ALPN)
    assert plan.alias_target is None
    assert plan.attempts == (
        ConnectionAttempt(1, "example.com.", 443, "quic", (b"h3",)),
        ConnectionAttempt(1, "example.com.", 443, "tcp", (b"h2", b"http/1.1")),
    )
```

The TCP attempt offers `h2` even though the record only implied `http/1.1` for TCP: the record chooses the transport, and ALPN inside the handshake chooses the protocol.

Output captured on 2026-11-08 from `labs/` on macOS 27.2 (Apple M3 Pro) with CPython 3.14.6 and OpenSSL 3.5.7:

```text
$ uv run --frozen pytest tests/f01_http_semantics_and_wire_protocols/test_alt_svc_and_http_version_negotiation.py -q
........................................................................ [ 80%]
.................                                                        [100%]
89 passed in 0.13s
```

## Pitfalls

> [!warning] A client that assumes it got `h2`
> Trigger: a Python server built on `ssl` with `set_alpn_protocols(["h2"])` meets a client that offers only `http/1.1`, or a gRPC-style client that requires `h2` meets an HTTP/1.1-only server.
> Symptom: no `no_application_protocol` alert; the handshake succeeds, `selected_alpn_protocol()` returns `None`, and the first bytes are interpreted under the wrong framing.
> Fix: treat `None` or an unexpected value as a failed connection, as RFC 7838 Section 2.4 requires for alternatives; Hypercorn 0.18.0 falls back to HTTP/1.1 on anything other than `h2` (hypercorn 0.18.0 hypercorn/protocol/__init__.py, ProtocolWrapper.__init__), and httpcore 1.0.9 uses HTTP/2 when the result equals `"h2"`, or without consulting ALPN when configured with `http1=False, http2=True` (httpcore 1.0.9 httpcore/_async/connection.py, AsyncHTTPConnection.handle_async_request).

> [!warning] ALPN selection memory disclosure (CVE-2024-5535)
> Trigger: calling OpenSSL's `SSL_select_next_proto` with an empty client protocol list.
> Symptom: per NVD (published 2024-06-27), a buffer overread that "could result in up to 255 bytes of arbitrary private data from memory being sent to the peer"; NVD shows a CVSS 3.1 base score of 9.1 from a secondary scorer, while the OpenSSL advisory text that NVD quotes rates it Low severity because ALPN servers normally pass the non-empty client list.
> Fix: upgrade OpenSSL, and never hand-roll selection callbacks that can pass empty lists; the lab's builds were OpenSSL 3.5.7, 3.5.9, and 4.1.0.

> [!warning] Header injection turns into origin hijack
> Trigger: an application that lets users or upstream services set response headers, on a host where other users can listen on other ports.
> Symptom: an injected `Alt-Svc: h2=":8443"; ma=2592000` redirects the whole origin to the attacker's port for 30 days (RFC 7838 Section 9.1).
> Fix: rely on certificate verification for the origin (RFC 7838 Section 2.1, proven by `test_alternative_must_present_a_certificate_for_the_origin`), strip `Alt-Svc` from application responses at the edge, and emit it only from the proxy configuration.

> [!warning] Advertising an alternative you then remove
> Trigger: turning off a QUIC listener or a UDP path while clients still hold `Alt-Svc` entries with long `ma`.
> Symptom: clients keep trying `h3` until expiry and pay a failed QUIC attempt before the TCP fallback that RFC 9114 Section 3.1 says they SHOULD try.
> Fix: lower `ma` well before the change, then send `Alt-Svc: clear` on every response; know your server's default, such as Hypercorn's automatic `ma=3600`.

> [!warning] The `Alt-Svc` cache is persistent security state
> Trigger: a client that writes its alternative-service cache to disk.
> Symptom: curl 7.69.0 to 7.83.1 could widen file permissions when atomically replacing its cookie, `alt-svc`, and HSTS files (CVE-2022-32207, curl vuln.json, fixed in 7.84.0).
> Fix: store the cache with the same protections as cookies, and clear it when origin-specific data is cleared, which RFC 7838 Section 9.4 requires of user agents.

> [!warning] Trusting the `ALTSVC` frame's origin
> Trigger: an HTTP/2 client on h2 4.4.1 that applies every `AlternativeServiceAvailable` event.
> Symptom: a server can advertise alternatives for an origin it is not authoritative for, and h2 delivers the event anyway (`test_h2_4_4_1_does_not_check_that_the_origin_is_authoritative`).
> Fix: accept a stream-0 origin only if the connection's certificate covers it, as RFC 7838 Section 4 requires.

> [!warning] HTTPS records pointing inward
> Trigger: a hostile resolver returns ServiceMode records with private addresses in `ipv4hint` or an internal `port`.
> Symptom: the client connects to an internal service with attacker-influenced bytes such as session tickets (RFC 9460 Section 12).
> Fix: apply the same port and address restrictions as for `https` URIs, which RFC 9460 Section 9 tells clients to apply to `port`.

## Security and robustness

- Threat model: `Alt-Svc` arrives over an authenticated channel but can be injected by anything that sets headers; HTTPS records arrive over DNS, which RFC 9460 Section 9.2.2 models as untrusted; ALPN is negotiated inside the authenticated handshake.
- The single control that holds all three together is that every alternative must authenticate as the origin, with SNI and `Host` naming the origin (RFC 7838 Sections 2.1 and 2.3, RFC 9460 Section 9.4); certificate pinning applies to alternatives too (RFC 7838 Section 9.2).
- Downgrade: an attacker can block UDP or drop HTTPS queries to force TCP or plain connections; RFC 9460 Section 3.1 says clients SHOULD abandon the attempt when protected DNS fails, RFC 9460 Section 7.1.2 keeps ALPN selection out of the attacker's reach, and RFC 9848 Section 5.1 forbids fallback when every endpoint advertises ECH.
- Scheme confusion: an `http://` origin moved to a TLS alternative may make the application believe the request is secure; servers should use `:scheme` or the absolute-form target, not the port (RFC 7838 Section 9.5).
- Tracking: unique alternative hostnames and `persist=1` can follow users across networks (RFC 7838 Section 9.4), which is why `persist` has no SvcParamKey (RFC 9460 Section 9.2.2) and why DNS caches must be partitioned or flushed per network (RFC 9460 Section 12).
- Input validation: the lab rejects lowercase or unnecessary percent-encoding, missing ports, port 0, control characters in quoted strings (including after a backslash, RFC 9110 Section 5.6.4), more than 8 empty list elements, out-of-order or duplicate SvcParamKeys, compression pointers in `TargetName`, and ALPN lists that do not exactly fill their length.
- OWASP API8:2023 Security Misconfiguration covers the common failure here: an edge that forwards application-set `Alt-Svc`, or advertises alternatives that do not hold the origin's certificate.
- Defense in depth: set `Alt-Svc` only at the edge, keep `ma` short until the alternative is proven, log the negotiated ALPN per connection, and alert when `None` appears on listeners that require `h2`.

## The principal's view

The decision is where protocol policy lives: ALPN is per connection and server-owned, `Alt-Svc` is per origin and response-owned, and the HTTPS record is per name and DNS-owned, so the same "turn on HTTP/3" change touches three teams and three caches with three lifetimes (the TCP connection, `ma`, and the DNS TTL).
For an API platform, put all three under the edge team: the CDN or proxy terminates TLS, chooses ALPN, sets `Alt-Svc`, and publishes HTTPS records, while application services speak whatever the edge negotiates with them internally.
Roll out HTTP/3 by publishing `Alt-Svc` with a short `ma` first, because a response header can be withdrawn on the next response, while DNS records live in resolvers you do not control (RFC 9460 Section 9.2.3).
Move to HTTPS records when you need first-connection HTTP/3, apex aliasing, or ECH, and accept that DNS cannot tailor per client (RFC 9460 Section 9.2.4).
For internal service-to-service traffic, skip discovery: configure the protocol explicitly (offer only `h2` in ALPN over TLS, since RFC 9113 Section 3.3 confines prior knowledge to cleartext TCP, or use gRPC), because discovery buys nothing when both ends are yours and adds a fallback path an attacker or misconfiguration can trigger.
In Python specifically, the client side has no `Alt-Svc` or HTTP/3 support in the pinned httpx stack (as of 2026-11), so a Python client gets HTTP/3 only through a different library or a proxy, and that is a valid reason to keep Python services behind an edge that speaks HTTP/3 for them.

## Interview questions

> [!question] L5: Why can't a client negotiate HTTP/3 the same way it negotiates HTTP/2?
>
> > [!success]- Answer
> > HTTP/2 is negotiated with ALPN inside the TLS handshake on the same TCP connection.
> > HTTP/3 runs over QUIC on UDP, a different transport, so a TCP handshake cannot switch to it.
> > The client must learn about it first, from `Alt-Svc`, an `ALTSVC` frame, or an HTTPS DNS record (RFC 9114 Section 3.1.1, RFC 9460 Section 7.1.2).
> >
> > Follow-up: What should the client do if the QUIC attempt fails?

> [!question] L5: Who chooses the protocol in ALPN, and what happens when there is no overlap?
>
> > [!success]- Answer
> > The server chooses its own most preferred protocol among those the client offered (RFC 7301 Section 3.2).
> > The client's order is only a hint; the lab shows a client offering `["http/1.1", "h2"]` gets `h2` from a server preferring `h2`.
> > With no overlap the RFC says the server SHALL send a fatal `no_application_protocol` alert, but CPython's server returns `NOACK` and completes the handshake with no protocol.
> >
> > Follow-up: How should a client treat a `None` result?

> [!question] L6: Predict the output: what does the lab's `parse_alt_svc` return for `h2=":443", clear`, for `h3=":443"; ma=60; persist=yes`, and for `h%32=":443"`?
>
> > [!success]- Answer
> > The first returns `AltSvcClear()`: `clear` invalidates everything even when mixed with alternatives (RFC 7838 Section 3).
> > The second returns one `AltService(b"h3", "", 443, max_age=60, persist=False)`: `persist` values other than `1` MUST be ignored.
> > The third raises `AltSvcParseError`: `2` is a token character and MUST NOT be percent-encoded, so `h%32` is not the canonical spelling of `h2`.
> >
> > Follow-up: Why does RFC 7838 insist on one canonical encoding?

> [!question] L6: A response arrives with `Age: 30` and `Alt-Svc: h2=":8000"; ma=60`; for how long may the client start new connections to the alternative, and what about connections already open?
>
> > [!success]- Answer
> > About 30 seconds from receipt, minus transit time, because `ma` counts from generation (RFC 7838 Section 3.1).
> > The response's `Cache-Control` lifetime does not affect it.
> > Connections already open to the alternative may continue after expiry; `ma` limits only new connections (RFC 7838 Section 2.2).
> >
> > Follow-up: Which test in the lab fails if the `Age` correction is removed?

> [!question] L6: Debug this API: after enabling HTTP/3 on the CDN, a small fraction of mobile clients see multi-second stalls on the first request after switching networks; what is happening and how do you confirm it?
>
> > [!success]- Answer
> > The CDN sends `Alt-Svc: h3=":443"; ma=86400; persist=1`, so clients keep the `h3` route across network changes (RFC 7838 Section 3.1).
> > On networks that block UDP, the client tries QUIC first and waits for it to fail before falling back to TCP (RFC 9114 Section 3.1).
> > Confirm by correlating stalls with the absence of a QUIC handshake in edge logs and with the negotiated ALPN of the fallback connection.
> > Fix: drop `persist=1` so the entry is cleared on network change, shorten `ma`, and make sure clients race TCP against QUIC instead of waiting.
> >
> > Follow-up: Why is `persist` absent from HTTPS records?

> [!question] L6: An HTTPS record says `alpn=h3` and nothing else; a client supports HTTP/1.1, HTTP/2, and HTTP/3; which ClientHellos does it send?
>
> > [!success]- Answer
> > The SVCB ALPN set is `h3` plus the default `http/1.1`, because `no-default-alpn` is absent (RFC 9460 Sections 7.1.1 and 9).
> > That implies QUIC and TLS over TCP, so the client may send a QUIC ClientHello with `["h3"]` and a TCP ClientHello with `["h2", "http/1.1"]`.
> > It includes `h2` because each ProtocolNameList lists all of the client's protocols for that transport, without regard to the SVCB ALPN set (RFC 9460 Section 7.1.2).
> >
> > Follow-up: What would change if the record also had `no-default-alpn`?

> [!question] L7+: Why must an alternative service present a certificate for the origin rather than for its own hostname, and what does that cost operationally?
>
> > [!success]- Answer
> > The alternative is reached through information the attacker may control (a header, a frame, or DNS), so the certificate for the origin is the only proof it is authorized (RFC 7838 Sections 2.1 and 9.2).
> > It also keeps SNI and `Host` on the origin, so routing changes never leak into application semantics.
> > The cost is that every alternative endpoint needs the origin's certificate and key, which pushes key material to every CDN edge or third-party alternative.
> >
> > Follow-up: How does ECH change what an on-path observer learns about the alternative connection?

> [!question] L7+: You run a multi-CDN setup and want first-visit HTTP/3 and ECH; would you publish HTTPS records, `Alt-Svc`, or both, and how do you roll it back?
>
> > [!success]- Answer
> > Both: HTTPS records give first-connection HTTP/3 and carry `ech`, while `Alt-Svc` lets each CDN steer its own clients per response.
> > Keep them consistent, because clients that implement both SHOULD make only attempts consistent with both (RFC 9460 Section 9.3), and publish `ech` for every alt-authority or names leak in clear ClientHellos (RFC 9848 Section 6).
> > Rollback is asymmetric: `Alt-Svc: clear` takes effect on the next response, while DNS records persist for the TTL and sometimes beyond it, so RFC 9460 Section 9.2.3 says to keep the old configuration working until clients have migrated.
> > If every endpoint has `ech`, clients will not fall back to non-ECH connections (RFC 9848 Section 5.1), so a broken ECH key is an outage, not a degradation.
> >
> > Follow-up: What monitoring would tell you ECH is failing for some resolvers?

> [!question] L7+: Should a Python API service participate in protocol discovery at all?
>
> > [!success]- Answer
> > Usually not directly: as of 2026-11, httpx 0.28.1 has no `Alt-Svc` handling or HTTP/3 transport, and Uvicorn 0.54.0 needs the separate `zttp` package for HTTP/2.
> > Hypercorn 0.18.0 can serve HTTP/3 and emits `Alt-Svc` automatically, but that hands rollout policy and `ma` to a server default.
> > The better design terminates HTTP/3 at the edge and lets the Python service speak HTTP/1.1 or `h2` behind it, configured explicitly.
> >
> > Follow-up: When would you accept HTTP/3 termination in the Python process?

## Related

Prerequisites: [[HTTP-3-Framing-and-QPACK-Header-Compression-RFC-9114]]

Next: [[Structured-Field-Values-for-HTTP-RFC-9651]]

See also: [[HTTP-Evolution-HTTP1-HTTP2-HTTP3]], [[curl-in-Depth]], [[QUIC-Transport-Streams-and-Connection-Migration-RFC-9000]], [[HTTP-2-Binary-Framing-and-Stream-State-Machine]], [[HTTP-Message-Abstraction-RFC-9110]], [[openssl-s_client-and-TLS-Debugging]], [[DNS-from-the-Shell-dig-and-getent]], [[DNS-and-CDN]], [[Public-Key-Crypto-and-TLS]]

- Lab: `labs/tests/f01_http_semantics_and_wire_protocols/test_alt_svc_and_http_version_negotiation.py`

## Further reading

- [RFC 7301: TLS Application-Layer Protocol Negotiation Extension](https://www.rfc-editor.org/rfc/rfc7301.html) - the extension format and the server-selection and alert rules in Section 3.2.
- [RFC 7838: HTTP Alternative Services](https://www.rfc-editor.org/rfc/rfc7838.html) - the `Alt-Svc` grammar, caching rules, the `ALTSVC` frame, `Alt-Used`, and the security considerations in Section 9.
- [RFC 9460: Service Binding and Parameter Specification via the DNS (SVCB and HTTPS RRs)](https://www.rfc-editor.org/rfc/rfc9460.html) - the RDATA format, the ALPN-to-transport procedure in Section 7.1.2, and the test vectors in Appendix D.
- [RFC 9848: Bootstrapping TLS Encrypted ClientHello with DNS Service Bindings](https://www.rfc-editor.org/rfc/rfc9848.txt) - the `ech` SvcParam and the no-fallback rule.
- [RFC 9849: TLS Encrypted Client Hello](https://www.rfc-editor.org/rfc/rfc9849.txt) - how the ClientHello, including ALPN, is hidden from the network.
- [RFC 9113: HTTP/2](https://www.rfc-editor.org/rfc/rfc9113.html) - Section 3 defines `h2`, deprecates `h2c` Upgrade, and specifies the preface.
- [RFC 9114: HTTP/3](https://www.rfc-editor.org/rfc/rfc9114.html) - Section 3 covers endpoint discovery, `h3`, and connection reuse.
- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html) - `Upgrade` in Section 7.8, misdirected requests in Section 7.4, and list parsing in Section 5.6.1.
- [CPython v3.14.6 Modules/_ssl.c](https://raw.githubusercontent.com/python/cpython/v3.14.6/Modules/_ssl.c) - `do_protocol_selection`, the `NOACK` behavior the lab pins.
- [CVE-2024-5535 in NVD](https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=CVE-2024-5535) - the OpenSSL ALPN selection overread and the selection semantics it documents.
