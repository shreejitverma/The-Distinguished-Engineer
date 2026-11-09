"""Mutual TLS client authentication, SPKI pinning, and SPIFFE X.509-SVID identities.

Pure functions parse SPIFFE IDs (SPIFFE-ID Section 2), validate X.509-SVID leaves and chains
(X509-SVID Sections 2, 4, 5), and compute RFC 7469 SPKI pins and RFC 8705 `x5t#S256`
thumbprints. `run_memory_handshake` drives a real OpenSSL client and server through
`ssl.MemoryBIO` pairs so every outcome is observable without sockets, and
`SpiffeAuthMiddleware` authorizes ASGI requests from the ASGI TLS extension's
`client_cert_chain`.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from starlette.types import ASGIApp, Receive, Scope, Send

MAX_SPIFFE_ID_BYTES = 2048  # SPIFFE-ID Section 2.3: MUST support, SHOULD NOT generate longer
MAX_TRUST_DOMAIN_BYTES = 255  # SPIFFE-ID Section 2.3, from the RFC 3986 host limit
_TRUST_DOMAIN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_")
_SEGMENT_CHARS = _TRUST_DOMAIN_CHARS | frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_PREFIX = "spiffe://"


class SpiffeIdError(ValueError):
    """The string is not a valid SPIFFE ID."""


class SvidError(ValueError):
    """The certificate or chain is not an acceptable X.509-SVID."""


@dataclass(frozen=True, slots=True)
class SpiffeId:
    trust_domain: str
    path: str  # "" for a trust domain's root ID, else "/seg/seg"

    def __str__(self) -> str:
        return f"{_PREFIX}{self.trust_domain}{self.path}"


def parse_spiffe_id(uri: str) -> SpiffeId:
    """Parse *uri* strictly; only the canonical lowercase form is accepted."""
    if len(uri.encode("utf-8")) > MAX_SPIFFE_ID_BYTES:
        raise SpiffeIdError(f"longer than {MAX_SPIFFE_ID_BYTES} bytes")
    if not uri.startswith(_PREFIX):
        raise SpiffeIdError("scheme must be 'spiffe' followed by '://'")
    rest = uri[len(_PREFIX) :]
    if "?" in rest or "#" in rest:
        raise SpiffeIdError("query and fragment are not allowed")
    authority, slash, tail = rest.partition("/")
    if not authority:
        raise SpiffeIdError("trust domain is empty")
    if "@" in authority or ":" in authority:
        raise SpiffeIdError("userinfo and port are not allowed")
    if len(authority) > MAX_TRUST_DOMAIN_BYTES:
        raise SpiffeIdError(f"trust domain longer than {MAX_TRUST_DOMAIN_BYTES} bytes")
    if bad := set(authority) - _TRUST_DOMAIN_CHARS:
        raise SpiffeIdError(f"trust domain characters not in [a-z0-9.-_]: {sorted(bad)}")
    path = slash + tail
    for segment in path.split("/")[1:]:
        if segment == "":
            raise SpiffeIdError("empty path segment or trailing '/'")
        if segment in {".", ".."}:
            raise SpiffeIdError("relative path segments are not allowed")
        if bad := set(segment) - _SEGMENT_CHARS:
            raise SpiffeIdError(f"path characters not in [a-zA-Z0-9.-_]: {sorted(bad)}")
    return SpiffeId(authority, path)


def leaf_svid_id(cert: x509.Certificate) -> SpiffeId:
    """Apply the X509-SVID leaf rules (Sections 2 and 5.2) and return the SPIFFE ID."""
    try:
        extensions = cert.extensions  # parsed lazily; malformed DER raises here
        san = extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    except x509.ExtensionNotFound:
        raise SvidError("no subjectAltName extension") from None
    except (x509.DuplicateExtension, x509.UnsupportedGeneralNameType) as exc:
        raise SvidError(f"malformed extensions: {exc}") from exc
    if len(uris) != 1:
        raise SvidError(f"an X.509-SVID has exactly one URI SAN, found {len(uris)}")
    try:
        basic = extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        basic = None  # RFC 5280 Section 4.2.1.9: cA defaults to FALSE
    if basic is not None and basic.ca:
        raise SvidError("cA is true: signing certificates must not authenticate")
    try:
        usage = extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        usage = None
    if usage is not None and usage.key_cert_sign:
        raise SvidError("keyCertSign is set on a leaf")
    if usage is not None and usage.crl_sign:
        raise SvidError("cRLSign is set on a leaf")
    try:
        spiffe_id = parse_spiffe_id(uris[0])
    except SpiffeIdError as exc:
        raise SvidError(f"URI SAN is not a SPIFFE ID: {exc}") from exc
    if not spiffe_id.path:
        raise SvidError("a leaf SPIFFE ID must have a non-root path")
    return spiffe_id


@dataclass(frozen=True, slots=True)
class VerifiedSvid:
    """A SPIFFE ID plus the chain the verifier built, leaf first and trust anchor last."""

    spiffe_id: SpiffeId
    chain: tuple[x509.Certificate, ...]


def verify_svid_chain(
    chain: Sequence[x509.Certificate],
    bundles: Mapping[str, Sequence[x509.Certificate]],
    *,
    at: dt.datetime,
) -> VerifiedSvid:
    """Verify *chain* (leaf first) against the bundle of the leaf's own trust domain only.

    Picking the bundle by the claimed trust domain is what makes the issuer authorized for
    that name (X509-SVID Section 5.2); one merged trust store would let any federated CA
    mint IDs in every trust domain.
    """
    if not chain:
        raise SvidError("no client certificate")
    leaf, *intermediates = chain
    spiffe_id = leaf_svid_id(leaf)
    anchors = bundles.get(spiffe_id.trust_domain)
    if not anchors:
        raise SvidError(f"no trust bundle for trust domain {spiffe_id.trust_domain!r}")
    verifier = PolicyBuilder().store(Store(list(anchors))).time(at).build_client_verifier()
    try:
        verified = verifier.verify(leaf, intermediates)
    except VerificationError as exc:
        raise SvidError(f"chain does not verify against {spiffe_id.trust_domain!r}") from exc
    return VerifiedSvid(spiffe_id, tuple(verified.chain))


def uri_sans_from_peercert(peercert: Mapping[str, object] | None) -> tuple[str, ...]:
    """URI SANs from `SSLSocket.getpeercert()`, refusing the two silent-empty cases."""
    if peercert is None:
        raise SvidError("the peer sent no certificate")
    if not peercert:
        raise SvidError("getpeercert() returned {}: verify_mode is CERT_NONE")
    names = peercert.get("subjectAltName", ())
    if not isinstance(names, tuple):
        raise SvidError("unexpected subjectAltName shape")
    uris: list[str] = []
    for entry in names:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(entry, tuple) and len(entry) == 2 and entry[0] == "URI":  # pyright: ignore[reportUnknownArgumentType]
            uris.append(str(entry[1]))  # pyright: ignore[reportUnknownArgumentType]
    return tuple(uris)


def spki_sha256_pin(cert: x509.Certificate) -> str:
    """RFC 7469 Section 2.4: base64(SHA-256(DER SubjectPublicKeyInfo))."""
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def chain_matches_pins(verified: VerifiedSvid, pins: Iterable[str]) -> bool:
    """RFC 7469 Section 2.6: pass when any SPKI of the *validated* chain is pinned.

    It takes a `VerifiedSvid`, not a presented chain: CA certificates are public, so a
    forger can append the pinned CA to any chain it sends.
    """
    wanted = frozenset(pins)
    return any(spki_sha256_pin(cert) in wanted for cert in verified.chain)


def x5t_s256(cert: x509.Certificate) -> str:
    """RFC 8705 Section 3.1: base64url(SHA-256(DER certificate)) without '=' padding."""
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def token_bound_to(claims: Mapping[str, object], cert: x509.Certificate) -> bool:
    """True when the token's `cnf.x5t#S256` names this exact certificate (RFC 8705 Section 3)."""
    cnf = claims.get("cnf")
    if not isinstance(cnf, dict):
        return False
    thumbprint = cnf.get("x5t#S256")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(thumbprint, str):
        return False
    return hmac.compare_digest(thumbprint.encode(), x5t_s256(cert).encode())


