"""HTTP Message Signatures (RFC 9421) over Content-Digest (RFC 9530), strict and minimal.

The module builds the RFC 9421 Section 2.5 signature base from a request, signs it with
Ed25519 (Section 3.3.6) or HMAC-SHA256 (Section 3.3.3), and verifies it under an explicit
application policy (Section 3.2.1): required coverage, a bounded `created` window, a
single-use `nonce`, an algorithm bound to the key, and a Content-Digest that is checked
against the received bytes (RFC 9421 Section 7.2.8). An httpx `Auth` signs on the client;
a Starlette app verifies on the server. Only the Structured Field subset that these
fields need is implemented (RFC 9651 Sections 3 and 4).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from starlette.requests import Request

# --------------------------------------------------------------------------------------
# Structured Field subset (RFC 9651): Dictionary, Inner List, Item, Parameters.
# --------------------------------------------------------------------------------------


class SFError(ValueError):
    """A Structured Field failed to parse; RFC 9651 Section 4.2 says fail the whole field."""


@dataclass(frozen=True)
class Token:
    """An RFC 9651 Section 3.3.4 Token, kept distinct from a String."""

    value: str


BareItem = int | str | bytes | bool | Token
Params = tuple[tuple[str, BareItem], ...]


@dataclass(frozen=True)
class Item:
    value: BareItem
    params: Params = ()


@dataclass(frozen=True)
class InnerList:
    items: tuple[Item, ...]
    params: Params = ()


_LCALPHA = "abcdefghijklmnopqrstuvwxyz"
_DIGIT = "0123456789"
_KEY_CHARS = frozenset(_LCALPHA + _DIGIT + "_-.*")
_TCHAR = frozenset(_LCALPHA + _LCALPHA.upper() + _DIGIT + "!#$%&'*+-.^_`|~:/")
_B64_CHARS = frozenset(_LCALPHA + _LCALPHA.upper() + _DIGIT + "+/=")


class _Parser:
    def __init__(self, text: str) -> None:
        if not text.isascii():
            raise SFError("field value is not ASCII")
        self.s = text
        self.i = 0

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_sp(self) -> None:
        while self.peek() == " ":
            self.i += 1

    def skip_ows(self) -> None:
        while self.peek() in (" ", "\t") and self.peek():
            self.i += 1

    def key(self) -> str:
        if not self.peek() or self.peek() not in _LCALPHA + "*":
            raise SFError(f"bad key start at {self.i}")
        start = self.i
        while self.peek() and self.peek() in _KEY_CHARS:
            self.i += 1
        return self.s[start : self.i]

    def bare_item(self) -> BareItem:
        c = self.peek()
        if c == "-" or (c and c in _DIGIT):
            return self.integer()
        if c == '"':
            return self.string()
        if c == ":":
            return self.byte_sequence()
        if c == "?":
            return self.boolean()
        if c and (c.isalpha() or c == "*"):
            return self.token()
        raise SFError(f"unrecognized bare item at {self.i}")

    def integer(self) -> int:
        start = self.i
        if self.peek() == "-":
            self.i += 1
        digits_start = self.i
        while self.peek() and self.peek() in _DIGIT:
            self.i += 1
        digits = self.i - digits_start
        if digits == 0 or digits > 15:  # RFC 9651 Section 4.2.4: at most 15 digits
            raise SFError("integer out of range")
        if self.peek() == ".":
            raise SFError("decimals are not used by these fields")
        return int(self.s[start : self.i])

    def string(self) -> str:
        self.i += 1  # opening DQUOTE
        out: list[str] = []
        while self.i < len(self.s):
            c = self.s[self.i]
            self.i += 1
            if c == "\\":
                if self.i >= len(self.s) or self.s[self.i] not in '"\\':
                    raise SFError("bad escape in string")
                out.append(self.s[self.i])
                self.i += 1
            elif c == '"':
                return "".join(out)
            elif not (0x20 <= ord(c) <= 0x7E):
                raise SFError("control character in string")
            else:
                out.append(c)
        raise SFError("unterminated string")  # RFC 9421 Section 7.5.3 injection guard

    def token(self) -> Token:
        start = self.i
        self.i += 1
        while self.peek() and self.peek() in _TCHAR:
            self.i += 1
        return Token(self.s[start : self.i])

    def byte_sequence(self) -> bytes:
        self.i += 1
        end = self.s.find(":", self.i)
        if end < 0:
            raise SFError("unterminated byte sequence")
        chunk = self.s[self.i : end]
        if any(ch not in _B64_CHARS for ch in chunk):
            raise SFError("bad base64 character")
        self.i = end + 1
        try:
            return base64.b64decode(chunk + "=" * (-len(chunk) % 4))
        except ValueError as exc:
            raise SFError("bad base64") from exc

    def boolean(self) -> bool:
        self.i += 1
        c = self.peek()
        if c not in ("0", "1") or not c:
            raise SFError("bad boolean")
        self.i += 1
        return c == "1"

    def params(self) -> Params:
        out: dict[str, BareItem] = {}
        while self.peek() == ";":
            self.i += 1
            self.skip_sp()
            name = self.key()
            value: BareItem = True
            if self.peek() == "=":
                self.i += 1
                value = self.bare_item()
            out[name] = value  # last duplicate wins, first position kept
        return tuple(out.items())

    def inner_list(self) -> InnerList:
        self.i += 1  # "("
        items: list[Item] = []
        while self.i < len(self.s):
            self.skip_sp()
            if self.peek() == ")":
                self.i += 1
                return InnerList(tuple(items), self.params())
            items.append(Item(self.bare_item(), self.params()))
            if self.peek() not in (" ", ")"):
                raise SFError("bad inner list delimiter")
        raise SFError("unterminated inner list")

    def member(self) -> Item | InnerList:
        if self.peek() == "(":
            return self.inner_list()
        return Item(self.bare_item(), self.params())

    def dictionary(self) -> dict[str, Item | InnerList]:
        out: dict[str, Item | InnerList] = {}
        self.skip_sp()
        while self.i < len(self.s):
            name = self.key()
            if self.peek() == "=":
                self.i += 1
                out[name] = self.member()
            else:
                out[name] = Item(True, self.params())
            self.skip_ows()
            if self.i >= len(self.s):
                return out
            if self.peek() != ",":
                raise SFError(f"expected comma at {self.i}")
            self.i += 1
            self.skip_ows()
            if self.i >= len(self.s):
                raise SFError("trailing comma")
        return out


def parse_dictionary(value: str) -> dict[str, Item | InnerList]:
    """Parse an RFC 9651 Dictionary; any syntax error fails the whole field."""
    return _Parser(value).dictionary()


def _ser_bare(value: BareItem) -> str:
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Token):
        return value.value
    if isinstance(value, bytes):
        return ":" + base64.b64encode(value).decode("ascii") + ":"
    if not all(0x20 <= ord(c) <= 0x7E for c in value):
        raise SFError("string contains a non-printable character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ser_params(params: Params) -> str:
    # RFC 9651 Section 4.1.1.2: Boolean true is elided.
    return "".join(f";{k}" if v is True else f";{k}={_ser_bare(v)}" for k, v in params)


def serialize_member(member: Item | InnerList) -> str:
    if isinstance(member, InnerList):
        inner = " ".join(_ser_bare(it.value) + _ser_params(it.params) for it in member.items)
        return f"({inner}){_ser_params(member.params)}"
    if member.value is True:
        return "?1" + _ser_params(member.params)
    return _ser_bare(member.value) + _ser_params(member.params)


# --------------------------------------------------------------------------------------
# RFC 9530 Content-Digest.
# --------------------------------------------------------------------------------------

# "Hash Algorithms for HTTP Digest Fields" registry, Active entries (RFC 9530 Section 7.2).
ACTIVE_DIGESTS: Mapping[str, Callable[[bytes], bytes]] = {
    "sha-256": lambda data: hashlib.sha256(data).digest(),
    "sha-512": lambda data: hashlib.sha512(data).digest(),
}


class DigestError(ValueError):
    pass


def content_digest(content: bytes, algorithms: Sequence[str] = ("sha-256",)) -> str:
    """Serialize a Content-Digest Dictionary over the exact message content bytes."""
    members = (f"{alg}={_ser_bare(ACTIVE_DIGESTS[alg](content))}" for alg in algorithms)
    return ", ".join(members)


def verify_content_digest(field_value: str, content: bytes) -> str:
    """Check every Active digest present; return the strongest algorithm that matched.

    Unknown or Deprecated keys (`md5`, `sha`, `crc32c`...) are ignored, never trusted
    (RFC 9530 Section 5 forbids them in adversarial settings); at least one Active
    digest must be present, and every Active digest present must match, so an
    attacker cannot pair a correct weak digest with a wrong strong one.
    """
    try:
        members = parse_dictionary(field_value)
    except SFError as exc:
        raise DigestError(f"malformed Content-Digest: {exc}") from exc
    matched: list[str] = []
    for alg, member in members.items():
        if alg not in ACTIVE_DIGESTS:
            continue
        if not isinstance(member, Item) or not isinstance(member.value, bytes):
            raise DigestError(f"{alg} value is not a Byte Sequence")
        expected = ACTIVE_DIGESTS[alg](content)
        if not hmac.compare_digest(member.value, expected):
            raise DigestError(f"{alg} digest mismatch")
        matched.append(alg)
    if not matched:
        raise DigestError("no Active digest algorithm present")
    return "sha-512" if "sha-512" in matched else matched[0]


# --------------------------------------------------------------------------------------
# RFC 9421 components and signature base.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRequest:
    """The signature context: what both signer and verifier must agree on (Section 2)."""

    method: str
    scheme: str
    authority: str  # Host or :authority, as received
    raw_path: str  # percent-encoded, never decoded (Section 2.2.6)
    query: str  # without the leading "?"; "" when absent
    headers: tuple[tuple[str, str], ...]
    content: bytes = b""


@dataclass(frozen=True)
class Component:
    """A component identifier: a name plus ordered parameters (Section 2)."""

    name: str
    params: Params = ()

    def serialize(self) -> str:
        return _ser_bare(self.name) + _ser_params(self.params)

    def equivalence_key(self) -> tuple[str, frozenset[tuple[str, BareItem]]]:
        # Parameter order is preserved on the wire but ignored for equality.
        return (self.name, frozenset(self.params))


class SignatureBaseError(ValueError):
    pass


DEFAULT_PORTS = {"http": "80", "https": "443"}
_OBS_FOLD = re.compile(r"\r?\n[ \t]+")  # RFC 9112 Section 5.2 obs-fold


def normalize_authority(authority: str, scheme: str) -> str:
    """RFC 9110 Section 4.2.3: lowercase the host and drop the scheme's default port."""
    host, sep, port = authority.rpartition(":")
    if not sep or "]" in port:  # no port, or the colon belonged to an IPv6 literal
        host, port = authority, ""
    host = host.lower()
    if port and port != DEFAULT_PORTS.get(scheme.lower()):
        return f"{host}:{port}"
    return host


