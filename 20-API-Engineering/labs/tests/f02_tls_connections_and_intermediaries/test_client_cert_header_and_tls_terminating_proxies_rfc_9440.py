"""Lab for Client-Cert-Header-and-TLS-Terminating-Proxies-RFC-9440."""

from __future__ import annotations

import base64
import math
from typing import TYPE_CHECKING, Any, cast

import h11
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.config import Config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_labs.f02_tls_connections_and_intermediaries.client_cert_header import (
    CLIENT_CERT,
    CLIENT_CERT_CHAIN,
    STATE_KEY,
    ClientCertError,
    ClientCertGuard,
    ForwardedClientCert,
    extract_client_cert,
    parse_byte_sequence_item,
    parse_byte_sequence_list,
    parse_client_cert,
    parse_client_cert_chain,
    parse_networks,
    peer_is_trusted,
    pem_to_byte_sequence,
    serialize_client_cert,
    serialize_client_cert_chain,
    ttrp_forward_headers,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp, Message
    from uvicorn._types import ASGI3Application

# RFC 9440 Appendix A, Figure 1: client (leaf), intermediate, and root certificates.
LEAF_PEM = """-----BEGIN CERTIFICATE-----
MIIBqDCCAU6gAwIBAgIBBzAKBggqhkjOPQQDAjA6MRswGQYDVQQKDBJMZXQncyBB
dXRoZW50aWNhdGUxGzAZBgNVBAMMEkxBIEludGVybWVkaWF0ZSBDQTAeFw0yMDAx
MTQyMjU1MzNaFw0yMTAxMjMyMjU1MzNaMA0xCzAJBgNVBAMMAkJDMFkwEwYHKoZI
zj0CAQYIKoZIzj0DAQcDQgAE8YnXXfaUgmnMtOXU/IncWalRhebrXmckC8vdgJ1p
5Be5F/3YC8OthxM4+k1M6aEAEFcGzkJiNy6J84y7uzo9M6NyMHAwCQYDVR0TBAIw
ADAfBgNVHSMEGDAWgBRm3WjLa38lbEYCuiCPct0ZaSED2DAOBgNVHQ8BAf8EBAMC
BsAwEwYDVR0lBAwwCgYIKwYBBQUHAwIwHQYDVR0RAQH/BBMwEYEPYmRjQGV4YW1w
bGUuY29tMAoGCCqGSM49BAMCA0gAMEUCIBHda/r1vaL6G3VliL4/Di6YK0Q6bMje
SkC3dFCOOB8TAiEAx/kHSB4urmiZ0NX5r5XarmPk0wmuydBVoU4hBVZ1yhk=
-----END CERTIFICATE-----"""
INTERMEDIATE_PEM = """-----BEGIN CERTIFICATE-----
MIIB5jCCAYugAwIBAgIBFjAKBggqhkjOPQQDAjBWMQswCQYDVQQGEwJVUzEbMBkG
A1UECgwSTGV0J3MgQXV0aGVudGljYXRlMSowKAYDVQQDDCFMZXQncyBBdXRoZW50
aWNhdGUgUm9vdCBBdXRob3JpdHkwHhcNMjAwMTE0MjEzMjMwWhcNMzAwMTExMjEz
MjMwWjA6MRswGQYDVQQKDBJMZXQncyBBdXRoZW50aWNhdGUxGzAZBgNVBAMMEkxB
IEludGVybWVkaWF0ZSBDQTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABJf+aA54
RC5pyLAR5yfXVYmNpgd+CGUTDp2KOGhc0gK91zxhHesEYkdXkpS2UN8Kati+yHtW
CV3kkhCngGyv7RqjZjBkMB0GA1UdDgQWBBRm3WjLa38lbEYCuiCPct0ZaSED2DAf
BgNVHSMEGDAWgBTEA2Q6eecKu9g9yb5glbkhhVINGDASBgNVHRMBAf8ECDAGAQH/
AgEAMA4GA1UdDwEB/wQEAwIBhjAKBggqhkjOPQQDAgNJADBGAiEA5pLvaFwRRkxo
mIAtDIwg9D7gC1xzxBl4r28EzmSO1pcCIQCJUShpSXO9HDIQMUgH69fNDEMHXD3R
RX5gP7kuu2KGMg==
-----END CERTIFICATE-----"""
ROOT_PEM = """-----BEGIN CERTIFICATE-----
MIICBjCCAaygAwIBAgIJAKS0yiqKtlhoMAoGCCqGSM49BAMCMFYxCzAJBgNVBAYT
AlVTMRswGQYDVQQKDBJMZXQncyBBdXRoZW50aWNhdGUxKjAoBgNVBAMMIUxldCdz
IEF1dGhlbnRpY2F0ZSBSb290IEF1dGhvcml0eTAeFw0yMDAxMTQyMTI1NDVaFw00
MDAxMDkyMTI1NDVaMFYxCzAJBgNVBAYTAlVTMRswGQYDVQQKDBJMZXQncyBBdXRo
ZW50aWNhdGUxKjAoBgNVBAMMIUxldCdzIEF1dGhlbnRpY2F0ZSBSb290IEF1dGhv
cml0eTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABFoaHU+Z5bPKmGzlYXtCf+E6
HYj62fORaHDOrt+yyh3H/rTcs7ynFfGn+gyFsrSP3Ez88rajv+U2NfD0o0uZ4Pmj
YzBhMB0GA1UdDgQWBBTEA2Q6eecKu9g9yb5glbkhhVINGDAfBgNVHSMEGDAWgBTE
A2Q6eecKu9g9yb5glbkhhVINGDAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQE
AwIBhjAKBggqhkjOPQQDAgNIADBFAiEAmAeg1ycKHriqHnaD4M/UDBpQRpkmdcRF
YGMg1Qyrkx4CIB4ivz3wQcQkGhcsUZ1SOImd/lq1Q0FLf09rGfLQPWDc
-----END CERTIFICATE-----"""

# RFC 9440 Appendix A, Figures 2 and 3, with the display-only line breaks removed.
FIGURE_2 = """:MIIBqDCCAU6gAwIBAgIBBzAKBggqhkjOPQQDAjA6MRswGQYDVQQKDBJ
 MZXQncyBBdXRoZW50aWNhdGUxGzAZBgNVBAMMEkxBIEludGVybWVkaWF0ZSBDQTAeFw0
 yMDAxMTQyMjU1MzNaFw0yMTAxMjMyMjU1MzNaMA0xCzAJBgNVBAMMAkJDMFkwEwYHKoZ
 Izj0CAQYIKoZIzj0DAQcDQgAE8YnXXfaUgmnMtOXU/IncWalRhebrXmckC8vdgJ1p5Be
 5F/3YC8OthxM4+k1M6aEAEFcGzkJiNy6J84y7uzo9M6NyMHAwCQYDVR0TBAIwADAfBgN
 VHSMEGDAWgBRm3WjLa38lbEYCuiCPct0ZaSED2DAOBgNVHQ8BAf8EBAMCBsAwEwYDVR0
 lBAwwCgYIKwYBBQUHAwIwHQYDVR0RAQH/BBMwEYEPYmRjQGV4YW1wbGUuY29tMAoGCCq
 GSM49BAMCA0gAMEUCIBHda/r1vaL6G3VliL4/Di6YK0Q6bMjeSkC3dFCOOB8TAiEAx/k
 HSB4urmiZ0NX5r5XarmPk0wmuydBVoU4hBVZ1yhk=:"""
FIGURE_3 = """:MIIB5jCCAYugAwIBAgIBFjAKBggqhkjOPQQDAjBWMQsw
 CQYDVQQGEwJVUzEbMBkGA1UECgwSTGV0J3MgQXV0aGVudGljYXRlMSowKAYDVQQ
 DDCFMZXQncyBBdXRoZW50aWNhdGUgUm9vdCBBdXRob3JpdHkwHhcNMjAwMTE0Mj
 EzMjMwWhcNMzAwMTExMjEzMjMwWjA6MRswGQYDVQQKDBJMZXQncyBBdXRoZW50a
 WNhdGUxGzAZBgNVBAMMEkxBIEludGVybWVkaWF0ZSBDQTBZMBMGByqGSM49AgEG
 CCqGSM49AwEHA0IABJf+aA54RC5pyLAR5yfXVYmNpgd+CGUTDp2KOGhc0gK91zx
 hHesEYkdXkpS2UN8Kati+yHtWCV3kkhCngGyv7RqjZjBkMB0GA1UdDgQWBBRm3W
 jLa38lbEYCuiCPct0ZaSED2DAfBgNVHSMEGDAWgBTEA2Q6eecKu9g9yb5glbkhh
 VINGDASBgNVHRMBAf8ECDAGAQH/AgEAMA4GA1UdDwEB/wQEAwIBhjAKBggqhkjO
 PQQDAgNJADBGAiEA5pLvaFwRRkxomIAtDIwg9D7gC1xzxBl4r28EzmSO1pcCIQC
 JUShpSXO9HDIQMUgH69fNDEMHXD3RRX5gP7kuu2KGMg==:, :MIICBjCCAaygAw
 IBAgIJAKS0yiqKtlhoMAoGCCqGSM49BAMCMFYxCzAJBgNVBAYTAlVTMRswGQYDV
 QQKDBJMZXQncyBBdXRoZW50aWNhdGUxKjAoBgNVBAMMIUxldCdzIEF1dGhlbnRp
 Y2F0ZSBSb290IEF1dGhvcml0eTAeFw0yMDAxMTQyMTI1NDVaFw00MDAxMDkyMTI
 1NDVaMFYxCzAJBgNVBAYTAlVTMRswGQYDVQQKDBJMZXQncyBBdXRoZW50aWNhdG
 UxKjAoBgNVBAMMIUxldCdzIEF1dGhlbnRpY2F0ZSBSb290IEF1dGhvcml0eTBZM
 BMGByqGSM49AgEGCCqGSM49AwEHA0IABFoaHU+Z5bPKmGzlYXtCf+E6HYj62fOR
 aHDOrt+yyh3H/rTcs7ynFfGn+gyFsrSP3Ez88rajv+U2NfD0o0uZ4PmjYzBhMB0
 GA1UdDgQWBBTEA2Q6eecKu9g9yb5glbkhhVINGDAfBgNVHSMEGDAWgBTEA2Q6ee
 cKu9g9yb5glbkhhVINGDAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBh
 jAKBggqhkjOPQQDAgNIADBFAiEAmAeg1ycKHriqHnaD4M/UDBpQRpkmdcRFYGMg
 1Qyrkx4CIB4ivz3wQcQkGhcsUZ1SOImd/lq1Q0FLf09rGfLQPWDc:"""

PROXY_CIDRS = ["10.0.0.0/24"]
PROXY_PEER = ("10.0.0.7", 51000)
OUTSIDE_PEER = ("203.0.113.9", 40000)


def unfold(figure: str) -> bytes:
    return "".join(line.strip() for line in figure.splitlines()).encode("ascii")


def pem_der(pem: str) -> bytes:
    body = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    return base64.b64decode(body, validate=True)


LEAF_DER = pem_der(LEAF_PEM)
CHAIN_DER = (pem_der(INTERMEDIATE_PEM), pem_der(ROOT_PEM))
# The leaf's KeyUsage extension value retagged from BIT STRING (03) to OCTET STRING (04):
# the outer DER still loads, and only the lazily parsed extensions fail.
_KEY_USAGE = bytes.fromhex("0603551d0f0101ff0404030206c0")
assert LEAF_DER.count(_KEY_USAGE) == 1
BAD_EXTENSION_DER = LEAF_DER.replace(_KEY_USAGE, bytes.fromhex("0603551d0f0101ff0404040206c0"))


async def whoami(request: Request) -> JSONResponse:
    cert: ForwardedClientCert | None = request.state.client_cert
    raw = request.headers.get("client-cert")
    if cert is None:
        return JSONResponse({"error": "client certificate required", "raw": raw}, 403)
    return JSONResponse(
        {"subject": cert.leaf.subject.rfc4514_string(), "chain": len(cert.chain), "raw": raw},
        headers={"Cache-Control": "no-store"},
    )


ORIGIN = Starlette(routes=[Route("/whoami", whoami)])


def client(app: ASGIApp, peer: tuple[str, int]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# --- Wire format: RFC 9651 Byte Sequences and Lists ----------------------------------------


@pytest.mark.unit
def test_leaf_serializes_to_rfc9440_figure_2() -> None:
    assert serialize_client_cert(LEAF_DER) == unfold(FIGURE_2)


@pytest.mark.unit
def test_chain_serializes_to_rfc9440_figure_3() -> None:
    assert serialize_client_cert_chain(CHAIN_DER) == unfold(FIGURE_3)


@pytest.mark.unit
def test_pem_shortcut_equals_der_serialization() -> None:
    assert pem_to_byte_sequence(LEAF_PEM).encode("ascii") == serialize_client_cert(LEAF_DER)


@pytest.mark.unit
@pytest.mark.parametrize(
    "pem",
    [
        pytest.param(LEAF_PEM + "\n" + INTERMEDIATE_PEM, id="two-certificates"),
        pytest.param(
            LEAF_PEM + "\n-----BEGIN PRIVATE KEY-----\nMC4CAQA=\n-----END PRIVATE KEY-----",
            id="cert-then-key",
        ),
        pytest.param("subject=CN=BC\n" + LEAF_PEM, id="leading-text"),
        pytest.param(LEAF_PEM.replace("CERTIFICATE", "PRIVATE KEY"), id="key-only"),
        pytest.param("-----BEGIN CERTIFICATE-----", id="armor-only"),
    ],
)
def test_pem_shortcut_accepts_exactly_one_certificate(pem: str) -> None:
    with pytest.raises(ClientCertError, match="exactly one PEM CERTIFICATE"):
        pem_to_byte_sequence(pem)


@pytest.mark.unit
def test_figures_parse_back_to_der() -> None:
    assert parse_client_cert([unfold(FIGURE_2)]) == LEAF_DER
    assert parse_client_cert_chain([unfold(FIGURE_3)]) == CHAIN_DER


@pytest.mark.unit
def test_header_size_is_base64_of_der_plus_two_colons() -> None:
    value = serialize_client_cert(LEAF_DER)
    assert len(LEAF_DER) == 428
    assert len(value) == 2 + 4 * math.ceil(len(LEAF_DER) / 3) == 574
    chain_value = serialize_client_cert_chain(CHAIN_DER)
    assert [len(d) for d in CHAIN_DER] == [490, 522]
    assert len(chain_value) == (2 + 656) + 2 + (2 + 696) == 1358


@pytest.mark.unit
def test_chain_split_across_field_lines_equals_one_line() -> None:
    first, second = (serialize_client_cert_chain([d]) for d in CHAIN_DER)
    assert parse_client_cert_chain([first, second]) == CHAIN_DER


@pytest.mark.unit
def test_byte_sequence_split_across_field_lines_fails() -> None:
    value = serialize_client_cert_chain(CHAIN_DER[:1])
    with pytest.raises(ClientCertError):
        parse_client_cert_chain([value[:300], value[300:]])


@pytest.mark.unit
def test_client_cert_singleton_rejects_second_line() -> None:
    value = serialize_client_cert(LEAF_DER)
    with pytest.raises(ClientCertError, match="singleton"):
        parse_client_cert([value, value])


@pytest.mark.unit
def test_client_cert_rejects_list_value() -> None:
    with pytest.raises(ClientCertError, match="trailing"):
        parse_byte_sequence_item(b":QQ==:, :QQ==:")


@pytest.mark.unit
def test_missing_padding_is_synthesized() -> None:
    assert parse_byte_sequence_item(b":QQ:") == b"A"
    assert parse_byte_sequence_item(b":QUI:") == b"AB"


@pytest.mark.unit
def test_nonzero_pad_bits_are_accepted() -> None:
    assert parse_byte_sequence_item(b":QR==:") == b"A"


@pytest.mark.unit
def test_leading_and_trailing_spaces_are_discarded() -> None:
    assert parse_byte_sequence_item(b"  :QQ==:  ") == b"A"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(b"QQ==", id="no-colons"),
        pytest.param(b":QQ==", id="unterminated"),
        pytest.param(b":Q-_=:", id="base64url-alphabet"),
        pytest.param(b":QQ ==:", id="space-inside"),
        pytest.param(b":QQ\n==:", id="line-feed-inside"),
        pytest.param(b":Q=Q=:", id="discontinuous-padding"),
        pytest.param(b":QQ==QQ==:", id="data-after-padding"),
        pytest.param(b":Q:", id="one-char-quantum"),
        pytest.param(b":QQ==:;v=1", id="parameter"),
        pytest.param(":\u00e9:".encode(), id="non-ascii"),
        # RFC 9651 Section 4.2 discards only leading and trailing SP, never HTAB.
        pytest.param(b"\t:QQ==:", id="leading-tab"),
        pytest.param(b":QQ==:\t", id="trailing-tab"),
    ],
)
def test_item_parser_rejects(value: bytes) -> None:
    with pytest.raises(ClientCertError):
        parse_byte_sequence_item(value)


