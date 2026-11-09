"""RFC 3986 reference resolution and normalization, and RFC 6570 Level 4 URI Template expansion."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
GEN_DELIMS = frozenset(":/?#[]@")
SUB_DELIMS = frozenset("!$&'()*+,;=")
RESERVED = GEN_DELIMS | SUB_DELIMS
DEFAULT_PORTS: Mapping[str, str] = {"http": "80", "https": "443"}

_HEX = frozenset("0123456789abcdefABCDEF")
_DIGITS = frozenset("0123456789")
# RFC 3986 Appendix B: matches every string, so parsing never fails; validation is separate.
_URI_REFERENCE = re.compile(r"(([^:/?#]+):)?(//([^/?#]*))?([^?#]*)(\?([^#]*))?(#(.*))?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class URIReference:
    """The five components of RFC 3986 Section 3; `None` means the delimiter was absent."""

    scheme: str | None
    authority: str | None
    path: str
    query: str | None
    fragment: str | None

    def recompose(self) -> str:
        """Rebuild the reference string (RFC 3986 Section 5.3)."""
        out: list[str] = []
        if self.scheme is not None:
            out.append(self.scheme + ":")
        if self.authority is not None:
            out.append("//" + self.authority)
        out.append(self.path)
        if self.query is not None:
            out.append("?" + self.query)
        if self.fragment is not None:
            out.append("#" + self.fragment)
        return "".join(out)


def parse_reference(reference: str) -> URIReference:
    """Split *reference* into components with the RFC 3986 Appendix B regular expression."""
    match = _URI_REFERENCE.fullmatch(reference)
    if match is None:  # pragma: no cover - the Appendix B expression matches every string
        raise ValueError("unparseable URI reference")
    return URIReference(match[2], match[4], match[5], match[7], match[9])


def remove_dot_segments(path: str) -> str:
    """Interpret "." and ".." segments with the two-buffer loop of RFC 3986 Section 5.2.4."""
    buffer = path
    output: list[str] = []
    while buffer:
        if buffer.startswith("../"):
            buffer = buffer[3:]
        elif buffer.startswith("./"):
            buffer = buffer[2:]
        elif buffer.startswith("/./"):
            buffer = "/" + buffer[3:]
        elif buffer == "/.":
            buffer = "/"
        elif buffer.startswith("/../") or buffer == "/..":
            buffer = "/" + buffer[4:]
            if output:
                output.pop()
        elif buffer in {".", ".."}:
            buffer = ""
        else:
            end = buffer.find("/", 1 if buffer.startswith("/") else 0)
            end = len(buffer) if end == -1 else end
            output.append(buffer[:end])
            buffer = buffer[end:]
    return "".join(output)


def _merge(base: URIReference, reference_path: str) -> str:
    """Merge a relative-path reference with the base path (RFC 3986 Section 5.2.3)."""
    if base.authority is not None and not base.path:
        return "/" + reference_path
    return base.path[: base.path.rfind("/") + 1] + reference_path


def resolve(base: str, reference: str, *, strict: bool = True) -> str:
    """Resolve *reference* against the absolute URI *base* (RFC 3986 Section 5.2.2)."""
    b = parse_reference(base)
    if b.scheme is None:
        raise ValueError("base URI must be absolute (RFC 3986 Section 5.2.1)")
    r = parse_reference(reference)
    scheme = None if (not strict and r.scheme == b.scheme) else r.scheme
    if scheme is not None:
        target = URIReference(scheme, r.authority, remove_dot_segments(r.path), r.query, r.fragment)
    elif r.authority is not None:
        target = URIReference(
            b.scheme, r.authority, remove_dot_segments(r.path), r.query, r.fragment
        )
    elif not r.path:
        query = r.query if r.query is not None else b.query
        target = URIReference(b.scheme, b.authority, b.path, query, r.fragment)
    else:
        merged = r.path if r.path.startswith("/") else _merge(b, r.path)
        target = URIReference(
            b.scheme, b.authority, remove_dot_segments(merged), r.query, r.fragment
        )
    return target.recompose()


def _is_triplet(text: str, index: int) -> bool:
    return (
        text[index] == "%"
        and index + 2 < len(text)
        and text[index + 1] in _HEX
        and text[index + 2] in _HEX
    )


def normalize_percent_encoding(component: str) -> str:
    """Uppercase every triplet and decode only unreserved octets (RFC 3986 Section 6.2.2.2)."""
    out: list[str] = []
    i = 0
    while i < len(component):
        if component[i] != "%":
            out.append(component[i])
            i += 1
            continue
        if not _is_triplet(component, i):
            raise ValueError(f"malformed percent-encoding at offset {i}")
        octet = chr(int(component[i + 1 : i + 3], 16))
        out.append(octet if octet in UNRESERVED else component[i : i + 3].upper())
        i += 3
    return "".join(out)


def _normalize_authority(authority: str, scheme: str) -> str:
    userinfo, at, hostport = authority.rpartition("@")
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            raise ValueError("unterminated IP-literal in authority")
        host, rest = hostport[: close + 1], hostport[close + 1 :]
        if rest and not rest.startswith(":"):
            raise ValueError("garbage after IP-literal in authority")
        port = rest[1:] if rest else None
    else:
        host, colon, port_text = hostport.partition(":")
        port = port_text if colon else None
    if port is not None and not set(port) <= _DIGITS:
        raise ValueError(f"invalid port {port!r}")
    if port is not None and (not port or port == DEFAULT_PORTS.get(scheme)):
        port = None
    head, *triplets = normalize_percent_encoding(host).split("%")
    host = head.lower() + "".join("%" + t[:2] + t[2:].lower() for t in triplets)
    prefix = normalize_percent_encoding(userinfo) + at
    return prefix + host + ("" if port is None else ":" + port)


def normalize_uri(uri: str) -> str:
    """Apply syntax-based normalization, plus scheme-based rules for http and https.

    Case and percent-encoding normalization run before dot-segment removal, so that an
    encoded `%2E%2E` segment is removed like a literal `..` (RFC 3986 Sections 2.4 and 6.2.2).
    """
    parts = parse_reference(uri)
    if parts.scheme is None:
        raise ValueError("only absolute URIs can be normalized")
    scheme = parts.scheme.lower()
    authority = parts.authority
    if authority is not None:
        authority = _normalize_authority(authority, scheme)
    path = remove_dot_segments(normalize_percent_encoding(parts.path))
    if scheme in DEFAULT_PORTS and authority is not None and not path:
        path = "/"
    query = None if parts.query is None else normalize_percent_encoding(parts.query)
    fragment = None if parts.fragment is None else normalize_percent_encoding(parts.fragment)
    return URIReference(scheme, authority, path, query, fragment).recompose()


def parse_path_segments(raw_path: bytes) -> tuple[str, ...]:
    """Split an origin-form path on "/" first, then strictly decode each segment.

    Splitting before decoding keeps `%2F` inside one segment (RFC 3986 Sections 2.4 and 7.3).
    Decoded dot segments, NUL, malformed triplets, and invalid UTF-8 raise `ValueError`.
    """
    text = raw_path.decode("ascii")
    if not text.startswith("/"):
        raise ValueError("origin-form path must start with '/'")
    segments: list[str] = []
    for raw in text[1:].split("/"):
        octets = bytearray()
        i = 0
        while i < len(raw):
            if raw[i] == "%":
                if not _is_triplet(raw, i):
                    raise ValueError(f"malformed percent-encoding in segment {raw!r}")
                octets.append(int(raw[i + 1 : i + 3], 16))
                i += 3
            else:
                octets.extend(raw[i].encode("ascii"))
                i += 1
        segment = octets.decode("utf-8")
        if segment in {".", ".."}:
            raise ValueError(f"dot segment {raw!r} after decoding")
        if "\x00" in segment:
            raise ValueError(f"NUL octet in segment {raw!r}")
        segments.append(segment)
    return tuple(segments)


class TemplateError(ValueError):
    """A URI Template that does not match the RFC 6570 Level 4 grammar."""


type TemplateValue = str | Sequence[str] | Mapping[str, str | None] | None


@dataclass(frozen=True, slots=True)
class _Operator:
    """One column of the RFC 6570 Appendix A expansion table."""

    first: str
    sep: str
    named: bool
    ifemp: str
    allow_reserved: bool


_OPERATORS: Mapping[str, _Operator] = {
    "": _Operator("", ",", named=False, ifemp="", allow_reserved=False),
    "+": _Operator("", ",", named=False, ifemp="", allow_reserved=True),
    "#": _Operator("#", ",", named=False, ifemp="", allow_reserved=True),
    ".": _Operator(".", ".", named=False, ifemp="", allow_reserved=False),
    "/": _Operator("/", "/", named=False, ifemp="", allow_reserved=False),
    ";": _Operator(";", ";", named=True, ifemp="", allow_reserved=False),
    "?": _Operator("?", "&", named=True, ifemp="=", allow_reserved=False),
    "&": _Operator("&", "&", named=True, ifemp="=", allow_reserved=False),
}
_FUTURE_OPERATORS = frozenset("=,!@|")
_VARCHAR = r"(?:[A-Za-z0-9_]|%[0-9A-Fa-f]{2})"
_VARSPEC = re.compile(rf"({_VARCHAR}(?:\.?{_VARCHAR})*)(?::([1-9][0-9]{{0,3}})|(\*))?")


def _encode(text: str, *, allow_reserved: bool) -> str:
    """Percent-encode as UTF-8 every character outside the allow set (RFC 6570 Section 3.2.1)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if allow_reserved and _is_triplet(text, i):
            out.append(text[i : i + 3])
            i += 3
            continue
        if char in UNRESERVED or (allow_reserved and char in RESERVED):
            out.append(char)
        else:
            out.extend(f"%{octet:02X}" for octet in char.encode("utf-8"))
        i += 1
    return "".join(out)


