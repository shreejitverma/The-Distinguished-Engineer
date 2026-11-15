"""Production API keys: prefixed CSPRNG secrets with a CRC32 checksum, HMAC-SHA256 lookup
digests under versioned peppers, constant-time comparison, and dual-key rotation with a grace
window, served through FastAPI as RFC 6750 bearer credentials."""

import hashlib
import hmac
import math
import re
import secrets
import threading
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Final, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

type Environment = Literal["live", "test"]
type Reason = Literal["malformed", "wrong_environment", "unknown", "expired", "revoked"]

BRAND: Final = "acme"
BASE62: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SECRET_BYTES: Final = 32  # 256 bits from the operating system CSPRNG
SECRET_CHARS: Final = 43  # ceil(256 / log2(62)): the fewest base62 digits that hold 2**256 - 1
CHECKSUM_CHARS: Final = 6  # ceil(32 / log2(62)): the fewest base62 digits that hold a CRC32
MAX_LIVE_KEYS: Final = 2  # one current key plus one key draining in its grace window
# fullmatch, not match with "$": "$" also matches before a trailing newline.
KEY_PATTERN: Final = re.compile(
    rf"{BRAND}_(?P<env>live|test)_(?P<secret>[0-9A-Za-z]{{{SECRET_CHARS}}})"
    rf"(?P<checksum>[0-9A-Za-z]{{{CHECKSUM_CHARS}}})"
)
# What a secret scanner greps for: the prefix makes the pattern specific, the word
# boundaries stop it matching inside a longer token, and the checksum filters lookalikes.
SCAN_PATTERN: Final = re.compile(
    rf"\b{BRAND}_(?:live|test)_[0-9A-Za-z]{{{SECRET_CHARS + CHECKSUM_CHARS}}}\b"
)


def base62_encode(value: int, width: int) -> str:
    """Fixed-width big-endian base62, so every key has the same length and shape."""
    if not 0 <= value < 62**width:
        raise ValueError(f"{value} does not fit in {width} base62 digits")
    digits: list[str] = []
    for _ in range(width):
        value, digit = divmod(value, 62)
        digits.append(BASE62[digit])
    return "".join(reversed(digits))


def checksum(payload: str) -> str:
    """CRC32 of the prefix and secret, as 6 base62 digits (the GitHub token-format design)."""
    return base62_encode(zlib.crc32(payload.encode("ascii")), CHECKSUM_CHARS)


def entropy_bits(alphabet_size: int, length: int) -> float:
    """Bits of entropy in `length` symbols drawn uniformly from `alphabet_size` symbols."""
    return length * math.log2(alphabet_size)


def generate_api_key(
    env: Environment, token_bytes: Callable[[int], bytes] = secrets.token_bytes
) -> str:
    """`acme_<env>_` + 43 base62 digits of 32 CSPRNG bytes + 6 base62 digits of CRC32."""
    secret = base62_encode(int.from_bytes(token_bytes(SECRET_BYTES), "big"), SECRET_CHARS)
    payload = f"{BRAND}_{env}_{secret}"
    return payload + checksum(payload)


@dataclass(frozen=True, slots=True)
class ParsedKey:
    env: Environment
    secret: str


def parse_api_key(presented: str) -> ParsedKey | None:
    """Offline validation: shape and checksum, with no database round trip."""
    match = KEY_PATTERN.fullmatch(presented)
    if match is None:
        return None
    payload = presented[:-CHECKSUM_CHARS]
    if not hmac.compare_digest(checksum(payload), match["checksum"]):
        return None
    env: Environment = "live" if match["env"] == "live" else "test"
    return ParsedKey(env=env, secret=match["secret"])


def display_hint(api_key: str) -> str:
    """What a dashboard or log may show: the prefix, 4 secret digits, and the last 4 digits."""
    prefix_len = len(BRAND) + len("_live_")
    return f"{api_key[: prefix_len + 4]}...{api_key[-4:]}"


def find_leaked_keys(text: str) -> list[str]:
    """Secret-scanner core: regex candidates filtered by the checksum (2**-32 false accepts)."""
    return [m.group() for m in SCAN_PATTERN.finditer(text) if parse_api_key(m.group())]


