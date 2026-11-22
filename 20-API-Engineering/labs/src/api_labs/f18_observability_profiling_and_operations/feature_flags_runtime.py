"""Local feature-flag evaluation: HMAC bucketing, kill switches, and stale-cache fallback.

Evaluation is a pure function of one immutable `View` (snapshot + kill set + freshness), never
raises, and never does I/O.  Delivery (polling, streaming) is out of scope: callers hand the store
a JSON document through `FlagStore.load` and confirm an unchanged source with `FlagStore.touch`.
"""

import hmac
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, NamedTuple, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

BUCKETS = 10_000
MAX_DOC_BYTES = 1_048_576
MAX_FLAGS = 10_000

FlagValue = bool | int | float | str
Freshness = Literal["fresh", "stale", "expired", "empty"]
ErrorCode = Literal[
    "PROVIDER_NOT_READY", "FLAG_NOT_FOUND", "TYPE_MISMATCH", "TARGETING_KEY_MISSING", "GENERAL"
]
Clock = Callable[[], float]


def hash64(salt: str, subject: str) -> int:
    """First 8 bytes of HMAC-SHA256(key=salt, msg=subject) as a big-endian unsigned integer."""
    digest = hmac.digest(salt.encode(), subject.encode(), "sha256")
    return int.from_bytes(digest[:8], "big")


def bucket_of(salt: str, subject: str) -> int:
    """Stable bucket in [0, BUCKETS): 2**64 % 10000 == 1616, so the modulo bias is ~5e-16."""
    return hash64(salt, subject) % BUCKETS


