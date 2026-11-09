"""Unit tests for HTTP content codings, negotiation, bomb guards, and BREACH."""

from __future__ import annotations

import gzip
import zlib

import h2.config
import h2.connection
import h2.events
import httpx
import pytest

from api_labs.f01_http_semantics_and_wire_protocols.compression import (
    CODING_MAGIC,
    HAS_STDLIB_ZSTD,
    ZSTD_MAX_WINDOW_BYTES,
    ZSTD_MAX_WINDOW_LOG,
    DecompressionLimitExceeded,
    GzipStreamCompressor,
    TruncatedStreamError,
    breach_distinguisher,
    build_demo_app,
    compress,
    decompress,
    decompress_bounded,
    demo_json_payload,
    detect_coding,
    mask_secret,
    measure_codecs,
    negotiate_content_coding,
    reflected_page,
    server_codings,
)

# zstd via the stdlib compression.zstd binding exists only on CPython 3.14+
# (PEP 784); on 3.12 and 3.13 the other codings still work, so parametrize over
# what this interpreter can actually produce rather than skipping.
CODINGS = ("identity", "gzip", "br", *(("zstd",) if HAS_STDLIB_ZSTD else ()))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("accept_encoding", "available", "expected"),
    [
        # Rule 1: no field present -> server takes its most-preferred coding.
        (None, ["br", "gzip"], "br"),
        # Listed codings with no weight default to q=1; server order breaks ties.
        ("gzip, br", ["zstd", "br", "gzip"], "br"),
        # Explicit weights win over server order.
        ("gzip;q=0.5, br;q=1.0", ["gzip", "br"], "br"),
        # Empty field value means "no content coding".
        ("", ["gzip"], "identity"),
        ("   ", ["gzip"], "identity"),
        # "*" matches any coding not explicitly listed.
        ("*", ["zstd"], "zstd"),
        # q=0 marks a coding unacceptable; fall back to identity.
        ("br;q=0", ["br"], "identity"),
        # identity and "*" both forbidden -> nothing acceptable (caller sends 406).
        ("identity;q=0, *;q=0", ["gzip"], None),
        # Rule 2: "*;q=0" alone also excludes identity (no more specific entry).
        ("*;q=0", ["gzip"], None),
        ("gzip;q=1.0, identity;q=0.5, *;q=0", ["br"], "identity"),
        # The "q" parameter name is case-insensitive (RFC 9110 Section 12.4.2).
        ("gzip;Q=0.5", ["gzip"], "gzip"),
        ("br;Q=0, gzip;q=0.2", ["br", "gzip"], "gzip"),
        # A qvalue above 1 is clamped to 1 (RFC 9110 Section 12.4.2), so it ties and
        # server order decides instead of the out-of-range weight winning.
        ("gzip;q=5, br", ["br", "gzip"], "br"),
    ],
)
def test_negotiate_content_coding_rfc9110(
    accept_encoding: str | None,
    available: list[str],
    expected: str | None,
) -> None:
    assert negotiate_content_coding(accept_encoding, available) == expected


@pytest.mark.unit
def test_default_levels_match_the_levels_the_note_reports() -> None:
    payload = demo_json_payload()
    assert compress("gzip", payload) == compress("gzip", payload, level=9)
    assert compress("br", payload) == compress("br", payload, level=11)
    assert compress("gzip", payload, level=1) != compress("gzip", payload)
    if HAS_STDLIB_ZSTD:
        assert compress("zstd", payload) == compress("zstd", payload, level=3)
        assert compress("zstd", payload, level=19) != compress("zstd", payload)


@pytest.mark.unit
def test_unsupported_coding_is_rejected_everywhere() -> None:
    with pytest.raises(ValueError, match="unsupported content coding"):
        compress("deflate", b"x")
    with pytest.raises(ValueError, match="unsupported content coding"):
        decompress("deflate", b"x")
    with pytest.raises(ValueError, match="unsupported content coding"):
        decompress_bounded("deflate", b"x", max_output=10)


@pytest.mark.unit
def test_negative_output_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        decompress_bounded("identity", b"", max_output=-1)


@pytest.mark.unit
@pytest.mark.parametrize("coding", CODINGS)
def test_codec_roundtrip(coding: str) -> None:
    payload = b"API payload " * 500
    blob = compress(coding, payload)
    assert decompress(coding, blob) == payload
    if coding != "identity":
        assert len(blob) < len(payload)


