"""TLS 1.3 key schedule against RFC 8448, live OpenSSL hello bytes, and ALPN selection."""

from __future__ import annotations

import ssl
import sys
from typing import TYPE_CHECKING

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from hypercorn.config import Config as HypercornConfig
from uvicorn.config import Config as UvicornConfig

from api_labs.f02_tls_connections_and_intermediaries.tls13_handshake import (
    APPLICATION_DATA,
    CHANGE_CIPHER_SPEC,
    DOWNGRADE_TLS11,
    DOWNGRADE_TLS12,
    HANDSHAKE,
    SUITES,
    TLS12,
    TLS13,
    HandshakeTrace,
    NoApplicationProtocolError,
    derive_application_secrets,
    derive_handshake_secrets,
    finished_verify_data,
    hkdf_label,
    loopback_handshake,
    open_record,
    parse_alpn,
    parse_client_hello,
    parse_extensions,
    parse_server_hello,
    record_nonce,
    run_memory_handshake,
    seal_record,
    select_alpn,
    split_handshake,
    split_records,
    traffic_keys,
    transcript_hash,
    write_self_signed_identity,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.types import Receive, Scope, Send

# RFC 8448 Section 3 "Simple 1-RTT Handshake" (TLS_AES_128_GCM_SHA256, x25519).
CLIENT_PRIVATE_KEY = bytes.fromhex(
    "49af42ba7f7994852d713ef2784bcbcaa7911de26adc5642cb634540e7ea5005"
)
CLIENT_HELLO = bytes.fromhex(
    "010000c00303cb34ecb1e78163ba1c38c6dacb196a6dffa21a8d9912ec18a2ef6283024dece70000"
    "06130113031302010000910000000b0009000006736572766572ff01000100000a00140012001d00"
    "17001800190100010101020103010400230000003300260024001d002099381de560e4bd43d23d8e"
    "435a7dbafeb3c06e51c13cae4d5413691e529aaf2c002b0003020304000d0020001e040305030603"
    "020308040805080604010501060102010402050206020202002d00020101001c00024001"
)
SERVER_HELLO = bytes.fromhex(
    "020000560303a6af06a4121860dc5e6e60249cd34c95930c8ac5cb1434dac155772ed3e269280013"
    "0100002e00330024001d0020c9828876112095fe66762bdbf7c672e156d6cc253b833df1dd69b1b0"
    "4e751f0f002b00020304"
)
EARLY_SECRET = bytes.fromhex("33ad0a1c607ec03b09e6cd9893680ce210adf300aa1f2660e1b22e10f170f92a")
ECDHE_SHARED_SECRET = bytes.fromhex(
    "8bd4054fb55b9d63fdfbacf9f04b9f0d35e6d63f537563efd46272900f89492d"
)
HANDSHAKE_SECRET = bytes.fromhex("1dc826e93606aa6fdc0aadc12f741b01046aa6b99f691ed221a9f0ca043fbeac")
C_HS_TRAFFIC_INFO = bytes.fromhex(
    "002012746c7331332063206873207472616666696320860c06edc07858ee8e78f0e7428c58edd6b4"
    "3f2ca3e6e95f02ed063cf0e1cad8"
)
C_HS_TRAFFIC = bytes.fromhex("b3eddb126e067f35a780b3abf45e2d8f3b1a950738f52e9600746a0e27a55a21")
S_HS_TRAFFIC = bytes.fromhex("b67b7d690cc16c4e75e54213cb2d37b4e9c912bcded9105d42befd59d391ad38")
MAIN_SECRET = bytes.fromhex("18df06843d13a08bf2a449844c5f8a478001bc4d4c627984d5a41da8d0402919")
S_HS_KEY = bytes.fromhex("3fce516009c21727d0f2e4e86ee403bc")
S_HS_IV = bytes.fromhex("5d313eb2671276ee13000b30")
S_FINISHED_VERIFY_DATA = bytes.fromhex(
    "9b9b141d906337fbd2cbdce71df4deda4ab42c309572cb7fffee5454b78f0718"
)
S_HANDSHAKE_RECORD = bytes.fromhex(
    "17030302a2d1ff334a56f5bff6594a07cc87b580233f500f45e489e7f33af35edf7869fcf40aa40a"
    "a2b8ea73f848a7ca07612ef9f945cb960b4068905123ea78b111b429ba9191cd05d2a389280f5261"
    "34aadc7fc78c4b729df828b5ecf7b13bd9aefb0e57f271585b8ea9bb355c7c79020716cfb9b1183e"
    "f3ab20e37d57a6b9d7477609aee6e122a4cf51427325250c7d0e509289444c9b3a648f1d71035d2e"
    "d65b0e3cdd0cbae8bf2d0b227812cbb360987255cc744110c453baa4fcd610928d809810e4b7ed1a"
    "8fd991f06aa6248204797e36a6a73b70a2559c09ead686945ba246ab66e5edd8044b4c6de3fcf2a8"
    "9441ac66272fd8fb330ef8190579b3684596c960bd596eea520a56a8d650f563aad27409960dca63"
    "d3e688611ea5e22f4415cf9538d51a200c27034272968a264ed6540c84838d89f72c24461aad6d26"
    "f59ecaba9acbbb317b66d902f4f292a36ac1b639c637ce343117b659622245317b49eeda0c6258f1"
    "00d7d961ffb138647e92ea330faeea6dfa31c7a84dc3bd7e1b7a6c7178af36879018e3f252107f24"
    "3d243dc7339d5684c8b0378bf30244da8c87c843f5e56eb4c5e8280a2b48052cf93b16499a66db7c"
    "ca71e4599426f7d461e66f99882bd89fc50800becca62d6c74116dbd2972fda1fa80f85df881edbe"
    "5a37668936b335583b599186dc5c6918a396fa48a181d6b6fa4f9d62d513afbb992f2b992f67f8af"
    "e67f76913fa388cb5630c8ca01e0c65d11c66a1e2ac4c85977b7c7a6999bbf10dc35ae69f5515614"
    "636c0b9b68c19ed2e31c0b3b66763038ebba42f3b38edc0399f3a9f23faa63978c317fc9fa66a73f"
    "60f0504de93b5b845e275592c12335ee340bbc4fddd502784016e4b3be7ef04dda49f4b440a30cb5"
    "d2af939828fd4ae3794e44f94df5a631ede42c1719bfdabf0253fe5175be898e750edc53370d2b"
)
C_AP_TRAFFIC = bytes.fromhex("9e40646ce79a7f9dc05af8889bce6552875afa0b06df0087f792ebb7c17504a5")
S_AP_TRAFFIC = bytes.fromhex("a11af9f05531f856ad47116b45a950328204b4f44bfb6b3a4b4f1f3fcb631643")
S_AP_KEY = bytes.fromhex("9f02283b6c9c07efc26bb9f2ac92e356")
S_AP_IV = bytes.fromhex("cf782b88dd83549aadf1e984")
S_TICKET_RECORD = bytes.fromhex(
    "17030300de3a6b8f90414a97d6959c3487680de5134a2b240e6cffac116e95d41d6af8f6b580dcf3"
    "d11d63c758db289a015940252f55713e061dc13e078891a38efbcf5753ad8ef170ad3c7353d16d9d"
    "a773b9ca7f2b9fa1b6c0d4a3d03f75e09c30ba1e62972ac46f75f7b981be63439b2999ce13064615"
    "139891d5e4c5b406f16e3fc181a77ca475840025db2f0a77f81b5ab05b94c01346755f69232c8651"
    "9d86cbeeac87aac347d143f9605d64f650db4d023e70e952ca49fe5137121c74bc2697687e248746"
    "d6df353005f3bce18696129c8153556b3b6c6779b37bf15985684f"
)
S_APP_RECORD = bytes.fromhex(
    "17030300432e937e11ef4ac740e538ad36005fc4a46932fc3225d05f82aa1b36e30efaf97d90e6df"
    "fc602dcb501a59a8fcc49c4bf2e5f0a21c0047c2abf332540dd032e167c2955d"
)

SUITE = SUITES[0x1301]
HOST = "api.test"
HTTPCORE_ALPN = ["http/1.1", "h2"]  # httpcore 1.0.9 AsyncHTTPConnection._connect, http2=True
HYPERCORN_ALPN = ["h2", "http/1.1"]  # hypercorn 0.18.0 Config.alpn_protocols
X25519MLKEM768 = 0x11EC  # RFC 10024 Section 7.1
X25519 = 0x001D
SECP384R1 = 0x0018


@pytest.fixture
def identity(tmp_path: Path) -> tuple[Path, Path]:
    return write_self_signed_identity(tmp_path, HOST)


def client_ctx(cert: Path, alpn: list[str] | None = None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=cert)
    if alpn:
        ctx.set_alpn_protocols(alpn)
    return ctx


def server_ctx(identity: tuple[Path, Path], alpn: list[str] | None = None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*identity)
    if alpn:
        ctx.set_alpn_protocols(alpn)
    return ctx


def hello_messages(trace: HandshakeTrace) -> tuple[bytes, bytes]:
    """First ClientHello and the first server handshake message (ServerHello or HRR)."""
    client_hello = split_handshake(trace.records(0)[0].fragment)[0]
    server_hello = split_handshake(trace.records(1)[0].fragment)[0]
    return client_hello, server_hello


def rfc8448_server_flight() -> list[bytes]:
    hello_hash = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO)
    secrets = derive_handshake_secrets(SUITE, ECDHE_SHARED_SECRET, hello_hash)
    keys = traffic_keys(SUITE, secrets.server_handshake_traffic_secret)
    inner_type, content = open_record(SUITE, keys, 0, S_HANDSHAKE_RECORD)
    assert inner_type == HANDSHAKE
    return split_handshake(content)


