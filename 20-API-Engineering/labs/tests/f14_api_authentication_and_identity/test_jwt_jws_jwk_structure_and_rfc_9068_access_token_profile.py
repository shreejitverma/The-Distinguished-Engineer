"""Lab: JWS structure, JWK thumbprints, and the RFC 9068 JWT access token profile.

RFC test vectors are reproduced byte for byte; every library comparison pins joserfc 1.8.0
and PyJWT 2.15.1 as locked in uv.lock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from joserfc import jws
from joserfc import jwt as jose_jwt
from joserfc.errors import (
    BadSignatureError,
    ExceededSizeError,
    ExpiredTokenError,
    SecurityWarning,
    UnsupportedAlgorithmError,
    UnsupportedHeaderError,
)
from joserfc.jwk import ECKey, OctKey, OKPKey, RSAKey
from joserfc.jwt import JWTClaimsRegistry

from api_labs.f14_api_authentication_and_identity.jwt_access_token import (
    REQUIRED_CLAIMS,
    AccessTokenPolicy,
    SigningKey,
    TokenRejected,
    b64url_decode,
    b64url_encode,
    bearer_challenge,
    build_resource_server,
    ed25519_signing_key,
    es256_signing_key,
    is_access_token_typ,
    jwk_set,
    jwk_thumbprint,
    mint_access_token,
    parse_json_object,
    rs256_signing_key,
    validate_access_token,
)

NOW = 1_794_700_800
ISSUER = "https://as.example.com/"
RESOURCE = "https://api.example.com/orders"
ED = ed25519_signing_key(bytes(range(32)))
ES = es256_signing_key(0x5EED_0001_0002_0003)
POLICY = AccessTokenPolicy(issuer=ISSUER, audience=RESOURCE)
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493
# PyJWT 2.15.1 reads the wall clock, so tokens it checks span 2023-11-14 to 2055-07-24.
WALL_CLOCK_SAFE = {"issued_at": 1_700_000_000, "lifetime_s": 1_000_000_000}

# RFC 7515 Appendix A.1 and A.3 inputs, copied from the RFC text.
RFC7515_HMAC_JWK: dict[str, str | list[str]] = {
    "kty": "oct",
    "k": "AyM1SysPpbyDfgZld3umj1qzKObwVMkoqQ-EstJQLr_T-1qS0gZH75aKtMN3Yj0iPS4hcgUuTwjAzZr1Z9CAow",
}
RFC7515_PAYLOAD_B64 = (
    "eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
)
RFC7515_A3_TOKEN = (
    "eyJhbGciOiJFUzI1NiJ9."
    + RFC7515_PAYLOAD_B64
    + ".DtEhU3ljbEg8L38VWAfUAqOyKAM6-Xx-F4GawxaepmXFCgfTjDxw5djxLa8ISlSApmWQxfKTUJqPP3-Kg6NU1Q"
)
RFC7515_A3_JWK: dict[str, str | list[str]] = {
    "kty": "EC",
    "crv": "P-256",
    "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
    "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
}
RFC8037_ED25519_JWK: dict[str, str | list[str]] = {
    "kty": "OKP",
    "crv": "Ed25519",
    "d": "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A",
    "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
}
RFC7638_RSA_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPeb"
    "WKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY368QQ"
    "MicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2N"
    "cRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)


@pytest.fixture(scope="module")
def rs() -> SigningKey:
    return rs256_signing_key()


def mint(key: SigningKey = ED, **overrides: Any) -> str:
    params: dict[str, Any] = {
        "issuer": ISSUER,
        "subject": "user-5ba552d67",
        "audience": RESOURCE,
        "client_id": "s6BhdRkqt3",
        "issued_at": NOW - 30,
        "lifetime_s": 300,
        "jti": "dbe39bf3a3ba4238a513f51d6e1691c4",
        "scope": ("orders:read",),
    }
    params.update(overrides)
    return mint_access_token(key, **params)


def sign_raw(key: SigningKey, header: dict[str, Any], payload: bytes) -> str:
    """Sign arbitrary payload bytes, bypassing the minting helper's JSON encoder."""
    registry = jws.JWSRegistry(algorithms=[key.alg], strict_check_header=False)
    return jws.serialize_compact(header, payload, key.private, registry=registry)


