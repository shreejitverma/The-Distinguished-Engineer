"""X.509 chain building, SAN matching, validity windows, and stapled OCSP acceptance."""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import ocsp
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import VerificationError

from api_labs.f02_tls_connections_and_intermediaries.x509_validation import (
    CertStatus,
    Issued,
    StapleRejected,
    chain_not_after,
    client_context,
    dns_id_matches,
    evaluate_staple,
    handshake_in_memory,
    make_ca,
    make_leaf,
    make_ocsp_response,
    reference_identity,
    requires_staple,
    revocation_decision,
    server_context,
    verify_server_chain,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# Fixed validation time for the cryptography verifier and the OCSP checks.
NOW = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)
HOST = "api.example.test"
MAX_AGE = dt.timedelta(days=4)

# OpenSSL include/openssl/x509_vfy.h.in (openssl-4.1.0) verify result codes.
X509_V_ERR_CERT_HAS_EXPIRED = 10
X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY = 20
X509_V_ERR_PATH_LENGTH_EXCEEDED = 25
X509_V_ERR_HOSTNAME_MISMATCH = 62
X509_V_ERR_IP_ADDRESS_MISMATCH = 64
X509_V_ERR_MISSING_AUTHORITY_KEY_IDENTIFIER = 85


@dataclass(frozen=True)
class Pki:
    root: Issued
    intermediate: Issued
    leaf: Issued


def build_pki(
    at: dt.datetime, sans: Sequence[x509.GeneralName] | None = None, *, must_staple: bool = False
) -> Pki:
    root = make_ca("Lab Root", issuer=None, not_before=at - 30 * DAY, not_after=at + 3650 * DAY)
    intermediate = make_ca(
        "Lab Issuing CA",
        issuer=root,
        not_before=at - 30 * DAY,
        not_after=at + 1825 * DAY,
        path_length=0,
    )
    leaf = make_leaf(
        intermediate,
        sans=[x509.DNSName(HOST)] if sans is None else sans,
        not_before=at - DAY,
        not_after=at + 90 * DAY,
        must_staple=must_staple,
    )
    return Pki(root, intermediate, leaf)


@pytest.fixture
def pki() -> Pki:
    return build_pki(NOW)


def verify(pki: Pki, leaf: x509.Certificate, reference: str = HOST) -> list[x509.Certificate]:
    return verify_server_chain(leaf, [pki.intermediate.cert], [pki.root.cert], reference, NOW)


# --- Chain building and path validation (cryptography 50.0.2 verifier) -----------------------


@pytest.mark.unit
def test_valid_chain_builds_leaf_intermediate_root(pki: Pki) -> None:
    path = verify(pki, pki.leaf.cert)
    assert path == [pki.leaf.cert, pki.intermediate.cert, pki.root.cert]


@pytest.mark.unit
def test_missing_intermediate_cannot_reach_the_root(pki: Pki) -> None:
    with pytest.raises(VerificationError, match="candidates exhausted"):
        verify_server_chain(pki.leaf.cert, [], [pki.root.cert], HOST, NOW)


@pytest.mark.unit
def test_untrusted_root_fails_even_with_full_chain(pki: Pki) -> None:
    other_root = make_ca("Other Root", issuer=None, not_before=NOW - DAY, not_after=NOW + DAY)
    with pytest.raises(VerificationError):
        verify_server_chain(
            pki.leaf.cert, [pki.intermediate.cert, pki.root.cert], [other_root.cert], HOST, NOW
        )


@pytest.mark.unit
def test_validity_window_is_inclusive_on_both_ends(pki: Pki) -> None:
    leaf = pki.leaf.cert
    roots, inters = [pki.root.cert], [pki.intermediate.cert]
    second = dt.timedelta(seconds=1)
    for at in (leaf.not_valid_before_utc, leaf.not_valid_after_utc):
        assert verify_server_chain(leaf, inters, roots, HOST, at)[0] == leaf
    for at in (leaf.not_valid_before_utc - second, leaf.not_valid_after_utc + second):
        with pytest.raises(VerificationError, match="not valid at validation time"):
            verify_server_chain(leaf, inters, roots, HOST, at)


