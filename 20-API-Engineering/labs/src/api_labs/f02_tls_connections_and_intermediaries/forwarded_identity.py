"""Client identity behind proxies: RFC 7239 Forwarded parsing and right-to-left trusted-hop peeling.

A client controls every byte of the forwarding headers it sends, so only entries appended by
proxies you operate are evidence.
Each proxy appends to the right (RFC 7239 Section 4, RFC 9110 Section 5.3), so the client
address is the rightmost entry that was not written by a trusted proxy.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
type Headers = Sequence[tuple[bytes, bytes]]

MAX_HOPS = 32
"""Upper bound on list elements accepted from one request; longer chains are rejected."""

_TCHAR = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_OBF_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_OWS = " \t"


class ForwardingHeaderError(ValueError):
    """A forwarding header is malformed or longer than `MAX_HOPS`."""


@dataclass(frozen=True, slots=True)
class Node:
    """One RFC 7239 Section 6 node identifier."""

    kind: Literal["ip", "unknown", "obfuscated"]
    ip: IPAddress | None
    port: int | None
    raw: str


@dataclass(frozen=True, slots=True)
class ClientResolution:
    """The resolved client address and how it was reached."""

    client: IPAddress | None
    peeled: int
    reason: Literal["peer-untrusted", "first-untrusted-hop", "all-trusted", "invalid-hop"]


def _field_values(headers: Headers, name: bytes) -> list[str]:
    """Return every field line value for `name`, in received order (RFC 9110 Section 5.3)."""
    return [value.decode("latin-1") for key, value in headers if key.lower() == name]


def _split_quoted(text: str, sep: str) -> list[str]:
    """Split on `sep` outside quoted-strings, honouring quoted-pair (RFC 9110 Section 5.6.4)."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif in_quotes and char == "\\":
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == sep and not in_quotes:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if in_quotes or escaped:
        raise ForwardingHeaderError(f"unterminated quoted-string in {text!r}")
    parts.append("".join(current))
    return parts


def _unquote(value: str) -> str:
    """Return a token as-is, or a quoted-string with its quotes and quoted-pairs removed."""
    if not value.startswith('"'):
        if not value or not set(value) <= _TCHAR:
            raise ForwardingHeaderError(f"value {value!r} is neither token nor quoted-string")
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise ForwardingHeaderError(f"malformed quoted-string {value!r}")
    out: list[str] = []
    inner = value[1:-1]
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner):
            index += 1
            char = inner[index]
        elif char in '"\\':
            raise ForwardingHeaderError(f"unescaped {char!r} inside quoted-string {value!r}")
        out.append(char)
        index += 1
    return "".join(out)


def _list_elements(values: Iterable[str]) -> list[str]:
    """Flatten field lines into non-empty list elements (RFC 9110 Section 5.6.1.2)."""
    elements = [
        element.strip(_OWS)
        for value in values
        for element in _split_quoted(value, ",")
        if element.strip(_OWS)
    ]
    if len(elements) > MAX_HOPS:
        raise ForwardingHeaderError(f"{len(elements)} hops exceeds MAX_HOPS={MAX_HOPS}")
    return elements


def parse_forwarded(headers: Headers) -> list[dict[str, str]]:
    """Parse all `Forwarded` field lines into one dict per element (RFC 7239 Section 4)."""
    elements: list[dict[str, str]] = []
    for element in _list_elements(_field_values(headers, b"forwarded")):
        pairs: dict[str, str] = {}
        for pair in _split_quoted(element, ";"):
            pair = pair.strip(_OWS)
            if not pair:
                continue
            name, eq, value = pair.partition("=")
            name = name.lower()
            if not eq or not name or not set(name) <= _TCHAR:
                raise ForwardingHeaderError(f"malformed forwarded-pair {pair!r}")
            if name in pairs:
                raise ForwardingHeaderError(f"parameter {name!r} repeated in one element")
            pairs[name] = _unquote(value)
        elements.append(pairs)
    return elements


def _is_obf(text: str) -> bool:
    return len(text) > 1 and text[0] == "_" and set(text[1:]) <= _OBF_CHARS


def parse_node(text: str) -> Node:
    """Parse an unquoted RFC 7239 Section 6 `node`: IPv4, `[IPv6]`, `unknown`, or `_obfnode`."""
    port: int | None = None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise ForwardingHeaderError(f"unclosed IPv6 bracket in {text!r}")
        name, rest = text[: end + 1], text[end + 1 :]
    else:
        name, colon, port_text = text.partition(":")
        rest = colon + port_text
    if rest:
        port_text = rest.removeprefix(":")
        if not rest.startswith(":") or not port_text:
            raise ForwardingHeaderError(f"malformed node-port in {text!r}")
        if port_text.isdigit() and len(port_text) <= 5:
            port = int(port_text)
        elif not _is_obf(port_text):
            raise ForwardingHeaderError(f"malformed node-port in {text!r}")
    if name.lower() == "unknown":
        return Node("unknown", None, port, text)
    if _is_obf(name):
        return Node("obfuscated", None, port, text)
    try:
        ip = (
            ipaddress.IPv6Address(name[1:-1])
            if name.startswith("[")
            else ipaddress.IPv4Address(name)
        )
    except ValueError as exc:
        raise ForwardingHeaderError(f"invalid nodename in {text!r}") from exc
    return Node("ip", ip, port, text)


