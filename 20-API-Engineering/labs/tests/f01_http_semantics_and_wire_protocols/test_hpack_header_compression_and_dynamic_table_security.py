"""Unit tests for HPACK (RFC 7541): static and dynamic tables, eviction, and CRIME-style probing."""

from __future__ import annotations

import pytest
from h2.errors import ErrorCodes
from hpack import Decoder, Encoder, HeaderTuple, NeverIndexedHeaderTuple
from hpack.exceptions import (
    HPACKDecodingError,
    InvalidTableIndexError,
    InvalidTableSizeError,
    OversizedHeaderListError,
)
from hpack.hpack import decode_integer, encode_integer
from hpack.huffman import HuffmanEncoder
from hpack.huffman_constants import REQUEST_CODES, REQUEST_CODES_LENGTH

from api_labs.f01_http_semantics_and_wire_protocols.hpack_compression import (
    STATIC_TABLE_LENGTH,
    Kind,
    deliver_block_to_h2_server,
    entry_size,
    h2_request_blocks,
    hpack_bomb_block,
    oracle_signal,
    parse_representations,
    typical_api_request,
)

TOKEN = b"eyJhbGciOiJIUzI1NiJ9." + b"Q" * 120
REQUESTS = [typical_api_request(i, bearer_token=TOKEN) for i in range(80)]
SECRET = b"session=8f14e45fceea167a5a36"

# RFC 7541 Appendix C.3 (requests, no Huffman) and C.5 (responses, 256-octet table).
C3_REQUESTS: list[list[tuple[bytes, bytes]]] = [
    [(b":method", b"GET"), (b":scheme", b"http"), (b":path", b"/")],
    [(b":method", b"GET"), (b":scheme", b"http"), (b":path", b"/")],
    [(b":method", b"GET"), (b":scheme", b"https"), (b":path", b"/index.html")],
]
for _request, _extra in zip(
    C3_REQUESTS,
    [[], [(b"cache-control", b"no-cache")], [(b"custom-key", b"custom-value")]],
    strict=True,
):
    _request += [(b":authority", b"www.example.com"), *_extra]
C3_HEX = [
    "828684410f7777772e6578616d706c652e636f6d",
    "828684be58086e6f2d6361636865",
    "828785bf400a637573746f6d2d6b65790c637573746f6d2d76616c7565",
]
DATE1 = b"Mon, 21 Oct 2013 20:13:21 GMT"
DATE2 = b"Mon, 21 Oct 2013 20:13:22 GMT"
LOCATION = (b"location", b"https://www.example.com")
COOKIE = b"foo=ASDJKHQKBZXOQWEOPIUAXQWEOIU; max-age=3600; version=1"
C5_RESPONSES = [
    [(b":status", b"302"), (b"cache-control", b"private"), (b"date", DATE1), LOCATION],
    [(b":status", b"307"), (b"cache-control", b"private"), (b"date", DATE1), LOCATION],
    [
        (b":status", b"200"),
        (b"cache-control", b"private"),
        (b"date", DATE2),
        LOCATION,
        (b"content-encoding", b"gzip"),
        (b"set-cookie", COOKIE),
    ],
]
C5_HEX = [
    "4803333032580770726976617465611d4d6f6e2c203231204f637420323031332032303a31333a3231"
    "20474d546e1768747470733a2f2f7777772e6578616d706c652e636f6d",
    "4803333037c1c0bf",
    "88c1611d4d6f6e2c203231204f637420323031332032303a31333a323220474d54c05a04677a6970"
    "7738666f6f3d4153444a4b48514b425a584f5157454f50495541585157454f49553b206d61782d61"
    "67653d333630303b2076657273696f6e3d31",
]


def kinds(block: bytes) -> list[Kind]:
    return [r.kind for r in parse_representations(block)]


@pytest.mark.unit
def test_integer_representation_matches_rfc_7541_appendix_c1() -> None:
    assert bytes(encode_integer(10, 5)) == b"\x0a"
    assert bytes(encode_integer(1337, 5)) == b"\x1f\x9a\x0a"
    assert bytes(encode_integer(42, 8)) == b"\x2a"
    assert bytes(encode_integer(30, 5)) == b"\x1e"
    assert bytes(encode_integer(31, 5)) == b"\x1f\x00"
    assert decode_integer(b"\x1f\x9a\x0a", 5) == (1337, 3)