@pytest.mark.unit
def test_gzip_framing_is_deterministic() -> None:
    # RFC 1952 Section 2.3.1: ID1=0x1f, ID2=0x8b, CM=0x08, and MTIME must be
    # pinned to 0 for a byte-identical representation (and a stable strong ETag).
    blob = compress("gzip", b"hello world")
    assert blob[:3] == b"\x1f\x8b\x08"
    assert blob[4:8] == b"\x00\x00\x00\x00"  # MTIME == 0
    assert compress("gzip", b"hello world") == blob


@pytest.mark.unit
def test_zstd_codec_is_gated_on_stdlib_binding() -> None:
    if HAS_STDLIB_ZSTD:
        # RFC 8878 Section 3.1.1: Zstandard frame magic 0xFD2FB528, little-endian.
        assert compress("zstd", b"x")[:4] == b"\x28\xb5\x2f\xfd"
        assert decompress("zstd", compress("zstd", b"")) == b""
        assert detect_coding(compress("zstd", b"body")) == "zstd"
    else:
        # On CPython < 3.14 the codec refuses rather than silently misbehaving.
        with pytest.raises(RuntimeError):
            compress("zstd", b"x")


@pytest.mark.unit
def test_detect_coding_from_magic_and_brotli_has_none() -> None:
    assert detect_coding(compress("gzip", b"body")) == "gzip"
    # RFC 7932 Section 9.1: a brotli stream carries no magic number, so a
    # receiver cannot sniff it; Content-Encoding is mandatory metadata.
    assert detect_coding(compress("br", b"body")) is None
    assert "br" not in CODING_MAGIC


@pytest.mark.unit
def test_stream_compressor_roundtrips_and_flushes_midstream() -> None:
    sc = GzipStreamCompressor()
    first = sc.compress(b"chunk-a ")
    # Z_SYNC_FLUSH makes the first chunk decodable before finish(), so a client
    # receives data live rather than waiting for the whole stream to end.
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    assert decoder.decompress(first) == b"chunk-a "
    wire = first + sc.compress(b"chunk-b ") + sc.finish()
    assert gzip.decompress(wire) == b"chunk-a chunk-b "


@pytest.mark.unit
@pytest.mark.parametrize("coding", CODINGS)
def test_decompress_bounded_blocks_bombs(coding: str) -> None:
    # A small, highly compressible input decodes to 1 MB: the guard must refuse
    # it (CVE-2025-66471 urllib3, CVE-2025-69223 aiohttp), yet still decode a
    # payload within budget.
    bomb = compress(coding, b"\x00" * 1_000_000)
    with pytest.raises(DecompressionLimitExceeded):
        decompress_bounded(coding, bomb, max_output=4096)
    small = compress(coding, b"y" * 256)
    assert decompress_bounded(coding, small, max_output=4096) == b"y" * 256


@pytest.mark.unit
@pytest.mark.parametrize("coding", CODINGS)
def test_decompress_bounded_allows_exact_budget(coding: str) -> None:
    blob = compress(coding, b"z" * 4096)
    assert decompress_bounded(coding, blob, max_output=4096) == b"z" * 4096
    with pytest.raises(DecompressionLimitExceeded):
        decompress_bounded(coding, blob, max_output=4095)


