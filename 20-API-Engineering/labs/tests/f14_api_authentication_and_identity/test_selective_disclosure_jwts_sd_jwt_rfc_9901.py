"""RFC 9901 SD-JWT lab: issuance, selective presentation, Key Binding, and verifier rejections."""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from joserfc import jws
from joserfc.errors import ExceededSizeError, UnsupportedAlgorithmError
from joserfc.jwk import ECKey
from joserfc.jws import JWSRegistry

from api_labs.f14_api_authentication_and_identity.sd_jwt import (
    Disclosure,
    Issuer,
    KeyBinding,
    Sd,
    SdJwtError,
    TransactionStore,
    VerifierPolicy,
    b64url_decode,
    b64url_encode,
    build_verifier_app,
    present,
    sd_digest,
    split_presentation,
    verify_presentation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# RFC 9901 Section 5 vectors, copied from the RFC text with line breaks removed.
RFC_SD_JWT = (
    "eyJhbGciOiAiRVMyNTYiLCAidHlwIjogImV4YW1wbGUrc2Qtand0In0.eyJfc2QiOiBbIkNyUWU3UzVrcUJBSHQt"
    "bk1ZWGdjNmJkdDJTSDVhVFkxc1VfTS1QZ2tqUEkiLCAiSnpZakg0c3ZsaUgwUjNQeUVNZmVadTZKdDY5dTVxZWha"
    "bzdGN0VQWWxTRSIsICJQb3JGYnBLdVZ1Nnh5bUphZ3ZrRnNGWEFiUm9jMkpHbEFVQTJCQTRvN2NJIiwgIlRHZjRv"
    "TGJnd2Q1SlFhSHlLVlFaVTlVZEdFMHc1cnREc3JaemZVYW9tTG8iLCAiWFFfM2tQS3QxWHlYN0tBTmtxVlI2eVoy"
    "VmE1TnJQSXZQWWJ5TXZSS0JNTSIsICJYekZyendzY002R242Q0pEYzZ2Vks4QmtNbmZHOHZPU0tmcFBJWmRBZmRF"
    "IiwgImdiT3NJNEVkcTJ4Mkt3LXc1d1BFemFrb2I5aFYxY1JEMEFUTjNvUUw5Sk0iLCAianN1OXlWdWx3UVFsaEZs"
    "TV8zSmx6TWFTRnpnbGhRRzBEcGZheVF3TFVLNCJdLCAiaXNzIjogImh0dHBzOi8vaXNzdWVyLmV4YW1wbGUuY29t"
    "IiwgImlhdCI6IDE2ODMwMDAwMDAsICJleHAiOiAxODgzMDAwMDAwLCAic3ViIjogInVzZXJfNDIiLCAibmF0aW9u"
    "YWxpdGllcyI6IFt7Ii4uLiI6ICJwRm5kamtaX1ZDem15VGE2VWpsWm8zZGgta284YUlLUWM5RGxHemhhVllvIn0s"
    "IHsiLi4uIjogIjdDZjZKa1B1ZHJ5M2xjYndIZ2VaOGtoQXYxVTFPU2xlclAwVmtCSnJXWjAifV0sICJfc2RfYWxn"
    "IjogInNoYS0yNTYiLCAiY25mIjogeyJqd2siOiB7Imt0eSI6ICJFQyIsICJjcnYiOiAiUC0yNTYiLCAieCI6ICJU"
    "Q0FFUjE5WnZ1M09IRjRqNFc0dmZTVm9ISVAxSUxpbERsczd2Q2VHZW1jIiwgInkiOiAiWnhqaVdXYlpNUUdIVldL"
    "VlE0aGJTSWlyc1ZmdWVjQ0U2dDRqVDlGMkhaUSJ9fX0.MczwjBFGtzf-6WMT-hIvYbkb11NrV1WMO-jTijpMPNbs"
    "wNzZ87wY2uHz-CXo6R04b7jYrpj9mNRAvVssXou1iw~WyIyR0xDNDJzS1F2ZUNmR2ZyeU5STjl3IiwgImdpdmVuX"
    "25hbWUiLCAiSm9obiJd~WyJlbHVWNU9nM2dTTklJOEVZbnN4QV9BIiwgImZhbWlseV9uYW1lIiwgIkRvZSJd~WyI"
    "2SWo3dE0tYTVpVlBHYm9TNXRtdlZBIiwgImVtYWlsIiwgImpvaG5kb2VAZXhhbXBsZS5jb20iXQ~WyJlSThaV205"
    "UW5LUHBOUGVOZW5IZGhRIiwgInBob25lX251bWJlciIsICIrMS0yMDItNTU1LTAxMDEiXQ~WyJRZ19PNjR6cUF4Z"
    "TQxMmExMDhpcm9BIiwgInBob25lX251bWJlcl92ZXJpZmllZCIsIHRydWVd~WyJBSngtMDk1VlBycFR0TjRRTU9x"
    "Uk9BIiwgImFkZHJlc3MiLCB7InN0cmVldF9hZGRyZXNzIjogIjEyMyBNYWluIFN0IiwgImxvY2FsaXR5IjogIkFu"
    "eXRvd24iLCAicmVnaW9uIjogIkFueXN0YXRlIiwgImNvdW50cnkiOiAiVVMifV0~WyJQYzMzSk0yTGNoY1VfbEhn"
    "Z3ZfdWZRIiwgImJpcnRoZGF0ZSIsICIxOTQwLTAxLTAxIl0~WyJHMDJOU3JRZmpGWFE3SW8wOXN5YWpBIiwgInVw"
    "ZGF0ZWRfYXQiLCAxNTcwMDAwMDAwXQ~WyJsa2x4RjVqTVlsR1RQVW92TU5JdkNBIiwgIlVTIl0~WyJuUHVvUW5rU"
    "kZxM0JJZUFtN0FuWEZBIiwgIkRFIl0~"
)

RFC_PRESENTATION = (
    "eyJhbGciOiAiRVMyNTYiLCAidHlwIjogImV4YW1wbGUrc2Qtand0In0.eyJfc2QiOiBbIkNyUWU3UzVrcUJBSHQt"
    "bk1ZWGdjNmJkdDJTSDVhVFkxc1VfTS1QZ2tqUEkiLCAiSnpZakg0c3ZsaUgwUjNQeUVNZmVadTZKdDY5dTVxZWha"
    "bzdGN0VQWWxTRSIsICJQb3JGYnBLdVZ1Nnh5bUphZ3ZrRnNGWEFiUm9jMkpHbEFVQTJCQTRvN2NJIiwgIlRHZjRv"
    "TGJnd2Q1SlFhSHlLVlFaVTlVZEdFMHc1cnREc3JaemZVYW9tTG8iLCAiWFFfM2tQS3QxWHlYN0tBTmtxVlI2eVoy"
    "VmE1TnJQSXZQWWJ5TXZSS0JNTSIsICJYekZyendzY002R242Q0pEYzZ2Vks4QmtNbmZHOHZPU0tmcFBJWmRBZmRF"
    "IiwgImdiT3NJNEVkcTJ4Mkt3LXc1d1BFemFrb2I5aFYxY1JEMEFUTjNvUUw5Sk0iLCAianN1OXlWdWx3UVFsaEZs"
    "TV8zSmx6TWFTRnpnbGhRRzBEcGZheVF3TFVLNCJdLCAiaXNzIjogImh0dHBzOi8vaXNzdWVyLmV4YW1wbGUuY29t"
    "IiwgImlhdCI6IDE2ODMwMDAwMDAsICJleHAiOiAxODgzMDAwMDAwLCAic3ViIjogInVzZXJfNDIiLCAibmF0aW9u"
    "YWxpdGllcyI6IFt7Ii4uLiI6ICJwRm5kamtaX1ZDem15VGE2VWpsWm8zZGgta284YUlLUWM5RGxHemhhVllvIn0s"
    "IHsiLi4uIjogIjdDZjZKa1B1ZHJ5M2xjYndIZ2VaOGtoQXYxVTFPU2xlclAwVmtCSnJXWjAifV0sICJfc2RfYWxn"
    "IjogInNoYS0yNTYiLCAiY25mIjogeyJqd2siOiB7Imt0eSI6ICJFQyIsICJjcnYiOiAiUC0yNTYiLCAieCI6ICJU"
    "Q0FFUjE5WnZ1M09IRjRqNFc0dmZTVm9ISVAxSUxpbERsczd2Q2VHZW1jIiwgInkiOiAiWnhqaVdXYlpNUUdIVldL"
    "VlE0aGJTSWlyc1ZmdWVjQ0U2dDRqVDlGMkhaUSJ9fX0.MczwjBFGtzf-6WMT-hIvYbkb11NrV1WMO-jTijpMPNbs"
    "wNzZ87wY2uHz-CXo6R04b7jYrpj9mNRAvVssXou1iw~WyJlbHVWNU9nM2dTTklJOEVZbnN4QV9BIiwgImZhbWlse"
    "V9uYW1lIiwgIkRvZSJd~WyJBSngtMDk1VlBycFR0TjRRTU9xUk9BIiwgImFkZHJlc3MiLCB7InN0cmVldF9hZGRy"
    "ZXNzIjogIjEyMyBNYWluIFN0IiwgImxvY2FsaXR5IjogIkFueXRvd24iLCAicmVnaW9uIjogIkFueXN0YXRlIiwg"
    "ImNvdW50cnkiOiAiVVMifV0~WyIyR0xDNDJzS1F2ZUNmR2ZyeU5STjl3IiwgImdpdmVuX25hbWUiLCAiSm9obiJd"
    "~WyJsa2x4RjVqTVlsR1RQVW92TU5JdkNBIiwgIlVTIl0~eyJhbGciOiAiRVMyNTYiLCAidHlwIjogImtiK2p3dCJ"
    "9.eyJub25jZSI6ICIxMjM0NTY3ODkwIiwgImF1ZCI6ICJodHRwczovL3ZlcmlmaWVyLmV4YW1wbGUub3JnIiwgIm"
    "lhdCI6IDE3NDg1MzcyNDQsICJzZF9oYXNoIjogIjBfQWYtMkItRWhMV1g1eWRoX3cyeHp3bU82aU02NkJfMlFDRW"
    "FuSTRmVVkifQ.T3SIus2OidNl41nmVkTZVCKKhOAX97aOldMyHFiYjHm261eLiJ1YiuONFiMN8QlCmYzDlBLAdPv"
    "rXh52KaLgUQ"
)

# RFC 9901 Appendix A.5: the Issuer public key for every example in the RFC.
RFC_ISSUER_JWK: dict[str, str | list[str]] = {
    "kty": "EC",
    "crv": "P-256",
    "x": "b28d4MwZMjw8-00CG4xfnn9SLMVMM19SlqZpVb_uNtQ",
    "y": "Xv5zWwuoaTgdS6hV43yI6gBwTnjukmFQQnJ_kCxzqk8",
}

ISSUER = "https://issuer.example.com"
VERIFIER = "https://verifier.example.org"
TYP = "example+sd-jwt"
NOW = 1_800_000_000
ISSUER_KEY = ECKey.generate_key("P-256")
HOLDER_KEY = ECKey.generate_key("P-256")
HOLDER_JWK = cast("dict[str, object]", HOLDER_KEY.as_dict(private=False))
CLAIMS: dict[str, object] = {
    "iss": ISSUER,
    "iat": NOW - 60,
    "exp": NOW + 3600,
    "sub": "user_42",
    "given_name": Sd("John"),
    "family_name": Sd("Doe"),
    "email": Sd("johndoe@example.com"),
    "birthdate": Sd("1940-01-01"),
    "nationalities": [Sd("US"), Sd("DE")],
}


def counter_salt() -> Callable[[], str]:
    counter = itertools.count()
    return lambda: f"salt-{next(counter):04d}-padding-xx"


def make_issuer(decoys: int = 0) -> Issuer:
    return Issuer(ISSUER_KEY, TYP, decoys_per_object=decoys, salt=counter_salt())


def policy(*, key_binding: bool, key: ECKey = ISSUER_KEY, issuer: str = ISSUER) -> VerifierPolicy:
    return VerifierPolicy(
        issuer=issuer,
        issuer_key=key,
        expected_typ=TYP,
        audience=VERIFIER,
        require_key_binding=key_binding,
    )


def binding(nonce: str = "n-123", *, iat: int = NOW, aud: str = VERIFIER) -> KeyBinding:
    return KeyBinding(HOLDER_KEY, aud, nonce, iat)


def sign_raw(payload: dict[str, object], *, header: dict[str, object] | None = None) -> str:
    """Sign an arbitrary payload with the Issuer key, bypassing the Issuer's safety checks."""
    protected: dict[str, object] = {"alg": "ES256", "typ": TYP, **(header or {})}
    return jws.serialize_compact(protected, json.dumps(payload).encode(), ISSUER_KEY, ["ES256"])


def rejection(presentation: str, pol: VerifierPolicy, nonce: str | None = "n-123") -> str:
    with pytest.raises(SdJwtError) as info:
        verify_presentation(presentation, pol, nonce=nonce, now=NOW)
    return info.value.code


@pytest.mark.unit
def test_disclosure_and_digest_match_rfc_9901_section_4_2_vectors() -> None:
    family = Disclosure.create("_26bc4LT-ac6q2KI6cBW5es", "family_name", "M\u00f6bius")
    assert (
        family.encoded == "WyJfMjZiYzRMVC1hYzZxMktJNmNCVzVlcyIsICJmYW1pbHlfbmFtZSIsICJNw7ZiaXVzIl0"
    )
    assert sd_digest(family.encoded) == "X9yH0Ajrdm1Oij4tWso9UzzKJvPoDxwmuEcO3XAdRC0"
    element = Disclosure.create("lklxF5jMYlGTPUovMNIvCA", None, "FR")
    assert element.encoded == "WyJsa2x4RjVqTVlsR1RQVW92TU5JdkNBIiwgIkZSIl0"
    assert sd_digest(element.encoded) == "w0I8EKcdCtUPkGCNUrfwVp2xEgNjtoIDlOxc9-PlOhs"
    decoded_bytes_hash = b64url_encode(hashlib.sha256(b64url_decode(family.encoded)).digest())
    assert decoded_bytes_hash != sd_digest(family.encoded)


@pytest.mark.unit
def test_equivalent_json_encodings_have_different_digests() -> None:
    compact = "WyJfMjZiYzRMVC1hYzZxMktJNmNCVzVlcyIsImZhbWlseV9uYW1lIiwiTcO2Yml1cyJd"
    spaced = "WyJfMjZiYzRMVC1hYzZxMktJNmNCVzVlcyIsICJmYW1pbHlfbmFtZSIsICJNw7ZiaXVzIl0"
    assert Disclosure.parse(compact).value == Disclosure.parse(spaced).value == "M\u00f6bius"
    assert sd_digest(compact) != sd_digest(spaced)


@pytest.mark.unit
def test_rfc_9901_section_5_presentation_verifies_end_to_end() -> None:
    rfc_policy = policy(key_binding=True, key=ECKey.import_key(RFC_ISSUER_JWK))
    claims = verify_presentation(RFC_PRESENTATION, rfc_policy, nonce="1234567890", now=1748537244)
    assert claims == {
        "iss": "https://issuer.example.com",
        "iat": 1683000000,
        "exp": 1883000000,
        "sub": "user_42",
        "nationalities": ["US"],
        "cnf": {
            "jwk": {
                "kty": "EC",
                "crv": "P-256",
                "x": "TCAER19Zvu3OHF4j4W4vfSVoHIP1ILilDls7vCeGemc",
                "y": "ZxjiWWbZMQGHVWKVQ4hbSIirsVfuecCE6t4jT9F2HZQ",
            }
        },
        "family_name": "Doe",
        "address": {
            "street_address": "123 Main St",
            "locality": "Anytown",
            "region": "Anystate",
            "country": "US",
        },
        "given_name": "John",
    }
    sd_part = RFC_PRESENTATION[: RFC_PRESENTATION.rindex("~") + 1]
    assert sd_digest(sd_part) == "0_Af-2B-EhLWX5ydh_w2xzwmO6iM66B_2QCEanI4fUY"


@pytest.mark.unit
def test_rfc_9901_section_5_sizes() -> None:
    issuer_jwt, disclosures, kb_jwt = split_presentation(RFC_SD_JWT)
    assert (len(RFC_SD_JWT), len(issuer_jwt), len(disclosures), kb_jwt) == (1967, 1186, 10, None)
    assert len(RFC_PRESENTATION) == 1859
    assert {len(sd_digest(d)) for d in disclosures} == {43}
    assert {len(Disclosure.parse(d).salt) for d in disclosures} == {22}


@pytest.mark.unit
def test_holder_discloses_only_the_selected_claim_with_key_binding() -> None:
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    shown = present(sd_jwt, lambda d: d.name == "birthdate", binding())
    _, disclosures, kb_jwt = split_presentation(shown)
    assert len(disclosures) == 1
    assert kb_jwt is not None
    claims = verify_presentation(shown, policy(key_binding=True), nonce="n-123", now=NOW)
    assert claims["birthdate"] == "1940-01-01"
    assert claims["nationalities"] == []
    assert not {"given_name", "family_name", "email", "_sd", "_sd_alg"} & claims.keys()


@pytest.mark.unit
def test_unreferenced_disclosure_is_rejected_not_silently_dropped() -> None:
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    issuer_jwt, disclosures, _ = split_presentation(sd_jwt)
    forged = Disclosure.create("salt-forged", "family_name", "Smith").encoded
    tampered = "~".join([issuer_jwt, disclosures[0], forged]) + "~"
    assert rejection(tampered, policy(key_binding=False)) == "unreferenced_disclosure"


@pytest.mark.unit
def test_invalid_utf8_disclosure_is_rejected_not_replaced() -> None:
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    issuer_jwt, _, _ = split_presentation(sd_jwt)
    broken = b64url_encode(b'["salt", "family_name", "Sm\xf2ith"]')
    code = rejection(f"{issuer_jwt}~{broken}~", policy(key_binding=False))
    assert code == "malformed_json"


@pytest.mark.unit
def test_stripping_the_kb_jwt_does_not_downgrade_the_policy() -> None:
    shown = present(make_issuer().issue(CLAIMS, HOLDER_JWK), lambda d: True, binding())
    stripped = shown[: shown.rindex("~") + 1]
    assert rejection(stripped, policy(key_binding=True)) == "key_binding_required"
    assert rejection(shown, policy(key_binding=False)) == "unexpected_kb_jwt"


@pytest.mark.unit
def test_sd_hash_pins_the_exact_disclosure_set() -> None:
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    shown = present(sd_jwt, lambda d: d.name in {"email", "birthdate"}, binding())
    issuer_jwt, disclosures, kb_jwt = split_presentation(shown)
    assert kb_jwt is not None
    _, all_disclosures, _ = split_presentation(sd_jwt)
    dropped = "~".join([issuer_jwt, disclosures[0], kb_jwt])
    extra = next(d for d in all_disclosures if d not in disclosures)
    added = "~".join([issuer_jwt, *disclosures, extra, kb_jwt])
    assert rejection(dropped, policy(key_binding=True)) == "sd_hash_mismatch"
    assert rejection(added, policy(key_binding=True)) == "sd_hash_mismatch"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kb", "nonce", "code"),
    [
        (binding("n-123"), "n-other", "kb_nonce_mismatch"),
        (binding("n-123", aud="https://evil.example"), "n-123", "kb_audience_mismatch"),
        (binding("n-123", iat=NOW - 301), "n-123", "kb_iat_out_of_window"),
        (binding("n-123", iat=NOW + 31), "n-123", "kb_iat_out_of_window"),
    ],
)
def test_kb_jwt_replay_controls(kb: KeyBinding, nonce: str, code: str) -> None:
    shown = present(make_issuer().issue(CLAIMS, HOLDER_JWK), lambda d: True, kb)
    assert rejection(shown, policy(key_binding=True), nonce=nonce) == code