def sign_ed25519_by_hand(header: dict[str, Any], payload: bytes) -> str:
    """Sign without joserfc, which refuses to emit a crit it does not understand."""
    signing_input = f"{b64url_encode(json.dumps(header).encode())}.{b64url_encode(payload)}"
    raw = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    return f"{signing_input}.{b64url_encode(raw.sign(signing_input.encode()))}"


def claims_bytes(drop: str | None = None, **override: Any) -> bytes:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "exp": NOW + 300,
        "aud": RESOURCE,
        "sub": "user-1",
        "client_id": "c1",
        "iat": NOW,
        "jti": "j1",
        **override,
    }
    if drop is not None:
        del claims[drop]
    return json.dumps(claims).encode()


def at_header(key: SigningKey = ED, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"typ": "at+jwt", "alg": key.alg, "kid": key.kid, **(extra or {})}


def reject_reason(token: str, policy: AccessTokenPolicy = POLICY, now: int = NOW) -> str:
    with pytest.raises(TokenRejected) as info:
        validate_access_token(token, jwk_set(ED, ES), policy, now=now)
    return info.value.reason


# --- base64url (RFC 7515 Section 2 and Appendix C) ----------------------------------------


@pytest.mark.unit
def test_rfc7515_appendix_c_base64url_example_round_trips() -> None:
    assert b64url_encode(bytes([3, 236, 255, 224, 193])) == "A-z_4ME"
    assert b64url_decode("A-z_4ME") == bytes([3, 236, 255, 224, 193])
    assert b64url_encode(b"") == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    "spelling",
    ["A-z_4ME=", "A-z+4ME", "A-z_4ME!", "A-z_4MF", "A-z_4ME A", "AAAAA"],
    ids=["padding", "std-alphabet", "junk", "trailing-bits", "space", "len-1-mod-4"],
)
def test_alternate_base64url_spellings_are_rejected(spelling: str) -> None:
    with pytest.raises(TokenRejected):
        b64url_decode(spelling)


# --- RFC test vectors -----------------------------------------------------------------------


@pytest.mark.unit
def test_rfc7515_a1_hs256_signature_is_byte_exact() -> None:
    header_b64 = "eyJ0eXAiOiJKV1QiLA0KICJhbGciOiJIUzI1NiJ9"
    signing_input = f"{header_b64}.{RFC7515_PAYLOAD_B64}".encode("ascii")
    key = b64url_decode(str(RFC7515_HMAC_JWK["k"]))
    mac = hmac.new(key, signing_input, hashlib.sha256).digest()
    assert b64url_encode(mac) == "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    # The header and payload contain CRLF and a leading space: nothing is canonicalized.
    assert b64url_decode(header_b64) == b'{"typ":"JWT",\r\n "alg":"HS256"}'
    token = f"{header_b64}.{RFC7515_PAYLOAD_B64}.{b64url_encode(mac)}"
    obj = jws.deserialize_compact(token, OctKey.import_key(RFC7515_HMAC_JWK), algorithms=["HS256"])
    assert obj.payload is not None
    assert obj.payload.startswith(b'{"iss":"joe",\r\n')


@pytest.mark.unit
def test_rfc7515_a3_es256_verifies_and_a_der_signature_does_not() -> None:
    key = ECKey.import_key(RFC7515_A3_JWK)
    assert jws.deserialize_compact(RFC7515_A3_TOKEN, key, algorithms=["ES256"]).payload
    head, _, sig_b64 = RFC7515_A3_TOKEN.rpartition(".")
    raw = b64url_decode(sig_b64)
    assert len(raw) == 64
    r, s = int.from_bytes(raw[:32]), int.from_bytes(raw[32:])
    der = encode_dss_signature(r, s)
    assert len(der) != 64
    with pytest.raises(BadSignatureError):
        jws.deserialize_compact(f"{head}.{b64url_encode(der)}", key, algorithms=["ES256"])