def field_values(request: HttpRequest, name: str) -> list[str]:
    return [v for k, v in request.headers if k.lower() == name]


def _derived_value(request: HttpRequest, name: str) -> str:
    scheme = request.scheme.lower()
    authority = normalize_authority(request.authority, scheme)
    path = request.raw_path or "/"
    query = "?" + request.query
    values = {
        "@method": request.method,
        "@scheme": scheme,
        "@authority": authority,
        "@path": path,
        "@query": query,
        "@target-uri": f"{scheme}://{authority}{path}" + (query if request.query else ""),
        "@request-target": path + (query if request.query else ""),
    }
    if name not in values:
        raise SignatureBaseError(f"unsupported derived component {name}")
    return values[name]


def component_value(request: HttpRequest, component: Component) -> str:
    """Section 2.5 step 2.5: names starting with "@" are never looked up as fields."""
    param_names = {k for k, _ in component.params}
    if component.name.startswith("@"):
        if param_names:
            raise SignatureBaseError(f"unsupported parameter on {component.name}")
        return _derived_value(request, component.name)
    if component.name != component.name.lower():
        raise SignatureBaseError("field component names must be lowercase")
    if param_names - {"bs"}:
        raise SignatureBaseError(f"unsupported parameters {sorted(param_names)}")
    values = field_values(request, component.name)
    if not values:
        raise SignatureBaseError(f"field {component.name} not present")
    # Section 2.1 steps 2 and 3: strip OWS, replace obs-fold with one SP.
    cleaned = [_OBS_FOLD.sub(" ", v).strip() for v in values]
    if "bs" in param_names:  # Section 2.1.3: wrap each line as a Byte Sequence first
        return ", ".join(_ser_bare(v.encode("latin-1")) for v in cleaned)
    return ", ".join(cleaned)  # Section 2.1 step 4: one comma, one space