@pytest.mark.unit
def test_kb_jwt_must_be_explicitly_typed_and_signed_by_the_cnf_key() -> None:
    sd_part = present(make_issuer().issue(CLAIMS, HOLDER_JWK), lambda d: True, None)
    body = {"nonce": "n-123", "aud": VERIFIER, "iat": NOW, "sd_hash": sd_digest(sd_part)}
    raw = json.dumps(body).encode()
    untyped = jws.serialize_compact({"alg": "ES256", "typ": "JWT"}, raw, HOLDER_KEY, ["ES256"])
    stranger = ECKey.generate_key("P-256")
    foreign = jws.serialize_compact({"alg": "ES256", "typ": "kb+jwt"}, raw, stranger, ["ES256"])
    assert rejection(sd_part + untyped, policy(key_binding=True)) == "wrong_kb_typ"
    assert rejection(sd_part + foreign, policy(key_binding=True)) == "bad_kb_jwt"


@pytest.mark.unit
def test_issuer_signature_typ_and_alg_none_are_enforced() -> None:
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    header, payload, signature = sd_jwt.split("~")[0].split(".")
    body = json.loads(b64url_decode(payload))
    body["sub"] = "admin"
    forged = ".".join([header, b64url_encode(json.dumps(body).encode()), signature])
    unsigned_header = b64url_encode(json.dumps({"alg": "none", "typ": TYP}).encode())
    unsigned = f"{unsigned_header}.{payload}."
    assert rejection(forged + "~", policy(key_binding=False)) == "bad_issuer_jwt"
    with pytest.raises(SdJwtError) as info:
        verify_presentation(unsigned + "~", policy(key_binding=False), nonce=None, now=NOW)
    assert info.value.code == "bad_issuer_jwt"
    assert isinstance(info.value.__cause__, UnsupportedAlgorithmError)
    plain_jwt = Issuer(ISSUER_KEY, "JWT").issue(CLAIMS, HOLDER_JWK)
    assert rejection(plain_jwt, policy(key_binding=False)) == "wrong_typ"
    assert rejection(sd_jwt, policy(key_binding=False, key=HOLDER_KEY)) == "bad_issuer_jwt"