@pytest.mark.unit
def test_expired_intermediate_breaks_a_valid_leaf() -> None:
    root = make_ca("Lab Root", issuer=None, not_before=NOW - 400 * DAY, not_after=NOW + 400 * DAY)
    intermediate = make_ca(
        "Short CA", issuer=root, not_before=NOW - 300 * DAY, not_after=NOW + 10 * DAY
    )
    leaf = make_leaf(
        intermediate, sans=[x509.DNSName(HOST)], not_before=NOW - DAY, not_after=NOW + 90 * DAY
    )
    chain = [leaf.cert, intermediate.cert, root.cert]
    assert chain_not_after(chain) == intermediate.cert.not_valid_after_utc
    later = NOW + 20 * DAY
    assert later < leaf.cert.not_valid_after_utc
    verify_server_chain(leaf.cert, [intermediate.cert], [root.cert], HOST, NOW)
    with pytest.raises(VerificationError):
        verify_server_chain(leaf.cert, [intermediate.cert], [root.cert], HOST, later)


@pytest.mark.unit
def test_end_entity_certificate_cannot_issue_certificates(pki: Pki) -> None:
    forged = make_leaf(
        pki.leaf,
        sans=[x509.DNSName("bank.example.test")],
        not_before=NOW - DAY,
        not_after=NOW + DAY,
    )
    with pytest.raises(VerificationError):
        verify_server_chain(
            forged.cert,
            [pki.leaf.cert, pki.intermediate.cert],
            [pki.root.cert],
            "bank.example.test",
            NOW,
        )


@pytest.mark.unit
def test_path_length_zero_blocks_a_second_intermediate(pki: Pki) -> None:
    sub_ca = make_ca("Sub CA", issuer=pki.intermediate, not_before=NOW - DAY, not_after=NOW + DAY)
    leaf = make_leaf(sub_ca, sans=[x509.DNSName(HOST)], not_before=NOW - DAY, not_after=NOW + DAY)
    inters = [sub_ca.cert, pki.intermediate.cert]
    with pytest.raises(VerificationError):
        verify_server_chain(leaf.cert, inters, [pki.root.cert], HOST, NOW)
    unconstrained = make_ca("Open CA", issuer=pki.root, not_before=NOW - DAY, not_after=NOW + DAY)
    sub_ok = make_ca("Sub CA", issuer=unconstrained, not_before=NOW - DAY, not_after=NOW + DAY)
    leaf_ok = make_leaf(
        sub_ok, sans=[x509.DNSName(HOST)], not_before=NOW - DAY, not_after=NOW + DAY
    )
    path = verify_server_chain(
        leaf_ok.cert, [sub_ok.cert, unconstrained.cert], [pki.root.cert], HOST, NOW
    )
    assert len(path) == 4


@pytest.mark.unit
def test_name_constraints_confine_what_an_intermediate_can_issue(pki: Pki) -> None:
    constrained = make_ca(
        "Tenant CA",
        issuer=pki.root,
        not_before=NOW - DAY,
        not_after=NOW + DAY,
        permitted_dns=["example.test"],
    )
    inside = make_leaf(
        constrained, sans=[x509.DNSName(HOST)], not_before=NOW - DAY, not_after=NOW + DAY
    )
    outside = make_leaf(
        constrained,
        sans=[x509.DNSName("login.victim.test")],
        not_before=NOW - DAY,
        not_after=NOW + DAY,
    )
    roots, inters = [pki.root.cert], [constrained.cert]
    verify_server_chain(inside.cert, inters, roots, HOST, NOW)
    with pytest.raises(VerificationError):
        verify_server_chain(outside.cert, inters, roots, "login.victim.test", NOW)


@pytest.mark.unit
def test_common_name_alone_never_identifies_the_server(pki: Pki) -> None:
    cn_only = make_leaf(
        pki.intermediate, sans=[], common_name=HOST, not_before=NOW - DAY, not_after=NOW + DAY
    )
    with pytest.raises(VerificationError):
        verify(pki, cn_only.cert)


# --- SAN matching: RFC 9525 Section 6.3 reference implementation vs cryptography -----------