def signature_params_value(components: Sequence[Component], params: Params) -> str:
    """Section 2.3: an Inner List of String identifiers plus ordered parameters."""
    inner = " ".join(c.serialize() for c in components)
    return f"({inner}){_ser_params(params)}"


def signature_base(request: HttpRequest, components: Sequence[Component], params: Params) -> bytes:
    """Section 2.5: one line per component, then the @signature-params line, no final LF."""
    seen: set[tuple[str, frozenset[tuple[str, BareItem]]]] = set()
    lines: list[str] = []
    for component in components:
        if component.name == "@signature-params":
            raise SignatureBaseError("@signature-params must not be a covered component")
        key = component.equivalence_key()
        if key in seen:
            raise SignatureBaseError(f"duplicate component {component.serialize()}")
        seen.add(key)
        value = component_value(request, component)
        if "\n" in value:
            raise SignatureBaseError("component value contains a newline")
        lines.append(f"{component.serialize()}: {value}")
    lines.append(f'"@signature-params": {signature_params_value(components, params)}')
    base = "\n".join(lines)
    if not base.isascii():
        raise SignatureBaseError("signature base is not ASCII")
    return base.encode("ascii")


# --------------------------------------------------------------------------------------
# Keys and HTTP_SIGN / HTTP_VERIFY (Section 3.3).
# --------------------------------------------------------------------------------------

