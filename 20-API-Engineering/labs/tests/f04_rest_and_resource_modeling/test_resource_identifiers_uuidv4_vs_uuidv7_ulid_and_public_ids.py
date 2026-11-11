"""UUIDv4 and UUIDv7 layouts (RFC 9562), base32 public IDs (TypeID 0.3.0), and B-tree locality."""

from __future__ import annotations

import random
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING

import httpx
import pytest

from api_labs.f04_rest_and_resource_modeling.resource_identifiers import (
    RAND_B_MAX,
    InvalidIdError,
    PublicId,
    UUIDv7Generator,
    WrongTypeError,
    build_app,
    decode_base32,
    decode_crockford_lenient,
    encode_base32,
    measure_batch_insert,
    ulid_text,
    uuid4_from_bytes,
    uuid7_from_parts,
    uuid7_unix_ts_ms,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.applications import Starlette

    from api_labs.f04_rest_and_resource_modeling.resource_identifiers import Layout

# RFC 9562 Appendix A.6, Figure 26.
RFC_V7 = uuid.UUID("017F22E2-79B0-7CC3-98C4-DC0C0C07398F")
RFC_V7_MS = 1645557742000

# jetify-com/typeid spec/valid.json and spec/invalid.json at commit ac41b7c1.
TYPEID_VALID = [
    ("00000000000000000000000000", "", "00000000-0000-0000-0000-000000000000"),
    ("00000000000000000000000001", "", "00000000-0000-0000-0000-000000000001"),
    ("0000000000000000000000000a", "", "00000000-0000-0000-0000-00000000000a"),
    ("0000000000000000000000000g", "", "00000000-0000-0000-0000-000000000010"),
    ("00000000000000000000000010", "", "00000000-0000-0000-0000-000000000020"),
    ("7zzzzzzzzzzzzzzzzzzzzzzzzz", "", "ffffffff-ffff-ffff-ffff-ffffffffffff"),
    ("prefix_0123456789abcdefghjkmnpqrs", "prefix", "0110c853-1d09-52d8-d73e-1194e95b5f19"),
    ("prefix_01h455vb4pex5vsknk084sn02q", "prefix", "01890a5d-ac96-774b-bcce-b302099a8057"),
    ("pre_fix_00000000000000000000000000", "pre_fix", "00000000-0000-0000-0000-000000000000"),
]
TYPEID_INVALID = [
    "PREFIX_00000000000000000000000000",
    "12345_00000000000000000000000000",
    "pre.fix_00000000000000000000000000",
    "pr\u00e9fix_00000000000000000000000000",
    "  prefix_00000000000000000000000000",
    "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyzabcdefghijkl_00000000000000000000000000",
    "_00000000000000000000000000",
    "_",
    "prefix_1234567890123456789012345",
    "prefix_123456789012345678901234567",
    "prefix_1234567890123456789012345 ",
    "prefix_0123456789ABCDEFGHJKMNPQRS",
    "prefix_123456789-123456789-123456",
    "prefix_ooooooiiiiiiuuuuuuulllllll",
    "prefix_i23456789ol23456789oi23456",
    "prefix_123456789-0123456789-0123456",
    "prefix_8zzzzzzzzzzzzzzzzzzzzzzzzz",
    "_prefix_00000000000000000000000000",
    "prefix__00000000000000000000000000",
    "",
    "prefix_",
]


def rng(seed: int) -> random.Random:
    """Seeded entropy so every run is identical; real IDs use `os.urandom`."""
    return random.Random(seed)  # noqa: S311 - deterministic test entropy, not a secret


def stepping_clock(start_ms: int, step_ms: int = 1) -> Callable[[], int]:
    state = [start_ms - step_ms]

    def clock() -> int:
        state[0] += step_ms
        return state[0]

    return clock


def scripted_clock(*readings: int) -> Callable[[], int]:
    remaining = list(readings)
    return lambda: remaining.pop(0)


def counter_42(value: uuid.UUID) -> int:
    """The 42-bit Method 1 counter: rand_a (12 bits) then the top 30 bits of rand_b."""
    rand = (((value.int >> 64) & 0xFFF) << 62) | (value.int & RAND_B_MAX)
    return rand >> 32


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
def test_rfc9562_appendix_a6_vector_round_trips() -> None:
    built = uuid7_from_parts(0x017F22E279B0, 0xCC3, (0b01 << 60) | 0x8C4DC0C0C07398F)
    assert built == RFC_V7
    assert built.version == 7
    assert built.variant == uuid.RFC_4122
    assert uuid7_unix_ts_ms(built) == RFC_V7_MS


@pytest.mark.unit
@pytest.mark.parametrize(
    ("unix_ts_ms", "rand_a", "rand_b"),
    [(1 << 48, 0, 0), (0, 1 << 12, 0), (0, 0, 1 << 62), (-1, 0, 0)],
    ids=["ts-49-bits", "rand-a-13-bits", "rand-b-63-bits", "negative-ts"],
)
def test_uuid7_layout_rejects_fields_that_would_clobber_neighbours(
    unix_ts_ms: int, rand_a: int, rand_b: int
) -> None:
    with pytest.raises(ValueError, match="must fit"):
        uuid7_from_parts(unix_ts_ms, rand_a, rand_b)


@pytest.mark.unit
def test_uuid4_overwrites_exactly_the_version_and_variant_bits() -> None:
    assert str(uuid4_from_bytes(bytes(16))) == "00000000-0000-4000-8000-000000000000"
    assert str(uuid4_from_bytes(b"\xff" * 16)) == "ffffffff-ffff-4fff-bfff-ffffffffffff"
    assert uuid4_from_bytes(rng(4).randbytes(16)).version == 4


@pytest.mark.unit
def test_generator_is_strictly_monotonic_within_one_millisecond() -> None:
    generate = UUIDv7Generator(clock_ms=lambda: RFC_V7_MS, entropy=rng(1).randbytes)
    ids = [generate() for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 1000
    assert {uuid7_unix_ts_ms(value) for value in ids} == {RFC_V7_MS}
    counters = [counter_42(value) for value in ids]
    assert counters == list(range(counters[0], counters[0] + 1000))


@pytest.mark.unit
def test_new_millisecond_reseeds_the_counter() -> None:
    generate = UUIDv7Generator(clock_ms=scripted_clock(5, 5, 6), entropy=rng(2).randbytes)
    first, second, third = generate(), generate(), generate()
    assert counter_42(second) == counter_42(first) + 1
    assert uuid7_unix_ts_ms(third) == 6
    # A fresh tick draws a new random counter, so the next value is not guessable as +1.
    assert counter_42(third) != counter_42(second) + 1
    # Seeding leaves the counter's top bit zero as a rollover guard.
    assert counter_42(first) >> 41 == 0
    assert counter_42(third) >> 41 == 0


@pytest.mark.unit
def test_counter_rollover_advances_the_timestamp_instead_of_wrapping() -> None:
    generate = UUIDv7Generator(
        clock_ms=lambda: 1000, entropy=lambda n: b"\xff" * n, counter_bits=12
    )
    ids = [generate() for _ in range(2050)]
    # Seed 0x7ff (top bit guarded), 2048 more values fit in 12 bits, the 2050th overflows.
    assert [uuid7_unix_ts_ms(value) for value in ids[-2:]] == [1000, 1001]
    # The clock still reads 1000: later IDs stay on 1001 instead of drifting further ahead.
    ids += [generate() for _ in range(10)]
    assert {uuid7_unix_ts_ms(value) for value in ids[-10:]} == {1001}
    assert ids == sorted(ids)
    assert len(set(ids)) == 2060


@pytest.mark.unit
def test_clock_regression_reuses_the_last_timestamp() -> None:
    generate = UUIDv7Generator(
        clock_ms=scripted_clock(10_000, 10_000, 5_000), entropy=rng(3).randbytes
    )
    first, second, after_rollback = generate(), generate(), generate()
    assert first < second < after_rollback
    assert uuid7_unix_ts_ms(after_rollback) == 10_000


@pytest.mark.unit
def test_counter_bits_outside_rfc_guidance_are_rejected() -> None:
    with pytest.raises(ValueError, match="between 12 and 42"):
        UUIDv7Generator(clock_ms=lambda: 0, counter_bits=43)
    with pytest.raises(ValueError, match="between 12 and 42"):
        UUIDv7Generator(clock_ms=lambda: 0, counter_bits=11)


@pytest.mark.unit
def test_lock_serializes_the_read_modify_write_across_threads() -> None:
    inside: list[int] = []
    overlaps: list[int] = []
    guard = threading.Lock()

    def slow_clock() -> int:
        with guard:
            inside.append(1)
            overlaps.append(len(inside))
        # Sleeping hands the CPU to other threads in the middle of the read-modify-write.
        time.sleep(0.001)
        with guard:
            inside.pop()
        return RFC_V7_MS

    generate = UUIDv7Generator(clock_ms=slow_clock, entropy=rng(10).randbytes)
    start = threading.Barrier(8)
    per_thread: list[list[uuid.UUID]] = [[] for _ in range(8)]

    def worker(out: list[uuid.UUID]) -> None:
        start.wait()
        out.extend(generate() for _ in range(25))

    threads = [threading.Thread(target=worker, args=(out,)) for out in per_thread]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(overlaps) == 1
    assert all(ids == sorted(ids) for ids in per_thread)
    counters = sorted(counter_42(value) for ids in per_thread for value in ids)
    assert counters == list(range(counters[0], counters[0] + 200))


@pytest.mark.unit
def test_stdlib_uuid7_and_uuid_time_track_the_python_version() -> None:
    if sys.version_info >= (3, 14):
        ids = [uuid.uuid7() for _ in range(10_000)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)
        assert all(value.version == 7 for value in ids)
        # 3.14 reads `UUID.time` as the 48-bit Unix milliseconds for version 7.
        assert RFC_V7.time == RFC_V7_MS
    else:
        assert not hasattr(uuid, "uuid7")
        # 3.12 and 3.13 reinterpret the bits with the version 1 layout.
        assert RFC_V7.time != RFC_V7_MS


@pytest.mark.unit
def test_bytes_hex_and_base32_all_sort_in_creation_order() -> None:
    generate = UUIDv7Generator(clock_ms=stepping_clock(RFC_V7_MS), entropy=rng(5).randbytes)
    created = [generate() for _ in range(300)]
    shuffled = rng(6).sample(created, len(created))
    assert sorted(shuffled, key=lambda value: value.bytes) == created
    assert sorted(shuffled, key=str) == created
    assert sorted(shuffled, key=lambda value: encode_base32(value.int)) == created


@pytest.mark.unit
def test_bytes_le_storage_destroys_time_order() -> None:
    generate = UUIDv7Generator(clock_ms=stepping_clock(RFC_V7_MS), entropy=rng(7).randbytes)
    created = [generate() for _ in range(300)]
    # Microsoft GUID byte order swaps the first three fields, including timestamp bytes.
    assert sorted(created, key=lambda value: value.bytes_le) != created


@pytest.mark.unit
@pytest.mark.parametrize(("text", "prefix", "expected"), TYPEID_VALID, ids=lambda v: str(v)[:12])
def test_typeid_spec_valid_vectors_round_trip(text: str, prefix: str, expected: str) -> None:
    parsed = PublicId.parse(text)
    assert parsed.prefix == prefix
    assert parsed.value == uuid.UUID(expected)
    assert str(parsed) == text


@pytest.mark.unit
@pytest.mark.parametrize("text", TYPEID_INVALID)
def test_typeid_spec_invalid_vectors_are_rejected(text: str) -> None:
    with pytest.raises(InvalidIdError):
        PublicId.parse(text)


@pytest.mark.unit
def test_wrong_type_prefix_is_a_distinct_error() -> None:
    customer = PublicId("cus", RFC_V7)
    with pytest.raises(WrongTypeError, match="expected a 'ord' ID"):
        PublicId.parse(str(customer), expected_prefix="ord")


@pytest.mark.unit
def test_lenient_crockford_decoding_aliases_distinct_strings() -> None:
    canonical = "01h455vb4pex5vsknk084sn02q"
    aliases = [
        "01H455VB4PEX5VSKNK084SN02Q",
        "o1h455vb4pex5vsknk084sn02q",
        "0lh455vb4pex5vsknk084sn02q",
        "01h455vb-4pex5vsk-nk084sn02q",
    ]
    value = decode_base32(canonical)
    assert {decode_crockford_lenient(alias) for alias in aliases} == {value}
    for alias in aliases:
        with pytest.raises(InvalidIdError):
            decode_base32(alias)


@pytest.mark.unit
def test_ulid_view_of_a_uuidv7_shares_the_48_bit_timestamp() -> None:
    text = ulid_text(RFC_V7)
    assert len(text) == 26
    assert text == text.upper()
    # The first 10 symbols carry 50 bits: two zero pad bits and unix_ts_ms.
    assert decode_crockford_lenient(text[:10]) == RFC_V7_MS
    assert decode_base32(text.lower()) == RFC_V7.int


@pytest.mark.unit
def test_public_v7_id_discloses_creation_time_and_v4_does_not() -> None:
    order = PublicId("ord", uuid7_from_parts(RFC_V7_MS, 0, 0))
    leaked = PublicId.parse(str(order)).value
    assert uuid7_unix_ts_ms(leaked) == RFC_V7_MS
    opaque = PublicId("ord", uuid4_from_bytes(rng(8).randbytes(16)))
    with pytest.raises(ValueError, match="not an RFC 9562 UUIDv7"):
        uuid7_unix_ts_ms(PublicId.parse(str(opaque)).value)


def v4_keys(count: int, seed: int) -> list[bytes]:
    entropy = rng(seed)
    return [uuid4_from_bytes(entropy.randbytes(16)).bytes for _ in range(count)]


def v7_keys(count: int, start_ms: int, seed: int) -> list[bytes]:
    generate = UUIDv7Generator(clock_ms=stepping_clock(start_ms), entropy=rng(seed).randbytes)
    return [generate().bytes for _ in range(count)]


@pytest.mark.unit
@pytest.mark.parametrize("layout", ["clustered", "heap_plus_index"])
def test_random_keys_dirty_far_more_pages_per_batch(tmp_path: Path, layout: Layout) -> None:
    v4 = measure_batch_insert(tmp_path / "v4.db", v4_keys(20_000, 1), v4_keys(500, 2), layout)
    start = RFC_V7_MS
    v7 = measure_batch_insert(
        tmp_path / "v7.db", v7_keys(20_000, start, 1), v7_keys(500, start + 20_000, 2), layout
    )
    print(f"\n{layout}: v4 {v4} v7 {v7}")
    assert v4.wal_frames >= 5 * v7.wal_frames
    # SQLite rebalances index pages across three siblings, so page counts stay close.
    assert abs(v4.key_leaf_pages - v7.key_leaf_pages) <= 0.1 * v7.key_leaf_pages


@pytest.mark.unit
def test_random_key_cost_grows_with_the_table_and_time_ordered_cost_does_not(
    tmp_path: Path,
) -> None:
    costs: dict[str, list[int]] = {"v4": [], "v7": []}
    for size in (2_000, 20_000):
        v4 = measure_batch_insert(tmp_path / f"v4-{size}.db", v4_keys(size, 3), v4_keys(500, 4))
        start = RFC_V7_MS
        v7 = measure_batch_insert(
            tmp_path / f"v7-{size}.db", v7_keys(size, start, 3), v7_keys(500, start + size, 4)
        )
        costs["v4"].append(v4.wal_frames)
        costs["v7"].append(v7.wal_frames)
    print(f"\nwal frames for 500 inserts at 2k and 20k rows: {costs}")
    assert costs["v4"][1] >= 4 * costs["v4"][0]
    assert costs["v7"][1] <= costs["v7"][0] + 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_mints_typed_ids_and_rejects_wrong_type_aliases_and_unknowns() -> None:
    generate = UUIDv7Generator(clock_ms=stepping_clock(RFC_V7_MS), entropy=rng(9).randbytes)
    async with client_for(build_app(generate)) as client:
        created = await client.post("/v1/orders", json={"sku": "book-1"})
        assert created.status_code == 201
        order_id = created.json()["id"]
        assert order_id.startswith("ord_")
        assert created.headers["location"] == f"/v1/orders/{order_id}"
        assert (await client.get(created.headers["location"])).json()["sku"] == "book-1"

        suffix = order_id.removeprefix("ord_")
        wrong_type = await client.get(f"/v1/orders/cus_{suffix}")
        assert wrong_type.status_code == 400
        assert "expected a 'ord' ID" in wrong_type.json()["error"]["message"]

        # Same 128 bits, non-canonical text: rejected, never served as an alias.
        alias = await client.get(f"/v1/orders/ord_{suffix.upper()}")
        assert alias.status_code == 400

        unknown = PublicId("ord", uuid7_from_parts(RFC_V7_MS, 1, 1))
        missing = await client.get(f"/v1/orders/{unknown}")
        assert missing.status_code == 404
        assert missing.json()["error"]["status"] == "NOT_FOUND"