SAN_CASES = [
    ("api.example.test", "api.example.test", True),
    ("API.Example.TEST", "api.example.test", True),
    ("*.svc.example.test", "orders.svc.example.test", True),
    ("*.svc.example.test", "svc.example.test", False),
    ("*.svc.example.test", "a.b.svc.example.test", False),
    ("api.example.test", "api.example.test.evil.test", False),
    ("api*.example.test", "api1.example.test", False),
    ("*.*.example.test", "a.b.example.test", False),
    ("a.*.example.test", "a.b.example.test", False),
    ("*", "localhost", False),
    ("192.0.2.10", "192.0.2.10", False),
    ("*.0.2.10", "1.0.2.10", False),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("presented", "reference", "expected"),
    SAN_CASES,
    ids=[f"{p}->{r}" for p, r, _ in SAN_CASES],
)
def test_dns_id_matching_agrees_with_cryptography(
    pki: Pki, presented: str, reference: str, expected: bool
) -> None:
    assert dns_id_matches(presented, reference) is expected
    leaf = make_leaf(
        pki.intermediate,
        sans=[x509.DNSName(presented)],
        not_before=NOW - DAY,
        not_after=NOW + DAY,
    )
    try:
        verify(pki, leaf.cert, reference)
        accepted = True
    except VerificationError:
        accepted = False
    assert accepted is expected


@pytest.mark.unit
def test_reference_identifier_is_never_a_wildcard() -> None:
    assert dns_id_matches("*.svc.example.test", "orders.svc.example.test")
    assert not dns_id_matches("*.svc.example.test", "*.svc.example.test")
    assert not dns_id_matches("a.*.example.test", "a.*.example.test")


@pytest.mark.unit
def test_empty_labels_never_match() -> None:
    assert dns_id_matches("api.example.test", "api.example.test")
    assert not dns_id_matches("api.example.test.", "api.example.test.")
    assert not dns_id_matches("api..example.test", "api..example.test")


@pytest.mark.unit
def test_u_labels_must_be_converted_to_a_labels_before_matching() -> None:
    u_label = "b\u00fccher.example.test"
    a_label = u_label.encode("idna").decode("ascii")
    assert a_label == "xn--bcher-kva.example.test"
    assert dns_id_matches(a_label, a_label)
    assert not dns_id_matches(a_label, u_label)
    assert not dns_id_matches(u_label, u_label)
    # U+212A KELVIN SIGN lowercases to ASCII "k": comparing before A-label conversion would let
    # a certificate for key.example.test answer for a different, internationalized name.
    kelvin = "\u212aey.example.test"
    assert kelvin.lower() == "key.example.test"
    assert not dns_id_matches("key.example.test", kelvin)


@pytest.mark.unit
def test_ip_reference_matches_only_an_ip_address_san(pki: Pki) -> None:
    assert reference_identity("192.0.2.10") == x509.IPAddress(ipaddress.ip_address("192.0.2.10"))
    assert reference_identity(HOST) == x509.DNSName(HOST)
    as_dns = make_leaf(
        pki.intermediate,
        sans=[x509.DNSName("192.0.2.10")],
        not_before=NOW - DAY,
        not_after=NOW + DAY,
    )
    as_ip = make_leaf(
        pki.intermediate,
        sans=[x509.IPAddress(ipaddress.ip_address("192.0.2.10"))],
        not_before=NOW - DAY,
        not_after=NOW + DAY,
    )
    with pytest.raises(VerificationError):
        verify(pki, as_dns.cert, "192.0.2.10")
    assert verify(pki, as_ip.cert, "192.0.2.10")[0] == as_ip.cert


# --- Stapled OCSP: RFC 6960 Section 3.2 acceptance and client policy ------------------------


def staple(
    pki: Pki,
    status: CertStatus = "good",
    *,
    signer: Issued | None = None,
    this_update: dt.datetime = NOW - dt.timedelta(hours=6),
    next_update: dt.datetime | None = NOW + dt.timedelta(days=3),
    leaf: x509.Certificate | None = None,
) -> bytes:
    return make_ocsp_response(
        pki.leaf.cert if leaf is None else leaf,
        pki.intermediate,
        pki.intermediate if signer is None else signer,
        status,
        this_update=this_update,
        next_update=next_update,
        revoked_at=NOW - DAY,
    )


