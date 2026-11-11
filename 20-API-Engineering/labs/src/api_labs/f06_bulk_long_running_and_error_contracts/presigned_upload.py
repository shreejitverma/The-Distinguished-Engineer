"""Two-phase direct-to-storage uploads with SigV4 presigned PUT URLs and POST policies.

Phase 1: the API authenticates the caller, chooses the object key, records a pending upload,
and mints a short-lived credential: a SigV4 query-string presigned `PUT` URL whose signature
covers `content-length`, `content-type`, `if-none-match`, and `x-amz-checksum-sha256`, or a
SigV4-signed POST policy with a `content-length-range` condition.
Phase 2: the object store emits an `ObjectCreated` event; the API authenticates it, re-reads
the stored object instead of trusting the event, and moves the record to `active` or
`rejected`.

The signing code follows the AWS S3 documents "Authenticating Requests: Using Query
Parameters (AWS Signature Version 4)" and "Creating a POST Policy", and the tests reproduce
both of their published example signatures byte for byte.
`ObjectStore` is an in-process stand-in for S3, not a client for it; its status codes and
reason strings are this lab's own.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote_plus, unquote_plus

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from starlette.requests import Request

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
SERVICE = "s3"
MAX_EXPIRES_SECONDS = 604_800  # "the maximum is 604800 (seven days)"
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
POLICY_EXPIRATION_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"
EVENT_SIGNATURE_HEADER = "x-lab-event-signature"  # lab-defined; S3 delivers via SNS/SQS/Lambda
_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~",
)
_POLICY_EXEMPT_FIELDS = frozenset({"x-amz-signature", "file", "policy"})
_MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "application/pdf": b"%PDF-",
}


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key_id: str
    secret_access_key: str = field(repr=False)


def uri_encode(text: str, *, encode_slash: bool = True) -> str:
    """AWS `UriEncode()`: keep RFC 3986 unreserved bytes, `%XX` uppercase for every other byte."""
    out: list[str] = []
    for byte in text.encode("utf-8"):
        if byte in _UNRESERVED or (byte == 0x2F and not encode_slash):
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def canonical_query(params: Mapping[str, str]) -> str:
    pairs = sorted((uri_encode(name), uri_encode(value)) for name, value in params.items())
    return "&".join(f"{name}={value}" for name, value in pairs)


def canonical_request(
    method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str]
) -> str:
    lowered = {name.lower(): value.strip() for name, value in headers.items()}
    names = sorted(lowered)
    canonical_headers = "".join(f"{name}:{lowered[name]}\n" for name in names)
    return "\n".join(
        [
            method,
            uri_encode(path, encode_slash=False),
            canonical_query(query),
            canonical_headers,
            ";".join(names),
            UNSIGNED_PAYLOAD,
        ]
    )


def signing_key(secret: str, date: str, region: str) -> bytes:
    key = hmac.new(b"AWS4" + secret.encode("utf-8"), date.encode("ascii"), hashlib.sha256).digest()
    for part in (region, SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode("ascii"), hashlib.sha256).digest()
    return key


def string_to_sign(stamp: str, scope: str, request_text: str) -> str:
    digest = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
    return f"{ALGORITHM}\n{stamp}\n{scope}\n{digest}"


def _hex_hmac(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _same(presented: str, expected: str) -> bool:
    """Constant-time comparison on bytes; `compare_digest` raises on non-ASCII `str`."""
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def presign_url(
    *,
    credentials: Credentials,
    method: str,
    host: str,
    path: str,
    region: str,
    now: datetime,
    expires: int,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Return a SigV4 query-string presigned URL; every header in `headers` is signed."""
    if not 1 <= expires <= MAX_EXPIRES_SECONDS:
        raise ValueError(f"X-Amz-Expires must be 1..{MAX_EXPIRES_SECONDS}, got {expires}")
    stamp = now.astimezone(UTC).strftime(AMZ_DATE_FORMAT)
    scope = f"{stamp[:8]}/{region}/{SERVICE}/aws4_request"
    signed = {"host": host} | {name.lower(): value for name, value in (headers or {}).items()}
    query = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{credentials.access_key_id}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": ";".join(sorted(signed)),
    }
    to_sign = string_to_sign(stamp, scope, canonical_request(method, path, query, signed))
    signature = _hex_hmac(signing_key(credentials.secret_access_key, stamp[:8], region), to_sign)
    target = uri_encode(path, encode_slash=False)
    return f"https://{host}{target}?{canonical_query(query)}&X-Amz-Signature={signature}"


