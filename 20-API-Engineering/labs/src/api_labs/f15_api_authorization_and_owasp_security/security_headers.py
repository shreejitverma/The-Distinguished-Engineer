"""Security response headers for JSON APIs and their HTML docs.

Server side: one outermost ASGI middleware that applies the header policy to every response.
Client side: small models of what a user agent does with those headers, transcribed from
RFC 6797 (HSTS), the WHATWG Fetch Standard (nosniff), CSP Level 3 (policy parsing and
inline-script hashes), and RFC 9116 (security.txt), so tests can assert the effect, not the bytes.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request  # Request at runtime: FastAPI resolves the annotation
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

RawHeaders = list[tuple[bytes, bytes]]

# ---------------------------------------------------------------------------
# HSTS, user-agent side (RFC 6797 Sections 6.1, 8.1, 8.1.1)
# ---------------------------------------------------------------------------

ONE_YEAR = 86400 * 365  # Chromium kMaxHSTSAgeSecs and the hstspreload.org minimum
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_MAX_DIGITS = 18  # clamp by length before int(); CPython refuses > 4300 digits


@dataclass(frozen=True, slots=True)
class StsPolicy:
    max_age: int
    include_subdomains: bool
    preload: bool  # not in RFC 6797; an unknown directive the UA ignores, read by hstspreload


def _skip_ows(value: str, i: int) -> int:
    while i < len(value) and value[i] in " \t":
        i += 1
    return i


def _sts_directives(value: str) -> list[tuple[str, str | None]] | None:
    """`[ directive ] *( ";" [ directive ] )`, quote-aware; None on any syntax error."""
    out: list[tuple[str, str | None]] = []
    i = 0
    while True:
        i = _skip_ows(value, i)
        if i < len(value) and value[i] != ";":
            name = _TOKEN.match(value, i)
            if name is None:
                return None
            i = _skip_ows(value, name.end())
            arg: str | None = None
            if i < len(value) and value[i] == "=":
                i = _skip_ows(value, i + 1)
                quoted = _QUOTED.match(value, i)
                token = _TOKEN.match(value, i)
                if quoted is not None:
                    arg, i = re.sub(r"\\(.)", r"\1", quoted.group(1)), quoted.end()
                elif token is not None:
                    arg, i = token.group(0), token.end()
                else:
                    return None
                i = _skip_ows(value, i)
            out.append((name.group(0).lower(), arg))
        if i >= len(value):
            return out
        if value[i] != ";":
            return None
        i += 1


def parse_sts(value: str, *, max_age_cap: int | None = None) -> StsPolicy | None:
    """Parse one STS field value as a conforming UA must (RFC 6797 Section 6.1)."""
    directives = _sts_directives(value)
    if directives is None:
        return None
    names = [name for name, _ in directives]
    if len(names) != len(set(names)):
        return None  # item 2: every directive MUST appear only once
    args = dict(directives)
    raw = args.get("max-age")
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None  # max-age is REQUIRED and is 1*DIGIT after unquoting (Section 6.1.1)
    if args.get("includesubdomains") is not None:
        return None  # a valueless directive with a value does not conform (Section 6.1.2)
    digits = raw.lstrip("0") or "0"
    seconds = 10**_MAX_DIGITS if len(digits) > _MAX_DIGITS else int(digits)
    if max_age_cap is not None:
        seconds = min(seconds, max_age_cap)
    return StsPolicy(seconds, "includesubdomains" in args, "preload" in args)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    return True


def received_sts(
    field_values: Sequence[str], *, secure: bool, host: str, max_age_cap: int | None = None
) -> StsPolicy | None:
    """What a UA notes from a response (RFC 6797 Sections 8.1 and 8.1.1)."""
    if not secure or not field_values:
        return None  # STS over insecure transport MUST be ignored
    if _is_ip_literal(host):
        return None  # an IP-literal host MUST NOT be noted as a Known HSTS Host
    return parse_sts(field_values[0], max_age_cap=max_age_cap)  # only the first field


def preload_eligible(policy: StsPolicy | None) -> bool:
    """The header checks of chromium/hstspreload header.go (as of 2026-11)."""
    return (
        policy is not None
        and policy.max_age >= ONE_YEAR
        and policy.include_subdomains
        and policy.preload
    )


# ---------------------------------------------------------------------------
# nosniff, user-agent side (Fetch Standard Sections 2.10, 3.6, 3.6.1)
# ---------------------------------------------------------------------------

SCRIPT_LIKE = frozenset(
    {"audioworklet", "paintworklet", "script", "serviceworker", "sharedworker", "worker"}
)
JAVASCRIPT_MIME_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/x-ecmascript",
        "application/x-javascript",
        "text/ecmascript",
        "text/javascript",
        *(f"text/javascript1.{n}" for n in range(6)),
        "text/jscript",
        "text/livescript",
        "text/x-ecmascript",
        "text/x-javascript",
    }
)


def get_decode_split(values: Sequence[str]) -> list[str] | None:
    """Fetch "get, decode, and split": combine lines with ", ", split on unquoted commas."""
    if not values:
        return None
    joined = ", ".join(values)
    out: list[str] = []
    current: list[str] = []
    in_quote = False
    i = 0
    while i < len(joined):
        char = joined[i]
        current.append(char)
        if in_quote and char == "\\" and i + 1 < len(joined):
            current.append(joined[i + 1])
            i += 1
        elif char == '"':
            in_quote = not in_quote
        elif char == "," and not in_quote:
            current.pop()
            out.append("".join(current).strip(" \t"))
            current = []
        i += 1
    out.append("".join(current).strip(" \t"))
    return out


def determine_nosniff(values: Sequence[str]) -> bool:
    """Only the first list member counts, ASCII case-insensitively (Fetch Section 3.6)."""
    members = get_decode_split(values)
    return members is not None and members[0].lower() == "nosniff"


def mime_essence(content_type: str | None) -> str | None:
    """Simplified "extract a MIME type": the essence of one value, or None for failure."""
    if content_type is None:
        return None
    essence = content_type.split(";", 1)[0].strip(" \t").lower()
    kind, slash, subtype = essence.partition("/")
    if not slash or not _TOKEN.fullmatch(kind) or not _TOKEN.fullmatch(subtype):
        return None
    return essence


def blocked(destination: str, content_type: str | None, nosniff_values: Sequence[str]) -> bool:
    """Fetch Section 2.10 (MIME type) or Section 3.6.1 (nosniff) blocks the response."""
    essence = mime_essence(content_type)
    if (
        destination in SCRIPT_LIKE
        and essence is not None
        and (essence.startswith(("audio/", "image/", "video/")) or essence == "text/csv")
    ):
        return True  # blocked even without nosniff
    if not determine_nosniff(nosniff_values):
        return False
    if destination in SCRIPT_LIKE:
        return essence is None or essence not in JAVASCRIPT_MIME_TYPES
    if destination == "style":
        return essence != "text/css"
    return False


# ---------------------------------------------------------------------------
# CSP, user-agent side (CSP Level 3 Sections 2.2.1, 2.3.1, 6.4.2)
# ---------------------------------------------------------------------------

_ASCII_WS = " \t\n\f\r"


def parse_csp(serialized: str) -> dict[str, list[str]]:
    """Parse a serialized CSP; a repeated directive is ignored, first one wins."""
    policy: dict[str, list[str]] = {}
    for raw in serialized.split(";"):
        token = raw.strip(_ASCII_WS)
        if not token or not token.isascii():
            continue
        name, *value = token.split()
        name = name.lower()
        if name not in policy:
            policy[name] = value
    return policy


def framing_allowed(csp_values: Sequence[str]) -> bool:
    """True unless an enforced policy has frame-ancestors 'none'; no default-src fallback."""
    for line in csp_values:
        for serialized in line.split(","):
            ancestors = parse_csp(serialized).get("frame-ancestors")
            if ancestors is not None and [s.lower() for s in ancestors] == ["'none'"]:
                return False
    return True


class _InlineScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.sources: list[str] = []
        self._buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and all(name != "src" for name, _ in attrs):
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._buffer is not None:
            self.sources.append("".join(self._buffer))
            self._buffer = None


def inline_scripts(html: str) -> list[str]:
    parser = _InlineScripts()
    parser.feed(html)
    parser.close()
    return parser.sources


def csp_hash(source: str) -> str:
    """A hash-source for an inline script: base64 SHA-256 of its UTF-8 text."""
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def swagger_ui_csp(
    html: str,
    *,
    asset_origin: str = "https://cdn.jsdelivr.net",
    favicon_origin: str = "https://fastapi.tiangolo.com",
) -> str:
    """A docs-page policy: no 'unsafe-inline'; each inline script allowed by its hash."""
    hashes = " ".join(csp_hash(source) for source in inline_scripts(html))
    return "; ".join(
        (
            "default-src 'none'",
            f"script-src {asset_origin} {hashes}",
            f"style-src {asset_origin}",
            f"img-src {favicon_origin} data:",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
            "form-action 'none'",
        )
    )


# ---------------------------------------------------------------------------
# Server side: the outermost ASGI middleware
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeaderPolicy:
    hsts: str = f"max-age={ONE_YEAR}; includeSubDomains"
    api_csp: str = "default-src 'none'; frame-ancestors 'none'"
    frame_options: str = "DENY"
    referrer_policy: str = "no-referrer"
    resource_policy: str = "same-origin"


def _replace(headers: RawHeaders, name: bytes, value: str | None) -> RawHeaders:
    kept = [(k, v) for k, v in headers if k.lower() != name]
    return kept if value is None else [*kept, (name, value.encode("latin-1"))]


def _values(headers: RawHeaders, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in headers if k.lower() == name]


def apply_policy(
    headers: RawHeaders, *, https: bool, credentialed: bool, policy: HeaderPolicy
) -> RawHeaders:
    """The header policy for one response; replace semantics, so each name appears once."""
    out = _replace(headers, b"x-content-type-options", "nosniff")
    # RFC 6797 Section 7.2: MUST NOT send STS over non-secure transport.
    out = _replace(out, b"strict-transport-security", policy.hsts if https else None)
    for name, default in (
        (b"content-security-policy", policy.api_csp),
        (b"x-frame-options", policy.frame_options),
        (b"referrer-policy", policy.referrer_policy),
        (b"cross-origin-resource-policy", policy.resource_policy),
    ):
        if not _values(out, name):
            out = _replace(out, name, default)  # a route may set its own, e.g. the docs CSP
    members = get_decode_split(_values(out, b"cache-control")) or []
    names = {m.split("=", 1)[0].strip(" \t").lower() for m in members if m}
    if not names or (credentialed and not names & {"no-store", "private"}):
        out = _replace(out, b"cache-control", "no-store")  # fail closed
    return out


class SecurityHeadersMiddleware:
    """Pure ASGI; rewrites `http.response.start`, never buffers the body."""

    def __init__(self, app: ASGIApp, policy: HeaderPolicy | None = None) -> None:
        self.app = app
        self.policy = policy or HeaderPolicy()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https"
        request_names = {name.lower() for name, _ in scope["headers"]}
        credentialed = bool(request_names & {b"authorization", b"cookie"})

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = apply_policy(
                    list(message.get("headers", [])),
                    https=https,
                    credentialed=credentialed,
                    policy=self.policy,
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def secure(app: ASGIApp, policy: HeaderPolicy | None = None) -> ASGIApp:
    """Wrap the whole application, outside Starlette's ServerErrorMiddleware."""
    return SecurityHeadersMiddleware(app, policy)