@pytest.mark.unit
def test_parameters_and_inner_lists_fail_with_a_named_reason() -> None:
    with pytest.raises(ClientCertError, match="parameters"):
        parse_byte_sequence_item(b":QQ==:;v=1")
    with pytest.raises(ClientCertError, match="parameters"):
        parse_byte_sequence_list(b":QQ==:;a, :QQ==:")
    with pytest.raises(ClientCertError, match="Inner List"):
        parse_byte_sequence_list(b":QQ==:, (:QQ==:)")


@pytest.mark.unit
def test_field_names_match_case_insensitively() -> None:
    headers = [(b"Client-Cert", unfold(FIGURE_2)), (b"CLIENT-CERT-CHAIN", unfold(FIGURE_3))]
    cert = extract_client_cert(headers)
    assert cert is not None
    assert (cert.leaf.serial_number, len(cert.chain)) == (7, 2)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(b":QQ==:,", id="trailing-comma"),
        pytest.param(b":QQ==:,,:QQ==:", id="empty-member"),
        pytest.param(b":QQ==: :QQ==:", id="missing-comma"),
        pytest.param(b"(:QQ==:)", id="inner-list"),
        pytest.param(b":QQ==:, abc", id="token-member"),
        pytest.param(b":QQ==:;a, :QQ==:", id="parameter"),
        pytest.param(b"\t:QQ==:", id="leading-tab"),
    ],
)
def test_list_parser_rejects(value: bytes) -> None:
    with pytest.raises(ClientCertError):
        parse_byte_sequence_list(value)


