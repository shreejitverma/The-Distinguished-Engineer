"""Lab for Session-Internals-Identity-Map-Unit-of-Work-Flush-and-Expire-On-Commit."""

from __future__ import annotations

import gc

import httpx
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import InvalidRequestError, MissingGreenlet, StatementError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import DetachedInstanceError

from api_labs.f13_database_access_and_transactions_for_apis.session_internals import (
    Author,
    Book,
    Node,
    build_app,
    lifecycle_state,
    make_async_engine,
    make_sync_engine,
)


@pytest.mark.unit
def test_identity_map_returns_same_object_and_get_hit_emits_no_sql() -> None:
    engine, log = make_sync_engine()
    with Session(engine) as s:
        s.add(Author(id=1, name="ada"))
        s.commit()
    with Session(engine) as s:
        log.clear()
        first = s.get(Author, 1)
        selects_after_miss = log.count("SELECT")
        second = s.get(Author, 1)
        selects_after_hit = log.count("SELECT")
        via_select = s.scalars(select(Author).where(Author.id == 1)).one()
        assert first is not None
        assert first is second is via_select
        assert inspect(first).key == (Author, (1,), None)
        assert (selects_after_miss, selects_after_hit) == (1, 1)
        assert log.count("SELECT") == 2  # select() always goes to the database


@pytest.mark.unit
def test_reselect_keeps_in_memory_values_until_populate_existing() -> None:
    engine, _ = make_sync_engine()
    with Session(engine) as s:
        s.add(Author(id=1, name="ada"))
        s.commit()
    with Session(engine) as s:
        author = s.get(Author, 1)
        assert author is not None
        assert author.name == "ada"
        s.execute(text("UPDATE author SET name = 'grace' WHERE id = 1"))
        stale = s.scalars(select(Author).where(Author.id == 1)).one()
        assert stale is author
        assert stale.name == "ada"  # the row said 'grace'; the identity map won
        fresh = s.scalars(
            select(Author).where(Author.id == 1).execution_options(populate_existing=True)
        ).one()
        assert fresh is author
        assert author.name == "grace"


@pytest.mark.unit
def test_identity_map_holds_clean_objects_weakly_and_dirty_objects_strongly() -> None:
    engine, _ = make_sync_engine()
    with Session(engine) as s:
        s.add(Author(id=1, name="ada"))
        s.commit()
    with Session(engine) as s:
        clean = s.get(Author, 1)
        assert len(s.identity_map) == 1
        del clean
        gc.collect()
        assert len(s.identity_map) == 0  # clean object collected, entry gone
        dirty = s.get(Author, 1)
        assert dirty is not None
        dirty.name = "changed"
        del dirty
        gc.collect()
        assert len(s.identity_map) == 1  # _strong_obj keeps the pending change alive
        s.commit()
    with Session(engine) as s:
        reloaded = s.get(Author, 1)
        assert reloaded is not None
        assert reloaded.name == "changed"


@pytest.mark.unit
def test_flush_orders_tables_by_foreign_key_not_by_add_order() -> None:
    engine, log = make_sync_engine()
    with Session(engine) as s:
        author = Author(id=1, name="ada")
        book = Book(id=10, title="notes", author=author)
        s.add(book)  # the child is added first; the author arrives by cascade
        s.flush()
        assert log.dml() == ["INSERT author", "INSERT book"]
        log.clear()
        s.delete(author)  # cascade="all, delete-orphan" deletes the book too
        s.flush()
        assert log.dml() == ["DELETE book", "DELETE author"]


@pytest.mark.unit
def test_self_referential_cycle_is_flushed_row_by_row_parent_first() -> None:
    engine, log = make_sync_engine()
    with Session(engine) as s:
        root = Node(id=1, name="root")
        leaf = Node(id=2, name="leaf", parent=root)
        s.add(leaf)
        s.add(root)
        s.flush()
        inserts = log.parameters_for("INSERT INTO node")
        assert len(inserts) == 2  # per-state actions: no single batched INSERT
        assert [params[1] for params in inserts] == ["root", "leaf"]


@pytest.mark.unit
def test_autoflush_makes_pending_rows_visible_and_no_autoflush_hides_them() -> None:
    engine, log = make_sync_engine()
    with Session(engine) as s:
        s.add(Author(id=2, name="pending"))
        with s.no_autoflush:
            assert s.scalars(select(Author).where(Author.id == 2)).first() is None
        assert log.dml() == []
        found = s.scalars(select(Author).where(Author.id == 2)).first()
        assert log.dml() == ["INSERT author"]
        assert found is not None
        assert found.name == "pending"


