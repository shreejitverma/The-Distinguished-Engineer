"""Lab: the OWASP API Security Top 10 (2023) as one reference API with ten switchable controls.

`build_app(store, controls)` serves the same routes whichever controls are on; each `Risk` maps
to exactly one control, and `audit` sends one benign request and one attack per risk. With every
control off the audit exploits all ten risks, with every control on it exploits none, and with
every control but one on it exploits exactly that one. The DNS table, the webhook targets, the
cloud metadata service, and the shipping partner are in-process fakes: no socket is opened.
"""

import base64
import ipaddress
import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Collection, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Final

import httpx
import jwt
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.routing import iter_route_contexts
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import MutableHeaders
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class Risk(StrEnum):
    API1 = "API1:2023"
    API2 = "API2:2023"
    API3 = "API3:2023"
    API4 = "API4:2023"
    API5 = "API5:2023"
    API6 = "API6:2023"
    API7 = "API7:2023"
    API8 = "API8:2023"
    API9 = "API9:2023"
    API10 = "API10:2023"


@dataclass(frozen=True, slots=True)
class ThreatModelRow:
    risk: Risk
    title: str
    control: str
    layer: str
    cwes: tuple[int, ...]


THREAT_MODEL: Final[tuple[ThreatModelRow, ...]] = (
    ThreatModelRow(
        Risk.API1,
        "Broken Object Level Authorization",
        "owner bound into the lookup: 404 for another owner's id",
        "query",
        (285, 639),
    ),
    ThreatModelRow(
        Risk.API2,
        "Broken Authentication",
        "HS256 pinned; signature, exp, aud, iss required",
        "edge dependency",
        (204, 307),
    ),
    ThreatModelRow(
        Risk.API3,
        "Broken Object Property Level Authorization",
        "extra=forbid input model and an allowlisted response_model",
        "schema",
        (213, 915),
    ),
    ThreatModelRow(
        Risk.API4,
        "Unrestricted Resource Consumption",
        "page size le=100 and a 16 KiB request body cap",
        "schema and ASGI middleware",
        (770, 400, 799),
    ),
    ThreatModelRow(
        Risk.API5,
        "Broken Function Level Authorization",
        "admin router carries a role dependency on every route",
        "router",
        (285,),
    ),
    ThreatModelRow(
        Risk.API6,
        "Unrestricted Access to Sensitive Business Flows",
        "per-account purchase cap and a minimum view-to-buy dwell",
        "domain",
        (),
    ),
    ThreatModelRow(
        Risk.API7,
        "Server Side Request Forgery",
        "https:443 only, every resolved address public, IP pinned, no redirects",
        "egress client",
        (918,),
    ),
    ThreatModelRow(
        Risk.API8,
        "Security Misconfiguration",
        "debug off, CORS origin allowlist, no-store and nosniff headers",
        "app configuration",
        (2, 16, 209, 319, 388, 444, 942),
    ),
    ThreatModelRow(
        Risk.API9,
        "Improper Inventory Management",
        "legacy v0 retired, docs off, routes diffed against the inventory",
        "deployment",
        (1059,),
    ),
    ThreatModelRow(
        Risk.API10,
        "Unsafe Consumption of APIs",
        "no redirects, 8 KiB read cap, strict schema on the partner response",
        "egress client",
        (20, 200, 319),
    ),
)

ALL_CONTROLS: Final[frozenset[Risk]] = frozenset(Risk)
NO_CONTROLS: Final[frozenset[Risk]] = frozenset()

SIGNING_KEY: Final = b"lab-only-hs256-signing-key-not-a-secret-0123"
AUDIENCE: Final = "orders-api"
ISSUER: Final = "https://idp.example"
MAX_PAGE: Final = 100
MAX_BODY_BYTES: Final = 16 * 1024
PER_ACCOUNT_LIMIT: Final = 1
MIN_DWELL_SECONDS: Final = 1.0
PARTNER_URL: Final = "https://rates.carrier.example/v2/quote"
MAX_UPSTREAM_BYTES: Final = 8 * 1024
UPSTREAM_TIMEOUT: Final = httpx.Timeout(2.0)
ALLOWED_ORIGINS: Final = ("https://app.example.com",)
NAT64_WELL_KNOWN: Final = ipaddress.IPv6Network("64:ff9b::/96")  # RFC 6052 Section 2.1
FAKE_IMDS_KEY: Final = "AKIAIOSFODNN7EXAMPLE"  # the AWS documentation example key id

