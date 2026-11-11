"""AIP-164 soft delete lab: Delete marks, Get still reads, List hides, Undelete restores."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from api_labs.f04_rest_and_resource_modeling.soft_delete import (
    NAIVE_SCHEMA,
    BookIn,
    SoftDeleteStore,
    build_app,
)

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ADMIN = {"authorization": "Bearer admin"}
EDITOR = {"authorization": "Bearer editor"}
OUTSIDER = {"authorization": "Bearer outsider"}
BOOK = {"title": "SICP", "isbn": "978-0262510875"}

GRANTS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "books.create",
            "books.get",
            "books.list",
            "books.delete",
            "books.undelete",
            "books.expunge",
        }
    ),
    "editor": frozenset({"books.create", "books.get", "books.list", "books.delete"}),
    "outsider": frozenset[str](),
}


@dataclass
class FakeClock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Env:
    client: httpx.AsyncClient
    store: SoftDeleteStore
    clock: FakeClock


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    clock = FakeClock()
    store = SoftDeleteStore(clock=clock)
    app = build_app(store, GRANTS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Env(client, store, clock)
    store.conn.close()


async def create(env: Env, book_id: str, body: dict[str, str] = BOOK) -> httpx.Response:
    return await env.client.post("/v1/books", params={"book_id": book_id}, json=body, headers=ADMIN)


def stored_ids(store: SoftDeleteStore) -> list[str]:
    return [rid for (rid,) in store.conn.execute("SELECT id FROM books ORDER BY id")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_marks_row_and_returns_resource_with_delete_and_purge_time(env: Env) -> None:
    assert (await create(env, "sicp")).status_code == 200
    env.clock.now = T0 + timedelta(hours=1)

    response = await env.client.delete("/v1/books/sicp", headers=EDITOR)

    assert response.status_code == 200
    assert response.json() == {
        "name": "books/sicp",
        "title": "SICP",
        "isbn": "978-0262510875",
        "state": "DELETED",
        "create_time": "2026-11-10T12:00:00Z",
        "delete_time": "2026-11-10T13:00:00Z",
        "purge_time": "2026-12-10T13:00:00Z",
    }
    assert stored_ids(env.store) == ["sicp"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_reads_soft_deleted_resource_and_list_hides_it_unless_show_deleted(
    env: Env,
) -> None:
    await create(env, "keep", {"title": "Keep", "isbn": "1"})
    await create(env, "gone", {"title": "Gone", "isbn": "2"})
    await env.client.delete("/v1/books/gone", headers=ADMIN)

    got = await env.client.get("/v1/books/gone", headers=ADMIN)
    live = await env.client.get("/v1/books/keep", headers=ADMIN)
    default = await env.client.get("/v1/books", headers=ADMIN)
    everything = await env.client.get("/v1/books?show_deleted=true", headers=ADMIN)

    assert got.status_code == 200
    assert got.json()["state"] == "DELETED"
    assert "delete_time" not in live.json()
    assert "purge_time" not in live.json()
    assert [b["name"] for b in default.json()["books"]] == ["books/keep"]
    assert "delete_time" not in default.json()["books"][0]
    assert [b["name"] for b in everything.json()["books"]] == ["books/gone", "books/keep"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeat_delete_never_extends_retention_and_errors_without_allow_missing(
    env: Env,
) -> None:
    await create(env, "sicp")
    first = await env.client.delete("/v1/books/sicp", headers=ADMIN)
    env.clock.now = T0 + timedelta(days=29)

    strict = await env.client.delete("/v1/books/sicp", headers=ADMIN)
    lenient = await env.client.delete("/v1/books/sicp?allow_missing=true", headers=ADMIN)
    absent = await env.client.delete("/v1/books/never?allow_missing=true", headers=ADMIN)

    assert strict.status_code == 404
    assert strict.json()["error"]["status"] == "NOT_FOUND"
    assert lenient.status_code == 200
    assert lenient.json()["purge_time"] == first.json()["purge_time"] == "2026-12-10T12:00:00Z"
    assert absent.status_code == 204
    assert absent.content == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_undelete_restores_live_resource_and_rejects_a_live_target_with_409(
    env: Env,
) -> None:
    await create(env, "sicp")
    await env.client.delete("/v1/books/sicp", headers=ADMIN)

    denied = await env.client.post("/v1/books/sicp:undelete", json={}, headers=EDITOR)
    restored = await env.client.post("/v1/books/sicp:undelete", json={}, headers=ADMIN)
    again = await env.client.post("/v1/books/sicp:undelete", json={}, headers=ADMIN)
    missing = await env.client.post("/v1/books/never:undelete", json={}, headers=ADMIN)

    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == "missing books.undelete"
    assert restored.status_code == 200
    assert restored.json()["state"] == "ACTIVE"
    assert "delete_time" not in restored.json()
    assert "purge_time" not in restored.json()
    assert again.status_code == 409
    assert again.json()["error"]["status"] == "ALREADY_EXISTS"
    assert missing.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_unique_index_frees_business_key_and_blocks_colliding_undelete(
    env: Env,
) -> None:
    await create(env, "first")
    duplicate = await create(env, "second")
    await env.client.delete("/v1/books/first", headers=ADMIN)

    reused = await create(env, "second")
    collision = await env.client.post("/v1/books/first:undelete", json={}, headers=ADMIN)

    assert duplicate.status_code == 409
    assert reused.status_code == 200
    assert collision.status_code == 409
    assert collision.json()["error"]["message"] == "a live book now holds this isbn"
    still_deleted = await env.client.get("/v1/books/first", headers=ADMIN)
    assert still_deleted.json()["state"] == "DELETED"


@pytest.mark.unit
def test_composite_unique_with_nullable_delete_column_lets_live_duplicates_in() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(NAIVE_SCHEMA)

    conn.execute("INSERT INTO books VALUES ('a', '978-0262510875', NULL)")
    conn.execute("INSERT INTO books VALUES ('b', '978-0262510875', NULL)")

    live = conn.execute("SELECT count(*) FROM books WHERE delete_us IS NULL").fetchone()
    conn.close()
    assert live == (2,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reaper_honors_purge_time_boundary_and_leaves_a_tombstone(env: Env) -> None:
    await create(env, "sicp")
    await env.client.delete("/v1/books/sicp", headers=ADMIN)
    purge_at = T0 + timedelta(days=30)

    env.clock.now = purge_at - timedelta(microseconds=1)
    early = env.store.purge_expired()
    env.clock.now = purge_at
    due = env.store.purge_expired()

    assert early == []
    assert due == ["sicp"]
    assert (await env.client.get("/v1/books/sicp", headers=ADMIN)).status_code == 404
    undelete = await env.client.post("/v1/books/sicp:undelete", json={}, headers=ADMIN)
    assert undelete.status_code == 404
    recreate = await create(env, "sicp")
    assert recreate.status_code == 409
    assert recreate.json()["error"]["status"] == "ALREADY_EXISTS"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_soft_deleted_id_is_not_reusable_by_create(env: Env) -> None:
    await create(env, "sicp")
    await env.client.delete("/v1/books/sicp", headers=ADMIN)

    response = await create(env, "sicp", {"title": "Other", "isbn": "9"})

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "books/sicp is taken or tombstoned"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_is_checked_before_existence_so_403_leaks_nothing(env: Env) -> None:
    await create(env, "sicp")

    existing = await env.client.get("/v1/books/sicp", headers=OUTSIDER)
    nonexistent = await env.client.get("/v1/books/never", headers=OUTSIDER)
    authorized_missing = await env.client.get("/v1/books/never", headers=EDITOR)

    assert existing.status_code == nonexistent.status_code == 403
    assert existing.json() == nonexistent.json()
    assert authorized_missing.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expunge_needs_its_own_permission_and_removes_immediately(env: Env) -> None:
    await create(env, "sicp")
    await env.client.delete("/v1/books/sicp", headers=EDITOR)

    denied = await env.client.post("/v1/books/sicp:expunge", json={}, headers=EDITOR)
    expunged = await env.client.post("/v1/books/sicp:expunge", json={}, headers=ADMIN)
    gone = await env.client.post("/v1/books/sicp:expunge", json={}, headers=ADMIN)

    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == "missing books.expunge"
    assert expunged.status_code == 200
    assert expunged.json() == {}
    assert gone.status_code == 404
    assert stored_ids(env.store) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_tombstone_insert_rolls_back_the_row_removal(env: Env) -> None:
    await create(env, "sicp")
    env.store.conn.execute("INSERT INTO tombstones (id, purged_us) VALUES ('sicp', 0)")

    with pytest.raises(sqlite3.IntegrityError):
        env.store.expunge("sicp")

    assert not env.store.conn.in_transaction
    assert stored_ids(env.store) == ["sicp"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expunge_losing_a_race_answers_404_and_writes_no_tombstone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate another connection removing the row between the existence check and the removal.
    def vanished(book_id: str) -> None:
        return None

    monkeypatch.setattr(env.store, "_require", vanished)

    response = await env.client.post("/v1/books/ghost:expunge", json={}, headers=ADMIN)

    assert response.status_code == 404
    assert response.json()["error"]["status"] == "NOT_FOUND"
    assert env.store.conn.execute("SELECT count(*) FROM tombstones").fetchone() == (0,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reaper_reports_only_the_rows_it_removed_when_another_reaper_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await create(env, "a", {"title": "A", "isbn": "1"})
    await create(env, "b", {"title": "B", "isbn": "2"})
    await env.client.delete("/v1/books/a", headers=ADMIN)
    await env.client.delete("/v1/books/b", headers=ADMIN)
    env.clock.now = T0 + timedelta(days=31)

    # Another reaper already removed "a" after this one listed it as due.
    def remove_unless_a(book_id: str, *, due_us: int | None = None) -> bool:
        return book_id != "a"

    monkeypatch.setattr(env.store, "_remove", remove_unless_a)

    assert env.store.purge_expired() == ["b"]


class UndeleteBeforeRemove(SoftDeleteStore):
    """A store where Undelete, and optionally a new Delete, lands between list and remove."""

    redelete = False

    def _remove(self, book_id: str, *, due_us: int | None = None) -> bool:
        if due_us is not None:
            self.undelete(book_id)
            if self.redelete:
                self.delete(book_id, allow_missing=False)
        return super()._remove(book_id, due_us=due_us)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("redelete", "state", "purge_time"),
    [(False, "ACTIVE", None), (True, "DELETED", datetime(2027, 1, 10, 12, tzinfo=UTC))],
    ids=["undeleted", "undeleted-then-deleted-again"],
)
def test_reaper_never_removes_a_row_that_was_restored_after_it_listed_it(
    redelete: bool, state: str, purge_time: datetime | None
) -> None:
    clock = FakeClock()
    store = UndeleteBeforeRemove(clock=clock)
    store.redelete = redelete
    store.create("sicp", BookIn(title="SICP", isbn="1"))
    store.delete("sicp", allow_missing=False)
    clock.now = T0 + timedelta(days=31)

    purged = store.purge_expired()

    book = store.get("sicp")
    assert purged == []
    assert (book.state, book.purge_time) == (state, purge_time)
    assert store.conn.execute("SELECT count(*) FROM tombstones").fetchone() == (0,)
    store.conn.close()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "book_id",
    ["sicp:undelete", "Upper", "9lead", "trailing-", "a" * 64, "sicp\n"],
    ids=["colon", "uppercase", "digit-first", "trailing-hyphen", "too-long", "trailing-newline"],
)
async def test_resource_id_outside_the_aip_122_alphabet_is_rejected(env: Env, book_id: str) -> None:
    response = await create(env, book_id)

    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert stored_ids(env.store) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_colon_custom_method_segment_is_captured_by_get_route_and_breaks_relative_join(
    env: Env,
) -> None:
    await create(env, "sicp")

    captured = await env.client.get("/v1/books/sicp:undelete", headers=ADMIN)
    base = httpx.URL("http://test/v1/books/")

    assert captured.status_code == 400
    assert captured.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert base.join("sicp:undelete") == httpx.URL("sicp:undelete")
    assert base.join("./sicp:undelete").path == "/v1/books/sicp:undelete"