# ---------------------------------------------------------------- key schedule (RFC 8448)


@pytest.mark.unit
def test_hkdf_label_serialization_matches_rfc8448_info_bytes() -> None:
    hello_hash = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO)
    info = hkdf_label(32, b"c hs traffic", hello_hash)
    assert info == C_HS_TRAFFIC_INFO
    assert info[:3] == b"\x00\x20\x12"  # uint16 length 32, then len("tls13 c hs traffic")
    with pytest.raises(ValueError, match="out of range"):
        hkdf_label(32, b"x" * 250, b"")


@pytest.mark.unit
def test_rfc8448_hellos_parse_to_the_offered_and_selected_parameters() -> None:
    client_hello = parse_client_hello(CLIENT_HELLO)
    server_hello = parse_server_hello(SERVER_HELLO)
    assert client_hello.legacy_version == TLS12
    assert client_hello.session_id == b""  # RFC 8448 does not use middlebox compatibility mode
    assert client_hello.cipher_suites == (0x1301, 0x1303, 0x1302)
    assert client_hello.supported_versions == (TLS13,)
    assert client_hello.key_shares == {X25519: 32}
    assert server_hello.legacy_version == TLS12
    assert server_hello.selected_version == TLS13
    assert server_hello.cipher_suite == 0x1301


