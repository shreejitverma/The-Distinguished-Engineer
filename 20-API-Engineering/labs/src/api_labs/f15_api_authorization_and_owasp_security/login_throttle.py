"""Login throttling that stops online guessing without handing attackers a lockout switch.

Three lanes decide whether a login attempt may reach the password hash:

- ``device``: a valid, unlocked device cookie for this username (OWASP device-cookie protocol);
  only that cookie's own failure budget applies.
- ``known-ip``: the client network already logged in to this username; only the
  per-(username, network) exponential backoff applies.
- ``untrusted``: everything else; the per-network failure budget, the per-username
  untrusted-client budget, and the per-(username, network) backoff all apply.

Every admitted attempt is charged as a failure *before* the password is verified and refunded
on success, so a concurrent burst cannot slip past the counters while the hash is computing.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import itertools
import math
import secrets
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from argon2 import PasswordHasher

DEVICE_COOKIE = "__Host-device"
UNPARSEABLE = "unparseable"
INVALID_CREDENTIALS = {
    "error": "invalid_grant",
    "error_description": "invalid username or password",
}
TOO_MANY_ATTEMPTS = {"error": "too_many_attempts"}

type Lane = Literal["device", "known-ip", "untrusted"]
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network
type Log = list[tuple[float, int]]  # (charged at, attempt id), oldest first


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    ip_max_failures: int = 30  # per client network, all usernames
    ip_window: float = 600.0
    user_max_failures: int = 10  # per username, untrusted clients only (N per T)
    user_window: float = 3600.0
    pair_free_failures: int = 3  # per (username, network) before backoff starts
    pair_base_delay: float = 1.0
    pair_max_delay: float = 900.0
    pair_ttl: float = 86_400.0
    device_max_failures: int = 10
    device_window: float = 3600.0
    known_ip_ttl: float = 30 * 86_400.0
    max_keys: int = 100_000  # per table; bounds memory under username spraying


@dataclass(frozen=True, slots=True)
class Admission:
    allowed: bool
    retry_after: float
    lane: Lane
    blocked_by: tuple[str, ...]
    attempt_id: int
    user: str
    client: str
    device: str | None


class Throttle(Protocol):
    def admit(self, user: str, client: str, device: str | None) -> Admission: ...
    def settle_failure(self, admission: Admission) -> None: ...
    def settle_success(self, admission: Admission) -> None: ...


def normalize_username(raw: str) -> str:
    """NFKC then casefold, so look-alike spellings share one throttle bucket."""
    return unicodedata.normalize("NFKC", raw).casefold().strip()


def network_bucket(host: str) -> str:
    """IPv4 per address, IPv6 per /64, IPv4-mapped IPv6 as the IPv4 address it carries."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return UNPARSEABLE
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(ip) >> 64 << 64, 64)))
    return str(ip)


def _trusted(host: str, proxies: Sequence[Network]) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in proxies)


def resolve_client(
    peer: str | None, forwarded_for: Sequence[str], proxies: Sequence[Network]
) -> str:
    """Rightmost untrusted hop: each trusted proxy appends the address it saw on the right."""
    hops = [hop.strip() for value in forwarded_for for hop in value.split(",")]
    chain = [*hops, peer or ""]
    if not _trusted(chain[-1], proxies):
        return network_bucket(chain[-1])  # direct client: X-Forwarded-For is attacker text
    for hop in reversed(chain):
        if not _trusted(hop, proxies):
            return network_bucket(hop)
    return network_bucket(chain[0])  # every hop is ours: the request came from inside


def backoff_delay(failures: int, policy: ThrottlePolicy) -> float:
    """0 for the free failures, then base * 2**k capped; k is clamped so floats never overflow."""
    if failures < policy.pair_free_failures:
        return 0.0
    exponent = min(failures - policy.pair_free_failures, 32)
    return min(policy.pair_base_delay * 2.0**exponent, policy.pair_max_delay)