@pytest.mark.unit
@pytest.mark.parametrize("expire_on_commit", [True, False])
def test_commit_expiry_decides_whether_next_read_emits_select(expire_on_commit: bool) -> None:
    engine, log = make_sync_engine()
    with Session(engine, expire_on_commit=expire_on_commit) as s:
        author = Author(id=1, name="ada")
        s.add(author)
        s.commit()
        assert inspect(author).expired is expire_on_commit
        assert ("name" in author.__dict__) is not expire_on_commit
        log.clear()
        assert author.name == "ada"
        assert log.count("SELECT") == (1 if expire_on_commit else 0)


@pytest.mark.unit
def test_expired_object_after_close_raises_detached_instance_error() -> None:
    engine, _ = make_sync_engine()
    s = Session(engine)
    author = Author(id=1, name="ada")
    s.add(author)
    s.commit()
    s.close()
    assert lifecycle_state(author) == "detached"
    with pytest.raises(DetachedInstanceError, match="not bound to a Session"):
        _ = author.name

    keeper = Session(engine, expire_on_commit=False)
    other = Author(id=2, name="lin")
    keeper.add(other)
    keeper.commit()
    keeper.close()
    assert lifecycle_state(other) == "detached"
    assert other.name == "lin"


@pytest.mark.unit
def test_savepoint_release_does_not_expire_but_outer_commit_does() -> None:
    engine, _ = make_sync_engine()
    with Session(engine) as s:
        author = Author(id=1, name="ada")
        s.add(author)
        s.flush()
        with s.begin_nested():
            author.name = "inner"
        assert not inspect(author).expired
        assert author.__dict__["name"] == "inner"
        s.commit()
        assert inspect(author).expired


@pytest.mark.unit
def test_rollback_reverts_pending_to_transient_and_expires_persistent() -> None:
    engine, _ = make_sync_engine()
    with Session(engine) as s:
        s.add(Author(id=1, name="ada"))
        s.commit()
    with Session(engine) as s:
        author = s.get(Author, 1)
        assert author is not None
        author.name = "dirty"
        newcomer = Author(id=3, name="new")
        s.add(newcomer)
        s.flush()
        assert lifecycle_state(newcomer) == "persistent"
        s.rollback()
        assert lifecycle_state(newcomer) == "transient"
        assert inspect(author).expired
        assert author.name == "ada"


@pytest.mark.unit
def test_merge_returns_the_identity_mapped_instance_not_its_argument() -> None:
    engine, _ = make_sync_engine()
    with Session(engine) as s:
        s.add(Author(id=1, name="ada"))
        s.commit()
    with Session(engine) as s:
        author = s.get(Author, 1)
        incoming = Author(id=1, name="from-request")
        merged = s.merge(incoming)
        assert merged is author
        assert merged is not incoming
        assert author is not None
        assert author.name == "from-request"
        assert lifecycle_state(incoming) == "transient"


@pytest.mark.unit
def test_attaching_a_second_object_with_a_mapped_key_is_refused() -> None:
    engine, _ = make_sync_engine()
    with Session(engine, expire_on_commit=False) as s:
        s.add(Author(id=1, name="ada"))
        s.commit()
        detached = s.get(Author, 1)
    with Session(engine) as s:
        resident = s.get(Author, 1)
        assert resident is not None
        assert detached is not None
        with pytest.raises(InvalidRequestError, match="already present in this session"):
            s.add(detached)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_default_expiry_turns_attribute_read_into_missing_greenlet() -> None:
    engine, _ = await make_async_engine()
    try:
        sessions = async_sessionmaker(engine)  # expire_on_commit defaults to True
        async with sessions() as s:
            author = Author(id=1, name="ada")
            s.add(author)
            await s.commit()
            with pytest.raises(
                (StatementError, MissingGreenlet), match="greenlet_spawn has not been called"
            ) as err:
                _ = author.name
            # 2.1.8 fails at cursor creation, before an execution context exists, so
            # the engine wraps it in StatementError; 2.0.57 fails in cursor.execute and
            # re-raises MissingGreenlet unwrapped.
            error = err.value
            root = error.orig if isinstance(error, StatementError) else error
            assert isinstance(root, MissingGreenlet)
            assert await author.awaitable_attrs.name == "ada"
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_expire_on_commit_false_reads_without_sql_even_after_close() -> None:
    engine, log = await make_async_engine()
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as s:
            author = Author(id=1, name="ada")
            s.add(author)
            await s.commit()
            log.clear()
            assert author.name == "ada"
            assert log.entries == []
        assert lifecycle_state(author) == "detached"
        assert author.name == "ada"
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(("expire_on_commit", "status"), [(True, 500), (False, 201)])
async def test_create_endpoint_status_depends_on_expire_on_commit(
    expire_on_commit: bool, status: int
) -> None:
    engine, _ = await make_async_engine()
    try:
        app = build_app(async_sessionmaker(engine, expire_on_commit=expire_on_commit))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/authors", json={"name": "ada"})
        assert response.status_code == status
        if status == 201:
            assert response.json() == {"id": 1, "name": "ada"}
    finally:
        await engine.dispose()