@pytest.mark.unit
def test_duplicate_extension_in_one_block_is_rejected() -> None:
    supported_versions = b"\x00\x2b\x00\x02\x03\x04"
    assert parse_extensions(supported_versions) == {43: b"\x03\x04"}
    with pytest.raises(ValueError, match="duplicate extension"):
        parse_extensions(supported_versions * 2)


@pytest.mark.unit
def test_parsers_reject_truncated_input_and_wrong_message_types() -> None:
    keys = traffic_keys(SUITE, S_AP_TRAFFIC)
    with pytest.raises(ValueError, match="truncated record"):
        open_record(SUITE, keys, 1, S_APP_RECORD[:-1])

    def zero_record(length: int) -> bytes:
        header = bytes([APPLICATION_DATA]) + TLS12.to_bytes(2, "big") + length.to_bytes(2, "big")
        return header + bytes(length)

    # TLSCiphertext.length may not exceed 2^14 + 256 (RFC 9846 Section 5.2).
    with pytest.raises(ValueError, match="record_overflow"):
        open_record(SUITE, keys, 0, zero_record(2**14 + 257))
    with pytest.raises(InvalidTag):  # at the limit the length check passes and the AEAD rejects it
        open_record(SUITE, keys, 0, zero_record(2**14 + 256))
    with pytest.raises(ValueError, match="all-zero"):
        open_record(SUITE, keys, 0, seal_record(SUITE, keys, 0, 0, b""))
    with pytest.raises(ValueError, match="truncated record"):
        split_records(S_APP_RECORD[:-1])
    with pytest.raises(ValueError, match="truncated handshake"):
        split_handshake(CLIENT_HELLO[:-1])
    with pytest.raises(ValueError, match="not a ClientHello"):
        parse_client_hello(SERVER_HELLO)
    with pytest.raises(ValueError, match="not a ServerHello"):
        parse_server_hello(CLIENT_HELLO)