# Every route this deployment is supposed to expose (API9): the inventory a CI job diffs against.
INVENTORY: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", "/v1/users/me"),
        ("PATCH", "/v1/users/me"),
        ("GET", "/v1/orders"),
        ("GET", "/v1/orders/{order_id}"),
        ("GET", "/v1/orders/{order_id}/invoice"),
        ("GET", "/v1/orders/{order_id}/shipping-quote"),
        ("GET", "/v1/drops/{sku}"),
        ("POST", "/v1/drops/{sku}/purchase"),
        ("POST", "/v1/webhooks/test"),
        ("GET", "/v1/admin/users"),
        ("DELETE", "/v1/admin/users/{user_id}"),
    }
)


@dataclass(slots=True)
class User:
    id: str
    email: str
    display_name: str
    role: str
    password_hash: str


@dataclass(frozen=True, slots=True)
class Order:
    id: int
    owner: str
    item: str
    total_cents: int
    ship_to: str


@dataclass(slots=True)
class FakeClock:
    now: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass(slots=True)
class Store:
    users: dict[str, User]
    orders: dict[int, Order]
    stock: dict[str, int]
    dns: dict[str, list[str]]
    clock: Callable[[], float]
    purchases: Counter[tuple[str, str]] = field(default_factory=Counter[tuple[str, str]])
    viewed_at: dict[tuple[str, str], float] = field(default_factory=dict[tuple[str, str], float])
    outbound: list[httpx.Request] = field(default_factory=list[httpx.Request])
    partner_mode: str = "honest"
    upstream_bytes_sent: int = 0

    def order_for(self, owner: str, order_id: int) -> Order | None:
        """The API1 control: the owner is part of the lookup key (`WHERE id = ? AND owner = ?`)."""
        order = self.orders.get(order_id)
        return order if order is not None and order.owner == owner else None

    def resolve(self, host: str) -> list[str]:
        """Fake getaddrinfo: IP literals resolve to themselves, names come from the table."""
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            if host not in self.dns:
                raise LookupError(host) from None
            return list(self.dns[host])


def seed_store(clock: Callable[[], float] | None = None) -> Store:
    """Two tenants' users, an admin, three orders, a limited drop, and a fake DNS table."""
    users = {
        "u-alice": User("u-alice", "alice@example.com", "Alice", "user", "argon2id$alice"),
        "u-bob": User("u-bob", "bob@example.com", "Bob", "user", "argon2id$bob"),
        "u-root": User("u-root", "root@example.com", "Root", "admin", "argon2id$root"),
    }
    orders = {
        1001: Order(1001, "u-alice", "keyboard", 12_900, "1 Alice Way, Springfield"),
        1002: Order(1002, "u-bob", "monitor", 34_900, "2 Bob Street, Shelbyville"),
        1003: Order(1003, "u-alice", "mouse", 4_900, "1 Alice Way, Springfield"),
    }
    dns = {
        # Globally routable addresses chosen for the fake table; nothing is contacted.
        "hooks.partner.example": ["93.184.215.14"],
        "rates.carrier.example": ["93.184.215.15"],
        "attacker.example": ["93.184.215.66"],
        "metadata.attacker.example": ["169.254.169.254"],
        "mixed.attacker.example": ["93.184.215.14", "10.0.0.5"],
        "nat64.attacker.example": ["64:ff9b::a9fe:a9fe"],
    }
    return Store(users, orders, {"console-x": 10}, dns, clock or FakeClock())


# ---------------------------------------------------------------- API2: tokens


def mint_token(
    sub: str,
    *,
    ttl: int = 3600,
    audience: str = AUDIENCE,
    key: bytes = SIGNING_KEY,
    now: float | None = None,
) -> str:
    issued = int(time.time() if now is None else now)
    claims = {"sub": sub, "aud": audience, "iss": ISSUER, "iat": issued, "exp": issued + ttl}
    return jwt.encode(claims, key, algorithm="HS256")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def forge_unsigned_token(claims: Mapping[str, object]) -> str:
    """An `alg: none` JWS: header and payload, then an empty signature after the last dot."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(dict(claims)).encode())
    return f"{header}.{payload}."


def verify_token(token: str) -> dict[str, Any] | None:
    """The API2 control: one pinned algorithm, a verified signature, and required claims."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            SIGNING_KEY,
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.InvalidTokenError:
        return None
    return claims


