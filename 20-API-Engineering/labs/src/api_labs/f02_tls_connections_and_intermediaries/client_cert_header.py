"""RFC 9440 Client-Cert and Client-Cert-Chain: serialize, parse, sanitize, and trust-gate.

The TTRP side (`ttrp_forward_headers`) strips every client-supplied copy and inserts the
certificate from its own mTLS handshake (RFC 9440 Section 2.4).  The origin side
(`ClientCertGuard`) accepts the fields only from a peer address inside the proxy CIDRs
(RFC 9440 Section 4), parses them as RFC 9651 Structured Fields, and hands the app parsed
certificates in `scope["state"]["client_cert"]` instead of raw header bytes.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography import x509
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from starlette.types import ASGIApp, Receive, Scope, Send

CLIENT_CERT = b"client-cert"
CLIENT_CERT_CHAIN = b"client-cert-chain"
CLIENT_CERT_FIELDS = frozenset({CLIENT_CERT, CLIENT_CERT_CHAIN})
STATE_KEY = "client_cert"

_OWS = " \t"

type Header = tuple[bytes, bytes]
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class ClientCertError(ValueError):
    """A Client-Cert or Client-Cert-Chain field that must not be used."""


# --- RFC 9651 Byte Sequence and List of Byte Sequences --------------------------------------


def serialize_byte_sequence(data: bytes) -> str:
    """RFC 9651 Section 4.1.8: ":" + padded base64 + ":"."""
    return ":" + base64.b64encode(data).decode("ascii") + ":"


def serialize_client_cert(leaf_der: bytes) -> bytes:
    """Client-Cert field value: one Byte Sequence holding the DER leaf (RFC 9440 Section 2.2)."""
    return serialize_byte_sequence(leaf_der).encode("ascii")


def serialize_client_cert_chain(chain_der: Sequence[bytes]) -> bytes:
    """Client-Cert-Chain field value: a List of Byte Sequences, members joined by ", "."""
    return ", ".join(serialize_byte_sequence(der) for der in chain_der).encode("ascii")


_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


def pem_to_byte_sequence(pem: str) -> str:
    """RFC 9440 Section 2.1 shortcut for exactly one certificate: armor -> ":", drop breaks.

    Anything else (a second block, a private key, `openssl x509 -subject` text) raises, so a
    combined cert-and-key file can never put the key into a request header.
    """
    lines = [raw.strip() for raw in pem.strip().splitlines()]
    body = lines[1:-1]
    if len(lines) < 3 or lines[0] != _PEM_BEGIN or lines[-1] != _PEM_END:
        raise ClientCertError("expected exactly one PEM CERTIFICATE block")
    if any(line.startswith("-----") for line in body):
        raise ClientCertError("expected exactly one PEM CERTIFICATE block")
    return ":" + "".join(body) + ":"


def _parse_byte_sequence(text: str, pos: int) -> tuple[bytes, int]:
    """RFC 9651 Section 4.2.7, starting at text[pos] == ":"; returns (bytes, next position)."""
    if pos >= len(text) or text[pos] != ":":
        raise ClientCertError("not a Byte Sequence")
    end = text.find(":", pos + 1)
    if end == -1:
        raise ClientCertError("Byte Sequence has no closing ':'")
    content = text[pos + 1 : end]
    padded = content + "=" * (-len(content) % 4)  # synthesize missing padding
    try:
        # strict_mode rejects characters outside the alphabet, line feeds, and misplaced "=",
        # yet accepts non-zero pad bits, matching RFC 9651 Section 4.2.7 steps 6 and 7.
        return binascii.a2b_base64(padded, strict_mode=True), end + 1
    except binascii.Error as exc:
        raise ClientCertError(f"base64 decoding failed: {exc}") from exc


def _reject_parameters(text: str, pos: int) -> None:
    # RFC 9440 defines no parameters; this lab fails closed rather than ship a partial parser.
    if pos < len(text) and text[pos] == ";":
        raise ClientCertError("parameters are not supported by this parser")


def _to_ascii(field_value: bytes) -> str:
    try:
        return field_value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ClientCertError("field value is not ASCII") from exc


def parse_byte_sequence_item(field_value: bytes) -> bytes:
    """RFC 9651 Section 4.2 with field_type "item", constrained to a bare Byte Sequence."""
    text = _to_ascii(field_value).lstrip(" ")
    data, pos = _parse_byte_sequence(text, 0)
    _reject_parameters(text, pos)
    if text[pos:].lstrip(" "):
        raise ClientCertError("trailing characters after the Byte Sequence")
    return data


def parse_byte_sequence_list(field_value: bytes) -> tuple[bytes, ...]:
    """RFC 9651 Section 4.2.1 List whose every member must be a bare Byte Sequence."""
    text = _to_ascii(field_value).lstrip(" ")
    members: list[bytes] = []
    pos = 0
    while pos < len(text):
        if text[pos] == "(":
            raise ClientCertError("Inner List member")
        data, pos = _parse_byte_sequence(text, pos)
        _reject_parameters(text, pos)
        members.append(data)
        while pos < len(text) and text[pos] in _OWS:
            pos += 1
        if pos == len(text):
            break
        if text[pos] != ",":
            raise ClientCertError("expected ',' between List members")
        pos += 1
        while pos < len(text) and text[pos] in _OWS:
            pos += 1
        if pos == len(text):
            raise ClientCertError("trailing comma")
    return tuple(members)


# --- Field lines -> certificates -----------------------------------------------------------


def field_lines(headers: Iterable[Header], name: bytes) -> list[bytes]:
    """Every value whose field name matches *name* case-insensitively (RFC 9110 Section 5.1)."""
    return [value for key, value in headers if key.lower() == name]


def parse_client_cert(lines: Sequence[bytes]) -> bytes | None:
    """DER leaf from Client-Cert, or None when absent; a repeated field line is an error."""
    if not lines:
        return None
    if len(lines) > 1:
        raise ClientCertError("Client-Cert is a singleton and occurred more than once")
    return parse_byte_sequence_item(lines[0])


def parse_client_cert_chain(lines: Sequence[bytes]) -> tuple[bytes, ...]:
    """DER chain from all Client-Cert-Chain lines, combined with ", " (RFC 9110 Section 5.3)."""
    if not lines:
        return ()
    return parse_byte_sequence_list(b", ".join(lines))


@dataclass(frozen=True, slots=True)
class ForwardedClientCert:
    """The client certificate a trusted TTRP vouched for, as parsed X.509 objects."""

    leaf: x509.Certificate
    chain: tuple[x509.Certificate, ...]


def _load_der(der: bytes, what: str) -> x509.Certificate:
    try:
        cert = x509.load_der_x509_certificate(der)
        _ = cert.extensions  # parsed lazily; force it so a bad extension is a 400, not a 500
        return cert
    except ValueError as exc:
        raise ClientCertError(f"{what} is not a DER X.509 certificate") from exc


def extract_client_cert(headers: Sequence[Header]) -> ForwardedClientCert | None:
    """Parse both fields from a request already known to come from a trusted TTRP."""
    leaf_der = parse_client_cert(field_lines(headers, CLIENT_CERT))
    chain_der = parse_client_cert_chain(field_lines(headers, CLIENT_CERT_CHAIN))
    if leaf_der is None:
        if chain_der:
            raise ClientCertError("Client-Cert-Chain without Client-Cert")
        return None
    chain = tuple(_load_der(der, "Client-Cert-Chain member") for der in chain_der)
    return ForwardedClientCert(leaf=_load_der(leaf_der, "Client-Cert"), chain=chain)


# --- TTRP side: sanitize then set ----------------------------------------------------------


def strip_client_cert_fields(headers: Iterable[Header]) -> list[Header]:
    """Remove every Client-Cert and Client-Cert-Chain line, whatever its case."""
    return [(k, v) for k, v in headers if k.lower() not in CLIENT_CERT_FIELDS]


def ttrp_forward_headers(
    incoming: Sequence[Header],
    leaf_der: bytes | None,
    chain_der: Sequence[bytes] = (),
) -> list[Header]:
    """RFC 9440 Section 2.4: drop client copies, then add the handshake's certificate if any."""
    out = strip_client_cert_fields(incoming)
    if leaf_der is not None:
        out.append((CLIENT_CERT, serialize_client_cert(leaf_der)))
        if chain_der:
            out.append((CLIENT_CERT_CHAIN, serialize_client_cert_chain(chain_der)))
    return out


