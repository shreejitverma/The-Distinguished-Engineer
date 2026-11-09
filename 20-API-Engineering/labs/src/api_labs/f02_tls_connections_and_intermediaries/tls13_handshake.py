"""TLS 1.3 key schedule, record protection, hello parsing, and ALPN (RFC 9846, RFC 7301)."""

from __future__ import annotations

import datetime
import socket
import ssl
import struct
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# Content types (RFC 9846 Section 5.1) and handshake types (RFC 9846 Appendix B.3).
CHANGE_CIPHER_SPEC = 20
HANDSHAKE = 22
APPLICATION_DATA = 23
CLIENT_HELLO = 1
SERVER_HELLO = 2
# Extension types (RFC 9846 Section 4.3, RFC 7301 Section 3.1).
EXT_ALPN = 16
EXT_SUPPORTED_VERSIONS = 43
EXT_KEY_SHARE = 51
TLS12 = 0x0303
TLS13 = 0x0304
MAX_PLAINTEXT = 2**14
MAX_CIPHERTEXT = 2**14 + 256
# SHA-256("HelloRetryRequest") and the downgrade sentinels (RFC 9846 Section 4.2.3).
HELLO_RETRY_REQUEST_RANDOM = bytes.fromhex(
    "cf21ad74e59a6111be1d8c021e65b891c2a211167abb8c5e079e09e2c8a8339c"
)
DOWNGRADE_TLS12 = b"DOWNGRD\x01"
DOWNGRADE_TLS11 = b"DOWNGRD\x00"


@dataclass(frozen=True, slots=True)
class CipherSuite:
    """A TLS 1.3 suite names only an AEAD and the HKDF hash (RFC 9846 Appendix B.4)."""

    code: int
    name: str
    hash_name: str
    key_length: int
    iv_length: int = 12

    def hash(self) -> hashes.HashAlgorithm:
        return hashes.SHA384() if self.hash_name == "sha384" else hashes.SHA256()

    @property
    def hash_length(self) -> int:
        return self.hash().digest_size

    def aead(self, key: bytes) -> AESGCM | ChaCha20Poly1305:
        if "CHACHA20" in self.name:
            return ChaCha20Poly1305(key)
        return AESGCM(key)


SUITES: dict[int, CipherSuite] = {
    0x1301: CipherSuite(0x1301, "TLS_AES_128_GCM_SHA256", "sha256", 16),
    0x1302: CipherSuite(0x1302, "TLS_AES_256_GCM_SHA384", "sha384", 32),
    0x1303: CipherSuite(0x1303, "TLS_CHACHA20_POLY1305_SHA256", "sha256", 32),
}


# ---------------------------------------------------------------- key schedule (Section 7)


def hkdf_extract(suite: CipherSuite, salt: bytes, ikm: bytes) -> bytes:
    """HKDF-Extract (RFC 5869 Section 2.2); an absent secret is Hash.length zero bytes."""
    zeros = bytes(suite.hash_length)
    return HKDF.extract(suite.hash(), salt or zeros, ikm or zeros)


def hkdf_label(length: int, label: bytes, context: bytes) -> bytes:
    """Serialize HkdfLabel: uint16 length, opaque "tls13 "+label<7..255>, context<0..255>."""
    full = b"tls13 " + label
    if not 7 <= len(full) <= 255 or len(context) > 255:
        raise ValueError("HkdfLabel field out of range")
    return struct.pack(">H", length) + bytes([len(full)]) + full + bytes([len(context)]) + context


def hkdf_expand_label(
    suite: CipherSuite, secret: bytes, label: bytes, context: bytes, length: int
) -> bytes:
    info = hkdf_label(length, label, context)
    return HKDFExpand(suite.hash(), length, info).derive(secret)


def transcript_hash(suite: CipherSuite, *messages: bytes) -> bytes:
    """Hash of the concatenated handshake messages, headers included (RFC 9846 Section 4.1)."""
    digest = hashes.Hash(suite.hash())
    for message in messages:
        digest.update(message)
    return digest.finalize()


def derive_secret(suite: CipherSuite, secret: bytes, label: bytes, thash: bytes) -> bytes:
    return hkdf_expand_label(suite, secret, label, thash, suite.hash_length)


@dataclass(frozen=True, slots=True)
class HandshakeSecrets:
    early_secret: bytes
    handshake_secret: bytes
    client_handshake_traffic_secret: bytes
    server_handshake_traffic_secret: bytes
    main_secret: bytes


