"""Presigned direct-to-storage uploads: SigV4 vectors, signed constraints, and finalization."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio

from api_labs.f06_bulk_long_running_and_error_contracts import presigned_upload
from api_labs.f06_bulk_long_running_and_error_contracts.presigned_upload import (
    EVENT_SIGNATURE_HEADER,
    MAX_EXPIRES_SECONDS,
    Credentials,
    ObjectStore,
    PolicyViolation,
    UploadService,
    UploadState,
    build_event,
    canonical_query,
    canonical_request,
    check_policy,
    event_keys,
    presign_url,
    sign_event,
    sign_policy,
    uri_encode,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

# AWS example credentials, published for testing signature code.
AWS_EXAMPLE = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
AWS_GET_URL = (
    "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
)
AWS_POST_POLICY_B64 = (
    "eyAiZXhwaXJhdGlvbiI6ICIyMDE1LTEyLTMwVDEyOjAwOjAwLjAwMFoiLA0KICAiY29uZGl0aW9ucyI6IFsNCiAgICB7"
    "ImJ1Y2tldCI6ICJzaWd2NGV4YW1wbGVidWNrZXQifSwNCiAgICBbInN0YXJ0cy13aXRoIiwgIiRrZXkiLCAidXNlci91"
    "c2VyMS8iXSwNCiAgICB7ImFjbCI6ICJwdWJsaWMtcmVhZCJ9LA0KICAgIHsic3VjY2Vzc19hY3Rpb25fcmVkaXJlY3Qi"
    "OiAiaHR0cDovL3NpZ3Y0ZXhhbXBsZWJ1Y2tldC5zMy5hbWF6b25hd3MuY29tL3N1Y2Nlc3NmdWxfdXBsb2FkLmh0bWwi"
    "fSwNCiAgICBbInN0YXJ0cy13aXRoIiwgIiRDb250ZW50LVR5cGUiLCAiaW1hZ2UvIl0sDQogICAgeyJ4LWFtei1tZXRh"
    "LXV1aWQiOiAiMTQzNjUxMjM2NTEyNzQifSwNCiAgICB7IngtYW16LXNlcnZlci1zaWRlLWVuY3J5cHRpb24iOiAiQUVT"
    "MjU2In0sDQogICAgWyJzdGFydHMtd2l0aCIsICIkeC1hbXotbWV0YS10YWciLCAiIl0sDQoNCiAgICB7IngtYW16LWNy"
    "ZWRlbnRpYWwiOiAiQUtJQUlPU0ZPRE5ON0VYQU1QTEUvMjAxNTEyMjkvdXMtZWFzdC0xL3MzL2F3czRfcmVxdWVzdCJ9"
    "LA0KICAgIHsieC1hbXotYWxnb3JpdGhtIjogIkFXUzQtSE1BQy1TSEEyNTYifSwNCiAgICB7IngtYW16LWRhdGUiOiAi"
    "MjAxNTEyMjlUMDAwMDAwWiIgfQ0KICBdDQp9"
)
START = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
EVENT_SECRET = b"e" * 32


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Rig:
    clock: Clock
    store: ObjectStore
    service: UploadService
    api: httpx.AsyncClient
    storage: httpx.AsyncClient

    async def create(self, bearer: str = "t-alice", **payload: Any) -> httpx.Response:
        body = {"content_type": "image/png", "size_bytes": len(PNG)} | payload
        return await self.api.post(
            "/v1/uploads", json=body, headers={"Authorization": f"Bearer {bearer}"}
        )

    async def deliver(self, body: bytes, signature: str | None = None) -> httpx.Response:
        headers = {EVENT_SIGNATURE_HEADER: signature or sign_event(EVENT_SECRET, body)}
        return await self.api.post("/internal/storage-events", content=body, headers=headers)

    async def drain(self) -> None:
        while self.store.outbox:
            response = await self.deliver(self.store.outbox.pop(0))
            assert response.status_code == 204

    async def state(self, upload_id: str) -> dict[str, Any]:
        response = await self.api.get(
            f"/v1/uploads/{upload_id}", headers={"Authorization": "Bearer t-alice"}
        )
        return cast("dict[str, Any]", response.json())


def _make_rig(*, enforce_post_policy: bool = True) -> tuple[Clock, ObjectStore, UploadService]:
    clock = Clock()
    store = ObjectStore(
        bucket="media",
        host="media.storage.test",
        region="eu-west-1",
        credentials=Credentials("AKIDLAB", "s" * 40),
        clock=clock,
        enforce_post_policy=enforce_post_policy,
    )
    ids = iter(f"u{n:04d}" for n in range(1, 100))
    service = UploadService(
        store=store,
        users={"t-alice": "alice", "t-bob": "bob"},
        event_secret=EVENT_SECRET,
        clock=clock,
        ttl_seconds=900,
        grace_seconds=600,
        max_bytes=1024,
        id_factory=lambda: next(ids),
    )
    return clock, store, service


@contextlib.asynccontextmanager
async def _open_rig(*, enforce_post_policy: bool = True) -> AsyncGenerator[Rig]:
    clock, store, service = _make_rig(enforce_post_policy=enforce_post_policy)
    api_transport = httpx.ASGITransport(app=service.app)
    storage_transport = httpx.ASGITransport(app=store.app)
    async with (
        httpx.AsyncClient(transport=api_transport, base_url="http://test") as api,
        httpx.AsyncClient(transport=storage_transport, base_url="http://test") as storage,
    ):
        yield Rig(clock, store, service, api, storage)


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    async with _open_rig() as opened:
        yield opened


@pytest_asyncio.fixture
async def lax_rig() -> AsyncIterator[Rig]:
    """A rig whose store skips POST policy checks: the GHSA-w5fh-f8xh-5x3p shape."""
    async with _open_rig(enforce_post_policy=False) as opened:
        yield opened


async def _put(rig: Rig, upload: dict[str, Any], body: bytes, **overrides: str) -> httpx.Response:
    headers = cast("dict[str, str]", upload["headers"]) | overrides
    return await rig.storage.put(cast("str", upload["url"]), content=body, headers=headers)


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


def _path(url: str) -> str:
    return urlsplit(url).path


def _path_raw(url: str) -> str:
    return url.split("?", 1)[0].removeprefix("https://media.storage.test")


def _with_query(url: str, **changes: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query)) | changes
    encoded = "&".join(f"{k}={uri_encode(v)}" for k, v in query.items())
    return urlunsplit(parts._replace(query=encoded))


# --- Signature Version 4 against the AWS published examples -------------------------------


@pytest.mark.unit
def test_presign_reproduces_the_aws_documented_presigned_get_url() -> None:
    url = presign_url(
        credentials=AWS_EXAMPLE,
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        region="us-east-1",
        now=datetime(2013, 5, 24, tzinfo=UTC),
        expires=86400,
    )
    assert url == AWS_GET_URL


@pytest.mark.unit
def test_canonical_request_hash_matches_the_aws_example() -> None:
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": "AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request",
        "X-Amz-Date": "20130524T000000Z",
        "X-Amz-Expires": "86400",
        "X-Amz-SignedHeaders": "host",
    }
    text = canonical_request(
        "GET", "/test.txt", query, {"Host": " examplebucket.s3.amazonaws.com "}
    )
    assert text.endswith("host:examplebucket.s3.amazonaws.com\n\nhost\nUNSIGNED-PAYLOAD")
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert digest == "3bfa292879f6447bbcda7001decf97f4a54dc650c8942174ae0a9121cf58ad04"


@pytest.mark.unit
def test_post_policy_signature_matches_the_aws_example() -> None:
    policy = base64.b64decode(AWS_POST_POLICY_B64)
    assert b'["starts-with", "$Content-Type", "image/"]' in policy
    signature = sign_policy(AWS_POST_POLICY_B64, AWS_EXAMPLE, "20151229", "us-east-1")
    assert signature == "8afdbf4008c03f22c2cd3cdb72e4afbb1f6a588f3255ac628749a66d7f09699e"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "encode_slash", "expected"),
    [
        ("a b", True, "a%20b"),
        ("a+b", True, "a%2Bb"),
        ("photos/Jan/sample.jpg", False, "photos/Jan/sample.jpg"),
        ("AKID/20130524", True, "AKID%2F20130524"),
        ("-._~", True, "-._~"),
        ("\u00e9", True, "%C3%A9"),
        ("*", True, "%2A"),
    ],
)
def test_uri_encode_follows_the_sigv4_rules(text: str, encode_slash: bool, expected: str) -> None:
    assert uri_encode(text, encode_slash=encode_slash) == expected


@pytest.mark.unit
def test_canonical_query_sorts_by_encoded_name_as_in_the_aws_example() -> None:
    params = {"prefix": "somePrefix", "marker": "someMarker", "max-keys": "20"}
    assert canonical_query(params) == "marker=someMarker&max-keys=20&prefix=somePrefix"


@pytest.mark.unit
@pytest.mark.parametrize("expires", [0, MAX_EXPIRES_SECONDS + 1, -5])
def test_presign_refuses_expiry_outside_one_second_to_seven_days(expires: int) -> None:
    with pytest.raises(ValueError, match="X-Amz-Expires"):
        presign_url(
            credentials=AWS_EXAMPLE,
            method="PUT",
            host="h",
            path="/k",
            region="us-east-1",
            now=START,
            expires=expires,
        )


@pytest.mark.unit
def test_presign_accepts_both_expiry_bounds() -> None:
    for expires in (1, MAX_EXPIRES_SECONDS):
        url = presign_url(
            credentials=AWS_EXAMPLE,
            method="PUT",
            host="h",
            path="/k",
            region="us-east-1",
            now=START,
            expires=expires,
        )
        assert f"X-Amz-Expires={expires}&" in url


# --- Phase 1: the API mints a constrained credential -----------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_returns_201_location_and_a_url_that_signs_the_constraints(rig: Rig) -> None:
    response = await rig.create(filename="cat.png")
    assert response.status_code == 201
    assert response.headers["location"] == "/v1/uploads/u0001"
    body = response.json()
    assert (body["upload_id"], body["state"]) == ("u0001", "pending")
    assert body["expires_at"] == "2026-11-11T12:15:00+00:00"
    url = cast("str", body["upload"]["url"])
    query = _query(url)
    assert query["X-Amz-SignedHeaders"] == "content-length;content-type;host;if-none-match"
    assert query["X-Amz-Expires"] == "900"
    assert _path(url) == "/uploads/alice/u0001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_object_key_is_server_chosen_and_ignores_the_client_filename(rig: Rig) -> None:
    response = await rig.create(filename="../../bob/index.html")
    assert _path(response.json()["upload"]["url"]) == "/uploads/alice/u0001"
    assert rig.service.records["u0001"].filename == "../../bob/index.html"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"content_type": "text/html"}, 400),
        ({"size_bytes": 1025}, 400),
        ({"size_bytes": 0}, 400),
        ({"size_bytes": True}, 400),
        ({"size_bytes": "64"}, 400),
        ({"method": "PATCH"}, 400),
    ],
)
async def test_create_rejects_disallowed_declarations(
    rig: Rig, payload: dict[str, Any], status: int
) -> None:
    response = await rig.create(**payload)
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert rig.service.records == {}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("size_bytes", [1, 1024])
async def test_create_accepts_both_size_bounds(rig: Rig, size_bytes: int) -> None:
    response = await rig.create(size_bytes=size_bytes)
    assert response.status_code == 201
    assert rig.service.records["u0001"].size_bytes == size_bytes


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_requires_a_known_bearer_token(rig: Rig) -> None:
    assert (await rig.create(bearer="t-mallory")).status_code == 401
    bad = await rig.api.post("/v1/uploads", content=b"{", headers={"Authorization": "Bearer t-bob"})
    assert bad.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authorization", "status"),
    [
        ("Basic t-alice", 401),
        ("Token t-alice", 401),
        ("t-alice", 401),
        ("bearer t-alice", 201),  # the scheme is case-insensitive (RFC 9110 Section 11.1)
    ],
    ids=["basic", "token", "bare-token", "lowercase-bearer"],
)
async def test_create_accepts_only_the_bearer_scheme(
    rig: Rig, authorization: str, status: int
) -> None:
    body = {"content_type": "image/png", "size_bytes": len(PNG)}
    response = await rig.api.post(
        "/v1/uploads", json=body, headers={"Authorization": authorization}
    )
    assert response.status_code == status
    assert len(rig.service.records) == (1 if status == 201 else 0)


# --- The store enforces what the signature covers ---------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_phase_put_upload_finalizes_to_active(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    stored = await _put(rig, upload, PNG)
    assert stored.status_code == 200
    assert (await rig.state("u0001"))["state"] == "pending"
    await rig.drain()
    state = await rig.state("u0001")
    etag = stored.headers["etag"]
    assert state == {
        "upload_id": "u0001",
        "state": "active",
        "etag": etag,
        "reason": None,
        "finalized_at": START.isoformat(),
    }


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [{"Content-Type": "text/html"}, {"Content-Length": str(len(PNG) + 1)}],
)
async def test_changing_a_signed_header_breaks_the_signature(
    rig: Rig, override: dict[str, str]
) -> None:
    upload = (await rig.create()).json()["upload"]
    body = PNG + b"x" if "Content-Length" in override else PNG
    response = await _put(rig, upload, body, **override)
    assert response.status_code == 403
    assert response.json()["reason"] == "signature_mismatch"
    assert rig.store.objects == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_that_disagrees_with_the_signed_length_is_rejected(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    response = await _put(rig, upload, PNG + b"extra")
    assert (response.status_code, response.json()["error"]) == (400, "IncompleteBody")
    assert rig.store.objects == {}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"X-Amz-Expires": "604800"}, "signature_mismatch"),
        ({"X-Amz-Expires": "604801"}, "bad_expires"),
        ({"X-Amz-Expires": "0"}, "bad_expires"),
        ({"X-Amz-Date": "20261111T130000Z"}, "signature_mismatch"),
        ({"X-Amz-SignedHeaders": "content-type;host;if-none-match"}, "signature_mismatch"),
        ({"X-Amz-SignedHeaders": "content-length;content-type"}, "bad_credential"),
        ({"X-Amz-Credential": "AKIDLAB/20261111/us-east-1/s3/aws4_request"}, "bad_scope"),
        ({"X-Amz-Credential": "AKIDLAB/20261110/eu-west-1/s3/aws4_request"}, "bad_scope"),
        ({"X-Amz-Credential": "AKIDLAB/20261111/eu-west-1/sqs/aws4_request"}, "bad_scope"),
        ({"X-Amz-Credential": "AKIDLAB/20261111/eu-west-1/s3/aws4"}, "bad_scope"),
        ({"X-Amz-Credential": "AKIDLAB/20261111/eu-west-1/s3"}, "malformed_query"),
        ({"X-Amz-Credential": "AKIDOTHER/20261111/eu-west-1/s3/aws4_request"}, "bad_credential"),
        ({"X-Amz-Algorithm": "AWS4-HMAC-SHA1"}, "bad_scope"),
        ({"X-Amz-Signature": "0" * 64}, "signature_mismatch"),
    ],
)
async def test_any_edited_query_parameter_is_403(
    rig: Rig, changes: dict[str, str], reason: str
) -> None:
    upload = (await rig.create()).json()["upload"]
    upload["url"] = _with_query(upload["url"], **changes)
    response = await _put(rig, upload, PNG)
    assert (response.status_code, response.json()["reason"]) == (403, reason)
    assert rig.store.objects == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitting_a_signed_header_is_403_not_a_silent_overwrite(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    headers = {k: v for k, v in upload["headers"].items() if k != "If-None-Match"}
    response = await rig.storage.put(upload["url"], content=PNG, headers=headers)
    assert (response.status_code, response.json()["reason"]) == (403, "signed_header_missing")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_verifies_regardless_of_query_parameter_order_on_the_wire(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    parts = urlsplit(cast("str", upload["url"]))
    shuffled = "&".join(reversed(parts.query.split("&")))
    upload["url"] = urlunsplit(parts._replace(query=shuffled))
    assert (await _put(rig, upload, PNG)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsigned_non_numeric_content_length_is_400_not_500(rig: Rig) -> None:
    store = rig.store
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/uploads/alice/loose",
        region=store.region,
        now=START,
        expires=60,
    )
    response = await rig.storage.put(url, content=PNG, headers={"Content-Length": "6x"})
    assert (response.status_code, response.json()["error"]) == (400, "IncompleteBody")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_url_works_until_expiry_and_fails_one_second_later(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    rig.clock.now = START + timedelta(seconds=901)
    late = await _put(rig, upload, PNG)
    assert (late.status_code, late.json()["reason"]) == (403, "expired")
    rig.clock.now = START + timedelta(seconds=900)
    assert (await _put(rig, upload, PNG)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_url_is_refused_before_its_x_amz_date(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    rig.clock.now = START - timedelta(seconds=1)
    early = await _put(rig, upload, PNG)
    assert (early.status_code, early.json()["reason"]) == (403, "not_yet_valid")
    assert rig.store.objects == {}
    rig.clock.now = START
    assert (await _put(rig, upload, PNG)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_credential_uses_utc_even_when_the_clock_is_not_utc(rig: Rig) -> None:
    plus_nine = timezone(timedelta(hours=9))
    rig.clock.now = START.astimezone(plus_nine)  # 21:00 local on the same instant
    upload = (await rig.create()).json()["upload"]
    query = _query(upload["url"])
    assert query["X-Amz-Date"] == "20261111T120000Z"
    assert query["X-Amz-Credential"] == "AKIDLAB/20261111/eu-west-1/s3/aws4_request"
    assert (await _put(rig, upload, PNG)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_with_reserved_and_non_ascii_characters_round_trips(rig: Rig) -> None:
    store = rig.store
    key = "uploads/alice/r\u00e9sum\u00e9 v2+final?#.png"
    headers = {"Content-Length": str(len(PNG))}
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/" + key,
        region=store.region,
        now=START,
        expires=60,
        headers=headers,
    )
    assert _path_raw(url) == "/uploads/alice/r%C3%A9sum%C3%A9%20v2%2Bfinal%3F%23.png"
    assert (await rig.storage.put(url, content=PNG, headers=headers)).status_code == 200
    assert list(store.objects) == [key]
    assert event_keys(store.outbox[0]) == [key]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_without_content_type_is_stored_as_binary_octet_stream(rig: Rig) -> None:
    store = rig.store
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/uploads/alice/u0001",
        region=store.region,
        now=START,
        expires=60,
    )
    await rig.create()
    assert (await rig.storage.put(url, content=PNG)).status_code == 200
    assert store.objects["uploads/alice/u0001"].content_type == "binary/octet-stream"
    await rig.drain()
    assert (await rig.state("u0001"))["reason"] == "content_type"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_if_none_match_turns_replay_into_412(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    assert (await _put(rig, upload, PNG)).status_code == 200
    replay = await _put(rig, upload, PNG)
    assert replay.status_code == 412
    assert len(rig.store.outbox) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_url_without_a_signed_if_none_match_replays_and_overwrites(rig: Rig) -> None:
    store = rig.store
    headers = {"Content-Length": str(len(PNG)), "Content-Type": "image/png"}
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/uploads/alice/u0001",
        region=store.region,
        now=START,
        expires=60,
        headers=headers,
    )
    await rig.create()
    first = await rig.storage.put(url, content=PNG, headers=headers)
    await rig.drain()
    assert (await rig.state("u0001"))["state"] == "active"
    other = PNG[:-1] + b"\x01"
    second = await rig.storage.put(url, content=other, headers=headers)
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.headers["etag"] != second.headers["etag"]
    assert store.objects["uploads/alice/u0001"].body == other
    await rig.drain()
    state = await rig.state("u0001")
    assert (state["state"], state["reason"]) == ("rejected", "overwritten")
    assert store.objects == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_checksum_binds_the_bytes_that_unsigned_payload_leaves_open(
    rig: Rig,
) -> None:
    digest = base64.b64encode(hashlib.sha256(PNG).digest()).decode()
    headers = {"Content-Length": str(len(PNG)), "x-amz-checksum-sha256": digest}
    store = rig.store
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/uploads/alice/pinned",
        region=store.region,
        now=START,
        expires=60,
        headers=headers,
    )
    swapped = PNG[:-1] + b"\x01"
    rejected = await rig.storage.put(url, content=swapped, headers=headers)
    assert (rejected.status_code, rejected.json()["error"]) == (400, "BadDigest")
    assert (await rig.storage.put(url, content=PNG, headers=headers)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_secret_comparison_is_constant_time_on_bytes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    upload = (await rig.create()).json()["upload"]
    assert (await _put(rig, upload, PNG)).status_code == 200
    await rig.drain()
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    assert (await _post_form(rig, fields, PNG)).status_code == 204
    signature = _query(upload["url"])["X-Amz-Signature"].encode()
    assert (signature, signature) in calls  # PUT query signature
    assert (fields["x-amz-signature"].encode(),) * 2 in calls  # POST policy signature
    assert {len(a) for a, b in calls if a == b} >= {64, 71}  # 71: "sha256=" + event HMAC
    checksum = base64.b64encode(hashlib.sha256(PNG).digest())
    store = rig.store
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/uploads/alice/pinned",
        region=store.region,
        now=START,
        expires=60,
        headers={"x-amz-checksum-sha256": checksum.decode()},
    )
    headers = {"x-amz-checksum-sha256": checksum.decode()}
    assert (await rig.storage.put(url, content=PNG, headers=headers)).status_code == 200
    assert (checksum, checksum) in calls


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["no_algorithm", "non_ascii_signature", "non_ascii_checksum"],
)
async def test_hostile_put_requests_are_4xx_never_500(rig: Rig, case: str) -> None:
    upload = (await rig.create()).json()["upload"]
    url = cast("str", upload["url"])
    signed = cast("dict[str, str]", upload["headers"])
    headers = {name.encode(): value.encode() for name, value in signed.items()}
    if case == "no_algorithm":
        parts = urlsplit(url)
        kept = [p for p in parts.query.split("&") if not p.startswith("X-Amz-Algorithm=")]
        url = urlunsplit(parts._replace(query="&".join(kept)))
    elif case == "non_ascii_signature":
        url = _with_query(url, **{"X-Amz-Signature": "\u00e9"})
    else:
        headers[b"x-amz-checksum-sha256"] = b"\xe9"
    response = await rig.storage.put(url, content=PNG, headers=headers)
    assert 400 <= response.status_code < 500
    assert rig.store.objects == {}


# --- Phase 2: finalization from storage events --------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsigned_event_is_401_and_changes_nothing(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    await _put(rig, upload, PNG)
    event = rig.store.outbox.pop()
    assert (await rig.deliver(event, signature="sha256=" + "0" * 64)).status_code == 401
    assert (await rig.state("u0001"))["state"] == "pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_event_delivery_is_idempotent(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    await _put(rig, upload, PNG)
    event = rig.store.outbox.pop()
    for minutes in range(3):
        rig.clock.now = START + timedelta(minutes=minutes)
        assert (await rig.deliver(event)).status_code == 204
    record = rig.service.records["u0001"]
    assert (record.state, record.etag) == (UploadState.ACTIVE, rig.store.objects[record.key].etag)
    assert record.finalized_at == START


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalization_trusts_storage_not_the_event_body(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    forged = build_event("media", "uploads/alice/u0001", len(PNG), '"x"', "01", "Put")
    assert (await rig.deliver(forged)).status_code == 204
    assert (await rig.state("u0001"))["state"] == "pending"
    assert rig.service.ignored_events == 1
    await _put(rig, upload, PNG)
    await rig.drain()
    assert (await rig.state("u0001"))["state"] == "active"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_content_type_is_a_label_so_finalization_sniffs_the_bytes(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    html = b"<script>alert(1)</script>".ljust(len(PNG), b" ")
    assert (await _put(rig, upload, html)).status_code == 200
    await rig.drain()
    state = await rig.state("u0001")
    assert (state["state"], state["reason"]) == ("rejected", "magic_bytes")
    assert rig.store.objects == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsigned_content_type_lets_the_client_choose_text_html(rig: Rig) -> None:
    store = rig.store
    url = presign_url(
        credentials=store.credentials,
        method="PUT",
        host=store.host,
        path="/uploads/alice/u0001",
        region=store.region,
        now=START,
        expires=60,
    )
    await rig.create()
    page = b"<html>" + b" " * 58
    response = await rig.storage.put(url, content=page, headers={"Content-Type": "text/html"})
    assert response.status_code == 200
    assert store.objects["uploads/alice/u0001"].content_type == "text/html"
    rig.clock.now = START + timedelta(seconds=30)
    await rig.drain()
    state = await rig.state("u0001")
    assert (state["state"], state["reason"]) == ("rejected", "content_type")
    assert state["finalized_at"] == (START + timedelta(seconds=30)).isoformat()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_events_name_the_creating_call_and_carry_increasing_sequencers(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    stored = await _put(rig, upload, PNG)
    # An entity tag is a quoted string (RFC 9110 Section 8.8.3).
    assert stored.headers["etag"] == '"' + hashlib.sha256(PNG).hexdigest()[:32] + '"'
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    assert (await _post_form(rig, fields, PNG)).status_code == 204
    records = [json.loads(event)["Records"][0] for event in rig.store.outbox]
    assert [record["eventName"] for record in records] == [
        "ObjectCreated:Put",
        "ObjectCreated:Post",
    ]
    assert [record["s3"]["bucket"] for record in records] == [{"name": "media"}] * 2
    objects = [record["s3"]["object"] for record in records]
    assert [(o["key"], o["size"]) for o in objects] == [
        ("uploads%2Falice%2Fu0001", len(PNG)),
        ("uploads%2Falice%2Fu0002", len(PNG)),
    ]
    assert [o["sequencer"] for o in objects] == ["0000000000000001", "0000000000000002"]


@pytest.mark.unit
def test_event_keys_are_form_urlencoded_and_decoded_with_plus_as_space() -> None:
    body = build_event("media", "uploads/a b+c.png", 1, '"e"', "01", "Put")
    assert b'"key":"uploads%2Fa+b%2Bc.png"' in body
    assert event_keys(body) == ["uploads/a b+c.png"]
    removed = json.dumps({"Records": [{"eventName": "ObjectRemoved:Delete"}]}).encode()
    assert event_keys(removed) == []
    assert event_keys(b"{}") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sweeper_expires_after_grace_and_a_late_object_is_deleted(rig: Rig) -> None:
    upload = (await rig.create()).json()["upload"]
    rig.clock.now = START + timedelta(seconds=900)
    assert (await _put(rig, upload, PNG)).status_code == 200
    rig.clock.now = START + timedelta(seconds=900 + 599)
    assert rig.service.sweep() == []
    rig.clock.now = START + timedelta(seconds=900 + 600)
    assert rig.service.sweep() == ["u0001"]
    assert rig.store.objects == {}
    rig.store.objects["uploads/alice/u0001"] = presigned_upload.StoredObject(
        PNG, "image/png", '"l"'
    )
    await rig.drain()
    assert rig.store.objects == {}
    assert (await rig.state("u0001"))["state"] == "expired"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sweeper_leaves_finalized_records_and_their_objects_alone(rig: Rig) -> None:
    accepted = (await rig.create()).json()["upload"]
    assert (await _put(rig, accepted, PNG)).status_code == 200
    refused = (await rig.create()).json()["upload"]
    assert (await _put(rig, refused, b"x" * len(PNG))).status_code == 200
    await rig.drain()
    rig.clock.now = START + timedelta(days=30)
    assert rig.service.sweep() == []
    assert (await rig.state("u0001"))["state"] == "active"
    assert (await rig.state("u0002"))["state"] == "rejected"
    assert list(rig.store.objects) == ["uploads/alice/u0001"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload_id", "authorization"),
    [("u0001", "Bearer t-bob"), ("u0001", ""), ("u9999", "Bearer t-alice")],
    ids=["other-user", "anonymous", "unknown-id"],
)
async def test_upload_record_is_404_for_anyone_but_its_owner(
    rig: Rig, upload_id: str, authorization: str
) -> None:
    await rig.create()
    response = await rig.api.get(
        f"/v1/uploads/{upload_id}", headers={"Authorization": authorization}
    )
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


# --- POST policies ------------------------------------------------------------------------


async def _post_form(
    rig: Rig, fields: dict[str, str], body: bytes, content_type: str = "image/png"
) -> httpx.Response:
    files = {"file": ("x", body, content_type)}
    return await rig.storage.post("/", data=fields, files=files)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_policy_upload_within_range_finalizes(rig: Rig) -> None:
    created = (await rig.create(method="POST", size_bytes=200)).json()["upload"]
    assert created["method"] == "POST"
    policy = json.loads(base64.b64decode(created["fields"]["policy"]))
    assert ["content-length-range", 1, 200] in policy["conditions"]
    response = await _post_form(rig, created["fields"], PNG)
    assert response.status_code == 204
    await rig.drain()
    assert (await rig.state("u0001"))["state"] == "active"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"key": "uploads/bob/u0001"}, "mismatch:key"),
        ({"Content-Type": "text/html"}, "mismatch:content-type"),
        ({"x-amz-meta-owner": "admin"}, "unlisted_field:x-amz-meta-owner"),
        ({"x-amz-signature": "0" * 64}, "signature_mismatch"),
    ],
)
async def test_post_policy_rejects_fields_it_does_not_allow(
    rig: Rig, changes: dict[str, str], reason: str
) -> None:
    created = (await rig.create(method="POST", size_bytes=200)).json()["upload"]
    fields = cast("dict[str, str]", created["fields"]) | changes
    response = await _post_form(rig, fields, PNG)
    assert (response.status_code, response.json()["reason"]) == (403, reason)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replayed_post_form_that_overwrites_an_accepted_object_is_rejected(
    rig: Rig,
) -> None:
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    assert (await _post_form(rig, fields, PNG)).status_code == 204
    await rig.drain()
    assert (await rig.state("u0001"))["state"] == "active"
    html = b"<svg onload=alert(1)>"
    assert (await _post_form(rig, fields, html)).status_code == 204  # policy still valid
    rig.clock.now = START + timedelta(seconds=60)
    await rig.drain()
    state = await rig.state("u0001")
    assert (state["state"], state["reason"], state["etag"]) == ("rejected", "overwritten", None)
    assert state["finalized_at"] == (START + timedelta(seconds=60)).isoformat()
    assert rig.store.objects == {}
    assert (await _post_form(rig, fields, PNG)).status_code == 204
    await rig.drain()
    assert rig.store.objects == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_policy_content_length_range_rejects_one_byte_over(rig: Rig) -> None:
    fields = (await rig.create(method="POST", size_bytes=64)).json()["upload"]["fields"]
    over = await _post_form(rig, fields, PNG + b"!")
    assert (over.status_code, over.json()["reason"]) == (403, "content_length_range")
    assert (await _post_form(rig, fields, PNG)).status_code == 204


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_policy_expires_with_the_upload_record(rig: Rig) -> None:
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    rig.clock.now = START + timedelta(seconds=901)
    response = await _post_form(rig, fields, PNG)
    assert (response.status_code, response.json()["reason"]) == (403, "policy_expired")
    rig.clock.now = START + timedelta(seconds=900)  # "not valid after": the last second works
    assert (await _post_form(rig, fields, PNG)).status_code == 204


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [PNG + b"0" * 900, b""], ids=["964-bytes", "empty"])
async def test_finalization_catches_a_store_that_skips_policy_checks(
    lax_rig: Rig, body: bytes
) -> None:
    fields = (await lax_rig.create(method="POST", size_bytes=64)).json()["upload"]["fields"]
    assert (await _post_form(lax_rig, fields, body)).status_code == 204
    assert len(lax_rig.store.objects["uploads/alice/u0001"].body) == len(body)
    await lax_rig.drain()
    state = await lax_rig.state("u0001")
    assert (state["state"], state["reason"]) == ("rejected", "size")
    assert lax_rig.store.objects == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content_length", "allowed"),
    [(0, False), (1, True), (64, True), (65, False)],
    ids=["below", "low-bound", "high-bound", "above"],
)
def test_content_length_range_is_inclusive_at_both_ends(content_length: int, allowed: bool) -> None:
    policy: dict[str, Any] = {
        "expiration": "2026-11-12T00:00:00.000Z",
        "conditions": [["content-length-range", 1, 64]],
    }
    if allowed:
        check_policy(policy, {}, content_length, START)
    else:
        with pytest.raises(PolicyViolation, match="content_length_range"):
            check_policy(policy, {}, content_length, START)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_form_is_capped_at_32_fields_and_one_file(rig: Rig) -> None:
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    assert len(fields) == 8
    padded = fields | {f"x-ignore-{n}": "0" for n in range(25)}  # 33 fields
    too_many = await _post_form(rig, padded, PNG)
    assert (too_many.status_code, too_many.text) == (
        400,
        "Too many fields. Maximum number of fields is 32.",
    )
    files = [("file", ("x", PNG, "image/png")), ("extra", ("y", PNG, "image/png"))]
    two_files = await rig.storage.post("/", data=fields, files=files)
    assert (two_files.status_code, two_files.text) == (
        400,
        "Too many files. Maximum number of files is 1.",
    )
    assert rig.store.objects == {}
    del padded["x-ignore-24"]  # exactly 32 fields
    assert (await _post_form(rig, padded, PNG)).status_code == 204


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_form_without_a_key_field_is_400_and_stores_nothing(rig: Rig) -> None:
    store = rig.store
    credential = "AKIDLAB/20261111/eu-west-1/s3/aws4_request"
    document = {
        "expiration": "2030-01-01T00:00:00.000Z",
        "conditions": [{"x-amz-credential": credential}],
    }
    policy_b64 = base64.b64encode(json.dumps(document).encode()).decode()
    fields = {
        "policy": policy_b64,
        "x-amz-credential": credential,
        "x-amz-signature": sign_policy(policy_b64, store.credentials, "20261111", store.region),
    }
    response = await _post_form(rig, fields, PNG)
    assert (response.status_code, response.json()["error"]) == (400, "MissingKey")
    assert (store.objects, store.outbox) == ({}, [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_credential_uses_utc_even_when_the_clock_is_not_utc(rig: Rig) -> None:
    minus_five = timezone(timedelta(hours=-5))
    rig.clock.now = START.astimezone(minus_five)
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    assert fields["x-amz-date"] == "20261111T120000Z"
    policy = json.loads(base64.b64decode(fields["policy"]))
    assert policy["expiration"] == "2026-11-11T12:15:00.000Z"
    assert (await _post_form(rig, fields, PNG)).status_code == 204


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential", "reason"),
    [
        ("AKIDLAB/20261111/us-east-1/s3/aws4_request", "bad_scope"),
        ("AKIDLAB/20261111/eu-west-1/sqs/aws4_request", "bad_scope"),
        ("AKIDOTHER/20261111/eu-west-1/s3/aws4_request", "bad_credential"),
        ("AKIDLAB/20261111/eu-west-1/s3", "malformed_policy"),
    ],
)
async def test_post_credential_scope_is_checked_before_the_signature(
    rig: Rig, credential: str, reason: str
) -> None:
    created = (await rig.create(method="POST", size_bytes=200)).json()["upload"]
    fields = cast("dict[str, str]", created["fields"]) | {"x-amz-credential": credential}
    response = await _post_form(rig, fields, PNG)
    assert (response.status_code, response.json()["reason"]) == (403, reason)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hostile_post_and_event_requests_are_4xx_never_500(rig: Rig) -> None:
    fields = (await rig.create(method="POST", size_bytes=200)).json()["upload"]["fields"]
    fields["x-amz-signature"] = "\u00e9"
    assert (await _post_form(rig, fields, PNG)).status_code == 403
    headers = {EVENT_SIGNATURE_HEADER.encode(): b"\xe9"}
    bad_sig = await rig.api.post("/internal/storage-events", content=b"{}", headers=headers)
    assert bad_sig.status_code == 401
    assert (await rig.deliver(b"not json")).status_code == 400
    assert (
        await rig.deliver(b'{"Records": [{"eventName": "ObjectCreated:Put"}]}')
    ).status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signed_but_malformed_policy_is_403_not_500(rig: Rig) -> None:
    store = rig.store
    policy_b64 = base64.b64encode(b'{"expiration": "2030-01-01T00:00:00.000Z", "conditions": [7]}')
    credential = "AKIDLAB/20261111/eu-west-1/s3/aws4_request"
    fields = {
        "policy": policy_b64.decode(),
        "x-amz-credential": credential,
        "x-amz-signature": sign_policy(
            policy_b64.decode(), store.credentials, "20261111", store.region
        ),
    }
    response = await _post_form(rig, fields, PNG)
    assert (response.status_code, response.json()["reason"]) == (403, "malformed_policy")


@pytest.mark.unit
def test_service_refuses_types_it_cannot_sniff() -> None:
    _, store, _ = _make_rig()
    with pytest.raises(ValueError, match="image/webp"):
        UploadService(
            store=store,
            users={},
            event_secret=EVENT_SECRET,
            clock=Clock(),
            allowed_types=frozenset({"image/png", "image/webp"}),
        )


@pytest.mark.unit
def test_starts_with_content_type_checks_every_comma_separated_value() -> None:
    policy: dict[str, Any] = {
        "expiration": "2026-11-12T00:00:00.000Z",
        "conditions": [["starts-with", "$Content-Type", "image/"]],
    }
    check_policy(policy, {"content-type": "image/jpg,image/png,image/gif"}, 1, START)
    with pytest.raises(PolicyViolation, match="prefix:content-type"):
        check_policy(policy, {"content-type": "image/jpg,text/plain"}, 1, START)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("condition", "fields", "reason"),
    [
        (["eq", "$acl", "private"], {"acl": "public-read"}, "mismatch:acl"),
        (["starts-with", "$key", "user/"], {"key": "admin/x"}, "prefix:key"),
        (["not-a-match-type", "$key", ""], {}, "unknown_condition:not-a-match-type"),
    ],
)
def test_policy_condition_match_types(
    condition: list[Any], fields: dict[str, str], reason: str
) -> None:
    policy: dict[str, Any] = {"expiration": "2026-11-12T00:00:00.000Z", "conditions": [condition]}
    with pytest.raises(PolicyViolation) as caught:
        check_policy(policy, fields, 1, START)
    assert caught.value.reason == reason


@pytest.mark.unit
def test_policy_exempts_signature_file_policy_and_x_ignore_fields() -> None:
    policy: dict[str, Any] = {"expiration": "2026-11-12T00:00:00.000Z", "conditions": []}
    exempt = {"x-amz-signature": "s", "file": "f", "policy": "p", "x-ignore-filename": "a.png"}
    check_policy(policy, exempt, 1, START)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_requests_to_the_store_are_4xx(rig: Rig) -> None:
    no_auth = await rig.storage.put("/uploads/alice/u0001", content=PNG)
    assert (no_auth.status_code, no_auth.json()["reason"]) == (403, "malformed_query")
    no_file = await rig.storage.post("/", data={"policy": "e30="}, files={"other": ("x", b"1")})
    assert no_file.status_code == 400
    bad_policy = await _post_form(rig, {"policy": "%%%"}, PNG)
    assert (bad_policy.status_code, bad_policy.json()["reason"]) == (403, "malformed_policy")