def _prefix(text: str, length: int, *, allow_reserved: bool) -> str:
    """Take *length* characters, counting an existing triplet as one under `+` and `#`."""
    i = 0
    for _ in range(length):
        if i >= len(text):
            break
        i += 3 if allow_reserved and _is_triplet(text, i) else 1
    return text[:i]


def _expand_variable(
    op: _Operator, name: str, value: TemplateValue, prefix: int | None, explode: bool
) -> str | None:
    """Expand one varspec; `None` means undefined and contributes nothing (Section 3.2.1)."""
    if value is None:
        return None
    reserved = op.allow_reserved
    if isinstance(value, str):
        text = value if prefix is None else _prefix(value, prefix, allow_reserved=reserved)
        encoded = _encode(text, allow_reserved=reserved)
        if op.named:
            return name + (op.ifemp if not value else "=" + encoded)
        return encoded
    if prefix is not None:
        raise TemplateError(f"prefix modifier on composite variable {name!r} (Section 2.4.1)")
    if isinstance(value, Mapping):
        pairs = [(key, member) for key, member in value.items() if member is not None]
        if not pairs:
            return None
        if explode:
            if op.named:
                return op.sep.join(
                    _encode(key, allow_reserved=True)
                    + ("=" + _encode(member, allow_reserved=reserved) if member else op.ifemp)
                    for key, member in pairs
                )
            return op.sep.join(
                _encode(key, allow_reserved=reserved)
                + "="
                + _encode(member, allow_reserved=reserved)
                for key, member in pairs
            )
        joined = ",".join(
            _encode(key, allow_reserved=reserved) + "," + _encode(member, allow_reserved=reserved)
            for key, member in pairs
        )
        return name + "=" + joined if op.named else joined
    members = list(value)
    if not members:
        return None
    if explode:
        if op.named:
            return op.sep.join(
                name + ("=" + _encode(member, allow_reserved=reserved) if member else op.ifemp)
                for member in members
            )
        return op.sep.join(_encode(member, allow_reserved=reserved) for member in members)
    joined = ",".join(_encode(member, allow_reserved=reserved) for member in members)
    return name + "=" + joined if op.named else joined


