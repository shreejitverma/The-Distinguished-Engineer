"""BOPLA / mass assignment (OWASP API3:2023): an auto-binding API and its strict-schema fix.

The vulnerable app binds request keys straight onto a SQLAlchemy row, filters on any column the
client names, and returns every column. The secure app accepts only per-role write models with
``extra="forbid"``, returns only per-audience read models, and refuses unknown query parameters.
The caller identity comes from an ``X-User-Id`` header: authentication is out of scope here.
"""

from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

type Role = Literal["user", "support", "admin"]


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    """Persistence model: it holds properties no client may read or write."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]
    display_name: Mapped[str]
    bio: Mapped[str | None]
    role: Mapped[str] = mapped_column(default="user")
    balance_cents: Mapped[int] = mapped_column(default=0)
    password_hash: Mapped[str]


PLACEHOLDER_HASH = "argon2id-EXAMPLE-NOT-A-REAL-HASH"

SEED_USERS: tuple[dict[str, Any], ...] = (
    {"id": 1, "email": "root@example.test", "display_name": "root", "role": "admin"},
    {"id": 2, "email": "alice@example.test", "display_name": "alice", "balance_cents": 500},
    {"id": 3, "email": "mallory@example.test", "display_name": "mallory", "balance_cents": 0},
)


def make_engine() -> Engine:
    """One shared in-memory SQLite database, safe to use from FastAPI's threadpool."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for seed in SEED_USERS:
            session.add(UserRow(bio=None, password_hash=PLACEHOLDER_HASH, **seed))
        session.commit()
    return engine


def caller_id(x_user_id: Annotated[int, Header()]) -> int:
    return x_user_id


CallerId = Annotated[int, Depends(caller_id)]


def get_session(request: Request) -> Iterator[Session]:
    engine: Engine = request.app.state.engine
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def row_to_dict(row: UserRow) -> dict[str, Any]:
    """The generic ``to_json()`` that API3:2023 warns about: every column, for every caller."""
    return {column.key: getattr(row, column.key) for column in UserRow.__table__.columns}


def create_vulnerable_app(engine: Engine) -> FastAPI:
    app = FastAPI()
    app.state.engine = engine

    @app.patch("/users/me")
    def update_me(
        payload: Annotated[dict[str, Any], Body()],
        caller: CallerId,
        session: SessionDep,
    ) -> dict[str, Any]:
        row = session.get_one(UserRow, caller)
        for key, value in payload.items():
            if hasattr(UserRow, key):  # the same guard as SQLAlchemy's default __init__
                setattr(row, key, value)
        session.commit()
        return row_to_dict(row)

    @app.get("/users/{user_id}")
    def read_user(user_id: int, _caller: CallerId, session: SessionDep) -> dict[str, Any]:
        return row_to_dict(session.get_one(UserRow, user_id))

    @app.get("/users")
    def list_users(request: Request, _caller: CallerId, session: SessionDep) -> list[int]:
        rows = session.scalars(select(UserRow).filter_by(**dict(request.query_params)))
        return [row.id for row in rows]

    return app


class WriteModel(BaseModel):
    """Base for request bodies: unknown keys are a 422, never silently dropped or bound."""

    model_config = ConfigDict(extra="forbid", strict=True)


class UserSelfUpdate(WriteModel):
    display_name: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    bio: Annotated[str, Field(max_length=280)] | None = None

    @field_validator("display_name")
    @classmethod
    def display_name_not_null(cls, value: str | None) -> str | None:
        # Defaults are not validated, so this fires only for an explicit JSON null.
        if value is None:
            raise ValueError("display_name cannot be null")
        return value


class AdminUserUpdate(UserSelfUpdate):
    role: Role | None = None

    @field_validator("role")
    @classmethod
    def role_not_null(cls, value: Role | None) -> Role | None:
        if value is None:
            raise ValueError("role cannot be null")
        return value


class ReadModel(BaseModel):
    """Base for responses: a dict with an undeclared key fails loudly instead of leaking."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class UserPublicView(ReadModel):
    id: int
    display_name: str
    bio: str | None


class UserSelfView(UserPublicView):
    email: str
    role: str
    balance_cents: int


class UserListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(ge=1, le=100)] = 10


def apply_patch(row: UserRow, patch: UserSelfUpdate) -> None:
    """Copy only the keys the client sent; the key set is bounded by the write model."""
    for key, value in patch.model_dump(exclude_unset=True).items():
        setattr(row, key, value)


def create_secure_app(engine: Engine) -> FastAPI:
    app = FastAPI()
    app.state.engine = engine

    @app.patch("/users/me", response_model=UserSelfView)
    def update_me(patch: UserSelfUpdate, caller: CallerId, session: SessionDep) -> UserRow:
        row = session.get_one(UserRow, caller)
        apply_patch(row, patch)
        session.commit()
        return row

    @app.get("/users/{user_id}", response_model=UserPublicView)
    def read_user(user_id: int, _caller: CallerId, session: SessionDep) -> UserRow:
        return session.get_one(UserRow, user_id)

    @app.get("/users")
    def list_users(
        query: Annotated[UserListQuery, Query()], _caller: CallerId, session: SessionDep
    ) -> list[int]:
        return list(session.scalars(select(UserRow.id).order_by(UserRow.id).limit(query.limit)))

    @app.patch("/admin/users/{user_id}", response_model=UserSelfView)
    def admin_update(
        user_id: int, patch: AdminUserUpdate, caller: CallerId, session: SessionDep
    ) -> UserRow:
        if session.get_one(UserRow, caller).role != "admin":
            raise HTTPException(status_code=403, detail="admin role required")
        row = session.get_one(UserRow, user_id)
        apply_patch(row, patch)
        session.commit()
        return row

    @app.get("/debug/leaky-dict", response_model=UserPublicView)
    def leaky_dict(_caller: CallerId, session: SessionDep) -> dict[str, Any]:
        # A handler that builds a dict by hand and forgets to drop a column.
        return row_to_dict(session.get_one(UserRow, 2))

    return app
