"""Lab: mutual TLS client authentication, SPKI pinning, and SPIFFE X.509-SVID identities."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import ssl
import sys
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from granian import Granian
from hypercorn.config import Config as HypercornConfig
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.config import create_ssl_context as uvicorn_ssl_context

from api_labs.f02_tls_connections_and_intermediaries.mtls_spiffe import (
    MAX_SPIFFE_ID_BYTES,
    Identity,
    SpiffeAuthMiddleware,
    SpiffeId,
    SpiffeIdError,
    SvidError,
    chain_matches_pins,
    issue_leaf,
    leaf_svid_id,
    make_ca,
    parse_spiffe_id,
    run_memory_handshake,
    spki_sha256_pin,
    token_bound_to,
    uri_sans_from_peercert,
    verify_svid_chain,
    x5t_s256,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from starlette.requests import Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

ORDERS = "spiffe://prod.example/ns/default/sa/orders"
BILLING = "spiffe://prod.example/ns/default/sa/billing"
NOW = dt.datetime(2026, 11, 9, tzinfo=dt.UTC)
NO_TX_CERTIFICATE_COMPRESSION = 1 << 32  # SSL_OP_NO_TX_CERTIFICATE_COMPRESSION

PROD_CA = make_ca("prod.example CA", 1001, spiffe_id="spiffe://prod.example")
CLIENT_CA = make_ca("prod.example client CA", 1002)
PARTNER_CA = make_ca("partner.example CA", 1003, spiffe_id="spiffe://partner.example")
ROGUE_CA = make_ca("untrusted CA", 1004)
CONSTRAINED_CA = make_ca("constrained CA", 1005, uri_constraint="prod.example")

SERVER = issue_leaf(
    PROD_CA,
    2001,
    uris=["spiffe://prod.example/ns/default/sa/api"],
    dns=["localhost"],
    ips=["127.0.0.1"],
)
CLIENT = issue_leaf(PROD_CA, 3001, uris=[ORDERS])
# OpenSSL 4 renamed the SSLV3_ALERT_* reason symbols to TLS_ALERT_* (same numeric codes).
ALERT = "TLS_ALERT_" if ssl.OPENSSL_VERSION_INFO[0] >= 4 else "SSLV3_ALERT_"
# KeyUsage(digital_signature, content_commitment, key_encipherment, data_encipherment,
#          key_agreement, key_cert_sign, crl_sign, encipher_only, decipher_only)
CERT_SIGN_ONLY = x509.KeyUsage(True, False, False, False, False, True, False, False, False)
CRL_SIGN_ONLY = x509.KeyUsage(True, False, False, False, False, False, True, False, False)
BUNDLES = {"prod.example": [PROD_CA.cert], "partner.example": [PARTNER_CA.cert]}


def duplicate_extension(cert: x509.Certificate) -> x509.Certificate:
    """Rewrite the AKID OID (2.5.29.35) to SKID (2.5.29.14): two SKID extensions."""
    der = cert.public_bytes(serialization.Encoding.DER)
    akid_oid, skid_oid = b"\x06\x03\x55\x1d\x23", b"\x06\x03\x55\x1d\x0e"
    assert der.count(akid_oid) == 1
    return x509.load_der_x509_certificate(der.replace(akid_oid, skid_oid))


def write_identity(directory: Path, ident: Identity) -> tuple[Path, Path]:
    serial = ident.cert.serial_number
    cert_path, key_path = directory / f"{serial}.crt", directory / f"{serial}.key"
    cert_path.write_text(ident.cert_pem())
    key_path.write_bytes(ident.key_pem())
    return cert_path, key_path


@pytest.fixture
def files(tmp_path: Path) -> Path:
    return tmp_path


def server_ctx(
    directory: Path,
    *,
    mode: ssl.VerifyMode = ssl.CERT_REQUIRED,
    trust: Sequence[Identity] = (PROD_CA,),
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*write_identity(directory, SERVER))
    ctx.verify_mode = mode
    for anchor in trust:
        ctx.load_verify_locations(cadata=anchor.cert_pem())
    return ctx


def client_ctx(
    directory: Path,
    ident: Identity | None = None,
    *,
    trust: Sequence[Identity] = (PROD_CA,),
    tls12: bool = False,
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for anchor in trust:
        ctx.load_verify_locations(cadata=anchor.cert_pem())
    if ident is not None:
        ctx.load_cert_chain(*write_identity(directory, ident))
    if tls12:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# --- SPIFFE ID syntax (SPIFFE-ID Section 2) ----------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "trust_domain", "path"),
    [
        (ORDERS, "prod.example", "/ns/default/sa/orders"),
        ("spiffe://prod.example", "prod.example", ""),
        ("spiffe://td_1.example/9eebccd2-12bf-40a6-b262-65fe0487d453", "td_1.example", None),
        ("spiffe://10.0.0.1/Web.FE", "10.0.0.1", "/Web.FE"),
    ],
    ids=["k8s-sa", "root", "opaque", "ipv4-and-mixed-case-path"],
)
def test_parse_spiffe_id_accepts_canonical_ids(
    uri: str, trust_domain: str, path: str | None
) -> None:
    parsed = parse_spiffe_id(uri)
    assert parsed.trust_domain == trust_domain
    assert path is None or parsed.path == path
    assert str(parsed) == uri


@pytest.mark.unit
@pytest.mark.parametrize(
    ("uri", "message"),
    [
        ("SPIFFE://prod.example/a", "scheme"),
        ("https://prod.example/a", "scheme"),
        ("spiffe:/prod.example/a", "scheme"),
        ("spiffe:///a", "trust domain is empty"),
        ("spiffe://prod.example:443/a", "port"),
        ("spiffe://admin@prod.example/a", "userinfo"),
        ("spiffe://Prod.example/a", "[a-z0-9.-_]"),
        ("spiffe://prod%2eexample/a", "[a-z0-9.-_]"),
        ("spiffe://[::1]/a", "port"),  # IPv6 needs ":", so it is excluded
        ("spiffe://prod.example/a?x=1", "query"),
        ("spiffe://prod.example/a#frag", "fragment"),
        ("spiffe://prod.example/a/", "trailing"),
        ("spiffe://prod.example//a", "empty path segment"),
        ("spiffe://prod.example/a/../admin", "relative"),
        ("spiffe://prod.example/./a", "relative"),
        ("spiffe://prod.example/a%2Fb", "[a-zA-Z0-9.-_]"),
        ("spiffe://prod.example/a b", "[a-zA-Z0-9.-_]"),
        ("spiffe://" + "a" * 256 + "/x", "trust domain longer"),
    ],
    ids=[
        "upper-scheme",
        "https",
        "one-slash",
        "empty-td",
        "port",
        "userinfo",
        "upper-td",
        "pct-td",
        "ipv6",
        "query",
        "fragment",
        "trailing-slash",
        "empty-segment",
        "dotdot",
        "dot",
        "pct-path",
        "space",
        "td-256",
    ],
)
def test_parse_spiffe_id_rejects_each_section_2_violation(uri: str, message: str) -> None:
    with pytest.raises(SpiffeIdError) as excinfo:
        parse_spiffe_id(uri)
    assert message in str(excinfo.value)


@pytest.mark.unit
def test_spiffe_id_length_limit_is_2048_bytes_inclusive() -> None:
    head = "spiffe://prod.example/"
    exact = head + "a" * (MAX_SPIFFE_ID_BYTES - len(head))
    assert len(exact) == 2048
    assert parse_spiffe_id(exact).path.startswith("/a")
    with pytest.raises(SpiffeIdError, match="longer than 2048"):
        parse_spiffe_id(exact + "a")
    assert parse_spiffe_id("spiffe://" + "a" * 255 + "/x").trust_domain == "a" * 255


# --- X509-SVID leaf rules (X509-SVID Sections 2 and 5.2) ---------------------------------


@pytest.mark.unit
def test_leaf_svid_id_reads_the_single_uri_san_and_ignores_dns_sans() -> None:
    assert leaf_svid_id(SERVER.cert) == SpiffeId("prod.example", "/ns/default/sa/api")
    assert leaf_svid_id(CLIENT.cert) == parse_spiffe_id(ORDERS)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ident", "message"),
    [
        (issue_leaf(PROD_CA, 3101, uris=[ORDERS, BILLING]), "exactly one URI SAN, found 2"),
        (issue_leaf(PROD_CA, 3102, dns=["orders.prod.example"]), "exactly one URI SAN, found 0"),
        (issue_leaf(PROD_CA, 3103, uris=[ORDERS], ca=True), "cA is true"),
        (issue_leaf(PROD_CA, 3106, uris=[ORDERS], usage=CERT_SIGN_ONLY), "keyCertSign is set"),
        (issue_leaf(PROD_CA, 3107, uris=[ORDERS], usage=CRL_SIGN_ONLY), "cRLSign is set"),
        (Identity(duplicate_extension(CLIENT.cert), CLIENT.key), "malformed extensions"),
        (issue_leaf(PROD_CA, 3104, uris=["spiffe://prod.example"]), "non-root path"),
        (issue_leaf(PROD_CA, 3105, uris=["https://prod.example/orders"]), "not a SPIFFE ID"),
        (PROD_CA, "cA is true"),
    ],
    ids=[
        "two-uris",
        "no-uri",
        "ca-leaf",
        "cert-sign-leaf",
        "crl-sign-leaf",
        "duplicate-extension",
        "root-id",
        "https-uri",
        "signing-cert",
    ],
)
def test_leaf_svid_rules_reject_non_leaf_identities(ident: Identity, message: str) -> None:
    with pytest.raises(SvidError, match=message):
        leaf_svid_id(ident.cert)


@pytest.mark.unit
def test_leaf_svid_rejects_a_certificate_without_any_san() -> None:
    with pytest.raises(SvidError, match="no subjectAltName"):
        leaf_svid_id(make_ca("bare", 1099).cert)


# --- Chain verification per trust domain -------------------------------------------------


@pytest.mark.unit
def test_chain_verifies_against_its_own_trust_domain_bundle() -> None:
    verified = verify_svid_chain([CLIENT.cert], BUNDLES, at=NOW)
    assert verified.spiffe_id == parse_spiffe_id(ORDERS)
    assert verified.chain == (CLIENT.cert, PROD_CA.cert)  # leaf first, anchor last


@pytest.mark.unit
def test_chain_through_an_intermediate_needs_the_intermediate() -> None:
    intermediate = make_ca("prod.example issuer", 1201, issuer=PROD_CA)
    leaf = issue_leaf(intermediate, 3251, uris=[ORDERS])
    verified = verify_svid_chain([leaf.cert, intermediate.cert], BUNDLES, at=NOW)
    assert verified.chain == (leaf.cert, intermediate.cert, PROD_CA.cert)
    with pytest.raises(SvidError, match="does not verify"):
        verify_svid_chain([leaf.cert], BUNDLES, at=NOW)
    partner_intermediate = make_ca("partner issuer", 1202, issuer=PARTNER_CA)
    smuggled = issue_leaf(partner_intermediate, 3252, uris=[ORDERS])
    with pytest.raises(SvidError, match="does not verify"):
        verify_svid_chain([smuggled.cert, partner_intermediate.cert], BUNDLES, at=NOW)


@pytest.mark.unit
def test_federated_ca_cannot_mint_ids_for_another_trust_domain(files: Path) -> None:
    forged = issue_leaf(PARTNER_CA, 3201, uris=[ORDERS])
    with pytest.raises(SvidError, match=r"does not verify against 'prod\.example'"):
        verify_svid_chain([forged.cert], BUNDLES, at=NOW)
    # One merged OpenSSL trust store accepts the same forgery during the handshake.
    merged = server_ctx(files, trust=(PROD_CA, PARTNER_CA))
    outcome = run_memory_handshake(client_ctx(files, forged), merged, "localhost")
    assert outcome.server_error is None
    assert uri_sans_from_peercert(outcome.peer_dict) == (ORDERS,)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ident", "at"),
    [
        (issue_leaf(PROD_CA, 3301, uris=[ORDERS], eku=[ExtendedKeyUsageOID.SERVER_AUTH]), NOW),
        (CLIENT, dt.datetime(2036, 1, 2, tzinfo=dt.UTC)),
        (CLIENT, dt.datetime(2025, 12, 31, tzinfo=dt.UTC)),
        (issue_leaf(ROGUE_CA, 3302, uris=[ORDERS]), NOW),
    ],
    ids=["server-auth-only-eku", "expired", "not-yet-valid", "unknown-issuer"],
)
def test_chain_verifier_enforces_eku_validity_and_issuer(ident: Identity, at: dt.datetime) -> None:
    with pytest.raises(SvidError, match="does not verify"):
        verify_svid_chain([ident.cert], BUNDLES, at=at)


@pytest.mark.unit
def test_unknown_trust_domain_and_empty_chain_are_rejected() -> None:
    stranger = issue_leaf(PROD_CA, 3401, uris=["spiffe://other.example/x"])
    with pytest.raises(SvidError, match=r"no trust bundle for trust domain 'other\.example'"):
        verify_svid_chain([stranger.cert], BUNDLES, at=NOW)
    with pytest.raises(SvidError, match="no client certificate"):
        verify_svid_chain([], BUNDLES, at=NOW)


# --- Pins and certificate-bound tokens (RFC 7469, RFC 8705) ------------------------------


@pytest.mark.unit
def test_spki_pin_survives_reissue_but_x5t_s256_does_not() -> None:
    reissued = issue_leaf(PROD_CA, 3001, uris=[ORDERS], serial=99)  # same key, new cert
    rotated = issue_leaf(PROD_CA, 3002, uris=[ORDERS])  # new key
    assert reissued.cert.serial_number != CLIENT.cert.serial_number
    assert spki_sha256_pin(reissued.cert) == spki_sha256_pin(CLIENT.cert)
    assert x5t_s256(reissued.cert) != x5t_s256(CLIENT.cert)
    assert spki_sha256_pin(rotated.cert) != spki_sha256_pin(CLIENT.cert)


@pytest.mark.unit
def test_pin_and_thumbprint_encodings_follow_their_rfcs() -> None:
    der = CLIENT.cert.public_bytes(serialization.Encoding.DER)
    spki = CLIENT.cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert spki in der  # the pin hashes a sub-structure of the certificate
    pin = spki_sha256_pin(CLIENT.cert)
    assert base64.b64decode(pin, validate=True) == hashlib.sha256(spki).digest()
    assert (len(pin), pin[-1]) == (44, "=")  # standard base64 keeps its padding
    thumb = x5t_s256(CLIENT.cert)
    assert (len(thumb), "=" in thumb) == (43, False)  # base64url, padding stripped
    assert base64.urlsafe_b64decode(thumb + "=") == hashlib.sha256(der).digest()


@pytest.mark.unit
def test_pins_match_any_member_of_the_validated_chain_only() -> None:
    verified = verify_svid_chain([CLIENT.cert], BUNDLES, at=NOW)
    ca_pin = spki_sha256_pin(PROD_CA.cert)
    assert chain_matches_pins(verified, {ca_pin})  # the anchor came from the bundle
    assert chain_matches_pins(verified, {spki_sha256_pin(CLIENT.cert)})
    assert not chain_matches_pins(verified, {spki_sha256_pin(PARTNER_CA.cert)})
    # A forger appends the public pinned CA: the presented chain "matches" the pin...
    forged = [issue_leaf(ROGUE_CA, 3260, uris=[ORDERS]).cert, PROD_CA.cert]
    assert ca_pin in {spki_sha256_pin(cert) for cert in forged}
    with pytest.raises(SvidError, match="does not verify"):  # ...but never validates
        verify_svid_chain(forged, BUNDLES, at=NOW)


@pytest.mark.unit
def test_token_binding_requires_the_exact_certificate() -> None:
    claims: dict[str, object] = {"sub": ORDERS, "cnf": {"x5t#S256": x5t_s256(CLIENT.cert)}}
    reissued = issue_leaf(PROD_CA, 3001, uris=[ORDERS], serial=99)
    assert token_bound_to(claims, CLIENT.cert)
    assert not token_bound_to(claims, reissued.cert)  # same key, different certificate
    assert not token_bound_to({"sub": ORDERS}, CLIENT.cert)
    assert not token_bound_to({"cnf": {"x5t#S256": 7}}, CLIENT.cert)


# --- Real OpenSSL handshakes over MemoryBIO ----------------------------------------------


@pytest.mark.unit
def test_mutual_tls13_server_reads_the_client_spiffe_id(files: Path) -> None:
    outcome = run_memory_handshake(client_ctx(files, CLIENT), server_ctx(files), "localhost")
    assert (outcome.version, outcome.client_error, outcome.server_error) == ("TLSv1.3", None, None)
    assert outcome.server_received.startswith(b"GET / HTTP/1.1")
    assert uri_sans_from_peercert(outcome.peer_dict) == (ORDERS,)
    assert outcome.peer_der is not None
    assert leaf_svid_id(x509.load_der_x509_certificate(outcome.peer_der)) == parse_spiffe_id(ORDERS)


@pytest.mark.unit
def test_tls13_rejection_reaches_the_client_only_at_first_read(files: Path) -> None:
    outcome = run_memory_handshake(client_ctx(files), server_ctx(files), "localhost")
    assert outcome.client_handshake_ok  # do_handshake() returned: the client thinks it is in
    assert outcome.server_error == "PEER_DID_NOT_RETURN_A_CERTIFICATE"
    assert outcome.client_error == "TLSV13_ALERT_CERTIFICATE_REQUIRED"  # alert 116
    assert outcome.client_error_phase == "first_read"
    assert outcome.server_received == b""  # the request bytes were sent but never decrypted


@pytest.mark.unit
def test_tls12_rejection_fails_inside_the_client_handshake(files: Path) -> None:
    outcome = run_memory_handshake(client_ctx(files, tls12=True), server_ctx(files), "localhost")
    assert not outcome.client_handshake_ok
    assert outcome.server_error == "PEER_DID_NOT_RETURN_A_CERTIFICATE"
    assert (outcome.client_error, outcome.client_error_phase) == (
        ALERT + "HANDSHAKE_FAILURE",
        "handshake",
    )


@pytest.mark.unit
def test_cert_optional_admits_anonymous_clients_but_aborts_untrusted_ones(files: Path) -> None:
    optional = server_ctx(files, mode=ssl.CERT_OPTIONAL)
    anonymous = run_memory_handshake(client_ctx(files), optional, "localhost")
    assert (anonymous.version, anonymous.peer_der, anonymous.peer_dict) == ("TLSv1.3", None, None)
    with pytest.raises(SvidError, match="no certificate"):
        uri_sans_from_peercert(anonymous.peer_dict)
    rogue = issue_leaf(ROGUE_CA, 3501, uris=[ORDERS])
    untrusted = run_memory_handshake(client_ctx(files, rogue), optional, "localhost")
    assert untrusted.server_error == "CERTIFICATE_VERIFY_FAILED"
    assert untrusted.client_error == "TLSV1_ALERT_UNKNOWN_CA"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ident", "trust", "verify", "client_alert"),
    [
        (
            issue_leaf(ROGUE_CA, 3601, uris=[ORDERS]),
            (PROD_CA,),
            ("unable to get local issuer certificate", 20),
            "TLSV1_ALERT_UNKNOWN_CA",
        ),
        (
            issue_leaf(
                PROD_CA,
                3602,
                uris=[ORDERS],
                not_before=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
                not_after=dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
            ),
            (PROD_CA,),
            ("certificate has expired", 10),
            ALERT + "CERTIFICATE_EXPIRED",
        ),
        (
            issue_leaf(PROD_CA, 3603, uris=[ORDERS], eku=[ExtendedKeyUsageOID.SERVER_AUTH]),
            (PROD_CA,),
            ("unsuitable certificate purpose", 26),
            ALERT + "UNSUPPORTED_CERTIFICATE",
        ),
        (
            issue_leaf(CONSTRAINED_CA, 3604, uris=["spiffe://evil.example/ns/a"]),
            (PROD_CA, CONSTRAINED_CA),
            ("permitted subtree violation", 47),
            ALERT + "CERTIFICATE_UNKNOWN",
        ),
    ],
    ids=["unknown-ca", "expired", "server-auth-eku", "uri-name-constraint"],
)
def test_openssl_rejects_client_certificates_with_a_cause_specific_alert(
    files: Path,
    ident: Identity,
    trust: Sequence[Identity],
    verify: tuple[str, int],
    client_alert: str,
) -> None:
    outcome = run_memory_handshake(
        client_ctx(files, ident), server_ctx(files, trust=trust), "localhost"
    )
    assert outcome.server_error == "CERTIFICATE_VERIFY_FAILED"
    assert (outcome.server_verify_message, outcome.server_verify_code) == verify
    assert (outcome.client_error, outcome.client_error_phase) == (client_alert, "first_read")


@pytest.mark.unit
def test_openssl_accepts_two_uri_sans_that_x509_svid_forbids(files: Path) -> None:
    two = issue_leaf(PROD_CA, 3650, uris=[ORDERS, BILLING])
    outcome = run_memory_handshake(client_ctx(files, two), server_ctx(files), "localhost")
    assert outcome.server_error is None  # path validation has no opinion on URI count
    assert uri_sans_from_peercert(outcome.peer_dict) == (ORDERS, BILLING)
    with pytest.raises(SvidError, match="found 2"):
        leaf_svid_id(two.cert)


@pytest.mark.unit
def test_uri_name_constraint_admits_ids_inside_the_trust_domain(files: Path) -> None:
    inside = issue_leaf(CONSTRAINED_CA, 3701, uris=["spiffe://prod.example/ns/a"])
    server = server_ctx(files, trust=(PROD_CA, CONSTRAINED_CA))
    outcome = run_memory_handshake(client_ctx(files, inside), server, "localhost")
    assert outcome.server_error is None
    assert uri_sans_from_peercert(outcome.peer_dict) == ("spiffe://prod.example/ns/a",)


@pytest.mark.unit
def test_client_auto_chains_its_trusted_issuer_into_the_certificate_message(
    files: Path,
) -> None:
    # Ed25519 signs CertificateVerify in exactly 64 bytes; ECDSA DER varies by 1-2 bytes.
    ident = issue_leaf(CLIENT_CA, 3801, uris=[ORDERS], use_ed25519=True)
    server = server_ctx(files, trust=(PROD_CA, CLIENT_CA))
    lean_client = client_ctx(files, ident)
    chained_client = client_ctx(files, ident, trust=(PROD_CA, CLIENT_CA))
    # Debian's OpenSSL negotiates RFC 8879 certificate compression, which shrinks the
    # Certificate message and breaks the exact byte arithmetic; the option is bit 32 of
    # SSL_OP_* (OpenSSL include/openssl/ssl.h.in) and CPython has no named constant for it.
    for ctx in (lean_client, chained_client):
        ctx.options |= NO_TX_CERTIFICATE_COMPRESSION
    lean = run_memory_handshake(lean_client, server, "localhost")
    chained = run_memory_handshake(chained_client, server, "localhost")
    assert lean.server_error is None
    assert chained.server_error is None
    ca_der = CLIENT_CA.cert.public_bytes(serialization.Encoding.DER)
    # CertificateEntry = uint24 length + DER + uint16 extensions length.
    assert chained.client_flights[1] - lean.client_flights[1] == len(ca_der) + 5


@pytest.mark.unit
def test_getpeercert_dict_is_empty_when_the_reader_did_not_verify(files: Path) -> None:
    with pytest.raises(SvidError, match="CERT_NONE"):
        uri_sans_from_peercert({})
    cpython_shape: dict[str, object] = {
        "subjectAltName": (("DNS", "localhost"), ("URI", ORDERS), ("IP Address", "127.0.0.1"))
    }
    assert uri_sans_from_peercert(cpython_shape) == (ORDERS,)
    with pytest.raises(SvidError, match="shape"):
        uri_sans_from_peercert({"subjectAltName": [("URI", ORDERS)]})
    ctx = client_ctx(files)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = ctx.wrap_bio(c_in, c_out, server_side=False)
    server = server_ctx(files, mode=ssl.CERT_NONE).wrap_bio(s_in, s_out, server_side=True)
    for _ in range(4):
        for side, out, peer_in in ((client, c_out, s_in), (server, s_out, c_in)):
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
            peer_in.write(out.read())
    assert client.getpeercert() == {}  # the server did send its certificate...
    assert client.getpeercert(binary_form=True) == SERVER.cert.public_bytes(
        serialization.Encoding.DER
    )  # ...and the DER form still exposes it


# --- What the Python servers do with mTLS ------------------------------------------------


@pytest.mark.unit
def test_uvicorn_and_hypercorn_require_client_certs_with_different_verify_flags(
    files: Path,
) -> None:
    cert_path, key_path = write_identity(files, SERVER)
    ca_path = files / "ca.pem"
    ca_path.write_text(PROD_CA.cert_pem())
    uv_ctx = uvicorn_ssl_context(
        cert_path,
        key_path,
        None,
        ssl.PROTOCOL_TLS_SERVER,
        ssl.CERT_REQUIRED,
        ca_path,
        None,
    )
    config = HypercornConfig()
    config.certfile, config.keyfile, config.ca_certs = str(cert_path), str(key_path), str(ca_path)
    config.verify_mode = ssl.CERT_REQUIRED
    hc_ctx = config.create_ssl_context()
    assert hc_ctx is not None
    assert uv_ctx.verify_mode == hc_ctx.verify_mode == ssl.CERT_REQUIRED
    assert uv_ctx.verify_flags & ssl.VERIFY_X509_STRICT == 0  # plain SSLContext(...)
    strict_default = sys.version_info >= (3, 13)  # create_default_context() since 3.13
    assert bool(hc_ctx.verify_flags & ssl.VERIFY_X509_STRICT) is strict_default
    for ctx in (uv_ctx, hc_ctx):
        refused = run_memory_handshake(client_ctx(files), ctx, "localhost")
        assert refused.client_error == "TLSV13_ALERT_CERTIFICATE_REQUIRED"
    no_akid = issue_leaf(PROD_CA, 3901, uris=[ORDERS], authority_key_id=False)
    lenient = run_memory_handshake(client_ctx(files, no_akid), uv_ctx, "localhost")
    assert lenient.server_error is None
    strict = run_memory_handshake(client_ctx(files, no_akid), hc_ctx, "localhost")
    if strict_default:
        assert (strict.server_verify_message, strict.server_verify_code) == (
            "Missing Authority Key Identifier",
            85,
        )
    else:
        assert strict.server_error is None


@pytest.mark.unit
def test_granian_client_verify_without_a_ca_is_silently_disabled(files: Path) -> None:
    cert_path, key_path = write_identity(files, SERVER)
    server = Granian(
        "app:app",
        ssl_cert=cert_path,
        ssl_key=key_path,
        ssl_client_verify=True,
        log_enabled=False,
    )
    ssl_ctx = cast("tuple[Any, ...]", server.ssl_ctx)  # pyright: ignore[reportUnknownMemberType]
    assert (ssl_ctx[0], ssl_ctx[5], ssl_ctx[7]) == (True, None, False)
    ca_path = files / "ca.pem"
    ca_path.write_text(PROD_CA.cert_pem())
    verified = Granian(
        "app:app",
        ssl_cert=cert_path,
        ssl_key=key_path,
        ssl_ca=ca_path,
        ssl_client_verify=True,
        log_enabled=False,
    )
    assert cast("tuple[Any, ...]", verified.ssl_ctx)[7] is True  # pyright: ignore[reportUnknownMemberType]


# --- ASGI authorization ------------------------------------------------------------------


async def whoami(request: Request) -> JSONResponse:
    return JSONResponse({"spiffe_id": request.state.spiffe_id})


def protected_app() -> SpiffeAuthMiddleware:
    inner = Starlette(routes=[Route("/whoami", whoami)])
    return SpiffeAuthMiddleware(inner, bundles=BUNDLES, allowed={ORDERS}, clock=lambda: NOW)


def with_tls_extension(app: ASGIApp, chain: Sequence[Identity]) -> ASGIApp:
    """Stand in for an ASGI server that implements ASGI TLS 0.3."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        tls = {"spec_version": "0.3", "client_cert_chain": [i.cert_pem() for i in chain]}
        await app({**scope, "extensions": {"tls": tls}}, receive, send)

    return wrapped