@pytest.mark.unit
@pytest.mark.parametrize("coding", [c for c in CODINGS if c != "identity"])
def test_decompress_bounded_rejects_truncated_body(coding: str) -> None:
    # A body cut short (dropped connection, hostile peer) must fail loudly, as the
    # unbounded decoders do, rather than return a silently partial representation.
    blob = compress(coding, bytes(range(256)) * 40)
    with pytest.raises(TruncatedStreamError):
        decompress_bounded(coding, blob[: len(blob) // 2], max_output=1 << 20)


@pytest.mark.unit
@pytest.mark.parametrize("coding", [c for c in CODINGS if c in ("gzip", "zstd")])
def test_decompress_bounded_decodes_every_member_and_caps_the_total(coding: str) -> None:
    # gzip members (RFC 1952 Section 2.2) and zstd frames (RFC 8878 Section 3.1)
    # concatenate; the budget covers the sum, so a bomb split across members fails.
    two = compress(coding, b"first ") + compress(coding, b"second")
    assert decompress_bounded(coding, two, max_output=100) == decompress(coding, two)
    halves = compress(coding, b"\x00" * 3000) + compress(coding, b"\x00" * 3000)
    with pytest.raises(DecompressionLimitExceeded):
        decompress_bounded(coding, halves, max_output=4096)


@pytest.mark.unit
def test_decompress_bounded_enforces_rfc9659_zstd_window() -> None:
    assert ZSTD_MAX_WINDOW_BYTES == 8 * 1024 * 1024
    if not HAS_STDLIB_ZSTD:
        # On CPython < 3.14 (no PEP 784 binding) the zstd path refuses outright.
        with pytest.raises(RuntimeError):
            decompress_bounded("zstd", b"", max_output=1)
        return
    from compression.zstd import CompressionParameter, ZstdCompressor, ZstdError

    def framed(window_log: int) -> bytes:
        # Streaming without a pledged size keeps the window in the frame header.
        encoder = ZstdCompressor(options={CompressionParameter.window_log: window_log})
        return encoder.compress(b"w" * 1000) + encoder.flush()

    assert decompress_bounded("zstd", framed(ZSTD_MAX_WINDOW_LOG), max_output=4096) == b"w" * 1000
    # A 16 MB window violates RFC 9659 Section 3 even though the plain decoder accepts it.
    oversized = framed(ZSTD_MAX_WINDOW_LOG + 1)
    assert decompress("zstd", oversized) == b"w" * 1000
    with pytest.raises(ZstdError, match="too much memory"):
        decompress_bounded("zstd", oversized, max_output=4096)


@pytest.mark.unit
def test_empty_body_encoded_sizes() -> None:
    # Fixed framing overhead: gzip 10-byte header + empty block + 8-byte trailer.
    assert len(compress("gzip", b"")) == 20
    assert len(compress("br", b"")) == 1
    if HAS_STDLIB_ZSTD:
        assert len(compress("zstd", b"")) == 9


@pytest.mark.unit
def test_measure_codecs_round_trips_and_orders_sizes() -> None:
    payload = demo_json_payload()
    sizes = {(m.coding, m.level): m.size for m in measure_codecs(payload, repeats=1)}
    assert sizes[("br", 11)] < sizes[("gzip", 9)] < len(payload)
    if HAS_STDLIB_ZSTD:
        assert sizes[("zstd", 19)] < sizes[("gzip", 9)]


@pytest.mark.unit
def test_demo_json_payload_is_the_139790_byte_body_the_note_measures() -> None:
    assert len(demo_json_payload()) == 139_790
    assert demo_json_payload() == demo_json_payload()
    assert len(demo_json_payload(rows=10)) < 1000


@pytest.mark.unit
def test_measure_codecs_validates_repeats_and_reports_each_setting() -> None:
    with pytest.raises(ValueError, match="repeats"):
        measure_codecs(b"x", repeats=0)
    results = measure_codecs(demo_json_payload(rows=50), repeats=2)
    assert [(m.coding, m.level) for m in results] == [
        ("gzip", 9),
        ("br", 11),
        *((("zstd", 3), ("zstd", 19)) if HAS_STDLIB_ZSTD else ()),
    ]
    assert all(m.size > 0 and m.best_seconds > 0 for m in results)


@pytest.mark.unit
def test_measure_codecs_refuses_a_codec_that_does_not_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api_labs.f01_http_semantics_and_wire_protocols.compression as module

    def corrupt(coding: str, data: bytes) -> bytes:
        return b"corrupt"

    monkeypatch.setattr(module, "decompress", corrupt)
    with pytest.raises(AssertionError, match="did not round-trip"):
        module.measure_codecs(b"payload " * 50, repeats=1)


@pytest.mark.unit
def test_breach_distinguisher_rejects_empty_candidates() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        breach_distinguisher("7f3a9c2e", [])


@pytest.mark.unit
def test_mask_secret_rejects_a_short_pad() -> None:
    with pytest.raises(ValueError, match="at least as long"):
        mask_secret(b"0123456789", b"short")


@pytest.mark.unit
def test_breach_oracle_leaks_true_prefix_but_not_without_compression() -> None:
    token = "7f3a9c2e"  # noqa: S105 - a reflected CSRF token, not a credential
    alphabet = list("0123456789abcdef")
    # The compression length oracle ranks the token's true leading byte first,
    # because that guess back-references the reflected token (BREACH, 2013).
    assert breach_distinguisher(token, alphabet) == token[0]
    # Control: an oracle that does not compress carries no signal, so the true
    # byte no longer wins. This proves compression is the leak, not reflection.
    plain = breach_distinguisher(token, alphabet, oracle=len)
    assert plain != token[0]


@pytest.mark.unit
def test_masking_changes_reflection_each_request_and_is_reversible() -> None:
    secret = bytes.fromhex("7f3a9c2e5b81d064")
    pad_a = bytes.fromhex("00112233445566778899aabbccddeeff")
    pad_b = bytes.fromhex("ffeeddccbbaa99887766554433221100")
    masked_a = mask_secret(secret, pad_a)
    masked_b = mask_secret(secret, pad_b)
    # Different pad -> different reflected ciphertext, so the oracle is unstable.
    assert masked_a != masked_b
    # The recipient recovers the secret: secret == pad XOR tail.
    n = len(secret)
    recovered = bytes(p ^ c for p, c in zip(masked_a[:n], masked_a[n:], strict=True))
    assert recovered == secret


@pytest.mark.unit
def test_reflected_page_contains_both_secret_and_input() -> None:
    body = reflected_page("search-term", "SECRET123")
    assert b"SECRET123" in body
    assert b"search-term" in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_gzip_above_minimum_size_sets_vary_and_encoding() -> None:
    app = build_demo_app(minimum_size=500)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/report", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["vary"] == "Accept-Encoding"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_tiny_body_below_minimum_is_not_compressed() -> None:
    app = build_demo_app(minimum_size=500)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/tiny", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.text == "ok"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_identity_request_skips_compression() -> None:
    app = build_demo_app(minimum_size=500)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/report", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in resp.headers


@pytest.mark.unit
def test_server_codings_advertises_zstd_only_when_available() -> None:
    codings = server_codings()
    assert codings[-1] == "gzip"
    assert ("zstd" in codings) is HAS_STDLIB_ZSTD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_download_returns_406_when_all_codings_refused() -> None:
    app = build_demo_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/download", headers={"Accept-Encoding": "identity;q=0, *;q=0"})
    # RFC 9110 Section 15.5.7: a failed proactive negotiation is 406, and the
    # content lists what is available.  415 plus Accept-Encoding is reserved for
    # request content in an unsupported coding (RFC 9110 Section 15.5.16).
    assert resp.status_code == 406
    assert "accept-encoding" not in resp.headers
    assert resp.headers["vary"] == "Accept-Encoding"
    for coding in (*server_codings(), "identity"):
        assert coding in resp.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_download_negotiates_best_available_coding() -> None:
    app = build_demo_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/download", headers={"Accept-Encoding": "br, gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "br"
    assert resp.headers["vary"] == "Accept-Encoding"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_gzip_middleware_ignores_q_zero() -> None:
    # starlette 1.10.0 GZipMiddleware.__call__ tests `"gzip" in Accept-Encoding`, a
    # substring match, so it compresses even when the client marked gzip q=0, which
    # RFC 9110 Section 12.5.3 Rule 3 calls "not acceptable".  Our negotiator does not.
    assert negotiate_content_coding("gzip;q=0", ["gzip"]) == "identity"
    app = build_demo_app(minimum_size=500)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/report", headers={"Accept-Encoding": "gzip;q=0"})
    assert resp.headers["content-encoding"] == "gzip"


def _h2_pair() -> tuple[h2.connection.H2Connection, h2.connection.H2Connection]:
    """Return a client and server H2Connection with the preface exchange done in memory."""
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
    server = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
    client.initiate_connection()
    server.initiate_connection()
    server.receive_data(client.data_to_send())
    client.receive_data(server.data_to_send())
    server.receive_data(client.data_to_send())
    client.send_headers(
        1,
        [
            (":method", "GET"),
            (":path", "/"),
            (":scheme", "https"),
            (":authority", "example.test"),
            ("accept-encoding", "br, gzip"),
        ],
        end_stream=True,
    )
    server.receive_data(client.data_to_send())
    return client, server


@pytest.mark.unit
def test_http2_carries_content_encoding_and_strips_transfer_encoding() -> None:
    # Content-Encoding is message semantics (RFC 9110 Section 8.4), so it crosses HTTP/2
    # framing unchanged; Transfer-Encoding is connection-specific and cannot appear in
    # HTTP/2 (RFC 9113 Section 8.2.2), which h2 enforces by dropping it before encoding.
    client, server = _h2_pair()
    body = compress("gzip", b"A" * 4096)
    server.send_headers(
        1,
        [
            (":status", "200"),
            ("content-encoding", "gzip"),
            ("transfer-encoding", "chunked"),
            ("vary", "accept-encoding"),
        ],
    )
    server.send_data(1, body, end_stream=True)
    events = client.receive_data(server.data_to_send())
    received = next(e for e in events if isinstance(e, h2.events.ResponseReceived))
    assert received.headers is not None
    names = {name: value for name, value in received.headers}
    assert names[b"content-encoding"] == b"gzip"
    assert b"transfer-encoding" not in names
    data = b"".join(e.data for e in events if isinstance(e, h2.events.DataReceived))
    assert decompress("gzip", data) == b"A" * 4096