class Split(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    variant: str
    weight: int = Field(ge=0, le=BUCKETS)


class Flag(BaseModel):
    """One flag. Weights are basis points that must sum to exactly BUCKETS (10000)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    salt: str = Field(min_length=1, max_length=128)
    variants: dict[str, FlagValue] = Field(min_length=1)
    off_variant: str
    default_variant: str
    enabled: bool = True
    bucket_by: str | None = None
    allow: dict[str, str] = Field(default_factory=dict)
    splits: tuple[Split, ...] = ()

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        referenced = {self.off_variant, self.default_variant, *self.allow.values()}
        referenced.update(split.variant for split in self.splits)
        unknown = referenced - set(self.variants)
        if unknown:
            raise ValueError(f"unknown variants {sorted(unknown)}")
        if self.splits and sum(split.weight for split in self.splits) != BUCKETS:
            raise ValueError(f"split weights must sum to {BUCKETS}")
        return self


class Document(BaseModel):
    """The wire document: treated as untrusted input even when we generated it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    flags: tuple[Flag, ...] = Field(max_length=MAX_FLAGS)


@dataclass(frozen=True, slots=True)
class Context:
    targeting_key: str | None = None
    attributes: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Resolution[T]:
    value: T
    variant: str | None
    reason: str
    error_code: ErrorCode | None = None
    version: int | None = None
    freshness: Freshness = "fresh"


@dataclass(frozen=True, slots=True)
class Snapshot:
    version: int
    flags: Mapping[str, Flag]
    loaded_at: float


@dataclass(frozen=True, slots=True)
class View:
    """An immutable, pinned read of the store: every `resolve` on it sees the same version."""

    snapshot: Snapshot | None
    kills: frozenset[str]
    freshness: Freshness

    @property
    def version(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.version

    def resolve[T](self, key: str, ctx: Context, default: T) -> Resolution[T]:
        """Resolve `key`; `default` is the compiled-in safe value and is returned on any error."""
        if self.snapshot is None:
            return self._fail(default, "PROVIDER_NOT_READY")
        if self.freshness == "expired":
            return self._fail(default, "GENERAL")
        flag = self.snapshot.flags.get(key)
        if flag is None:
            return self._fail(default, "FLAG_NOT_FOUND")
        variant, reason = self._select(flag, ctx)
        if variant is None:
            return self._fail(default, "TARGETING_KEY_MISSING")
        value = flag.variants[variant]
        if type(value) is not type(default):  # `True` is an `int` to isinstance, not here
            return self._fail(default, "TYPE_MISMATCH")
        return Resolution(cast("T", value), variant, reason, None, self.version, self.freshness)

    def _select(self, flag: Flag, ctx: Context) -> tuple[str | None, str]:
        if flag.key in self.kills:
            return flag.off_variant, "KILL_SWITCH"
        if not flag.enabled:
            return flag.off_variant, "DISABLED"
        by = flag.bucket_by
        subject = ctx.targeting_key if by is None else ctx.attributes.get(by)
        if subject is not None and (allowed := flag.allow.get(subject)) is not None:
            return allowed, "TARGETING_MATCH"
        if not flag.splits:
            return flag.default_variant, "STATIC"
        if subject is None:
            return None, "ERROR"
        bucket = bucket_of(flag.salt, subject)
        cumulative = 0
        for split in flag.splits:
            cumulative += split.weight
            if bucket < cumulative:
                return split.variant, "SPLIT"
        return flag.splits[-1].variant, "SPLIT"  # unreachable: weights sum to BUCKETS

    def _fail[T](self, default: T, code: ErrorCode) -> Resolution[T]:
        return Resolution(default, None, "ERROR", code, self.version, self.freshness)


@dataclass(frozen=True, slots=True)
class Policy:
    ttl_s: float = 30.0
    max_stale_s: float = 3600.0

    def __post_init__(self) -> None:
        if not 0 < self.ttl_s <= self.max_stale_s:
            raise ValueError("need 0 < ttl_s <= max_stale_s")


class LoadResult(NamedTuple):
    accepted: bool
    detail: str


@dataclass(frozen=True, slots=True)
class _State:
    snapshot: Snapshot | None
    kills: frozenset[str]


class FlagStore:
    """Copy-on-write holder: readers take one reference, writers serialize on a lock."""

    def __init__(self, policy: Policy = Policy(), clock: Clock = time.monotonic) -> None:  # noqa: B008
        self._policy = policy
        self._clock = clock
        self._state = _State(None, frozenset())
        self._lock = threading.Lock()

    def view(self) -> View:
        state = self._state  # one attribute read: snapshot and kills cannot be torn
        if state.snapshot is None:
            return View(None, state.kills, "empty")
        age = self._clock() - state.snapshot.loaded_at
        if age <= self._policy.ttl_s:
            freshness: Freshness = "fresh"
        elif age <= self._policy.max_stale_s:
            freshness = "stale"
        else:
            freshness = "expired"
        return View(state.snapshot, state.kills, freshness)

    def evaluate[T](self, key: str, ctx: Context, default: T) -> Resolution[T]:
        return self.view().resolve(key, ctx, default)

    def load(self, raw: bytes | str) -> LoadResult:
        """Validate and publish a document; on any problem keep serving the last good one."""
        size = len(raw) if isinstance(raw, bytes) else len(raw.encode())
        if size > MAX_DOC_BYTES:
            return LoadResult(False, "document too large")
        try:
            doc = Document.model_validate_json(raw)
        except ValidationError as exc:
            return LoadResult(False, f"invalid document: {exc.error_count()} error(s)")
        flags = {flag.key: flag for flag in doc.flags}
        if len(flags) != len(doc.flags):
            return LoadResult(False, "duplicate flag key")
        with self._lock:
            current = self._state.snapshot
            if current is not None:
                if doc.version <= current.version:
                    return LoadResult(False, "version not newer")
                if len(flags) * 2 < len(current.flags):
                    return LoadResult(False, "shrink guard: fewer than half the flags")
            snapshot = Snapshot(doc.version, MappingProxyType(flags), self._clock())
            self._state = _State(snapshot, self._state.kills)
        return LoadResult(True, f"version {doc.version}")

    def touch(self) -> None:
        """The source confirmed our version is still current (for example HTTP 304)."""
        with self._lock:
            snapshot = self._state.snapshot
            if snapshot is not None:
                refreshed = replace(snapshot, loaded_at=self._clock())
                self._state = _State(refreshed, self._state.kills)

    def kill(self, key: str) -> None:
        with self._lock:
            self._state = _State(self._state.snapshot, self._state.kills | {key})

    def revive(self, key: str) -> None:
        with self._lock:
            self._state = _State(self._state.snapshot, self._state.kills - {key})


def _context(request: Request) -> Context:
    tenant = request.headers.get("x-tenant-id")
    attributes = {} if tenant is None else {"tenant_id": tenant}
    return Context(request.headers.get("x-user-id"), attributes)


def _authorized(request: Request, credential: str) -> bool:
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    ok = hmac.compare_digest(supplied.encode(), credential.encode())
    return scheme.lower() == "bearer" and ok


def create_app(
    store: FlagStore,
    *,
    kill_credential: str,
    between_reads: Callable[[], Awaitable[None]] | None = None,
) -> Starlette:
    """ASGI app: a pinned and an unpinned read path, a guarded kill route, a 503 shed route."""

    async def checkout(request: Request) -> Response:
        ctx, view = _context(request), store.view()  # pinned: one view for the whole request
        flow = view.resolve("checkout-flow", ctx, "legacy")
        if between_reads is not None:
            await between_reads()
        pay = view.resolve("payments-v2", ctx, False)
        return JSONResponse({"flow": [flow.value, flow.version], "pay": [pay.value, pay.version]})

    async def checkout_unpinned(request: Request) -> Response:
        ctx = _context(request)
        flow = store.evaluate("checkout-flow", ctx, "legacy")  # new view per call: can tear
        if between_reads is not None:
            await between_reads()
        pay = store.evaluate("payments-v2", ctx, False)
        return JSONResponse({"flow": [flow.value, flow.version], "pay": [pay.value, pay.version]})

    async def export(request: Request) -> Response:
        enabled = store.evaluate("export-enabled", _context(request), False)
        if not enabled.value:
            return JSONResponse(
                {"error": "disabled", "reason": enabled.reason},
                status_code=503,
                headers={"Retry-After": "60"},
            )
        return JSONResponse({"export": "started"})

    async def kill(request: Request) -> Response:
        if not _authorized(request, kill_credential):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        store.kill(request.path_params["key"])
        return Response(status_code=204)

    return Starlette(
        routes=[
            Route("/checkout", checkout),
            Route("/checkout-unpinned", checkout_unpinned),
            Route("/export", export),
            Route("/admin/kill/{key}", kill, methods=["POST"]),
        ]
    )