def redact(text: str) -> str:
    """Replace every well-formed key in `text` with its display hint before it is logged."""
    return SCAN_PATTERN.sub(lambda m: display_hint(m.group()), text)


def lookup_digest(api_key: str, pepper: bytes) -> bytes:
    """HMAC-SHA256 under a server-side pepper: the only form of the key the database holds."""
    return hmac.new(pepper, api_key.encode("ascii"), hashlib.sha256).digest()


@dataclass(slots=True)
class KeyRecord:
    key_id: str
    owner: str
    env: Environment
    digest: bytes
    pepper_id: int
    hint: str
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    def usable_at(self, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or now < self.expires_at


@dataclass(frozen=True, slots=True)
class Verification:
    record: KeyRecord | None
    reason: Reason | None
    hmac_calls: int


class TooManyKeysError(Exception):
    """Raised when a rotation would leave an owner with more than `MAX_LIVE_KEYS` usable keys."""


@dataclass
class KeyStore:
    """In-memory model of an `api_keys` table with a unique index on `digest`."""

    peppers: dict[int, bytes]
    current_pepper: int
    by_digest: dict[bytes, KeyRecord] = field(default_factory=dict[bytes, KeyRecord])
    by_id: dict[str, KeyRecord] = field(default_factory=dict[str, KeyRecord])
    index_lookups: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def issue(
        self,
        owner: str,
        env: Environment,
        now: datetime,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> tuple[str, KeyRecord]:
        """Mint a key; the plaintext is returned exactly once and never stored."""
        api_key = generate_api_key(env, token_bytes)
        record = KeyRecord(
            key_id=f"key_{secrets.token_hex(8)}",
            owner=owner,
            env=env,
            digest=lookup_digest(api_key, self.peppers[self.current_pepper]),
            pepper_id=self.current_pepper,
            hint=display_hint(api_key),
            created_at=now,
        )
        with self._lock:
            self.by_digest[record.digest] = record
            self.by_id[record.key_id] = record
        return api_key, record

    def _peppers_newest_first(self) -> Iterator[tuple[int, bytes]]:
        yield self.current_pepper, self.peppers[self.current_pepper]
        for pepper_id in sorted(self.peppers, reverse=True):
            if pepper_id != self.current_pepper:
                yield pepper_id, self.peppers[pepper_id]

    def verify(self, presented: str, now: datetime, environment: Environment) -> Verification:
        parsed = parse_api_key(presented)
        if parsed is None:
            return Verification(None, "malformed", 0)
        if parsed.env != environment:
            return Verification(None, "wrong_environment", 0)
        calls = 0
        for pepper_id, pepper in self._peppers_newest_first():
            digest = lookup_digest(presented, pepper)
            calls += 1
            self.index_lookups += 1
            record = self.by_digest.get(digest)
            # The index lookup already matched; the constant-time check is defense in depth
            # for stores that index a truncated digest or a separate public key id.
            if record is None or not hmac.compare_digest(record.digest, digest):
                continue
            if record.revoked_at is not None:
                return Verification(None, "revoked", calls)
            if not record.usable_at(now):
                return Verification(None, "expired", calls)
            if pepper_id != self.current_pepper:
                self._rehash(record, presented)
            record.last_used_at = now
            return Verification(record, None, calls)
        return Verification(None, "unknown", calls)

    def _rehash(self, record: KeyRecord, presented: str) -> None:
        """Lazy pepper migration: only a successful presentation reveals the plaintext."""
        with self._lock:
            del self.by_digest[record.digest]
            record.digest = lookup_digest(presented, self.peppers[self.current_pepper])
            record.pepper_id = self.current_pepper
            self.by_digest[record.digest] = record

    def live_keys(self, owner: str, now: datetime) -> list[KeyRecord]:
        return [r for r in self.by_id.values() if r.owner == owner and r.usable_at(now)]

    def rotate(
        self,
        key_id: str,
        now: datetime,
        grace: timedelta,
        token_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> tuple[str, KeyRecord]:
        """Issue a successor and schedule the old key to expire after `grace` (dual-key window).

        The count check and the successor's insert share one critical section; releasing the
        lock between them would let two concurrent rotations both see one live key."""
        with self._lock:
            old = self.by_id[key_id]
            if len(self.live_keys(old.owner, now)) >= MAX_LIVE_KEYS:
                raise TooManyKeysError(f"{old.owner} already has {MAX_LIVE_KEYS} usable keys")
            deadline = now + grace
            if old.expires_at is None or deadline < old.expires_at:
                old.expires_at = deadline
            return self.issue(old.owner, old.env, now, token_bytes)

    def revoke(self, key_id: str, now: datetime) -> None:
        with self._lock:
            self.by_id[key_id].revoked_at = now

    def revoke_leaked(self, text: str, now: datetime) -> list[str]:
        """Secret-scanning webhook core: revoke every key found in `text` that we issued."""
        revoked: list[str] = []
        for api_key in find_leaked_keys(text):
            parsed = parse_api_key(api_key)
            if parsed is None:  # pragma: no cover - find_leaked_keys already checked
                continue
            result = self.verify(api_key, now, parsed.env)
            if result.record is not None:
                self.revoke(result.record.key_id, now)
                revoked.append(result.record.key_id)
        return revoked


def create_app(
    store: KeyStore,
    clock: Callable[[], datetime],
    environment: Environment = "live",
    grace: timedelta = timedelta(hours=24),
) -> FastAPI:
    app = FastAPI()
    bearer = HTTPBearer(auto_error=False)
    challenge = f'Bearer realm="{BRAND}"'

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> KeyRecord:
        if credentials is None:  # RFC 6750 Section 3.1: no error code without credentials
            raise HTTPException(401, "missing credentials", {"WWW-Authenticate": challenge})
        result = store.verify(credentials.credentials, clock(), environment)
        if result.record is None:  # one external answer for every internal reason
            headers = {"WWW-Authenticate": f'{challenge}, error="invalid_token"'}
            raise HTTPException(401, "invalid api key", headers)
        return result.record

    @app.get("/v1/whoami")
    async def whoami(record: Annotated[KeyRecord, Depends(authenticate)]) -> dict[str, str]:
        return {"owner": record.owner, "key_id": record.key_id, "hint": record.hint}

    @app.post("/v1/keys/rotate")
    async def rotate(
        record: Annotated[KeyRecord, Depends(authenticate)], response: Response
    ) -> dict[str, str]:
        try:
            api_key, new = store.rotate(record.key_id, clock(), grace)
        except TooManyKeysError as exc:
            raise HTTPException(409, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"  # the body holds a live secret
        expires_at = record.expires_at.isoformat() if record.expires_at else ""
        return {"api_key": api_key, "key_id": new.key_id, "old_key_expires_at": expires_at}

    return app


def naive_equal(expected: bytes, given: bytes) -> tuple[bool, int]:
    """Early-exit comparison, like `==`; returns the result and the bytes it examined."""
    if len(expected) != len(given):
        return False, 0
    steps = 0
    for x, y in zip(expected, given, strict=True):
        steps += 1
        if x != y:
            return False, steps
    return True, steps


def constant_time_equal(expected: bytes, given: bytes) -> tuple[bool, int]:
    """Model of CPython `_tscmp`: the loop runs len(given) times whatever the contents."""
    same_length = len(expected) == len(given)
    left = expected if same_length else given
    result = 0 if same_length else 1
    steps = 0
    for x, y in zip(left, given, strict=True):
        result |= x ^ y
        steps += 1
    return result == 0, steps


def timing_attack(
    oracle: Callable[[bytes], tuple[bool, int]], length: int, alphabet: bytes
) -> tuple[bytes | None, int]:
    """Recover a secret one byte at a time by keeping the guess that ran longest."""
    known = b""
    queries = 0
    for position in range(length):
        best, best_steps = alphabet[0], -1
        for candidate in alphabet:
            guess = known + bytes([candidate]) + b"\x00" * (length - position - 1)
            ok, steps = oracle(guess)
            queries += 1
            if ok:
                return guess, queries
            if steps > best_steps:
                best, best_steps = candidate, steps
        known += bytes([best])
    return None, queries