@pytest.mark.unit
def test_duplicate_digest_and_duplicate_disclosure_are_rejected() -> None:
    disclosure = Disclosure.create("salt-a", "email", "a@example.com").encoded
    digest = sd_digest(disclosure)
    base: dict[str, object] = {"iss": ISSUER, "exp": NOW + 60, "_sd_alg": "sha-256"}
    twice = sign_raw({**base, "_sd": [digest], "nested": {"_sd": [digest]}})
    once = sign_raw({**base, "_sd": [digest]})
    assert rejection(f"{twice}~{disclosure}~", policy(key_binding=False)) == "duplicate_digest"
    code = rejection(f"{once}~{disclosure}~{disclosure}~", policy(key_binding=False))
    assert code == "duplicate_disclosure"


@pytest.mark.unit
def test_claim_name_collision_and_reserved_names_are_rejected() -> None:
    collide = Disclosure.create("salt-b", "email", "attacker@example.com").encoded
    reserved = Disclosure.create("salt-c", "_sd", ["x"]).encoded
    base: dict[str, object] = {"iss": ISSUER, "exp": NOW + 60, "email": "real@example.com"}
    token = sign_raw({**base, "_sd": sorted([sd_digest(collide), sd_digest(reserved)])})
    assert rejection(f"{token}~{collide}~", policy(key_binding=False)) == "claim_name_collision"
    code = rejection(f"{token}~{reserved}~", policy(key_binding=False))
    assert code == "reserved_claim_name"


