"""Lab: prefixed, checksummed API keys stored as HMAC digests, compared in constant time, and
rotated with an overlapping grace window."""

import hashlib
import hmac
import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from api_labs.f14_api_authentication_and_identity.api_keys import (
    BASE62,
    CHECKSUM_CHARS,
    SECRET_CHARS,
    KeyStore,
    TooManyKeysError,
    constant_time_equal,
    create_app,
    display_hint,
    entropy_bits,
    find_leaked_keys,
    generate_api_key,
    lookup_digest,
    naive_equal,
    parse_api_key,
    redact,
    timing_attack,
)

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
PEPPER_V1 = b"lab-pepper-v1-not-a-real-secret"
PEPPER_V2 = b"lab-pepper-v2-not-a-real-secret"
PREFIX_LEN = len("acme_live_")


def seeded(seed: int) -> Callable[[int], bytes]:
    """Deterministic stand-in for `secrets.token_bytes`, so every run sees the same keys."""
    return lambda n: hashlib.sha256(seed.to_bytes(4, "big")).digest()[:n]


def new_store() -> KeyStore:
    return KeyStore(peppers={1: PEPPER_V1}, current_pepper=1)


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


async def call(
    store: KeyStore, clock: FakeClock, method: str, path: str, credential: bytes | None
) -> httpx.Response:
    headers = {} if credential is None else {b"authorization": credential}
    transport = httpx.ASGITransport(app=create_app(store, clock, grace=GRACE))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers)


# Format, entropy, and checksum


@pytest.mark.unit
def test_key_is_prefix_then_43_secret_digits_then_6_checksum_digits() -> None:
    api_key = generate_api_key("live", seeded(1))
    assert api_key.startswith("acme_live_")
    assert len(api_key) == PREFIX_LEN + SECRET_CHARS + CHECKSUM_CHARS == 59
    assert set(api_key[PREFIX_LEN:]) <= set(BASE62)
    assert parse_api_key(api_key) is not None


@pytest.mark.unit
@pytest.mark.parametrize("fill", [0x00, 0xFF], ids=["all-zero-bytes", "all-one-bytes"])
def test_fixed_width_encoding_keeps_length_constant_at_the_extremes(fill: int) -> None:
    api_key = generate_api_key("test", lambda n: bytes([fill]) * n)
    assert len(api_key) == 59
    assert parse_api_key(api_key) is not None


@pytest.mark.unit
def test_digit_counts_are_the_minimum_that_hold_256_and_32_bits() -> None:
    assert 62**SECRET_CHARS > 2**256 > 62 ** (SECRET_CHARS - 1)
    assert 62**CHECKSUM_CHARS > 2**32 > 62 ** (CHECKSUM_CHARS - 1)
    assert math.floor(entropy_bits(62, SECRET_CHARS)) == 256
    # GitHub's 30 random base62 characters give the "178" bits its 2021 post reports.
    assert math.floor(entropy_bits(62, 30)) == 178


@pytest.mark.unit
def test_every_single_character_substitution_fails_the_checksum() -> None:
    api_key = generate_api_key("live", seeded(2))
    tried = 0
    for i in range(PREFIX_LEN, len(api_key)):
        for c in BASE62:
            if c != api_key[i]:
                assert parse_api_key(api_key[:i] + c + api_key[i + 1 :]) is None
                tried += 1
    assert tried == 49 * 61


@pytest.mark.unit
def test_every_adjacent_transposition_fails_the_checksum() -> None:
    api_key = generate_api_key("live", seeded(3))
    swapped = 0
    for i in range(PREFIX_LEN, len(api_key) - 1):
        if api_key[i] != api_key[i + 1]:
            flipped = api_key[:i] + api_key[i + 1] + api_key[i] + api_key[i + 2 :]
            assert parse_api_key(flipped) is None
            swapped += 1
    assert swapped > 40


@pytest.mark.unit
@pytest.mark.parametrize(
    "suffix", ["\n", " ", "\r\n", "x"], ids=["newline", "space", "crlf", "extra-char"]
)
def test_trailing_bytes_make_a_key_malformed(suffix: str) -> None:
    assert parse_api_key(generate_api_key("live", seeded(4)) + suffix) is None


