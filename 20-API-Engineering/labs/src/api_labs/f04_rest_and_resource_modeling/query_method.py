"""HTTP QUERY (RFC 10008): a safe, idempotent, cacheable request whose body is the query.

The Starlette app below implements one queryable collection, ``/contacts``:

* ``QUERY /contacts`` with ``Content-Type: application/json`` runs the query (RFC 10008 Section 2).
* A missing or wrong ``Content-Type`` fails with 400 or 415 plus ``Accept-Query`` (Section 2.1).
* A well-formed but meaningless query fails with 422; an unsatisfiable ``Accept`` with 406.
* A 200 carries ``Location`` naming the equivalent resource (Sections 2.2 and 2.4), which
  answers ``GET`` with a fresh result, and an ``ETag`` for conditional requests (Section 2.6).
* The stored-query id is an HMAC of the normalized cache key, so a retried QUERY lands on the
  same URI (idempotent resource creation) and the URI leaks nothing of the query (Section 4).

``query_cache_key`` is the cache side of Section 2.7, ``accept_query_value`` serializes the
Structured Field of Section 3, and ``query_retry_policy`` patches urllib3's retry allowlist.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Body, FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.requests import Request

QUERY_MEDIA_TYPE = "application/json"
# RFC 9651 Section 3.3.4: sf-token = ( ALPHA / "*" ) *( tchar / ":" / "/" )
_SF_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*")
# RFC 9651 Section 3.3.3: unescaped = %x20-21 / %x23-24 / %x26-5B / %x5D-7E, plus "%" and escapes
_SF_STRING_CHARS = re.compile(r"[\x20-\x7e]*")
# RFC 9110 Section 12.4.2: qvalue = ( "0" [ "." 0*3DIGIT ] ) / ...
_ZERO_WEIGHT = re.compile(r"q=0(\.0{0,3})?")
# RFC 9110 Section 12.5.1: the most specific matching range decides the weight
_JSON_RANGE_SPECIFICITY = {"*/*": 0, "application/*": 1, QUERY_MEDIA_TYPE: 2}


def accept_query_value(media_types: Iterable[str]) -> str:
    """Serialize media ranges as the RFC 9651 List that ``Accept-Query`` requires."""
    members: list[str] = []
    for media_type in media_types:
        if _SF_TOKEN.fullmatch(media_type):
            members.append(media_type)
        elif _SF_STRING_CHARS.fullmatch(media_type):
            escaped = media_type.replace("\\", "\\\\").replace('"', '\\"')
            members.append(f'"{escaped}"')
        else:
            raise ValueError(f"not serializable as an sf-token or sf-string: {media_type!r}")
    return ", ".join(members)


def media_type_of(content_type: str | None) -> str | None:
    """Return the lowercased ``type/subtype`` of a ``Content-Type`` value, or None."""
    if content_type is None:
        return None
    essence = content_type.split(";", 1)[0].strip().lower()
    return essence if "/" in essence else None


def _is_json(media_type: str) -> bool:
    return media_type == QUERY_MEDIA_TYPE or media_type.endswith("+json")


def query_cache_key(
    *, target: str, content_type: str | None, content_encoding: str | None, body: bytes
) -> str:
    """Build a QUERY cache key that incorporates the content (RFC 10008 Section 2.7).

    Normalization only affects the key, never the forwarded request: content coding is
    removed, and JSON is re-serialized with sorted keys and no insignificant whitespace.
    """
    media_type = media_type_of(content_type) or ""
    if content_encoding is not None and content_encoding.strip().lower() == "gzip":
        body = gzip.decompress(body)
    if _is_json(media_type):
        body = json.dumps(
            json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    digest = hashlib.sha256()
    for part in (b"QUERY", target.encode(), media_type.encode(), body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def query_retry_policy(total: int = 3) -> Retry:
    """urllib3 retries only its default idempotent allowlist; QUERY must be added explicitly."""
    return Retry(
        total=total,
        status_forcelist=(502, 503, 504),
        allowed_methods=Retry.DEFAULT_ALLOWED_METHODS | {"QUERY"},
    )


class ContactQuery(BaseModel):
    """The query language: one JSON document, unknown fields rejected."""

    model_config = ConfigDict(extra="forbid")

    surname_prefix: str = ""
    select: tuple[str, ...] = ("surname", "email")
    limit: int = Field(default=10, ge=1, le=100)


@dataclass
class ContactStore:
    """Mutable data set and the server-assigned equivalent resources."""

    contacts: list[dict[str, str]]
    secret: bytes
    version: int = 1
    stored_queries: dict[str, ContactQuery] = field(default_factory=dict[str, ContactQuery])

    def run(self, query: ContactQuery) -> list[dict[str, str]]:
        hits = [c for c in self.contacts if c["surname"].startswith(query.surname_prefix)]
        return [{k: c[k] for k in query.select if k in c} for c in hits[: query.limit]]

    def etag(self, query_id: str) -> str:
        return f'"{query_id}-{self.version}"'

    def remember(self, cache_key: str, query: ContactQuery) -> str:
        query_id = hmac.new(self.secret, cache_key.encode(), hashlib.sha256).hexdigest()[:16]
        self.stored_queries.setdefault(query_id, query)
        return query_id


def _accepts_json(accept: str | None) -> bool:
    if accept is None:
        return True
    best_rank = -1
    acceptable = False
    for member in accept.split(","):
        media_range, *params = (p.strip().lower() for p in member.split(";"))
        rank = _JSON_RANGE_SPECIFICITY.get(media_range, -1)
        if rank > best_rank:
            best_rank = rank
            # RFC 9110 Section 12.4.2: a qvalue of 0 means "not acceptable"
            acceptable = not any(_ZERO_WEIGHT.fullmatch(p) for p in params)
    return acceptable


def _error(status: int, detail: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def build_app(store: ContactStore) -> Starlette:
    """Starlette app serving QUERY on /contacts and GET on the equivalent resources."""
    accept_query = accept_query_value([QUERY_MEDIA_TYPE])

    async def query_contacts(request: Request) -> Response:
        media_type = media_type_of(request.headers.get("content-type"))
        if media_type is None:
            return _error(400, "QUERY requires a Content-Type", {"Accept-Query": accept_query})
        if media_type != QUERY_MEDIA_TYPE:
            headers = {"Accept-Query": accept_query, "Accept": QUERY_MEDIA_TYPE}
            return _error(415, f"unsupported query type {media_type}", headers)
        if not _accepts_json(request.headers.get("accept")):
            return _error(406, f"results are only available as {QUERY_MEDIA_TYPE}")
        body = await request.body()
        try:
            document: Any = json.loads(body)
        except ValueError:
            return _error(400, "content is not valid JSON despite Content-Type")
        try:
            query = ContactQuery.model_validate(document)
        except ValidationError as exc:
            return _error(422, f"{exc.error_count()} invalid query field(s)")
        key = query_cache_key(
            target=request.url.path,
            content_type=media_type,
            content_encoding=request.headers.get("content-encoding"),
            body=body,
        )
        query_id = store.remember(key, query)
        headers = {
            "Location": f"/contacts/stored-queries/{query_id}",
            "ETag": store.etag(query_id),
            "Accept-Query": accept_query,
        }
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        return JSONResponse(store.run(query), headers=headers)

    async def stored_query(request: Request) -> Response:
        query_id: str = request.path_params["query_id"]
        query = store.stored_queries.get(query_id)
        if query is None:
            return _error(404, "unknown or expired stored query; resend the QUERY")
        etag = store.etag(query_id)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(store.run(query), headers={"ETag": etag})

    return Starlette(
        routes=[
            Route("/contacts", query_contacts, methods=["QUERY"]),
            Route("/contacts/stored-queries/{query_id}", stored_query, methods=["GET"]),
        ]
    )


def build_fastapi_app() -> FastAPI:
    """The same QUERY route declared in FastAPI, to inspect what its OpenAPI export emits."""
    app = FastAPI(title="contacts", version="1")

    @app.api_route("/contacts", methods=["QUERY"])
    async def query_contacts(
        query: Annotated[ContactQuery, Body()],
    ) -> list[dict[str, str]]:
        return [{"surname": query.surname_prefix}]

    return app


def openapi_32_with_query_body(app: FastAPI, path: str = "/contacts") -> dict[str, Any]:
    """Promote FastAPI's export to OAS 3.2.1 and restore the dropped QUERY requestBody."""
    document = app.openapi()
    document["openapi"] = "3.2.1"
    operation: dict[str, Any] = document["paths"][path]["query"]
    operation["requestBody"] = {
        "required": True,
        "content": {QUERY_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/ContactQuery"}}},
    }
    return document