@pytest.mark.unit
def test_x25519_shared_secret_from_hello_key_shares_matches_rfc8448() -> None:
    group, server_public = parse_server_hello(SERVER_HELLO).key_share
    assert group == X25519
    client_key = X25519PrivateKey.from_private_bytes(CLIENT_PRIVATE_KEY)
    shared = client_key.exchange(X25519PublicKey.from_public_bytes(server_public))
    assert shared == ECDHE_SHARED_SECRET


@pytest.mark.unit
def test_handshake_key_schedule_matches_rfc8448() -> None:
    hello_hash = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO)
    secrets = derive_handshake_secrets(SUITE, ECDHE_SHARED_SECRET, hello_hash)
    assert secrets.early_secret == EARLY_SECRET  # HKDF-Extract(0, 0) even without a PSK
    assert secrets.handshake_secret == HANDSHAKE_SECRET
    assert secrets.client_handshake_traffic_secret == C_HS_TRAFFIC
    assert secrets.server_handshake_traffic_secret == S_HS_TRAFFIC
    assert secrets.main_secret == MAIN_SECRET
    keys = traffic_keys(SUITE, secrets.server_handshake_traffic_secret)
    assert (keys.key, keys.iv) == (S_HS_KEY, S_HS_IV)


@pytest.mark.unit
def test_decrypting_rfc8448_server_flight_yields_encrypted_extensions_through_finished() -> None:
    messages = rfc8448_server_flight()
    # EncryptedExtensions(8), Certificate(11), CertificateVerify(15), Finished(20)
    assert [m[0] for m in messages] == [8, 11, 15, 20]
    assert [len(m) for m in messages] == [40, 445, 136, 36]


@pytest.mark.unit
def test_server_finished_is_hmac_over_transcript_through_certificate_verify() -> None:
    ee, cert, cert_verify, finished = rfc8448_server_flight()
    full = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO, ee, cert, cert_verify)
    verify_data = finished_verify_data(SUITE, S_HS_TRAFFIC, full)
    assert verify_data == S_FINISHED_VERIFY_DATA == finished[4:]
    short = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO, ee, cert)
    assert finished_verify_data(SUITE, S_HS_TRAFFIC, short) != verify_data


@pytest.mark.unit
def test_application_secrets_use_transcript_through_server_finished() -> None:
    flight = rfc8448_server_flight()
    through_finished = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO, *flight)
    client_ap, server_ap = derive_application_secrets(SUITE, MAIN_SECRET, through_finished)
    assert (client_ap, server_ap) == (C_AP_TRAFFIC, S_AP_TRAFFIC)
    keys = traffic_keys(SUITE, server_ap)
    assert (keys.key, keys.iv) == (S_AP_KEY, S_AP_IV)
    hello_only = transcript_hash(SUITE, CLIENT_HELLO, SERVER_HELLO)
    assert derive_application_secrets(SUITE, MAIN_SECRET, hello_only)[0] != C_AP_TRAFFIC


