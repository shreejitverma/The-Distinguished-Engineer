"""AIP-163 `validate_only` that runs the live create path inside a SQL SAVEPOINT.

A dry run executes authorization, schema validation, immediate constraints (`flush`),
deferred constraints (`PRAGMA foreign_key_check`), and idempotency bookkeeping exactly as
the live request does.  The only branch on `validate_only` is the last database command:
`ROLLBACK TO SAVEPOINT` for a dry run, `RELEASE SAVEPOINT` for a live request.  Work done
before the savepoint (the audit row) commits in both modes.
"""

# No `from __future__ import annotations`: FastAPI must evaluate the endpoint annotations,
# and `get_session` is local to `create_app`, so a string annotation would not resolve.
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, UniqueConstraint, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CREATE_PERMISSION = "library.books.create"

# How the sqlite3 driver and SQLAlchemy share transaction control:
# - "explicit-begin": driver implicit BEGIN disabled, SQLAlchemy emits BEGIN (the lab default);
# - "autocommit-false": PEP 249 mode, the driver opens a transaction at connect time;
# - "legacy": sqlite3 default, BEGIN only before DML, so a leading SAVEPOINT is a transaction.
TransactionControl = Literal["explicit-begin", "autocommit-false", "legacy"]


class Base(DeclarativeBase):
    pass


class Shelf(Base):
    __tablename__ = "shelves"

    id: Mapped[int] = mapped_column(primary_key=True)
    theme: Mapped[str] = mapped_column(String(64))


class Book(Base):
    __tablename__ = "books"
    __table_args__ = (
        UniqueConstraint("shelf_id", "title", name="uq_books_shelf_title"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Deferred: SQLite checks it only at COMMIT, so a flush inside a savepoint never sees it.
    shelf_id: Mapped[int] = mapped_column(
        ForeignKey("shelves.id", deferrable=True, initially="DEFERRED")
    )
    title: Mapped[str] = mapped_column(String(200))


class AuditEntry(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    principal: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    validate_only: Mapped[bool]


class RequestRecord(Base):
    """AIP-155 deduplication record: the response first returned for a `request_id`."""

    __tablename__ = "request_ids"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_json: Mapped[str]


class CreateBookParams(BaseModel):
    # `extra="forbid"` turns a misspelled `?validateOnly=true` into a 422, not a live write.
    model_config = ConfigDict(extra="forbid")

    validate_only: bool = False
    request_id: str | None = Field(default=None, min_length=1, max_length=64)


class CreateBookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)


class BookOut(BaseModel):
    # Server-generated resource name; AIP-163 says to omit such fields on validation.
    name: str | None
    shelf: str
    title: str


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    permissions: frozenset[str]


TOKENS: dict[str, Principal] = {
    "writer-token": Principal("alice", frozenset({CREATE_PERMISSION})),
    "reader-token": Principal("bob", frozenset()),
}


class DeferredConstraintViolation(Exception):
    """A deferred constraint that the eventual COMMIT would reject."""


def make_engine(
    url: str = "sqlite+aiosqlite:///:memory:",
    *,
    transaction_control: TransactionControl = "explicit-begin",
) -> AsyncEngine:
    connect_args: dict[str, object] = {}
    if transaction_control == "autocommit-false":
        connect_args["autocommit"] = False
    engine = _create_async_engine(url, connect_args=connect_args)

    def on_connect(dbapi_connection: Any, _record: object) -> None:
        if transaction_control == "explicit-begin":
            dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        # A no-op inside an open transaction, which "autocommit-false" always has here.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def on_begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN")

    event.listen(engine.sync_engine, "connect", on_connect)
    if transaction_control == "explicit-begin":
        event.listen(engine.sync_engine, "begin", on_begin)
    return engine


async def authenticate(authorization: Annotated[str | None, Header()] = None) -> Principal:
    token = (authorization or "").removeprefix("Bearer ")
    principal = TOKENS.get(token)
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid credentials", {"WWW-Authenticate": "Bearer"}
        )
    return principal


async def assert_deferred_constraints(session: AsyncSession) -> None:
    violations = (await session.execute(text("PRAGMA foreign_key_check"))).all()
    if violations:
        raise DeferredConstraintViolation(f"{len(violations)} foreign key violation(s)")


def to_http_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if isinstance(exc, DeferredConstraintViolation) or "FOREIGN KEY" in message:
        return HTTPException(status.HTTP_404_NOT_FOUND, "parent shelf does not exist")
    if "UNIQUE" in message:
        return HTTPException(status.HTTP_409_CONFLICT, "a book with this title is on the shelf")
    raise exc


def create_app(engine: AsyncEngine, *, check_deferred_constraints: bool = True) -> FastAPI:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    @app.post("/v1/shelves/{shelf_id}/books", status_code=status.HTTP_201_CREATED)
    async def create_book(
        shelf_id: Annotated[int, Path(ge=1)],
        body: CreateBookBody,
        params: Annotated[CreateBookParams, Query()],
        principal: Annotated[Principal, Depends(authenticate)],
        session: Annotated[AsyncSession, Depends(get_session)],
        response: Response,
    ) -> dict[str, Any]:
        # 1. Authorization runs before any branch on validate_only (AIP-163: "must").
        if CREATE_PERMISSION not in principal.permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing {CREATE_PERMISSION}")
        # 2. Outer-transaction work that must survive a dry run.
        session.add(
            AuditEntry(
                principal=principal.subject,
                action=CREATE_PERMISSION,
                validate_only=params.validate_only,
            )
        )
        if params.request_id is not None:
            prior = await session.get(RequestRecord, params.request_id)
            if prior is not None:
                await session.commit()
                replay: dict[str, Any] = json.loads(prior.response_json)
                return replay
        # 3. The live code path, inside a SAVEPOINT (begin_nested flushes the audit row first).
        savepoint = await session.begin_nested()
        try:
            book = Book(shelf_id=shelf_id, title=body.title)
            session.add(book)
            await session.flush()  # immediate constraints: UNIQUE, NOT NULL, CHECK
            if check_deferred_constraints:
                await assert_deferred_constraints(session)  # what COMMIT would check
            name = None if params.validate_only else f"shelves/{shelf_id}/books/{book.id}"
            payload = BookOut(name=name, shelf=f"shelves/{shelf_id}", title=book.title)
            out = payload.model_dump(exclude_none=True)
            if params.request_id is not None:
                record = RequestRecord(request_id=params.request_id, response_json=json.dumps(out))
                session.add(record)
                await session.flush()
        except (IntegrityError, DeferredConstraintViolation) as exc:
            await savepoint.rollback()
            await session.commit()  # the audit row records the failed attempt
            raise to_http_error(exc) from exc
        # 4. The only branch on validate_only.
        if params.validate_only:
            await savepoint.rollback()  # ROLLBACK TO SAVEPOINT
        else:
            await savepoint.commit()  # RELEASE SAVEPOINT
            response.headers["Location"] = f"/v1/{name}"
        try:
            await session.commit()
        except IntegrityError as exc:  # reachable only when the deferred check is skipped
            await session.rollback()
            raise to_http_error(exc) from exc
        return out

    return app
