"""OpenAPI 3.2.1 additions as executable rules: QUERY, itemSchema, tag trees, OAuth metadata."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from starlette.requests import Request


class SchemaValidator(Protocol):
    """The slice of the jsonschema validator interface this lab relies on."""

    def iter_errors(self, instance: Any) -> Iterator[Any]: ...


SELF_URI = "https://api.example.com/openapi/feed.json"
ISSUER = "https://auth.example.com/tenants/acme"
FIXED_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")
SEQUENTIAL_MEDIA_TYPES = frozenset(
    {"application/jsonl", "application/x-ndjson", "application/json-seq", "text/event-stream"}
)
STREAM_MEDIA_TYPES = ("application/jsonl", "application/json-seq", "text/event-stream")
OFFERS = ("application/json", *STREAM_MEDIA_TYPES)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
RS = b"\x1e"
FEED: tuple[dict[str, Any], ...] = (
    {"id": 1, "title": "alpha"},
    {"id": 2, "title": "beta"},
    {"id": 3, "title": "alphabet"},
)


def build_document() -> dict[str, Any]:
    """Return an OpenAPI 3.2.1 description that uses every addition this lab exercises."""
    item_ref = {"$ref": "#/components/schemas/FeedItem"}
    query_content = {"schema": {"$ref": "#/components/schemas/FeedQuery"}}
    responses = {
        "200": {
            "summary": "Matching feed items",
            "content": {
                "application/json": {"schema": {"type": "array", "items": item_ref}},
                "application/jsonl": {"itemSchema": item_ref},
                "application/json-seq": {"itemSchema": item_ref},
                "text/event-stream": {"itemSchema": {"$ref": "#/components/schemas/FeedEvent"}},
            },
        }
    }
    return {
        "openapi": "3.2.1",
        "$self": SELF_URI,
        "info": {"title": "Feed", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com", "name": "production"}],
        "tags": [
            {"name": "catalog", "summary": "Catalog", "kind": "nav"},
            {"name": "feed", "summary": "Feed", "parent": "catalog", "kind": "nav"},
            {"name": "feed-streaming", "summary": "Streaming", "parent": "feed", "kind": "nav"},
            {"name": "partner", "summary": "Partner", "kind": "audience"},
        ],
        "paths": {
            "/feed": {
                "query": {
                    "operationId": "queryFeed",
                    "tags": ["feed-streaming", "partner"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": query_content},
                    },
                    "responses": responses,
                    "security": [{"feed_oauth": ["feed:read"]}],
                },
                "get": {
                    "operationId": "getFeed",
                    "tags": ["feed"],
                    "parameters": [
                        {
                            "name": "filter",
                            "in": "querystring",
                            "content": {"application/x-www-form-urlencoded": query_content},
                        }
                    ],
                    "responses": responses,
                },
                "additionalOperations": {
                    "PURGE": {
                        "operationId": "purgeFeedCache",
                        "tags": ["feed"],
                        "responses": {"204": {"summary": "Cache entry dropped"}},
                    }
                },
            }
        },
        "components": {
            "schemas": {
                "FeedQuery": {
                    "type": "object",
                    "required": ["q"],
                    "properties": {
                        "q": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "additionalProperties": False,
                },
                "FeedItem": {
                    "type": "object",
                    "required": ["id", "title"],
                    "properties": {
                        "id": {"type": "integer", "minimum": 1},
                        "title": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "FeedEvent": {
                    "type": "object",
                    "required": ["data", "event"],
                    "properties": {
                        "data": {
                            "type": "string",
                            "contentMediaType": "application/json",
                            "contentSchema": item_ref,
                        },
                        "event": {"const": "item"},
                        "id": {"type": "string"},
                        "retry": {"type": "integer", "minimum": 0},
                    },
                },
            },
            "securitySchemes": {
                "feed_oauth": {
                    "type": "oauth2",
                    "oauth2MetadataUrl": well_known_metadata_url(ISSUER),
                    "flows": {
                        "deviceAuthorization": {
                            "deviceAuthorizationUrl": f"{ISSUER}/device_authorization",
                            "tokenUrl": f"{ISSUER}/token",
                            "scopes": {"feed:read": "Read the feed"},
                        }
                    },
                }
            },
        },
    }


def tag_paths(tags: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Map each tag name to its root-first ancestry, rejecting unknown parents and cycles."""
    parents: dict[str, str | None] = {tag["name"]: tag.get("parent") for tag in tags}
    paths: dict[str, tuple[str, ...]] = {}
    for name in parents:
        trail: list[str] = []
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current not in paths:
            if current not in parents:
                raise ValueError(f"tag {trail[-1]!r} references unknown parent {current!r}")
            if current in seen:
                cycle = [*trail[trail.index(current) :], current]
                raise ValueError(f"circular tag hierarchy: {' -> '.join(cycle)}")
            seen.add(current)
            trail.append(current)
            current = parents[current]
        prefix = paths[current] if current is not None else ()
        for node in reversed(trail):
            prefix = (*prefix, node)
            paths[node] = prefix
    return paths