def evaluate(pki: Pki, der: bytes) -> CertStatus:
    return evaluate_staple(der, pki.leaf.cert, pki.intermediate.cert, at=NOW, max_age=MAX_AGE)


def der_children(der: bytes) -> list[bytes]:
    """Split the content of the DER constructed value `der` into its full child TLVs."""

    def header(pos: int) -> tuple[int, int]:
        length, pos = der[pos + 1], pos + 2
        if length & 0x80:
            size = length & 0x7F
            length, pos = int.from_bytes(der[pos : pos + size], "big"), pos + size
        return pos, pos + length

    pos, end = header(0)
    children: list[bytes] = []
    while pos < end:
        _, stop = header(pos)
        children.append(der[pos:stop])
        pos = stop
    return children


def der_tlv(tag: int, content: bytes) -> bytes:
    if len(content) < 0x80:
        return bytes([tag, len(content)]) + content
    size = len(content).to_bytes((len(content).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(size)]) + size + content


def single_responses(der: bytes) -> list[bytes]:
    tbs = ocsp.load_der_ocsp_response(der).tbs_response_bytes
    return der_children(next(f for f in der_children(tbs) if f[0] == 0x30))


def restaple(template: bytes, singles: Sequence[bytes], signer: Issued) -> bytes:
    """RFC 6960 Section 4.2.1: re-sign `template` with `singles` as its SEQUENCE OF SingleResponse.

    `OCSPResponseBuilder` emits exactly one SingleResponse, so the DER is rebuilt by hand.
    """
    status, response_bytes = der_children(template)
    oid, octets = der_children(der_children(response_bytes)[0])
    _, sig_alg, _, *certs = der_children(der_children(octets)[0])
    tbs = ocsp.load_der_ocsp_response(template).tbs_response_bytes
    fields = [der_tlv(0x30, b"".join(singles)) if f[0] == 0x30 else f for f in der_children(tbs)]
    new_tbs = der_tlv(0x30, b"".join(fields))
    signature = signer.key.sign(new_tbs, ec.ECDSA(hashes.SHA256()))
    basic = der_tlv(0x30, new_tbs + sig_alg + der_tlv(0x03, b"\x00" + signature) + b"".join(certs))
    inner = der_tlv(0x30, oid + der_tlv(0x04, basic))
    return der_tlv(0x30, status + der_tlv(0xA0, inner))


@pytest.mark.unit
def test_restaple_round_trips_a_single_response(pki: Pki) -> None:
    der = staple(pki, "revoked")
    rebuilt = restaple(der, single_responses(der), pki.intermediate)
    assert ocsp.load_der_ocsp_response(rebuilt).tbs_response_bytes == (
        ocsp.load_der_ocsp_response(der).tbs_response_bytes
    )
    assert evaluate(pki, rebuilt) == "revoked"


@pytest.mark.unit
@pytest.mark.parametrize("leaf_first", [True, False], ids=["leaf-first", "leaf-second"])
def test_multi_response_staple_reports_the_leafs_own_status(pki: Pki, leaf_first: bool) -> None:
    # RFC 6960 Section 4.2.1 allows several SingleResponses; only the leaf's CertID counts.
    sibling = make_leaf(
        pki.intermediate, sans=[x509.DNSName(HOST)], not_before=NOW - DAY, not_after=NOW + DAY
    )
    (own,) = single_responses(staple(pki, "revoked"))
    (other,) = single_responses(staple(pki, "good", leaf=sibling.cert))
    singles = [own, other] if leaf_first else [other, own]
    der = restaple(staple(pki), singles, pki.intermediate)
    assert len(list(ocsp.load_der_ocsp_response(der).responses)) == 2
    assert evaluate(pki, der) == "revoked"
    decision = revocation_decision(
        pki.leaf.cert, pki.intermediate.cert, der, at=NOW, max_age=MAX_AGE
    )
    assert decision == "reject"


@pytest.mark.unit
def test_multi_response_staple_without_the_leaf_is_a_cert_id_mismatch(pki: Pki) -> None:
    others = [
        make_leaf(pki.intermediate, sans=[], not_before=NOW - DAY, not_after=NOW + DAY).cert
        for _ in range(2)
    ]
    singles = [s for leaf in others for s in single_responses(staple(pki, leaf=leaf))]
    der = restaple(staple(pki), singles, pki.intermediate)
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == "cert-id-mismatch"


