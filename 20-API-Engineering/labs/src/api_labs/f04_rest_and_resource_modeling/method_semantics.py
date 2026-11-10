"""HTTP method safety and idempotency (RFC 9110 Section 9.2) as executable invariants.

The resource model is a pure function ``apply(store, request) -> Outcome`` so that the two
method properties become equations over server state:

* safe (RFC 9110 Section 9.2.1): ``apply(s, r).store == s``
* idempotent (RFC 9110 Section 9.2.2): ``apply(apply(s, r).store, r).store == apply(s, r).store``

``MethodSemanticsApp`` exposes the same model over ASGI, and ``send_with_retry`` shows the
client-side consequence: only requests whose repetition is harmless are retried.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import httpx
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send


@dataclass(frozen=True, slots=True)
class MethodProperties:
    """Registry row: the two properties every method registration must declare."""

    safe: bool
    idempotent: bool
    spec: str


# RFC 9110 Section 18.2 Table 7, plus PATCH (RFC 5789 Section 2) and QUERY (RFC 10008 Section 2).
METHOD_REGISTRY: Final[Mapping[str, MethodProperties]] = MappingProxyType(
    {
        "GET": MethodProperties(safe=True, idempotent=True, spec="RFC 9110 Section 9.3.1"),
        "HEAD": MethodProperties(safe=True, idempotent=True, spec="RFC 9110 Section 9.3.2"),
        "POST": MethodProperties(safe=False, idempotent=False, spec="RFC 9110 Section 9.3.3"),
        "PUT": MethodProperties(safe=False, idempotent=True, spec="RFC 9110 Section 9.3.4"),
        "DELETE": MethodProperties(safe=False, idempotent=True, spec="RFC 9110 Section 9.3.5"),
        "CONNECT": MethodProperties(safe=False, idempotent=False, spec="RFC 9110 Section 9.3.6"),
        "OPTIONS": MethodProperties(safe=True, idempotent=True, spec="RFC 9110 Section 9.3.7"),
        "TRACE": MethodProperties(safe=True, idempotent=True, spec="RFC 9110 Section 9.3.8"),
        "PATCH": MethodProperties(safe=False, idempotent=False, spec="RFC 5789 Section 2"),
        "QUERY": MethodProperties(safe=True, idempotent=True, spec="RFC 10008 Section 2"),
    }
)

# A method whose safety is unknown is treated as unsafe (RFC 9111 Section 4.4).
UNKNOWN_METHOD: Final = MethodProperties(safe=False, idempotent=False, spec="RFC 9111 Section 4.4")


def properties(method: str) -> MethodProperties:
    """Look up a method token exactly: tokens are case-sensitive (RFC 9110 Section 9.1)."""
    return METHOD_REGISTRY.get(method, UNKNOWN_METHOD)


def may_retry_automatically(method: str, *, idempotency_key: str | None = None) -> bool:
    """RFC 9110 Section 9.2.2: retry idempotent methods, others only with a means to know."""
    return properties(method).idempotent or idempotency_key is not None


def allow_header(methods: frozenset[str]) -> str:
    """Render an ``Allow`` value in a stable order, independent of ``PYTHONHASHSEED``."""
    return ", ".join(sorted(methods))


Document = Mapping[str, int]
MERGE_PATCH: Final = "application/merge-patch+json"
INCREMENT_PATCH: Final = "application/x-increment+json"
ITEM_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"})
COLLECTION_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "POST"})


@dataclass(frozen=True, slots=True)
class Store:
    """Immutable server state; every write returns a new ``Store``."""

    docs: Mapping[str, Document] = field(default_factory=dict[str, Document])
    next_id: int = 1
    replayed_keys: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class Call:
    """One HTTP request against ``/items`` or ``/items/{id}``."""

    method: str
    path: str
    body: Mapping[str, int | None] | None = None
    content_type: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    store: Store
    status: int
    body: object = None
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


Handler = Callable[[Store, Call], Outcome]


def _with_doc(store: Store, item_id: str, doc: Document | None) -> Store:
    docs = dict(store.docs)
    if doc is None:
        docs.pop(item_id, None)
    else:
        docs[item_id] = doc
    return replace(store, docs=docs)


def _item(store: Store, call: Call, item_id: str) -> Outcome:
    method, current = call.method, store.docs.get(item_id)
    if method in ("GET", "HEAD"):
        if current is None:
            return Outcome(store, 404)
        return Outcome(store, 200, None if method == "HEAD" else dict(current))
    if method == "PUT":
        new_doc = {k: v for k, v in (call.body or {}).items() if v is not None}
        # RFC 9110 Section 9.3.4: 201 when PUT creates, 200 or 204 when it replaces.
        status = 201 if current is None else 200
        return Outcome(_with_doc(store, item_id, new_doc), status, new_doc)
    if method == "DELETE":
        # Same end state on every repetition; only the response differs (204 then 404).
        if current is None:
            return Outcome(store, 404)
        return Outcome(_with_doc(store, item_id, None), 204)
    # PATCH: the method is not idempotent, but one patch format can be (RFC 5789 Section 2).
    if current is None:
        return Outcome(store, 404)
    patched = dict(current)
    if call.content_type == MERGE_PATCH:
        for key, value in (call.body or {}).items():
            if value is None:
                patched.pop(key, None)
            else:
                patched[key] = value
    elif call.content_type == INCREMENT_PATCH:
        for key, delta in (call.body or {}).items():
            patched[key] = patched.get(key, 0) + (delta or 0)
    else:
        return Outcome(store, 415)
    return Outcome(_with_doc(store, item_id, patched), 200, patched)


def _create(store: Store, call: Call) -> Outcome:
    key = call.idempotency_key
    if key is not None and key in store.replayed_keys:
        item_id = store.replayed_keys[key]
        return Outcome(store, 201, dict(store.docs.get(item_id, {})), {"location": item_id})
    item_id = f"/items/{store.next_id}"
    doc = {k: v for k, v in (call.body or {}).items() if v is not None}
    created = replace(_with_doc(store, item_id, doc), next_id=store.next_id + 1)
    if key is not None:
        created = replace(created, replayed_keys={**store.replayed_keys, key: item_id})
    # RFC 9110 Section 9.3.3: 201 with a Location for the resource the server named.
    return Outcome(created, 201, doc, {"location": item_id})


def apply(store: Store, call: Call) -> Outcome:
    """Execute one request; the returned ``Outcome.store`` is the post-request server state."""
    if call.method not in METHOD_REGISTRY:
        # Unrecognized method: 501, not 405 (RFC 9110 Section 9.1 and Section 15.6.2).
        return Outcome(store, 501)
    is_item = call.path.startswith("/items/")
    allowed = ITEM_METHODS if is_item else COLLECTION_METHODS
    if call.method not in allowed:
        # RFC 9110 Section 15.5.6: a 405 MUST carry Allow.
        return Outcome(store, 405, headers={"allow": allow_header(allowed)})
    if call.method == "OPTIONS":
        return Outcome(store, 204, headers={"allow": allow_header(allowed)})
    if is_item:
        return _item(store, call, call.path)
    if call.method == "POST":
        return _create(store, call)
    return Outcome(store, 200, None if call.method == "HEAD" else sorted(store.docs))


def check_method_contract(handler: Handler, store: Store, call: Call) -> tuple[str, ...]:
    """Return every RFC 9110 Section 9.2 property that ``handler`` violates for ``call``."""
    declared = properties(call.method)
    once = handler(store, call)
    twice = handler(once.store, call)
    problems: list[str] = []
    if declared.safe and once.store != store:
        problems.append(f"{call.method} is safe but changed server state")
    if declared.idempotent and twice.store != once.store:
        problems.append(f"{call.method} is idempotent but f(f(s)) != f(s)")
    return tuple(problems)


class MethodSemanticsApp:
    """ASGI adapter over ``apply``; holds the current ``Store`` between requests."""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store if store is not None else Store()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise TypeError(f"unsupported ASGI scope type {scope['type']!r}")
        request = StarletteRequest(scope, receive)
        raw = await request.body()
        body: dict[str, int | None] | None = None
        if raw:
            body = {str(k): v for k, v in (await request.json()).items()}
        call = Call(
            method=scope["method"],
            path=scope["path"],
            body=body,
            content_type=request.headers.get("content-type"),
            idempotency_key=request.headers.get("idempotency-key"),
        )
        outcome = apply(self.store, call)
        self.store = outcome.store
        headers = dict(outcome.headers)
        response: Response
        if outcome.body is None or outcome.status == 204:
            response = Response(status_code=outcome.status, headers=headers)
        else:
            response = JSONResponse(outcome.body, status_code=outcome.status, headers=headers)
        await response(scope, receive, send)


async def send_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: Mapping[str, int] | None = None,
    idempotency_key: str | None = None,
    attempts: int = 3,
    retry_any_method: bool = False,
) -> httpx.Response:
    """Retry transport failures only when repeating the request cannot double its effect.

    ``retry_any_method=True`` models the unsafe client the RFC warns about.
    """
    headers = {"idempotency-key": idempotency_key} if idempotency_key else {}
    retryable = retry_any_method or may_retry_automatically(method, idempotency_key=idempotency_key)
    attempt = 1
    while True:
        try:
            return await client.request(method, url, json=json, headers=headers)
        except httpx.TransportError:
            if not retryable or attempt >= attempts:
                raise
            attempt += 1
