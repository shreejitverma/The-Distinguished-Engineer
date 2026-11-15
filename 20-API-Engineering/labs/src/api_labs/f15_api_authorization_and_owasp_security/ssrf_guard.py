"""SSRF defenses for httpx: why a pre-flight URL check fails, and a connect-time guard.

The guard lives in an httpcore network backend, the one place that sees every TCP
connection httpx opens, including redirect hops and pool refills.  It resolves the
host once, rejects the whole answer set if any address is special-purpose, and then
connects to the vetted IP literal, so the socket layer never resolves the name again.
TLS still verifies against the original host name, because httpcore passes the URL
host (not the connected address) as `server_hostname`.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit

import anyio
import httpcore
import httpx

if TYPE_CHECKING:
    import ssl
    from collections.abc import Iterable

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type SocketOption = (
    tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
)

# IANA IPv4 and IPv6 Special-Purpose Address Registries (as of 2026-11), every block
# whose "Globally Reachable" column is False, N/A, or blank, plus multicast and the
# deprecated site-local range.  Explicit tables instead of `ip.is_global`: CPython's
# copy of the registry changes between releases (CVE-2024-4032) and lags it
# (5f00::/16 and 100:0:0:1::/64 are is_global on 3.12.13 to 3.15.2).
BLOCKED_V4: tuple[tuple[ipaddress.IPv4Network, str], ...] = tuple(
    (ipaddress.IPv4Network(net), reason)
    for net, reason in (
        ("0.0.0.0/8", "this-network"),
        ("10.0.0.0/8", "private-use"),
        ("100.64.0.0/10", "shared-address-space"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local"),
        ("172.16.0.0/12", "private-use"),
        ("192.0.0.0/24", "ietf-protocol-assignments"),
        ("192.0.2.0/24", "documentation"),
        ("192.88.99.0/24", "6to4-relay-anycast"),
        ("192.168.0.0/16", "private-use"),
        ("198.18.0.0/15", "benchmarking"),
        ("198.51.100.0/24", "documentation"),
        ("203.0.113.0/24", "documentation"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
    )
)
BLOCKED_V6: tuple[tuple[ipaddress.IPv6Network, str], ...] = tuple(
    (ipaddress.IPv6Network(net), reason)
    for net, reason in (
        ("::/96", "ipv4-compatible"),  # covers ::/128 and ::1/128; RFC 4291 2.5.5.1
        ("64:ff9b:1::/48", "local-use-translation"),
        ("100::/64", "discard-only"),
        ("100:0:0:1::/64", "dummy-prefix"),  # RFC 9780
        ("2001::/23", "ietf-protocol-assignments"),
        ("2001:db8::/32", "documentation"),
        ("3fff::/20", "documentation"),
        ("5f00::/16", "srv6-sids"),
        ("fc00::/7", "unique-local"),
        ("fe80::/10", "link-local"),
        ("fec0::/10", "site-local"),
        ("ff00::/8", "multicast"),
    )
)
NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")  # RFC 6052 Section 2.1


def embedded_ipv4(address: ipaddress.IPv6Address) -> tuple[ipaddress.IPv4Address, str] | None:
    """Return the IPv4 address a v6 address reaches through mapping or translation."""
    if address.ipv4_mapped is not None:  # ::ffff:0:0/96, RFC 4291 Section 2.5.5.2
        return address.ipv4_mapped, "ipv4-mapped"
    if address in NAT64_WELL_KNOWN:  # a NAT64 gateway forwards to the low 32 bits
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF), "nat64"
    if address.sixtofour is not None:  # 2002::/16, RFC 3056
        return address.sixtofour, "6to4"
    return None


def blocked_reason(address: IPAddress) -> str | None:
    """Return why `address` must not be dialed, or None if it is a public unicast address."""
    if isinstance(address, ipaddress.IPv6Address):
        inner = embedded_ipv4(address)
        if inner is not None:
            v4, via = inner
            reason = blocked_reason(v4)
            return None if reason is None else f"{via}:{reason}"
        for v6_net, v6_reason in BLOCKED_V6:
            if address in v6_net:
                return v6_reason
        return None
    for v4_net, v4_reason in BLOCKED_V4:
        if address in v4_net:
            return v4_reason
    return None


def parse_ip_literal(host: str) -> IPAddress | None:
    """Strict literal parse: '127.1' and '2130706433' are host names here, not addresses."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


class SSRFBlockedError(Exception):
    """Raised before any socket is opened to a destination the policy forbids."""

    def __init__(self, host: str, address: str, reason: str) -> None:
        super().__init__(f"egress to {host!r} ({address or 'n/a'}) blocked: {reason}")
        self.host = host
        self.address = address
        self.reason = reason