# --- A deterministic private PKI for the lab ---------------------------------------------

LAB_NOT_BEFORE = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
LAB_NOT_AFTER = dt.datetime(2036, 1, 1, tzinfo=dt.UTC)
_SIGN_KU = x509.KeyUsage(True, False, False, False, False, False, False, False, False)
_CA_KU = x509.KeyUsage(False, False, False, False, False, True, True, False, False)
_BOTH_EKU = (ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH)


def lab_key(seed: int) -> ec.EllipticCurvePrivateKey:
    """P-256 key derived from *seed*, so every run uses the same key pairs."""
    return ec.derive_private_key(seed, ec.SECP256R1())


def lab_ed25519_key(seed: int) -> ed25519.Ed25519PrivateKey:
    """Ed25519 key from *seed*: its TLS signatures are always 64 bytes."""
    return ed25519.Ed25519PrivateKey.from_private_bytes(seed.to_bytes(32, "big"))


@dataclass(frozen=True, slots=True)
class Identity:
    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey

    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def key_pem(self) -> bytes:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )


def make_ca(
    name: str,
    seed: int,
    *,
    spiffe_id: str | None = None,
    uri_constraint: str | None = None,
    issuer: Identity | None = None,
) -> Identity:
    """Signing certificate (X509-SVID Section 3.2): self-signed, or an intermediate of *issuer*."""
    key = lab_key(seed)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject if issuer is None else issuer.cert.subject)
        .public_key(key.public_key())
        .serial_number(seed)
        .not_valid_before(LAB_NOT_BEFORE)
        .not_valid_after(LAB_NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_CA_KU, critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if spiffe_id is not None:
        san = x509.SubjectAlternativeName([x509.UniformResourceIdentifier(spiffe_id)])
        builder = builder.add_extension(san, critical=False)
    if uri_constraint is not None:
        permitted = [x509.UniformResourceIdentifier(uri_constraint)]
        builder = builder.add_extension(x509.NameConstraints(permitted, None), critical=True)
    if issuer is None:
        return Identity(builder.sign(key, hashes.SHA256()), key)
    akid = x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key())
    builder = builder.add_extension(akid, critical=False)
    return Identity(builder.sign(issuer.key, _signature_hash(issuer)), key)