@pytest.mark.unit
def test_each_record_under_one_key_uses_the_next_sequence_number() -> None:
    keys = traffic_keys(SUITE, S_AP_TRAFFIC)
    ticket_type, ticket = open_record(SUITE, keys, 0, S_TICKET_RECORD)
    assert (ticket_type, ticket[0]) == (HANDSHAKE, 4)  # NewSessionTicket uses sequence 0
    assert open_record(SUITE, keys, 1, S_APP_RECORD) == (APPLICATION_DATA, bytes(range(50)))
    with pytest.raises(InvalidTag):
        open_record(SUITE, keys, 0, S_APP_RECORD)


@pytest.mark.unit
def test_record_nonce_xors_the_left_padded_sequence_number_into_the_iv() -> None:
    assert record_nonce(S_HS_IV, 0) == S_HS_IV
    assert record_nonce(bytes(12), 0x0102) == bytes(10) + b"\x01\x02"
    assert record_nonce(b"\xff" * 12, 1) == b"\xff" * 11 + b"\xfe"


@pytest.mark.unit
def test_record_header_is_authenticated_as_additional_data() -> None:
    keys = traffic_keys(SUITE, S_AP_TRAFFIC)
    downgraded_header = S_APP_RECORD[:1] + b"\x03\x01" + S_APP_RECORD[3:]
    with pytest.raises(InvalidTag):
        open_record(SUITE, keys, 1, downgraded_header)
    with pytest.raises(ValueError, match="outer type"):
        open_record(SUITE, keys, 1, bytes([HANDSHAKE]) + S_APP_RECORD[1:])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "secret_length", "key_length"),
    [(0x1301, 32, 16), (0x1302, 48, 32), (0x1303, 32, 32)],
)
def test_suite_hash_sets_secret_length_and_aead_key_length(
    code: int, secret_length: int, key_length: int
) -> None:
    suite = SUITES[code]
    hello_hash = transcript_hash(suite, CLIENT_HELLO, SERVER_HELLO)
    secrets = derive_handshake_secrets(suite, ECDHE_SHARED_SECRET, hello_hash)
    assert len(secrets.server_handshake_traffic_secret) == secret_length
    keys = traffic_keys(suite, secrets.server_handshake_traffic_secret)
    assert (len(keys.key), len(keys.iv)) == (key_length, 12)
    sealed = seal_record(suite, keys, 7, 21, b"\x01\x00", padding=100)
    assert sealed[0] == APPLICATION_DATA  # the real type (alert) is inside the ciphertext
    assert int.from_bytes(sealed[3:5], "big") == 2 + 1 + 100 + 16
    assert open_record(suite, keys, 7, sealed) == (21, b"\x01\x00")
    with pytest.raises(ValueError, match="record_overflow"):
        seal_record(suite, keys, 8, APPLICATION_DATA, bytes(2**14 + 1))


# ---------------------------------------------------------------- live OpenSSL handshakes


@pytest.mark.unit
def test_openssl_client_hello_wire_layout(identity: tuple[Path, Path]) -> None:
    trace = run_memory_handshake(
        client_ctx(identity[0], HTTPCORE_ALPN), server_ctx(identity, HYPERCORN_ALPN), HOST
    )
    record = trace.records(0)[0]
    assert (record.content_type, record.legacy_version) == (HANDSHAKE, 0x0301)
    hello = parse_client_hello(split_handshake(record.fragment)[0])
    assert hello.legacy_version == TLS12
    assert hello.supported_versions[0] == TLS13
    assert len(hello.session_id) == 32  # middlebox compatibility mode (RFC 9846 Appendix E.4)
    assert hello.compression_methods == b"\x00"
    assert set(hello.cipher_suites[:3]) == {0x1301, 0x1302, 0x1303}
    assert hello.extensions[16] == b"\x00\x0c\x08http/1.1\x02h2"
    assert hello.alpn == (b"http/1.1", b"h2")


