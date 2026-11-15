"""Lab for HTTP-Message-Signatures-RFC-9421-and-Content-Digests-RFC-9530.

Ground truth is the RFC text itself: the Ed25519 and HMAC-SHA256 signatures in RFC 9421
Appendix B are deterministic, so this lab must reproduce them byte for byte, and the
RFC 9530 Appendix D digests must match hashlib exactly.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from hypercorn.utils import filter_pseudo_headers
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from api_labs.f14_api_authentication_and_identity.http_message_signatures import (
    Component,
    HttpRequest,
    HttpSignatureAuth,
    InnerList,
    Item,
    Policy,
    SFError,
    SignatureBaseError,
    SignatureError,
    SigningKey,
    VerificationKey,
    components,
    content_digest,
    http_verify,
    make_app,
    normalize_authority,
    parse_dictionary,
    request_from_starlette,
    sign_request,
    signature_base,
    verify_content_digest,
    verify_request,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from starlette.requests import Request

# RFC 9421 Appendix B.1.4 test-key-ed25519 and B.1.5 test-shared-secret (test-only keys).
RFC_ED25519_PEM = (
    b"-----BEGIN PRIVATE KEY-----\n"
    b"MC4CAQAwBQYDK2VwBCIEIJ+DYvh6SEqVTm50DFtMDoQikTmiCqirVv9mWG9qfSnF\n"
    b"-----END PRIVATE KEY-----\n"
)
RFC_HMAC_KEY_B64 = (
    "uzvJfB4u3N0Jy4T7NZ75MDVcr8zSTInedJtkgcu46YW4XByzNJjxBdtjUkdJPBtbmHhIDi6pcl8jsasjlTMtDQ=="
)
RFC_SHA512_HELLO = (
    "WZDPaVn/7XgHaAy8pmojAkGWoRx2UFChF41A2svX+TaPm+AbwAgBWnrIiYllu7BNNyealdVLvRwEmTHWXvJwew=="
)
HELLO = b'{"hello": "world"}'
NOW = 1_700_000_000


def rfc_ed25519() -> Ed25519PrivateKey:
    key = load_pem_private_key(RFC_ED25519_PEM, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def rfc_test_request() -> HttpRequest:
    """RFC 9421 Appendix B.2 test-request."""
    return HttpRequest(
        method="POST",
        scheme="https",
        authority="example.com",
        raw_path="/foo",
        query="param=Value&Pet=dog",
        headers=(
            ("Host", "example.com"),
            ("Date", "Tue, 20 Apr 2021 02:07:55 GMT"),
            ("Content-Type", "application/json"),
            ("Content-Digest", f"sha-512=:{RFC_SHA512_HELLO}:"),
            ("Content-Length", "18"),
        ),
        content=HELLO,
    )


# --------------------------------------------------------------------------------------
# RFC 9421 Appendix B vectors, byte for byte.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_b26_ed25519_signature_base_and_signature_match_rfc_bytes() -> None:
    covered = components("date", "@method", "@path", "@authority", "content-type")
    covered += components("content-length")
    sig_input, signature = sign_request(
        rfc_test_request(),
        SigningKey("test-key-ed25519", "ed25519", private=rfc_ed25519()),
        covered,
        label="sig-b26",
        created=1618884473,
    )
    params = (("created", 1618884473), ("keyid", "test-key-ed25519"))
    base = signature_base(rfc_test_request(), covered, params)
    assert base == (
        b'"date": Tue, 20 Apr 2021 02:07:55 GMT\n'
        b'"@method": POST\n'
        b'"@path": /foo\n'
        b'"@authority": example.com\n'
        b'"content-type": application/json\n'
        b'"content-length": 18\n'
        b'"@signature-params": ("date" "@method" "@path" "@authority" '
        b'"content-type" "content-length");created=1618884473;keyid="test-key-ed25519"'
    )
    assert sig_input == (
        'sig-b26=("date" "@method" "@path" "@authority" "content-type" "content-length")'
        ';created=1618884473;keyid="test-key-ed25519"'
    )
    assert signature == (
        "sig-b26=:wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lI"
        "p5WPpBKRCw==:"
    )


@pytest.mark.unit
def test_b25_hmac_sha256_signature_matches_rfc_bytes() -> None:
    key = SigningKey("test-shared-secret", "hmac-sha256", secret=base64.b64decode(RFC_HMAC_KEY_B64))
    _, signature = sign_request(
        rfc_test_request(),
        key,
        components("date", "@authority", "content-type"),
        label="sig-b25",
        created=1618884473,
    )
    assert signature == "sig-b25=:pxcQw6G3AjtMBQjwo8XzkZf/bws5LelbaMk5rGIGtE8=:"


B4_SIGNATURE_INPUT = (
    'transform=("@method" "@path" "@authority" "accept")'
    ';created=1618884473;keyid="test-key-ed25519"'
)
B4_SIGNATURE = (
    "transform=:ZT1kooQsEHpZ0I1IjCqtQppOmIqlJPeo7DHR3SoMn0s5JZ1eRGS0A+vyYP9t/LXlh5QMFFQ6cpLt2m0pmj3"
    "NDA==:"
)


def b4_request(
    method: str, host: str, query: str, fields: tuple[tuple[str, str], ...]
) -> HttpRequest:
    headers = (("Host", host), *fields)
    headers += (("Signature-Input", B4_SIGNATURE_INPUT), ("Signature", B4_SIGNATURE))
    return HttpRequest(method, "https", host, "/demo", query, headers)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("case", "method", "host", "query", "fields", "valid"),
    [
        ("original", "GET", "example.org", "name1=Value1&Name2=value2",
         (("Accept", "application/json"), ("Accept", "*/*")), True),
        ("unsigned header and query added", "GET", "example.org",
         "name1=Value1&Name2=value2&param=added",
         (("Accept", "application/json"), ("Accept", "*/*"),
          ("Accept-Language", "en-US,en;q=0.5")), True),
        ("accept lines collapsed", "GET", "example.org", "name1=Value1&Name2=value2",
         (("Referer", "https://developer.example.org/demo"),
          ("Accept", "application/json, */*")), True),
        ("method and authority changed", "POST", "example.com", "name1=Value1&Name2=value2",
         (("Accept", "application/json"), ("Accept", "*/*")), False),
        ("accept lines reordered", "GET", "example.org", "name1=Value1&Name2=value2",
         (("Accept", "*/*"), ("Accept", "application/json")), False),
    ],
)  # fmt: skip
def test_b4_transformations_keep_or_break_the_signature(
    case: str,
    method: str,
    host: str,
    query: str,
    fields: tuple[tuple[str, str], ...],
    valid: bool,
) -> None:
    public = rfc_ed25519().public_key().public_bytes_raw()
    keys = {"test-key-ed25519": VerificationKey("test-key-ed25519", "ed25519", public)}
    policy = Policy(components("@method", "@path", "@authority"), max_age=60, require_nonce=False)
    request = b4_request(method, host, query, fields)
    if valid:
        assert verify_request(request, keys, policy, now=1618884473).label == "transform", case
    else:
        with pytest.raises(SignatureError, match="bad_signature"):
            verify_request(request, keys, policy, now=1618884473)


@pytest.mark.unit
def test_section_2_1_field_canonicalization_examples() -> None:
    request = HttpRequest(
        "GET", "https", "www.example.com", "/", "",
        (
            ("X-OWS-Header", "   Leading and trailing whitespace.   "),
            ("X-Obs-Fold-Header", "Obsolete\r\n    line folding."),
            ("Cache-Control", "max-age=60"),
            ("Cache-Control", "   must-revalidate"),
            ("X-Empty-Header", ""),
        ),
    )  # fmt: skip
    base = signature_base(
        request,
        components("x-ows-header", "x-obs-fold-header", "cache-control", "x-empty-header"),
        (),
    )
    assert base.split(b"\n")[:4] == [
        b'"x-ows-header": Leading and trailing whitespace.',
        b'"x-obs-fold-header": Obsolete line folding.',
        b'"cache-control": max-age=60, must-revalidate',
        b'"x-empty-header": ',
    ]


@pytest.mark.unit
def test_bs_parameter_separates_two_lines_from_one_combined_line() -> None:
    two = HttpRequest(
        "GET",
        "https",
        "a",
        "/",
        "",
        (("Example-Header", "value, with, lots"), ("Example-Header", "of, commas")),
    )
    one = HttpRequest("GET", "https", "a", "/", "",
                      (("Example-Header", "value, with, lots, of, commas"),))  # fmt: skip
    plain = components("example-header")
    assert signature_base(two, plain, ()) == signature_base(one, plain, ())  # the collision
    wrapped = (Component("example-header", (("bs", True),)),)
    assert signature_base(two, wrapped, ()).split(b"\n")[0] == (
        b'"example-header";bs: :dmFsdWUsIHdpdGgsIGxvdHM=:, :b2YsIGNvbW1hcw==:'
    )
    assert signature_base(one, wrapped, ()).split(b"\n")[0] == (
        b'"example-header";bs: :dmFsdWUsIHdpdGgsIGxvdHMsIG9mLCBjb21tYXM=:'
    )


@pytest.mark.unit
def test_derived_names_are_never_read_from_header_fields() -> None:
    forged = HttpRequest("POST", "https", "example.com", "/foo", "", (("@method", "GET"),))
    assert signature_base(forged, components("@method"), ()).startswith(b'"@method": POST\n')


@pytest.mark.unit
def test_base_rejects_duplicates_and_self_reference() -> None:
    request = rfc_test_request()
    with pytest.raises(SignatureBaseError, match="duplicate"):
        signature_base(request, components("@method", "@method"), ())
    with pytest.raises(SignatureBaseError, match="must not be a covered"):
        signature_base(request, components("@signature-params"), ())
    with pytest.raises(SignatureBaseError, match="not present"):
        signature_base(request, components("authorization"), ())


@pytest.mark.unit
def test_authority_normalization_lowercases_and_drops_default_port() -> None:
    assert normalize_authority("Example.COM:443", "https") == "example.com"
    assert normalize_authority("Example.COM:8443", "https") == "example.com:8443"
    assert normalize_authority("example.com:443", "http") == "example.com:443"
    assert normalize_authority("[2001:DB8::1]:80", "http") == "[2001:db8::1]"


# --------------------------------------------------------------------------------------
# RFC 9530 Content-Digest.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_appendix_d_and_b1_digest_values() -> None:
    assert content_digest(HELLO, ("sha-256", "sha-512")) == (
        f"sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:, sha-512=:{RFC_SHA512_HELLO}:"
    )
    assert content_digest(HELLO + b"\n") == "sha-256=:RK/0qy18MlBSVnWgjwz6lZEWjP/lF5HF9bvEF8FabDg=:"
    assert content_digest(b"") == "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:"


@pytest.mark.unit
def test_digest_verification_rejects_weak_only_and_mixed_mismatch() -> None:
    assert verify_content_digest(content_digest(HELLO, ("sha-256", "sha-512")), HELLO) == "sha-512"
    md5_only = "md5=:Sd/dVLAcvNLSq16eXua5uQ==:"  # RFC 9530 Appendix D, Deprecated
    with pytest.raises(ValueError, match="no Active digest"):
        verify_content_digest(md5_only, HELLO)
    good_256 = content_digest(HELLO)
    bad_512 = "sha-512=:" + base64.b64encode(hashlib.sha512(b"other").digest()).decode() + ":"
    with pytest.raises(ValueError, match="sha-512 digest mismatch"):
        verify_content_digest(f"{good_256}, {bad_512}", HELLO)


@pytest.mark.unit
def test_erratum_8890_brotli_bytes_and_digest() -> None:
    decompress = cast("Callable[[bytes], bytes]", importlib.import_module("brotli").decompress)
    printed = bytes.fromhex("8B08807B2268656C6C6F223A2022776F726C64227D0A03")
    corrected = bytes.fromhex("0B09807B2268656C6C6F223A2022776F726C64227D0A03")
    with pytest.raises(Exception, match="brotli: decoder failed"):
        decompress(printed)
    assert decompress(corrected) == HELLO + b"\n"
    assert content_digest(corrected) == "sha-256=:d435Qo+nKZ+gLcUHn7GQtQ72hiBVAgqoLsZnZPiTGPk=:"
    assert content_digest(printed) == "sha-256=:MklYnI/SsUF/5X7enJ2TU+DFjodRObdKLFaPPLe/Kcw=:"


# --------------------------------------------------------------------------------------
# Verifier policy: the attacks from RFC 9421 Section 7.
# --------------------------------------------------------------------------------------

LEGIT = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
LEGIT_PUBLIC = LEGIT.public_key().public_bytes_raw()
KEYS = {"client-a": VerificationKey("client-a", "ed25519", LEGIT_PUBLIC)}
COVER = components("@method", "@authority", "@path", "content-digest", "content-type")


def signed(
    body: bytes = b'{"amount": 10}',
    *,
    created: int = NOW,
    key: SigningKey | None = None,
    nonce: str | None = "n-1",
    tag: str | None = "payments-v1",
    include_alg: bool = False,
    expires: int | None = None,
    cover: tuple[Component, ...] = COVER,
) -> HttpRequest:
    unsigned = HttpRequest(
        "POST", "https", "api.example", "/payments", "",
        (("Content-Type", "application/json"), ("Content-Digest", content_digest(body))),
        body,
    )  # fmt: skip
    sig_input, signature = sign_request(
        unsigned,
        key or SigningKey("client-a", "ed25519", private=LEGIT),
        cover,
        label="sig1",
        created=created,
        expires=expires,
        nonce=nonce,
        tag=tag,
        include_alg=include_alg,
    )
    headers = (*unsigned.headers, ("Signature-Input", sig_input), ("Signature", signature))
    return HttpRequest("POST", "https", "api.example", "/payments", "", headers, body)


def policy() -> Policy:
    return Policy(COVER, max_age=300, max_skew=5, tag="payments-v1")


def reason(request: HttpRequest, pol: Policy | None = None, now: int = NOW) -> str:
    with pytest.raises(SignatureError) as info:
        verify_request(request, KEYS, pol or policy(), now)
    return info.value.reason


@pytest.mark.unit
def test_valid_signature_verifies_and_reports_digest() -> None:
    verified = verify_request(signed(), KEYS, policy(), NOW)
    assert (verified.keyid, verified.digest_alg) == ("client-a", "sha-256")


@pytest.mark.unit
def test_body_swap_with_untouched_digest_is_rejected() -> None:
    original = signed(b'{"amount": 10}')
    swapped = replace(original, content=b'{"amount": 99}')
    assert reason(swapped) == "digest_mismatch"


@pytest.mark.unit
def test_alg_parameter_cannot_downgrade_ed25519_key_to_hmac() -> None:
    forger = SigningKey("client-a", "hmac-sha256", secret=LEGIT_PUBLIC)  # public key as secret
    forged = signed(key=forger, include_alg=True)
    sig_input = dict(forged.headers)["Signature-Input"]
    member = parse_dictionary(sig_input)["sig1"]
    assert isinstance(member, InnerList)
    cover = tuple(Component(cast("str", i.value), i.params) for i in member.items)
    base = signature_base(forged, cover, member.params)
    raw = parse_dictionary(dict(forged.headers)["Signature"])["sig1"]
    assert isinstance(raw, Item)
    assert isinstance(raw.value, bytes)
    assert http_verify("hmac-sha256", LEGIT_PUBLIC, base, raw.value)  # a naive verifier accepts
    assert reason(forged) == "alg_mismatch"


@pytest.mark.unit
def test_replayed_nonce_is_rejected_but_failed_attempts_do_not_burn_it() -> None:
    pol = policy()
    tampered = signed(nonce="n-7")
    tampered = replace(tampered, method="PUT")
    assert reason(tampered, pol) == "bad_signature"
    assert verify_request(signed(nonce="n-7"), KEYS, pol, NOW).label == "sig1"
    assert reason(signed(nonce="n-7"), pol) == "replayed_nonce"


@pytest.mark.unit
def test_time_window_rejects_stale_future_and_expired() -> None:
    assert reason(signed(created=NOW - 301)) == "stale_or_future"
    assert reason(signed(created=NOW + 6)) == "stale_or_future"
    assert reason(signed(expires=NOW)) == "expired"
    assert verify_request(signed(created=NOW - 300), KEYS, policy(), NOW).label == "sig1"


@pytest.mark.unit
def test_policy_enforces_coverage_nonce_and_tag() -> None:
    assert reason(signed(cover=components("@method", "@authority", "@path"))) == (
        "insufficient_coverage"
    )
    assert reason(signed(nonce=None)) == "missing_nonce"
    assert reason(signed(tag="other-app")) == "tag_skipped"


@pytest.mark.unit
def test_injected_signature_from_unknown_key_does_not_deny_valid_one() -> None:
    legit = signed()
    evil_key = SigningKey(
        "mallory", "ed25519", private=Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    )
    evil = signed(key=evil_key, nonce="n-evil")
    evil_in = dict(evil.headers)["Signature-Input"].replace("sig1=", "evil=", 1)
    evil_sig = dict(evil.headers)["Signature"].replace("sig1=", "evil=", 1)
    both = replace(
        legit, headers=(*legit.headers, ("Signature-Input", evil_in), ("Signature", evil_sig))
    )
    assert verify_request(both, KEYS, policy(), NOW).label == "sig1"
    only_evil = replace(
        legit, headers=(*legit.headers[:2], ("Signature-Input", evil_in), ("Signature", evil_sig))
    )
    assert reason(only_evil) == "unknown_key"


@pytest.mark.unit
def test_malformed_or_mismatched_signature_fields_fail_closed() -> None:
    good = signed()
    unterminated = dict(good.headers)["Signature-Input"].replace('"@path"', '"@path', 1)
    with pytest.raises(SFError, match=r"unterminated string|bad inner list"):
        parse_dictionary(unterminated)
    broken = replace(
        good, headers=(*good.headers[:2], ("Signature-Input", unterminated), good.headers[3])
    )
    assert reason(broken) == "malformed_signature_fields"
    relabelled_sig = good.headers[3][1].replace("sig1=", "sig2=", 1)
    relabelled = replace(good, headers=(*good.headers[:3], ("Signature", relabelled_sig)))
    assert reason(relabelled) == "label_mismatch"


@pytest.mark.unit
def test_rfc_b26_signature_without_content_digest_fails_strict_policy() -> None:
    public = rfc_ed25519().public_key().public_bytes_raw()
    keys = {"test-key-ed25519": VerificationKey("test-key-ed25519", "ed25519", public)}
    sig_input, signature = sign_request(
        rfc_test_request(),
        SigningKey("test-key-ed25519", "ed25519", private=rfc_ed25519()),
        components("date", "@method", "@path", "@authority", "content-type", "content-length"),
        label="sig-b26",
        created=1618884473,
    )
    base_request = rfc_test_request()
    request = replace(
        base_request,
        headers=(*base_request.headers, ("Signature-Input", sig_input), ("Signature", signature)),
    )
    loose = Policy(components("@method"), max_age=60, require_nonce=False)
    with pytest.raises(SignatureError, match="content_not_covered"):
        verify_request(request, keys, loose, now=1618884473)


@pytest.mark.unit
def test_signature_sizes_on_the_wire() -> None:
    ed_member = dict(signed().headers)["Signature"]
    hmac_key = SigningKey("h", "hmac-sha256", secret=b"k" * 32)
    hmac_member = dict(signed(key=hmac_key).headers)["Signature"]
    assert len(ed_member) == len("sig1=::") + 88  # 64 bytes -> 88 base64 characters
    assert len(hmac_member) == len("sig1=::") + 44  # 32 bytes -> 44 base64 characters


# --------------------------------------------------------------------------------------
# End to end: httpx Auth on the client, Starlette verifier on the server.
# --------------------------------------------------------------------------------------


def client_auth(nonces: list[str]) -> HttpSignatureAuth:
    return HttpSignatureAuth(
        SigningKey("client-a", "ed25519", private=LEGIT),
        COVER,
        clock=lambda: NOW,
        tag="payments-v1",
        nonce_factory=nonces.pop,
    )


def server() -> Starlette:
    pol = Policy(COVER, max_age=300, tag="payments-v1")
    return make_app(KEYS, pol, clock=lambda: NOW)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_e2e_signed_request_accepted_then_replay_rejected() -> None:
    app = server()
    auth = client_auth(["n-2", "n-1"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example") as client:
        first = await client.post("/payments", content=b'{"amount": 10}', auth=auth,
                                  headers={"Content-Type": "application/json"})  # fmt: skip
        assert first.status_code == 200
        assert first.json() == {"keyid": "client-a", "label": "sig1", "digest": "sha-256"}
        assert first.request.headers["content-digest"].startswith("sha-256=:")
        replay = httpx.Request("POST", first.request.url, headers=first.request.headers,
                               content=b'{"amount": 10}')  # fmt: skip
        again = await client.send(replay)
        assert (again.status_code, again.json()["reason"]) == (401, "replayed_nonce")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_e2e_tampered_body_rejected_with_accept_signature_hint() -> None:
    transport = httpx.ASGITransport(app=server())
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example") as client:
        request = client.build_request("POST", "/payments", content=b'{"amount": 10}',
                                       headers={"Content-Type": "application/json"})  # fmt: skip
        flow = client_auth(["n-3"]).sync_auth_flow(request)
        signed_request = next(flow)
        tampered = httpx.Request("POST", signed_request.url, headers=signed_request.headers,
                                 content=b'{"amount": 99}')  # fmt: skip
        response = await client.send(tampered)
    assert (response.status_code, response.json()["reason"]) == (401, "digest_mismatch")
    hint = parse_dictionary(response.headers["accept-signature"])["sig1"]
    assert isinstance(hint, InnerList)
    assert [i.value for i in hint.items] == [c.name for c in COVER]
    assert dict(hint.params)["tag"] == "payments-v1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_e2e_streamed_body_is_read_before_signing() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"amount": '
        yield b"10}"

    transport = httpx.ASGITransport(app=server())
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example") as client:
        response = await client.post("/payments", content=chunks(), auth=client_auth(["n-4"]),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_url_is_not_a_signature_context() -> None:
    async def echo(request: Request) -> JSONResponse:
        message = await request_from_starlette(request)
        return JSONResponse(
            {
                "netloc": request.url.netloc,
                "url_path": request.url.path,
                "raw_path": message.raw_path,
                "authority": normalize_authority(message.authority, message.scheme),
            }
        )

    app = Starlette(routes=[Route("/{rest:path}", echo)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example") as client:
        response = await client.get("/a%2Fb", headers={"Host": "API.Example:443"})
    assert response.json() == {
        "netloc": "API.Example:443",
        "url_path": "/a/b",
        "raw_path": "/a%2Fb",
        "authority": "api.example",
    }


@pytest.mark.unit
def test_hypercorn_maps_h2_h3_authority_to_host_for_the_verifier() -> None:
    headers = [(b":method", b"POST"), (b":authority", b"api.example"), (b"host", b"other")]
    assert filter_pseudo_headers(headers) == [(b"host", b"api.example")]