@pytest.mark.unit
def test_empty_list_value_parses_to_no_members() -> None:
    assert parse_byte_sequence_list(b"") == ()
    assert parse_byte_sequence_list(b"   ") == ()


@pytest.mark.unit
def test_list_allows_tabs_around_commas() -> None:
    assert parse_byte_sequence_list(b":QQ==:\t,\t:QUI=:") == (b"A", b"AB")


@pytest.mark.unit
def test_parser_meets_rfc9651_minimums() -> None:
    blob = bytes(range(256)) * 64  # 16384 octets, Section 3.3.5 minimum
    assert parse_byte_sequence_item(serialize_client_cert(blob)) == blob
    members = [bytes([i % 256]) for i in range(1024)]  # Section 3.1 minimum
    assert parse_client_cert_chain([serialize_client_cert_chain(members)]) == tuple(members)


# --- Proxy side: sanitize, then set ---------------------------------------------------------


@pytest.mark.unit
def test_ttrp_replaces_injected_fields_in_any_case() -> None:
    incoming = [
        (b"host", b"api"),
        (b"Client-Cert", b":Zm9yZ2Vk:"),
        (b"CLIENT-CERT-CHAIN", b":Zm9yZ2Vk:"),
    ]
    out = ttrp_forward_headers(incoming, LEAF_DER, CHAIN_DER[:1])
    assert out == [
        (b"host", b"api"),
        (CLIENT_CERT, unfold(FIGURE_2)),
        (CLIENT_CERT_CHAIN, serialize_client_cert_chain(CHAIN_DER[:1])),
    ]


