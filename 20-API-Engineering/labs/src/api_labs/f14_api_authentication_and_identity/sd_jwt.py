"""RFC 9901 Selective Disclosure for JWTs (SD-JWT): issuer, holder, and verifier.

The Issuer-signed JWT and the Key Binding JWT are ES256 JWS objects produced and checked by
joserfc 1.8.0; everything SD-JWT specific (Disclosures, digests, the tilde format, the
Section 7.1 processing algorithm, and the Section 7.3 Key Binding checks) is implemented here.
No network access; salts, nonces, and clocks are injectable so every test is deterministic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from joserfc import jws
from joserfc.errors import JoseError
from joserfc.jwk import ECKey
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

SD_JWT_MEDIA_TYPE: Final = "application/sd-jwt"
KB_JWT_TYP: Final = "kb+jwt"
JWS_ALGORITHMS: Final = ("ES256",)
SD_ALG_DEFAULT: Final = "sha-256"
MAX_PRESENTATION_BYTES: Final = 64 * 1024
_HASHES: Final[Mapping[str, Callable[[bytes], bytes]]] = {
    "sha-256": lambda data: hashlib.sha256(data).digest(),
    "sha-384": lambda data: hashlib.sha384(data).digest(),
    "sha-512": lambda data: hashlib.sha512(data).digest(),
}
_RESERVED_NAMES: Final = frozenset({"_sd", "..."})
_CRITICAL_CLAIMS: Final = frozenset({"iss", "exp", "nbf", "cnf"})
_B64URL_CHARS: Final = re.compile(r"[A-Za-z0-9_-]*")


class SdJwtError(Exception):
    """A rejected SD-JWT; ``code`` is a stable, machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def b64url_encode(data: bytes) -> str:
    """Base64url without padding (RFC 7515 Section 2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict base64url: URL-safe alphabet only, no padding, canonical trailing bits."""
    if not _B64URL_CHARS.fullmatch(text) or len(text) % 4 == 1:
        raise SdJwtError("malformed_base64url", text[:16])
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url_encode(data) != text:
        raise SdJwtError("malformed_base64url", "non-canonical trailing bits")
    return data


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise SdJwtError("duplicate_member", name)
        result[name] = value
    return result


def _reject_constant(name: str) -> object:
    raise SdJwtError("malformed_json", f"non-finite number {name}")


