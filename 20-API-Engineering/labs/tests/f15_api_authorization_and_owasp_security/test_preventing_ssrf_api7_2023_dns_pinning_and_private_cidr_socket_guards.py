"""SSRF lab: string checks and pre-flight DNS checks fail; a connect-time guard holds.

No real socket is opened: `FakeNetwork` is an httpcore network backend that maps IP
literals to canned HTTP/1.1 responses, and `ScriptedResolver` plays an attacker's DNS
server with TTL 0, answering differently on each query.
"""

from __future__ import annotations

import ipaddress
import socket
from collections import deque
from typing import TYPE_CHECKING, Any

import anyio
import httpcore
import httpx
import pytest

from api_labs.f15_api_authorization_and_owasp_security.ssrf_guard import (
    Dial,
    EgressPolicy,
    GuardedNetworkBackend,
    GuardedTransport,
    SSRFBlockedError,
    SystemResolver,
    blocked_reason,
    guarded_client,
    naive_preflight_is_safe,
    naive_string_check,
    parse_ip_literal,
)

if TYPE_CHECKING:
    import ssl
    from collections.abc import Iterable

PUBLIC = "93.184.215.14"  # a globally routable address; nothing is ever dialed for real
PUBLIC_2 = "93.184.215.15"
METADATA = "169.254.169.254"
REBIND = "rebind.attacker.test"
SHOP = "shop.example.test"


def http_response(status: int, body: bytes, extra: str = "") -> bytes:
    reason = {200: "OK", 302: "Found"}[status]
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n{extra}\r\n"
    return head.encode("ascii") + body


ROUTES: dict[str, bytes] = {
    PUBLIC: http_response(200, b"public page"),
    PUBLIC_2: http_response(200, b"public page 2"),
    "127.0.0.1": http_response(200, b"internal admin console"),
    METADATA: http_response(200, b'{"AccessKeyId": "AKIAIOSFODNN7EXAMPLE"}'),
}


class FakeStream(httpcore.AsyncNetworkStream):
    """Answers each complete request head written to it with the same canned response."""

    def __init__(self, response: bytes) -> None:
        self._response = response
        self._served = 0
        self.written = bytearray()
        self.tls_server_hostname: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        if self._served < self.written.count(b"\r\n\r\n"):
            self._served += 1
            return self._response
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self.written += buffer

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self.tls_server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class FakeNetwork(httpcore.AsyncNetworkBackend):
    """Routes by IP literal only, as a kernel would after the name is resolved."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.opened: list[tuple[str, int, FakeStream]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host not in self.routes:
            raise httpcore.ConnectError(f"no route to host {host}")
        stream = FakeStream(self.routes[host])
        self.opened.append((host, port, stream))
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("no unix sockets in the fake network")

    async def sleep(self, seconds: float) -> None:
        return None

    def dialed(self) -> list[tuple[str, int]]:
        return [(host, port) for host, port, _ in self.opened]


class ScriptedResolver:
    """An attacker-controlled DNS zone with TTL 0: each query pops the next answer."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = {host: deque(seq) for host, seq in answers.items()}
        self.queries: list[str] = []

    async def resolve(self, host: str, port: int) -> list[str]:
        self.queries.append(host)
        queue = self._answers[host]
        return queue.popleft() if len(queue) > 1 else queue[0]


class UnguardedResolvingBackend(httpcore.AsyncNetworkBackend):
    """What anyio.connect_tcp does for a host name: resolve at dial time, no vetting."""

    def __init__(self, resolver: ScriptedResolver, inner: FakeNetwork) -> None:
        self.resolver = resolver
        self.inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        address = host if parse_ip_literal(host) else (await self.resolver.resolve(host, port))[0]
        return await self.inner.connect_tcp(address, port, timeout)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unused")

    async def sleep(self, seconds: float) -> None:
        return None


class UnguardedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, backend: httpcore.AsyncNetworkBackend) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(network_backend=backend)