def sign_policy(policy_b64: str, credentials: Credentials, date: str, region: str) -> str:
    """POST policy signature: hex HMAC-SHA256 of the base64 policy under the SigV4 signing key."""
    return _hex_hmac(signing_key(credentials.secret_access_key, date, region), policy_b64)


class PolicyViolation(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _condition_value(fields: Mapping[str, str], name: str) -> str:
    return fields.get(name.lower().removeprefix("$"), "")


def check_policy(
    policy: Mapping[str, Any], fields: Mapping[str, str], content_length: int, now: datetime
) -> None:
    """Enforce a POST policy against lowercased form `fields` and the file size."""
    expiration = datetime.strptime(str(policy["expiration"]), POLICY_EXPIRATION_FORMAT)
    if now > expiration.replace(tzinfo=UTC):  # "not valid after" the expiration
        raise PolicyViolation("policy_expired")
    covered: set[str] = set()
    for condition in cast("list[Any]", policy["conditions"]):
        if isinstance(condition, dict):
            for name, expected in cast("dict[str, Any]", condition).items():
                covered.add(name.lower())
                if _condition_value(fields, name) != str(expected):
                    raise PolicyViolation(f"mismatch:{name.lower()}")
            continue
        op, *args = cast("list[Any]", condition)
        if op == "content-length-range":
            low, high = int(args[0]), int(args[1])
            if not low <= content_length <= high:
                raise PolicyViolation("content_length_range")
        elif op in {"eq", "starts-with"}:
            name, expected = str(args[0]).lower().removeprefix("$"), str(args[1])
            covered.add(name)
            values = _condition_value(fields, name)
            if op == "eq" and values != expected:
                raise PolicyViolation(f"mismatch:{name}")
            parts = values.split(",") if name == "content-type" else [values]
            if op == "starts-with" and not all(part.startswith(expected) for part in parts):
                raise PolicyViolation(f"prefix:{name}")
        else:
            raise PolicyViolation(f"unknown_condition:{op}")
    for name in fields:
        exempt = name in _POLICY_EXEMPT_FIELDS or name.startswith("x-ignore-")
        if not exempt and name not in covered:
            raise PolicyViolation(f"unlisted_field:{name}")


def build_event(bucket: str, key: str, size: int, etag: str, sequencer: str, kind: str) -> bytes:
    """A subset of the S3 event message: `Records[].s3.object.{key,size,eTag,sequencer}`."""
    record = {
        "eventName": f"ObjectCreated:{kind}",
        "s3": {
            "bucket": {"name": bucket},
            "object": {"key": quote_plus(key), "size": size, "eTag": etag, "sequencer": sequencer},
        },
    }
    return json.dumps({"Records": [record]}, separators=(",", ":")).encode("ascii")


def event_keys(body: bytes) -> list[str]:
    """Object keys of every `ObjectCreated` record, URL-decoded (`+` is a space)."""
    document = cast("dict[str, Any]", json.loads(body))
    keys: list[str] = []
    for record in cast("list[dict[str, Any]]", document.get("Records", [])):
        if str(record.get("eventName", "")).startswith("ObjectCreated:"):
            keys.append(unquote_plus(str(record["s3"]["object"]["key"])))
    return keys


def sign_event(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredObject:
    body: bytes
    content_type: str
    etag: str


def _forbidden(reason: str) -> JSONResponse:
    return JSONResponse({"error": "AccessDenied", "reason": reason}, status_code=403)


class ObjectStore:
    """In-process object store that verifies presigned `PUT` URLs and POST policies."""

    def __init__(
        self,
        *,
        bucket: str,
        host: str,
        region: str,
        credentials: Credentials,
        clock: Callable[[], datetime],
        enforce_post_policy: bool = True,
    ) -> None:
        self.bucket = bucket
        self.host = host
        self.region = region
        self.credentials = credentials
        self.clock = clock
        self.enforce_post_policy = enforce_post_policy
        self.objects: dict[str, StoredObject] = {}
        self.outbox: list[bytes] = []
        self._sequence = 0
        self.app = Starlette(
            routes=[
                Route("/", self._post_object, methods=["POST"]),
                Route("/{key:path}", self._put_object, methods=["PUT"]),
            ]
        )

    def head(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def _store(self, key: str, body: bytes, content_type: str, kind: str) -> str:
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        self.objects[key] = StoredObject(body, content_type, etag)
        self._sequence += 1
        sequencer = f"{self._sequence:016X}"
        self.outbox.append(build_event(self.bucket, key, len(body), etag, sequencer, kind))
        return etag

    def _verify_query_signature(self, request: Request) -> str | None:
        query = dict(request.query_params)
        signature = query.pop("X-Amz-Signature", "")
        try:
            akid, date, region, service, terminator = query["X-Amz-Credential"].split("/")
            stamp = query["X-Amz-Date"]
            expires = int(query["X-Amz-Expires"])
            signed_names = query["X-Amz-SignedHeaders"].split(";")
            issued = datetime.strptime(stamp, AMZ_DATE_FORMAT).replace(tzinfo=UTC)
            algorithm = query["X-Amz-Algorithm"]
        except (KeyError, ValueError):
            return "malformed_query"
        scope_ok = (region, service, terminator) == (self.region, SERVICE, "aws4_request")
        if algorithm != ALGORITHM or not scope_ok or date != stamp[:8]:
            return "bad_scope"
        if akid != self.credentials.access_key_id or "host" not in signed_names:
            return "bad_credential"
        if not 1 <= expires <= MAX_EXPIRES_SECONDS:
            return "bad_expires"
        if any(name not in request.headers for name in signed_names):
            return "signed_header_missing"
        headers = {name: request.headers[name] for name in signed_names}
        # The ASGI scope path is the decoded key; `request.url.path` re-escapes `?` and `#`.
        path = cast("str", request.scope["path"])
        request_text = canonical_request(request.method, path, query, headers)
        key = signing_key(self.credentials.secret_access_key, date, region)
        scope = f"{date}/{region}/{SERVICE}/aws4_request"
        expected = _hex_hmac(key, string_to_sign(stamp, scope, request_text))
        if not _same(signature, expected):
            return "signature_mismatch"
        now = self.clock()
        if now < issued:
            return "not_yet_valid"
        if now > issued + timedelta(seconds=expires):
            return "expired"
        return None

    async def _put_object(self, request: Request) -> Response:
        problem = self._verify_query_signature(request)
        if problem is not None:
            return _forbidden(problem)
        body = await request.body()
        if request.headers.get("content-length") != str(len(body)):
            return JSONResponse({"error": "IncompleteBody"}, status_code=400)
        checksum = request.headers.get("x-amz-checksum-sha256")
        if checksum is not None:
            actual = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
            if not _same(checksum, actual):
                return JSONResponse({"error": "BadDigest"}, status_code=400)
        key = request.path_params["key"]
        if request.headers.get("if-none-match") == "*" and key in self.objects:
            return JSONResponse({"error": "PreconditionFailed"}, status_code=412)
        content_type = request.headers.get("content-type", "binary/octet-stream")
        etag = self._store(key, body, content_type, "Put")
        return Response(status_code=200, headers={"ETag": etag})

    async def _post_object(self, request: Request) -> Response:
        async with request.form(max_files=1, max_fields=32) as form:
            fields = {k.lower(): v for k, v in form.items() if isinstance(v, str)}
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                return JSONResponse({"error": "MissingFile"}, status_code=400)
            body = await upload.read()
        try:
            policy_b64 = fields["policy"]
            policy = cast("dict[str, Any]", json.loads(base64.b64decode(policy_b64, validate=True)))
            akid, date, region, service, terminator = fields["x-amz-credential"].split("/")
        except (KeyError, ValueError, binascii.Error):
            return _forbidden("malformed_policy")
        if (region, service, terminator) != (self.region, SERVICE, "aws4_request"):
            return _forbidden("bad_scope")
        if akid != self.credentials.access_key_id:
            return _forbidden("bad_credential")
        expected = sign_policy(policy_b64, self.credentials, date, region)
        if not _same(fields.get("x-amz-signature", ""), expected):
            return _forbidden("signature_mismatch")
        if self.enforce_post_policy:
            try:
                check_policy(policy, fields, len(body), self.clock())
            except PolicyViolation as violation:
                return _forbidden(violation.reason)
            except (KeyError, TypeError, ValueError, IndexError):
                return _forbidden("malformed_policy")
        key = fields.get("key", "")
        if not key:
            return JSONResponse({"error": "MissingKey"}, status_code=400)
        etag = self._store(key, body, fields.get("content-type", "binary/octet-stream"), "Post")
        return Response(status_code=204, headers={"ETag": etag})


class UploadState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(slots=True)
class UploadRecord:
    upload_id: str
    owner: str
    key: str
    content_type: str
    size_bytes: int
    method: str
    expires_at: datetime
    filename: str
    state: UploadState = UploadState.PENDING
    etag: str | None = None
    reason: str | None = None
    finalized_at: datetime | None = None


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    body = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class UploadService:
    """The API side: mint upload credentials (phase 1), finalize from storage events (phase 2)."""

    def __init__(
        self,
        *,
        store: ObjectStore,
        users: Mapping[str, str],
        event_secret: bytes,
        clock: Callable[[], datetime],
        ttl_seconds: int = 900,
        grace_seconds: int = 3600,
        max_bytes: int = 10 * 1024 * 1024,
        allowed_types: frozenset[str] = frozenset(_MAGIC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if not allowed_types <= _MAGIC.keys():
            unknown = sorted(allowed_types - _MAGIC.keys())
            raise ValueError(f"no magic-byte signature for {unknown}; refusing to fail open")
        self.store = store
        self.users = dict(users)
        self.event_secret = event_secret
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        self.grace_seconds = grace_seconds
        self.max_bytes = max_bytes
        self.allowed_types = allowed_types
        self.id_factory = id_factory
        self.records: dict[str, UploadRecord] = {}
        self.by_key: dict[str, str] = {}
        self.ignored_events = 0
        self.app = Starlette(
            routes=[
                Route("/v1/uploads", self._create, methods=["POST"]),
                Route("/v1/uploads/{upload_id}", self._get, methods=["GET"]),
                Route("/internal/storage-events", self._event, methods=["POST"]),
            ]
        )

    def _principal(self, request: Request) -> str | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return self.users.get(token) if scheme.lower() == "bearer" else None

    def _credential(self, record: UploadRecord, now: datetime) -> dict[str, Any]:
        store = self.store
        if record.method == "PUT":
            headers = {
                "Content-Length": str(record.size_bytes),
                "Content-Type": record.content_type,
                "If-None-Match": "*",
            }
            url = presign_url(
                credentials=store.credentials,
                method="PUT",
                host=store.host,
                path="/" + record.key,
                region=store.region,
                now=now,
                expires=self.ttl_seconds,
                headers=headers,
            )
            return {"method": "PUT", "url": url, "headers": headers}
        stamp = now.astimezone(UTC).strftime(AMZ_DATE_FORMAT)
        credential = f"{store.credentials.access_key_id}/{stamp[:8]}/{store.region}/s3/aws4_request"
        fields = {
            "key": record.key,
            "Content-Type": record.content_type,
            "x-amz-algorithm": ALGORITHM,
            "x-amz-credential": credential,
            "x-amz-date": stamp,
        }
        policy = {
            "expiration": record.expires_at.astimezone(UTC).strftime(POLICY_EXPIRATION_FORMAT),
            "conditions": [
                {"bucket": store.bucket},
                {"key": record.key},
                {"content-type": record.content_type},
                ["content-length-range", 1, record.size_bytes],
                {"x-amz-algorithm": ALGORITHM},
                {"x-amz-credential": credential},
                {"x-amz-date": stamp},
            ],
        }
        fields["bucket"] = store.bucket
        fields["policy"] = base64.b64encode(json.dumps(policy).encode("ascii")).decode("ascii")
        fields["x-amz-signature"] = sign_policy(
            fields["policy"], store.credentials, stamp[:8], store.region
        )
        return {"method": "POST", "url": f"https://{store.host}/", "fields": fields}

    async def _create(self, request: Request) -> Response:
        owner = self._principal(request)
        if owner is None:
            return _problem(401, "Unauthorized", "a bearer token is required")
        try:
            payload = cast("dict[str, Any]", await request.json())
            content_type = str(payload["content_type"])
            size_bytes = payload["size_bytes"]
            method = str(payload.get("method", "PUT"))
            filename = str(payload.get("filename", ""))
        except (ValueError, KeyError, TypeError):
            return _problem(400, "Bad Request", "need JSON content_type and size_bytes")
        if type(size_bytes) is not int or not 1 <= size_bytes <= self.max_bytes:
            return _problem(400, "Bad Request", f"size_bytes must be 1..{self.max_bytes}")
        if content_type not in self.allowed_types or method not in {"PUT", "POST"}:
            return _problem(400, "Bad Request", "content_type or method is not allowed")
        now = self.clock()
        upload_id = self.id_factory()
        record = UploadRecord(
            upload_id=upload_id,
            owner=owner,
            key=f"uploads/{owner}/{upload_id}",
            content_type=content_type,
            size_bytes=size_bytes,
            method=method,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            filename=filename,
        )
        self.records[upload_id] = record
        self.by_key[record.key] = upload_id
        body = {
            "upload_id": upload_id,
            "state": record.state.value,
            "expires_at": record.expires_at.isoformat(),
            "upload": self._credential(record, now),
        }
        return JSONResponse(body, status_code=201, headers={"Location": f"/v1/uploads/{upload_id}"})

    async def _get(self, request: Request) -> Response:
        record = self.records.get(request.path_params["upload_id"])
        if record is None or record.owner != self._principal(request):
            return _problem(404, "Not Found", "no such upload")
        body = {
            "upload_id": record.upload_id,
            "state": record.state.value,
            "etag": record.etag,
            "reason": record.reason,
            "finalized_at": record.finalized_at.isoformat() if record.finalized_at else None,
        }
        return JSONResponse(body)

    def _finalize(self, key: str) -> None:
        upload_id = self.by_key.get(key)
        record = self.records.get(upload_id) if upload_id is not None else None
        stored = self.store.head(key)
        if record is None or stored is None:
            self.ignored_events += 1
            return
        if record.state in {UploadState.EXPIRED, UploadState.REJECTED}:
            self.store.delete(key)  # late or replayed object for a closed record: an orphan
            return
        if record.state is UploadState.ACTIVE and stored.etag == record.etag:
            return  # duplicate delivery: events are at-least-once
        reason = None
        if record.state is UploadState.ACTIVE:
            reason = "overwritten"  # a replayed credential replaced an accepted object
        elif not 1 <= len(stored.body) <= record.size_bytes:
            reason = "size"
        elif stored.content_type != record.content_type:
            reason = "content_type"
        elif not stored.body.startswith(_MAGIC[record.content_type]):
            reason = "magic_bytes"
        record.finalized_at = self.clock()
        if reason is None:
            record.state, record.etag = UploadState.ACTIVE, stored.etag
        else:
            record.state, record.reason, record.etag = UploadState.REJECTED, reason, None
            self.store.delete(key)

    async def _event(self, request: Request) -> Response:
        body = await request.body()
        presented = request.headers.get(EVENT_SIGNATURE_HEADER, "")
        if not _same(presented, sign_event(self.event_secret, body)):
            return _problem(401, "Unauthorized", "bad event signature")
        try:
            keys = event_keys(body)
        except (ValueError, KeyError, TypeError, AttributeError):
            return _problem(400, "Bad Request", "malformed event")
        for key in keys:
            self._finalize(key)
        return Response(status_code=204)

    def sweep(self) -> list[str]:
        """Expire pending uploads once the URL lifetime plus a grace period has passed."""
        cutoff = self.clock() - timedelta(seconds=self.grace_seconds)
        expired: list[str] = []
        for record in self.records.values():
            if record.state is UploadState.PENDING and record.expires_at <= cutoff:
                record.state = UploadState.EXPIRED
                self.store.delete(record.key)
                expired.append(record.upload_id)
        return expired