def derive_handshake_secrets(
    suite: CipherSuite, shared_secret: bytes, hello_hash: bytes, psk: bytes = b""
) -> HandshakeSecrets:
    """Walk Figure 5 down to the main secret; hello_hash covers ClientHello...ServerHello."""
    empty_hash = transcript_hash(suite)
    early = hkdf_extract(suite, b"", psk)
    hs = hkdf_extract(suite, derive_secret(suite, early, b"derived", empty_hash), shared_secret)
    main = hkdf_extract(suite, derive_secret(suite, hs, b"derived", empty_hash), b"")
    return HandshakeSecrets(
        early_secret=early,
        handshake_secret=hs,
        client_handshake_traffic_secret=derive_secret(suite, hs, b"c hs traffic", hello_hash),
        server_handshake_traffic_secret=derive_secret(suite, hs, b"s hs traffic", hello_hash),
        main_secret=main,
    )


def derive_application_secrets(
    suite: CipherSuite, main_secret: bytes, server_finished_hash: bytes
) -> tuple[bytes, bytes]:
    """client_/server_application_traffic_secret_0 over ClientHello...server Finished."""
    return (
        derive_secret(suite, main_secret, b"c ap traffic", server_finished_hash),
        derive_secret(suite, main_secret, b"s ap traffic", server_finished_hash),
    )


def finished_verify_data(suite: CipherSuite, base_key: bytes, thash: bytes) -> bytes:
    """verify_data = HMAC(finished_key, Transcript-Hash(...)) (RFC 9846 Section 4.5.3)."""
    finished_key = hkdf_expand_label(suite, base_key, b"finished", b"", suite.hash_length)
    mac = HMAC(finished_key, suite.hash())
    mac.update(thash)
    return mac.finalize()


@dataclass(frozen=True, slots=True)
class TrafficKeys:
    key: bytes
    iv: bytes


def traffic_keys(suite: CipherSuite, secret: bytes) -> TrafficKeys:
    """[sender]_write_key and [sender]_write_iv (RFC 9846 Section 7.3)."""
    return TrafficKeys(
        key=hkdf_expand_label(suite, secret, b"key", b"", suite.key_length),
        iv=hkdf_expand_label(suite, secret, b"iv", b"", suite.iv_length),
    )


# ---------------------------------------------------------------- record layer (Section 5)


def record_nonce(iv: bytes, sequence: int) -> bytes:
    """Left-pad the 64-bit sequence number to iv_length and XOR it with the IV (Section 5.3)."""
    padded = sequence.to_bytes(len(iv), "big")
    return bytes(a ^ b for a, b in zip(iv, padded, strict=True))


def seal_record(
    suite: CipherSuite,
    keys: TrafficKeys,
    sequence: int,
    content_type: int,
    content: bytes,
    padding: int = 0,
) -> bytes:
    """Build a TLSCiphertext: outer type 23, version 0x0303, AAD = the 5-byte header."""
    if len(content) > MAX_PLAINTEXT:
        raise ValueError("record_overflow: content exceeds 2^14 bytes")
    inner = content + bytes([content_type]) + bytes(padding)
    header = struct.pack(">BHH", APPLICATION_DATA, TLS12, len(inner) + 16)
    nonce = record_nonce(keys.iv, sequence)
    return header + suite.aead(keys.key).encrypt(nonce, inner, header)


def open_record(
    suite: CipherSuite, keys: TrafficKeys, sequence: int, record: bytes
) -> tuple[int, bytes]:
    """Decrypt one TLSCiphertext and return (inner content type, content), padding stripped."""
    opaque_type, _version, length = struct.unpack(">BHH", record[:5])
    if opaque_type != APPLICATION_DATA:
        raise ValueError("protected records always carry outer type application_data")
    if length > MAX_CIPHERTEXT or len(record) != 5 + length:
        raise ValueError("record_overflow or truncated record")
    inner = suite.aead(keys.key).decrypt(record_nonce(keys.iv, sequence), record[5:], record[:5])
    stripped = inner.rstrip(b"\x00")
    if not stripped:
        raise ValueError("unexpected_message: all-zero TLSInnerPlaintext")
    return stripped[-1], stripped[:-1]


# ---------------------------------------------------------------- wire parsing (Section 4)


@dataclass(frozen=True, slots=True)
class Record:
    content_type: int
    legacy_version: int
    fragment: bytes


