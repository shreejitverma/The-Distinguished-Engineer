"""HTTP/1.1 request-smuggling framing model: CL.TE, TE.CL, and TE.TE desync (RFC 9112).

This module models the one decision that makes request smuggling possible: how a
recipient of an HTTP/1.1 message computes the ``message-body`` length from the
``Content-Length`` and ``Transfer-Encoding`` header fields (RFC 9112 Section 6.3).

Two recipients that resolve an *ambiguous* message (one carrying both framing
headers, or an obfuscated ``Transfer-Encoding``) to *different* body boundaries
desynchronize: bytes one recipient counts as body, the other counts as the start
of the next request. ``forward_and_reparse`` reproduces that split deterministically
without any sockets, and ``classify_framing(strict=True)`` models the RFC 9112
Section 6.1 defence of rejecting the ambiguous message outright.

Nothing here opens a network connection; every function consumes a ``bytes`` buffer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_CRLF = b"\r\n"
_HEADER_SEP = b"\r\n\r\n"
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
_MAX_CHUNK_HEX_DIGITS = 16
# RFC 9110 Section 5.6.2 tchar: the only octets allowed in a field-name token.
_TCHAR = frozenset(b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_CONTENT_LENGTH = b"content-length"
_TRANSFER_ENCODING = b"transfer-encoding"


class AmbiguousFraming(ValueError):
    """A message cannot be framed unambiguously under RFC 9112 Section 6.3.

    Raised by the strict recipient that implements the RFC 9112 Section 6.1
    defence: reject the message rather than guess a boundary an upstream peer
    might compute differently.
    """


class Framing(Enum):
    """Which rule of RFC 9112 Section 6.3 determined the body length."""

    NONE = "none"
    CONTENT_LENGTH = "content-length"
    CHUNKED = "chunked"


class Prefer(Enum):
    """Which header a lenient recipient trusts when a message carries both.

    ``TE`` models RFC 9112 Section 6.3 step 3 conformance (Transfer-Encoding
    overrides Content-Length); ``CL`` models a legacy or HTTP/1.0-only parser
    that ignores an unknown Transfer-Encoding and falls back to Content-Length.
    """

    TE = "te"
    CL = "cl"


@dataclass(frozen=True, slots=True)
class FramingDecision:
    """Resolved framing for one message under a given recipient policy."""

    framing: Framing
    content_length: int | None
    has_content_length: bool
    has_chunked_te: bool

    @property
    def is_ambiguous(self) -> bool:
        """True when both framing headers are present (the CL.TE/TE.CL trigger)."""
        return self.has_content_length and self.has_chunked_te


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """One request parsed off a connection buffer, plus where its bytes end."""

    method: bytes
    target: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    decision: FramingDecision
    end: int


@dataclass(frozen=True, slots=True)
class DesyncResult:
    """Outcome of forwarding one message through a front-end to a back-end.

    ``smuggled`` is the suffix of the forwarded bytes that the back-end treats
    as the beginning of a *new* request because it framed the body differently
    from the front-end. An empty ``smuggled`` with ``starved`` false means the
    two recipients agree and no request boundary was desynchronized.
    """

    front_body: bytes
    back_body: bytes
    smuggled: bytes
    starved: bool


def split_head(raw: bytes) -> tuple[bytes, int]:
    """Return the header block (through the blank line) and the body start offset."""
    sep = raw.find(_HEADER_SEP)
    if sep == -1:
        raise ValueError("incomplete message: no CRLFCRLF header terminator")
    return raw[:sep], sep + len(_HEADER_SEP)


def parse_request_head(block: bytes) -> tuple[bytes, bytes, tuple[tuple[bytes, bytes], ...]]:
    """Parse a request-line and field-lines with RFC 9112 Section 5.1 strictness.

    Rejects whitespace between field-name and colon (RFC 9112 Section 5.1), a
    field-name that is not a token (RFC 9110 Section 5.1), obsolete line folding
    (RFC 9112 Section 5.2), and bare CR (RFC 9112 Section 2.2), because each has
    been used to smuggle a framing header past one parser in a chain.
    """
    if b"\r" in block.replace(_CRLF, b""):
        raise ValueError("bare CR in control data (RFC 9112 Section 2.2)")
    lines = block.split(_CRLF)
    request_line = lines[0]
    parts = request_line.split(b" ")
    if len(parts) != 3:
        raise ValueError(f"malformed request-line: {request_line!r}")
    method, target, _version = parts
    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise ValueError("obsolete line folding (RFC 9112 Section 5.2)")
        colon = line.find(b":")
        if colon <= 0:
            raise ValueError(f"malformed field-line: {line!r}")
        name = line[:colon]
        if name[-1:] in (b" ", b"\t"):
            raise ValueError("whitespace before colon (RFC 9112 Section 5.1)")
        if any(b not in _TCHAR for b in name):
            raise ValueError(f"field-name is not a token: {name!r} (RFC 9110 Section 5.1)")
        value = line[colon + 1 :].strip(b" \t")
        # RFC 9110 Section 5.5: a field value is field-vchar (VCHAR / obs-text)
        # with interior SP/HTAB only. Any other control octet (for example a
        # vertical tab) is a smuggling obfuscation and must be rejected.
        if any((b < 0x20 and b != 0x09) or b == 0x7F for b in value):
            raise ValueError(f"control octet in field value: {value!r} (RFC 9110 Section 5.5)")
        headers.append((name.lower(), value))
    return method, target, tuple(headers)


def _content_length(headers: tuple[tuple[bytes, bytes], ...]) -> tuple[bool, int | None]:
    values = [v for n, v in headers if n == _CONTENT_LENGTH]
    if not values:
        return False, None
    unique = {v.strip() for v in values}
    if len(unique) != 1:
        raise AmbiguousFraming("conflicting Content-Length values (RFC 9112 Section 6.3 step 5)")
    only = unique.pop()
    if not only.isdigit():
        raise AmbiguousFraming(f"invalid Content-Length {only!r} (RFC 9112 Section 6.3 step 5)")
    return True, int(only)


def _chunked_te(headers: tuple[tuple[bytes, bytes], ...], *, strict: bool) -> bool:
    values = [v for n, v in headers if n == _TRANSFER_ENCODING]
    if not values:
        return False
    # Transfer coding names are case-insensitive, and several field lines combine
    # into one list (RFC 9112 Section 6.1). A lenient recipient only asks whether
    # the final coding is chunked (RFC 9112 Section 6.3 step 4).
    codings = [c.strip(b" \t").lower() for v in values for c in v.split(b",")]
    if strict and codings != [b"chunked"]:
        # The strict recipient understands only chunked, so any other, stacked, or
        # non-final coding is rejected (RFC 9112 Section 6.1 and Section 6.3 step 4),
        # the same policy as h11 0.16.0 normalize_and_validate.
        raise AmbiguousFraming(
            f"Transfer-Encoding is not exactly one chunked coding: {values!r} "
            "(RFC 9112 Section 6.1)"
        )
    return codings[-1] == b"chunked"


def classify_framing(
    method: bytes,
    headers: tuple[tuple[bytes, bytes], ...],
    *,
    prefer: Prefer = Prefer.TE,
    strict: bool = False,
    status_code: int | None = None,
) -> FramingDecision:
    """Resolve body framing per the RFC 9112 Section 6.3 precedence cascade.

    With ``strict=True`` a message carrying both ``Content-Length`` and a chunked
    ``Transfer-Encoding`` raises ``AmbiguousFraming`` (RFC 9112 Section 6.1 defence).
    With ``strict=False`` the ``prefer`` policy decides which header wins, which is
    exactly the divergence that CL.TE and TE.CL smuggling exploits.
    """
    has_cl, cl = _content_length(headers)
    has_te = _chunked_te(headers, strict=strict)

    # Step 1: HEAD and 204/304 responses are always empty (RFC 9112 Section 6.3 step 1).
    if status_code is not None and (method == b"HEAD" or status_code in (204, 304)):
        return FramingDecision(Framing.NONE, 0, has_cl, has_te)

    if has_cl and has_te:
        if strict:
            raise AmbiguousFraming(
                "both Content-Length and Transfer-Encoding present (RFC 9112 Section 6.3 step 3)"
            )
        if prefer is Prefer.TE:
            return FramingDecision(Framing.CHUNKED, None, has_cl, has_te)
        return FramingDecision(Framing.CONTENT_LENGTH, cl, has_cl, has_te)

    if has_te:
        return FramingDecision(Framing.CHUNKED, None, has_cl, has_te)
    if has_cl:
        return FramingDecision(Framing.CONTENT_LENGTH, cl, has_cl, has_te)
    # Step 7: a request with neither header has a zero-length body.
    return FramingDecision(Framing.NONE, 0, has_cl, has_te)


def _read_chunked(raw: bytes, offset: int) -> int:
    """Return the offset just past the terminating chunk and trailer section."""
    while True:
        line_end = raw.find(_CRLF, offset)
        if line_end == -1:
            raise ValueError("unterminated chunk-size line")
        size_field = raw[offset:line_end]
        hex_digits = size_field.split(b";", 1)[0].strip()
        if not (1 <= len(hex_digits) <= _MAX_CHUNK_HEX_DIGITS) or any(
            b not in _HEX_DIGITS for b in hex_digits
        ):
            raise ValueError(f"invalid chunk-size {hex_digits!r}")
        size = int(hex_digits, 16)
        offset = line_end + len(_CRLF)
        if size == 0:
            # Consume an optional trailer section ending at the blank line.
            while True:
                tr_end = raw.find(_CRLF, offset)
                if tr_end == -1:
                    raise ValueError("unterminated trailer section")
                if tr_end == offset:
                    return offset + len(_CRLF)
                offset = tr_end + len(_CRLF)
        data_end = offset + size
        if data_end + len(_CRLF) > len(raw) or raw[data_end : data_end + len(_CRLF)] != _CRLF:
            raise ValueError("truncated chunk data or missing chunk CRLF")
        offset = data_end + len(_CRLF)


def parse_one_message(
    raw: bytes,
    *,
    prefer: Prefer = Prefer.TE,
    strict: bool = False,
) -> ParsedMessage:
    """Parse the first request on *raw* and report the byte offset where it ends."""
    block, body_start = split_head(raw)
    method, target, headers = parse_request_head(block)
    decision = classify_framing(method, headers, prefer=prefer, strict=strict)

    if decision.framing is Framing.NONE:
        return ParsedMessage(method, target, headers, b"", decision, body_start)
    if decision.framing is Framing.CONTENT_LENGTH:
        length = decision.content_length if decision.content_length is not None else 0
        end = body_start + length
        if end > len(raw):
            raise ValueError("incomplete body: fewer bytes than Content-Length")
        return ParsedMessage(method, target, headers, raw[body_start:end], decision, end)
    end = _read_chunked(raw, body_start)
    return ParsedMessage(method, target, headers, raw[body_start:end], decision, end)


def forward_and_reparse(
    raw: bytes,
    *,
    front: Prefer,
    back: Prefer,
) -> DesyncResult:
    """Forward the front-end's view of message one to the back-end and reparse it.

    The front-end parses the first request under its ``front`` policy and forwards
    exactly the bytes it considers that request (``head + body``). The back-end
    reparses those forwarded bytes under its ``back`` policy. If the back-end frames
    a shorter body, the remainder is a smuggled request prefix; if it frames a longer
    body, it starves waiting for bytes the front-end withheld.
    """
    front_msg = parse_one_message(raw, prefer=front)
    forwarded = raw[: front_msg.end]
    try:
        back_msg = parse_one_message(forwarded, prefer=back)
    except ValueError:
        # The back-end could not complete the body within the forwarded bytes:
        # it is starved, holding a partial request until the next client write.
        return DesyncResult(front_msg.body, b"", b"", starved=True)
    smuggled = forwarded[back_msg.end :]
    return DesyncResult(front_msg.body, back_msg.body, smuggled, starved=False)


def te_obfuscations() -> tuple[tuple[str, bytes], ...]:
    """Transfer-Encoding header lines used to smuggle chunked past one parser.

    Each is a raw field-line (without trailing CRLF) for the TE.TE technique.
    ``space-before-colon``, ``leading-space-line``, ``prefixed-token``, and
    ``duplicate-line`` are in Kettle 2019; ``vertical-tab`` and ``control-in-name``
    are lab variants that replace Kettle's tab with a non-OWS control octet.
    ``parse_request_head`` plus ``classify_framing(strict=True)`` reject every one;
    a lenient parser that lowercases, strips, or ignores the unknown token may
    still treat it as chunked.
    """
    return (
        ("space-before-colon", b"Transfer-Encoding : chunked"),
        ("vertical-tab", b"Transfer-Encoding: \x0bchunked"),
        ("control-in-name", b"Transfer-Encoding\x0b: chunked"),
        ("leading-space-line", b" Transfer-Encoding: chunked"),
        ("prefixed-token", b"Transfer-Encoding: xchunked"),
        ("duplicate-line", b"Transfer-Encoding: chunked\r\nTransfer-Encoding: x"),
    )