def _expand_expression(body: str, variables: Mapping[str, TemplateValue]) -> str:
    if not body:
        raise TemplateError("empty expression '{}'")
    if body[0] in _FUTURE_OPERATORS:
        raise TemplateError(f"operator {body[0]!r} is reserved for future extensions")
    op_char = body[0] if body[0] in _OPERATORS else ""
    op = _OPERATORS[op_char]
    expansions: list[str] = []
    for spec in body[len(op_char) :].split(","):
        match = _VARSPEC.fullmatch(spec)
        if match is None:
            raise TemplateError(f"invalid varspec {spec!r} in expression {{{body}}}")
        name, prefix, explode = match[1], match[2], match[3] is not None
        expanded = _expand_variable(
            op, name, variables.get(name), None if prefix is None else int(prefix), explode
        )
        if expanded is not None:
            expansions.append(expanded)
    return op.first + op.sep.join(expansions) if expansions else ""


def expand(template: str, variables: Mapping[str, TemplateValue]) -> str:
    """Expand an RFC 6570 Level 4 URI Template, raising `TemplateError` on malformed input.

    RFC 6570 Section 3 says a processor SHOULD return a partial expansion plus an error
    indication; raising instead keeps malformed templates from reaching the network.
    """
    out: list[str] = []
    pos = 0
    while pos < len(template):
        start = template.find("{", pos)
        literal = template[pos:] if start == -1 else template[pos:start]
        if "}" in literal:
            raise TemplateError(f"unmatched '}}' at offset {pos + literal.index('}')}")
        out.append(_encode(literal, allow_reserved=True))
        if start == -1:
            break
        end = template.find("}", start + 1)
        if end == -1:
            raise TemplateError(f"unclosed expression at offset {start}")
        out.append(_expand_expression(template[start + 1 : end], variables))
        pos = end + 1
    return "".join(out)
