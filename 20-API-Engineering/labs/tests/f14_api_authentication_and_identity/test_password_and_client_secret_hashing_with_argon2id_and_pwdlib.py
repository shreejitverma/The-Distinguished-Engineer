"""Lab: Argon2id password hashing (pwdlib 0.3.1, argon2-cffi 25.1.0) and client-secret digests."""

from __future__ import annotations

import base64
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import httpx
import pytest
from argon2 import Parameters, PasswordHasher, Type, extract_parameters, profiles
from argon2.exceptions import HashingError
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher

from api_labs.f14_api_authentication_and_identity.password_hashing import (
    HIGH_ENTROPY_BITS,
    MAX_PASSWORD_BYTES,
    ClientSecretStore,
    InvalidPasswordInputError,
    PasswordAuthenticator,
    UserRecord,
    apply_pepper,
    argon2_hasher,
    create_app,
    normalize_password,
    storage_scheme_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pwdlib.hashers import HasherProtocol

# Cheap but real Argon2id: 1 MiB, 1 pass, 1 lane. Tests that need a measurable cost use TIMED.
FAST = replace(profiles.RFC_9106_LOW_MEMORY, memory_cost=1024, time_cost=1, parallelism=1)
TIMED = replace(profiles.RFC_9106_LOW_MEMORY, memory_cost=8192, time_cost=1, parallelism=1)
PEPPERS = {"p1": b"pepper-one-EXAMPLE-NOT-A-REAL-KEY", "p2": b"pepper-two-EXAMPLE-NOT-A-REAL-KEY"}


@dataclass
class CountingHasher:
    """A ``HasherProtocol`` wrapper that records calls and the peak number in flight."""

    inner: HasherProtocol
    delay: float = 0.0
    hashed: list[str | bytes] = field(default_factory=list[str | bytes])
    verified: list[str | bytes] = field(default_factory=list[str | bytes])
    in_flight: int = 0
    peak: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def identify(cls, hash: str | bytes) -> bool:
        # pwdlib's identify is a classmethod: one regex for every Argon2Hasher instance.
        return Argon2Hasher.identify(hash)

    def _enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        time.sleep(self.delay)

    def _leave(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
        self._enter()
        try:
            self.hashed.append(password)
            return self.inner.hash(password, salt=salt)
        finally:
            self._leave()

    def verify(self, password: str | bytes, hash: str | bytes) -> bool:
        self._enter()
        try:
            self.verified.append(hash)
            return self.inner.verify(password, hash)
        finally:
            self._leave()

    def check_needs_rehash(self, hash: str | bytes) -> bool:
        return self.inner.check_needs_rehash(hash)


def _authenticator(
    params: Parameters = FAST, *, delay: float = 0.0, max_concurrent_hashes: int = 4
) -> tuple[PasswordAuthenticator, CountingHasher, dict[str, UserRecord]]:
    counter = CountingHasher(argon2_hasher(params), delay=delay)
    users: dict[str, UserRecord] = {}
    auth = PasswordAuthenticator(
        PasswordHash((counter,)),
        users,
        peppers=PEPPERS,
        current_pepper_id="p1",
        max_concurrent_hashes=max_concurrent_hashes,
    )
    return auth, counter, users


def _median_seconds(fn: Callable[[], object], runs: int = 7) -> float:
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


# --- encoded hash format and library defaults -------------------------------------------


@pytest.mark.unit
def test_recommended_uses_rfc_9106_second_recommended_option() -> None:
    encoded = PasswordHash.recommended().hash("correct horse battery staple")
    assert encoded.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert extract_parameters(encoded) == profiles.RFC_9106_LOW_MEMORY
    assert len(encoded) == 97


@pytest.mark.unit
def test_phc_string_fields_are_unpadded_base64_of_salt_and_tag() -> None:
    encoded = argon2_hasher(FAST).hash("pw", salt=b"0123456789abcdef")
    empty, variant, version, params, salt_b64, tag_b64 = encoded.split("$")
    assert (empty, variant, version, params) == ("", "argon2id", "v=19", "m=1024,t=1,p=1")
    assert salt_b64 == base64.b64encode(b"0123456789abcdef").decode().rstrip("=")
    assert (len(salt_b64), len(tag_b64)) == (22, 43)  # 16 and 32 bytes, no "=" padding
    assert len(base64.b64decode(tag_b64 + "=")) == 32


@pytest.mark.unit
def test_random_salt_makes_equal_passwords_store_differently() -> None:
    hasher = argon2_hasher(FAST)
    first, second = hasher.hash("same password"), hasher.hash("same password")
    assert first != second
    assert hasher.verify("same password", first)
    assert hasher.verify("same password", second)


@pytest.mark.unit
def test_explicit_salt_is_deterministic_but_empty_salt_falls_back_to_random() -> None:
    hasher = argon2_hasher(FAST)
    salt = b"fixed-salt-16byt"
    assert hasher.hash("pw", salt=salt) == hasher.hash("pw", salt=salt)
    # argon2-cffi uses ``salt or os.urandom(salt_len)``, so b"" is treated as "no salt given".
    assert hasher.hash("pw", salt=b"") != hasher.hash("pw", salt=b"")


@pytest.mark.unit
def test_reference_implementation_rejects_salts_shorter_than_8_bytes() -> None:
    with pytest.raises(HashingError, match="Salt is too short"):
        argon2_hasher(FAST).hash("pw", salt=b"1234567")
    assert argon2_hasher(FAST).hash("pw", salt=b"12345678").count("$") == 5


@pytest.mark.unit
def test_nul_bytes_are_hashed_not_treated_as_terminators() -> None:
    hasher = argon2_hasher(FAST)
    stored = hasher.hash(b"abc\x00def")
    assert hasher.verify(b"abc\x00def", stored)
    assert not hasher.verify(b"abc\x00xyz", stored)
    assert not hasher.verify(b"abc", stored)


# --- pwdlib API contracts ---------------------------------------------------------------


@pytest.mark.unit
def test_pwdlib_verify_takes_password_then_hash_unlike_argon2_cffi() -> None:
    ph = PasswordHash((argon2_hasher(FAST),))
    stored = ph.hash("pw")
    assert ph.verify("pw", stored) is True
    with pytest.raises(UnknownHashError):
        ph.verify(stored, "pw")  # the pre-0.2.0 order: "pw" is not a recognizable hash
    assert PasswordHasher.from_parameters(FAST).verify(stored, "pw") is True


@pytest.mark.unit
def test_mismatch_and_corrupt_argon2_hash_return_false_but_foreign_format_raises() -> None:
    ph = PasswordHash((argon2_hasher(FAST),))
    stored = ph.hash("pw")
    assert ph.verify("wrong", stored) is False
    corrupt = stored[:-10] + "!!!!!!!!!!"  # still matches pwdlib's identify regex
    assert ph.verify("pw", corrupt) is False
    with pytest.raises(UnknownHashError):
        ph.verify("pw", "!unusable-password-marker")
    with pytest.raises(UnknownHashError):
        ph.verify("pw", "$2b$12$" + "a" * 53)  # bcrypt, but no BcryptHasher configured


@pytest.mark.unit
@pytest.mark.parametrize(
    "changed",
    [
        replace(FAST, time_cost=2),
        replace(FAST, memory_cost=2048),
        replace(FAST, parallelism=2),
        replace(FAST, hash_len=16),
        replace(FAST, salt_len=8),
        replace(FAST, type=Type.I),
    ],
    ids=["time_cost", "memory_cost", "parallelism", "hash_len", "salt_len", "type"],
)
def test_check_needs_rehash_compares_every_encoded_parameter(changed: Parameters) -> None:
    current = argon2_hasher(FAST)
    older = argon2_hasher(changed)
    assert current.check_needs_rehash(older.hash("pw")) is True
    assert current.check_needs_rehash(current.hash("pw")) is False


@pytest.mark.unit
def test_verify_and_update_rehashes_only_on_success_with_stale_parameters() -> None:
    stale = argon2_hasher(replace(FAST, time_cost=2)).hash("pw")
    ph = PasswordHash((argon2_hasher(FAST),))
    assert ph.verify_and_update("wrong", stale) == (False, None)
    valid, updated = ph.verify_and_update("pw", stale)
    assert valid is True
    assert updated is not None
    assert extract_parameters(updated) == FAST
    assert ph.verify_and_update("pw", updated) == (True, None)


@pytest.mark.unit
def test_first_argon2_hasher_claims_every_argon2_hash() -> None:
    current = CountingHasher(argon2_hasher(FAST))
    legacy = CountingHasher(argon2_hasher(replace(FAST, time_cost=2)))
    old_hash = argon2_hasher(replace(FAST, time_cost=2)).hash("pw")
    ph = PasswordHash((current, legacy))
    valid, updated = ph.verify_and_update("pw", old_hash)
    assert (valid, updated is not None) == (True, True)
    # identify() is a regex over any $argon2*$ string, so the legacy entry is never consulted.
    assert (len(current.verified), len(legacy.verified)) == (1, 0)


@pytest.mark.unit
def test_verify_cost_follows_the_stored_hash_not_the_hasher_configuration() -> None:
    configured_expensive = argon2_hasher(TIMED)
    cheap_hash = argon2_hasher(replace(FAST, memory_cost=8)).hash("pw")
    expensive_hash = configured_expensive.hash("pw")
    cheap = _median_seconds(lambda: configured_expensive.verify("pw", cheap_hash))
    expensive = _median_seconds(lambda: configured_expensive.verify("pw", expensive_hash))
    assert cheap < expensive / 10


# --- input normalization and pepper -----------------------------------------------------


@pytest.mark.unit
def test_nfc_normalization_makes_composed_and_decomposed_passwords_equal() -> None:
    composed, decomposed = "caf\u00e9-pass", "cafe\u0301-pass"
    assert composed != decomposed
    assert normalize_password(composed) == normalize_password(decomposed)
    raw = argon2_hasher(FAST)  # pwdlib itself only UTF-8 encodes; it does not normalize
    assert not raw.verify(decomposed, raw.hash(composed))
    auth, _, _ = _authenticator()
    auth.register("ana", composed)
    assert auth.authenticate("ana", decomposed) is not None


@pytest.mark.unit
def test_oversized_or_unencodable_password_is_rejected_before_any_hashing() -> None:
    auth, counter, _ = _authenticator()
    baseline = len(counter.hashed) + len(counter.verified)
    with pytest.raises(InvalidPasswordInputError):
        auth.authenticate("nobody", "x" * (MAX_PASSWORD_BYTES + 1))
    with pytest.raises(InvalidPasswordInputError):
        auth.authenticate("nobody", "\ud800")
    assert len(counter.hashed) + len(counter.verified) == baseline
    assert len(normalize_password("x" * MAX_PASSWORD_BYTES)) == MAX_PASSWORD_BYTES


@pytest.mark.unit
def test_stolen_hash_cannot_be_checked_without_the_pepper() -> None:
    auth, _, users = _authenticator()
    record = auth.register("bo", "hunter2-but-longer")
    assert record.password_hash is not None
    raw = argon2_hasher(FAST)
    assert not raw.verify("hunter2-but-longer", record.password_hash)
    peppered = apply_pepper(b"hunter2-but-longer", PEPPERS["p1"])
    assert raw.verify(peppered, record.password_hash)
    assert users["bo"] == record


# --- timing-equalized authentication ----------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "record",
    [
        None,
        UserRecord("sso", None),
        UserRecord("dj", "!unusable", "p1"),
        UserRecord("x", "h", "p9"),
    ],
    ids=["unknown-user", "sso-only", "foreign-hash-format", "retired-pepper"],
)
def test_every_failure_path_runs_exactly_one_dummy_verify(record: UserRecord | None) -> None:
    auth, counter, users = _authenticator()
    if record is not None:
        users["target"] = record
    before = len(counter.verified)
    assert auth.authenticate("target", "any password at all") is None
    assert auth.authenticate("target", "another password") is None
    first, second = counter.verified[before:]  # exactly one verify per attempt
    assert first == second  # the same dummy hash every time ...
    assert extract_parameters(str(first)) == FAST  # ... made with the current parameters
    assert record is None or first != record.password_hash


@pytest.mark.unit
def test_unknown_user_takes_as_long_as_a_wrong_password() -> None:
    auth, _, _ = _authenticator(TIMED)
    auth.register("real", "the real password")
    wrong_password = _median_seconds(lambda: auth.authenticate("real", "a wrong password"))
    unknown_user = _median_seconds(lambda: auth.authenticate("ghost", "a wrong password"))
    # Without the dummy verify the ratio is about 1/1000; with it, about 1.
    assert unknown_user > wrong_password / 4


@pytest.mark.unit
def test_login_with_stale_parameters_upgrades_the_stored_hash() -> None:
    auth, counter, users = _authenticator()
    stale = argon2_hasher(replace(FAST, time_cost=2)).hash(
        apply_pepper(b"pw-123456", PEPPERS["p1"])
    )
    users["old"] = UserRecord("old", stale, "p1")
    assert auth.authenticate("old", "wrong") is None
    assert users["old"].password_hash == stale  # a failed login never rewrites
    user = auth.authenticate("old", "pw-123456")
    assert user is not None
    assert users["old"] == user
    assert user.password_hash is not None
    assert extract_parameters(user.password_hash) == FAST
    hashes_before = len(counter.hashed)
    assert auth.authenticate("old", "pw-123456") == user
    assert len(counter.hashed) == hashes_before  # already current: no rewrite


@pytest.mark.unit
def test_login_after_pepper_rotation_rewraps_under_the_current_pepper() -> None:
    raw = argon2_hasher(FAST)
    users = {"lee": UserRecord("lee", raw.hash(apply_pepper(b"pw-abcdef", PEPPERS["p2"])), "p2")}
    auth = PasswordAuthenticator(
        PasswordHash((raw,)), users, peppers=PEPPERS, current_pepper_id="p1"
    )
    user = auth.authenticate("lee", "pw-abcdef")
    assert user is not None
    assert user.pepper_id == "p1"
    assert user.password_hash is not None
    assert raw.verify(apply_pepper(b"pw-abcdef", PEPPERS["p1"]), user.password_hash)
    assert users["lee"] == user


@pytest.mark.unit
def test_concurrent_logins_never_exceed_the_hash_slot_limit() -> None:
    auth, counter, _ = _authenticator(delay=0.02, max_concurrent_hashes=2)

    def attempt(i: int) -> UserRecord | None:
        return auth.authenticate(f"u{i}", "pw")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results == [None] * 16
    assert counter.peak == 2


# --- high-entropy client secrets --------------------------------------------------------


@pytest.mark.unit
def test_storage_class_switches_at_112_bits_of_entropy() -> None:
    assert HIGH_ENTROPY_BITS == 112
    assert storage_scheme_for(6 * math.log2(10)) == "password-hash"  # 6-digit look-up code
    assert storage_scheme_for(111.9) == "password-hash"
    assert storage_scheme_for(112) == "keyed-fast-hash"
    assert storage_scheme_for(32 * 8) == "keyed-fast-hash"  # secrets.token_urlsafe(32)


@pytest.mark.unit
def test_client_secret_has_256_bits_and_only_its_hmac_is_stored() -> None:
    store = ClientSecretStore(b"server-key-EXAMPLE-NOT-A-REAL-KEY")
    secret = store.issue("svc-a")
    assert len(secret) == 43
    assert len(base64.urlsafe_b64decode(secret + "=")) == 32
    record = store.record("svc-a")
    assert record is not None
    assert len(record.secret_digest) == 32
    assert secret.encode() not in record.secret_digest
    assert record.secret_digest == store.digest(secret)


@pytest.mark.unit
def test_client_secret_verify_rejects_wrong_secret_unknown_client_and_other_key() -> None:
    store = ClientSecretStore(b"server-key-EXAMPLE-NOT-A-REAL-KEY")
    secret = store.issue("svc-a")
    assert store.verify("svc-a", secret) is True
    assert store.verify("svc-a", secret[:-1] + ("A" if secret[-1] != "A" else "B")) is False
    assert store.verify("svc-b", secret) is False
    other = ClientSecretStore(b"other-key-EXAMPLE-NOT-A-REAL-KEY")
    assert other.digest(secret) != store.digest(secret)


# --- FastAPI endpoints over ASGI --------------------------------------------------------


def _client(auth: PasswordAuthenticator, clients: ClientSecretStore) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(auth, clients))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_user_and_wrong_password_get_identical_401_responses() -> None:
    auth, _, users = _authenticator()
    auth.register("real", "the real password")
    users["sso"] = UserRecord("sso", None)
    clients = ClientSecretStore(b"server-key-EXAMPLE-NOT-A-REAL-KEY")
    async with _client(auth, clients) as client:
        responses = [
            await client.post("/login", data={"username": name, "password": "nope"})
            for name in ("real", "ghost", "sso")
        ]
        ok = await client.post("/login", data={"username": "real", "password": "the real password"})
    shapes = {
        (r.status_code, r.content, r.headers["www-authenticate"], r.headers["content-length"])
        for r in responses
    }
    assert shapes == {(401, b'{"detail":"Incorrect username or password"}', "Bearer", "43")}
    assert ok.status_code == 200
    assert ok.json()["token_type"] == "bearer"  # noqa: S105


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_basic_auth_failure_is_invalid_client_with_basic_challenge() -> None:
    auth, _, _ = _authenticator()
    clients = ClientSecretStore(b"server-key-EXAMPLE-NOT-A-REAL-KEY")
    secret = clients.issue("svc a")
    async with _client(auth, clients) as client:
        # RFC 6749 Section 2.3.1: form-urlencode the client_id, so the space travels as "+".
        good = await client.post("/client-token", auth=("svc+a", secret))
        bad = await client.post("/client-token", auth=("svc+a", "wrong"))
        missing = await client.post("/client-token")
    assert good.status_code == 200
    for response in (bad, missing):
        assert response.status_code == 401
        assert response.json() == {"error": "invalid_client"}
        assert response.headers["www-authenticate"] == 'Basic realm="token"'