def unguarded_client(
    resolver: ScriptedResolver, network: FakeNetwork, *, follow_redirects: bool
) -> httpx.AsyncClient:
    backend = UnguardedResolvingBackend(resolver, network)
    return httpx.AsyncClient(
        transport=UnguardedTransport(backend), follow_redirects=follow_redirects
    )


# --- 1. String validation -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "host", ["2130706433", "0x7f000001", "017700000001", "0x7f.0.0.1", "0x7f.1"]
)
def test_string_blocklist_passes_hosts_that_libc_parses_as_loopback(host: str) -> None:
    assert naive_string_check(f"http://{host}/admin") is True
    assert parse_ip_literal(host) is None  # ipaddress refuses the legacy inet_aton forms
    infos = socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    assert {info[4][0] for info in infos} == {"127.0.0.1"}


@pytest.mark.unit
def test_httpx_rejects_dotted_octal_but_passes_other_legacy_forms_to_the_resolver() -> None:
    with pytest.raises(httpx.InvalidURL, match="Invalid IPv4 address"):
        httpx.URL("http://0177.0.0.1/")
    assert httpx.URL("http://0x7f.1/").host == "0x7f.1"
    assert httpx.URL("http://2130706433/").host == "2130706433"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_system_resolver_turns_decimal_host_into_loopback_and_guard_blocks_it() -> None:
    network = FakeNetwork(ROUTES)
    async with guarded_client(SystemResolver(), inner=network) as client:
        with pytest.raises(SSRFBlockedError) as info:
            await client.get("http://2130706433/admin")
    assert (info.value.address, info.value.reason) == ("127.0.0.1", "loopback")
    assert network.opened == []


# --- 2. Pre-flight resolution (time of check != time of use) ---------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_check_is_bypassed_by_dns_rebinding() -> None:
    resolver = ScriptedResolver({REBIND: [[PUBLIC], ["127.0.0.1"]]})
    network = FakeNetwork(ROUTES)
    url = f"http://{REBIND}/"

    assert await naive_preflight_is_safe(url, resolver) is True
    async with unguarded_client(resolver, network, follow_redirects=False) as client:
        response = await client.get(url)

    assert response.text == "internal admin console"
    assert resolver.queries == [REBIND, REBIND]  # checked one answer, dialed another
    assert network.dialed() == [("127.0.0.1", 80)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preflight_check_is_bypassed_by_a_redirect_to_metadata() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC]]})
    redirect = http_response(302, b"", f"Location: http://{METADATA}/latest/meta-data/iam/\r\n")
    network = FakeNetwork({**ROUTES, PUBLIC: redirect})
    url = f"http://{SHOP}/avatar.png"

    assert await naive_preflight_is_safe(url, resolver) is True
    async with unguarded_client(resolver, network, follow_redirects=True) as client:
        response = await client.get(url)

    assert "AccessKeyId" in response.text
    assert network.dialed() == [(PUBLIC, 80), (METADATA, 80)]


