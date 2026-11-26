"""Automatic Persisted Queries, trusted documents, and a shared HTTP cache in front of GraphQL.

Four small pieces, all in-process and deterministic:

* `ApqServer`: the origin. It follows the order of Apollo Server 5.5.2's request pipeline: resolve
  the source text (hash lookup or hash check), parse, validate, refuse mutations over safe
  methods, and only then register the text, so a query that fails any step is never persisted.
* `EdgeCache`: a shared HTTP cache in the style of RFC 9111, keyed by method plus target URI, so
  `POST` bypasses it and a hash-only `GET` is cacheable.
* `ApqClient`: the client half of the handshake (hash-only first, one retry with the full text).
* `build_app`: a Starlette binding, so the same behavior can be exercised through real ASGI.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import parse_qsl, quote, urlsplit

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    build_schema,
    execute_sync,  # pyright: ignore[reportUnknownVariableType]
    get_named_type,
    get_operation_ast,
    is_composite_type,
    parse,
    print_ast,
    validate,
)
from graphql.error import GraphQLSyntaxError
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from graphql import DocumentNode, GraphQLNamedType, GraphQLResolveInfo
    from starlette.requests import Request

Clock = Callable[[], float]
JsonObject = dict[str, Any]

CODE_NOT_FOUND: Final = "PERSISTED_QUERY_NOT_FOUND"
CODE_NOT_SUPPORTED: Final = "PERSISTED_QUERY_NOT_SUPPORTED"
CODE_NOT_ALLOWED: Final = "PERSISTED_QUERY_NOT_ALLOWED"
UNCACHED_PQ_ERROR: Final = "private, no-cache, must-revalidate"


def sha256_hex(text: str) -> str:
    """SHA-256 of the UTF-8 bytes of `text`, lowercase hex: the APQ identifier."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ManualClock:
    """A clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """The parts of an HTTP/1.1 request the lab needs; header names are lowercase."""

    method: str
    target: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes = b""

    @property
    def wire_bytes(self) -> int:
        """Request-target bytes plus body bytes; headers are excluded on purpose."""
        return len(self.target.encode("ascii")) + len(self.body)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> JsonObject:
        decoded: JsonObject = json.loads(self.body)
        return decoded


Transport = Callable[[HttpRequest], HttpResponse]


class ApqRegistry:
    """Bounded hash -> query-text store: LRU eviction, optional TTL, per-entry size cap."""

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        max_query_bytes: int = 64 * 1024,
        ttl_seconds: float | None = None,
        clock: Clock = lambda: 0.0,
    ) -> None:
        self._entries: OrderedDict[str, tuple[str, float | None]] = OrderedDict()
        self._max_entries = max_entries
        self._max_query_bytes = max_query_bytes
        self._ttl = ttl_seconds
        self._clock = clock
        self.evictions = 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, digest: str) -> str | None:
        entry = self._entries.get(digest)
        if entry is None:
            return None
        text, expires_at = entry
        if expires_at is not None and self._clock() >= expires_at:
            del self._entries[digest]
            return None
        self._entries.move_to_end(digest)
        return text

    def put(self, digest: str, text: str) -> bool:
        """Store `text`; return False (and store nothing) when it exceeds the size cap."""
        if len(text.encode("utf-8")) > self._max_query_bytes:
            return False
        expires_at = None if self._ttl is None else self._clock() + self._ttl
        self._entries[digest] = (text, expires_at)
        self._entries.move_to_end(digest)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1
        return True


def build_manifest(queries: Iterable[str], *, normalize: bool = True) -> dict[str, str]:
    """Build-time trusted-documents manifest: sha256 of each (normalized) text -> the text."""
    manifest: dict[str, str] = {}
    for query in queries:
        text = print_ast(parse(query)) if normalize else query
        manifest[sha256_hex(text)] = text
    return manifest


SCHEMA_SDL: Final = """
type Query {
  product(id: ID!): Product
  viewer: Viewer
  clock: String!
}
type Mutation {
  setPrice(id: ID!, price: Float!): Product
}
type Product {
  id: ID!
  name: String!
  price: Float!
  reviews(limit: Int = 10): [Review!]!
}
type Review {
  body: String!
}
type Viewer {
  name: String!
  email: String!
}
"""


@dataclass(frozen=True, slots=True)
class CacheHint:
    """A `maxAge` and scope hint for one field coordinate; `None` means "no hint"."""

    max_age: int | None = None
    private: bool = False


DEFAULT_HINTS: Final[Mapping[str, CacheHint]] = {
    "Query.product": CacheHint(max_age=60),
    "Product.reviews": CacheHint(max_age=30),
    "Query.viewer": CacheHint(max_age=60, private=True),
}


@dataclass(frozen=True, slots=True)
class CachePolicy:
    max_age: int
    private: bool

    def header_value(self) -> str:
        if self.max_age <= 0:
            return "no-store"
        return f"max-age={self.max_age}, {'private' if self.private else 'public'}"


def compute_cache_policy(
    schema: GraphQLSchema,
    document: DocumentNode,
    operation: OperationDefinitionNode,
    hints: Mapping[str, CacheHint],
) -> CachePolicy:
    """Static version of Apollo's rule: min `maxAge` over hinted fields, `private` if any is.

    A root field or a field returning a composite type with no hint counts as `maxAge` 0.
    A non-root scalar with no hint contributes nothing.
    """
    fragments = {
        d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)
    }
    root = schema.get_root_type(operation.operation)
    contributions: list[CacheHint] = []

    def walk(selection_set: SelectionSetNode, parent: GraphQLObjectType, is_root: bool) -> None:
        for selection in selection_set.selections:
            if isinstance(selection, FieldNode):
                name = selection.name.value
                if name == "__typename" and not is_root:
                    continue
                definition = parent.fields.get(name)
                hint = hints.get(f"{parent.name}.{name}", CacheHint())
                target = cast(
                    "GraphQLNamedType | None",
                    get_named_type(definition.type) if definition is not None else None,
                )
                composite = target is not None and is_composite_type(target)
                max_age = hint.max_age
                if max_age is None and (is_root or composite or definition is None):
                    max_age = 0
                contributions.append(CacheHint(max_age, hint.private))
                if selection.selection_set is not None and isinstance(target, GraphQLObjectType):
                    walk(selection.selection_set, target, False)
            elif isinstance(selection, InlineFragmentNode):
                walk(selection.selection_set, parent, is_root)
            elif isinstance(selection, FragmentSpreadNode):
                spread = fragments.get(selection.name.value)
                if spread is not None:
                    walk(spread.selection_set, parent, is_root)

    if root is not None:
        walk(operation.selection_set, root, True)
    ages = [c.max_age for c in contributions if c.max_age is not None]
    return CachePolicy(
        max_age=min(ages) if ages else 0,
        private=any(c.private for c in contributions),
    )


class PersistedQueryMode(Enum):
    APQ = "apq"  # register on the fly, any client can add entries
    ALLOWLIST = "allowlist"  # build-time manifest only, nothing is ever registered
    DISABLED = "disabled"  # answer PERSISTED_QUERY_NOT_SUPPORTED


@dataclass(slots=True)
class ServerStats:
    requests: int = 0
    executed: int = 0
    not_found: int = 0
    not_supported: int = 0
    hits: int = 0
    registered: int = 0
    registration_rejected: int = 0
    forbidden: int = 0


class _Rejected(Exception):
    def __init__(self, response: HttpResponse) -> None:
        super().__init__(response.status)
        self.response = response


@dataclass(frozen=True, slots=True)
class _Params:
    query: str | None
    variables: JsonObject | None
    operation_name: str | None
    extensions: JsonObject


def _response(status: int, payload: JsonObject, headers: Mapping[str, str]) -> HttpResponse:
    merged = {"content-type": "application/json", **headers}
    return HttpResponse(status, merged, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _error(
    status: int,
    message: str,
    *,
    code: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    error: JsonObject = {"message": message}
    if code is not None:
        error["extensions"] = {"code": code}
    return _response(status, {"errors": [error]}, {"cache-control": "no-store", **(headers or {})})


class ApqServer:
    """GraphQL origin with an APQ registry or a trusted-documents manifest."""

    def __init__(
        self,
        *,
        registry: ApqRegistry | None = None,
        manifest: Mapping[str, str] | None = None,
        mode: PersistedQueryMode = PersistedQueryMode.APQ,
        require_id: bool = False,
        hints: Mapping[str, CacheHint] = DEFAULT_HINTS,
        vary: tuple[str, ...] = (),
        enable_query_method: bool = False,
    ) -> None:
        self.registry = registry if registry is not None else ApqRegistry()
        self.manifest = dict(manifest or {})
        self.mode = mode
        self.stats = ServerStats()
        self._require_id = require_id
        self._hints = hints
        self._vary = vary
        self._allowed: set[str] = {"GET", "POST"}
        if enable_query_method:
            self._allowed.add("QUERY")
        self._schema = build_schema(SCHEMA_SDL)
        self._prices = {"1": 10.0, "2": 25.5}
        self._ticks = 0

    # -- resolvers -----------------------------------------------------------------------------
    def _product(self, product_id: str) -> JsonObject | None:
        if product_id not in self._prices:
            return None

        def reviews(_info: GraphQLResolveInfo[Any], limit: int = 10) -> list[JsonObject]:
            return [{"body": f"review {n} of {product_id}"} for n in range(1, limit + 1)]

        return {
            "id": product_id,
            "name": f"product-{product_id}",
            "price": self._prices[product_id],
            "reviews": reviews,
        }

    def _root_value(self) -> JsonObject:
        def product(_info: GraphQLResolveInfo[Any], id: str) -> JsonObject | None:
            return self._product(id)

        def viewer(info: GraphQLResolveInfo[Any]) -> JsonObject | None:
            name = info.context.get("viewer")
            return None if name is None else {"name": name, "email": f"{name}@example.invalid"}

        def clock(_info: GraphQLResolveInfo[Any]) -> str:
            self._ticks += 1
            return f"tick-{self._ticks}"

        def set_price(_info: GraphQLResolveInfo[Any], id: str, price: float) -> JsonObject | None:
            if id in self._prices:
                self._prices[id] = price
            return self._product(id)

        return {"product": product, "viewer": viewer, "clock": clock, "setPrice": set_price}

    # -- request pipeline ----------------------------------------------------------------------
    def handle(self, request: HttpRequest) -> HttpResponse:
        self.stats.requests += 1
        try:
            return self._handle(request)
        except _Rejected as rejected:
            return rejected.response

    def _handle(self, request: HttpRequest) -> HttpResponse:
        method = request.method.upper()
        if method not in self._allowed:
            raise _Rejected(
                _error(
                    405, "Method not allowed", headers={"allow": ", ".join(sorted(self._allowed))}
                )
            )
        params = self._parse_params(request, method)
        query, digest, register = self._resolve_source(params)
        document = self._parse_and_validate(query)
        operation = get_operation_ast(document, params.operation_name)
        if operation is None:
            raise _Rejected(_error(422, "Unknown or ambiguous operation"))
        if method != "POST" and operation.operation is not OperationType.QUERY:
            raise _Rejected(
                _error(
                    405,
                    f"{method} requests only support query operations",
                    headers={"allow": "POST"},
                )
            )
        if register:
            self._register(digest, query)

        authorization = request.headers.get("authorization", "")
        viewer = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None
        )
        self.stats.executed += 1
        result = execute_sync(
            self._schema,
            document,
            root_value=self._root_value(),
            context_value={"viewer": viewer},
            variable_values=params.variables,
            operation_name=params.operation_name,
        )
        policy = compute_cache_policy(self._schema, document, operation, self._hints)
        cacheable = not result.errors and policy.max_age > 0
        headers = {"cache-control": policy.header_value() if cacheable else "no-store"}
        if cacheable and self._vary:
            headers["vary"] = ", ".join(self._vary)
        status = 422 if result.errors and result.data is None else 200
        return _response(status, cast("JsonObject", result.formatted), headers)

    def _parse_params(self, request: HttpRequest, method: str) -> _Params:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        raw: JsonObject
        if method == "GET":
            if content_type and content_type != "application/json":
                raise _Rejected(_error(415, "GET requests may not carry another content type"))
            raw = {}
            for name, value in parse_qsl(urlsplit(request.target).query, keep_blank_values=True):
                if name in raw:
                    raise _Rejected(_error(400, f"The '{name}' search parameter may be given once"))
                raw[name] = value
            for name in ("variables", "extensions"):
                if raw.get(name):
                    raw[name] = self._json_param(name, raw[name])
                else:
                    raw.pop(name, None)
        else:
            if content_type != "application/json":
                raise _Rejected(_error(415, "Content-Type must be application/json"))
            try:
                decoded = json.loads(request.body)
            except ValueError:
                raise _Rejected(_error(400, "Unable to parse request body as JSON")) from None
            if not isinstance(decoded, dict):
                raise _Rejected(_error(422, "The request body must be a JSON object"))
            raw = cast("JsonObject", decoded)
        query: Any = raw.get("query")
        variables: Any = raw.get("variables")
        operation_name: Any = raw.get("operationName")
        extensions: Any = raw.get("extensions") or {}
        if (
            not (query is None or isinstance(query, str))
            or not (variables is None or isinstance(variables, dict))
            or not (operation_name is None or isinstance(operation_name, str))
            or not isinstance(extensions, dict)
        ):
            raise _Rejected(_error(422, "Not a well-formed GraphQL-over-HTTP request"))
        return _Params(
            query,
            cast("JsonObject | None", variables),
            operation_name,
            cast("JsonObject", extensions),
        )

    @staticmethod
    def _json_param(name: str, value: str) -> JsonObject:
        try:
            decoded = json.loads(value)
        except ValueError:
            raise _Rejected(
                _error(400, f"The {name} search parameter contains invalid JSON")
            ) from None
        if not isinstance(decoded, dict):
            raise _Rejected(_error(400, f"The {name} search parameter must be a JSON object"))
        return cast("JsonObject", decoded)

    def _pq_error(self, message: str, code: str) -> _Rejected:
        # 200 and uncached on purpose (Apollo Server 5.5.2 getPersistedQueryErrorHttp): the client
        # must see the body to retry, and no cache may keep it, because the next attempt succeeds.
        if code == CODE_NOT_FOUND:
            self.stats.not_found += 1
        else:
            self.stats.not_supported += 1
        return _Rejected(_error_200(message, code))

    def _forbid(self, message: str) -> _Rejected:
        self.stats.forbidden += 1
        return _Rejected(_error(403, message, code=CODE_NOT_ALLOWED))

    def _resolve_source(self, params: _Params) -> tuple[str, str, bool]:
        """Return (query text, digest, whether to register it after the pipeline succeeds)."""
        persisted = params.extensions.get("persistedQuery")
        if persisted is None:
            return self._plain_source(params)
        if self.mode is PersistedQueryMode.DISABLED:
            raise self._pq_error("PersistedQueryNotSupported", CODE_NOT_SUPPORTED)
        if not isinstance(persisted, dict):
            raise _Rejected(_error(400, "Unsupported persisted query version"))
        persisted_fields = cast("JsonObject", persisted)
        if persisted_fields.get("version") != 1:
            raise _Rejected(_error(400, "Unsupported persisted query version"))
        digest: Any = persisted_fields.get("sha256Hash")
        if not isinstance(digest, str):
            raise _Rejected(_error(400, "persistedQuery.sha256Hash must be a string"))
        allowlist = self.mode is PersistedQueryMode.ALLOWLIST
        if params.query is None:
            text = self.manifest.get(digest) if allowlist else self.registry.get(digest)
            if text is None:
                if allowlist:
                    raise self._forbid("Unknown persisted query id")
                raise self._pq_error("PersistedQueryNotFound", CODE_NOT_FOUND)
            self.stats.hits += 1
            return text, digest, False
        if sha256_hex(params.query) != digest:
            # Apollo Server: "This prevents hash hijacking" (requestPipeline.ts).
            raise _Rejected(_error(400, "provided sha does not match query"))
        if allowlist:
            if self._require_id or digest not in self.manifest:
                raise self._forbid("Operation text is not allowed")
            return params.query, digest, False
        return params.query, digest, True

    def _plain_source(self, params: _Params) -> tuple[str, str, bool]:
        if not params.query:
            raise _Rejected(
                _error(400, "Operations need a non-empty `query` or a `persistedQuery` extension")
            )
        digest = sha256_hex(params.query)
        if self.mode is PersistedQueryMode.ALLOWLIST and (
            self._require_id or digest not in self.manifest
        ):
            raise self._forbid("Operation text is not allowed")
        return params.query, digest, False

    def _parse_and_validate(self, query: str) -> DocumentNode:
        try:
            document = parse(query)
        except GraphQLSyntaxError as exc:
            raise _Rejected(_error(400, exc.message)) from None
        errors = validate(self._schema, document)
        if errors:
            payload = {"errors": [{"message": e.message} for e in errors]}
            raise _Rejected(_response(422, payload, {"cache-control": "no-store"}))
        return document

    def _register(self, digest: str, query: str) -> None:
        if self.registry.put(digest, query):
            self.stats.registered += 1
        else:
            self.stats.registration_rejected += 1


def _error_200(message: str, code: str) -> HttpResponse:
    payload = {"errors": [{"message": message, "extensions": {"code": code}}]}
    return _response(200, payload, {"cache-control": UNCACHED_PQ_ERROR})


# -- shared cache ------------------------------------------------------------------------------


def parse_cache_control(value: str) -> dict[str, str | None]:
    """Parse a `Cache-Control` value into lowercase directive -> optional argument."""
    directives: dict[str, str | None] = {}
    for part in value.split(","):
        name, separator, argument = part.strip().partition("=")
        if name:
            directives[name.lower()] = argument.strip().strip('"') if separator else None
    return directives


def cache_key(request: HttpRequest) -> tuple[str, ...]:
    """Method plus target URI; `QUERY` also keys on type and body (RFC 10008 Section 2.7)."""
    method = request.method.upper()
    if method == "QUERY":
        return (
            method,
            request.target,
            request.headers.get("content-type", ""),
            _body_digest(request),
        )
    return (method, request.target)


def _body_digest(request: HttpRequest) -> str:
    return hashlib.sha256(request.body).hexdigest()


@dataclass(slots=True)
class EdgeStats:
    hits: int = 0
    misses: int = 0
    bypasses: int = 0
    stores: int = 0
    too_long: int = 0


@dataclass(slots=True)
class _Stored:
    response: HttpResponse
    stored_at: float
    ttl: float
    vary: tuple[tuple[str, str | None], ...]


class EdgeCache:
    """A shared cache: stores only what RFC 9111 Section 3 lets a shared cache store."""

    def __init__(
        self,
        origin: Transport,
        *,
        clock: Clock,
        max_url_bytes: int = 8192,
        heuristic_ttl: float = 0.0,
        cacheable_methods: frozenset[str] = frozenset({"GET", "HEAD"}),
    ) -> None:
        self._origin = origin
        self._clock = clock
        self._max_url_bytes = max_url_bytes
        self._heuristic_ttl = heuristic_ttl
        self._methods = cacheable_methods
        self._store: dict[tuple[str, ...], _Stored] = {}
        self.stats = EdgeStats()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.wire_bytes - len(request.body) > self._max_url_bytes:
            self.stats.too_long += 1
            return HttpResponse(414, {"content-type": "text/plain"}, b"URI Too Long")
        if request.method.upper() not in self._methods:
            self.stats.bypasses += 1
            return self._tag(self._origin(request), "BYPASS")
        key = cache_key(request)
        stored = self._lookup(key, request)
        if stored is not None:
            self.stats.hits += 1
            age = int(self._clock() - stored.stored_at)
            return self._tag(
                replace(stored.response, headers={**stored.response.headers, "age": str(age)}),
                "HIT",
            )
        self.stats.misses += 1
        response = self._origin(request)
        self._maybe_store(key, request, response)
        return self._tag(response, "MISS")

    @staticmethod
    def _tag(response: HttpResponse, state: str) -> HttpResponse:
        return replace(response, headers={**response.headers, "x-lab-edge": state})

    def _lookup(self, key: tuple[str, ...], request: HttpRequest) -> _Stored | None:
        stored = self._store.get(key)
        if stored is None:
            return None
        if self._clock() - stored.stored_at >= stored.ttl:
            del self._store[key]
            return None
        if any(request.headers.get(name) != value for name, value in stored.vary):
            return None
        return stored

    def _ttl(self, request: HttpRequest, response: HttpResponse) -> float | None:
        if response.status != 200:
            return None
        directives = parse_cache_control(response.headers.get("cache-control", ""))
        if {"no-store", "private", "no-cache"} & directives.keys():
            return None
        if "authorization" in request.headers and not (
            {"public", "s-maxage", "must-revalidate"} & directives.keys()
        ):
            return None  # RFC 9111 Section 3.5
        explicit = directives.get("s-maxage") or directives.get("max-age")
        ttl = float(explicit) if explicit is not None else self._heuristic_ttl
        return ttl if ttl > 0 else None

    def _maybe_store(
        self, key: tuple[str, ...], request: HttpRequest, response: HttpResponse
    ) -> None:
        vary = [v.strip().lower() for v in response.headers.get("vary", "").split(",") if v.strip()]
        ttl = self._ttl(request, response)
        if ttl is None or "*" in vary:
            return
        snapshot = tuple((name, request.headers.get(name)) for name in vary)
        self._store[key] = _Stored(response, self._clock(), ttl, snapshot)
        self.stats.stores += 1


# -- client ------------------------------------------------------------------------------------


def _encode_params(params: JsonObject, *, canonical: bool) -> str:
    names = sorted(params) if canonical else list(params)
    pairs: list[str] = []
    for name in names:
        value = params[name]
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, separators=(",", ":"), sort_keys=canonical)
        )
        pairs.append(f"{quote(name, safe='')}={quote(text, safe='')}")
    return "&".join(pairs)


@dataclass(frozen=True, slots=True)
class ClientResult:
    response: HttpResponse
    requests: tuple[HttpRequest, ...]

    @property
    def wire_bytes(self) -> int:
        return sum(r.wire_bytes for r in self.requests)


class ApqClient:
    """Client half of APQ: hash-only first, then exactly one retry with the full text."""

    def __init__(
        self,
        transport: Transport,
        *,
        endpoint: str = "/graphql",
        use_get_for_hashed_queries: bool = True,
        normalize_document: bool = True,
        canonical_urls: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._endpoint = endpoint
        self._use_get = use_get_for_hashed_queries
        self._normalize = normalize_document
        self._canonical = canonical_urls
        self._headers = dict(headers or {})
        self.apq_enabled = True

    def execute(
        self,
        query: str,
        variables: JsonObject | None = None,
        operation_name: str | None = None,
    ) -> ClientResult:
        document = parse(query)
        text = print_ast(document) if self._normalize else query
        is_mutation = any(
            isinstance(d, OperationDefinitionNode) and d.operation is OperationType.MUTATION
            for d in document.definitions
        )
        base: JsonObject = {}
        if operation_name is not None:
            base["operationName"] = operation_name
        if variables is not None:
            base["variables"] = variables
        extensions = {"persistedQuery": {"version": 1, "sha256Hash": sha256_hex(text)}}

        if not self.apq_enabled:
            first = self._post({**base, "query": text})
            return ClientResult(self._transport(first), (first,))
        hashed: JsonObject = {**base, "extensions": extensions}
        first = self._get(hashed) if self._use_get and not is_mutation else self._post(hashed)
        response = self._transport(first)
        code = _pq_error_code(response)
        if code is None:
            return ClientResult(response, (first,))
        if code == CODE_NOT_SUPPORTED:
            self.apq_enabled = False
            second = self._post({**base, "query": text})
        else:
            second = self._post({**base, "query": text, "extensions": extensions})
        return ClientResult(self._transport(second), (first, second))

    def _get(self, params: JsonObject) -> HttpRequest:
        target = f"{self._endpoint}?{_encode_params(params, canonical=self._canonical)}"
        return HttpRequest("GET", target, dict(self._headers))

    def _post(self, payload: JsonObject) -> HttpRequest:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=self._canonical).encode("utf-8")
        headers = {**self._headers, "content-type": "application/json"}
        return HttpRequest("POST", self._endpoint, headers, body)


def _pq_error_code(response: HttpResponse) -> str | None:
    """Read a persisted-query error code from the body, whatever the HTTP status is."""
    try:
        payload: Any = json.loads(response.body)
    except ValueError:
        return None
    try:
        codes = [str(error["extensions"]["code"]) for error in payload["errors"]]
    except (KeyError, TypeError):
        return None
    for code in codes:
        if code in (CODE_NOT_FOUND, CODE_NOT_SUPPORTED):
            return code
    return None


# -- ASGI binding ------------------------------------------------------------------------------


def build_app(server: ApqServer) -> Starlette:
    """Mount `server` at `/graphql` on Starlette; the binding adds no GraphQL behavior."""

    async def endpoint(request: Request) -> Response:
        query = request.url.query
        target = request.url.path + (f"?{query}" if query else "")
        headers = {name.lower(): value for name, value in request.headers.items()}
        result = server.handle(HttpRequest(request.method, target, headers, await request.body()))
        return Response(result.body, status_code=result.status, headers=dict(result.headers))

    return Starlette(routes=[Route("/graphql", endpoint, methods=["GET", "POST"])])