# --- Origin side: trust by transport peer address ------------------------------------------


def parse_networks(cidrs: Iterable[str]) -> tuple[Network, ...]:
    """Strict CIDR parsing: a typo raises instead of silently trusting nothing or everything."""
    return tuple(ipaddress.ip_network(c, strict=True) for c in cidrs)


def peer_is_trusted(client: Sequence[object] | None, networks: Sequence[Network]) -> bool:
    """True only for an IP-literal peer inside one of *networks*; anything else is untrusted."""
    if not client or not isinstance(client[0], str):
        return False
    try:
        ip = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # a dual-stack socket reports an IPv4 peer as ::ffff:a.b.c.d
    return any(ip in net for net in networks)


class ClientCertGuard:
    """ASGI middleware that must see the transport peer, so it wraps every middleware that
    rewrites `scope["client"]`; under Uvicorn that means `--no-proxy-headers` and adding
    `ProxyHeadersMiddleware` inside this guard, because Uvicorn installs it outermost.
    """

    def __init__(self, app: ASGIApp, trusted_proxies: Iterable[str]) -> None:
        self.app = app
        self.networks = parse_networks(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers: list[Header] = list(scope["headers"])
        forwarded: ForwardedClientCert | None = None
        if peer_is_trusted(scope.get("client"), self.networks):
            try:
                forwarded = extract_client_cert(headers)
            except ClientCertError as exc:
                if scope["type"] == "websocket":
                    # Before accept, ASGI servers turn this close into an HTTP 403 response.
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await PlainTextResponse(f"bad Client-Cert: {exc}", status_code=400)(
                    scope, receive, send
                )
                return
        # Share the per-request state dict (outer middleware reads it after the app returns),
        # but give the app a header list without the raw fields.
        state = scope.setdefault("state", {})
        state[STATE_KEY] = forwarded
        child = dict(scope)
        child["headers"] = strip_client_cert_fields(headers)
        await self.app(child, receive, send)