@pytest.mark.unit
def test_ttrp_without_mtls_removes_every_copy() -> None:
    incoming = [(b"client-cert", b":Zm9yZ2Vk:"), (b"client-cert", b":Zm9yZ2Vk:"), (b"a", b"b")]
    assert ttrp_forward_headers(incoming, None) == [(b"a", b"b")]
    # A chain without a leaf must never be emitted: Client-Cert-Chain MUST NOT appear alone.
    assert ttrp_forward_headers(incoming, None, CHAIN_DER[:1]) == [(b"a", b"b")]


@pytest.mark.unit
def test_ttrp_omits_chain_when_not_configured() -> None:
    out = ttrp_forward_headers([], LEAF_DER)
    assert [k for k, _ in out] == [CLIENT_CERT]


# --- Origin side: trust by transport peer ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("peer", "trusted"),
    [
        pytest.param(("10.0.0.7", 1), True, id="inside-cidr"),
        pytest.param(("10.0.1.7", 1), False, id="outside-cidr"),
        pytest.param(("::ffff:10.0.0.7", 1), True, id="ipv4-mapped-inside"),
        pytest.param(("::ffff:10.0.1.7", 1), False, id="ipv4-mapped-outside"),
        pytest.param(("testclient", 1), False, id="hostname"),
        pytest.param(("/run/app.sock", 0), False, id="unix-socket-path"),
        pytest.param(None, False, id="no-client"),
    ],
)
def test_peer_is_trusted(peer: tuple[str, int] | None, trusted: bool) -> None:
    assert peer_is_trusted(peer, parse_networks(PROXY_CIDRS)) is trusted