@pytest.mark.unit
def test_unknown_cert_id_hash_algorithm_is_rejected_not_raised(pki: Pki) -> None:
    # CertID hashAlgorithm id-sha256 (2.16.840.1.101.3.4.2.1) rewritten to the unassigned .99.
    sha256_oid = bytes.fromhex("0609608648016503040201")
    (single,) = single_responses(staple(pki))
    assert single.count(sha256_oid) == 1
    unknown = single.replace(sha256_oid, sha256_oid[:-1] + b"\x63")
    der = restaple(staple(pki), [unknown], pki.intermediate)
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == "unsupported-algorithm"


@pytest.mark.unit
def test_unknown_signature_algorithm_is_rejected_not_raised(pki: Pki) -> None:
    # ecdsa-with-SHA256 (1.2.840.10045.4.3.2) rewritten to the unassigned 1.2.840.10045.4.3.99.
    ecdsa_sha256 = bytes.fromhex("06082a8648ce3d040302")
    der = staple(pki)
    assert der.count(ecdsa_sha256) == 1
    tampered = der.replace(ecdsa_sha256, ecdsa_sha256[:-1] + b"\x63")
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, tampered)
    assert excinfo.value.reason == "unsupported-algorithm"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["good", "revoked", "unknown"])
def test_issuer_signed_staple_reports_its_status(pki: Pki, status: CertStatus) -> None:
    assert evaluate(pki, staple(pki, status)) == status


@pytest.mark.unit
@pytest.mark.parametrize(
    ("this_update", "next_update", "reason"),
    [
        (NOW + dt.timedelta(minutes=5), NOW + 3 * DAY, "not-yet-valid"),
        (NOW - 5 * DAY, NOW + DAY, "stale"),
        (NOW - 2 * DAY, NOW, "expired"),
    ],
    ids=["thisUpdate-in-future", "older-than-max-age", "nextUpdate-passed"],
)
def test_staple_time_window_is_enforced(
    pki: Pki, this_update: dt.datetime, next_update: dt.datetime, reason: str
) -> None:
    der = staple(pki, this_update=this_update, next_update=next_update)
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == reason
    fresh = staple(pki, this_update=NOW - dt.timedelta(hours=1), next_update=NOW + DAY)
    assert evaluate(pki, fresh) == "good"


@pytest.mark.unit
def test_staple_time_window_boundaries_are_inclusive(pki: Pki) -> None:
    second = dt.timedelta(seconds=1)
    assert evaluate(pki, staple(pki, this_update=NOW, next_update=NOW + DAY)) == "good"
    assert evaluate(pki, staple(pki, this_update=NOW - MAX_AGE, next_update=NOW + DAY)) == "good"
    for this_update, reason in ((NOW + second, "not-yet-valid"), (NOW - MAX_AGE - second, "stale")):
        with pytest.raises(StapleRejected) as excinfo:
            evaluate(pki, staple(pki, this_update=this_update, next_update=NOW + DAY))
        assert excinfo.value.reason == reason
    assert evaluate(pki, staple(pki, next_update=NOW + second)) == "good"


@pytest.mark.unit
@pytest.mark.parametrize("by_name", [False, True], ids=["byKey", "byName"])
def test_responder_id_by_key_and_by_name_both_resolve(pki: Pki, by_name: bool) -> None:
    der = make_ocsp_response(
        pki.leaf.cert,
        pki.intermediate,
        pki.intermediate,
        "good",
        this_update=NOW - DAY,
        next_update=NOW + DAY,
        responder_by_name=by_name,
    )
    response = ocsp.load_der_ocsp_response(der)
    assert (response.responder_name is not None) is by_name
    assert (response.responder_key_hash is not None) is not by_name
    assert evaluate(pki, der) == "good"