@pytest.mark.unit
def test_parser_classifies_rfc_7541_appendix_c2_examples() -> None:
    c21 = bytes.fromhex("400a637573746f6d2d6b65790d637573746f6d2d686561646572")
    c22 = bytes.fromhex("040c2f73616d706c652f70617468")
    c23 = bytes.fromhex("100870617373776f726406736563726574")
    parsed = [parse_representations(b)[0] for b in (c21, c22, c23, b"\x82")]
    assert [(r.kind, r.index, r.octets) for r in parsed] == [
        (Kind.INCREMENTAL, 0, 26),
        (Kind.WITHOUT_INDEXING, 4, 14),
        (Kind.NEVER_INDEXED, 0, 17),
        (Kind.INDEXED, 2, 1),
    ]
    with pytest.raises(ValueError, match="truncated"):
        parse_representations(c22[:-1])


@pytest.mark.unit
def test_request_sequence_matches_rfc_7541_appendix_c3_byte_for_byte() -> None:
    encoder = Encoder()
    decoder = Decoder()
    sizes: list[int] = []
    for headers, expected in zip(C3_REQUESTS, C3_HEX, strict=True):
        block = encoder.encode(headers, huffman=False)
        assert block.hex() == expected
        assert list(decoder.decode(block, raw=True)) == headers
        sizes.append(sum(entry_size(n, v) for n, v in decoder.header_table.dynamic_entries))
    assert sizes == [57, 110, 164]
    assert list(decoder.header_table.dynamic_entries) == [
        (b"custom-key", b"custom-value"),
        (b"cache-control", b"no-cache"),
        (b":authority", b"www.example.com"),
    ]
    assert Encoder().encode(C3_REQUESTS[0]).hex() == "828684418cf1e3c2e5f23a6ba0ab90f4ff"


@pytest.mark.unit
def test_eviction_matches_rfc_7541_appendix_c5_at_256_octets() -> None:
    encoder = Encoder()
    encoder.header_table_size = 256
    decoder = Decoder()
    tables: list[list[tuple[bytes, bytes]]] = []
    for number, (headers, expected) in enumerate(zip(C5_RESPONSES, C5_HEX, strict=True)):
        block = encoder.encode(headers, huffman=False)
        size_update = b"\x3f\xe1\x01" if number == 0 else b""
        assert block == size_update + bytes.fromhex(expected)
        assert list(decoder.decode(block, raw=True)) == headers
        tables.append(list(decoder.header_table.dynamic_entries))
    assert tables[0][-1] == (b":status", b"302")
    assert tables[1][0] == (b":status", b"307")
    assert (b":status", b"302") not in tables[1]
    assert tables[2] == [(b"set-cookie", COOKIE), (b"content-encoding", b"gzip"), (b"date", DATE2)]
    assert sum(entry_size(n, v) for n, v in tables[2]) == 215


@pytest.mark.unit
def test_entry_larger_than_the_table_empties_it_without_error() -> None:
    encoder = Encoder()
    encoder.header_table_size = 256
    decoder = Decoder()
    decoder.decode(encoder.encode([(b"a", b"1"), (b"b", b"2")], huffman=False))
    assert len(decoder.header_table.dynamic_entries) == 2
    huge = (b"x-huge", b"z" * 300)
    block = encoder.encode([huge], huffman=False)
    assert list(decoder.decode(block, raw=True)) == [huge]
    assert len(decoder.header_table.dynamic_entries) == 0


@pytest.mark.unit
def test_h2_first_request_pays_literals_and_repeats_collapse_to_indexes() -> None:
    blocks = h2_request_blocks(REQUESTS[:3])
    assert [len(b) for b in blocks] == [237, 162, 162]
    first, second = (parse_representations(b) for b in blocks[:2])
    assert [r.kind for r in first].count(Kind.INCREMENTAL) == 5
    stable = [r for r in second if r.kind is Kind.INDEXED and r.index > STATIC_TABLE_LENGTH]
    assert [r.index for r in stable] == [66, 65, 64, 63]
    assert all(r.octets == 1 for r in stable)
    # A block past the 16,384-octet default SETTINGS_MAX_FRAME_SIZE spans HEADERS plus
    # CONTINUATION (RFC 9113 Sections 4.2 and 6.10); the joined fragments decode whole.
    big = [*REQUESTS[0][:4], (b"x-big", b"a" * 40_000)]
    (joined,) = h2_request_blocks([big])
    assert len(joined) > 16_384
    assert Decoder().decode(joined, raw=True) == big