def is_sequential(media_type: str) -> bool:
    """Report whether OpenAPI 3.2 treats the media type as a sequence of items."""
    essence = media_type.split(";", 1)[0].strip().lower()
    return (
        essence in SEQUENTIAL_MEDIA_TYPES
        or essence.endswith("+json-seq")
        or essence.startswith("multipart/")
    )


def _operations(path_item: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    for field in FIXED_METHODS:
        if field in path_item:
            yield field.upper(), path_item[field]
    yield from path_item.get("additionalOperations", {}).items()


def _media_types(operation: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    yield from operation.get("requestBody", {}).get("content", {}).items()
    for response in operation.get("responses", {}).values():
        yield from response.get("content", {}).items()


def lint_oas32(document: Mapping[str, Any]) -> list[str]:
    """Return the OpenAPI 3.2 rule violations that the published JSON Schema cannot express."""
    findings: list[str] = []
    schemes = document.get("components", {}).get("securitySchemes", {})
    for name, scheme in schemes.items():
        url = scheme.get("oauth2MetadataUrl")
        if url is not None and urlsplit(str(url)).scheme != "https":
            findings.append(f"securitySchemes.{name}: oauth2MetadataUrl requires TLS")
    for path, path_item in document.get("paths", {}).items():
        for method in path_item.get("additionalOperations", {}):
            if method.lower() in FIXED_METHODS:
                findings.append(
                    f"{path}: additionalOperations {method!r} belongs in the "
                    f"{method.lower()!r} fixed field"
                )
        for method, operation in _operations(path_item):
            parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
            if {"query", "querystring"} <= {parameter.get("in") for parameter in parameters}:
                findings.append(f"{method} {path}: in=query and in=querystring are exclusive")
            for media_type, media in _media_types(operation):
                if "itemSchema" in media and not is_sequential(media_type):
                    findings.append(f"{method} {path}: itemSchema on non-sequential {media_type}")
    return findings


def loads_strict(text: str | bytes) -> Any:
    """Parse RFC 8259 JSON only: reject NaN and Infinity, report runaway nesting as ValueError."""

    def reject(constant: str) -> Any:
        raise ValueError(f"{constant} is not JSON")

    try:
        return json.loads(text, parse_constant=reject)
    except RecursionError as error:
        raise ValueError("JSON nesting is too deep") from error


def decode_jsonl(body: bytes) -> list[Any]:
    """Decode JSON Lines or NDJSON: one JSON value per LF-terminated line, no blank lines."""
    lines = body.decode("utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    return [loads_strict(line) for line in lines]


def decode_json_seq(body: bytes) -> tuple[list[Any], int]:
    """Decode an RFC 7464 sequence, returning the items and the count of dropped texts."""
    items: list[Any] = []
    leading, *texts = body.split(RS)
    dropped = 1 if leading else 0
    for text in texts:
        if not text:
            continue
        try:
            value = loads_strict(text)
        except ValueError:
            dropped += 1
            continue
        if not isinstance(value, dict | list | str) and text[-1:] not in b" \t\r\n":
            dropped += 1
            continue
        items.append(value)
    return items, dropped


def decode_sse(body: bytes) -> list[dict[str, Any]]:
    """Parse text/event-stream into the objects that an OpenAPI 3.2 itemSchema validates.

    Each object carries the fields of its own event block, so "id" is not inherited from an
    earlier event the way the WHATWG last event ID string is.
    """
    text = body.decode("utf-8", errors="replace").removeprefix("\ufeff")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")[:-1]
    events: list[dict[str, Any]] = []
    data: list[str] = []
    fields: dict[str, Any] = {}
    for line in lines:
        if line == "":
            if data:
                events.append({**fields, "data": "\n".join(data)})
            data, fields = [], {}
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "data":
                data.append(value)
            elif name == "event" and value:
                fields["event"] = value
            elif name == "event":
                fields.pop("event", None)
            elif name == "id" and "\x00" not in value:
                fields["id"] = value
            elif name == "retry" and value.isascii() and value.isdigit():
                fields["retry"] = int(value)
    return events


def schema_validator(document: Mapping[str, Any], pointer: str) -> SchemaValidator:
    """Build a validator for the Schema Object at a JSON Pointer inside the document."""
    base = str(document.get("$self", "urn:oas32:lab"))
    resource: Resource[Any] = Resource.from_contents(document, default_specification=DRAFT202012)
    registry = Registry[Any]().with_resource(base, resource)
    validator: SchemaValidator = Draft202012Validator(
        {"$ref": f"{base}#{pointer}"}, registry=registry
    )
    return validator


def item_errors(document: Mapping[str, Any], pointer: str, items: Iterable[Any]) -> list[str]:
    """Apply the schema at the pointer to every item independently, as itemSchema requires."""
    validator = schema_validator(document, pointer)
    return [
        f"item[{index}]: {error.message}"
        for index, item in enumerate(items)
        for error in validator.iter_errors(item)
    ]


def content_errors(document: Mapping[str, Any], pointer: str, texts: Iterable[str]) -> list[str]:
    """Assert a contentSchema, which JSON Schema validators only collect as an annotation."""
    validator = schema_validator(document, pointer)
    findings: list[str] = []
    for index, text in enumerate(texts):
        try:
            value = loads_strict(text)
        except ValueError:
            findings.append(f"item[{index}]: content is not application/json")
            continue
        findings.extend(f"item[{index}]: {error.message}" for error in validator.iter_errors(value))
    return findings


def _encode(media_type: str, items: Sequence[Mapping[str, Any]]) -> Iterator[bytes]:
    for item in items:
        text = json.dumps(item, separators=(",", ":"))
        if media_type == "text/event-stream":
            yield f"event: item\nid: {item['id']}\ndata: {text}\n\n".encode()
        elif media_type == "application/json-seq":
            yield RS + text.encode() + b"\n"
        else:
            yield text.encode() + b"\n"


def negotiate(accept: str) -> str | None:
    """Pick the offered media type with the highest Accept weight, or None if none is acceptable."""
    weights: dict[str, float] = {}
    for element in accept.split(","):
        media_range, *parameters = (part.strip().lower() for part in element.split(";"))
        weight = 1.0
        for parameter in parameters:
            if parameter.startswith("q="):
                try:
                    weight = float(parameter[2:])
                except ValueError:
                    weight = 0.0
        weights[media_range] = weight if 0.0 <= weight <= 1.0 else 0.0

    def weigh(offer: str) -> float:
        for media_range in (offer, f"{offer.split('/')[0]}/*", "*/*"):
            if media_range in weights:
                return weights[media_range]
        return 0.0

    best = max(OFFERS, key=weigh)
    return best if weigh(best) > 0.0 else None


def _answer(query: Any, accept: str) -> Response:
    headers = {"Accept-Query": "application/json"}
    if list(schema_validator(build_document(), "/components/schemas/FeedQuery").iter_errors(query)):
        return JSONResponse({"title": "query cannot be processed"}, 422, headers)
    media_type = negotiate(accept or "*/*")
    if media_type is None:
        return JSONResponse({"title": "no acceptable representation"}, 406, headers)
    items = [item for item in FEED if query["q"] in item["title"]][: int(query.get("limit", 100))]
    if media_type == "application/json":
        return JSONResponse(items, headers=headers)
    return StreamingResponse(_encode(media_type, items), media_type=media_type, headers=headers)


async def _feed(request: Request) -> Response:
    headers = {"Accept-Query": "application/json"}
    accept = request.headers.get("accept", "application/json")
    if request.method == "PURGE":
        return Response(status_code=204)
    if request.method != "QUERY":
        pairs = parse_qs(request.url.query, keep_blank_values=True)
        fields: dict[str, Any] = {name: values[-1] for name, values in pairs.items()}
        if str(fields.get("limit", "")).isdecimal():
            fields["limit"] = int(fields["limit"])
        return _answer(fields, accept)
    content_type = request.headers.get("content-type")
    if content_type is None:
        return JSONResponse({"title": "QUERY requires Content-Type"}, 400, headers)
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse({"title": "unsupported query format"}, 415, headers)
    try:
        query = loads_strict(await request.body())
    except ValueError:
        return JSONResponse({"title": "content is not application/json"}, 400, headers)
    return _answer(query, accept)


def create_app() -> Starlette:
    """Serve the /feed path item of build_document(): QUERY, GET, and the PURGE extension method."""
    return Starlette(routes=[Route("/feed", _feed, methods=["QUERY", "GET", "PURGE"])])


def well_known_metadata_url(issuer: str, suffix: str = "oauth-authorization-server") -> str:
    """Insert the well-known suffix between the issuer host and path (RFC 8414 Section 3.1)."""
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.netloc or "?" in issuer or "#" in issuer:
        raise ValueError(f"issuer must be an https URL without query or fragment: {issuer!r}")
    return f"https://{parts.netloc}/.well-known/{suffix}{parts.path.rstrip('/')}"


def metadata_drift(scheme: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[str]:
    """Compare an oauth2 Security Scheme Object with the RFC 8414 document it points at."""
    findings: list[str] = []
    try:
        location = well_known_metadata_url(str(metadata.get("issuer", "")))
    except ValueError:
        location = ""
    if location != scheme.get("oauth2MetadataUrl"):
        findings.append("issuer does not match the URL the metadata was retrieved from")
    endpoints = {
        "authorizationUrl": "authorization_endpoint",
        "tokenUrl": "token_endpoint",
        "deviceAuthorizationUrl": "device_authorization_endpoint",
    }
    flows: Mapping[str, Mapping[str, Any]] = scheme.get("flows", {})
    for flow_name, flow in flows.items():
        for field, claim in endpoints.items():
            if field in flow and flow[field] != metadata.get(claim):
                findings.append(f"{flow_name}.{field} differs from {claim}")
    grants = metadata.get("grant_types_supported", ["authorization_code", "implicit"])
    if "deviceAuthorization" in flows and DEVICE_GRANT not in grants:
        findings.append("deviceAuthorization flow is not in grant_types_supported")
    return findings
