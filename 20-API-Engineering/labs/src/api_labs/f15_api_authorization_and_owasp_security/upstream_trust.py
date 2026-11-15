"""API9:2023 inventory drift and API10:2023 unsafe upstream consumption, made executable.

Part 1 (API9) enumerates the operations an app really serves, diffs them against its
OpenAPI document, and stamps RFC 9745 `Deprecation` / RFC 8594 `Sunset` lifecycle headers.
Part 2 (API10) consumes a third-party JSON API through one choke point that refuses
redirects, caps decoded bytes, enforces a total deadline, rejects duplicate keys, and
validates into a typed model, and verifies Standard Webhooks v1 signatures on pushes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal

import anyio
import httpx
from fastapi import FastAPI, Path, Query
from fastapi.responses import JSONResponse
from fastapi.routing import iter_route_contexts
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.routing import Mount

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence

    from starlette.routing import BaseRoute
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Part 1 - API9:2023 Improper Inventory Management
# ---------------------------------------------------------------------------

OPENAPI_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


@dataclass(frozen=True, slots=True, order=True)
class Operation:
    """One externally reachable (method, path template) pair."""

    method: str
    path: str


def served_operations(routes: Sequence[BaseRoute], prefix: str = "") -> frozenset[Operation]:
    """Walk the routing tree the way the router does: included routers and mounts too."""
    found: set[Operation] = set()
    for context in iter_route_contexts(routes):
        route = context.original_route
        if isinstance(route, Mount):
            found |= served_operations(route.routes, prefix + route.path)
            continue
        path, methods = context.path_format, context.methods
        if path is None or methods is None:  # WebSocket routes carry no HTTP methods
            continue
        found.update(Operation(m, prefix + path) for m in methods if m != "HEAD")
    return frozenset(found)


def documented_operations(openapi: Mapping[str, Any]) -> frozenset[Operation]:
    """Every operation the OpenAPI document admits to."""
    found: set[Operation] = set()
    paths: Mapping[str, Mapping[str, Any]] = openapi.get("paths", {})
    for path, item in paths.items():
        found.update(Operation(key.upper(), path) for key in item if key in OPENAPI_METHODS)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class InventoryDrift:
    undocumented: frozenset[Operation]  # served but absent from the spec: shadow APIs
    phantom: frozenset[Operation]  # in the spec but not served: stale documentation


def inventory_drift(app: FastAPI) -> InventoryDrift:
    served = served_operations(app.routes)
    documented = documented_operations(app.openapi())
    return InventoryDrift(undocumented=served - documented, phantom=documented - served)


def build_shadow_app() -> FastAPI:
    """A realistic drift: a hidden debug route and a mounted, forgotten v1 app."""
    app = FastAPI(title="orders")
    legacy = FastAPI(title="orders-v1")

    @app.get("/v2/orders/{order_id}")
    async def get_order(order_id: int) -> dict[str, int]:
        return {"id": order_id}

    @app.get("/v2/debug/config", include_in_schema=False)
    async def debug_config() -> dict[str, str]:
        return {"db": "postgresql://orders@db-primary/orders"}

    @legacy.get("/orders/{order_id}")
    async def get_order_v1(order_id: str) -> dict[str, str]:
        return {"id": order_id}  # v1 predates the integer id and the BOLA fix

    app.mount("/v1", legacy)
    return app


def deprecation_value(at: datetime) -> str:
    """RFC 9745 Section 2.1: an RFC 9651 Date item, `@` plus integer epoch seconds."""
    return f"@{int(at.timestamp())}"


def sunset_value(at: datetime) -> str:
    """RFC 8594 Section 3: an HTTP-date (IMF-fixdate, RFC 9110 Section 5.6.7)."""
    return format_datetime(at.astimezone(UTC), usegmt=True)


@dataclass(frozen=True, slots=True)
class ApiVersion:
    """One inventory row: a version prefix and its lifecycle dates."""

    prefix: str
    policy_url: str
    deprecated_at: datetime | None = None
    sunset_at: datetime | None = None

    def __post_init__(self) -> None:
        for moment in (self.deprecated_at, self.sunset_at):
            if moment is not None and moment.tzinfo is None:
                raise ValueError("lifecycle dates must be timezone-aware")
        if self.deprecated_at and self.sunset_at and self.sunset_at < self.deprecated_at:
            raise ValueError("RFC 9745 Section 4: Sunset MUST NOT precede Deprecation")

    def matches(self, path: str) -> bool:
        return path == self.prefix or path.startswith(self.prefix + "/")


class LifecycleMiddleware:
    """Pure ASGI: announce deprecation and sunset, and answer `410` once sunset passes."""

    def __init__(
        self, app: ASGIApp, versions: Sequence[ApiVersion], clock: Callable[[], datetime]
    ) -> None:
        self.app, self.versions, self.clock = app, tuple(versions), clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        version = None
        if scope["type"] == "http":
            version = next((v for v in self.versions if v.matches(scope["path"])), None)
        if version is None:
            await self.app(scope, receive, send)
            return
        extra: list[tuple[bytes, bytes]] = []
        if version.deprecated_at is not None:
            extra.append((b"deprecation", deprecation_value(version.deprecated_at).encode()))
            link = f'<{version.policy_url}>; rel="deprecation"'
            extra.append((b"link", link.encode()))
        if version.sunset_at is not None:
            extra.append((b"sunset", sunset_value(version.sunset_at).encode()))
            extra.append((b"link", f'<{version.policy_url}>; rel="sunset"'.encode()))
            if self.clock() >= version.sunset_at:
                await _send_problem(send, 410, "Gone", f"{version.prefix} is retired", extra)
                return

        async def send_with_lifecycle(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_lifecycle)


async def _send_problem(
    send: Send, status: int, title: str, detail: str, extra: list[tuple[bytes, bytes]]
) -> None:
    body = json.dumps({"type": "about:blank", "title": title, "status": status, "detail": detail})
    headers = [(b"content-type", b"application/problem+json"), *extra]
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body.encode()})


# ---------------------------------------------------------------------------
# Part 2 - API10:2023 Unsafe Consumption of APIs
# ---------------------------------------------------------------------------


class UpstreamFailure(StrEnum):
    INSECURE_URL = "insecure_url"
    HOST_NOT_ALLOWED = "host_not_allowed"
    REDIRECT_REFUSED = "redirect_refused"
    BAD_STATUS = "bad_status"
    BAD_CONTENT_TYPE = "bad_content_type"
    TOO_LARGE = "too_large"
    DEADLINE = "deadline"
    MALFORMED_JSON = "malformed_json"
    DUPLICATE_KEY = "duplicate_key"
    SCHEMA = "schema"
    IDENTITY_MISMATCH = "identity_mismatch"


class UpstreamError(Exception):
    def __init__(self, failure: UpstreamFailure, detail: str) -> None:
        super().__init__(f"{failure.value}: {detail}")
        self.failure = failure


@dataclass(frozen=True, slots=True)
class UpstreamPolicy:
    allowed_hosts: frozenset[str]
    max_body_bytes: int = 64 * 1024  # counted after Content-Encoding is undone
    deadline_seconds: float = 2.0  # wall clock for the whole exchange, not per read


class PartnerPrice(BaseModel):
    """What we accept from the pricing partner; unknown fields are dropped, not kept."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    sku: Annotated[str, Field(pattern=r"^[A-Z0-9-]{1,32}$")]
    unit_price_cents: Annotated[int, Field(ge=0, le=100_000_000)]
    currency: Literal["USD", "EUR", "GBP"]


