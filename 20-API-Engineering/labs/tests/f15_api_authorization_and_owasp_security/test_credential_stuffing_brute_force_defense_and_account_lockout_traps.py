"""Lab: throttle guessing on a login endpoint without giving attackers a lockout switch."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from argon2 import PasswordHasher
from slowapi.util import get_ipaddr, get_remote_address
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from api_labs.f15_api_authorization_and_owasp_security.login_throttle import (
    DEVICE_COOKIE,
    UNPARSEABLE,
    Argon2Verifier,
    LoginGuard,
    NaiveAccountLockout,
    Throttle,
    ThrottlePolicy,
    backoff_delay,
    create_app,
    issue_device_cookie,
    network_bucket,
    normalize_username,
    resolve_client,
    verify_device_cookie,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Receive, Scope, Send

SECRET = b"lab-device-cookie-key-EXAMPLE-NOT-A-REAL-KEY"
ALICE_PASSWORD = "correct horse battery staple"  # noqa: S105 - lab fixture
MALLORY_PASSWORD = "mallory owns this account"  # noqa: S105 - lab fixture
FULLWIDTH_ALICE = chr(0xFF21) + "lice"  # NFKC folds U+FF21 to "A"
ALICE_HOME = "198.51.100.10"


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingVerifier(Argon2Verifier):
    """Real Argon2id with lab-sized parameters that records every verification."""

    def __init__(self) -> None:
        super().__init__(PasswordHasher(time_cost=1, memory_cost=64, parallelism=1))
        self.calls: list[str] = []

    def verify(self, stored_hash: str, password: str) -> bool:
        self.calls.append(stored_hash)
        return super().verify(stored_hash, password)


class Lab:
    def __init__(self, policy: ThrottlePolicy | None = None, *, naive: bool = False) -> None:
        self.clock = FakeClock()
        self.verifier = CountingVerifier()
        self.guard = LoginGuard(policy or ThrottlePolicy(), self.clock)
        throttle: Throttle = NaiveAccountLockout(self.clock) if naive else self.guard
        self.users = {
            "alice": self.verifier.hash(ALICE_PASSWORD),
            "mallory": self.verifier.hash(MALLORY_PASSWORD),
        }
        self.app: FastAPI = create_app(
            throttle=throttle,
            users=self.users,
            verifier=self.verifier,
            device_secret=SECRET,
            wall_clock=self.clock,
            trusted_proxies=["10.0.0.0/8"],
        )

    async def login(
        self,
        username: str,
        password: str,
        ip: str,
        *,
        cookie: str | None = None,
        forwarded_for: str | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if cookie is not None:
            headers["Cookie"] = f"{DEVICE_COOKIE}={cookie}"
        if forwarded_for is not None:
            headers["X-Forwarded-For"] = forwarded_for
        transport = httpx.ASGITransport(app=self.app, client=(ip, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"username": username, "password": password}
            return await client.post("/login", json=body, headers=headers)


def botnet(count: int) -> list[str]:
    base = int(ipaddress.IPv4Address("100.64.0.1"))
    return [str(ipaddress.IPv4Address(base + i)) for i in range(count)]


def device_value(response: httpx.Response) -> str:
    set_cookie = response.headers["set-cookie"]
    return set_cookie.split(";")[0].split("=", 1)[1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_lockout_hands_the_attacker_a_denial_of_service() -> None:
    lab = Lab(naive=True)
    for ip in botnet(5):
        assert (await lab.login("alice", "guess", ip)).status_code == 400
    victim = await lab.login("alice", ALICE_PASSWORD, ALICE_HOME)
    assert victim.status_code == 429  # the right password, refused: the attacker owns the lock
    assert len(lab.verifier.calls) == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trusted_device_logs_in_during_a_distributed_attack() -> None:
    lab = Lab()
    first = await lab.login("alice", ALICE_PASSWORD, ALICE_HOME)
    assert first.status_code == 200
    cookie = device_value(first)
    statuses = [(await lab.login("alice", "guess", ip)).status_code for ip in botnet(25)]
    assert statuses == [400] * 10 + [429] * 15
    on_the_road = await lab.login("alice", ALICE_PASSWORD, "192.0.2.50", cookie=cookie)
    assert on_the_road.status_code == 200
    new_laptop = await lab.login("alice", ALICE_PASSWORD, "192.0.2.51")
    assert new_laptop.status_code == 429  # untrusted clients stay locked for this account


@pytest.mark.unit
@pytest.mark.asyncio
async def test_known_network_logs_in_without_a_cookie_during_the_attack() -> None:
    lab = Lab()
    assert (await lab.login("alice", ALICE_PASSWORD, ALICE_HOME)).status_code == 200
    for ip in botnet(12):
        await lab.login("alice", "guess", ip)
    assert (await lab.login("alice", ALICE_PASSWORD, "192.0.2.77")).status_code == 429
    assert (await lab.login("alice", ALICE_PASSWORD, ALICE_HOME)).status_code == 200


@pytest.mark.unit
def test_untrusted_lane_caps_botnet_guesses_per_account_per_window() -> None:
    clock = FakeClock()
    guard = LoginGuard(ThrottlePolicy(), clock)
    admitted = [guard.admit("alice", ip, None) for ip in botnet(1000)]
    allowed = [a for a in admitted if a.allowed]
    assert len(allowed) == 10
    assert {a.blocked_by for a in admitted if not a.allowed} == {("user",)}
    assert admitted[-1].retry_after == pytest.approx(3600.0)
    clock.advance(3600.0)
    assert guard.admit("alice", "100.127.0.1", None).allowed


@pytest.mark.unit
def test_pair_backoff_doubles_from_one_second_and_caps() -> None:
    policy = ThrottlePolicy(ip_max_failures=10_000, user_max_failures=10_000)
    clock = FakeClock()
    guard = LoginGuard(policy, clock)
    waits: list[float] = []
    while len(waits) < 13:
        admission = guard.admit("alice", "203.0.113.9", None)
        if not admission.allowed:
            assert admission.blocked_by == ("pair",)
            waits.append(admission.retry_after)
            clock.advance(admission.retry_after)
    assert waits == [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 900, 900, 900]
    assert backoff_delay(10_000, policy) == 900.0  # no OverflowError from 2.0 ** 9997


@pytest.mark.unit
def test_network_budget_blocks_stuffing_across_usernames() -> None:
    guard = LoginGuard(ThrottlePolicy(), FakeClock())
    results = [guard.admit(f"user{i}", "203.0.113.9", None) for i in range(31)]
    assert [r.allowed for r in results] == [True] * 30 + [False]
    assert results[-1].blocked_by == ("ip",)
    assert guard.admit("user99", "203.0.113.10", None).allowed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_logging_in_to_your_own_account_does_not_reset_the_network_budget() -> None:
    lab = Lab()
    attacker = "203.0.113.9"
    for i in range(29):
        assert (await lab.login(f"victim{i}", "guess", attacker)).status_code == 400
    assert (await lab.login("mallory", MALLORY_PASSWORD, attacker)).status_code == 200
    assert (await lab.login("victim29", "guess", attacker)).status_code == 400
    assert (await lab.login("victim30", "guess", attacker)).status_code == 429


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_victims_own_login_does_not_reopen_the_untrusted_window() -> None:
    lab = Lab()
    for ip in botnet(9):
        await lab.login("alice", "guess", ip)
    assert (await lab.login("alice", ALICE_PASSWORD, ALICE_HOME)).status_code == 200
    statuses = [(await lab.login("alice", "guess", ip)).status_code for ip in botnet(11)]
    assert statuses == [400] + [429] * 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_throttled_attempt_never_reaches_the_hash() -> None:
    lab = Lab()
    for ip in botnet(10):
        await lab.login("alice", "guess", ip)
    hashed = len(lab.verifier.calls)
    right = await lab.login("alice", ALICE_PASSWORD, "192.0.2.99")
    wrong = await lab.login("alice", "guess", "192.0.2.98")
    assert right.status_code == wrong.status_code == 429
    assert right.content == wrong.content  # no oracle while locked
    assert len(lab.verifier.calls) == hashed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_user_and_wrong_password_are_identical_and_cost_one_hash() -> None:
    lab = Lab()
    wrong = await lab.login("alice", "guess", "203.0.113.1")
    unknown = await lab.login("nobody", "guess", "203.0.113.2")
    assert wrong.status_code == unknown.status_code == 400
    assert wrong.content == unknown.content
    assert dict(wrong.headers) == dict(unknown.headers)
    alice_hash, dummy_hash = lab.verifier.calls
    assert alice_hash == lab.users["alice"]
    assert dummy_hash != alice_hash
    assert dummy_hash.split("$")[:4] == alice_hash.split("$")[:4]  # same algorithm and cost


@pytest.mark.unit
def test_argon2_verifier_treats_a_corrupt_hash_as_a_mismatch() -> None:
    verifier = CountingVerifier()
    assert verifier.verify(verifier.hash("pw"), "pw")
    assert not verifier.verify(verifier.hash("pw"), "other")
    assert not verifier.verify("$argon2id$garbage", "pw")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_concurrent_burst_cannot_outrun_the_counter() -> None:
    lab = Lab()
    burst = [lab.login("alice", f"guess{i}", "203.0.113.9") for i in range(20)]
    statuses = sorted(r.status_code for r in await asyncio.gather(*burst))
    assert statuses == [400] * 3 + [429] * 17
    assert len(lab.verifier.calls) == 3


@pytest.mark.unit
def test_resolve_client_takes_the_rightmost_untrusted_hop() -> None:
    proxies = [ipaddress.ip_network("10.0.0.0/8")]
    assert resolve_client("203.0.113.9", ["6.6.6.6"], proxies) == "203.0.113.9"
    assert resolve_client("10.0.0.2", ["6.6.6.6, 198.51.100.7"], proxies) == "198.51.100.7"
    chain = ["6.6.6.6, 198.51.100.7", "10.0.0.3"]
    assert resolve_client("10.0.0.2", chain, proxies) == "198.51.100.7"
    assert resolve_client("10.0.0.2", [], proxies) == "10.0.0.2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_spoofed_forwarded_for_cannot_rotate_the_network_bucket() -> None:
    lab = Lab()
    statuses = [
        (
            await lab.login(
                f"victim{i}", "guess", "10.0.0.2", forwarded_for=f"6.6.{i}.6, 198.51.100.7"
            )
        ).status_code
        for i in range(31)
    ]
    assert statuses == [400] * 30 + [429]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uvicorn_proxy_headers_with_star_trusts_the_leftmost_spoofed_hop() -> None:
    async def echo_client(scope: Scope, receive: Receive, send: Send) -> None:
        host = scope["client"][0].encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": host})

    seen: dict[str, str] = {}
    for trusted in ("*", "127.0.0.1"):
        middleware = ProxyHeadersMiddleware(echo_client, trusted_hosts=trusted)  # type: ignore[arg-type]
        app = cast("ASGIApp", middleware)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
        seen[trusted] = response.text
    assert seen == {"*": "6.6.6.6", "127.0.0.1": "198.51.100.7"}


@pytest.mark.unit
def test_slowapi_get_ipaddr_returns_an_underscore_header_verbatim() -> None:
    def request(name: bytes) -> Request:
        scope: Scope = {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(name, b"6.6.6.6")],
            "client": ("198.51.100.7", 40000),
        }
        return Request(scope)

    assert get_ipaddr(request(b"x_forwarded_for")) == "6.6.6.6"
    assert get_ipaddr(request(b"x-forwarded-for")) == "198.51.100.7"
    assert get_remote_address(request(b"x_forwarded_for")) == "198.51.100.7"


@pytest.mark.unit
def test_network_bucket_groups_ipv6_by_64_and_unwraps_ipv4_mapped() -> None:
    assert network_bucket("2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert network_bucket("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert network_bucket("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert network_bucket("::ffff:198.51.100.7") == "198.51.100.7"
    assert network_bucket("::ffff:198.51.100.8") == "198.51.100.8"
    assert network_bucket("198.51.100.7:443") == UNPARSEABLE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_username_spellings_share_one_bucket() -> None:
    assert {normalize_username(n) for n in ("Alice", " ALICE ", FULLWIDTH_ALICE)} == {"alice"}
    lab = Lab()
    spellings = ["Alice", "ALICE", "alice ", FULLWIDTH_ALICE, "aLiCe"] * 3
    ips = botnet(len(spellings))
    pairs = zip(spellings, ips, strict=True)
    statuses = [(await lab.login(name, "guess", ip)).status_code for name, ip in pairs]
    assert statuses == [400] * 10 + [429] * 5


@pytest.mark.unit
def test_device_cookie_rejects_forgery_reuse_and_expiry() -> None:
    now, ttl = 1_800_000_000.0, 86_400.0
    value = issue_device_cookie("alice", SECRET, now=now)
    nonce = value.split(".")[1]
    assert verify_device_cookie(value, "alice", SECRET, now=now, ttl=ttl) == nonce
    issued, _, mac = value.split(".")
    forged = f"{issued}.{nonce}.{'0' * len(mac)}"
    assert verify_device_cookie(forged, "alice", SECRET, now=now, ttl=ttl) is None
    assert verify_device_cookie(value, "bob", SECRET, now=now, ttl=ttl) is None
    assert verify_device_cookie(value, "alice", b"other-key", now=now, ttl=ttl) is None
    assert verify_device_cookie(value, "alice", SECRET, now=now + ttl + 1, ttl=ttl) is None
    assert verify_device_cookie(value, "alice", SECRET, now=now - 1, ttl=ttl) is None
    assert verify_device_cookie("a.b", "alice", SECRET, now=now, ttl=ttl) is None


@pytest.mark.unit
def test_a_locked_device_cookie_falls_back_to_the_untrusted_lane() -> None:
    guard = LoginGuard(ThrottlePolicy(), FakeClock())
    lanes = [guard.admit("alice", f"192.0.2.{i}", "nonce-1").lane for i in range(12)]
    assert lanes == ["device"] * 10 + ["untrusted"] * 2


@pytest.mark.unit
def test_tables_stay_bounded_under_username_spraying() -> None:
    policy = ThrottlePolicy(max_keys=1000, ip_max_failures=10_000)
    guard = LoginGuard(policy, FakeClock())
    for i, ip in enumerate(botnet(5000)):
        guard.admit(f"sprayed{i}", ip, None)
    assert len(guard.users) == len(guard.pairs) == len(guard.ips) == 1000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_429_carries_retry_after_and_no_store_and_never_a_cookie() -> None:
    lab = Lab()
    for _ in range(3):
        await lab.login("alice", "guess", "203.0.113.9")
    throttled = await lab.login("alice", "guess", "203.0.113.9")
    assert throttled.status_code == 429
    assert throttled.headers["retry-after"] == "1"
    assert throttled.headers["cache-control"] == "no-store"
    assert "set-cookie" not in throttled.headers
    assert throttled.json() == {"error": "too_many_attempts"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_sets_a_host_prefixed_hardened_device_cookie() -> None:
    lab = Lab()
    response = await lab.login("alice", ALICE_PASSWORD, ALICE_HOME)
    attributes = [part.strip() for part in response.headers["set-cookie"].split(";")]
    assert attributes[0].startswith(f"{DEVICE_COOKIE}=")
    assert {"HttpOnly", "Path=/", "SameSite=strict", "Secure"} <= set(attributes)
    assert not any(a.lower().startswith("domain=") for a in attributes)
    assert response.headers["cache-control"] == "no-store"