# Storage and verification


@pytest.mark.unit
def test_malformed_key_is_rejected_without_touching_the_index() -> None:
    store = new_store()
    api_key, _ = store.issue("alice", "live", T0, seeded(5))
    typo = api_key[:-1] + ("A" if api_key[-1] != "A" else "B")
    result = store.verify(typo, T0, "live")
    assert (result.reason, result.hmac_calls, store.index_lookups) == ("malformed", 0, 0)


@pytest.mark.unit
def test_test_mode_key_is_refused_by_a_live_deployment() -> None:
    store = new_store()
    api_key, _ = store.issue("alice", "test", T0, seeded(6))
    assert store.verify(api_key, T0, "live").reason == "wrong_environment"
    assert store.verify(api_key, T0, "test").record is not None


@pytest.mark.unit
def test_store_keeps_the_hmac_digest_and_never_the_plaintext() -> None:
    store = new_store()
    api_key, record = store.issue("alice", "live", T0, seeded(7))
    assert record.digest == hmac.new(PEPPER_V1, api_key.encode(), hashlib.sha256).digest()
    assert api_key not in repr(record)
    assert api_key[PREFIX_LEN + 4 : -4] not in repr(record)
    assert record.hint == display_hint(api_key) == f"{api_key[:14]}...{api_key[-4:]}"


@pytest.mark.unit
def test_a_dumped_table_cannot_be_matched_without_the_pepper() -> None:
    store = new_store()
    api_key, _ = store.issue("alice", "live", T0, seeded(8))
    unkeyed = hashlib.sha256(api_key.encode()).digest()
    wrong_pepper = lookup_digest(api_key, b"guessed-pepper")
    assert unkeyed not in store.by_digest
    assert wrong_pepper not in store.by_digest
    assert lookup_digest(api_key, PEPPER_V1) in store.by_digest


@pytest.mark.unit
def test_pepper_rotation_still_verifies_old_digests_and_rehashes_on_first_use() -> None:
    store = new_store()
    api_key, record = store.issue("alice", "live", T0, seeded(9))
    store.peppers[2] = PEPPER_V2
    store.current_pepper = 2
    first = store.verify(api_key, T0, "live")
    assert (first.record is record, first.hmac_calls, record.pepper_id) == (True, 2, 2)
    assert record.digest == lookup_digest(api_key, PEPPER_V2)
    second = store.verify(api_key, T0, "live")
    assert (second.record is record, second.hmac_calls) == (True, 1)


@pytest.mark.unit
def test_unknown_key_costs_one_hmac_per_active_pepper() -> None:
    store = new_store()
    store.peppers[2] = PEPPER_V2
    stranger = generate_api_key("live", seeded(10))
    result = store.verify(stranger, T0, "live")
    assert (result.reason, result.hmac_calls) == ("unknown", 2)


# Rotation


@pytest.mark.unit
def test_both_keys_verify_until_the_grace_deadline_then_only_the_new_one() -> None:
    store = new_store()
    old_key, old = store.issue("alice", "live", T0, seeded(11))
    new_key, _ = store.rotate(old.key_id, T0, GRACE, seeded(12))
    just_before = T0 + GRACE - timedelta(seconds=1)
    assert store.verify(old_key, just_before, "live").record is old
    assert store.verify(new_key, just_before, "live").record is not None
    assert store.verify(old_key, T0 + GRACE, "live").reason == "expired"
    assert store.verify(new_key, T0 + GRACE, "live").record is not None


@pytest.mark.unit
def test_a_third_live_key_is_refused_until_the_draining_key_expires() -> None:
    store = new_store()
    _, old = store.issue("alice", "live", T0, seeded(13))
    _, new = store.rotate(old.key_id, T0, GRACE, seeded(14))
    with pytest.raises(TooManyKeysError):
        store.rotate(new.key_id, T0 + timedelta(hours=1), GRACE, seeded(15))
    _, newest = store.rotate(new.key_id, T0 + GRACE, GRACE, seeded(16))
    assert [r.key_id for r in store.live_keys("alice", T0 + GRACE)] == [
        new.key_id,
        newest.key_id,
    ]