def split_records(data: bytes) -> list[Record]:
    records: list[Record] = []
    offset = 0
    while offset < len(data):
        content_type, version, length = struct.unpack(">BHH", data[offset : offset + 5])
        fragment = data[offset + 5 : offset + 5 + length]
        if len(fragment) != length:
            raise ValueError("truncated record")
        records.append(Record(content_type, version, fragment))
        offset += 5 + length
    return records


def split_handshake(data: bytes) -> list[bytes]:
    """Split coalesced handshake messages, each kept with its 4-byte type and length header."""
    messages: list[bytes] = []
    offset = 0
    while offset < len(data):
        length = int.from_bytes(data[offset + 1 : offset + 4], "big")
        end = offset + 4 + length
        if end > len(data):
            raise ValueError("truncated handshake message")
        messages.append(data[offset:end])
        offset = end
    return messages


def _vector(data: bytes, offset: int, width: int) -> tuple[bytes, int]:
    length = int.from_bytes(data[offset : offset + width], "big")
    start = offset + width
    if start + length > len(data):
        raise ValueError("decode_error: vector overruns message")
    return data[start : start + length], start + length


def parse_extensions(block: bytes) -> dict[int, bytes]:
    extensions: dict[int, bytes] = {}
    offset = 0
    while offset < len(block):
        ext_type = int.from_bytes(block[offset : offset + 2], "big")
        body, offset = _vector(block, offset + 2, 2)
        if ext_type in extensions:
            raise ValueError("illegal_parameter: duplicate extension")
        extensions[ext_type] = body
    return extensions


def _key_shares(body: bytes) -> dict[int, int]:
    """Map group -> key_exchange length for a KeyShareEntry list."""
    shares: dict[int, int] = {}
    offset = 0
    while offset < len(body):
        group = int.from_bytes(body[offset : offset + 2], "big")
        key_exchange, offset = _vector(body, offset + 2, 2)
        shares[group] = len(key_exchange)
    return shares


def parse_alpn(body: bytes) -> tuple[bytes, ...]:
    """ProtocolNameList: opaque ProtocolName<1..2^8-1> inside a <2..2^16-1> vector."""
    names_block, end = _vector(body, 0, 2)
    if end != len(body) or len(names_block) < 2:
        raise ValueError("decode_error: malformed ProtocolNameList")
    names: list[bytes] = []
    offset = 0
    while offset < len(names_block):
        name, offset = _vector(names_block, offset, 1)
        if not name:
            raise ValueError("decode_error: empty ProtocolName")
        names.append(name)
    return tuple(names)


@dataclass(frozen=True, slots=True)
class ClientHello:
    legacy_version: int
    random: bytes
    session_id: bytes
    cipher_suites: tuple[int, ...]
    compression_methods: bytes
    extensions: dict[int, bytes]

    @property
    def supported_versions(self) -> tuple[int, ...]:
        body, _ = _vector(self.extensions[EXT_SUPPORTED_VERSIONS], 0, 1)
        return tuple(int.from_bytes(body[i : i + 2], "big") for i in range(0, len(body), 2))

    @property
    def key_shares(self) -> dict[int, int]:
        body, _ = _vector(self.extensions[EXT_KEY_SHARE], 0, 2)
        return _key_shares(body)

    @property
    def alpn(self) -> tuple[bytes, ...]:
        body = self.extensions.get(EXT_ALPN)
        return () if body is None else parse_alpn(body)


@dataclass(frozen=True, slots=True)
class ServerHello:
    legacy_version: int
    random: bytes
    session_id_echo: bytes
    cipher_suite: int
    extensions: dict[int, bytes]

    @property
    def selected_version(self) -> int:
        """supported_versions overrides legacy_version (RFC 9846 Section 4.3.1)."""
        body = self.extensions.get(EXT_SUPPORTED_VERSIONS)
        return self.legacy_version if body is None else int.from_bytes(body, "big")

    @property
    def is_hello_retry_request(self) -> bool:
        return self.random == HELLO_RETRY_REQUEST_RANDOM

    @property
    def key_share(self) -> tuple[int, bytes]:
        body = self.extensions[EXT_KEY_SHARE]
        group = int.from_bytes(body[:2], "big")
        if self.is_hello_retry_request:
            return group, b""
        key_exchange, _ = _vector(body, 2, 2)
        return group, key_exchange