# --- 3. The connect-time guard ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_resolves_once_and_dials_the_vetted_address() -> None:
    resolver = ScriptedResolver({REBIND: [[PUBLIC], ["127.0.0.1"]]})
    network = FakeNetwork(ROUTES)
    async with guarded_client(resolver, inner=network) as client:
        response = await client.get(f"http://{REBIND}/")
        again = await client.get(f"http://{REBIND}/")  # keep-alive reuse, no new lookup

    assert (response.text, again.status_code) == ("public page", 200)
    assert resolver.queries == [REBIND]
    assert network.dialed() == [(PUBLIC, 80)]
    written = bytes(network.opened[0][2].written)
    assert b"Host: rebind.attacker.test\r\n" in written


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_blocks_rebinding_when_the_dial_time_answer_is_internal() -> None:
    resolver = ScriptedResolver({REBIND: [["127.0.0.1"]]})
    network = FakeNetwork(ROUTES)
    async with guarded_client(resolver, inner=network) as client:
        with pytest.raises(SSRFBlockedError, match="loopback"):
            await client.get(f"http://{REBIND}/")
    assert network.opened == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_internal_answer_poisons_the_whole_answer_set() -> None:
    resolver = ScriptedResolver({REBIND: [[PUBLIC, "10.0.0.5"]]})
    network = FakeNetwork(ROUTES)
    async with guarded_client(resolver, inner=network) as client:
        with pytest.raises(SSRFBlockedError) as info:
            await client.get(f"http://{REBIND}/")
    assert (info.value.address, info.value.reason) == ("10.0.0.5", "private-use")
    assert network.opened == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guarded_client_returns_the_302_instead_of_following_it() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC]]})
    redirect = http_response(302, b"", f"Location: http://{METADATA}/latest/meta-data/\r\n")
    network = FakeNetwork({**ROUTES, PUBLIC: redirect})
    async with guarded_client(resolver, inner=network) as client:
        response = await client.get(f"http://{SHOP}/avatar.png")

    assert response.status_code == 302
    assert response.next_request is not None
    assert response.next_request.url.host == METADATA
    assert network.dialed() == [(PUBLIC, 80)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_guard_vets_every_redirect_hop_when_redirects_are_enabled() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC]]})
    redirect = http_response(302, b"", f"Location: http://{METADATA}/latest/meta-data/\r\n")
    network = FakeNetwork({**ROUTES, PUBLIC: redirect})
    async with guarded_client(resolver, inner=network) as client:
        with pytest.raises(SSRFBlockedError, match="link-local"):
            await client.get(f"http://{SHOP}/avatar.png", follow_redirects=True)
    assert network.dialed() == [(PUBLIC, 80)]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://[::ffff:169.254.169.254]/", "ipv4-mapped:link-local"),
        ("http://[::ffff:a9fe:a9fe]/", "ipv4-mapped:link-local"),
        ("http://[64:ff9b::7f00:1]/", "nat64:loopback"),
        ("http://[fd00:ec2::254]/", "unique-local"),
        ("http://0.0.0.0/", "this-network"),
    ],
)
async def test_ip_literals_are_vetted_without_asking_the_resolver(url: str, reason: str) -> None:
    resolver = ScriptedResolver({})
    network = FakeNetwork(ROUTES)
    async with guarded_client(resolver, inner=network) as client:
        with pytest.raises(SSRFBlockedError) as info:
            await client.get(url)
    assert info.value.reason == reason
    assert resolver.queries == []
    assert network.opened == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_https_dials_the_ip_but_keeps_the_host_name_for_sni_and_host() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC]]})
    network = FakeNetwork(ROUTES)
    async with guarded_client(resolver, inner=network) as client:
        response = await client.get(f"https://{SHOP}/")

    assert response.text == "public page"
    host, port, stream = network.opened[0]
    assert (host, port, stream.tls_server_hostname) == (PUBLIC, 443, SHOP)
    assert b"Host: shop.example.test\r\n" in bytes(stream.written)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failover_only_walks_vetted_addresses() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC, PUBLIC_2]]})
    network = FakeNetwork({PUBLIC_2: ROUTES[PUBLIC_2]})  # first address is unreachable
    backend = GuardedNetworkBackend(resolver, inner=network)
    stream = await backend.connect_tcp(SHOP, 80)

    assert isinstance(stream, FakeStream)
    assert backend.dials == [Dial(SHOP, PUBLIC_2, 80)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_vetted_addresses_failing_is_a_connect_error() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC, PUBLIC_2]]})
    async with guarded_client(resolver, inner=FakeNetwork({})) as client:
        with pytest.raises(httpx.ConnectError, match="all 2 vetted addresses failed"):
            await client.get(f"http://{SHOP}/")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_web_ports_are_blocked_before_resolution() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC]]})
    network = FakeNetwork(ROUTES)
    async with guarded_client(resolver, inner=network) as client:
        with pytest.raises(SSRFBlockedError, match="port 6379 not allowed"):
            await client.get(f"http://{SHOP}:6379/")
    assert resolver.queries == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_host_allow_list_rejects_unlisted_public_hosts() -> None:
    resolver = ScriptedResolver({SHOP: [[PUBLIC]], REBIND: [[PUBLIC]]})
    policy = EgressPolicy(allowed_hosts=frozenset({SHOP}))
    async with guarded_client(resolver, policy, inner=FakeNetwork(ROUTES)) as client:
        assert (await client.get(f"http://{SHOP}/")).status_code == 200
        with pytest.raises(SSRFBlockedError, match="allow list"):
            await client.get(f"http://{REBIND}/")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_answer_set_is_blocked() -> None:
    resolver = ScriptedResolver({SHOP: [[]]})
    async with guarded_client(resolver, inner=FakeNetwork(ROUTES)) as client:
        with pytest.raises(SSRFBlockedError, match="no addresses"):
            await client.get(f"http://{SHOP}/")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slow_resolution_is_bounded_by_the_connect_timeout() -> None:
    class HangingResolver:
        async def resolve(self, host: str, port: int) -> list[str]:
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

    async with guarded_client(HangingResolver(), inner=FakeNetwork(ROUTES), timeout=0.05) as c:
        with pytest.raises(httpx.ConnectTimeout, match="resolving"):
            await c.get(f"http://{SHOP}/")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unix_socket_targets_are_refused() -> None:
    backend = GuardedNetworkBackend(ScriptedResolver({}), inner=FakeNetwork(ROUTES))
    with pytest.raises(SSRFBlockedError, match="unix sockets"):
        await backend.connect_unix_socket("/var/run/docker.sock")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_transport_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.1:3128")
    resolver = ScriptedResolver({SHOP: [[PUBLIC]]})
    network = FakeNetwork(ROUTES)
    transport = GuardedTransport(GuardedNetworkBackend(resolver, inner=network))
    async with httpx.AsyncClient(transport=transport, trust_env=True) as client:
        response = await client.get(f"http://{SHOP}/")
    assert response.text == "public page"
    assert network.dialed() == [(PUBLIC, 80)]