async def get_whoami(app: ASGIApp) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/whoami")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_admits_an_allowed_svid_and_publishes_request_state() -> None:
    response = await get_whoami(with_tls_extension(protected_app(), [CLIENT]))
    assert response.status_code == 200
    assert response.json() == {"spiffe_id": ORDERS}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain", "detail"),
    [
        ([issue_leaf(PROD_CA, 4001, uris=[BILLING])], "caller not allowed"),
        ([issue_leaf(PARTNER_CA, 4002, uris=[ORDERS])], "client certificate rejected"),
        ([issue_leaf(PROD_CA, 4003, uris=[ORDERS, BILLING])], "client certificate rejected"),
        ([], "client certificate rejected"),
    ],
    ids=["other-workload", "forged-by-partner-ca", "two-uri-sans", "no-certificate"],
)
async def test_middleware_answers_403_without_reaching_the_app(
    chain: list[Identity], detail: str
) -> None:
    response = await get_whoami(with_tls_extension(protected_app(), chain))
    assert response.status_code == 403
    assert response.json() == {"detail": detail}
    assert "www-authenticate" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_fails_closed_when_the_server_omits_the_tls_extension() -> None:
    response = await get_whoami(protected_app())  # ASGITransport sets no "tls" extension
    assert response.status_code == 403


