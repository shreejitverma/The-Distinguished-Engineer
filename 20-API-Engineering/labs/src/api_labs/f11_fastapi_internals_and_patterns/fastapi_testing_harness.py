"""FastAPI testing harness: Lifespan, dependency overrides, and transactional rollback."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

EXPECTED_AUTH_CREDENTIAL = "valid-test-credential"


class Base(DeclarativeBase):
    """Declarative base class for SQLAlchemy models."""


class LedgerAccount(Base):
    """Transactional ledger account table for testing state isolation and rollback."""

    __tablename__ = "ledger_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    holder_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    balance: Mapped[int] = mapped_column(nullable=False, default=0)


@dataclass
class AppLifecycleState:
    """Explicit state tracker for lifespan startup and shutdown verification."""

    startup_called: bool = False
    shutdown_called: bool = False
    active_connections: int = 0


def create_test_engine(
    url: str = "sqlite+aiosqlite:///:memory:",
) -> AsyncEngine:
    """Create an asynchronous SQLite test engine with sqlite3 autocommit=False (Python 3.12+).

    With autocommit=False the sqlite3 driver keeps a transaction open at all times
    (BEGIN DEFERRED on connect and after every commit or rollback), so SAVEPOINTs nest
    inside it and the outer rollback also discards released SAVEPOINTs.
    StaticPool ensures all async connections within the process share the same in-memory DB.
    """
    return create_async_engine(
        url,
        connect_args={"autocommit": False, "check_same_thread": False},
        poolclass=StaticPool,
    )


async def init_db(engine: AsyncEngine) -> None:
    """Create all declarative tables on the test engine."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Production database session dependency; tests replace it via dependency_overrides."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_current_user(request: Request) -> str:
    """Default authentication dependency requiring a valid Bearer token."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    bearer_credential = auth_header.removeprefix("Bearer ").strip()
    if bearer_credential != EXPECTED_AUTH_CREDENTIAL:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    return "prod_user"


def create_app(
    engine: AsyncEngine,
    lifecycle_state: AppLifecycleState | None = None,
) -> FastAPI:
    """Application factory configuring lifespan state and route endpoints."""
    tracker = lifecycle_state or AppLifecycleState()

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncGenerator[dict[str, Any], None]:
        tracker.startup_called = True
        tracker.active_connections = 1
        app.state.ready = True
        app.state.tracker = tracker
        try:
            yield {"ready": True, "tracker": tracker}
        finally:
            tracker.shutdown_called = True
            tracker.active_connections = 0
            app.state.ready = False

    app = FastAPI(title="FastAPI-Testing-Harness", lifespan=app_lifespan)

    # Production wiring goes through app.state, never dependency_overrides: any non-empty
    # overrides dict makes solve_dependencies() rebuild every sub-dependant per request.
    app.state.session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    @app.get("/health")
    async def health_check(request: Request) -> dict[str, Any]:
        if not getattr(request.app.state, "ready", False):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Application lifespan uninitialized",
            )
        return {
            "status": "healthy",
            "ready": request.app.state.ready,
            "startup_called": request.app.state.tracker.startup_called,
        }

    @app.get("/me")
    async def get_me(user: Annotated[str, Depends(get_current_user)]) -> dict[str, str]:
        return {"user": user}

    @app.get("/me-cached")
    async def get_me_cached(
        user1: Annotated[str, Depends(get_current_user, use_cache=True)],
        user2: Annotated[str, Depends(get_current_user, use_cache=True)],
    ) -> dict[str, Any]:
        return {"user1": user1, "user2": user2, "identical": user1 is user2}

    @app.post("/accounts", status_code=status.HTTP_201_CREATED)
    async def create_account(
        holder_name: str,
        balance: int,
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> dict[str, Any]:
        account = LedgerAccount(holder_name=holder_name, balance=balance)
        session.add(account)
        await session.commit()
        await session.refresh(account)
        return {
            "id": account.id,
            "holder_name": account.holder_name,
            "balance": account.balance,
        }

    @app.get("/accounts")
    async def list_accounts(
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> list[dict[str, Any]]:
        query = select(LedgerAccount).order_by(LedgerAccount.id)
        result = await session.execute(query)
        accounts = result.scalars().all()
        return [
            {"id": acc.id, "holder_name": acc.holder_name, "balance": acc.balance}
            for acc in accounts
        ]

    @app.post("/transfer")
    async def transfer_balance(
        source_id: int,
        target_id: int,
        amount: int,
        session: Annotated[AsyncSession, Depends(get_db)],
    ) -> dict[str, Any]:
        if amount <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Transfer amount must be strictly positive",
            )
        src_res = await session.execute(select(LedgerAccount).where(LedgerAccount.id == source_id))
        source = src_res.scalar_one_or_none()
        dst_res = await session.execute(select(LedgerAccount).where(LedgerAccount.id == target_id))
        target = dst_res.scalar_one_or_none()
        if source is None or target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or both accounts not found",
            )
        if source.balance < amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insufficient funds",
            )
        source.balance -= amount
        target.balance += amount
        await session.commit()
        return {
            "source_balance": source.balance,
            "target_balance": target.balance,
            "transferred": amount,
        }

    return app


def create_transactional_session(connection: AsyncConnection) -> AsyncSession:
    """Create an AsyncSession bound to an external transaction using SAVEPOINTs.

    join_transaction_mode='create_savepoint' forces the session to execute begin_nested()
    for all transactional operations. Calling commit() inside route handlers releases only
    the savepoint without committing the enclosing outer transaction.
    """
    return AsyncSession(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