def read_token_unverified(token: str) -> dict[str, Any] | None:
    """The API2 bug: `verify_signature=False` also turns off every exp, aud, and iss check."""
    try:
        claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        return None
    return claims


# ---------------------------------------------------------------- API7: egress guard


class UnsafeTargetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    host: str
    address: str
    url: str


def embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """IPv4 addresses that IPv6 forms smuggle: mapped, 6to4, Teredo, and NAT64 (RFC 6052)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)
    return None


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        inner = embedded_ipv4(ip)
        if inner is not None:
            return is_public_address(inner)
    return ip.is_global and not ip.is_multicast


def guard_outbound_url(raw: str, resolve: Callable[[str], list[str]]) -> PinnedTarget:
    """The API7 control: parse once, allowlist scheme and port, check every resolved address,
    and return a URL pinned to the checked address so a second DNS answer cannot change it."""
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise UnsafeTargetError("unparseable url") from exc
    if url.scheme != "https":
        raise UnsafeTargetError("scheme must be https")
    if url.port not in (None, 443):
        raise UnsafeTargetError("port must be 443")
    if url.userinfo:
        raise UnsafeTargetError("userinfo is not allowed")
    try:
        addresses = [ipaddress.ip_address(a) for a in resolve(url.host)]
    except LookupError as exc:
        raise UnsafeTargetError("host does not resolve") from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise UnsafeTargetError("host resolves to a non-public address")
    pinned = addresses[0]
    return PinnedTarget(url.host, str(pinned), str(url.copy_with(host=str(pinned))))


# ---------------------------------------------------------------- the fake internet


class CountingStream(httpx.AsyncByteStream):
    """A 1 MiB upstream body delivered in 1 KiB chunks; counts what the producer handed over."""

    def __init__(self, store: Store, chunks: int = 1024) -> None:
        self.store = store
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        parts = [b'{"carrier":"ACME Freight","price_cents":1299,"eta_days":3,"pad":"']
        parts += [b"A" * 1024] * self.chunks
        parts.append(b'"}')
        for part in parts:
            self.store.upstream_bytes_sent += len(part)
            yield part


def fake_internet(store: Store) -> httpx.MockTransport:
    """Route each outbound request by the IP it would really connect to, then by Host."""

    async def handler(request: httpx.Request) -> httpx.Response:
        store.outbound.append(request)
        address = store.resolve(request.url.host)[0]
        name = request.headers.get("host", request.url.host).split(":")[0]
        if address == "169.254.169.254":
            creds = {"AccessKeyId": FAKE_IMDS_KEY, "Token": "lab-session-token"}
            return httpx.Response(200, json=creds)
        if not ipaddress.ip_address(address).is_global:
            return httpx.Response(200, text="internal admin console")
        if name == "rates.carrier.example":
            return partner_response(store)
        return httpx.Response(200, json={"received": True})

    return httpx.MockTransport(handler)


def partner_response(store: Store) -> httpx.Response:
    json_type = {"content-type": "application/json"}
    quote: dict[str, object] = {"carrier": "ACME Freight", "price_cents": 1299, "eta_days": 3}
    match store.partner_mode:
        case "honest":
            return httpx.Response(200, json=quote)
        case "negative":
            return httpx.Response(200, json={**quote, "price_cents": -50_000})
        case "redirect":
            return httpx.Response(308, headers={"location": "https://attacker.example/collect"})
        case "huge":
            return httpx.Response(200, headers=json_type, stream=CountingStream(store))
        case mode:
            raise ValueError(f"unknown partner mode {mode!r}")


# ---------------------------------------------------------------- API10: consuming the partner


class UpstreamError(Exception):
    pass


class ShippingQuote(BaseModel):
    model_config = ConfigDict(strict=True)
    carrier: str = Field(pattern=r"^[A-Za-z0-9 .&-]{1,40}$")
    price_cents: int = Field(ge=0, le=1_000_000)
    eta_days: int = Field(ge=1, le=60)


async def fetch_quote_safely(transport: httpx.AsyncBaseTransport, order: Order) -> ShippingQuote:
    """The API10 control: no redirects, a timeout, a cap on decoded bytes, a strict schema."""
    payload = {"order_id": order.id, "ship_to": order.ship_to}
    async with (
        httpx.AsyncClient(
            transport=transport, follow_redirects=False, timeout=UPSTREAM_TIMEOUT
        ) as client,
        client.stream("POST", PARTNER_URL, json=payload) as response,
    ):
        if response.status_code != 200:
            raise UpstreamError(f"partner status {response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():  # decoded bytes: a gzip bomb hits the cap too
            body += chunk
            if len(body) > MAX_UPSTREAM_BYTES:
                raise UpstreamError("partner response exceeds the read cap")
    try:
        return ShippingQuote.model_validate_json(bytes(body))
    except ValidationError as exc:
        raise UpstreamError("partner response failed validation") from exc


# ---------------------------------------------------------------- API3: schemas


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=64)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str


class WebhookTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)


# ---------------------------------------------------------------- API8: response headers


class SecurityHeadersMiddleware:
    """Adds `Cache-Control: no-store` and `nosniff` to every response the app itself sends."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Cache-Control", "no-store")
                headers.setdefault("X-Content-Type-Options", "nosniff")
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------- the application