def parse_client_hello(message: bytes) -> ClientHello:
    if message[0] != CLIENT_HELLO:
        raise ValueError("not a ClientHello")
    body = message[4:]
    legacy_version = int.from_bytes(body[0:2], "big")
    random = body[2:34]
    session_id, offset = _vector(body, 34, 1)
    suites_block, offset = _vector(body, offset, 2)
    compression, offset = _vector(body, offset, 1)
    extensions_block, _ = _vector(body, offset, 2)
    suites = tuple(
        int.from_bytes(suites_block[i : i + 2], "big") for i in range(0, len(suites_block), 2)
    )
    return ClientHello(
        legacy_version, random, session_id, suites, compression, parse_extensions(extensions_block)
    )


def parse_server_hello(message: bytes) -> ServerHello:
    if message[0] != SERVER_HELLO:
        raise ValueError("not a ServerHello")
    body = message[4:]
    session_id, offset = _vector(body, 34, 1)
    cipher_suite = int.from_bytes(body[offset : offset + 2], "big")
    extensions_block, _ = _vector(body, offset + 3, 2)
    return ServerHello(
        legacy_version=int.from_bytes(body[0:2], "big"),
        random=body[2:34],
        session_id_echo=session_id,
        cipher_suite=cipher_suite,
        extensions=parse_extensions(extensions_block),
    )


# ---------------------------------------------------------------- ALPN (RFC 7301 Section 3.2)


class NoApplicationProtocolError(Exception):
    """The fatal no_application_protocol(120) alert RFC 7301 Section 3.2 requires."""


def select_alpn(server_preference: Sequence[bytes], client_offer: Sequence[bytes]) -> bytes | None:
    """Server-preference selection; None means the client sent no ALPN extension."""
    if not client_offer:
        return None
    for protocol in server_preference:
        if protocol in client_offer:
            return protocol
    raise NoApplicationProtocolError("no_application_protocol")


# ---------------------------------------------------------------- live OpenSSL handshakes


def write_self_signed_identity(directory: Path, hostname: str) -> tuple[Path, Path]:
    """Write a P-256 self-signed certificate and PKCS#8 key; return (cert_path, key_path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    not_before = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@dataclass(frozen=True, slots=True)
class HandshakeTrace:
    flights: tuple[tuple[str, bytes], ...]
    client_round_trips: int
    version: str | None
    cipher: str | None
    client_alpn: str | None
    server_alpn: str | None

    def records(self, index: int) -> list[Record]:
        return split_records(self.flights[index][1])


def run_memory_handshake(
    client_context: ssl.SSLContext, server_context: ssl.SSLContext, hostname: str
) -> HandshakeTrace:
    """Pump a real OpenSSL handshake through MemoryBIOs and record every flight's bytes.

    client_round_trips counts how often the client had to wait for the server before its
    do_handshake() returned, which is when it may send application data.
    """
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(c_in, c_out, server_hostname=hostname)
    server = server_context.wrap_bio(s_in, s_out, server_side=True)
    flights: list[tuple[str, bytes]] = []
    client_done = server_done = False
    waits = 0
    for _ in range(8):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                waits += 1
        if data := c_out.read():
            flights.append(("client", data))
            s_in.write(data)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        if data := s_out.read():
            flights.append(("server", data))
            c_in.write(data)
        if client_done and server_done:
            break
    else:
        raise RuntimeError("handshake did not converge")
    cipher = client.cipher()
    return HandshakeTrace(
        flights=tuple(flights),
        client_round_trips=waits,
        version=client.version(),
        cipher=None if cipher is None else cipher[0],
        client_alpn=client.selected_alpn_protocol(),
        server_alpn=server.selected_alpn_protocol(),
    )


def loopback_handshake(
    client_context: ssl.SSLContext, server_context: ssl.SSLContext, hostname: str
) -> tuple[str | None, str | None, str | None]:
    """Handshake over a real 127.0.0.1:0 TCP socket; return (version, client ALPN, server ALPN)."""
    server_alpn: list[str | None] = []
    errors: list[BaseException] = []
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(5)
        port = listener.getsockname()[1]

        def serve() -> None:
            try:
                conn, _ = listener.accept()
                with server_context.wrap_socket(conn, server_side=True) as tls:
                    server_alpn.append(tls.selected_alpn_protocol())
                    tls.recv(1)
            except (OSError, ssl.SSLError) as exc:
                errors.append(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with (
                socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
                client_context.wrap_socket(raw, server_hostname=hostname) as tls,
            ):
                result = (tls.version(), tls.selected_alpn_protocol())
                tls.sendall(b"x")
        finally:
            thread.join(timeout=5)
    if errors:
        raise errors[0]
    return result[0], result[1], server_alpn[0]