def issue_leaf(
    issuer: Identity,
    seed: int,
    *,
    uris: Sequence[str] = (),
    dns: Sequence[str] = (),
    ips: Sequence[str] = (),
    eku: Sequence[x509.ObjectIdentifier] = _BOTH_EKU,
    ca: bool = False,
    usage: x509.KeyUsage | None = None,
    authority_key_id: bool = True,
    use_ed25519: bool = False,
    serial: int | None = None,
    not_before: dt.datetime = LAB_NOT_BEFORE,
    not_after: dt.datetime = LAB_NOT_AFTER,
) -> Identity:
    """Leaf X.509-SVID with an empty Subject, so the SAN is critical (X509-SVID 3.1)."""
    key = lab_ed25519_key(seed) if use_ed25519 else lab_key(seed)
    names: list[x509.GeneralName] = [x509.UniformResourceIdentifier(u) for u in uris]
    names += [x509.DNSName(d) for d in dns]
    names += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(issuer.cert.subject)
        .public_key(key.public_key())
        .serial_number(serial if serial is not None else seed)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(usage or (_CA_KU if ca else _SIGN_KU), critical=True)
        .add_extension(x509.SubjectAlternativeName(names), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if authority_key_id:
        akid = x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key())
        builder = builder.add_extension(akid, critical=False)
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
    return Identity(builder.sign(issuer.key, _signature_hash(issuer)), key)