@pytest.mark.unit
def test_h2_never_indexes_authorization_on_every_request() -> None:
    blocks = h2_request_blocks(REQUESTS[:10])
    for block in blocks:
        never = [r for r in parse_representations(block) if r.kind is Kind.NEVER_INDEXED]
        assert [(r.index, r.octets) for r in never] == [(23, 131)]
        assert block[1:].count(b"\x1f\x08") >= 1


@pytest.mark.unit
def test_h2_indexes_custom_secret_headers_and_long_cookies() -> None:
    base = list(REQUESTS[0][:4])
    api_key = [*base, (b"x-api-key", b"sk_live_0123456789")]
    blocks = h2_request_blocks([api_key, api_key])
    assert kinds(blocks[0])[-1] is Kind.INCREMENTAL
    assert kinds(blocks[1])[-1] is Kind.INDEXED
    short = h2_request_blocks([[*base, (b"cookie", b"s" * 19)]])[0]
    long = h2_request_blocks([[*base, (b"cookie", b"s" * 20)]])[0]
    assert kinds(short)[-1] is Kind.NEVER_INDEXED
    assert kinds(long)[-1] is Kind.INCREMENTAL


@pytest.mark.unit
def test_split_outbound_cookies_never_indexes_each_short_crumb() -> None:
    base = list(REQUESTS[0][:4])
    cookie = (b"cookie", b"a=1; b=2; c=" + b"z" * 30)
    block = h2_request_blocks([[*base, cookie]], split_outbound_cookies=True)[0]
    crumbs = parse_representations(block)[len(base) :]
    assert [(r.kind, r.index) for r in crumbs] == [
        (Kind.NEVER_INDEXED, 32),
        (Kind.NEVER_INDEXED, 32),
        (Kind.INCREMENTAL, 32),
    ]
    whole = h2_request_blocks([[*base, cookie]])[0]
    assert kinds(whole)[-1] is Kind.INCREMENTAL
    assert len(parse_representations(whole)) == len(base) + 1


@pytest.mark.unit
def test_unique_request_ids_churn_the_table_and_evict_stable_entries() -> None:
    blocks = h2_request_blocks(REQUESTS)
    relisted = [i for i, b in enumerate(blocks) if i > 0 and kinds(b).count(Kind.INCREMENTAL) > 1]
    assert relisted == [49]
    assert len(blocks[49]) == 229
    assert entry_size(b"x-request-id", REQUESTS[0][-1][1]) == 80


@pytest.mark.unit
def test_peer_table_size_zero_disables_dynamic_indexing() -> None:
    blocks = h2_request_blocks(REQUESTS, peer_header_table_size=0)
    first = parse_representations(blocks[0])
    assert (first[0].kind, first[0].index) == (Kind.SIZE_UPDATE, 0)
    for block in blocks:
        reps = parse_representations(block)
        assert not [r for r in reps if r.kind is Kind.INDEXED and r.index > STATIC_TABLE_LENGTH]
        assert Kind.WITHOUT_INDEXING not in [r.kind for r in reps]
        assert [r.kind for r in reps].count(Kind.INCREMENTAL) == 5
    assert sum(len(b) for b in blocks) == 19023
    assert sum(len(b) for b in h2_request_blocks(REQUESTS)) == 13163


@pytest.mark.unit
def test_hpack_encoder_signals_every_intermediate_table_size() -> None:
    encoder = Encoder()
    encoder.header_table_size = 100
    encoder.header_table_size = 50
    encoder.header_table_size = 200
    block = encoder.encode([(b":method", b"GET")])
    assert block == b"\x3f\x45" + b"\x3f\x13" + b"\x3f\xa9\x01" + b"\x82"
    assert [r.index for r in parse_representations(block)[:3]] == [100, 50, 200]


@pytest.mark.unit
def test_size_update_after_a_field_is_rejected() -> None:
    block = b"\x82\x3f\x01"
    with pytest.raises(HPACKDecodingError, match="not at the start"):
        list(Decoder().decode(block))
    outcome = deliver_block_to_h2_server(block)
    assert outcome.exception == "ProtocolError"
    assert outcome.goaway_error_code == ErrorCodes.PROTOCOL_ERROR