def build_app(store: Store, controls: Collection[Risk] = ALL_CONTROLS) -> FastAPI:
    on = frozenset(controls)
    internet = fake_internet(store)
    docs = Risk.API9 not in on
    app = FastAPI(
        debug=Risk.API8 not in on,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    bearer = HTTPBearer(auto_error=False)

    async def current_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> User:
        challenge = {"WWW-Authenticate": 'Bearer realm="orders"'}
        if credentials is None:
            raise HTTPException(401, "missing bearer token", challenge)
        token = credentials.credentials
        claims = verify_token(token) if Risk.API2 in on else read_token_unverified(token)
        user = store.users.get(str(claims.get("sub"))) if claims is not None else None
        if user is None:
            challenge["WWW-Authenticate"] += ', error="invalid_token"'
            raise HTTPException(401, "invalid bearer token", challenge)
        return user

    def load_order(user: User, order_id: int) -> Order:
        if Risk.API1 in on:
            order = store.order_for(user.id, order_id)
        else:
            order = store.orders.get(order_id)  # the BOLA: any caller, any id
        if order is None:  # the same 404 for "absent" and "not yours": no existence oracle
            raise HTTPException(404, "order not found")
        return order

    async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role != "admin":
            raise HTTPException(403, "admin role required")
        return user

    # API1 and API8: object lookup, plus an endpoint with a latent bug for the 500 path.
    @app.get("/v1/orders/{order_id}")
    async def get_order(
        order_id: int, user: Annotated[User, Depends(current_user)]
    ) -> dict[str, Any]:
        return asdict(load_order(user, order_id))

    @app.get("/v1/orders/{order_id}/invoice")
    async def get_invoice(
        order_id: int, user: Annotated[User, Depends(current_user)]
    ) -> dict[str, Any]:
        load_order(user, order_id)
        raise RuntimeError(
            "invoice renderer failed: dsn=postgresql://orders_app@db.internal/orders"
        )

    # API4: page size.
    if Risk.API4 in on:

        @app.get("/v1/orders")
        async def list_orders(
            user: Annotated[User, Depends(current_user)],
            limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 20,
        ) -> list[dict[str, Any]]:
            return [asdict(o) for o in store.orders.values() if o.owner == user.id][:limit]

        app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)
    else:

        @app.get("/v1/orders")
        async def list_orders_unbounded(
            user: Annotated[User, Depends(current_user)], limit: Annotated[int, Query(ge=1)] = 20
        ) -> list[dict[str, Any]]:
            return [asdict(o) for o in store.orders.values() if o.owner == user.id][:limit]

    # API3: property-level authorization on input and output.
    if Risk.API3 in on:

        @app.get("/v1/users/me", response_model=UserOut)
        async def read_me(user: Annotated[User, Depends(current_user)]) -> Any:
            return asdict(user)  # response_model validation drops role and password_hash

        @app.patch("/v1/users/me", response_model=UserOut)
        async def update_me(patch: UserPatch, user: Annotated[User, Depends(current_user)]) -> Any:
            user.display_name = patch.display_name
            return asdict(user)
    else:

        @app.get("/v1/users/me")
        async def read_me_raw(user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
            return asdict(user)

        @app.patch("/v1/users/me")
        async def update_me_raw(
            request: Request, user: Annotated[User, Depends(current_user)]
        ) -> dict[str, Any]:
            changes = await request.json()
            for key, value in changes.items():  # mass assignment: every client field is bound
                if hasattr(user, key):
                    setattr(user, key, value)
            return asdict(user)

    # API5: function-level authorization at the router, deny by default.
    admin = APIRouter(
        prefix="/v1/admin",
        dependencies=[Depends(require_admin)] if Risk.API5 in on else [Depends(current_user)],
    )

    @admin.get("/users")
    async def list_users() -> list[dict[str, str]]:
        return [{"id": u.id, "email": u.email, "role": u.role} for u in store.users.values()]

    @admin.delete("/users/{user_id}", status_code=204)
    async def delete_user(user_id: str) -> None:
        store.users.pop(user_id, None)

    app.include_router(admin)

    # API6: a sensitive business flow.
    @app.get("/v1/drops/{sku}")
    async def view_drop(sku: str, user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        if sku not in store.stock:
            raise HTTPException(404, "unknown drop")
        store.viewed_at[(user.id, sku)] = store.clock()
        return {"sku": sku, "stock": store.stock[sku]}

    @app.post("/v1/drops/{sku}/purchase")
    async def purchase(sku: str, user: Annotated[User, Depends(current_user)]) -> dict[str, Any]:
        if sku not in store.stock:
            raise HTTPException(404, "unknown drop")
        if Risk.API6 in on:
            viewed = store.viewed_at.get((user.id, sku))
            elapsed = store.clock() - viewed if viewed is not None else 0.0
            if viewed is None or elapsed < MIN_DWELL_SECONDS:
                wait = str(max(1, int(MIN_DWELL_SECONDS - elapsed + 0.999)))
                raise HTTPException(429, "view-to-buy faster than a human", {"Retry-After": wait})
            if store.purchases[(user.id, sku)] >= PER_ACCOUNT_LIMIT:
                raise HTTPException(409, "per-account purchase limit reached")
        if store.stock[sku] <= 0:
            raise HTTPException(409, "sold out")
        store.stock[sku] -= 1
        store.purchases[(user.id, sku)] += 1
        return {"sku": sku, "owned": store.purchases[(user.id, sku)]}

    # API7: a user-supplied URL fetched by the server.
    @app.post("/v1/webhooks/test")
    async def test_webhook(
        body: WebhookTest, user: Annotated[User, Depends(current_user)]
    ) -> dict[str, Any]:
        if Risk.API7 not in on:
            async with httpx.AsyncClient(transport=internet, follow_redirects=True) as client:
                response = await client.get(body.url)
            return {"status": response.status_code, "body": response.text}  # raw echo
        try:
            target = guard_outbound_url(body.url, store.resolve)
        except UnsafeTargetError as exc:
            raise HTTPException(400, "webhook target not allowed") from exc
        async with httpx.AsyncClient(
            transport=internet, follow_redirects=False, timeout=UPSTREAM_TIMEOUT
        ) as client:
            response = await client.get(
                target.url, headers={"Host": target.host}, extensions={"sni_hostname": target.host}
            )
        return {"delivered": response.is_success, "status": response.status_code}

    # API10: data from an integrated third party.
    @app.get("/v1/orders/{order_id}/shipping-quote")
    async def shipping_quote(
        order_id: int, user: Annotated[User, Depends(current_user)]
    ) -> dict[str, Any]:
        order = load_order(user, order_id)
        if Risk.API10 not in on:
            async with httpx.AsyncClient(transport=internet, follow_redirects=True) as client:
                response = await client.post(
                    PARTNER_URL, json={"order_id": order.id, "ship_to": order.ship_to}
                )
            quote: dict[str, Any] = response.json()  # trusted as if it were our own data
            return quote
        try:
            return (await fetch_quote_safely(internet, order)).model_dump()
        except UpstreamError as exc:
            raise HTTPException(502, "shipping partner returned an unusable response") from exc

    # API9: the v0 API nobody retired; it predates the auth dependency.
    if Risk.API9 not in on:
        legacy = APIRouter(prefix="/v0")

        @legacy.get("/users/{user_id}")
        async def legacy_user(user_id: str) -> dict[str, Any]:
            user = store.users.get(user_id)
            if user is None:
                raise HTTPException(404, "not found")
            return asdict(user)

        app.include_router(legacy)

    # API8: transport-adjacent configuration.
    if Risk.API8 in on:
        app.add_middleware(SecurityHeadersMiddleware)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(ALLOWED_ORIGINS),
            allow_credentials=True,
            allow_methods=["GET", "PATCH", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    return app


def route_inventory(app: FastAPI) -> frozenset[tuple[str, str]]:
    """Every (method, path) the app will actually answer, HEAD excluded.

    FastAPI 0.162.0 keeps an included router as one `_IncludedRouter` node in `app.routes`, so
    walking `app.routes` misses every route under it; `iter_route_contexts` (the walker that
    `get_openapi` uses) expands them, including routes hidden with `include_in_schema=False`."""
    found: set[tuple[str, str]] = set()
    for context in iter_route_contexts(app.routes):
        if context.path is not None:
            found.update((m, context.path) for m in context.methods or () if m != "HEAD")
    return frozenset(found)


def inventory_drift(app: FastAPI) -> frozenset[tuple[str, str]]:
    """The API9 gate: routes that are served but not declared in `INVENTORY`."""
    return route_inventory(app) - INVENTORY


# ---------------------------------------------------------------- the audit


class Finding(StrEnum):
    EXPLOITED = "exploited"
    BLOCKED = "blocked"
    BROKEN = "broken"  # the benign request failed: a control that blocks everything is a bug


@dataclass(slots=True)
class Probe:
    client: httpx.AsyncClient
    store: Store
    alice: dict[str, str]
    bob: dict[str, str]
    root: dict[str, str]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def probe_api1(p: Probe) -> Finding:
    if (await p.client.get("/v1/orders/1001", headers=p.alice)).status_code != 200:
        return Finding.BROKEN
    stolen = await p.client.get("/v1/orders/1002", headers=p.alice)
    return Finding.EXPLOITED if stolen.status_code == 200 else Finding.BLOCKED


async def probe_api2(p: Probe) -> Finding:
    if (await p.client.get("/v1/users/me", headers=p.alice)).status_code != 200:
        return Finding.BROKEN
    forged = bearer(forge_unsigned_token({"sub": "u-root", "aud": AUDIENCE, "iss": ISSUER}))
    expired = bearer(mint_token("u-alice", now=1_000_000_000))
    responses = [await p.client.get("/v1/users/me", headers=h) for h in (forged, expired)]
    statuses = [r.status_code for r in responses]
    return Finding.EXPLOITED if 200 in statuses else Finding.BLOCKED


async def probe_api3(p: Probe) -> Finding:
    ok = await p.client.patch("/v1/users/me", headers=p.bob, json={"display_name": "Bobby"})
    if ok.status_code != 200 or ok.json()["display_name"] != "Bobby":
        return Finding.BROKEN
    leaked = "password_hash" in ok.json()
    await p.client.patch("/v1/users/me", headers=p.bob, json={"display_name": "B", "role": "admin"})
    escalated = p.store.users["u-bob"].role == "admin"
    return Finding.EXPLOITED if leaked or escalated else Finding.BLOCKED


async def probe_api4(p: Probe) -> Finding:
    if (await p.client.get("/v1/orders?limit=10", headers=p.alice)).status_code != 200:
        return Finding.BROKEN
    huge_page = await p.client.get("/v1/orders?limit=1000000", headers=p.alice)
    big = b'{"display_name": "' + b"x" * (1024 * 1024) + b'"}'
    headers = {**p.alice, "Content-Type": "application/json"}
    big_body = await p.client.patch("/v1/users/me", headers=headers, content=big)
    unbounded = huge_page.status_code == 200 or big_body.status_code != 413
    return Finding.EXPLOITED if unbounded else Finding.BLOCKED


async def probe_api5(p: Probe) -> Finding:
    if (await p.client.get("/v1/admin/users", headers=p.root)).status_code != 200:
        return Finding.BROKEN
    as_user = await p.client.get("/v1/admin/users", headers=p.bob)
    return Finding.EXPLOITED if as_user.status_code == 200 else Finding.BLOCKED


async def probe_api6(p: Probe) -> Finding:
    clock = p.store.clock
    if not isinstance(clock, FakeClock):
        raise TypeError("probe_api6 needs a FakeClock to model human dwell time")
    await p.client.get("/v1/drops/console-x", headers=p.alice)
    clock.now += 5.0  # a human reads the page before buying
    if (await p.client.post("/v1/drops/console-x/purchase", headers=p.alice)).status_code != 200:
        return Finding.BROKEN
    await p.client.get("/v1/drops/console-x", headers=p.bob)
    for _ in range(20):  # a bot: no dwell, no stopping
        await p.client.post("/v1/drops/console-x/purchase", headers=p.bob)
    bought = p.store.purchases[("u-bob", "console-x")]
    return Finding.EXPLOITED if bought > PER_ACCOUNT_LIMIT else Finding.BLOCKED


async def probe_api7(p: Probe) -> Finding:
    url = "https://hooks.partner.example/events"
    benign = await p.client.post("/v1/webhooks/test", headers=p.alice, json={"url": url})
    if benign.status_code != 200:
        return Finding.BROKEN
    imds = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    attack = await p.client.post("/v1/webhooks/test", headers=p.alice, json={"url": imds})
    return Finding.EXPLOITED if FAKE_IMDS_KEY in attack.text else Finding.BLOCKED


async def probe_api8(p: Probe) -> Finding:
    if (await p.client.get("/v1/orders/1001", headers=p.alice)).status_code != 200:
        return Finding.BROKEN
    plain = {**p.alice, "Accept": "text/plain"}
    crash = await p.client.get("/v1/orders/1001/invoice", headers=plain)
    evil = {**p.alice, "Origin": "https://evil.example"}
    cors = await p.client.get("/v1/users/me", headers=evil)
    leaks_trace = "Traceback" in crash.text or "db.internal" in crash.text
    reflects = cors.headers.get("access-control-allow-origin") == "https://evil.example"
    cacheable = "no-store" not in cors.headers.get("cache-control", "")
    return Finding.EXPLOITED if leaks_trace or reflects or cacheable else Finding.BLOCKED


async def probe_api9(p: Probe) -> Finding:
    if (await p.client.get("/v1/users/me", headers=p.alice)).status_code != 200:
        return Finding.BROKEN
    legacy = await p.client.get("/v0/users/u-root")  # no credentials at all
    spec = await p.client.get("/openapi.json")
    exposed = legacy.status_code == 200 or spec.status_code == 200
    return Finding.EXPLOITED if exposed else Finding.BLOCKED


async def probe_api10(p: Probe) -> Finding:
    p.store.partner_mode = "honest"
    quote = await p.client.get("/v1/orders/1001/shipping-quote", headers=p.alice)
    if quote.status_code != 200 or quote.json()["price_cents"] != 1299:
        return Finding.BROKEN
    p.store.partner_mode = "negative"
    negative = await p.client.get("/v1/orders/1001/shipping-quote", headers=p.alice)
    p.store.partner_mode = "redirect"
    await p.client.get("/v1/orders/1001/shipping-quote", headers=p.alice)
    p.store.partner_mode = "honest"
    trusted_bad_data = negative.status_code == 200
    leaked = any(r.url.host == "attacker.example" for r in p.store.outbound)
    return Finding.EXPLOITED if trusted_bad_data or leaked else Finding.BLOCKED


PROBES: Final[Mapping[Risk, Callable[[Probe], Any]]] = {
    Risk.API1: probe_api1,
    Risk.API2: probe_api2,
    Risk.API3: probe_api3,
    Risk.API4: probe_api4,
    Risk.API5: probe_api5,
    Risk.API6: probe_api6,
    Risk.API7: probe_api7,
    Risk.API8: probe_api8,
    Risk.API9: probe_api9,
    Risk.API10: probe_api10,
}


async def audit(controls: Collection[Risk]) -> dict[Risk, Finding]:
    """Run every probe against a fresh store and app built with `controls`."""
    findings: dict[Risk, Finding] = {}
    for risk, probe in PROBES.items():
        store = seed_store()
        transport = httpx.ASGITransport(app=build_app(store, controls), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tokens = {u: bearer(mint_token(u)) for u in ("u-alice", "u-bob", "u-root")}
            p = Probe(client, store, tokens["u-alice"], tokens["u-bob"], tokens["u-root"])
            findings[risk] = await probe(p)
    return findings