@pytest.mark.unit
def test_delegated_responder_needs_ocsp_signing_eku(pki: Pki) -> None:
    start, end = NOW - DAY, NOW + 7 * DAY
    delegated = make_leaf(
        pki.intermediate, sans=[], ocsp_signer=True, not_before=start, not_after=end
    )
    tls_cert = make_leaf(
        pki.intermediate, sans=[x509.DNSName("www.example.test")], not_before=start, not_after=end
    )
    assert evaluate(pki, staple(pki, signer=delegated)) == "good"
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, staple(pki, signer=tls_cert))
    assert excinfo.value.reason == "unauthorized-responder"
    by_name = make_ocsp_response(
        pki.leaf.cert,
        pki.intermediate,
        delegated,
        "good",
        this_update=NOW - DAY,
        next_update=NOW + DAY,
        responder_by_name=True,
    )
    assert evaluate(pki, by_name) == "good"


@pytest.mark.unit
def test_expired_delegated_responder_is_not_authorized(pki: Pki) -> None:
    lapsed = make_leaf(
        pki.intermediate, sans=[], ocsp_signer=True, not_before=NOW - 10 * DAY, not_after=NOW - DAY
    )
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, staple(pki, signer=lapsed))
    assert excinfo.value.reason == "unauthorized-responder"


@pytest.mark.unit
def test_responder_certified_by_another_ca_is_not_authorized(pki: Pki) -> None:
    other = build_pki(NOW)
    foreign = make_leaf(
        other.intermediate, sans=[], ocsp_signer=True, not_before=NOW - DAY, not_after=NOW + DAY
    )
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, staple(pki, signer=foreign))
    assert excinfo.value.reason == "unauthorized-responder"


@pytest.mark.unit
def test_tampered_signature_is_rejected(pki: Pki) -> None:
    good = staple(pki)
    signature = ocsp.load_der_ocsp_response(good).signature
    assert good.count(signature) == 1
    tampered = good.replace(signature, signature[:-1] + bytes([signature[-1] ^ 0x01]))
    assert evaluate(pki, good) == "good"
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, tampered)
    assert excinfo.value.reason == "bad-signature"


@pytest.mark.unit
def test_staple_for_a_different_certificate_is_rejected(pki: Pki) -> None:
    sibling = make_leaf(
        pki.intermediate, sans=[x509.DNSName(HOST)], not_before=NOW - DAY, not_after=NOW + DAY
    )
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, staple(pki, leaf=sibling.cert))
    assert excinfo.value.reason == "cert-id-mismatch"


@pytest.mark.unit
def test_same_serial_under_another_issuer_is_rejected(pki: Pki) -> None:
    # Serials are unique only per issuer: the CertID must also bind the issuer name and key.
    other_ca = make_ca("Other CA", issuer=pki.root, not_before=NOW - DAY, not_after=NOW + DAY)
    der = make_ocsp_response(
        pki.leaf.cert,
        other_ca,
        pki.intermediate,
        "good",
        this_update=NOW - DAY,
        next_update=NOW + DAY,
    )
    response = ocsp.load_der_ocsp_response(der)
    assert response.serial_number == pki.leaf.cert.serial_number
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == "cert-id-mismatch"


@pytest.mark.unit
def test_issuer_name_hash_is_part_of_the_cert_id(pki: Pki) -> None:
    # issuerNameHash hashes the leaf's issuer DN, so a twin leaf with the same serial and the
    # same issuing key but another issuer DN differs from the real leaf in issuerNameHash only.
    twin = (
        x509.CertificateBuilder()
        .subject_name(pki.leaf.cert.subject)
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Renamed CA")]))
        .public_key(pki.leaf.key.public_key())
        .serial_number(pki.leaf.cert.serial_number)
        .not_valid_before(NOW - DAY)
        .not_valid_after(NOW + DAY)
        .sign(pki.intermediate.key, hashes.SHA256())
    )
    der = staple(pki, leaf=twin)
    response = ocsp.load_der_ocsp_response(der)
    request = (
        ocsp.OCSPRequestBuilder()
        .add_certificate(pki.leaf.cert, pki.intermediate.cert, hashes.SHA256())
        .build()
    )
    assert response.serial_number == request.serial_number
    assert response.issuer_key_hash == request.issuer_key_hash
    assert response.issuer_name_hash != request.issuer_name_hash
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == "cert-id-mismatch"


