"""Server-certificate validation for API clients: chains, SAN matching, validity, stapled OCSP.

Everything runs in memory.
Keys and certificates are generated per call, OCSP responses are built locally, and the
CPython `ssl` handshake runs over `ssl.MemoryBIO` pairs, so no socket or network is used.
"""

from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import ocsp
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence
    from pathlib import Path

CertStatus = Literal["good", "revoked", "unknown"]
StapleFailure = Literal[
    "malformed",
    "not-successful",
    "unauthorized-responder",
    "unsupported-algorithm",
    "bad-signature",
    "cert-id-mismatch",
    "not-yet-valid",
    "stale",
    "expired",
]
Decision = Literal["accept", "accept-unchecked", "reject"]


@dataclass(frozen=True)
class Issued:
    """A certificate together with the private key for its subject public key."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey


class StapleRejected(Exception):
    """A stapled OCSP response failed an RFC 6960 Section 3.2 acceptance check."""

    def __init__(self, reason: StapleFailure) -> None:
        super().__init__(reason)
        self.reason: StapleFailure = reason


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _sign(
    builder: x509.CertificateBuilder,
    key: ec.EllipticCurvePrivateKey,
    issuer: Issued | None,
    *,
    include_aki: bool = True,
) -> x509.Certificate:
    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
    )
    if issuer is not None and include_aki:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key()),
            critical=False,
        )
    signing_key = key if issuer is None else issuer.key
    return builder.sign(signing_key, hashes.SHA256())


def _base(
    common_name: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: Issued | None,
    not_before: dt.datetime,
    not_after: dt.datetime,
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name) if issuer is None else issuer.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )


def make_ca(
    common_name: str,
    *,
    issuer: Issued | None,
    not_before: dt.datetime,
    not_after: dt.datetime,
    path_length: int | None = None,
    permitted_dns: Sequence[str] | None = None,
) -> Issued:
    """Issue a CA certificate; `issuer=None` makes a self-signed root."""
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        _base(common_name, key, issuer, not_before, not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if permitted_dns is not None:
        builder = builder.add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(name) for name in permitted_dns],
                excluded_subtrees=None,
            ),
            critical=True,
        )
    return Issued(_sign(builder, key, issuer), key)


def make_leaf(
    issuer: Issued,
    *,
    sans: Sequence[x509.GeneralName],
    not_before: dt.datetime,
    not_after: dt.datetime,
    common_name: str = "api server",
    must_staple: bool = False,
    include_aki: bool = True,
    ocsp_signer: bool = False,
) -> Issued:
    """Issue an end-entity certificate for TLS server auth, or a delegated OCSP signer."""
    key = ec.generate_private_key(ec.SECP256R1())
    eku = ExtendedKeyUsageOID.OCSP_SIGNING if ocsp_signer else ExtendedKeyUsageOID.SERVER_AUTH
    builder = (
        _base(common_name, key, issuer, not_before, not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    if must_staple:
        builder = builder.add_extension(
            x509.TLSFeature([x509.TLSFeatureType.status_request]), critical=False
        )
    return Issued(_sign(builder, key, issuer, include_aki=include_aki), key)


def reference_identity(reference: str) -> x509.DNSName | x509.IPAddress:
    """Classify the name the client dialed: an IP literal is an IP-ID, anything else a DNS-ID."""
    try:
        return x509.IPAddress(ipaddress.ip_address(reference))
    except ValueError:
        return x509.DNSName(reference)


def verify_server_chain(
    leaf: x509.Certificate,
    intermediates: Sequence[x509.Certificate],
    roots: Sequence[x509.Certificate],
    reference: str,
    at: dt.datetime,
) -> list[x509.Certificate]:
    """Build and validate leaf -> intermediates -> root at time `at`; return the built path."""
    verifier = (
        PolicyBuilder()
        .store(Store(list(roots)))
        .time(at)
        .build_server_verifier(reference_identity(reference))
    )
    return verifier.verify(leaf, list(intermediates))


def dns_id_matches(presented: str, reference: str) -> bool:
    """RFC 9525 Section 6.3 DNS-ID matching for ASCII (A-label) names.

    Labels compare case-insensitively and one by one.
    A presented `*` label is honored only as the complete left-most label, matching exactly one
    reference label; any other `*` can never equal a hostname label, so it never matches.
    The reference is the hostname the client dialed and never contains a wildcard.
    An IP-literal reference is an IP-ID and is never compared with DNS-IDs (Section 6.4).
    """
    if "*" in reference or not (presented.isascii() and reference.isascii()):
        return False
    if isinstance(reference_identity(reference), x509.IPAddress):
        return False
    p_labels = presented.lower().split(".")
    r_labels = reference.lower().split(".")
    if "" in p_labels or "" in r_labels:
        return False
    if p_labels[0] == "*":
        return len(p_labels) > 1 and p_labels[1:] == r_labels[1:]
    return p_labels == r_labels


def chain_not_after(chain: Sequence[x509.Certificate]) -> dt.datetime:
    """A path is only as valid as its earliest-expiring certificate."""
    return min(cert.not_valid_after_utc for cert in chain)


def make_ocsp_response(
    leaf: x509.Certificate,
    issuer: Issued,
    signer: Issued,
    status: CertStatus,
    *,
    this_update: dt.datetime,
    next_update: dt.datetime | None,
    revoked_at: dt.datetime | None = None,
    responder_by_name: bool = False,
) -> bytes:
    """DER-encode a signed OCSP response, as a server would staple it into its handshake."""
    cert_status = {
        "good": ocsp.OCSPCertStatus.GOOD,
        "revoked": ocsp.OCSPCertStatus.REVOKED,
        "unknown": ocsp.OCSPCertStatus.UNKNOWN,
    }[status]
    builder = ocsp.OCSPResponseBuilder().add_response(
        cert=leaf,
        issuer=issuer.cert,
        algorithm=hashes.SHA256(),
        cert_status=cert_status,
        this_update=this_update,
        next_update=next_update,
        revocation_time=revoked_at if status == "revoked" else None,
        revocation_reason=x509.ReasonFlags.key_compromise if status == "revoked" else None,
    )
    encoding = (
        ocsp.OCSPResponderEncoding.NAME if responder_by_name else ocsp.OCSPResponderEncoding.HASH
    )
    builder = builder.responder_id(encoding, signer.cert)
    if signer.cert != issuer.cert:
        builder = builder.certificates([signer.cert])
    response = builder.sign(signer.key, hashes.SHA256())
    return response.public_bytes(serialization.Encoding.DER)


def _is_responder(response: ocsp.OCSPResponse, cert: x509.Certificate) -> bool:
    if response.responder_key_hash is not None:
        key_hash = x509.SubjectKeyIdentifier.from_public_key(cert.public_key()).digest
        return response.responder_key_hash == key_hash
    return response.responder_name == cert.subject


def _authorized_signer(
    response: ocsp.OCSPResponse, issuer: x509.Certificate, at: dt.datetime
) -> x509.Certificate:
    """RFC 6960 Section 4.2.2.2: the issuing CA, or a cert it issued with id-kp-OCSPSigning."""
    if _is_responder(response, issuer):
        return issuer
    for cert in response.certificates:
        if not _is_responder(response, cert):
            continue
        try:
            cert.verify_directly_issued_by(issuer)
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except (ValueError, TypeError, InvalidSignature, x509.ExtensionNotFound):
            continue
        in_validity = cert.not_valid_before_utc <= at <= cert.not_valid_after_utc
        if ExtendedKeyUsageOID.OCSP_SIGNING in eku and in_validity:
            return cert
    raise StapleRejected("unauthorized-responder")


def evaluate_staple(
    der: bytes,
    leaf: x509.Certificate,
    issuer: x509.Certificate,
    *,
    at: dt.datetime,
    max_age: dt.timedelta,
) -> CertStatus:
    """Apply the RFC 6960 Section 3.2 acceptance checks, then report the certificate status.

    The staple is attacker-controlled bytes, so a parse failure is `malformed`, not a crash.
    This lab supports ECDSA signers only; any other key type is `unsupported-algorithm`.
    """
    try:
        response = ocsp.load_der_ocsp_response(der)
    except ValueError:
        raise StapleRejected("malformed") from None
    if response.response_status is not ocsp.OCSPResponseStatus.SUCCESSFUL:
        raise StapleRejected("not-successful")
    signer = _authorized_signer(response, issuer, at)
    public_key = signer.public_key()
    hash_algorithm = response.signature_hash_algorithm
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or hash_algorithm is None:
        raise StapleRejected("unsupported-algorithm")
    try:
        public_key.verify(response.signature, response.tbs_response_bytes, ec.ECDSA(hash_algorithm))
    except InvalidSignature:
        raise StapleRejected("bad-signature") from None
    expected = (
        ocsp.OCSPRequestBuilder().add_certificate(leaf, issuer, response.hash_algorithm).build()
    )
    if (
        response.serial_number != expected.serial_number
        or response.issuer_name_hash != expected.issuer_name_hash
        or response.issuer_key_hash != expected.issuer_key_hash
    ):
        raise StapleRejected("cert-id-mismatch")
    if response.this_update_utc > at:
        raise StapleRejected("not-yet-valid")
    if at - response.this_update_utc > max_age:
        raise StapleRejected("stale")
    next_update = response.next_update_utc
    if next_update is not None and next_update <= at:
        raise StapleRejected("expired")
    status = response.certificate_status
    if status is ocsp.OCSPCertStatus.GOOD:
        return "good"
    if status is ocsp.OCSPCertStatus.REVOKED:
        return "revoked"
    return "unknown"


def requires_staple(leaf: x509.Certificate) -> bool:
    """True when the leaf carries the RFC 7633 TLS Feature extension listing status_request."""
    try:
        features = leaf.extensions.get_extension_for_class(x509.TLSFeature).value
    except x509.ExtensionNotFound:
        return False
    return x509.TLSFeatureType.status_request in features


def revocation_decision(
    leaf: x509.Certificate,
    issuer: x509.Certificate,
    staple: bytes | None,
    *,
    at: dt.datetime,
    max_age: dt.timedelta,
) -> Decision:
    """Client policy for the stapled response (RFC 6066 Section 8, RFC 7633 Section 4.3.3).

    No staple on a must-staple leaf is a hard failure; no staple otherwise is a soft failure
    reported as `accept-unchecked`, which is the default posture of most non-browser clients.
    """
    if staple is None:
        return "reject" if requires_staple(leaf) else "accept-unchecked"
    try:
        status = evaluate_staple(staple, leaf, issuer, at=at, max_age=max_age)
    except StapleRejected:
        return "reject"
    if status == "good":
        return "accept"
    if status == "unknown" and not requires_staple(leaf):
        return "accept-unchecked"
    return "reject"


def pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def client_context(roots: Sequence[x509.Certificate]) -> ssl.SSLContext:
    """What `httpx` 0.28.1 builds by default, with a private root store in place of certifi."""
    return ssl.create_default_context(cadata=b"".join(pem(root) for root in roots).decode())


def server_context(
    chain: Sequence[x509.Certificate], key: ec.EllipticCurvePrivateKey, workdir: Path
) -> ssl.SSLContext:
    """Load leaf-first `chain` and `key` into a TLS server context; the PEM files are removed."""
    chain_path = workdir / "chain.pem"
    key_path = workdir / "key.pem"
    chain_path.write_bytes(b"".join(pem(cert) for cert in chain))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(chain_path, key_path)
    finally:
        chain_path.unlink()
        key_path.unlink()
    return context


def handshake_in_memory(
    client_ctx: ssl.SSLContext,
    server_ctx: ssl.SSLContext,
    server_hostname: str,
    *,
    max_rounds: int = 16,
) -> ssl.SSLObject:
    """Run a full TLS handshake between two `SSLObject`s by shuttling bytes between MemoryBIOs.

    Raises `ssl.SSLCertVerificationError` from the client side when OpenSSL rejects the chain.
    """
    to_client, from_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    to_server, from_server = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_ctx.wrap_bio(to_client, from_client, server_hostname=server_hostname)
    server = server_ctx.wrap_bio(to_server, from_server, server_side=True)
    client_done = server_done = False
    for _ in range(max_rounds):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        to_server.write(from_client.read())
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        to_client.write(from_server.read())
        if client_done and server_done:
            return client
    raise RuntimeError(f"handshake did not finish in {max_rounds} rounds")