@pytest.mark.unit
def test_default_key_share_is_the_x25519mlkem768_hybrid(identity: tuple[Path, Path]) -> None:
    trace = run_memory_handshake(client_ctx(identity[0]), server_ctx(identity), HOST)
    client_hello, server_hello = hello_messages(trace)
    assert parse_client_hello(client_hello).key_shares == {X25519MLKEM768: 1216, X25519: 32}
    group, server_share = parse_server_hello(server_hello).key_share
    assert (group, len(server_share)) == (X25519MLKEM768, 1120)
    assert len(trace.flights[0][1]) > 1500  # one ClientHello no longer fits a 1500-byte MTU


@pytest.mark.unit
def test_server_hello_selects_tls13_through_supported_versions(
    identity: tuple[Path, Path],
) -> None:
    trace = run_memory_handshake(client_ctx(identity[0]), server_ctx(identity), HOST)
    raw_ch, raw_sh = hello_messages(trace)
    ch, sh = parse_client_hello(raw_ch), parse_server_hello(raw_sh)
    assert trace.records(1)[0].legacy_version == TLS12
    assert sh.legacy_version == TLS12
    assert sh.selected_version == TLS13
    assert sh.session_id_echo == ch.session_id
    assert sh.cipher_suite in ch.cipher_suites
    assert SUITES[sh.cipher_suite].name == trace.cipher == "TLS_AES_256_GCM_SHA384"
    assert not sh.is_hello_retry_request
    assert sh.random[-8:] not in (DOWNGRADE_TLS12, DOWNGRADE_TLS11)


@pytest.mark.unit
def test_tls13_client_finishes_after_one_round_trip_and_tls12_after_two(
    identity: tuple[Path, Path],
) -> None:
    tls13 = run_memory_handshake(client_ctx(identity[0]), server_ctx(identity), HOST)
    capped = client_ctx(identity[0])
    capped.maximum_version = ssl.TLSVersion.TLSv1_2
    tls12 = run_memory_handshake(capped, server_ctx(identity), HOST)
    assert (tls13.version, tls13.client_round_trips) == ("TLSv1.3", 1)
    assert (tls12.version, tls12.client_round_trips) == ("TLSv1.2", 2)


@pytest.mark.unit
def test_tls13_encrypts_alpn_and_certificate_that_tls12_sends_in_clear(
    identity: tuple[Path, Path],
) -> None:
    der = ssl.PEM_cert_to_DER_cert(identity[0].read_text())
    alpn = ["http/1.1"]
    tls13 = run_memory_handshake(client_ctx(identity[0], alpn), server_ctx(identity, alpn), HOST)
    after_hello = tls13.records(1)[1:]
    assert {r.content_type for r in after_hello} <= {CHANGE_CIPHER_SPEC, APPLICATION_DATA}
    protected = b"".join(r.fragment for r in after_hello)
    assert b"http/1.1" not in protected
    assert der not in protected
    capped = client_ctx(identity[0], alpn)
    capped.maximum_version = ssl.TLSVersion.TLSv1_2
    tls12 = run_memory_handshake(capped, server_ctx(identity, alpn), HOST)
    _, server_hello = hello_messages(tls12)
    assert parse_alpn(parse_server_hello(server_hello).extensions[16]) == (b"http/1.1",)
    assert der in tls12.flights[1][1]
    assert tls13.server_alpn == tls12.server_alpn == "http/1.1"


@pytest.mark.unit
def test_hello_retry_request_costs_a_second_round_trip(identity: tuple[Path, Path]) -> None:
    server = server_ctx(identity)
    server.set_ecdh_curve("secp384r1")  # SSL_CTX_set1_groups with one group on OpenSSL 3+
    trace = run_memory_handshake(client_ctx(identity[0]), server, HOST)
    first_ch, hrr = hello_messages(trace)
    retry = parse_server_hello(hrr)
    assert retry.is_hello_retry_request
    assert retry.key_share[0] == SECP384R1
    assert SECP384R1 not in parse_client_hello(first_ch).key_shares
    second_ch = next(r for r in trace.records(2) if r.content_type == HANDSHAKE)
    assert parse_client_hello(second_ch.fragment).key_shares == {SECP384R1: 97}
    assert (trace.version, trace.client_round_trips) == ("TLSv1.3", 2)