Algorithm = Literal["ed25519", "hmac-sha256"]


@dataclass(frozen=True)
class SigningKey:
    keyid: str
    alg: Algorithm
    private: Ed25519PrivateKey | None = None
    secret: bytes = b""

    def sign(self, base: bytes) -> bytes:
        if self.alg == "ed25519" and self.private is not None:
            return self.private.sign(base)  # Section 3.3.6: no prehash
        if self.alg == "hmac-sha256" and self.secret:
            return hmac.new(self.secret, base, hashlib.sha256).digest()
        raise ValueError(f"key {self.keyid} cannot sign with {self.alg}")


@dataclass(frozen=True)
class VerificationKey:
    """Raw key bytes plus the one algorithm this key may ever be used with."""

    keyid: str
    alg: Algorithm
    material: bytes  # Ed25519: 32-byte public key; HMAC: the shared secret


def http_verify(alg: str, material: bytes, base: bytes, signature: bytes) -> bool:
    if alg == "ed25519":
        try:
            Ed25519PublicKey.from_public_bytes(material).verify(signature, base)
        except (InvalidSignature, ValueError):
            return False
        return True
    if alg == "hmac-sha256":  # Section 3.3.3, compared in constant time
        return hmac.compare_digest(hmac.new(material, base, hashlib.sha256).digest(), signature)
    return False


def resolve_algorithm(key: VerificationKey, alg_param: str | None) -> str:
    """Section 3.2 step 6: every source of the algorithm must agree, or verification fails.

    The key's own algorithm is authoritative. Letting the `alg` parameter choose instead
    is the RFC 9421 Section 7.3.6 downgrade: HMAC keyed with the public key bytes.
    """
    if alg_param is not None and alg_param != key.alg:
        raise SignatureError("alg_mismatch")
    return key.alg


# --------------------------------------------------------------------------------------
# Signing (Section 3.1) and verification under an application policy (Sections 3.2, 3.2.1).
# --------------------------------------------------------------------------------------