def parse_x_forwarded_for(headers: Headers) -> list[str]:
    """Return every `X-Forwarded-For` entry across all field lines, leftmost first."""
    return _list_elements(_field_values(headers, b"x-forwarded-for"))


def xff_entry_ip(entry: str) -> IPAddress | None:
    """Parse a de facto `X-Forwarded-For` entry: IP, `IPv4:port`, `[IPv6]`, or `[IPv6]:port`."""
    candidate = entry
    if entry.startswith("["):
        candidate = entry[1 : entry.find("]")] if "]" in entry else entry
    elif entry.count(":") == 1:
        candidate = entry.partition(":")[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def xff_to_forwarded(entries: Sequence[str]) -> str:
    """Translate `X-Forwarded-For` entries to a `Forwarded` value (RFC 7239 Section 7.4)."""
    out: list[str] = []
    for entry in entries:
        ip = xff_entry_ip(entry)
        if ip is None:
            raise ForwardingHeaderError(f"cannot translate X-Forwarded-For entry {entry!r}")
        out.append(f'for="[{ip}]"' if ip.version == 6 else f"for={ip}")
    return ", ".join(out)


@dataclass(frozen=True, slots=True)
class TrustedProxies:
    """The CIDR ranges whose hosts are proxies you operate."""

    networks: tuple[IPNetwork, ...]

    @classmethod
    def from_cidrs(cls, *cidrs: str) -> TrustedProxies:
        return cls(tuple(ipaddress.ip_network(cidr) for cidr in cidrs))

    def __contains__(self, ip: IPAddress | None) -> bool:
        return ip is not None and any(ip in net for net in self.networks)


def resolve_client(
    peer: IPAddress, hops: Sequence[IPAddress | None], trusted: TrustedProxies
) -> ClientResolution:
    """Peel trusted proxies right-to-left from `hops + [peer]`; `None` marks an unparseable hop.

    Headers are evidence only if the TCP peer itself is a trusted proxy.
    Every entry left of the first untrusted hop was written by an untrusted party and is ignored.
    """
    if peer not in trusted:
        return ClientResolution(peer, 0, "peer-untrusted")
    for peeled, hop in enumerate(reversed(hops), start=1):
        if hop is None:
            return ClientResolution(None, peeled, "invalid-hop")
        if hop not in trusted:
            return ClientResolution(hop, peeled, "first-untrusted-hop")
    return ClientResolution(hops[0] if hops else peer, len(hops), "all-trusted")


def client_from_xff(peer: IPAddress, headers: Headers, trusted: TrustedProxies) -> ClientResolution:
    """Resolve the client from `X-Forwarded-For`, joining every field line first."""
    if peer not in trusted:
        return resolve_client(peer, [], trusted)
    hops = [xff_entry_ip(entry) for entry in parse_x_forwarded_for(headers)]
    return resolve_client(peer, hops, trusted)


def client_from_forwarded(
    peer: IPAddress, headers: Headers, trusted: TrustedProxies
) -> ClientResolution:
    """Resolve the client from the `for=` parameter of each RFC 7239 `Forwarded` element."""
    if peer not in trusted:
        return resolve_client(peer, [], trusted)
    hops: list[IPAddress | None] = []
    for element in parse_forwarded(headers):
        node = parse_node(element["for"]) if "for" in element else None
        hops.append(node.ip if node is not None else None)
    return resolve_client(peer, hops, trusted)


def naive_leftmost_xff(headers: Headers, peer: str) -> str:
    """The vulnerable pattern: trust the first `X-Forwarded-For` entry, which the client wrote."""
    entries = parse_x_forwarded_for(headers)
    return entries[0] if entries else peer


def proxy_append_xff(headers: Headers, remote_addr: str) -> list[tuple[bytes, bytes]]:
    """Emulate nginx `$proxy_add_x_forwarded_for`: join existing entries, append `remote_addr`."""
    existing = ", ".join(_field_values(headers, b"x-forwarded-for"))
    value = f"{existing}, {remote_addr}" if existing else remote_addr
    kept = [(k, v) for k, v in headers if k.lower() != b"x-forwarded-for"]
    return [*kept, (b"x-forwarded-for", value.encode("latin-1"))]
