"""Proactive content negotiation, media type parsing, and charsets (RFC 9110 Sections 8.3, 12)."""

from __future__ import annotations

import codecs
import json
import re
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.requests import Request

# RFC 9110 Section 5.6.2: tchar = any VCHAR except delimiters.
_TCHAR = frozenset("!#$%&'*+-.^_`|~" + string.digits + string.ascii_letters)
# RFC 9110 Section 5.6.4: qdtext = HTAB / SP / %x21 / %x23-5B / %x5D-7E / obs-text.
_QDTEXT = frozenset(
    "\t !" + "".join(chr(c) for c in range(0x23, 0x5C)) + "".join(chr(c) for c in range(0x5D, 0x7F))
) | frozenset(chr(c) for c in range(0x80, 0x100))
# RFC 9110 Section 5.6.4: quoted-pair = "\" ( HTAB / SP / VCHAR / obs-text ).
_QPAIR = frozenset("\t" + "".join(chr(c) for c in range(0x20, 0x7F))) | frozenset(
    chr(c) for c in range(0x80, 0x100)
)
# RFC 9110 Section 12.4.2: qvalue = ( "0" [ "." 0*3DIGIT ] ) / ( "1" [ "." 0*3("0") ] ).
_QVALUE = re.compile(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\Z")
# RFC 9110 Section 5.6.1.2 asks recipients to bound list parsing; this lab picks 64 members.
MAX_ACCEPT_MEMBERS = 64


@dataclass(frozen=True, slots=True)
class MediaType:
    """A parsed media type: lowercased type, subtype, and parameter names (RFC 9110 8.3.1)."""

    type: str
    subtype: str
    params: tuple[tuple[str, str], ...] = ()

    @property
    def essence(self) -> str:
        return f"{self.type}/{self.subtype}"

    @property
    def suffix(self) -> str | None:
        """Structured syntax suffix after the last "+" (RFC 6838 Section 4.2.8)."""
        _, plus, tail = self.subtype.rpartition("+")
        return tail if plus else None

    def param(self, name: str) -> str | None:
        return dict(self.params).get(name.lower())

    def __str__(self) -> str:
        out = self.essence
        for name, value in self.params:
            token = value != "" and all(c in _TCHAR for c in value)
            quoted = value.replace("\\", "\\\\").replace('"', '\\"')
            out += f";{name}={value}" if token else f';{name}="{quoted}"'
        return out


@dataclass(frozen=True, slots=True)
class MediaRange:
    """One Accept member: a media range plus its weight in thousandths (0..1000)."""

    media: MediaType
    weight: int

    @property
    def specificity(self) -> tuple[bool, bool, int]:
        """Precedence key from RFC 9110 Section 12.5.1: */* < type/* < type/sub < +params."""
        return (self.media.type != "*", self.media.subtype != "*", len(self.media.params))

    def matches(self, offer: MediaType) -> bool:
        if self.media.type not in ("*", offer.type):
            return False
        if self.media.subtype not in ("*", offer.subtype):
            return False
        offered = dict(offer.params)
        return all(offered.get(name) == value for name, value in self.media.params)


def _token(text: str, i: int) -> tuple[str, int]:
    start = i
    while i < len(text) and text[i] in _TCHAR:
        i += 1
    if i == start:
        raise ValueError(f"expected token at offset {start} in {text!r}")
    return text[start:i], i


def _quoted_string(text: str, i: int) -> tuple[str, int]:
    out: list[str] = []
    i += 1  # opening DQUOTE
    while i < len(text):
        c = text[i]
        if c == '"':
            return "".join(out), i + 1
        if c == "\\" and i + 1 < len(text) and text[i + 1] in _QPAIR:
            out.append(text[i + 1])  # quoted-pair: keep the octet after the backslash
            i += 2
            continue
        if c not in _QDTEXT:
            raise ValueError(f"invalid character {c!r} in quoted-string")
        out.append(c)
        i += 1
    raise ValueError(f"unterminated quoted-string in {text!r}")


def _skip_ows(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _parse(text: str, *, allow_wildcard: bool) -> MediaType:
    i = _skip_ows(text, 0)
    type_, i = _token(text, i)
    if i >= len(text) or text[i] != "/":
        raise ValueError(f"missing '/' in media type {text!r}")
    subtype, i = _token(text, i + 1)
    type_, subtype = type_.lower(), subtype.lower()
    if "*" in type_ or "*" in subtype:
        # media-range allows only "*/*" and "type/*" (RFC 9110 Section 12.5.1).
        legal = allow_wildcard and subtype == "*" and (type_ == "*" or "*" not in type_)
        if not legal:
            raise ValueError(f"illegal wildcard in {text!r}")
    params: list[tuple[str, str]] = []
    while True:
        i = _skip_ows(text, i)
        if i == len(text):
            break
        if text[i] != ";":
            raise ValueError(f"unexpected {text[i]!r} at offset {i} in {text!r}")
        i = _skip_ows(text, i + 1)
        if i == len(text) or text[i] == ";":
            continue  # parameters = *( OWS ";" OWS [ parameter ] ) allows empty parameters
        name, i = _token(text, i)
        if i >= len(text) or text[i] != "=":
            raise ValueError(f"parameter {name!r} has no '=' (no whitespace allowed around it)")
        if i + 1 < len(text) and text[i + 1] == '"':
            value, i = _quoted_string(text, i + 1)
        else:
            value, i = _token(text, i + 1)
        name = name.lower()
        params.append((name, value.lower() if name == "charset" else value))
    return MediaType(type_, subtype, tuple(params))


def parse_media_type(text: str) -> MediaType:
    """Parse a ``Content-Type`` value; wildcards are not media types."""
    return _parse(text, allow_wildcard=False)


def parse_qvalue(text: str) -> int:
    """Return the weight in thousandths; exact, so ``0.001`` never rounds to zero."""
    if not _QVALUE.match(text):
        raise ValueError(f"invalid qvalue {text!r}")
    whole, _, frac = text.partition(".")
    return int(whole) * 1000 + int((frac + "000")[:3])


def _split_list(text: str) -> list[str]:
    """Split a #rule list on commas that are not inside a quoted-string."""
    members: list[str] = []
    start, i, in_quotes = 0, 0, False
    while i < len(text):
        c = text[i]
        if in_quotes and c == "\\":
            i += 2
            continue
        if c == '"':
            in_quotes = not in_quotes
        elif c == "," and not in_quotes:
            members.append(text[start:i])
            start = i + 1
        i += 1
    members.append(text[start:])
    return members


def parse_accept(text: str) -> tuple[MediaRange, ...]:
    """Parse an ``Accept`` field value into media ranges (RFC 9110 Section 12.5.1)."""
    members = _split_list(text)
    if len(members) > MAX_ACCEPT_MEMBERS:
        raise ValueError(f"Accept has {len(members)} members, limit is {MAX_ACCEPT_MEMBERS}")
    ranges: list[MediaRange] = []
    for member in members:
        if not member.strip(" \t"):
            continue  # RFC 9110 Section 5.6.1.2: ignore empty list elements
        media = _parse(member, allow_wildcard=True)
        weight = 1000
        kept: list[tuple[str, str]] = []
        for name, value in media.params:
            if name == "q":
                weight = parse_qvalue(value)  # "q" is the weight wherever it appears
            else:
                kept.append((name, value))
        ranges.append(MediaRange(MediaType(media.type, media.subtype, tuple(kept)), weight))
    return tuple(ranges)


def quality(offer: MediaType, ranges: Sequence[MediaRange]) -> int:
    """Weight of *offer*: taken from the most specific matching range, 0 if none matches."""
    best: MediaRange | None = None
    for candidate in ranges:
        if candidate.matches(offer) and (best is None or candidate.specificity > best.specificity):
            best = candidate
    return 0 if best is None else best.weight


def negotiate(accept: str | None, offers: Sequence[str]) -> str | None:
    """Pick the offer with the highest nonzero weight; ties go to the server's order.

    An absent ``Accept`` means no preference (RFC 9110 Section 12.4.1): the first offer wins.
    ``None`` means nothing is acceptable, which the caller maps to ``406 Not Acceptable``.
    """
    if accept is None:
        return offers[0] if offers else None
    ranges = parse_accept(accept)
    best_offer: str | None = None
    best_weight = 0
    for offer in offers:
        weight = quality(parse_media_type(offer), ranges)
        if weight > best_weight:
            best_offer, best_weight = offer, weight
    return best_offer


class UnsupportedContentError(ValueError):
    """The request content cannot be processed; maps to ``415 Unsupported Media Type``."""


def decode_text(media: MediaType, body: bytes) -> str:
    """Decode request content using the charset rules of its media type."""
    is_json = media.type == "application" and "json" in (media.subtype, media.suffix)
    if is_json:
        charset = media.param("charset")
        if charset not in (None, "utf-8"):
            raise UnsupportedContentError(f"JSON must be UTF-8 (RFC 8259 8.1), got {charset!r}")
        return body.decode("utf-8")
    if media.essence == "text/plain":
        charset = media.param("charset") or "us-ascii"  # RFC 6657 Section 4 default
        try:
            return body.decode(codecs.lookup(charset).name)
        except LookupError as exc:
            raise UnsupportedContentError(f"unknown charset {charset!r}") from exc
    raise UnsupportedContentError(f"unsupported media type {media.essence!r}")


REPORT = {"id": 7, "title": "Q3 latency", "p99_ms": 41}
REPORT_TYPES = ("application/json", "text/csv", "text/html;charset=utf-8")
ACCEPTED_REQUEST_TYPES = "application/json, text/plain"


def _render(media_type: str) -> bytes:
    if media_type == "application/json":
        return json.dumps(REPORT, separators=(",", ":")).encode()
    if media_type == "text/csv":
        return b"id,title,p99_ms\r\n7,Q3 latency,41\r\n"
    return b"<h1>Q3 latency</h1><p>p99 41 ms</p>"


async def get_report(request: Request) -> Response:
    # RFC 9110 Section 5.3: field lines with one name combine into one comma-separated value.
    lines = request.headers.getlist("accept")
    accept = ", ".join(lines) if lines else None
    vary = {"Vary": "Accept"}
    try:
        chosen = negotiate(accept, REPORT_TYPES)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400, headers=vary)
    if chosen is None:
        body = {"detail": "no acceptable representation", "available": list(REPORT_TYPES)}
        return JSONResponse(body, status_code=406, headers=vary)
    return Response(_render(chosen), media_type=chosen, headers=vary)


async def post_note(request: Request) -> Response:
    lines = request.headers.getlist("content-type")
    if len(lines) > 1:
        return JSONResponse({"detail": "Content-Type is a singleton field"}, status_code=400)
    body = await request.body()
    try:
        if not lines:
            raise UnsupportedContentError("missing Content-Type")
        text = decode_text(parse_media_type(lines[0]), body)
    except UnsupportedContentError as exc:
        headers = {"Accept": ACCEPTED_REQUEST_TYPES}
        return JSONResponse({"detail": str(exc)}, status_code=415, headers=headers)
    except (ValueError, UnicodeDecodeError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"chars": len(text), "text": text}, status_code=201)


app = Starlette(
    routes=[
        Route("/report", get_report, methods=["GET"]),
        Route("/notes", post_note, methods=["POST"]),
    ]
)
