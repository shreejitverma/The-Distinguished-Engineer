"""Opaque page tokens: strict base64url, HMAC and AEAD integrity, and binding to the query."""

from __future__ import annotations

import base64
import hmac
import struct
import time
import warnings
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import itsdangerous
import pytest

from api_labs.f05_pagination_filtering_and_query_patterns.opaque_page_token import (
    DEFAULT_TTL_SECONDS,
    MAX_PAGE_SIZE,
    MAX_TOKEN_CHARS,
    AeadPageTokens,
    HmacPageTokens,
    InvalidPageToken,
    Keyring,
    Order,
    Position,
    QueryContext,
    b64url_decode_strict,
    b64url_encode,
    build_app,
    select_page,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from api_labs.f05_pagination_filtering_and_query_patterns.opaque_page_token import (
        PageTokenCodec,
    )

NOW = 1_800_000_000
KEY_1 = bytes(range(32))
KEY_2 = bytes(range(32, 64))
CONTEXT = QueryContext("users/alice", "stores/main", 'status = "open"', "create_time desc")
POSITION = Position(create_time=1_700_000_123, row_id=4242)


def _clock() -> int:
    return NOW


def _codec(
    kind: str, keyring: Keyring | None = None, purpose: str = "orders.list"
) -> HmacPageTokens | AeadPageTokens:
    ring = keyring or Keyring({1: KEY_1}, current=1)
    if kind == "hmac":
        return HmacPageTokens(ring, purpose, clock=_clock)
    return AeadPageTokens(ring, purpose, clock=_clock)


CODECS = ["hmac", "aead"]


def _lenient_decode(text: str) -> bytes:
    """What itsdangerous 2.2.0 `base64_decode` does: repair padding, then decode leniently."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # CPython 3.15 warns about '+' and '/'
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# Encoding


@pytest.mark.unit
def test_b64url_is_unpadded_url_unreserved_and_round_trips() -> None:
    for size in range(65):
        raw = bytes((i * 151 + size) % 256 for i in range(size))
        token = b64url_encode(raw)
        assert "=" not in token
        assert len(token) == (4 * size + 2) // 3
        assert quote(token, safe="") == token  # RFC 3986 unreserved: no percent-encoding
        assert b64url_decode_strict(token) == raw


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spelling", "reason"),
    [
        ("+_-_AAE", "not_base64url"),  # standard-alphabet '+' for '-'
        ("-/-_AAE", "not_base64url"),  # standard-alphabet '/' for '_'
        ("-_-_AAE....", "not_base64url"),  # unreserved junk survives any URL untouched
        ("-_-_AAE=", "not_base64url"),  # padding
        ("-_-_\r\n\r\nAAE", "not_base64url"),  # MIME-style line breaks
        ("-_-_AAF", "non_canonical"),  # non-zero pad bits (RFC 4648 Section 3.5)
        ("-_-_AAG", "non_canonical"),
    ],
)
def test_strict_decoder_rejects_spellings_the_python_lenient_decoder_accepts(
    spelling: str, reason: str
) -> None:
    raw = b"\xfb\xff\xbf\x00\x01"
    assert b64url_encode(raw) == "-_-_AAE"
    assert _lenient_decode(spelling) == raw  # seven strings, one byte sequence
    with pytest.raises(InvalidPageToken) as caught:
        b64url_decode_strict(spelling)
    assert caught.value.reason == reason


@pytest.mark.unit
def test_strict_decoder_rejects_impossible_length_and_oversized_input() -> None:
    with pytest.raises(InvalidPageToken) as impossible:
        b64url_decode_strict("AAAAA")  # 4k+1 characters never encode whole bytes
    assert impossible.value.reason == "not_base64url"
    with pytest.raises(InvalidPageToken) as oversized:
        b64url_decode_strict("A" * (MAX_TOKEN_CHARS + 4))
    assert oversized.value.reason == "too_long"
    assert len(b64url_decode_strict("A" * MAX_TOKEN_CHARS)) == MAX_TOKEN_CHARS * 3 // 4


@pytest.mark.unit
def test_token_length_cap_is_the_documented_512_characters() -> None:
    assert MAX_TOKEN_CHARS == 512  # the note's Numbers table quotes this value
    assert len(b64url_decode_strict("A" * 512)) == 384
    with pytest.raises(InvalidPageToken) as oversized:
        b64url_decode_strict("A" * 516)
    assert oversized.value.reason == "too_long"


@pytest.mark.unit
def test_itsdangerous_accepts_a_non_canonical_signature_spelling() -> None:
    signer = itsdangerous.Signer(KEY_1)
    value, signature = signer.sign(b"position").split(b".")
    assert len(signature) == 27  # 20-byte HMAC-SHA1 -> 27 chars, 2 pad bits in the last
    alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(signature[-1:])
    twin = signature[:-1] + alphabet[last ^ 1 : (last ^ 1) + 1]
    assert twin != signature
    assert signer.unsign(value + b"." + twin) == b"position"  # second valid spelling


# Layout and confidentiality


@pytest.mark.unit
def test_hmac_token_is_56_chars_and_its_position_is_readable() -> None:
    token = _codec("hmac").mint(POSITION, CONTEXT)
    raw = b64url_decode_strict(token)
    assert (len(token), len(raw)) == (56, 42)
    assert raw[:2] == b"\x01\x01"  # version 1, key id 1
    assert struct.unpack(">Qqq", raw[2:26]) == (NOW, POSITION.create_time, POSITION.row_id)


@pytest.mark.unit
def test_aead_token_is_72_chars_and_hides_the_position() -> None:
    token = _codec("aead").mint(POSITION, CONTEXT)
    raw = b64url_decode_strict(token)
    assert (len(token), len(raw)) == (72, 54)
    assert raw[:2] == b"\x02\x01"
    assert struct.pack(">q", POSITION.row_id) not in raw
    assert struct.pack(">q", POSITION.create_time) not in raw
    fixed = AeadPageTokens(
        Keyring({1: KEY_1}, current=1), "orders.list", clock=_clock, nonce_source=bytes
    )
    one = b64url_decode_strict(fixed.mint(POSITION, CONTEXT))
    other = b64url_decode_strict(fixed.mint(replace(POSITION, row_id=POSITION.row_id ^ 1), CONTEXT))
    assert sum(a != b for a, b in zip(one[14:38], other[14:38], strict=True)) > 8  # not XOR


@pytest.mark.unit
def test_aead_random_nonces_unlink_tokens_and_a_fixed_nonce_links_them() -> None:
    random_codec = AeadPageTokens(Keyring({1: KEY_1}, current=1), "orders.list", clock=_clock)
    first, second = (random_codec.mint(POSITION, CONTEXT) for _ in range(2))
    assert first != second
    assert random_codec.open(first, CONTEXT) == random_codec.open(second, CONTEXT) == POSITION

    def zero_nonce(size: int) -> bytes:
        return b"\x00" * size

    fixed = replace(random_codec, nonce_source=zero_nonce)
    assert fixed.mint(POSITION, CONTEXT) == fixed.mint(POSITION, CONTEXT)  # RFC 8452 Section 9


# Integrity and binding


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
def test_round_trip_returns_the_minted_position(kind: str) -> None:
    codec = _codec(kind)
    assert codec.open(codec.mint(POSITION, CONTEXT), CONTEXT) == POSITION


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
def test_every_single_bit_flip_is_rejected(kind: str) -> None:
    codec = _codec(kind)
    raw = b64url_decode_strict(codec.mint(POSITION, CONTEXT))
    reasons: set[str] = set()
    for index in range(len(raw)):
        for bit in range(8):
            mutated = bytearray(raw)
            mutated[index] ^= 1 << bit
            with pytest.raises(InvalidPageToken) as caught:
                codec.open(b64url_encode(bytes(mutated)), CONTEXT)
            reasons.add(caught.value.reason)
    assert reasons == {"unsupported_version", "unknown_key", "bad_tag"}


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
@pytest.mark.parametrize(
    "changed",
    [
        {"principal": "users/bob"},
        {"parent": "stores/annex"},
        {"filter": 'status = "closed"'},
        {"order_by": "create_time asc"},
    ],
)
def test_token_is_bound_to_every_query_field(kind: str, changed: dict[str, str]) -> None:
    codec = _codec(kind)
    token = codec.mint(POSITION, CONTEXT)
    with pytest.raises(InvalidPageToken) as caught:
        codec.open(token, replace(CONTEXT, **changed))
    assert caught.value.reason == "bad_tag"


@pytest.mark.unit
def test_context_digest_is_unambiguous_where_a_joined_string_collides() -> None:
    left = QueryContext("users/a|b", "stores/x", "", "create_time desc")
    right = QueryContext("users/a", "b|stores/x", "", "create_time desc")
    assert "|".join([left.principal, left.parent]) == "|".join([right.principal, right.parent])
    assert left.digest() != right.digest()


@pytest.mark.unit
def test_context_digests_differ_for_quoting_and_unicode_edge_cases() -> None:
    awkward = ["", "a", 'a"', "a\\", "a,b", '"a","b"', "a\u0000", chr(0xE9), "e" + chr(0x301)]
    contexts = [
        QueryContext(p, q, f, "create_time desc") for p in awkward for q in awkward for f in awkward
    ]
    assert len({context.digest() for context in contexts}) == len(contexts)


@pytest.mark.unit
def test_keyring_snapshots_keys_so_later_edits_cannot_skip_codec_checks() -> None:
    shared = {1: KEY_1}
    keyring = Keyring(shared, current=1)
    codec = AeadPageTokens(keyring, "orders.list", clock=_clock)
    token = codec.mint(POSITION, CONTEXT)
    shared[1] = b"short"
    shared[300] = KEY_2
    assert dict(keyring.keys) == {1: KEY_1}
    assert codec.open(token, CONTEXT) == POSITION


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
def test_purpose_label_stops_cross_endpoint_replay(kind: str) -> None:
    token = _codec(kind, purpose="orders.list").mint(POSITION, CONTEXT)
    with pytest.raises(InvalidPageToken) as caught:
        _codec(kind, purpose="invoices.list").open(token, CONTEXT)
    assert caught.value.reason == "bad_tag"


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
def test_rotated_key_verifies_until_removed(kind: str) -> None:
    old_token = _codec(kind, Keyring({1: KEY_1}, current=1)).mint(POSITION, CONTEXT)
    rotated = _codec(kind, Keyring({1: KEY_1, 2: KEY_2}, current=2))
    assert rotated.open(old_token, CONTEXT) == POSITION
    assert b64url_decode_strict(rotated.mint(POSITION, CONTEXT))[1] == 2
    retired = _codec(kind, Keyring({2: KEY_2}, current=2))
    with pytest.raises(InvalidPageToken) as caught:
        retired.open(old_token, CONTEXT)
    assert caught.value.reason == "unknown_key"


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
def test_expiry_boundary_is_ttl_seconds(kind: str) -> None:
    token = _codec(kind).mint(POSITION, CONTEXT)
    at_ttl = replace(_codec(kind), clock=lambda: NOW + DEFAULT_TTL_SECONDS)
    assert at_ttl.open(token, CONTEXT) == POSITION
    past_ttl = replace(_codec(kind), clock=lambda: NOW + DEFAULT_TTL_SECONDS + 1)
    with pytest.raises(InvalidPageToken) as caught:
        past_ttl.open(token, CONTEXT)
    assert caught.value.reason == "expired"


@pytest.mark.unit
@pytest.mark.parametrize("kind", CODECS)
def test_truncated_and_headerless_tokens_are_bad_length(kind: str) -> None:
    codec = _codec(kind)
    raw = b64url_decode_strict(codec.mint(POSITION, CONTEXT))
    for broken in (raw[:1], raw[:-1], raw + b"\x00"):
        with pytest.raises(InvalidPageToken) as caught:
            codec.open(b64url_encode(broken), CONTEXT)
        assert caught.value.reason == "bad_length"


@pytest.mark.unit
def test_default_clock_stamps_wall_time() -> None:
    before = int(time.time())
    codec = HmacPageTokens(Keyring({1: KEY_1}, current=1), "orders.list")
    token = codec.mint(POSITION, CONTEXT)
    (issued_at,) = struct.unpack(">Q", b64url_decode_strict(token)[2:10])
    assert before <= issued_at <= int(time.time())
    assert codec.open(token, CONTEXT) == POSITION


@pytest.mark.unit
def test_hmac_tag_comparison_goes_through_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    real: Callable[[bytes, bytes], bool] = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((len(a), len(b)))
        return real(a, b)

    codec = _codec("hmac")
    token = codec.mint(POSITION, CONTEXT)
    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert codec.open(token, CONTEXT) == POSITION
    assert calls == [(16, 16)]


@pytest.mark.unit
def test_codecs_refuse_weak_or_wrong_size_keys() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        HmacPageTokens(Keyring({1: b"k" * 31}, current=1), "orders.list")
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        AeadPageTokens(Keyring({1: b"k" * 16}, current=1), "orders.list")
    with pytest.raises(ValueError, match="current key id"):
        Keyring({1: KEY_1}, current=2)


# Why binding matters


def _rows() -> list[Order]:
    rows: list[Order] = []
    for row_id in range(1, 25):
        rows.append(
            Order(
                row_id=row_id,
                store="main",
                owner="users/alice" if row_id % 3 else "users/bob",
                status="open" if row_id % 2 else "closed",
                create_time=1_700_000_000 + row_id // 2,  # pairs share a timestamp
            )
        )
    return rows


@pytest.mark.unit
@pytest.mark.parametrize("descending", [True, False])
@pytest.mark.parametrize("page_size", range(1, 9))
def test_walk_splits_timestamp_ties_across_pages_without_loss(
    descending: bool, page_size: int
) -> None:
    rows = _rows()
    scope: dict[str, Any] = {"principal": "users/alice", "store": "main", "status": None}
    served: list[int] = []
    after: Position | None = None
    for _ in range(len(rows) + 1):
        page, after = select_page(
            rows, descending=descending, after=after, page_size=page_size, **scope
        )
        served.extend(row.row_id for row in page)
        if after is None:
            break
    assert served == _expected("users/alice", None, descending)
    assert len(served) == 16  # includes tied pairs such as (1_700_000_002, 4) and (.., 5)


@pytest.mark.unit
def test_unbound_cursor_reused_with_a_new_filter_silently_skips_rows() -> None:
    rows = _rows()
    scope: dict[str, Any] = {"principal": "users/alice", "store": "main", "descending": True}
    _, cursor = select_page(rows, status="open", after=None, page_size=3, **scope)
    assert cursor == Position(create_time=1_700_000_008, row_id=17)
    every_closed, _ = select_page(rows, status="closed", after=None, page_size=99, **scope)
    reused, _ = select_page(rows, status="closed", after=cursor, page_size=99, **scope)
    never_served = [row.row_id for row in every_closed if row not in reused]
    assert never_served == [22, 20]  # newer closed rows, no error, no gap marker


# HTTP surface


async def _get(app: Any, path: str, principal: str = "users/alice", **params: str) -> Any:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, params=params, headers={"x-principal": principal})
    return response


async def _walk(app: Any, principal: str, **params: str) -> tuple[list[int], int]:
    ids: list[int] = []
    pages = 0
    token = ""
    while True:
        query = dict(params, page_token=token) if token else dict(params)
        response = await _get(app, "/v1/stores/main/orders", principal, **query)
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(order["id"] for order in body["orders"])
        pages += 1
        token = body["next_page_token"]
        if token == "" or pages > 50:
            return ids, pages


def _expected(principal: str, status: str | None, descending: bool) -> list[int]:
    rows = [r for r in _rows() if r.owner == principal and (status is None or r.status == status)]
    rows.sort(key=lambda r: (r.create_time, r.row_id), reverse=descending)
    return [r.row_id for r in rows]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CODECS)
@pytest.mark.parametrize("order_by", ["create_time desc", "create_time asc"])
async def test_walk_returns_each_owned_row_once_and_ends_with_an_empty_token(
    kind: str, order_by: str
) -> None:
    app = build_app(_rows(), _codec(kind))
    ids, pages = await _walk(
        app, "users/alice", page_size="3", filter='status = "open"', order_by=order_by
    )
    assert ids == _expected("users/alice", "open", descending=order_by.endswith("desc"))
    assert pages == 3  # 8 open rows: 3 + 3 + 2, the last page carries an empty token


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CODECS)
@pytest.mark.parametrize(
    ("path", "changed"),
    [
        ("/v1/stores/main/orders", {"filter": 'status = "closed"'}),
        ("/v1/stores/main/orders", {"order_by": "create_time asc"}),
        ("/v1/stores/annex/orders", {}),
    ],
)
async def test_changed_parameter_with_an_old_token_is_400_invalid_argument(
    kind: str, path: str, changed: dict[str, str]
) -> None:
    app = build_app(_rows(), _codec(kind))
    first = {"page_size": "3", "filter": 'status = "open"', "order_by": "create_time desc"}
    token = (await _get(app, "/v1/stores/main/orders", **first)).json()["next_page_token"]
    response = await _get(app, path, **dict(first, page_token=token, **changed))
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert "orders" not in response.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_equivalent_filter_spelling_keeps_the_token_valid() -> None:
    app = build_app(_rows(), _codec("hmac"))
    first = await _get(app, "/v1/stores/main/orders", page_size="3", filter='status="open"')
    token = first.json()["next_page_token"]
    second = await _get(
        app, "/v1/stores/main/orders", page_size="3", filter=' status = "open" ', page_token=token
    )
    assert second.status_code == 200
    assert [o["id"] for o in second.json()["orders"]] == _expected("users/alice", "open", True)[3:6]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_page_size_may_change_between_pages() -> None:
    app = build_app(_rows(), _codec("hmac"))
    first = await _get(app, "/v1/stores/main/orders", page_size="2")
    token = first.json()["next_page_token"]
    second = await _get(app, "/v1/stores/main/orders", page_size="5", page_token=token)
    assert second.status_code == 200
    assert [o["id"] for o in second.json()["orders"]] == _expected("users/alice", None, True)[2:7]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_another_principal_cannot_replay_a_token_and_sees_only_own_rows() -> None:
    app = build_app(_rows(), _codec("hmac"))
    alice_token = (await _get(app, "/v1/stores/main/orders", page_size="2")).json()[
        "next_page_token"
    ]
    replay = await _get(
        app, "/v1/stores/main/orders", "users/bob", page_size="2", page_token=alice_token
    )
    assert replay.status_code == 400
    bob_ids, _ = await _walk(app, "users/bob", page_size="2")
    assert bob_ids == _expected("users/bob", None, descending=True)
    assert all(row.owner == "users/bob" for row in _rows() if row.row_id in bob_ids)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    ["not a token", "-_-_AAE", "A" * (MAX_TOKEN_CHARS + 4), "AAAA" * 10 + "%2B", chr(0x0661) * 8],
)
async def test_hostile_tokens_are_400_never_500(token: str) -> None:
    app = build_app(_rows(), _codec("aead"))
    response = await _get(app, "/v1/stores/main/orders", page_token=token)
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"filter": 'status = "pending"'},
        {"filter": "status = open"},
        {"order_by": "price desc"},
        {"page_size": "1_0"},
        {"page_size": chr(0x0663)},
        {"page_size": "ten"},
    ],
)
async def test_unparseable_query_parameters_are_400(params: dict[str, str]) -> None:
    response = await _get(build_app(_rows(), _codec("hmac")), "/v1/stores/main/orders", **params)
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_page_size_negative_is_400_and_oversized_is_coerced() -> None:
    many = [
        Order(row_id=i, store="main", owner="users/alice", status="open", create_time=i)
        for i in range(1, MAX_PAGE_SIZE + 30)
    ]
    app = build_app(many, _codec("hmac"))
    negative = await _get(app, "/v1/stores/main/orders", page_size="-1")
    assert negative.status_code == 400
    default = await _get(app, "/v1/stores/main/orders")
    assert len(default.json()["orders"]) == 50
    coerced = await _get(app, "/v1/stores/main/orders", page_size="5000")
    assert len(coerced.json()["orders"]) == MAX_PAGE_SIZE
    assert coerced.json()["next_page_token"] != ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_principal_is_401() -> None:
    codec: PageTokenCodec = _codec("hmac")
    response = await _get(build_app(_rows(), codec), "/v1/stores/main/orders", principal="")
    assert response.status_code == 401