@pytest.mark.unit
def test_concurrent_rotations_cannot_both_pass_the_two_key_check() -> None:
    store = new_store()
    _, old = store.issue("alice", "live", T0, seeded(32))
    outcome: list[str] = []

    def rotate_b() -> None:
        try:
            store.rotate(old.key_id, T0, GRACE, seeded(33))
            outcome.append("rotated")
        except TooManyKeysError:
            outcome.append("refused")

    def bytes_for_a(n: int) -> bytes:
        # Rotation A is between its count check and its insert: start rotation B now.
        racer = threading.Thread(target=rotate_b)
        racer.start()
        racer.join(timeout=0.5)
        threads.append(racer)
        return seeded(34)(n)

    threads: list[threading.Thread] = []
    store.rotate(old.key_id, T0, GRACE, bytes_for_a)
    for racer in threads:
        racer.join()
    assert outcome == ["refused"]
    assert len(store.live_keys("alice", T0)) == 2


@pytest.mark.unit
def test_revocation_beats_the_grace_window() -> None:
    store = new_store()
    old_key, old = store.issue("alice", "live", T0, seeded(17))
    store.rotate(old.key_id, T0, GRACE, seeded(18))
    store.revoke(old.key_id, T0 + timedelta(minutes=5))
    assert store.verify(old_key, T0 + timedelta(minutes=6), "live").reason == "revoked"


@pytest.mark.unit
def test_last_used_at_shows_when_the_old_key_has_drained() -> None:
    store = new_store()
    old_key, old = store.issue("alice", "live", T0, seeded(19))
    new_key, new = store.rotate(old.key_id, T0, GRACE, seeded(20))
    store.verify(old_key, T0 + timedelta(hours=1), "live")
    store.verify(new_key, T0 + timedelta(hours=3), "live")
    assert old.last_used_at == T0 + timedelta(hours=1)
    assert new.last_used_at == T0 + timedelta(hours=3)


# Secret scanning and redaction


@pytest.mark.unit
def test_scanner_reports_real_keys_and_drops_lookalikes_that_fail_the_checksum() -> None:
    real = generate_api_key("live", seeded(21))
    lookalike = real[:-6] + "000000"
    embedded = "x" + generate_api_key("test", seeded(22))
    text = f"export ACME_KEY={real}\nold={lookalike}\nid={embedded}\n"
    assert find_leaked_keys(text) == [real]


@pytest.mark.unit
def test_redaction_leaves_only_the_display_hint_in_a_log_line() -> None:
    api_key = generate_api_key("live", seeded(23))
    line = redact(f"401 for Authorization: Bearer {api_key} from 192.0.2.7")
    assert api_key not in line
    assert f"Bearer {display_hint(api_key)} from" in line


@pytest.mark.unit
def test_scanning_webhook_revokes_only_keys_this_store_issued() -> None:
    store = new_store()
    leaked, record = store.issue("alice", "live", T0, seeded(24))
    foreign = generate_api_key("live", seeded(25))
    revoked = store.revoke_leaked(f"{leaked} {foreign}", T0)
    assert revoked == [record.key_id]
    assert store.verify(leaked, T0, "live").reason == "revoked"


# Comparison timing


@pytest.mark.unit
def test_early_exit_compare_reveals_the_first_mismatch_position() -> None:
    expected = b"abcdefgh"
    assert naive_equal(expected, b"xbcdefgh") == (False, 1)
    assert naive_equal(expected, b"abcdefgx") == (False, 8)
    assert constant_time_equal(expected, b"xbcdefgh") == (False, 8)
    assert constant_time_equal(expected, b"abcdefgx") == (False, 8)
    assert constant_time_equal(expected, b"abc") == (False, 3)


TARGET = b"Q7xK2pLm9ZrT4vWc"
ALPHABET = BASE62.encode()


@pytest.mark.unit
def test_timing_oracle_on_an_early_exit_compare_recovers_the_secret() -> None:
    found, queries = timing_attack(lambda g: naive_equal(TARGET, g), len(TARGET), ALPHABET)
    assert found == TARGET
    assert queries <= 62 * len(TARGET)


