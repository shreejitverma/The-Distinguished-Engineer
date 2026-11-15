"""Parameterized class-based dependencies for FastAPI.

Provides reusable callable classes (__call__) for role-based access guards,
sliding window rate limiters, pagination parsers, and scoped audit yield dependencies.
"""

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from starlette import status


@dataclass(frozen=True)
class User:
    """Authenticated user context containing identity and granted permissions."""

    username: str
    permissions: frozenset[str]
    is_superuser: bool = False


async def get_current_user(request: Request) -> User:
    """Extract authenticated user context from request headers."""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Token format: Bearer <username>|<perm1,perm2,...>[|<superuser>]
    token = auth_header.removeprefix("Bearer ").strip()
    parts = token.split("|")
    if len(parts) < 2:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed bearer token format",
            headers={"WWW-Authenticate": "Bearer"},
        )
    username = parts[0]
    roles = frozenset(r.strip() for r in parts[1].split(",") if r.strip())
    is_super = len(parts) > 2 and parts[2].lower() in {"superuser", "true", "1"}
    return User(username=username, permissions=roles, is_superuser=is_super)


@dataclass(frozen=True)
class RequirePermission:
    """Parameterized authorization guard checking required permissions on the caller.

    Because this class is a frozen dataclass, it is immutable, hashable, and supports
    value equality. Two distinct instances configured with the same permission compare
    equal and share the same hash, enabling clean value-based mocking in
    FastAPI's app.dependency_overrides dictionary without requiring global singleton leaks.
    """

    permission: str
    allow_superuser: bool = True

    def __call__(
        self,
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        """Evaluate permission against current authenticated user."""
        if self.allow_superuser and user.is_superuser:
            return user
        if self.permission not in user.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: missing required permission '{self.permission}'",
            )
        return user


class SlidingWindowRateLimiter:
    """In-memory sliding window rate limiter implemented as a callable class.

    Maintains per-client timestamp logs protected by an asyncio.Lock.
    Emits IETF draft RateLimit and RFC 9110 Retry-After response headers.
    """

    def __init__(self, times: int, seconds: float) -> None:
        self.times = times
        self.seconds = seconds
        self._history: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, request: Request, response: Response) -> None:
        """Evaluate sliding window rate limit for the requesting client host."""
        client_host = request.client.host if request.client else "127.0.0.1"
        now = time.monotonic()
        cutoff = now - self.seconds

        async with self._lock:
            timestamps = self._history.setdefault(client_host, deque())
            # Evict timestamps outside the active sliding window
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            current_count = len(timestamps)
            if current_count >= self.times:
                oldest = timestamps[0]
                retry_after = max(1, int(oldest + self.seconds - now))
                response.headers["RateLimit-Limit"] = str(self.times)
                response.headers["RateLimit-Remaining"] = "0"
                response.headers["RateLimit-Reset"] = str(retry_after)
                response.headers["Retry-After"] = str(retry_after)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too Many Requests",
                    headers={
                        "RateLimit-Limit": str(self.times),
                        "RateLimit-Remaining": "0",
                        "RateLimit-Reset": str(retry_after),
                        "Retry-After": str(retry_after),
                    },
                )

            timestamps.append(now)
            remaining = self.times - len(timestamps)
            reset_seconds = int(self.seconds)
            response.headers["RateLimit-Limit"] = str(self.times)
            response.headers["RateLimit-Remaining"] = str(remaining)
            response.headers["RateLimit-Reset"] = str(reset_seconds)


@dataclass(frozen=True)
class PaginationParams:
    """Validated pagination and ordering parameters returned to endpoint handlers."""

    offset: int
    limit: int
    order_by: str
    descending: bool


