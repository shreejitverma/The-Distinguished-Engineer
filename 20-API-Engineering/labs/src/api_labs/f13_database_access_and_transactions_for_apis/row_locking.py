"""Optimistic version columns vs pessimistic FOR UPDATE row locks behind an HTTP API.

Optimistic: SQLAlchemy `version_id_col` adds `AND version = <loaded>` to every flushed
UPDATE and DELETE and raises `StaleDataError` when the matched rowcount is wrong; the API
maps that to `412` (the client's `If-Match` lost the race) or retries and then answers `409`.
Pessimistic: `Select.with_for_update()` renders a dialect-specific locking clause, which
SQLite and SQL Server compile to nothing, so a SKIP LOCKED claim that is correct on
PostgreSQL double-claims on SQLite; a guarded `UPDATE ... WHERE status = 'open'` does not.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Final

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import ClauseElement, Dialect, Select, String, make_url, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from api_labs.f03_http_caching_and_conditional_requests.if_match_preconditions import (
    MalformedPreconditionError,
    etag_for,
    expected_versions,
    parse_if_match,
)

MAX_RESERVE_ATTEMPTS: Final = 3


class Base(DeclarativeBase):
    pass


class Item(Base):
    """A versioned row: every ORM UPDATE or DELETE is guarded by `version`."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[int]
    version: Mapped[int] = mapped_column(nullable=False)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Ticket(Base):
    """An unversioned work-queue row claimed by workers."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default="open")
    claimed_by: Mapped[str | None] = mapped_column(String(20), default=None)


# --- Pessimistic: the locking clause each dialect actually emits -------------------------


def claimable(batch: int, *, skip_locked: bool = True, nowait: bool = False) -> Select[Ticket]:
    """The queue-claim SELECT: oldest open tickets, locked, skipping rows others hold."""
    return (
        select(Ticket)
        .where(Ticket.status == "open")
        .order_by(Ticket.id)
        .limit(batch)
        .with_for_update(skip_locked=skip_locked, nowait=nowait)
    )


def dialect_for(backend: str) -> Dialect:
    """The default dialect for a URL scheme, built offline (the DBAPI is never imported)."""
    return make_url(f"{backend}://").get_dialect()()


def render(statement: ClauseElement, dialect: Dialect) -> str:
    """Compile a statement for a dialect without connecting, as one line of SQL."""
    return " ".join(str(statement.compile(dialect=dialect)).split())


async def select_claimable(session: AsyncSession, batch: int) -> list[Ticket]:
    """Step 1 of a SKIP LOCKED claim: on PostgreSQL the rows stay locked until commit."""
    return list((await session.scalars(claimable(batch))).all())


async def mark_claimed(session: AsyncSession, tickets: Sequence[Ticket], worker: str) -> None:
    """Step 2 of a SKIP LOCKED claim: an unguarded write, safe only if step 1 locked."""
    for ticket in tickets:
        ticket.status = "claimed"
        ticket.claimed_by = worker
    await session.flush()


async def guarded_claim(
    session: AsyncSession, candidate_ids: Sequence[int], worker: str
) -> list[int]:
    """Portable compare-and-set claim: only rows still `open` at write time are taken."""
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id.in_(candidate_ids), Ticket.status == "open")
        .values(status="claimed", claimed_by=worker)
        .returning(Ticket.id)
    )
    return sorted(result.scalars().all())


# --- Optimistic: version_id_col mapped onto HTTP preconditions ---------------------------

type RaceHook = Callable[[int, int], Awaitable[None]]


async def _no_race(item_id: int, attempt: int) -> None:
    return None


class ItemPatch(BaseModel):
    quantity: int = Field(ge=0)


class Reservation(BaseModel):
    amount: int = Field(ge=1)


def _body(item: Item) -> dict[str, int | str]:
    return {"id": item.id, "name": item.name, "quantity": item.quantity, "version": item.version}


def build_app(sessions: async_sessionmaker[AsyncSession], race: RaceHook = _no_race) -> FastAPI:
    """`race(item_id, attempt)` runs after the read and before the commit; tests use it to
    commit a competing write at exactly the point a real concurrent request would."""
    app = FastAPI()

    async def load(session: AsyncSession, item_id: int) -> Item:
        item = await session.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "no such item")
        return item

    @app.get("/items/{item_id}")
    async def read_item(item_id: int, response: Response) -> dict[str, int | str]:
        async with sessions() as session:
            item = await load(session, item_id)
            response.headers["ETag"] = str(etag_for(item.version))
            return _body(item)

    @app.patch("/items/{item_id}")
    async def patch_item(
        item_id: int,
        patch: ItemPatch,
        response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> dict[str, int | str]:
        if if_match is None:
            raise HTTPException(428, "send If-Match with the ETag you read")
        try:
            allowed = expected_versions(parse_if_match([if_match]))
        except MalformedPreconditionError as exc:
            raise HTTPException(400, str(exc)) from exc
        async with sessions() as session:
            item = await load(session, item_id)
            if allowed is not None and item.version not in allowed:
                raise HTTPException(412, "If-Match does not match the current ETag")
            item.quantity = patch.quantity
            await race(item_id, 1)
            try:
                # The flushed UPDATE carries "AND version = <the version If-Match named>",
                # so the precondition is re-checked atomically by the database.
                await session.commit()
            except StaleDataError as exc:
                raise HTTPException(412, "the resource changed after If-Match was checked") from exc
            response.headers["ETag"] = str(etag_for(item.version))
            return _body(item)

    @app.post("/items/{item_id}/reservations")
    async def reserve(
        item_id: int, reservation: Reservation, response: Response
    ) -> dict[str, int | str]:
        for attempt in range(1, MAX_RESERVE_ATTEMPTS + 1):
            async with sessions() as session:
                item = await load(session, item_id)
                if item.quantity < reservation.amount:
                    raise HTTPException(409, "insufficient quantity")
                item.quantity -= reservation.amount
                await race(item_id, attempt)
                try:
                    await session.commit()
                except StaleDataError:
                    continue  # re-read the new version and re-apply the decision
                response.headers["ETag"] = str(etag_for(item.version))
                response.headers["X-Attempts"] = str(attempt)
                return _body(item)
        raise HTTPException(409, f"gave up after {MAX_RESERVE_ATTEMPTS} conflicting attempts")

    return app