# --- 4. The address classifier -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private-use"),
        ("172.31.255.255", "private-use"),
        ("192.168.1.1", "private-use"),
        ("169.254.169.254", "link-local"),
        ("169.254.170.2", "link-local"),
        ("100.64.0.1", "shared-address-space"),
        ("0.0.0.0", "this-network"),  # noqa: S104 - an address under test, not a bind
        ("198.18.0.1", "benchmarking"),
        ("224.0.0.1", "multicast"),
        ("255.255.255.255", "reserved"),
        ("::1", "ipv4-compatible"),
        ("::", "ipv4-compatible"),
        ("::127.0.0.1", "ipv4-compatible"),
        ("::ffff:127.0.0.1", "ipv4-mapped:loopback"),
        ("64:ff9b::a9fe:a9fe", "nat64:link-local"),
        ("2002:a9fe:a9fe::", "6to4:link-local"),
        ("64:ff9b:1::1", "local-use-translation"),
        ("fd00:ec2::254", "unique-local"),
        ("fe80::1", "link-local"),
        ("ff02::1", "multicast"),
        ("5f00::1", "srv6-sids"),
        ("100:0:0:1::1", "dummy-prefix"),
        ("2001:db8::1", "documentation"),
    ],
)
def test_special_purpose_addresses_are_blocked(address: str, reason: str) -> None:
    assert blocked_reason(ipaddress.ip_address(address)) == reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111", "64:ff9b::5db8:d70e", "::ffff:8.8.8.8"]
)
def test_public_unicast_addresses_are_allowed(address: str) -> None:
    assert blocked_reason(ipaddress.ip_address(address)) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "address", ["64:ff9b::7f00:1", "::127.0.0.1", "5f00::1", "100:0:0:1::1", "224.0.0.1"]
)
def test_stdlib_is_global_says_true_where_the_guard_blocks(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert ip.is_global is True
    assert blocked_reason(ip) is not None