@dataclass(frozen=True)
class PaginationParser:
    """Parameterized pagination parser with configurable limit caps and sort allowlists.

    The __call__ method declares query parameters with validation metadata.
    FastAPI's signature inspection automatically registers these parameters in the
    route Dependant and the generated OpenAPI specification.
    """

    default_limit: int = 20
    max_limit: int = 100
    allowed_order_by: tuple[str, ...] = ("id", "created_at")

    def __call__(
        self,
        offset: Annotated[int, Query(ge=0, description="Number of items to skip")] = 0,
        limit: Annotated[
            int | None,
            Query(ge=1, description="Page size limit"),
        ] = None,
        order_by: Annotated[
            str,
            Query(description="Sort field name"),
        ] = "id",
        descending: Annotated[
            bool,
            Query(description="Sort in descending order if true"),
        ] = False,
    ) -> PaginationParams:
        """Parse, clamp, and validate pagination query parameters."""
        resolved_limit = limit if limit is not None else self.default_limit
        if resolved_limit > self.max_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Requested limit {resolved_limit} exceeds maximum "
                    f"allowed limit of {self.max_limit}"
                ),
            )
        if order_by not in self.allowed_order_by:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid order_by field '{order_by}'. "
                    f"Allowed sort fields: {list(self.allowed_order_by)}"
                ),
            )
        return PaginationParams(
            offset=offset,
            limit=resolved_limit,
            order_by=order_by,
            descending=descending,
        )


@dataclass
class AuditLifecycleTracker:
    """Deterministic event recorder tracking lifecycle of yield dependencies."""

    events: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class AuditScopeDependency:
    """Parameterized callable class implementing a yield dependency for scoped auditing.

    Configures the audit action name at construction time. Accesses runtime state
    via the request rather than storing mutable state on self. This preserves purity,
    guarantees safe concurrency across concurrent requests, and avoids Python's
    typing.Annotated metadata caching trap.
    """

    action_name: str

    async def __call__(self, request: Request) -> AsyncIterator[str]:
        """Record enter event before endpoint runs and exit event during teardown."""
        tracker: AuditLifecycleTracker | None = getattr(
            request.app.state,
            "audit_tracker",
            None,
        )
        if tracker is not None:
            tracker.events.append(f"enter:{self.action_name}")
        try:
            yield self.action_name
        finally:
            if tracker is not None:
                tracker.events.append(f"exit:{self.action_name}")


def build_app(
    *,
    rate_limit_requests: int = 2,
    rate_limit_window: float = 60.0,
    audit_tracker: AuditLifecycleTracker | None = None,
) -> FastAPI:
    """Construct a FastAPI test application registering parameterized dependencies."""
    app = FastAPI(title="Parameterized Dependencies Lab", version="1.0.0")

    tracker = audit_tracker if audit_tracker is not None else AuditLifecycleTracker()
    app.state.audit_tracker = tracker
    rate_limiter = SlidingWindowRateLimiter(times=rate_limit_requests, seconds=rate_limit_window)
    pagination = PaginationParser(
        default_limit=10,
        max_limit=50,
        allowed_order_by=("id", "created_at", "amount"),
    )
    audit_dep = AuditScopeDependency(action_name="ledger_mutation")

    @app.get("/secure/ledger")
    def get_ledger(
        current_user: Annotated[User, Depends(RequirePermission("ledger:post"))],
    ) -> dict[str, str]:
        return {
            "status": "success",
            "message": f"Ledger posted by {current_user.username}",
        }

    @app.get("/rate-limited")
    async def get_rate_limited(
        _: Annotated[None, Depends(rate_limiter)],
    ) -> dict[str, str]:
        return {"status": "ok", "resource": "protected_data"}

    @app.get("/items")
    def list_items(
        pagination_data: Annotated[PaginationParams, Depends(pagination)],
    ) -> dict[str, object]:
        return {
            "offset": pagination_data.offset,
            "limit": pagination_data.limit,
            "order_by": pagination_data.order_by,
            "descending": pagination_data.descending,
            "items": [],
        }

    @app.post("/audited-action")
    async def perform_audited_action(
        action: Annotated[str, Depends(audit_dep)],
    ) -> dict[str, str]:
        tracker.events.append(f"endpoint_executed:{action}")
        return {"action": action, "result": "completed"}

    return app