@pytest.mark.unit
def test_timing_oracle_learns_nothing_from_a_constant_time_compare() -> None:
    found, queries = timing_attack(lambda g: constant_time_equal(TARGET, g), len(TARGET), ALPHABET)
    assert found is None
    assert queries == 62 * len(TARGET)


@pytest.mark.unit
def test_timing_oracle_learns_nothing_when_digests_not_secrets_are_compared() -> None:
    stored = hashlib.sha256(TARGET).digest()

    def oracle(guess: bytes) -> tuple[bool, int]:
        return naive_equal(stored, hashlib.sha256(guess).digest())

    found, _ = timing_attack(oracle, len(TARGET), ALPHABET)
    assert found is None


@pytest.mark.unit
def test_cpython_compare_digest_raises_on_non_ascii_str_but_not_on_bytes() -> None:
    with pytest.raises(TypeError, match="non-ASCII"):
        hmac.compare_digest("acme_live_caf\u00e9", "acme_live_cafe")
    assert hmac.compare_digest("acme_live_caf\u00e9".encode(), b"acme_live_cafe") is False


# HTTP surface (FastAPI 0.162.0 HTTPBearer, RFC 6750)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_credentials_get_a_bearer_challenge_without_an_error_code() -> None:
    response = await call(new_store(), FakeClock(), "GET", "/v1/whoami", None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="acme"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_key_authenticates_and_the_scheme_is_case_insensitive() -> None:
    store = new_store()
    api_key, record = store.issue("alice", "live", T0, seeded(26))
    response = await call(store, FakeClock(), "GET", "/v1/whoami", b"bearer " + api_key.encode())
    assert response.status_code == 200
    assert response.json() == {"owner": "alice", "key_id": record.key_id, "hint": record.hint}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_ascii_credential_is_a_401_not_a_500() -> None:
    response = await call(new_store(), FakeClock(), "GET", "/v1/whoami", b"Bearer acme_\xe9")
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_rejection_reason_looks_identical_to_the_client() -> None:
    store = new_store()
    clock = FakeClock()
    expired_key, expired = store.issue("alice", "live", T0, seeded(27))
    expired.expires_at = T0
    revoked_key, revoked = store.issue("bob", "live", T0, seeded(28))
    store.revoke(revoked.key_id, T0)
    test_key, _ = store.issue("carol", "test", T0, seeded(29))
    candidates = [expired_key, revoked_key, test_key, generate_api_key("live", seeded(30)), "x"]
    seen = set[tuple[int, str, str]]()
    for candidate in candidates:
        r = await call(store, clock, "GET", "/v1/whoami", b"Bearer " + candidate.encode())
        seen.add((r.status_code, r.headers["www-authenticate"], r.text))
    assert seen == {
        (401, 'Bearer realm="acme", error="invalid_token"', '{"detail":"invalid api key"}')
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rotation_over_http_has_no_window_where_neither_key_works() -> None:
    store = new_store()
    clock = FakeClock()
    old_key, _ = store.issue("alice", "live", T0, seeded(31))
    old_auth = b"Bearer " + old_key.encode()

    rotated = await call(store, clock, "POST", "/v1/keys/rotate", old_auth)
    assert rotated.status_code == 200
    assert rotated.headers["cache-control"] == "no-store"
    body = rotated.json()
    assert body["old_key_expires_at"] == (T0 + GRACE).isoformat()
    new_auth = b"Bearer " + body["api_key"].encode()

    again = await call(store, clock, "POST", "/v1/keys/rotate", new_auth)
    assert again.status_code == 409

    statuses: list[tuple[int, int]] = []
    for offset in (timedelta(0), GRACE - timedelta(seconds=1), GRACE):
        clock.now = T0 + offset
        old = await call(store, clock, "GET", "/v1/whoami", old_auth)
        new = await call(store, clock, "GET", "/v1/whoami", new_auth)
        statuses.append((old.status_code, new.status_code))
    assert statuses == [(200, 200), (200, 200), (401, 200)]