@pytest.mark.unit
def test_recursive_array_disclosures_need_their_parent() -> None:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "exp": NOW + 60,
        "nationalities": Sd([Sd("DE"), Sd("FR"), Sd("UK")]),
    }
    sd_jwt = make_issuer().issue(claims)
    shown = present(sd_jwt, lambda d: d.name == "nationalities" or d.value == "DE", None)
    out = verify_presentation(shown, policy(key_binding=False), nonce=None, now=NOW)
    assert out["nationalities"] == ["DE"]
    with pytest.raises(SdJwtError) as info:
        present(sd_jwt, lambda d: d.value == "DE", None)
    assert info.value.code == "unreferenced_disclosure"
    issuer_jwt, disclosures, _ = split_presentation(sd_jwt)
    child = next(d for d in disclosures if Disclosure.parse(d).value == "DE")
    code = rejection(f"{issuer_jwt}~{child}~", policy(key_binding=False))
    assert code == "unreferenced_disclosure"


@pytest.mark.unit
def test_decoys_pad_every_object_and_digests_are_sorted() -> None:
    plain = make_issuer(decoys=0).issue(CLAIMS, HOLDER_JWK)
    padded = make_issuer(decoys=8).issue(CLAIMS, HOLDER_JWK)
    plain_body = json.loads(b64url_decode(plain.split(".")[1]))
    padded_body = json.loads(b64url_decode(padded.split(".")[1]))
    assert len(plain_body["_sd"]) == 4
    assert len(padded_body["_sd"]) == 12
    assert padded_body["_sd"] == sorted(padded_body["_sd"])
    plain_json = json.dumps(plain_body)
    padded_json = json.dumps(padded_body)
    assert len(padded_json) - len(plain_json) == 8 * 47
    shown = present(padded, lambda d: True, None)
    claims = verify_presentation(shown, policy(key_binding=False), nonce=None, now=NOW)
    assert claims["nationalities"] == ["US", "DE"]