@pytest.mark.unit
def test_rfc8037_a4_ed25519_jws_is_byte_exact_and_eddsa_is_deprecated() -> None:
    key = OKPKey.import_key(RFC8037_ED25519_JWK)
    with pytest.warns(SecurityWarning, match="RFC 9864"):
        compact = jws.serialize_compact(
            {"alg": "EdDSA"}, b"Example of Ed25519 signing", key, algorithms=["EdDSA"]
        )
    assert compact == (
        "eyJhbGciOiJFZERTQSJ9.RXhhbXBsZSBvZiBFZDI1NTE5IHNpZ25pbmc."
        "hgyY0il_MGCjP0JzlnLWG1PPOt7-09PGcvMg3AIbQR6dWbhijcNR4ki4iylGjg5BhVsPt9g7sVvpAr_MuM0KAg"
    )
    fully_specified = jws.serialize_compact(
        {"alg": "Ed25519"}, b"Example of Ed25519 signing", key, algorithms=["Ed25519"]
    )
    assert fully_specified.split(".")[0] == "eyJhbGciOiJFZDI1NTE5In0"
    assert fully_specified.split(".")[2] != compact.split(".")[2]
    with pytest.raises(UnsupportedAlgorithmError):
        jws.deserialize_compact(compact, key, algorithms=["Ed25519"])


@pytest.mark.unit
def test_rfc7638_and_rfc8037_thumbprint_vectors() -> None:
    rsa_jwk = {"kty": "RSA", "n": RFC7638_RSA_N, "e": "AQAB", "alg": "RS256", "kid": "2011-04-29"}
    assert jwk_thumbprint(rsa_jwk) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"
    assert jwk_thumbprint(RFC8037_ED25519_JWK) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    assert OKPKey.import_key(RFC8037_ED25519_JWK).thumbprint() == jwk_thumbprint(
        RFC8037_ED25519_JWK
    )


@pytest.mark.unit
def test_thumbprint_ignores_optional_members_and_private_parts() -> None:
    public = ED.public_jwk()
    bare = {k: public[k] for k in ("kty", "crv", "x")}
    assert jwk_thumbprint(public) == jwk_thumbprint(bare) == ED.kid
    private = ED.private.as_dict(private=True)
    assert "d" in private
    assert jwk_thumbprint(private) == ED.kid


@pytest.mark.unit
def test_rfc7515_appendix_e_crit_negative_case_is_rejected() -> None:
    compact = (
        "eyJhbGciOiJub25lIiwNCiAiY3JpdCI6WyJodHRwOi8vZXhhbXBsZS5jb20vVU5ERUZJTkVEIl0sDQogImh0dHA6"
        "Ly9leGFtcGxlLmNvbS9VTkRFRklORUQiOnRydWUNCn0.RkFJTA."
    )
    # Rejected by the crit check before the alg lookup, so no 'none' SecurityWarning fires.
    with pytest.raises(UnsupportedHeaderError):
        jws.deserialize_compact(compact, None, algorithms=["none"])
    critical = sign_ed25519_by_hand(
        at_header(extra={"crit": ["urn:example:ext"], "urn:example:ext": 1}), claims_bytes()
    )
    assert reject_reason(critical) == "crit names an extension this server does not understand"


# --- Wire layout of a minted token ------------------------------------------------------------


@pytest.mark.unit
def test_minted_token_has_the_rfc9068_header_and_claims_on_the_wire() -> None:
    header_b64, payload_b64, signature_b64 = mint().split(".")
    assert b64url_decode(header_b64) == (
        b'{"typ":"at+jwt","alg":"Ed25519","kid":"' + ED.kid.encode() + b'"}'
    )
    claims = json.loads(b64url_decode(payload_b64))
    assert list(claims) == [*REQUIRED_CLAIMS, "scope"]
    assert claims["exp"] - claims["iat"] == 300
    assert len(b64url_decode(signature_b64)) == 64


@pytest.mark.unit
def test_signature_sizes_per_algorithm(rs: SigningKey) -> None:
    sizes = {k.alg: len(b64url_decode(mint(k).split(".")[2])) for k in (ED, ES, rs)}
    assert sizes == {"Ed25519": 64, "ES256": 64, "RS256": 256}
    assert len(mint(rs).split(".")[2]) == 342