# ---------------------------------------------------------------------------
# security.txt (RFC 9116)
# ---------------------------------------------------------------------------

SECURITY_TXT_PATH = "/.well-known/security.txt"
_FIELD = re.compile(r"([!-9;-~]+):(.*)")  # RFC 5322 field-name: printable ASCII except ":"
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})")
_WEB_URI_FIELDS = frozenset(
    {"acknowledgments", "canonical", "contact", "encryption", "hiring", "policy"}
)
MAX_BYTES, MAX_LINES, MAX_FIELD = 32 * 1024, 1000, 2048  # RFC 9116 Section 5.4


def parse_rfc3339(value: str) -> datetime | None:
    """RFC 3339 date-time only: offset required, `T` and `Z` case-insensitive."""
    if not _RFC3339.fullmatch(value):
        return None
    normalized = value[:10] + "T" + value[11:]
    if normalized[-1] in "Zz":
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def render_security_txt(
    *,
    contacts: Sequence[str],
    expires: datetime,
    canonical: Sequence[str] = (),
    policy: str | None = None,
    preferred_languages: Sequence[str] = (),
) -> str:
    lines = [f"Contact: {contact}" for contact in contacts]
    lines.append(f"Expires: {expires.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines += [f"Canonical: {uri}" for uri in canonical]
    if policy is not None:
        lines.append(f"Policy: {policy}")
    if preferred_languages:
        lines.append(f"Preferred-Languages: {', '.join(preferred_languages)}")
    return "\n".join(lines) + "\n"


def validate_security_txt(text: str, *, now: datetime, retrieved_from: str) -> list[str]:
    """Problems an RFC 9116 consumer would flag in an unsigned file, as short codes."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        return ["too-large"]
    problems: list[str] = []
    if text and not text.endswith("\n"):
        problems.append("no-final-newline")  # every line MUST end with CRLF or LF (2.2)
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        return ["too-many-lines"]
    fields: dict[str, list[str]] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip(" \t") or line.startswith("#"):
            continue
        match = _FIELD.fullmatch(line.rstrip(" \t"))
        if match is None or not match.group(2).startswith(" ") or len(line) > MAX_FIELD:
            problems.append(f"malformed-line-{number}")  # `field-name ":" SP value`
            continue
        fields.setdefault(match.group(1).lower(), []).append(match.group(2)[1:].strip(" \t"))
    if not fields.get("contact"):
        problems.append("contact-missing")
    for name in sorted(_WEB_URI_FIELDS & fields.keys()):
        # A web URI MUST begin with "https://"; mailto:, tel:, dns:, openpgp4fpr: are fine.
        if any(uri.lower().startswith("http://") for uri in fields[name]):
            problems.append(f"insecure-uri-{name}")
    expires = fields.get("expires", [])
    if len(expires) != 1:
        problems.append("expires-missing" if not expires else "expires-repeated")
    else:
        stamp = parse_rfc3339(expires[0])
        if stamp is None:
            problems.append("expires-invalid")
        elif stamp <= now:
            problems.append("expires-past")
        elif stamp - now > timedelta(days=365):
            problems.append("expires-over-a-year")
    if len(fields.get("preferred-languages", [])) > 1:
        problems.append("preferred-languages-repeated")
    canonical = fields.get("canonical", [])
    if canonical and retrieved_from not in canonical:
        problems.append("canonical-mismatch")  # SHOULD NOT be trusted (Section 2.5.2)
    return problems


# ---------------------------------------------------------------------------
# The demo service: a JSON API, its Swagger UI page, and security.txt
# ---------------------------------------------------------------------------


def build_app(*, security_txt: Callable[[], str], docs_csp: bool = True) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/v1/me")
    async def me() -> JSONResponse:
        return JSONResponse({"user": "u-1"})

    @app.get("/v1/catalog")
    async def catalog() -> JSONResponse:
        # A route that wrongly marks a possibly per-user response as shareable.
        return JSONResponse({"items": []}, headers={"Cache-Control": "public, max-age=60"})

    @app.get("/v1/boom")
    async def boom() -> JSONResponse:
        raise RuntimeError("unhandled")

    @app.get("/docs", include_in_schema=False)
    async def docs(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        page = get_swagger_ui_html(openapi_url=f"{root_path}/openapi.json", title="API docs")
        if docs_csp:
            page.headers["Content-Security-Policy"] = swagger_ui_csp(bytes(page.body).decode())
        return page

    @app.get(SECURITY_TXT_PATH, include_in_schema=False)
    async def well_known_security_txt() -> PlainTextResponse:
        return PlainTextResponse(security_txt())

    @app.get("/security.txt", include_in_schema=False)
    async def legacy_security_txt() -> RedirectResponse:
        return RedirectResponse(SECURITY_TXT_PATH, status_code=301)

    return app