class SignatureError(ValueError):
    """Verification failed; `reason` is a short machine code, never the signature base."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sign_request(
    request: HttpRequest,
    key: SigningKey,
    components: Sequence[Component],
    *,
    label: str,
    created: int,
    expires: int | None = None,
    nonce: str | None = None,
    tag: str | None = None,
    include_alg: bool = False,
) -> tuple[str, str]:
    """Return (Signature-Input, Signature) member strings for one labelled signature."""
    params: list[tuple[str, BareItem]] = [("created", created)]
    if expires is not None:
        params.append(("expires", expires))
    params.append(("keyid", key.keyid))
    if include_alg:
        params.append(("alg", key.alg))
    if nonce is not None:
        params.append(("nonce", nonce))
    if tag is not None:
        params.append(("tag", tag))
    frozen = tuple(params)
    base = signature_base(request, components, frozen)
    signature = key.sign(base)
    return (
        f"{label}={signature_params_value(components, frozen)}",
        f"{label}={_ser_bare(signature)}",
    )


@dataclass
class Policy:
    """The Section 1.4 application profile that RFC 9421 says every deployment must define."""

    required: tuple[Component, ...]
    max_age: int = 300
    max_skew: int = 5
    tag: str | None = None
    require_nonce: bool = True
    seen_nonces: set[str] = field(default_factory=lambda: set[str]())


@dataclass(frozen=True)
class Verified:
    label: str
    keyid: str
    covered: tuple[str, ...]
    digest_alg: str | None


def _combined(request: HttpRequest, name: str) -> str:
    return ", ".join(v.strip() for v in field_values(request, name))


def _str_param(params: Mapping[str, BareItem], name: str) -> str | None:
    value = params.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SignatureError(f"bad_{name}_type")
    return value


def _int_param(params: Mapping[str, BareItem], name: str) -> int | None:
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SignatureError(f"bad_{name}_type")
    return value


def _verify_one(
    request: HttpRequest,
    label: str,
    sig_input: Item | InnerList,
    signature: Item | InnerList,
    keys: Mapping[str, VerificationKey],
    policy: Policy,
    now: int,
) -> Verified:
    if not isinstance(sig_input, InnerList) or not isinstance(signature, Item):
        raise SignatureError("bad_member_type")
    if not isinstance(signature.value, bytes):
        raise SignatureError("bad_signature_type")
    components: list[Component] = []
    for item in sig_input.items:
        if not isinstance(item.value, str):
            raise SignatureError("component_not_string")
        components.append(Component(item.value, item.params))
    params = dict(sig_input.params)
    covered = {c.equivalence_key() for c in components}
    if any(r.equivalence_key() not in covered for r in policy.required):
        raise SignatureError("insufficient_coverage")
    keyid = _str_param(params, "keyid")
    if keyid is None or keyid not in keys:
        raise SignatureError("unknown_key")
    key = keys[keyid]
    alg = resolve_algorithm(key, _str_param(params, "alg"))
    created = _int_param(params, "created")
    if created is None or created > now + policy.max_skew or now - created > policy.max_age:
        raise SignatureError("stale_or_future")
    expires = _int_param(params, "expires")
    if expires is not None and now >= expires:
        raise SignatureError("expired")
    nonce = _str_param(params, "nonce")
    if policy.require_nonce and nonce is None:
        raise SignatureError("missing_nonce")
    if nonce is not None and nonce in policy.seen_nonces:
        raise SignatureError("replayed_nonce")
    try:
        base = signature_base(request, components, sig_input.params)
    except SignatureBaseError as exc:
        raise SignatureError("unbuildable_base") from exc
    if not http_verify(alg, key.material, base, signature.value):
        raise SignatureError("bad_signature")
    digest_alg: str | None = None
    if request.content or ("content-digest", frozenset()) in covered:
        if ("content-digest", frozenset()) not in covered:
            raise SignatureError("content_not_covered")
        try:  # Section 7.2.8: the signature covers the field, not the bytes
            digest_alg = verify_content_digest(
                _combined(request, "content-digest"), request.content
            )
        except DigestError as exc:
            raise SignatureError("digest_mismatch") from exc
    if nonce is not None:
        policy.seen_nonces.add(nonce)  # only after every other check passed
    return Verified(label, keyid, tuple(c.serialize() for c in components), digest_alg)


def verify_request(
    request: HttpRequest,
    keys: Mapping[str, VerificationKey],
    policy: Policy,
    now: int,
) -> Verified:
    """Section 3.2: accept the first applicable signature from a known key, else fail.

    Signatures with a different `tag` are skipped, not failed, so an injected bogus
    signature cannot deny service to a valid one (Section 7.2.6).
    """
    raw_input = _combined(request, "signature-input")
    raw_sig = _combined(request, "signature")
    if not raw_input or not raw_sig:
        raise SignatureError("missing_signature")
    try:
        inputs = parse_dictionary(raw_input)
        signatures = parse_dictionary(raw_sig)
    except SFError as exc:
        raise SignatureError("malformed_signature_fields") from exc
    if set(inputs) != set(signatures):
        raise SignatureError("label_mismatch")  # Section 4: same labels in both fields
    reasons: list[str] = []
    for label, sig_input in inputs.items():
        if policy.tag is not None:
            tag = sig_input.params and dict(sig_input.params).get("tag")
            if tag != policy.tag:
                reasons.append("tag_skipped")
                continue
        try:
            return _verify_one(request, label, sig_input, signatures[label], keys, policy, now)
        except SignatureError as exc:
            reasons.append(exc.reason)
    raise SignatureError(reasons[-1] if reasons else "no_signature")


def accept_signature(policy: Policy, label: str = "sig1") -> str:
    """Section 5.1: tell the client exactly what to sign next time."""
    params: list[tuple[str, BareItem]] = [("created", True)]
    if policy.require_nonce:
        params.append(("nonce", secrets.token_urlsafe(16)))
    if policy.tag is not None:
        params.append(("tag", policy.tag))
    return f"{label}={signature_params_value(policy.required, tuple(params))}"


# --------------------------------------------------------------------------------------
# Wire integration: httpx client Auth and a Starlette verifier.
# --------------------------------------------------------------------------------------


def request_from_httpx(request: httpx.Request) -> HttpRequest:
    raw_path, _, _ = request.url.raw_path.partition(b"?")
    return HttpRequest(
        method=request.method,
        scheme=request.url.scheme,
        authority=request.headers["host"],
        raw_path=raw_path.decode("ascii"),
        query=request.url.query.decode("ascii"),
        headers=tuple(request.headers.multi_items()),
        content=request.content,
    )


class HttpSignatureAuth(httpx.Auth):
    """Adds Content-Digest, Signature-Input, and Signature to every outgoing request."""

    requires_request_body = True  # httpx reads the body before auth_flow runs

    def __init__(
        self,
        key: SigningKey,
        components: Sequence[Component],
        clock: Callable[[], int],
        *,
        label: str = "sig1",
        tag: str | None = None,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        self.key = key
        self.components = tuple(components)
        self.clock = clock
        self.label = label
        self.tag = tag
        self.nonce_factory = nonce_factory

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Content-Digest"] = content_digest(request.content)
        sig_input, signature = sign_request(
            request_from_httpx(request),
            self.key,
            self.components,
            label=self.label,
            created=self.clock(),
            nonce=self.nonce_factory(),
            tag=self.tag,
        )
        request.headers["Signature-Input"] = sig_input
        request.headers["Signature"] = signature
        yield request


async def request_from_starlette(request: Request) -> HttpRequest:
    scope = request.scope
    headers = tuple((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"])
    host = next((v for k, v in headers if k == "host"), "")
    return HttpRequest(
        method=scope["method"],
        scheme=scope["scheme"],
        authority=host,
        raw_path=scope.get("raw_path", scope["path"].encode()).decode("ascii"),
        query=scope["query_string"].decode("ascii"),
        headers=headers,
        content=await request.body(),
    )


def make_app(
    keys: Mapping[str, VerificationKey], policy: Policy, clock: Callable[[], int]
) -> Starlette:
    async def payments(request: Request) -> JSONResponse:
        message = await request_from_starlette(request)
        try:
            verified = verify_request(message, keys, policy, clock())
        except SignatureError as exc:
            return JSONResponse(
                {"title": "signature verification failed", "reason": exc.reason},
                status_code=401,
                headers={"Accept-Signature": accept_signature(policy)},
            )
        return JSONResponse(
            {"keyid": verified.keyid, "label": verified.label, "digest": verified.digest_alg}
        )

    return Starlette(routes=[Route("/payments", payments, methods=["POST"])])


def components(*names: str) -> tuple[Component, ...]:
    return tuple(Component(n) for n in names)