@pytest.mark.unit
@pytest.mark.parametrize("alg", ["Ed25519", "ES256", "RS256"])
def test_valid_token_for_each_allowed_algorithm(alg: str, rs: SigningKey) -> None:
    key = {"Ed25519": ED, "ES256": ES, "RS256": rs}[alg]
    token = validate_access_token(mint(key), jwk_set(ED, ES, rs), POLICY, now=NOW)
    assert (token.subject, token.client_id, token.scopes) == (
        "user-5ba552d67",
        "s6BhdRkqt3",
        frozenset({"orders:read"}),
    )


# --- Explicit typing (RFC 9068 Section 2.1 and 4, RFC 7515 Section 4.1.9) ----------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("typ", "accepted"),
    [
        ("at+jwt", True),
        ("at+JWT", True),
        ("AT+JWT", True),
        ("application/at+jwt", True),
        ("Application/AT+JWT", True),
        ("JWT", False),
        ("application/jwt", False),
        ("at+jwt; v=1", False),
        ("dpop+jwt", False),
        (None, False),
        (1, False),
    ],
)
def test_typ_is_compared_as_a_case_insensitive_media_type(typ: object, accepted: bool) -> None:
    assert is_access_token_typ(typ) is accepted


@pytest.mark.unit
def test_id_token_shaped_jwt_is_rejected_by_the_profile_but_accepted_by_both_libraries() -> None:
    id_token_like = mint(typ="JWT")
    assert reject_reason(id_token_like) == "typ is not at+jwt"
    decoded = jose_jwt.decode(id_token_like, ED.private, algorithms=["Ed25519"])
    assert decoded.header["typ"] == "JWT"
    public_pem = ES.private.as_pem(private=False)
    es_id_token = mint(ES, typ="JWT", **WALL_CLOCK_SAFE)
    claims = pyjwt.decode(
        es_id_token, public_pem, algorithms=["ES256"], audience=RESOURCE, issuer=ISSUER
    )
    assert claims["sub"] == "user-5ba552d67"


# --- Algorithm and key selection --------------------------------------------------------------


@pytest.mark.unit
def test_alg_none_is_rejected_before_any_key_lookup() -> None:
    header = b64url_encode(b'{"typ":"at+jwt","alg":"none"}')
    unsigned = f"{header}.{b64url_encode(claims_bytes())}."
    assert reject_reason(unsigned) == "alg is missing or none"


@pytest.mark.unit
def test_alg_outside_the_allow_list_is_rejected() -> None:
    header = b64url_encode(json.dumps({"typ": "at+jwt", "alg": "HS256", "kid": ED.kid}).encode())
    signing_input = f"{header}.{b64url_encode(b'{}')}"
    secret = json.dumps(ED.public_jwk()).encode()
    mac = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    assert reject_reason(f"{signing_input}.{b64url_encode(mac)}") == "alg is not allowed"


@pytest.mark.unit
def test_header_alg_must_match_the_selected_keys_alg() -> None:
    forged = sign_raw(ES, {"typ": "at+jwt", "alg": "ES256", "kid": ED.kid}, b"{}")
    assert reject_reason(forged) == "header alg does not match the key's alg"


@pytest.mark.unit
def test_kid_selection_rules() -> None:
    no_kid = sign_raw(ED, {"typ": "at+jwt", "alg": "Ed25519"}, claims_bytes())
    assert reject_reason(no_kid) == "no kid and the JWK Set holds more than one key"
    single = validate_access_token(no_kid, jwk_set(ED), POLICY, now=NOW)
    assert single.jti == "j1"
    assert reject_reason(sign_raw(ED, at_header(extra={"kid": "rotated-away"}), b"{}")) == (
        "no unique key for kid"
    )
    duplicated = {"keys": [ED.public_jwk(), ED.public_jwk()]}
    with pytest.raises(TokenRejected, match="no unique key for kid"):
        validate_access_token(mint(), duplicated, POLICY, now=NOW)
    numeric_kid = sign_ed25519_by_hand({"typ": "at+jwt", "alg": "Ed25519", "kid": 7}, b"{}")
    assert reject_reason(numeric_kid) == "kid is not a string"