@pytest.mark.unit
def test_tls13_server_negotiating_tls12_stamps_the_downgrade_sentinel(
    identity: tuple[Path, Path],
) -> None:
    capped_client = client_ctx(identity[0])
    capped_client.maximum_version = ssl.TLSVersion.TLSv1_2
    downgraded = parse_server_hello(
        hello_messages(run_memory_handshake(capped_client, server_ctx(identity), HOST))[1]
    )
    assert downgraded.selected_version == TLS12
    assert 43 not in downgraded.extensions
    assert downgraded.random[-8:] == DOWNGRADE_TLS12
    capped_server = server_ctx(identity)
    capped_server.maximum_version = ssl.TLSVersion.TLSv1_2
    tls12_only = parse_server_hello(
        hello_messages(run_memory_handshake(client_ctx(identity[0]), capped_server, HOST))[1]
    )
    assert tls12_only.selected_version == TLS12
    assert tls12_only.random[-8:] != DOWNGRADE_TLS12  # a TLS 1.2-only server has no 1.3 to protect


@pytest.mark.unit
def test_default_server_sends_dummy_change_cipher_spec_after_server_hello(
    identity: tuple[Path, Path],
) -> None:
    trace = run_memory_handshake(client_ctx(identity[0]), server_ctx(identity), HOST)
    ccs = trace.records(1)[1]
    assert (ccs.content_type, ccs.fragment) == (CHANGE_CIPHER_SPEC, b"\x01")
    assert trace.records(2)[0].content_type == CHANGE_CIPHER_SPEC


@pytest.mark.unit
def test_hypercorn_options_assignment_drops_the_middlebox_ccs(
    identity: tuple[Path, Path],
) -> None:
    config = HypercornConfig()
    config.certfile, config.keyfile = str(identity[0]), str(identity[1])
    context = config.create_ssl_context()
    assert context is not None
    assert context.options & ssl.OP_ENABLE_MIDDLEBOX_COMPAT == 0
    assert context.options & ssl.OP_CIPHER_SERVER_PREFERENCE == 0
    trace = run_memory_handshake(client_ctx(identity[0], HTTPCORE_ALPN), context, HOST)
    raw_ch, raw_sh = hello_messages(trace)
    assert len(parse_client_hello(raw_ch).session_id) == 32
    assert parse_server_hello(raw_sh).session_id_echo == parse_client_hello(raw_ch).session_id
    assert CHANGE_CIPHER_SPEC not in {r.content_type for r in trace.records(1)}
    assert (trace.version, trace.client_alpn, trace.server_alpn) == ("TLSv1.3", "h2", "h2")


# ---------------------------------------------------------------- ALPN


@pytest.mark.unit
@pytest.mark.parametrize(
    ("server_alpn", "expected"),
    [(HYPERCORN_ALPN, "h2"), (["http/1.1", "h2"], "http/1.1")],
)
def test_alpn_server_preference_beats_client_order(
    identity: tuple[Path, Path], server_alpn: list[str], expected: str
) -> None:
    trace = run_memory_handshake(
        client_ctx(identity[0], HTTPCORE_ALPN), server_ctx(identity, server_alpn), HOST
    )
    assert trace.client_alpn == trace.server_alpn == expected
    offer = [p.encode() for p in HTTPCORE_ALPN]
    assert select_alpn([p.encode() for p in server_alpn], offer) == expected.encode()


@pytest.mark.unit
def test_cpython_server_completes_handshake_without_alpn_on_mismatch(
    identity: tuple[Path, Path],
) -> None:
    trace = run_memory_handshake(
        client_ctx(identity[0], ["h2"]), server_ctx(identity, ["http/1.1"]), HOST
    )
    assert (trace.version, trace.client_alpn, trace.server_alpn) == ("TLSv1.3", None, None)
    with pytest.raises(NoApplicationProtocolError):
        select_alpn([b"http/1.1"], [b"h2"])
    assert select_alpn([b"http/1.1"], []) is None