@pytest.mark.unit
@pytest.mark.parametrize("der", [b"garbage", b"\x30\x03\x0a\x01\x00"], ids=["not-der", "no-body"])
def test_malformed_staple_is_rejected_not_raised(pki: Pki, der: bytes) -> None:
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == "malformed"
    decision = revocation_decision(
        pki.leaf.cert, pki.intermediate.cert, der, at=NOW, max_age=MAX_AGE
    )
    assert decision == "reject"


@pytest.mark.unit
def test_unsuccessful_response_status_is_rejected(pki: Pki) -> None:
    der = ocsp.OCSPResponseBuilder.build_unsuccessful(
        ocsp.OCSPResponseStatus.TRY_LATER
    ).public_bytes(serialization.Encoding.DER)
    with pytest.raises(StapleRejected) as excinfo:
        evaluate(pki, der)
    assert excinfo.value.reason == "not-successful"


@pytest.mark.unit
def test_must_staple_turns_a_missing_staple_into_a_hard_failure() -> None:
    plain = build_pki(NOW)
    stapled = build_pki(NOW, must_staple=True)
    assert not requires_staple(plain.leaf.cert)
    assert requires_staple(stapled.leaf.cert)

    def decide(p: Pki, der: bytes | None) -> str:
        return revocation_decision(p.leaf.cert, p.intermediate.cert, der, at=NOW, max_age=MAX_AGE)

    assert decide(plain, None) == "accept-unchecked"
    assert decide(stapled, None) == "reject"
    assert decide(plain, staple(plain)) == "accept"
    assert decide(stapled, staple(stapled)) == "accept"
    assert decide(plain, staple(plain, "revoked")) == "reject"
    assert decide(stapled, staple(stapled, "revoked")) == "reject"
    assert decide(plain, staple(plain, "unknown")) == "accept-unchecked"
    assert decide(stapled, staple(stapled, "unknown")) == "reject"
    assert decide(plain, staple(plain, next_update=NOW - DAY)) == "reject"


# --- CPython ssl over MemoryBIO: what httpx and urllib actually enforce ---------------------


@pytest.fixture
def live_pki() -> Pki:
    # OpenSSL validates against the wall clock, so these certificates are anchored to now.
    return build_pki(dt.datetime.now(dt.UTC).replace(microsecond=0))


def tls(
    tmp_path: Path,
    pki: Pki,
    leaf: Issued,
    chain: Sequence[x509.Certificate],
    hostname: str = HOST,
    client: ssl.SSLContext | None = None,
) -> ssl.SSLObject:
    server = server_context([leaf.cert, *chain], leaf.key, tmp_path)
    return handshake_in_memory(client or client_context([pki.root.cert]), server, hostname)


def verify_code(
    tmp_path: Path,
    pki: Pki,
    leaf: Issued,
    chain: Sequence[x509.Certificate],
    hostname: str = HOST,
    client: ssl.SSLContext | None = None,
) -> int:
    with pytest.raises(ssl.SSLCertVerificationError) as excinfo:
        tls(tmp_path, pki, leaf, chain, hostname, client)
    return int(excinfo.value.verify_code)


@pytest.mark.unit
def test_default_context_flags_follow_the_python_version() -> None:
    ctx = ssl.create_default_context()
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.verify_flags & ssl.VERIFY_X509_TRUSTED_FIRST
    strict = bool(ctx.verify_flags & ssl.VERIFY_X509_STRICT)
    partial = bool(ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN)
    assert strict is partial is (sys.version_info >= (3, 13))


@pytest.mark.unit
def test_httpx_default_verification_is_cpython_default_context() -> None:
    default = httpx.create_ssl_context()
    assert default.verify_mode is ssl.CERT_REQUIRED
    assert default.check_hostname is True
    assert default.verify_flags == ssl.create_default_context().verify_flags
    disabled = httpx.create_ssl_context(verify=False)
    assert disabled.verify_mode is ssl.CERT_NONE
    assert disabled.check_hostname is False


@pytest.mark.unit
def test_handshake_succeeds_and_exposes_the_san(tmp_path: Path, live_pki: Pki) -> None:
    client = tls(tmp_path, live_pki, live_pki.leaf, [live_pki.intermediate.cert])
    assert client.version() == "TLSv1.3"
    peer = client.getpeercert()
    assert peer is not None
    assert peer["subjectAltName"] == (("DNS", HOST),)