class EnrichedAddress(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)

    street: Annotated[str, Field(min_length=1, max_length=200)]


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise UpstreamError(UpstreamFailure.DUPLICATE_KEY, f"duplicate member {key!r}")
        obj[key] = value
    return obj


def _no_constants(token: str) -> Any:
    raise UpstreamError(UpstreamFailure.MALFORMED_JSON, f"non-JSON constant {token}")


def decode_upstream[M: BaseModel](body: bytes, model: type[M]) -> M:
    """Validate first (jiter bounds nesting), then reject what pydantic silently accepts."""
    try:
        validated = model.model_validate_json(body)
    except ValidationError as exc:
        failure = (
            UpstreamFailure.MALFORMED_JSON
            if exc.errors()[0]["type"] == "json_invalid"
            else UpstreamFailure.SCHEMA
        )
        raise UpstreamError(failure, str(exc.error_count())) from None
    try:
        json.loads(body, object_pairs_hook=_unique_keys, parse_constant=_no_constants)
    except (ValueError, RecursionError) as exc:
        raise UpstreamError(UpstreamFailure.MALFORMED_JSON, type(exc).__name__) from None
    return validated


async def fetch_validated[M: BaseModel](
    client: httpx.AsyncClient, url: str, model: type[M], policy: UpstreamPolicy
) -> M:
    """The single choke point for third-party reads."""
    target = httpx.URL(url)
    if target.scheme != "https":
        raise UpstreamError(UpstreamFailure.INSECURE_URL, target.scheme)
    if target.host not in policy.allowed_hosts:
        raise UpstreamError(UpstreamFailure.HOST_NOT_ALLOWED, target.host)
    body = bytearray()
    try:
        with anyio.fail_after(policy.deadline_seconds):
            async with client.stream("GET", target, follow_redirects=False) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "")
                    raise UpstreamError(UpstreamFailure.REDIRECT_REFUSED, location)
                if response.status_code != 200:
                    raise UpstreamError(UpstreamFailure.BAD_STATUS, str(response.status_code))
                media_type = response.headers.get("content-type", "").split(";")[0]
                media_type = media_type.strip().lower()
                if media_type != "application/json" and not media_type.endswith("+json"):
                    raise UpstreamError(UpstreamFailure.BAD_CONTENT_TYPE, media_type)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > policy.max_body_bytes:
                    raise UpstreamError(UpstreamFailure.TOO_LARGE, f"declared {declared}")
                async for chunk in response.aiter_bytes():  # decoded bytes
                    body += chunk
                    if len(body) > policy.max_body_bytes:
                        raise UpstreamError(UpstreamFailure.TOO_LARGE, f">{len(body) - 1}")
    except TimeoutError:
        raise UpstreamError(UpstreamFailure.DEADLINE, f"{policy.deadline_seconds}s") from None
    return decode_upstream(bytes(body), model)