@pytest.mark.unit
def test_jwk_set_entry_with_private_material_is_refused() -> None:
    leaked = {"keys": [{**ED.private.as_dict(private=True), "kid": ED.kid, "alg": "Ed25519"}]}
    with pytest.raises(TokenRejected, match="private or symmetric"):
        validate_access_token(mint(), leaked, POLICY, now=NOW)


# --- Claims (RFC 9068 Section 2.2 and 4, RFC 7519 Section 4.1) ---------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", REQUIRED_CLAIMS)
def test_each_required_claim_is_enforced(name: str) -> None:
    token = sign_raw(ED, at_header(), claims_bytes(drop=name))
    assert reject_reason(token) == f"missing required claims ['{name}']"


@pytest.mark.unit
def test_exp_boundary_and_leeway() -> None:
    token = mint(issued_at=NOW - 300, lifetime_s=300)  # exp == NOW
    strict = AccessTokenPolicy(issuer=ISSUER, audience=RESOURCE, leeway_s=0)
    assert reject_reason(token, strict) == "token expired"
    assert validate_access_token(token, jwk_set(ED), POLICY, now=NOW + 59).jti
    assert reject_reason(token, POLICY, now=NOW + 60) == "token expired"


@pytest.mark.unit
def test_iat_in_the_future_and_nbf_are_enforced() -> None:
    assert reject_reason(mint(issued_at=NOW + 61)) == "iat is in the future"
    assert validate_access_token(mint(issued_at=NOW + 60), jwk_set(ED), POLICY, now=NOW).jti
    later = mint(extra_claims={"nbf": NOW + 120})
    assert reject_reason(later) == "token not yet valid"


@pytest.mark.unit
def test_issuer_is_an_exact_string_match() -> None:
    assert reject_reason(mint(issuer="https://as.example.com")) == (
        "iss does not match the expected issuer"
    )
    assert reject_reason(mint(issuer="HTTPS://as.example.com/")) == (
        "iss does not match the expected issuer"
    )


@pytest.mark.unit
def test_audience_string_or_array_must_contain_this_resource_server() -> None:
    both = mint(audience=["https://api.example.com/billing", RESOURCE])
    assert validate_access_token(both, jwk_set(ED), POLICY, now=NOW).jti
    other = mint(audience=["https://api.example.com/billing"])
    assert reject_reason(other) == "aud does not contain this resource server"
    assert reject_reason(mint(audience=[])) == "aud is not a string or array of strings"
    assert reject_reason(mint(extra_claims={"aud": 7})) == "aud is not a string or array of strings"


@pytest.mark.unit
def test_scope_is_a_space_delimited_string() -> None:
    token = validate_access_token(
        mint(scope=("orders:read", "orders:write")), jwk_set(ED), POLICY, now=NOW
    )
    assert token.scopes == {"orders:read", "orders:write"}
    assert validate_access_token(mint(scope=()), jwk_set(ED), POLICY, now=NOW).scopes == set()
    assert reject_reason(mint(extra_claims={"scope": "a  b"})) == "scope has an empty scope-token"
    assert reject_reason(mint(extra_claims={"scope": ["a"]})) == "scope is not a string"


# --- Strict JSON (RFC 7515 Section 10.12, RFC 7519 Section 4) ----------------------------------


@pytest.mark.unit
def test_duplicate_claim_names_are_rejected_while_lenient_parsers_keep_the_last() -> None:
    payload = (
        b'{"iss":"https://as.example.com/","exp":%d,"aud":"https://api.example.com/billing",'
        b'"aud":"https://api.example.com/orders","sub":"u","client_id":"c","iat":%d,"jti":"j"}'
        % (NOW + 300, NOW)
    )
    token = sign_raw(ED, at_header(), payload)
    assert reject_reason(token) == "duplicate member name 'aud'"
    assert json.loads(payload)["aud"] == RESOURCE
    lenient = jose_jwt.decode(token, ED.private, algorithms=["Ed25519"])
    assert lenient.claims["aud"] == RESOURCE