class _LruTable[V]:
    """Size-bounded map; evicting the least recently used key forgets its counters."""

    def __init__(self, max_keys: int) -> None:
        self._data: OrderedDict[str, V] = OrderedDict()
        self._max_keys = max_keys

    def get(self, key: str) -> V | None:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, key: str, value: V) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max_keys:
            self._data.popitem(last=False)

    def pop(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


class LoginGuard:
    """Charge-then-verify throttle with device, known-network, and untrusted lanes."""

    def __init__(self, policy: ThrottlePolicy, clock: Callable[[], float]) -> None:
        self.policy = policy
        self._clock = clock
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.ips: _LruTable[Log] = _LruTable(policy.max_keys)
        self.users: _LruTable[Log] = _LruTable(policy.max_keys)
        self.devices: _LruTable[Log] = _LruTable(policy.max_keys)
        self.pairs: _LruTable[tuple[int, float]] = _LruTable(policy.max_keys)
        self.known: _LruTable[float] = _LruTable(policy.max_keys)

    @staticmethod
    def _live(table: _LruTable[Log], key: str, now: float, window: float) -> Log:
        return [entry for entry in table.get(key) or [] if entry[0] > now - window]

    def admit(self, user: str, client: str, device: str | None) -> Admission:
        p = self.policy
        with self._lock:
            now = self._clock()
            attempt = next(self._ids)
            if device is not None:
                log = self._live(self.devices, device, now, p.device_window)
                if len(log) < p.device_max_failures:
                    self.devices.put(device, [*log, (now, attempt)])
                    return Admission(True, 0.0, "device", (), attempt, user, client, device)
                # A locked device cookie counts as no cookie at all.
            pair_key = f"{user}\x00{client}"
            known_until = self.known.get(pair_key)
            is_known = known_until is not None and known_until > now
            lane: Lane = "known-ip" if is_known else "untrusted"
            count, last = self.pairs.get(pair_key) or (0, -math.inf)
            if now - last > p.pair_ttl:
                count = 0
            waits: dict[str, float] = {}
            pair_wait = last + backoff_delay(count, p) - now
            if count >= p.pair_free_failures and pair_wait > 0:
                waits["pair"] = pair_wait
            ip_log = self._live(self.ips, client, now, p.ip_window)
            user_log = self._live(self.users, user, now, p.user_window)
            if lane == "untrusted":
                if len(ip_log) >= p.ip_max_failures:
                    waits["ip"] = ip_log[0][0] + p.ip_window - now
                if len(user_log) >= p.user_max_failures:
                    waits["user"] = user_log[0][0] + p.user_window - now
            if waits:
                blocked = tuple(sorted(waits))
                return Admission(
                    False, max(waits.values()), lane, blocked, attempt, user, client, None
                )
            self.pairs.put(pair_key, (count + 1, now))
            if lane == "untrusted":
                self.ips.put(client, [*ip_log, (now, attempt)])
                self.users.put(user, [*user_log, (now, attempt)])
            return Admission(True, 0.0, lane, (), attempt, user, client, None)

    def settle_failure(self, admission: Admission) -> None:
        """Nothing to do: the failure was charged when the attempt was admitted."""

    def settle_success(self, admission: Admission) -> None:
        """Refund only this attempt's own charges; reset the backoff of the client that succeeded.

        The network and untrusted-account windows keep every *other* charge: logging in to an
        account you own must not reset the budget you are spending on someone else's account,
        and the victim's own login must not hand a botnet a fresh window.
        """
        with self._lock:
            now = self._clock()
            pair_key = f"{admission.user}\x00{admission.client}"
            self.pairs.pop(pair_key)
            if admission.device is not None:
                self.devices.pop(admission.device)
            for table, key in ((self.ips, admission.client), (self.users, admission.user)):
                log = table.get(key)
                if log is not None:
                    table.put(key, [e for e in log if e[1] != admission.attempt_id])
            self.known.put(pair_key, now + self.policy.known_ip_ttl)


class NaiveAccountLockout:
    """The anti-pattern: N failures for a username lock it for every client."""

    def __init__(self, clock: Callable[[], float], max_failures: int = 5, lockout: float = 900.0):
        self._clock = clock
        self._max_failures = max_failures
        self._lockout = lockout
        self._ids = itertools.count(1)
        self.failures: dict[str, int] = {}
        self.locked_until: dict[str, float] = {}

    def admit(self, user: str, client: str, device: str | None) -> Admission:
        wait = self.locked_until.get(user, 0.0) - self._clock()
        blocked = ("account",) if wait > 0 else ()
        return Admission(
            not blocked, max(wait, 0.0), "untrusted", blocked, next(self._ids), user, client, None
        )

    def settle_failure(self, admission: Admission) -> None:
        count = self.failures.get(admission.user, 0) + 1
        self.failures[admission.user] = count
        if count >= self._max_failures:
            self.locked_until[admission.user] = self._clock() + self._lockout
            self.failures[admission.user] = 0

    def settle_success(self, admission: Admission) -> None:
        self.failures.pop(admission.user, None)


def _cookie_mac(secret: bytes, user: str, issued: int, nonce: str) -> str:
    message = f"device-cookie-v1|{len(user)}|{user}|{issued}|{nonce}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def issue_device_cookie(user: str, secret: bytes, *, now: float) -> str:
    issued, nonce = int(now), secrets.token_urlsafe(16)
    return f"{issued}.{nonce}.{_cookie_mac(secret, user, issued, nonce)}"


def verify_device_cookie(
    value: str | None, user: str, secret: bytes, *, now: float, ttl: float
) -> str | None:
    """Return the cookie's nonce if it was issued by us, to this username, within *ttl*."""
    if value is None or value.count(".") != 2:
        return None
    issued_text, nonce, mac = value.split(".")
    if not issued_text.isdigit() or len(issued_text) > 12 or not 0 <= now - int(issued_text) <= ttl:
        return None
    if not hmac.compare_digest(mac, _cookie_mac(secret, user, int(issued_text), nonce)):
        return None
    return nonce


class Verifier(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, stored_hash: str, password: str) -> bool: ...


class Argon2Verifier:
    """argon2-cffi behind a boolean interface; a mismatch and a corrupt hash are both False."""

    def __init__(self, hasher: PasswordHasher) -> None:
        self.hasher = hasher

    def hash(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        try:
            return self.hasher.verify(stored_hash, password)
        except (VerificationError, InvalidHashError):
            return False


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


def create_app(
    *,
    throttle: Throttle,
    users: Mapping[str, str],
    verifier: Verifier,
    device_secret: bytes,
    wall_clock: Callable[[], float],
    trusted_proxies: Sequence[str] = (),
    device_ttl: float = 365 * 86_400.0,
) -> FastAPI:
    """``users`` maps normalized usernames to password hashes made by ``verifier``."""
    proxies: tuple[Network, ...] = tuple(ipaddress.ip_network(net) for net in trusted_proxies)
    dummy_hash = verifier.hash(secrets.token_urlsafe(32))  # same parameters as real hashes
    app = FastAPI()

    @app.post("/login")
    async def login(body: LoginRequest, request: Request) -> Response:
        user = normalize_username(body.username)
        peer = request.client.host if request.client else None
        client = resolve_client(peer, request.headers.getlist("x-forwarded-for"), proxies)
        cookie = request.cookies.get(DEVICE_COOKIE)
        device = verify_device_cookie(cookie, user, device_secret, now=wall_clock(), ttl=device_ttl)
        admission = throttle.admit(user, client, device)
        if not admission.allowed:
            retry = str(max(1, math.ceil(admission.retry_after)))
            headers = {"Retry-After": retry, "Cache-Control": "no-store"}
            return JSONResponse(TOO_MANY_ATTEMPTS, status_code=429, headers=headers)
        stored = users.get(user)
        matched = await run_in_threadpool(verifier.verify, stored or dummy_hash, body.password)
        if stored is None or not matched:
            throttle.settle_failure(admission)
            return JSONResponse(
                INVALID_CREDENTIALS, status_code=400, headers={"Cache-Control": "no-store"}
            )
        throttle.settle_success(admission)
        response = JSONResponse({"authenticated": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            DEVICE_COOKIE,
            issue_device_cookie(user, device_secret, now=wall_clock()),
            max_age=int(device_ttl),
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    return app