async def fetch_naive(client: httpx.AsyncClient, url: str) -> Any:
    """The vulnerable shape: follow anything, read everything, trust the dict."""
    response = await client.get(url, follow_redirects=True)
    return response.json()


def naive_total(raw: Mapping[str, Any], qty: int) -> Any:
    return raw["unit_price_cents"] * qty


def store_address_naive(conn: sqlite3.Connection, user_id: int, street: str) -> None:
    sql = f"INSERT INTO addresses (user_id, street) VALUES ({user_id}, '{street}')"  # noqa: S608 - deliberately injectable
    conn.execute(sql)


def store_address_safe(conn: sqlite3.Connection, user_id: int, street: str) -> None:
    conn.execute("INSERT INTO addresses (user_id, street) VALUES (?, ?)", (user_id, street))


def _problem(status: int, detail: str) -> JSONResponse:
    title = "Gateway Timeout" if status == 504 else "Bad Gateway"
    content = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    return JSONResponse(content, status_code=status, media_type="application/problem+json")


def build_quote_app(upstream: httpx.AsyncClient, policy: UpstreamPolicy, base_url: str) -> FastAPI:
    app = FastAPI()

    @app.get("/quotes/{sku}")
    async def quote(
        sku: Annotated[str, Path(pattern=r"^[A-Z0-9-]{1,32}$")],
        qty: Annotated[int, Query(ge=1, le=1000)] = 1,
    ) -> JSONResponse:
        try:
            price = await fetch_validated(
                upstream, f"{base_url}/v1/prices/{sku}", PartnerPrice, policy
            )
            if price.sku != sku:
                raise UpstreamError(UpstreamFailure.IDENTITY_MISMATCH, price.sku)
        except UpstreamError as exc:
            return _problem(504 if exc.failure is UpstreamFailure.DEADLINE else 502, exc.failure)
        total = price.unit_price_cents * qty
        return JSONResponse({"sku": sku, "currency": price.currency, "total_cents": total})

    return app


# --- Inbound pushes: Standard Webhooks v1 (HMAC-SHA256) -----------------------


class WebhookRejected(Exception):
    pass


def sign_webhook(key: bytes, msg_id: str, timestamp: int, body: bytes) -> str:
    to_sign = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


@dataclass(slots=True)
class WebhookVerifier:
    keys: tuple[bytes, ...]  # every active secret, so rotation needs no downtime
    tolerance_seconds: int = 300  # the 5 minutes the reference libraries use
    seen_ids: set[str] = field(default_factory=set[str])

    def verify(self, headers: Mapping[str, str], body: bytes, now: int) -> None:
        try:
            msg_id = headers["webhook-id"]
            timestamp = int(headers["webhook-timestamp"])
            offered = headers["webhook-signature"].split(" ")
        except (KeyError, ValueError):
            raise WebhookRejected("missing or malformed webhook headers") from None
        expected = [sign_webhook(key, msg_id, timestamp, body) for key in self.keys]
        if not any(hmac.compare_digest(o, e) for o in offered for e in expected):
            raise WebhookRejected("no matching signature")
        if abs(now - timestamp) > self.tolerance_seconds:
            raise WebhookRejected("timestamp outside tolerance")
        if msg_id in self.seen_ids:
            raise WebhookRejected("replayed webhook-id")
        self.seen_ids.add(msg_id)