@pytest.mark.unit
def test_nan_and_trailing_data_are_not_json() -> None:
    assert json.loads('{"exp": NaN}')["exp"] != json.loads('{"exp": NaN}')["exp"]
    with pytest.raises(TokenRejected, match="NaN is not JSON"):
        parse_json_object(b'{"exp": NaN}', "claims")
    with pytest.raises(TokenRejected, match="not valid JSON"):
        parse_json_object(b'{"tag":"value"}ABCD', "header")
    with pytest.raises(TokenRejected, match="not UTF-8"):
        parse_json_object(b'{"sub":"\xff"}', "claims")


@pytest.mark.unit
def test_string_exp_is_accepted_by_pyjwt_but_not_by_joserfc_or_the_profile() -> None:
    payload = json.loads(claims_bytes(iat=1_700_000_000, exp="9999999999"))
    token = sign_raw(ES, at_header(ES), json.dumps(payload).encode())
    public_pem = ES.private.as_pem(private=False)
    accepted = pyjwt.decode(token, public_pem, algorithms=["ES256"], audience=RESOURCE)
    assert accepted["exp"] == "9999999999"
    with pytest.raises(Exception, match="NumericDate"):
        JWTClaimsRegistry(now=NOW).validate(payload)
    assert reject_reason(token) == "exp is not a NumericDate"


# --- One token, many serializations --------------------------------------------------------------


@pytest.mark.unit
def test_padded_signature_is_a_second_serialization_only_pyjwt_accepts() -> None:
    token = mint(ES, **WALL_CLOCK_SAFE)
    padded = token + "=="
    assert hashlib.sha256(padded.encode()).digest() != hashlib.sha256(token.encode()).digest()
    public_pem = ES.private.as_pem(private=False)
    assert pyjwt.decode(padded, public_pem, algorithms=["ES256"], audience=RESOURCE)["jti"]
    with pytest.raises(BadSignatureError):
        jose_jwt.decode(padded, ES.private, algorithms=["ES256"])
    assert reject_reason(padded) == "segment is not unpadded base64url"


@pytest.mark.unit
def test_ecdsa_signatures_are_malleable_so_revoke_by_jti_not_by_token_string() -> None:
    token = mint(ES)
    head, _, sig_b64 = token.rpartition(".")
    raw = b64url_decode(sig_b64)
    r, s = int.from_bytes(raw[:32]), int.from_bytes(raw[32:])
    twin = f"{head}.{b64url_encode(r.to_bytes(32) + (P256_ORDER - s).to_bytes(32))}"
    assert twin != token
    first = validate_access_token(token, jwk_set(ES), POLICY, now=NOW)
    second = validate_access_token(twin, jwk_set(ES), POLICY, now=NOW)
    assert first.jti == second.jti
    # The same (r, n - s) pair also verifies directly in cryptography.
    public = ec.derive_private_key(0x5EED_0001_0002_0003, ec.SECP256R1()).public_key()
    der = encode_dss_signature(r, P256_ORDER - s)
    assert decode_dss_signature(der) == (r, P256_ORDER - s)
    public.verify(der, head.encode(), ec.ECDSA(hashes.SHA256()))


@pytest.mark.unit
def test_ed25519_rejects_the_non_canonical_s_plus_l_twin() -> None:
    token = mint(ED)
    head, _, sig_b64 = token.rpartition(".")
    raw = b64url_decode(sig_b64)
    s = int.from_bytes(raw[32:], "little")
    twin_s = (s + ED25519_ORDER).to_bytes(32, "little")
    twin = f"{head}.{b64url_encode(raw[:32] + twin_s)}"
    assert reject_reason(twin) == "signature verification failed"


# --- Structure and size limits ------------------------------------------------------------------


@pytest.mark.unit
def test_segment_count_distinguishes_jws_from_jwe() -> None:
    assert reject_reason("a.b.c.d.e") == "encrypted JWT (JWE) was not negotiated"
    assert reject_reason("a.b") == "not a JWS Compact Serialization"
    assert reject_reason(mint() + ".") == "not a JWS Compact Serialization"
    unsigned = mint().rpartition(".")[0] + "."
    assert reject_reason(unsigned) == "empty signature"