@pytest.mark.unit
def test_validity_claims_stay_plaintext_and_are_required() -> None:
    with pytest.raises(SdJwtError) as info:
        make_issuer().issue({**CLAIMS, "exp": Sd(NOW + 60)})
    assert info.value.code == "critical_claim_concealed"
    hidden_exp = Disclosure.create("salt-d", "exp", NOW + 60).encoded
    token = sign_raw({"iss": ISSUER, "_sd": [sd_digest(hidden_exp)]})
    assert rejection(f"{token}~", policy(key_binding=False)) == "missing_validity_claim"
    expired = sign_raw({"iss": ISSUER, "exp": NOW - 31})
    assert rejection(f"{expired}~", policy(key_binding=False)) == "expired"


@pytest.mark.unit
@pytest.mark.parametrize("sd_alg", ["SHA-256", "sha-1", "sha-256-32", 256])
def test_sd_alg_is_matched_exactly_against_policy(sd_alg: object) -> None:
    token = sign_raw({"iss": ISSUER, "exp": NOW + 60, "_sd_alg": sd_alg})
    assert rejection(f"{token}~", policy(key_binding=False)) == "unsupported_sd_alg"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("WyJ=", "malformed_base64url"),
        ("WyJ+", "malformed_base64url"),
        ("WyK", "malformed_base64url"),
        (b64url_encode(b'["s", "a", {"x": 1, "x": 2}]'), "duplicate_member"),
        (b64url_encode(b'["s", "a", NaN]'), "malformed_json"),
        (b64url_encode(b'["s", 1, 2]'), "malformed_disclosure"),
        (b64url_encode(b'["s"]'), "malformed_disclosure"),
    ],
)
def test_disclosure_parsing_is_strict(text: str, code: str) -> None:
    with pytest.raises(SdJwtError) as info:
        Disclosure.parse(text)
    assert info.value.code == code