@pytest.mark.unit
def test_cidr_with_host_bits_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="host bits set"):
        parse_networks(["10.0.0.7/24"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trusted_proxy_certificate_reaches_app_as_parsed_object() -> None:
    headers = ttrp_forward_headers([], LEAF_DER, CHAIN_DER[:1])
    async with client(ClientCertGuard(ORIGIN, PROXY_CIDRS), PROXY_PEER) as c:
        r = await c.get("/whoami", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"subject": "CN=BC", "chain": 1, "raw": None}
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forged_certificate_from_outside_peer_is_stripped() -> None:
    headers = ttrp_forward_headers([], LEAF_DER)  # attacker replays a real leaf
    async with client(ClientCertGuard(ORIGIN, PROXY_CIDRS), OUTSIDE_PEER) as c:
        r = await c.get("/whoami", headers=headers)
    assert r.status_code == 403
    assert r.json()["raw"] is None


async def naive_whoami(request: Request) -> JSONResponse:
    # Reads the raw field itself: correct only if every peer that can reach it is the TTRP.
    cert = extract_client_cert(request.headers.raw)
    if cert is None:
        return JSONResponse({"error": "client certificate required"}, 403)
    return JSONResponse({"subject": cert.leaf.subject.rfc4514_string()})


NAIVE = Starlette(routes=[Route("/whoami", naive_whoami)])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_origin_reading_raw_field_trusts_any_peer_until_guarded() -> None:
    forged = {"client-cert": unfold(FIGURE_2).decode()}
    async with client(NAIVE, OUTSIDE_PEER) as c:
        r = await c.get("/whoami", headers=forged)
    assert (r.status_code, r.json()) == (200, {"subject": "CN=BC"})
    async with client(ClientCertGuard(NAIVE, PROXY_CIDRS), OUTSIDE_PEER) as c:
        r = await c.get("/whoami", headers=forged)
    assert r.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        pytest.param([(b"client-cert", b"MIIB")], "not a Byte Sequence", id="pem-no-colons"),
        pytest.param([(b"client-cert", b":Zm9yZ2Vk:")], "not a DER X.509", id="not-der"),
        pytest.param(
            [(b"client-cert", serialize_client_cert(BAD_EXTENSION_DER))],
            "not a DER X.509",
            id="malformed-extension",
        ),
        pytest.param(
            [(b"client-cert-chain", unfold(FIGURE_3))], "without Client-Cert", id="chain-only"
        ),
        pytest.param(
            [(b"client-cert", unfold(FIGURE_2)), (b"client-cert", unfold(FIGURE_2))],
            "singleton",
            id="duplicate",
        ),
    ],
)
async def test_malformed_fields_from_trusted_proxy_are_400(
    headers: list[tuple[bytes, bytes]], reason: str
) -> None:
    async with client(ClientCertGuard(ORIGIN, PROXY_CIDRS), PROXY_PEER) as c:
        r = await c.get("/whoami", headers=headers)
    assert r.status_code == 400
    assert reason in r.text