def with_raw_extensions(app: ASGIApp, extensions: object) -> ASGIApp:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        await app({**scope, "extensions": extensions}, receive, send)

    return wrapped


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extensions",
    [
        {"tls": {"client_cert_chain": ["not a pem"]}},
        {"tls": {"client_cert_chain": ["\u00e9"]}},
        {"tls": {"client_cert_chain": [CLIENT.cert_pem().encode()]}},
        {"tls": {"client_cert_chain": CLIENT.cert_pem()}},
        {"tls": {"client_cert_chain": 7}},
        {"tls": {"client_cert_chain": [7]}},
        {"tls": "yes"},
        ["tls"],
        {
            "tls": {
                "client_cert_chain": [
                    duplicate_extension(CLIENT.cert)
                    .public_bytes(serialization.Encoding.PEM)
                    .decode()
                ]
            }
        },
    ],
    ids=[
        "garbage",
        "non-ascii",
        "bytes",
        "bare-string",
        "int-chain",
        "int-entry",
        "tls-not-dict",
        "list",
        "dup-ext",
    ],
)
async def test_middleware_answers_403_not_500_for_malformed_input(extensions: object) -> None:
    response = await get_whoami(with_raw_extensions(protected_app(), extensions))
    assert response.status_code == 403
    assert response.json() == {"detail": "client certificate rejected"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_closes_websockets_and_passes_lifespan_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = SpiffeAuthMiddleware(inner, bundles=BUNDLES, allowed={ORDERS}, clock=lambda: NOW)
    await middleware({"type": "websocket", "extensions": {}}, receive, send)
    await middleware({"type": "lifespan"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert seen == ["lifespan"]


# --- Live Uvicorn over loopback ----------------------------------------------------------


async def scope_probe(scope: Scope, receive: Receive, send: Send) -> None:
    assert scope["type"] == "http"
    body = repr(sorted(scope.get("extensions", {}))).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@pytest.mark.socket
@pytest.mark.asyncio
async def test_uvicorn_verifies_client_certs_but_never_forwards_them(files: Path) -> None:
    cert_path, key_path = write_identity(files, SERVER)
    ca_path = files / "ca.pem"
    ca_path.write_text(PROD_CA.cert_pem())
    results: dict[str, object] = {}
    for name, app in (("probe", scope_probe), ("protected", protected_app())):
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
            ssl_cert_reqs=ssl.CERT_REQUIRED,
            ssl_ca_certs=str(ca_path),
            http="h11",
            lifespan="off",
            log_level="critical",
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        task.result()  # re-raise the startup error
                        pytest.fail("uvicorn exited before starting")
                    await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            url = f"https://127.0.0.1:{port}/whoami"
            async with httpx.AsyncClient(verify=client_ctx(files, CLIENT)) as client:
                response = await client.get(url)
                results[name] = (response.status_code, response.text)
            if name == "probe":
                async with httpx.AsyncClient(verify=client_ctx(files)) as anonymous:
                    with pytest.raises(httpx.HTTPError) as excinfo:
                        await anonymous.get(url)
                    results["anonymous"] = type(excinfo.value).__name__
        finally:
            server.should_exit = True
            await task
    assert results["probe"] == (200, "[]")  # no "tls" extension in the scope
    assert results["protected"] == (403, '{"detail":"client certificate rejected"}')
    assert results["anonymous"] in {"ReadError", "RemoteProtocolError", "ConnectError"}