@pytest.mark.unit
def test_uvicorn_h11_ssl_context_negotiates_no_alpn(identity: tuple[Path, Path]) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError

    config = UvicornConfig(
        app, ssl_certfile=str(identity[0]), ssl_keyfile=str(identity[1]), http="h11"
    )
    config.load()
    assert config.ssl is not None
    trace = run_memory_handshake(client_ctx(identity[0], HTTPCORE_ALPN), config.ssl, HOST)
    assert (trace.version, trace.client_alpn, trace.server_alpn) == ("TLSv1.3", None, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        b"\x00\x00",  # empty list; ProtocolNameList is <2..2^16-1>
        b"\x00\x03\x03h2",  # 3-byte name inside a 3-byte list overruns it
        b"\x00\x03\x00\x01x",  # empty ProtocolName
        b"\x00\x03\x02h2\x00",  # trailing bytes after the list
    ],
)
def test_parse_alpn_rejects_malformed_protocol_name_lists(body: bytes) -> None:
    with pytest.raises(ValueError, match="decode_error"):
        parse_alpn(body)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["", "x" * 256])
def test_cpython_rejects_alpn_names_outside_1_to_255_bytes(name: str) -> None:
    with pytest.raises(ssl.SSLError, match="1 to 255"):
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).set_alpn_protocols([name])


# ---------------------------------------------------------------- cipher suite configuration


def tls13_offered(trace: HandshakeTrace) -> tuple[int, ...]:
    hello = parse_client_hello(hello_messages(trace)[0])
    return tuple(code for code in hello.cipher_suites if 0x1301 <= code <= 0x1305)


@pytest.mark.unit
def test_set_ciphers_restricts_only_tls12_suites(identity: tuple[Path, Path]) -> None:
    client = client_ctx(identity[0])
    client.set_ciphers("ECDHE-ECDSA-AES128-GCM-SHA256")
    trace = run_memory_handshake(client, server_ctx(identity), HOST)
    hello = parse_client_hello(hello_messages(trace)[0])
    assert set(tls13_offered(trace)) == {0x1301, 0x1302, 0x1303}
    assert set(hello.cipher_suites) - {0x1301, 0x1302, 0x1303} == {0xC02B}
    assert trace.cipher == "TLS_AES_256_GCM_SHA384"
    with pytest.raises(ssl.SSLError, match="No cipher can be selected"):
        client.set_ciphers("TLS_AES_128_GCM_SHA256")  # a TLS 1.3 name is not a TLS 1.2 cipher


@pytest.mark.unit
def test_tls13_suite_selection_api_exists_from_cpython_3_15(identity: tuple[Path, Path]) -> None:
    client = client_ctx(identity[0])
    if sys.version_info >= (3, 15):
        client.set_ciphersuites("TLS_CHACHA20_POLY1305_SHA256")
        trace = run_memory_handshake(client, server_ctx(identity), HOST)
        assert tls13_offered(trace) == (0x1303,)
        assert trace.cipher == "TLS_CHACHA20_POLY1305_SHA256"
    else:
        assert not hasattr(client, "set_ciphersuites")
        trace = run_memory_handshake(client, server_ctx(identity), HOST)
        assert len(tls13_offered(trace)) == 3


# ---------------------------------------------------------------- real loopback socket


@pytest.mark.socket
def test_loopback_tls13_handshake_negotiates_h2_with_hypercorn_context(
    identity: tuple[Path, Path],
) -> None:
    config = HypercornConfig()
    config.certfile, config.keyfile = str(identity[0]), str(identity[1])
    server = config.create_ssl_context()
    assert server is not None
    result = loopback_handshake(client_ctx(identity[0], HTTPCORE_ALPN), server, HOST)
    assert result == ("TLSv1.3", "h2", "h2")