def strict_json(raw: bytes) -> object:
    """Strict UTF-8 (never U+FFFD substitution), no duplicate names, no NaN or Infinity."""
    try:
        text = raw.decode("utf-8")
        parsed: object = json.loads(
            text, object_pairs_hook=_unique_members, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise SdJwtError("malformed_json", type(exc).__name__) from exc
    return parsed


def sd_digest(encoded: str, sd_alg: str = SD_ALG_DEFAULT) -> str:
    """Section 4.2.3: hash the US-ASCII bytes of the base64url text, then base64url the digest."""
    return b64url_encode(_HASHES[sd_alg](encoded.encode("ascii")))


@dataclass(frozen=True, slots=True)
class Disclosure:
    """One Disclosure: ``[salt, name, value]`` for a property, ``[salt, value]`` for an element."""

    encoded: str
    salt: str
    name: str | None
    value: object

    @classmethod
    def create(cls, salt: str, name: str | None, value: object) -> Disclosure:
        items: list[object] = [salt, value] if name is None else [salt, name, value]
        raw = json.dumps(items, ensure_ascii=False).encode("utf-8")
        return cls(b64url_encode(raw), salt, name, value)

    @classmethod
    def parse(cls, encoded: str) -> Disclosure:
        if not encoded:
            raise SdJwtError("malformed_disclosure", "empty")
        parsed = strict_json(b64url_decode(encoded))
        if not isinstance(parsed, list):
            raise SdJwtError("malformed_disclosure", "not a JSON array")
        items = cast("list[object]", parsed)
        name: str | None = None
        if len(items) == 3:
            salt, raw_name, value = items
            if not isinstance(raw_name, str):
                raise SdJwtError("malformed_disclosure", "claim name is not a string")
            name = raw_name
        elif len(items) == 2:
            salt, value = items
        else:
            raise SdJwtError("malformed_disclosure", f"{len(items)} elements")
        if not isinstance(salt, str):
            raise SdJwtError("malformed_disclosure", "salt is not a string")
        return cls(encoded, salt, name, value)


@dataclass(frozen=True, slots=True)
class Sd:
    """Marks an object property value or an array element as selectively disclosable."""

    value: object


def random_salt() -> str:
    """128 bits from the OS CSPRNG, 22 base64url characters (Section 9.3)."""
    return b64url_encode(secrets.token_bytes(16))


@dataclass
class Issuer:
    """Builds ``<Issuer-signed JWT>~<D.1>~...~<D.N>~`` from claims marked with ``Sd``."""

    key: ECKey
    typ: str
    sd_alg: str = SD_ALG_DEFAULT
    decoys_per_object: int = 0
    salt: Callable[[], str] = random_salt

    def issue(
        self, claims: Mapping[str, object], holder_jwk: Mapping[str, object] | None = None
    ) -> str:
        for name in sorted(_CRITICAL_CLAIMS):
            if isinstance(claims.get(name), Sd):
                raise SdJwtError("critical_claim_concealed", name)
        disclosures: list[Disclosure] = []
        payload = self._conceal_object(claims, disclosures)
        payload["_sd_alg"] = self.sd_alg
        if holder_jwk is not None:
            payload["cnf"] = {"jwk": dict(holder_jwk)}
        token = jws.serialize_compact(
            {"alg": "ES256", "typ": self.typ},
            json.dumps(payload).encode("utf-8"),
            self.key,
            algorithms=JWS_ALGORITHMS,
        )
        return "~".join([token, *(d.encoded for d in disclosures)]) + "~"

    def _conceal(self, node: object, out: list[Disclosure]) -> object:
        if isinstance(node, Sd):
            raise SdJwtError("misplaced_sd", "Sd must wrap a property value or array element")
        if isinstance(node, Mapping):
            return self._conceal_object(cast("Mapping[str, object]", node), out)
        if isinstance(node, list):
            result: list[object] = []
            for element in cast("list[object]", node):
                if isinstance(element, Sd):
                    disclosure = Disclosure.create(
                        self.salt(), None, self._conceal(element.value, out)
                    )
                    out.append(disclosure)
                    result.append({"...": sd_digest(disclosure.encoded, self.sd_alg)})
                else:
                    result.append(self._conceal(element, out))
            return result
        return node

    def _conceal_object(
        self, node: Mapping[str, object], out: list[Disclosure]
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        digests: list[str] = []
        for name, value in node.items():
            if name in _RESERVED_NAMES or name == "_sd_alg":
                raise SdJwtError("reserved_claim_name", name)
            if isinstance(value, Sd):
                disclosure = Disclosure.create(self.salt(), name, self._conceal(value.value, out))
                out.append(disclosure)
                digests.append(sd_digest(disclosure.encoded, self.sd_alg))
            else:
                result[name] = self._conceal(value, out)
        digests.extend(sd_digest(self.salt(), self.sd_alg) for _ in range(self.decoys_per_object))
        if digests:
            result["_sd"] = sorted(digests)  # Section 4.2.4.1: never reveal the claim order
        return result


def split_presentation(text: str) -> tuple[str, list[str], str | None]:
    """Section 4: the final component is empty for an SD-JWT and a KB-JWT for an SD-JWT+KB."""
    parts = text.split("~")
    if len(parts) < 2:
        raise SdJwtError("malformed_sd_jwt", "no '~' separator")
    issuer_jwt, *disclosures, last = parts
    if any(not item for item in disclosures):
        raise SdJwtError("malformed_sd_jwt", "empty Disclosure component")
    return issuer_jwt, disclosures, last or None


def _unverified_payload(issuer_jwt: str) -> dict[str, object]:
    segments = issuer_jwt.split(".")
    if len(segments) != 3:
        raise SdJwtError("bad_issuer_jwt", "not a JWS Compact Serialization")
    payload = strict_json(b64url_decode(segments[1]))
    if not isinstance(payload, dict):
        raise SdJwtError("bad_issuer_jwt", "payload is not a JSON object")
    return cast("dict[str, object]", payload)


def _array_digest(element: object) -> str | None:
    if isinstance(element, dict):
        obj = cast("dict[str, object]", element)
        digest = obj.get("...")
        if len(obj) == 1 and isinstance(digest, str):
            return digest
    return None


def embedded_digests(node: object) -> Iterator[str]:
    """Every digest in ``_sd`` arrays and ``{"...": digest}`` elements, at any depth."""
    if isinstance(node, dict):
        obj = cast("dict[str, object]", node)
        listed = obj.get("_sd")
        if isinstance(listed, list):
            yield from (d for d in cast("list[object]", listed) if isinstance(d, str))
        for name, value in obj.items():
            if name != "_sd":
                yield from embedded_digests(value)
    elif isinstance(node, list):
        for element in cast("list[object]", node):
            digest = _array_digest(element)
            if digest is not None:
                yield digest
            else:
                yield from embedded_digests(element)


@dataclass(frozen=True, slots=True)
class KeyBinding:
    """Inputs to the Key Binding JWT (Section 4.3)."""

    key: ECKey
    audience: str
    nonce: str
    iat: int


def present(sd_jwt: str, keep: Callable[[Disclosure], bool], binding: KeyBinding | None) -> str:
    """Section 7.2: select Disclosures, check they connect, then append an optional KB-JWT."""
    issuer_jwt, encoded, kb_jwt = split_presentation(sd_jwt)
    if kb_jwt is not None:
        raise SdJwtError("unexpected_kb_jwt", "a Holder receives an SD-JWT, never an SD-JWT+KB")
    payload = _unverified_payload(issuer_jwt)
    sd_alg = _sd_alg(payload, frozenset(_HASHES))
    chosen = [item for item in encoded if keep(Disclosure.parse(item))]
    reachable = set(embedded_digests(payload))
    by_digest = {sd_digest(item, sd_alg): item for item in chosen}
    expanded: set[str] = set()
    while pending := (reachable & by_digest.keys()) - expanded:
        for digest in pending:
            expanded.add(digest)
            reachable.update(embedded_digests(Disclosure.parse(by_digest[digest]).value))
    if orphans := by_digest.keys() - reachable:
        raise SdJwtError("unreferenced_disclosure", f"{len(orphans)} selected without parent")
    sd_part = "~".join([issuer_jwt, *chosen]) + "~"
    if binding is None:
        return sd_part
    kb_payload = {
        "nonce": binding.nonce,
        "aud": binding.audience,
        "iat": binding.iat,
        "sd_hash": sd_digest(sd_part, sd_alg),
    }
    return sd_part + jws.serialize_compact(
        {"alg": "ES256", "typ": KB_JWT_TYP},
        json.dumps(kb_payload).encode("utf-8"),
        binding.key,
        algorithms=JWS_ALGORITHMS,
    )


@dataclass(frozen=True, slots=True)
class VerifierPolicy:
    """Decided before a presentation arrives; never inferred from it (Section 9.5)."""

    issuer: str
    issuer_key: ECKey
    expected_typ: str
    audience: str
    require_key_binding: bool
    allowed_sd_algs: frozenset[str] = frozenset({SD_ALG_DEFAULT})
    required_claims: frozenset[str] = frozenset({"iss", "exp"})
    max_kb_age: int = 300
    clock_skew: int = 30


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sd_alg(payload: Mapping[str, object], allowed: frozenset[str]) -> str:
    sd_alg = payload.get("_sd_alg", SD_ALG_DEFAULT)
    if not isinstance(sd_alg, str) or sd_alg not in allowed or sd_alg not in _HASHES:
        raise SdJwtError("unsupported_sd_alg", repr(sd_alg))  # case-sensitive (Section 4.1.1)
    return sd_alg


def _verify_jws(token: str, key: ECKey, code: str) -> tuple[dict[str, object], dict[str, object]]:
    try:
        obj = jws.deserialize_compact(token, key, algorithms=JWS_ALGORITHMS)
    except (JoseError, ValueError) as exc:
        raise SdJwtError(code, type(exc).__name__) from exc
    payload = strict_json(obj.payload)
    if not isinstance(payload, dict):
        raise SdJwtError(code, "payload is not a JSON object")
    return dict(obj.headers()), cast("dict[str, object]", payload)


@dataclass
class _Walk:
    """Section 7.1 step 3: replace digests with Disclosure content, recursively."""

    by_digest: dict[str, Disclosure]
    seen: set[str] = field(default_factory=set[str])
    used: set[str] = field(default_factory=set[str])

    def claim(self, digest: str) -> Disclosure | None:
        if digest in self.seen:
            raise SdJwtError("duplicate_digest", digest)  # Section 7.1 step 4
        self.seen.add(digest)
        disclosure = self.by_digest.get(digest)
        if disclosure is not None:
            self.used.add(digest)
        return disclosure  # None: a decoy or a withheld claim, ignored (step 3.c.i)

    def walk_value(self, node: object) -> object:
        if isinstance(node, dict):
            return self.walk_object(cast("dict[str, object]", node))
        if isinstance(node, list):
            return self.walk_array(cast("list[object]", node))
        return node

    def walk_object(self, node: dict[str, object]) -> dict[str, object]:
        result = {name: self.walk_value(value) for name, value in node.items() if name != "_sd"}
        listed = node.get("_sd")
        if listed is None:
            return result
        if not isinstance(listed, list) or not all(
            isinstance(d, str) for d in cast("list[object]", listed)
        ):
            raise SdJwtError("malformed_sd", "_sd must be an array of strings")
        for digest in cast("list[str]", listed):
            disclosure = self.claim(digest)
            if disclosure is None:
                continue
            if disclosure.name is None:
                raise SdJwtError("disclosure_shape", "array-element Disclosure in _sd")
            if disclosure.name in _RESERVED_NAMES:
                raise SdJwtError("reserved_claim_name", disclosure.name)
            if disclosure.name in result:
                raise SdJwtError("claim_name_collision", disclosure.name)
            result[disclosure.name] = self.walk_value(disclosure.value)
        return result

    def walk_array(self, node: list[object]) -> list[object]:
        result: list[object] = []
        for element in node:
            digest = _array_digest(element)
            if digest is None:
                result.append(self.walk_value(element))
                continue
            disclosure = self.claim(digest)
            if disclosure is None:
                continue  # Section 7.1 step 3.d: drop undisclosed elements
            if disclosure.name is not None:
                raise SdJwtError("disclosure_shape", "property Disclosure in an array")
            result.append(self.walk_value(disclosure.value))
        return result


def process_sd_jwt(
    payload: dict[str, object], encoded: list[str], sd_alg: str
) -> dict[str, object]:
    """Section 7.1 steps 3 to 5 over an already signature-checked payload."""
    by_digest: dict[str, Disclosure] = {}
    for item in encoded:
        digest = sd_digest(item, sd_alg)
        if digest in by_digest:
            raise SdJwtError("duplicate_disclosure", digest)
        by_digest[digest] = Disclosure.parse(item)
    walk = _Walk(by_digest)
    try:
        processed = walk.walk_object(payload)
    except RecursionError as exc:
        raise SdJwtError("nesting_too_deep") from exc
    processed.pop("_sd_alg", None)
    if unused := by_digest.keys() - walk.used:
        raise SdJwtError("unreferenced_disclosure", f"{len(unused)} not referenced")  # step 5
    return processed


def _check_validity(claims: Mapping[str, object], policy: VerifierPolicy, now: int) -> None:
    if missing := sorted(policy.required_claims - claims.keys()):
        raise SdJwtError("missing_validity_claim", ",".join(missing))  # Section 7.1 step 6
    if claims.get("iss") != policy.issuer:
        raise SdJwtError("wrong_issuer", repr(claims.get("iss")))
    exp, nbf = claims.get("exp"), claims.get("nbf")
    if exp is not None and (not _is_int(exp) or now >= cast("int", exp) + policy.clock_skew):
        raise SdJwtError("expired")
    if nbf is not None and (not _is_int(nbf) or now + policy.clock_skew < cast("int", nbf)):
        raise SdJwtError("not_yet_valid")


def _holder_key(claims: Mapping[str, object]) -> ECKey:
    cnf = claims.get("cnf")
    jwk = cast("dict[str, object]", cnf).get("jwk") if isinstance(cnf, dict) else None
    if not isinstance(jwk, dict):
        raise SdJwtError("missing_cnf", "no cnf.jwk to verify the KB-JWT")
    if "d" in jwk:
        raise SdJwtError("bad_cnf", "cnf.jwk carries a private key")
    try:
        return ECKey.import_key(cast("dict[str, str | list[str]]", jwk))
    except (JoseError, ValueError) as exc:
        raise SdJwtError("bad_cnf", type(exc).__name__) from exc


def verify_presentation(
    presentation: str, policy: VerifierPolicy, *, nonce: str | None, now: int
) -> dict[str, object]:
    """Sections 7.1 and 7.3; returns the Processed SD-JWT Payload or raises SdJwtError."""
    issuer_jwt, encoded, kb_jwt = split_presentation(presentation)
    if policy.require_key_binding and kb_jwt is None:
        raise SdJwtError("key_binding_required")  # Section 7.3 step 2
    if not policy.require_key_binding and kb_jwt is not None:
        raise SdJwtError("unexpected_kb_jwt")  # Section 4: final component must be empty
    header, payload = _verify_jws(issuer_jwt, policy.issuer_key, "bad_issuer_jwt")
    if header.get("typ") != policy.expected_typ:
        raise SdJwtError("wrong_typ", repr(header.get("typ")))  # Section 9.11
    sd_alg = _sd_alg(payload, policy.allowed_sd_algs)
    claims = process_sd_jwt(payload, encoded, sd_alg)
    _check_validity(claims, policy, now)
    if kb_jwt is None:
        return claims
    kb_header, kb = _verify_jws(kb_jwt, _holder_key(claims), "bad_kb_jwt")
    if kb_header.get("typ") != KB_JWT_TYP:
        raise SdJwtError("wrong_kb_typ", repr(kb_header.get("typ")))
    iat = kb.get("iat")
    if not _is_int(iat) or not (
        now - policy.max_kb_age <= cast("int", iat) <= now + policy.clock_skew
    ):
        raise SdJwtError("kb_iat_out_of_window", repr(iat))
    if kb.get("aud") != policy.audience:
        raise SdJwtError("kb_audience_mismatch", repr(kb.get("aud")))
    if nonce is None or kb.get("nonce") != nonce:
        raise SdJwtError("kb_nonce_mismatch")
    sd_part = presentation[: len(presentation) - len(kb_jwt)]
    if kb.get("sd_hash") != sd_digest(sd_part, sd_alg):
        raise SdJwtError("sd_hash_mismatch")  # Section 7.3 step 5.g
    return claims


@dataclass
class TransactionStore:
    """One-time nonces per presentation transaction (Section 7.3 step 5.f replay detection)."""

    token: Callable[[], str] = lambda: secrets.token_urlsafe(16)
    nonces: dict[str, str] = field(default_factory=dict[str, str])

    def begin(self) -> tuple[str, str]:
        transaction, nonce = self.token(), self.token()
        self.nonces[transaction] = nonce
        return transaction, nonce

    def consume(self, transaction: str) -> str | None:
        return self.nonces.pop(transaction, None)


def build_verifier_app(
    policy: VerifierPolicy, store: TransactionStore, clock: Callable[[], int]
) -> Starlette:
    """``POST /transactions`` issues a nonce; ``POST /presentations/{txn}`` spends it once."""

    async def begin(request: Request) -> JSONResponse:
        transaction, nonce = store.begin()
        body = {"transaction": transaction, "nonce": nonce, "aud": policy.audience}
        return JSONResponse(body, status_code=201)

    async def receive(request: Request) -> JSONResponse:
        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type != SD_JWT_MEDIA_TYPE:
            return JSONResponse({"error": "unsupported_media_type"}, status_code=415)
        body = await request.body()
        if len(body) > MAX_PRESENTATION_BYTES:
            return JSONResponse({"error": "presentation_too_large"}, status_code=413)
        nonce = store.consume(request.path_params["txn"])  # burned even if verification fails
        if nonce is None:
            return JSONResponse({"error": "unknown_transaction"}, status_code=404)
        try:
            claims = verify_presentation(body.decode("ascii"), policy, nonce=nonce, now=clock())
        except UnicodeDecodeError:
            return JSONResponse({"error": "malformed_sd_jwt"}, status_code=400)
        except SdJwtError as exc:
            return JSONResponse({"error": exc.code}, status_code=400)
        return JSONResponse({"claims": claims})

    return Starlette(
        routes=[
            Route("/transactions", begin, methods=["POST"]),
            Route("/presentations/{txn}", receive, methods=["POST"]),
        ]
    )