@pytest.mark.unit
def test_unknown_header_parameter_is_ignored_by_the_profile_but_refused_by_joserfc_default() -> (
    None
):
    token = sign_raw(ED, at_header(extra={"x-trace": "t1"}), claims_bytes())
    with pytest.raises(UnsupportedHeaderError):
        jws.deserialize_compact(token, ED.private, algorithms=["Ed25519"])
    assert validate_access_token(token, jwk_set(ED), POLICY, now=NOW).jti == "j1"


@pytest.mark.unit
def test_joserfc_jwt_decode_checks_the_signature_but_no_claims() -> None:
    expired = mint(issued_at=NOW - 900, lifetime_s=300)  # exp ten minutes before NOW
    decoded = jose_jwt.decode(expired, ED.private, algorithms=["Ed25519"])
    assert decoded.claims["exp"] == NOW - 600
    with pytest.raises(ExpiredTokenError):
        JWTClaimsRegistry(now=NOW).validate(decoded.claims)
    assert reject_reason(expired) == "token expired"


@pytest.mark.unit
def test_joserfc_default_registry_accepts_only_its_recommended_algorithms() -> None:
    with pytest.raises(UnsupportedAlgorithmError, match="not recommended"):
        jose_jwt.decode(mint(ED), ED.private)
    assert jose_jwt.decode(mint(ES), ES.private).claims["jti"]


@pytest.mark.unit
def test_joserfc_512_byte_header_cap_versus_the_profile_limit() -> None:
    long_kid = SigningKey("Ed25519", "k" * 400, ED.private)
    token = mint(long_kid)
    assert len(token.split(".")[0]) > 512
    with pytest.raises(ExceededSizeError):
        jws.deserialize_compact(token, ED.private, algorithms=["Ed25519"])
    assert validate_access_token(token, jwk_set(long_kid), POLICY, now=NOW).jti
    small = AccessTokenPolicy(issuer=ISSUER, audience=RESOURCE, max_token_bytes=256)
    assert reject_reason(token, small) == "token exceeds the size limit"


@pytest.mark.unit
def test_rsa_1024_is_warned_by_joserfc_and_refused_only_when_pyjwt_enforces() -> None:
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505 - the weak key is the point
    with pytest.warns(SecurityWarning, match="2048"):
        RSAKey.import_key(weak)
    strict = pyjwt.PyJWT(options={"enforce_minimum_key_length": True})
    with pytest.raises(pyjwt.InvalidKeyError, match="1024 bits"):
        strict.encode({"sub": "x"}, weak, algorithm="RS256")


# --- RFC 6750 over ASGI ----------------------------------------------------------------------


def _client(scope_required: str = "orders:read") -> httpx.AsyncClient:
    app = build_resource_server(
        jwk_set(ED, ES), POLICY, clock=lambda: NOW, required_scope=scope_required
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resource_server_status_codes_and_bearer_challenges() -> None:
    async with _client() as client:
        anonymous = await client.get("/orders")
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"] == 'Bearer realm="orders"'

        malformed = await client.get("/orders", headers={"Authorization": "Basic abc"})
        assert malformed.status_code == 400
        assert 'error="invalid_request"' in malformed.headers["www-authenticate"]

        expired = await client.get(
            "/orders",
            headers={"Authorization": f"Bearer {mint(issued_at=NOW - 100, lifetime_s=10)}"},
        )
        assert expired.status_code == 401
        assert expired.headers["www-authenticate"] == (
            'Bearer realm="orders", error="invalid_token", error_description="token expired"'
        )

        no_scope = await client.get(
            "/orders", headers={"Authorization": f"Bearer {mint(scope=('profile',))}"}
        )
        assert no_scope.status_code == 403
        assert no_scope.headers["www-authenticate"] == (
            'Bearer realm="orders", error="insufficient_scope", scope="orders:read"'
        )

        ok = await client.get("/orders", headers={"Authorization": f"Bearer {mint()}"})
        assert ok.status_code == 200
        assert ok.json() == {"sub": "user-5ba552d67", "client_id": "s6BhdRkqt3"}


@pytest.mark.unit
def test_bearer_challenge_strips_characters_rfc6750_forbids() -> None:
    challenge = bearer_challenge("r", "invalid_token", 'bad "kid" \\ here')
    assert challenge == 'Bearer realm="r", error="invalid_token", error_description="bad kid  here"'