@pytest.mark.unit
def test_compact_serialization_shape_rules() -> None:
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    issuer_jwt = sd_jwt.split("~")[0]
    with pytest.raises(SdJwtError, match="malformed_sd_jwt"):
        split_presentation(issuer_jwt)
    with pytest.raises(SdJwtError, match="malformed_sd_jwt"):
        split_presentation(f"{issuer_jwt}~~")
    shown = present(sd_jwt, lambda d: True, binding())
    with pytest.raises(SdJwtError, match="unexpected_kb_jwt"):
        present(shown, lambda d: True, None)


@pytest.mark.unit
def test_joserfc_size_limits_bound_the_issuer_jwt() -> None:
    assert (JWSRegistry.max_header_length, JWSRegistry.max_payload_length) == (512, 128000)
    big_header = sign_raw({"iss": ISSUER, "exp": NOW + 60}, header={"kid": "k" * 400})
    with pytest.raises(SdJwtError) as info:
        verify_presentation(f"{big_header}~", policy(key_binding=False), nonce=None, now=NOW)
    assert info.value.code == "bad_issuer_jwt"
    assert isinstance(info.value.__cause__, ExceededSizeError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verifier_api_spends_each_nonce_once() -> None:
    tokens = iter(["txn-1", "nonce-1"])
    store = TransactionStore(token=lambda: next(tokens))
    app = build_verifier_app(policy(key_binding=True), store, clock=lambda: NOW)
    sd_jwt = make_issuer().issue(CLAIMS, HOLDER_JWK)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = (await client.post("/transactions")).json()
        assert started == {"transaction": "txn-1", "nonce": "nonce-1", "aud": VERIFIER}
        shown = present(sd_jwt, lambda d: d.name == "email", binding(started["nonce"]))
        sd_headers = {"content-type": "application/sd-jwt"}
        wrong_type = await client.post("/presentations/txn-1", content=shown)
        first = await client.post("/presentations/txn-1", content=shown, headers=sd_headers)
        replay = await client.post("/presentations/txn-1", content=shown, headers=sd_headers)
    assert (wrong_type.status_code, wrong_type.json()) == (415, {"error": "unsupported_media_type"})
    assert first.status_code == 200
    assert first.json()["claims"]["email"] == "johndoe@example.com"
    assert (replay.status_code, replay.json()) == (404, {"error": "unknown_transaction"})