@pytest.mark.unit
def test_size_update_above_the_acknowledged_limit_is_rejected() -> None:
    decoder = Decoder()
    assert decoder.max_allowed_table_size == 4096
    assert list(decoder.decode(b"\x3f\xe1\x1f\x82", raw=True)) == [(b":method", b"GET")]
    with pytest.raises(InvalidTableSizeError):
        list(Decoder().decode(b"\x3f\xe2\x1f\x82", raw=True))


@pytest.mark.unit
def test_invalid_index_is_protocol_error_not_compression_error_in_h2() -> None:
    first_dynamic = bytes([0x80 | (STATIC_TABLE_LENGTH + 1)])
    with pytest.raises(InvalidTableIndexError):
        list(Decoder().decode(first_dynamic))
    outcome = deliver_block_to_h2_server(first_dynamic)
    assert outcome.goaway_error_code == ErrorCodes.PROTOCOL_ERROR
    assert outcome.goaway_error_code != ErrorCodes.COMPRESSION_ERROR


@pytest.mark.unit
def test_hpack_bomb_hits_the_64_kib_header_list_limit_at_the_17th_field() -> None:
    at_limit = hpack_bomb_block(15)
    headers = list(Decoder().decode(at_limit, raw=True))
    assert len(headers) == 16
    assert sum(entry_size(n, v) for n, v in headers) == 65536
    with pytest.raises(ValueError, match="too small"):
        hpack_bomb_block(1, table_size=38)
    over = hpack_bomb_block(16)
    assert len(over) == 4085
    with pytest.raises(OversizedHeaderListError):
        list(Decoder().decode(over, raw=True))
    outcome = deliver_block_to_h2_server(over)
    assert outcome.exception == "DenialOfServiceError"
    assert outcome.goaway_error_code == ErrorCodes.ENHANCE_YOUR_CALM


@pytest.mark.unit
def test_integer_octet_limit_is_six_octets_and_admits_values_above_uint32() -> None:
    largest = bytes([0x1F, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F])
    value, consumed = decode_integer(largest, 5)
    assert (value, consumed) == (31 + 2**35 - 1, 6)
    assert value > 2**32 - 1
    with pytest.raises(HPACKDecodingError, match="too long"):
        decode_integer(bytes([0x1F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01]), 5)


@pytest.mark.unit
def test_compression_oracle_confirms_only_whole_value_guesses() -> None:
    victim = [(b"cookie", SECRET)]
    assert oracle_signal(victim, (b"cookie", SECRET)) == 21
    assert oracle_signal(victim, (b"cookie", SECRET[:-1] + b"7")) == 0
    assert oracle_signal(victim, (b"cookie", SECRET[:-1])) == 0


@pytest.mark.unit
def test_never_indexed_victim_closes_the_oracle() -> None:
    victim = [NeverIndexedHeaderTuple(b"cookie", SECRET)]
    assert oracle_signal(victim, (b"cookie", SECRET)) == 0


@pytest.mark.unit
def test_never_indexed_survives_h2_decode_and_proxy_reencode() -> None:
    request = [*REQUESTS[0][:4], NeverIndexedHeaderTuple(b"x-api-key", b"sk_live_0123456789")]
    inbound = h2_request_blocks([request])[0]
    assert kinds(inbound)[-1] is Kind.NEVER_INDEXED
    received = deliver_block_to_h2_server(inbound).headers
    assert isinstance(received[-1], NeverIndexedHeaderTuple)
    assert type(received[0]) is HeaderTuple
    upstream = h2_request_blocks([list(received)])[0]
    assert kinds(upstream)[-1] is Kind.NEVER_INDEXED


@pytest.mark.unit
def test_hpack_emits_an_index_for_a_sensitive_value_already_in_the_table() -> None:
    base = list(REQUESTS[0][:4])
    plain = [*base, (b"x-api-key", b"sk_live_0123456789")]
    sensitive = [*base, NeverIndexedHeaderTuple(b"x-api-key", b"sk_live_0123456789")]
    blocks = h2_request_blocks([plain, sensitive])
    assert parse_representations(blocks[1])[-1].kind is Kind.INDEXED
    fresh = h2_request_blocks([sensitive])[0]
    assert parse_representations(fresh)[-1].kind is Kind.NEVER_INDEXED


@pytest.mark.unit
def test_static_huffman_code_can_expand_a_literal() -> None:
    coder = HuffmanEncoder(REQUEST_CODES, REQUEST_CODES_LENGTH)
    assert len(coder.encode(b"~" * 10)) == 17
    assert len(coder.encode(b"application/json")) == 11