def uvicorn_proxy_headers(app: ASGIApp, trusted_hosts: str = "*") -> ASGIApp:
    """Uvicorn 0.54.0 ProxyHeadersMiddleware, as `--forwarded-allow-ips` configures it."""
    wrapped = ProxyHeadersMiddleware(cast("ASGI3Application", app), trusted_hosts=trusted_hosts)
    return cast("ASGIApp", wrapped)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_inside_proxy_headers_middleware_trusts_spoofed_xff() -> None:
    # Uvicorn wraps the app in ProxyHeadersMiddleware outermost; with "*" it rewrites
    # scope["client"] from X-Forwarded-For, so an inner guard sees an attacker-chosen IP.
    spoof = {"x-forwarded-for": "10.0.0.7", "client-cert": unfold(FIGURE_2).decode()}
    inner = uvicorn_proxy_headers(ClientCertGuard(ORIGIN, PROXY_CIDRS))
    async with client(inner, OUTSIDE_PEER) as c:
        r = await c.get("/whoami", headers=spoof)
    assert (r.status_code, r.json()["subject"]) == (200, "CN=BC")
    outer = ClientCertGuard(uvicorn_proxy_headers(ORIGIN), PROXY_CIDRS)
    async with client(outer, OUTSIDE_PEER) as c:
        r = await c.get("/whoami", headers=spoof)
    assert r.status_code == 403


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_inside_restricted_proxy_headers_strips_the_real_certificate() -> None:
    # forwarded_allow_ips = the proxy CIDR: the TTRP's own X-Forwarded-For moves
    # scope["client"] to the end client, so the inner guard no longer sees the proxy.
    headers = [(b"x-forwarded-for", b"198.51.100.4"), *ttrp_forward_headers([], LEAF_DER)]
    inner = uvicorn_proxy_headers(ClientCertGuard(ORIGIN, PROXY_CIDRS), "10.0.0.0/24")
    async with client(inner, PROXY_PEER) as c:
        r = await c.get("/whoami", headers=headers)
    assert r.status_code == 403
    outer = ClientCertGuard(uvicorn_proxy_headers(ORIGIN, "10.0.0.0/24"), PROXY_CIDRS)
    async with client(outer, PROXY_PEER) as c:
        r = await c.get("/whoami", headers=headers)
    assert (r.status_code, r.json()["subject"]) == (200, "CN=BC")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_with_malformed_field_is_denied_before_accept() -> None:
    # A close before websocket.accept is sent to the client as HTTP 403 (ASGI spec).
    sent: list[Message] = []
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "websocket",
        "client": PROXY_PEER,
        "headers": [(b"client-cert", b"x")],
    }
    await ClientCertGuard(app, PROXY_CIDRS)(scope, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert not called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_passes_through_untouched() -> None:
    seen: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    scope: dict[str, Any] = {"type": "lifespan", "state": {}}
    await ClientCertGuard(app, PROXY_CIDRS)(scope, receive, send)
    assert seen == [scope]
    assert seen[0] is scope


@pytest.mark.unit
def test_extract_returns_none_without_fields() -> None:
    assert extract_client_cert([(b"host", b"api")]) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_preserves_existing_state_and_sets_key() -> None:
    seen: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        return None

    headers = [(b"client-cert", unfold(FIGURE_2)), (b"x", b"y")]
    scope: dict[str, Any] = {
        "type": "http",
        "client": PROXY_PEER,
        "headers": headers,
        "state": {"db": 1},
    }
    await ClientCertGuard(app, PROXY_CIDRS)(scope, receive, send)
    child = seen[0]
    assert child["headers"] == [(b"x", b"y")]
    assert child["state"] is scope["state"]  # outer middleware sees what the app stores
    assert scope["state"]["db"] == 1
    assert scope["state"][STATE_KEY].leaf.serial_number == 7
    assert scope["headers"] == headers  # the parent's header list is not mutated


async def run_websocket_guard(peer: tuple[str, int]) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        return None

    headers = [(b"client-cert", unfold(FIGURE_2)), (b"x", b"y")]
    scope: dict[str, Any] = {"type": "websocket", "client": peer, "headers": headers}
    await ClientCertGuard(app, PROXY_CIDRS)(scope, receive, send)
    return seen[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_from_trusted_proxy_gets_parsed_certificate() -> None:
    child = await run_websocket_guard(PROXY_PEER)
    assert child["headers"] == [(b"x", b"y")]
    assert child["state"][STATE_KEY].leaf.serial_number == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_websocket_from_outside_peer_is_stripped() -> None:
    child = await run_websocket_guard(OUTSIDE_PEER)
    assert child["headers"] == [(b"x", b"y")]
    assert child["state"][STATE_KEY] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_field_from_outside_peer_is_never_parsed() -> None:
    async with client(ClientCertGuard(ORIGIN, PROXY_CIDRS), OUTSIDE_PEER) as c:
        r = await c.get("/whoami", headers={"client-cert": "x"})
    assert r.status_code == 403  # not a 400 that would expose the parser to outsiders
    assert r.json()["raw"] is None


@pytest.mark.unit
def test_uvicorn_installs_proxy_headers_outermost_by_default() -> None:
    guarded = ClientCertGuard(ORIGIN, PROXY_CIDRS)
    default = Config(app=guarded)
    default.load()
    outer = cast("object", default.loaded_app)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(outer, ProxyHeadersMiddleware)
    assert outer.app is guarded
    disabled = Config(app=guarded, proxy_headers=False)
    disabled.load()
    assert disabled.loaded_app is guarded  # pyright: ignore[reportUnknownMemberType]


# --- Size: certificates against server header limits ----------------------------------------


def request_head(cert_value_len: int) -> bytes:
    value = b":" + b"A" * cert_value_len + b":"
    return b"GET / HTTP/1.1\r\nHost: origin\r\nClient-Cert: " + value + b"\r\n\r\n"


@pytest.mark.unit
def test_h11_rejects_only_incomplete_heads_over_16384_bytes() -> None:
    head = request_head(40_000)
    ok = h11.Connection(h11.SERVER)
    ok.receive_data(head[:16384])
    assert ok.next_event() is h11.NEED_DATA
    over = h11.Connection(h11.SERVER)
    over.receive_data(head[:16385])
    with pytest.raises(h11.RemoteProtocolError) as info:
        over.next_event()
    assert info.value.error_status_hint == 431


@pytest.mark.unit
def test_h11_accepts_a_40k_head_delivered_in_one_read() -> None:
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(request_head(40_000))
    event = conn.next_event()
    assert isinstance(event, h11.Request)
    assert len(dict(event.headers)[b"client-cert"]) == 40_002