def socket_backend() -> httpcore.AsyncNetworkBackend:
    """httpcore's real anyio dialer (its module declares a stub when anyio is absent)."""
    return cast("httpcore.AsyncNetworkBackend", httpcore.AnyIOBackend())


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    """getaddrinfo in a worker thread (anyio), de-duplicated, in resolver order."""

    async def resolve(self, host: str, port: int) -> list[str]:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    allowed_ports: frozenset[int] = frozenset({80, 443})
    allowed_hosts: frozenset[str] | None = None  # None: any host that resolves publicly


@dataclass(frozen=True, slots=True)
class Dial:
    host: str
    address: str
    port: int


@dataclass(slots=True)
class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, vet every answer, then dial only vetted IP literals."""

    resolver: Resolver
    policy: EgressPolicy = field(default_factory=EgressPolicy)
    inner: httpcore.AsyncNetworkBackend = field(default_factory=socket_backend)
    dials: list[Dial] = field(default_factory=list[Dial])

    async def vet(self, host: str, port: int, resolve_timeout: float | None = None) -> list[str]:
        if port not in self.policy.allowed_ports:
            raise SSRFBlockedError(host, "", f"port {port} not allowed")
        if self.policy.allowed_hosts is not None and host not in self.policy.allowed_hosts:
            raise SSRFBlockedError(host, "", "host not in allow list")
        literal = parse_ip_literal(host)
        if literal is not None:
            candidates = [literal]
        else:
            try:
                with anyio.fail_after(resolve_timeout):
                    answers = await self.resolver.resolve(host, port)
            except TimeoutError as exc:
                raise httpcore.ConnectTimeout(f"resolving {host!r} timed out") from exc
            candidates = [ipaddress.ip_address(answer) for answer in answers]
        if not candidates:
            raise SSRFBlockedError(host, "", "no addresses")
        for candidate in candidates:  # one bad answer poisons the whole set
            reason = blocked_reason(candidate)
            if reason is not None:
                raise SSRFBlockedError(host, str(candidate), reason)
        return [candidate.compressed for candidate in candidates]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend signature
        local_address: str | None = None,
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self.vet(host, port, timeout)
        failures: list[httpcore.ConnectError] = []
        for address in addresses:  # sequential failover, only across vetted addresses
            try:
                stream = await self.inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except httpcore.ConnectError as exc:
                failures.append(exc)
                continue
            self.dials.append(Dial(host, address, port))
            return stream
        raise httpcore.ConnectError(f"all {len(addresses)} vetted addresses failed: {failures}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's backend signature
        socket_options: Iterable[SocketOption] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SSRFBlockedError(path, "", "unix sockets are never an egress target")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """AsyncHTTPTransport whose connection pool dials through a GuardedNetworkBackend.

    httpx 0.28.1 does not expose `network_backend`, so the pool built by the base
    class is replaced before first use; no proxy is ever configured on this transport.
    """

    def __init__(
        self,
        backend: GuardedNetworkBackend,
        *,
        verify: ssl.SSLContext | bool = True,
        limits: httpx.Limits = DEFAULT_LIMITS,
        http2: bool = False,
    ) -> None:
        super().__init__(verify=verify, limits=limits, http2=http2, trust_env=False)
        self.backend = backend
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify, trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=http2,
            network_backend=backend,
        )


def guarded_client(
    resolver: Resolver,
    policy: EgressPolicy | None = None,
    *,
    inner: httpcore.AsyncNetworkBackend | None = None,
    timeout: float = 5.0,
) -> httpx.AsyncClient:
    backend = GuardedNetworkBackend(resolver, policy or EgressPolicy(), inner or socket_backend())
    return httpx.AsyncClient(
        transport=GuardedTransport(backend),
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )


# --- The two defenses this note shows failing -------------------------------------

NAIVE_DENY_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "0.", "localhost", "[::1]")


def naive_string_check(url: str) -> bool:
    """A typical string blocklist: True means 'allowed'.  Bypassed by other encodings."""
    host = urlsplit(url).hostname or ""
    return not host.startswith(NAIVE_DENY_PREFIXES)


async def naive_preflight_is_safe(url: str, resolver: Resolver) -> bool:
    """Resolve and vet the URL's host now; the HTTP client will resolve it again later."""
    parsed = httpx.URL(url)
    literal = parse_ip_literal(parsed.host)
    if literal is not None:
        return blocked_reason(literal) is None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    answers = await resolver.resolve(parsed.host, port)
    return bool(answers) and all(blocked_reason(ipaddress.ip_address(a)) is None for a in answers)