@pytest.mark.unit
def test_openssl_rejections_carry_specific_verify_codes(tmp_path: Path, live_pki: Pki) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    inter = live_pki.intermediate
    chain = [inter.cert]

    def leaf(*names: x509.GeneralName, start: dt.datetime = now - DAY) -> Issued:
        return make_leaf(inter, sans=list(names), not_before=start, not_after=start + 2 * DAY)

    good = live_pki.leaf
    wildcard = leaf(x509.DNSName("*.svc.example.test"))
    expired = leaf(x509.DNSName(HOST), start=now - 3 * DAY)
    ip_as_dns = leaf(x509.DNSName("192.0.2.10"))
    ip_as_ip = leaf(x509.IPAddress(ipaddress.ip_address("192.0.2.10")))
    sub_ca = make_ca("Sub CA", issuer=inter, not_before=now - DAY, not_after=now + DAY)
    deep = make_leaf(sub_ca, sans=[x509.DNSName(HOST)], not_before=now - DAY, not_after=now + DAY)

    assert verify_code(tmp_path, live_pki, good, chain, "other.example.test") == (
        X509_V_ERR_HOSTNAME_MISMATCH
    )
    assert verify_code(tmp_path, live_pki, good, []) == (
        X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY
    )
    assert verify_code(tmp_path, live_pki, expired, chain) == X509_V_ERR_CERT_HAS_EXPIRED
    assert tls(tmp_path, live_pki, wildcard, chain, "orders.svc.example.test")
    assert verify_code(tmp_path, live_pki, wildcard, chain, "a.b.svc.example.test") == (
        X509_V_ERR_HOSTNAME_MISMATCH
    )
    assert verify_code(tmp_path, live_pki, ip_as_dns, chain, "192.0.2.10") == (
        X509_V_ERR_IP_ADDRESS_MISMATCH
    )
    assert tls(tmp_path, live_pki, ip_as_ip, chain, "192.0.2.10")
    assert verify_code(tmp_path, live_pki, deep, [sub_ca.cert, inter.cert]) == (
        X509_V_ERR_PATH_LENGTH_EXCEEDED
    )


@pytest.mark.unit
def test_missing_aki_is_rejected_only_under_x509_strict(tmp_path: Path, live_pki: Pki) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    no_aki = make_leaf(
        live_pki.intermediate,
        sans=[x509.DNSName(HOST)],
        not_before=now - DAY,
        not_after=now + DAY,
        include_aki=False,
    )
    chain = [live_pki.intermediate.cert]
    if sys.version_info >= (3, 13):
        assert verify_code(tmp_path, live_pki, no_aki, chain) == (
            X509_V_ERR_MISSING_AUTHORITY_KEY_IDENTIFIER
        )
    else:
        assert tls(tmp_path, live_pki, no_aki, chain)
    relaxed = client_context([live_pki.root.cert])
    relaxed.verify_flags &= ~ssl.VERIFY_X509_STRICT
    assert tls(tmp_path, live_pki, no_aki, chain, client=relaxed)


@pytest.mark.unit
def test_common_name_fallback_depends_on_the_linked_openssl(tmp_path: Path, live_pki: Pki) -> None:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    cn_only = make_leaf(
        live_pki.intermediate, sans=[], common_name=HOST, not_before=now - DAY, not_after=now + DAY
    )
    chain = [live_pki.intermediate.cert]
    default = client_context([live_pki.root.cert])
    assert default.hostname_checks_common_name is True
    if ssl.OPENSSL_VERSION_INFO >= (4, 1, 0):
        assert verify_code(tmp_path, live_pki, cn_only, chain) == X509_V_ERR_HOSTNAME_MISMATCH
    else:
        assert tls(tmp_path, live_pki, cn_only, chain)
    san_only = client_context([live_pki.root.cert])
    san_only.hostname_checks_common_name = False
    assert verify_code(tmp_path, live_pki, cn_only, chain, client=san_only) == (
        X509_V_ERR_HOSTNAME_MISMATCH
    )