def _signature_hash(issuer: Identity) -> hashes.SHA256 | None:
    return None if isinstance(issuer.key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


# --- Driving a real OpenSSL handshake in memory ------------------------------------------


@dataclass(frozen=True, slots=True)
class HandshakeOutcome:
    version: str | None  # negotiated version, None if the server aborted
    client_handshake_ok: bool  # did the client's do_handshake() return?
    client_error: str | None  # SSLError.reason the client saw, in handshake or first read
    client_error_phase: str | None  # "handshake" or "first_read"
    server_error: str | None
    server_verify_message: str | None
    server_verify_code: int | None  # X509_V_ERR_* number, stable across OpenSSL majors
    client_flights: tuple[int, ...]  # bytes per client flight
    server_received: bytes  # application data the server decrypted
    peer_der: bytes | None  # server-side getpeercert(binary_form=True)
    peer_dict: Mapping[str, object] | None  # server-side getpeercert()


def _reason(exc: ssl.SSLError) -> str:
    return str(exc.reason) if exc.reason else type(exc).__name__


def run_memory_handshake(
    client_ctx: ssl.SSLContext,
    server_ctx: ssl.SSLContext,
    server_hostname: str,
    *,
    probe: bytes = b"GET / HTTP/1.1\r\nHost: api\r\n\r\n",
) -> HandshakeOutcome:
    """Handshake, then send *probe* from client to server and read one reply byte."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_side=False, server_hostname=server_hostname)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    client_ok = server_ok = False
    client_error = server_error = verify_message = phase = None
    verify_code: int | None = None
    flights: list[int] = []
    for _ in range(16):
        if not client_ok and client_error is None:
            try:
                client.do_handshake()
                client_ok = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as exc:
                client_error, phase = _reason(exc), "handshake"
        if data := c_out.read():
            flights.append(len(data))
            s_in.write(data)
        if not server_ok and server_error is None:
            try:
                server.do_handshake()
                server_ok = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLError as exc:
                server_error = _reason(exc)
                if isinstance(exc, ssl.SSLCertVerificationError):
                    verify_message, verify_code = exc.verify_message, exc.verify_code
        if data := s_out.read():
            c_in.write(data)
        if (client_ok or client_error) and (server_ok or server_error):
            break
    else:
        raise RuntimeError("handshake did not converge")
    received = b""
    if client_ok:
        client.write(probe)
        s_in.write(c_out.read())
        if server_ok:
            received = server.read(len(probe))
            server.write(b"H")
            c_in.write(s_out.read())
        try:
            client.read(1)
        except ssl.SSLError as exc:
            client_error, phase = _reason(exc), "first_read"
    return HandshakeOutcome(
        version=client.version() if server_ok else None,
        client_handshake_ok=client_ok,
        client_error=client_error,
        client_error_phase=phase,
        server_error=server_error,
        server_verify_message=verify_message,
        server_verify_code=verify_code,
        client_flights=tuple(flights),
        server_received=received,
        peer_der=server.getpeercert(binary_form=True) if server_ok else None,
        peer_dict=server.getpeercert() if server_ok else None,
    )


# --- ASGI authorization from the ASGI TLS extension --------------------------------------


class SpiffeAuthMiddleware:
    """Authorize each connection scope by the SPIFFE ID of its verified client certificate.

    It re-verifies `scope["extensions"]["tls"]["client_cert_chain"]` instead of trusting the
    server, because ASGI TLS 0.3 does not say whether that chain was verified; an absent
    extension fails closed. The ID is published as `request.state.spiffe_id`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        bundles: Mapping[str, Sequence[x509.Certificate]],
        allowed: Iterable[str],
        clock: Callable[[], dt.datetime],
    ) -> None:
        self.app = app
        self.bundles = bundles
        self.allowed = frozenset(allowed)
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        try:
            pems = _client_cert_chain(scope)
            chain = [x509.load_pem_x509_certificate(pem.encode("ascii")) for pem in pems]
            spiffe_id = verify_svid_chain(chain, self.bundles, at=self.clock()).spiffe_id
        except ValueError:  # SvidError, a non-ASCII or unparseable PEM, a malformed scope
            await self._deny(scope, receive, send, "client certificate rejected")
            return
        if str(spiffe_id) not in self.allowed:
            await self._deny(scope, receive, send, "caller not allowed")
            return
        scope.setdefault("state", {})["spiffe_id"] = str(spiffe_id)
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})  # policy violation
            return
        # 403, not 401: RFC 9110 Section 15.5.2 requires WWW-Authenticate on 401 and no HTTP
        # auth scheme names a TLS client certificate.
        await JSONResponse({"detail": detail}, status_code=403)(scope, receive, send)


def _client_cert_chain(scope: Scope) -> tuple[str, ...]:
    """`scope["extensions"]["tls"]["client_cert_chain"]`, or `()`; ValueError if malformed."""
    extensions: object = scope.get("extensions") or {}
    if not isinstance(extensions, dict):
        raise ValueError("scope extensions are not a mapping")
    tls: object = cast("dict[str, object]", extensions).get("tls") or {}
    if not isinstance(tls, dict):
        raise ValueError("the tls extension is not a mapping")
    chain = cast("dict[str, object]", tls).get("client_cert_chain", ())
    if not isinstance(chain, (list, tuple)):
        raise ValueError("client_cert_chain is not a sequence")
    items = cast("Sequence[object]", chain)
    pems = tuple(pem for pem in items if isinstance(pem, str))
    if len(pems) != len(items):
        raise ValueError("client_cert_chain entries must be PEM strings")
    return pems
