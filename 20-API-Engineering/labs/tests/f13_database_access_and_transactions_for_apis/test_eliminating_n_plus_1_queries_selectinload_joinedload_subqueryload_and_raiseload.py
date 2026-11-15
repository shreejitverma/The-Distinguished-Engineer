"""Loading strategies measured as statement counts, SQL shapes, and exceptions (SQLAlchemy 2.1)."""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import exc, select
from sqlalchemy.orm import (
    Load,
    Session,
    contains_eager,
    joinedload,
    lazyload,
    noload,
    raiseload,
    selectinload,
    subqueryload,
)

from api_labs.f13_database_access_and_transactions_for_apis.loading_strategies import (
    Author,
    Book,
    Edition,
    Printing,
    RaiseloadSession,
    StatementLog,
    Strategy,
    authors_with_books,
    count_books,
    counting_engine,
    create_app,
    create_async_engine,
    create_schema,
    record_statements,
    seed,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Engine

Lab = tuple["Engine", StatementLog]


@pytest.fixture
def lab() -> Iterator[Lab]:
    with counting_engine() as (engine, log):
        seed(engine, n_authors=3, books_per_author=3, reviews_per_book=2, tags=4)
        log.clear()
        yield engine, log


# --- statement counts ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("lazy", lambda n: 1 + n),
        ("immediate", lambda n: 1 + n),
        ("selectin", lambda n: 2),
        ("subquery", lambda n: 2),
        ("joined", lambda n: 1),
    ],
)
@pytest.mark.parametrize("n_authors", [5, 40])
def test_statement_count_per_strategy(
    strategy: Strategy, expected: Callable[[int], int], n_authors: int
) -> None:
    with counting_engine() as (engine, log):
        seed(engine, n_authors=n_authors, books_per_author=2, reviews_per_book=0)
        log.clear()
        with Session(engine) as session:
            assert count_books(session, strategy) == 2 * n_authors
        assert len(log) == expected(n_authors), log.statements


@pytest.mark.unit
def test_selectinload_batches_parent_keys_500_at_a_time() -> None:
    with counting_engine() as (engine, log):
        seed(engine, n_authors=1201, books_per_author=0, reviews_per_book=0)
        log.clear()
        with Session(engine) as session:
            session.scalars(authors_with_books("selectin")).all()
        assert len(log) == 1 + 3
        assert log.in_list_sizes() == [500, 500, 201]


@pytest.mark.unit
def test_selectinload_chunksize_option_is_honored(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books, chunksize=2))
        assert sum(len(a.books) for a in session.scalars(stmt)) == 9
    assert log.in_list_sizes() == [2, 1]
    with Session(engine) as session, pytest.raises(exc.ArgumentError, match="chunksize=0"):
        session.scalars(select(Author).options(selectinload(Author.books, chunksize=0))).all()


@pytest.mark.unit
def test_selectinload_many_to_one_dedupes_foreign_keys_and_omits_the_join(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        books = session.scalars(select(Book).options(selectinload(Book.author))).all()
        assert len(books) == 9
        assert {b.author.name for b in books} == {"author-1", "author-2", "author-3"}
    second = log.statements[1]
    assert second.startswith("SELECT author.id, author.name FROM author WHERE author.id IN")
    assert "JOIN" not in second
    assert log.in_list_sizes() == [3]  # nine books, three distinct author_id values


@pytest.mark.unit
def test_selectinload_many_to_many_joins_the_secondary_table(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        authors = session.scalars(select(Author).options(selectinload(Author.tags))).all()
        assert [len(a.tags) for a in authors] == [4, 4, 4]
    assert log.statements[1].startswith(
        "SELECT author_tag.author_id, tag.id, tag.label FROM author_tag JOIN tag"
    )


@pytest.mark.unit
def test_selectinload_composite_key_renders_tuple_in() -> None:
    with counting_engine() as (engine, log):
        with Session(engine) as session:
            session.add_all(
                [
                    Edition(prefix="978", seq=1, printings=[Printing(id=1), Printing(id=2)]),
                    Edition(prefix="979", seq=2, printings=[Printing(id=3)]),
                ]
            )
            session.commit()
        log.clear()
        with Session(engine) as session:
            stmt = select(Edition).order_by(Edition.prefix).options(selectinload(Edition.printings))
            assert [len(e.printings) for e in session.scalars(stmt)] == [2, 1]
        assert "(printing.prefix, printing.seq) IN (VALUES (?, ?), (?, ?))" in log.statements[1]


@pytest.mark.unit
def test_selectinload_works_with_yield_per_one_batch_per_partition(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books)).execution_options(yield_per=2)
        assert sum(len(a.books) for a in session.scalars(stmt)) == 9
    assert log.in_list_sizes() == [2, 1]


# --- joined eager loading -----------------------------------------------------------------------


@pytest.mark.unit
def test_joinedload_collection_requires_unique(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        with pytest.raises(exc.InvalidRequestError, match=r"unique\(\) method must be invoked"):
            session.scalars(select(Author).options(joinedload(Author.books))).all()
        authors = session.scalars(select(Author).options(joinedload(Author.books))).unique().all()
        assert [len(a.books) for a in authors] == [3, 3, 3]
    assert "LEFT OUTER JOIN book AS book_1 ON author.id = book_1.author_id" in log.statements[-1]


@pytest.mark.unit
def test_joinedload_with_limit_wraps_the_parent_query_in_a_subquery(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(joinedload(Author.books)).order_by(Author.id).limit(2)
        authors = session.scalars(stmt).unique().all()
        assert [(a.id, len(a.books)) for a in authors] == [(1, 3), (2, 3)]
    (sql,) = log.statements
    assert (
        "FROM (SELECT author.id AS id, author.name AS name FROM author ORDER BY author.id " in sql
    )
    assert ") AS anon_1 LEFT OUTER JOIN book AS book_1 ON anon_1.id = book_1.author_id" in sql


@pytest.mark.unit
def test_joinedload_of_two_collections_multiplies_rows(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(joinedload(Author.books), joinedload(Author.tags))
        compiled = stmt.compile(engine, compile_kwargs={"literal_binds": True})
        raw_rows = session.connection().exec_driver_sql(str(compiled)).all()
        assert len(raw_rows) == 3 * 3 * 4  # authors x books x tags
        authors = session.scalars(stmt).unique().all()
        assert [(len(a.books), len(a.tags)) for a in authors] == [(3, 4)] * 3
    log.clear()
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books), selectinload(Author.tags))
        session.scalars(stmt).all()
    assert len(log) == 3  # one per entity, no cross product


@pytest.mark.unit
def test_joinedload_innerjoin_for_non_nullable_many_to_one(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        books = session.scalars(select(Book).options(joinedload(Book.author, innerjoin=True))).all()
        assert len(books) == 9  # many-to-one: no row multiplication, so no unique() needed
    (sql,) = log.statements
    assert "FROM book JOIN author AS author_1 ON author_1.id = book.author_id" in sql


# --- subquery eager loading ---------------------------------------------------------------------


@pytest.mark.unit
def test_subqueryload_reruns_the_original_query_as_a_subquery(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).where(Author.name != "nobody").options(subqueryload(Author.books))
        assert sum(len(a.books) for a in session.scalars(stmt)) == 9
    first, second = log.statements
    assert "WHERE author.name != ?" in first
    assert "FROM (SELECT author.id AS author_id FROM author WHERE author.name != ?) AS anon_1" in (
        second
    )


@pytest.mark.unit
@pytest.mark.parametrize("option", [joinedload(Author.books), subqueryload(Author.books)])
def test_buffering_loaders_refuse_yield_per(lab: Lab, option: object) -> None:
    engine, _ = lab
    stmt = select(Author).options(option).execution_options(yield_per=1)  # type: ignore[arg-type]
    with Session(engine) as session, pytest.raises(exc.InvalidRequestError, match="yield_per"):
        session.scalars(stmt).all()


# --- explicit joins and filtered collections ----------------------------------------------------


@pytest.mark.unit
def test_contains_eager_filtered_collection_needs_populate_existing(lab: Lab) -> None:
    engine, log = lab
    filtered = (
        select(Author)
        .join(Author.books)
        .where(Book.title.like("%-b0"))
        .order_by(Author.id)
        .options(contains_eager(Author.books))
    )
    with Session(engine) as session:
        # Hold the references: the identity map is weak, so unreferenced objects would be
        # garbage collected and the filtered query would build new ones.
        loaded = session.scalars(select(Author).options(selectinload(Author.books))).all()
        stale = session.scalars(filtered).unique().all()
        assert stale == loaded
        assert [len(a.books) for a in stale] == [3, 3, 3]  # already loaded: not overwritten
        fresh = session.scalars(filtered.execution_options(populate_existing=True)).unique().all()
        assert [[b.title for b in a.books] for a in fresh] == [["a1-b0"], ["a2-b0"], ["a3-b0"]]
    assert "JOIN book ON author.id = book.author_id WHERE book.title LIKE ?" in log.statements[-1]


@pytest.mark.unit
def test_loader_criteria_and_filters_the_selectin_batch(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books.and_(Book.title.like("%-b1"))))
        assert [[b.title for b in a.books] for a in session.scalars(stmt)] == [
            ["a1-b1"],
            ["a2-b1"],
            ["a3-b1"],
        ]
    assert "WHERE book.author_id IN (?, ?, ?) AND book.title LIKE ?" in log.statements[1]


# --- raiseload ----------------------------------------------------------------------------------


@pytest.mark.unit
def test_raiseload_fails_before_emitting_sql(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        author = session.scalars(select(Author).options(raiseload(Author.books))).first()
        assert author is not None
        before = len(log)
        with pytest.raises(
            exc.InvalidRequestError, match=r"'Author\.books' is not available due to"
        ):
            _ = author.books
        assert len(log) == before


@pytest.mark.unit
def test_raiseload_star_reaches_nested_entities(lab: Lab) -> None:
    engine, _ = lab
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books), raiseload("*"))
        book = session.scalars(stmt).first().books[0]  # type: ignore[union-attr]
        with pytest.raises(exc.InvalidRequestError, match=r"'Book\.reviews' is not available"):
            _ = book.reviews


@pytest.mark.unit
def test_load_entity_raiseload_star_is_scoped_and_lets_n_plus_1_through(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books), Load(Author).raiseload("*"))
        authors = session.scalars(stmt).all()
        assert sum(len(b.reviews) for a in authors for b in a.books) == 18
        with pytest.raises(exc.InvalidRequestError, match=r"'Author\.tags'"):
            _ = authors[0].tags
    assert len(log) == 2 + 9  # one lazy SELECT per book slipped through


@pytest.mark.unit
def test_raiseload_sql_only_allows_identity_map_hits(lab: Lab) -> None:
    engine, log = lab
    for sql_only, raises in [(False, True), (True, False)]:
        with Session(engine) as session:
            stmt = select(Author).options(
                selectinload(Author.books), raiseload("*", sql_only=sql_only)
            )
            author = session.scalars(stmt).first()
            assert author is not None
            book = author.books[0]
            before = len(log)
            if raises:
                with pytest.raises(exc.InvalidRequestError, match="lazy='raise'"):
                    _ = book.author
            else:
                assert book.author.name == "author-1"  # parent is in the identity map
            assert len(log) == before


@pytest.mark.unit
def test_sql_only_outcome_depends_on_the_parent_still_being_referenced(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).options(selectinload(Author.books), raiseload("*", sql_only=True))
        book = session.scalars(stmt).first().books[0]  # type: ignore[union-attr]
        gc.collect()  # the Author had no strong reference left; the identity map is weak
        before = len(log)
        with pytest.raises(exc.InvalidRequestError, match="lazy='raise_on_sql'"):
            _ = book.author
        assert len(log) == before


@pytest.mark.unit
def test_raiseload_does_not_apply_to_the_flush(lab: Lab) -> None:
    engine, log = lab
    with Session(engine) as session:
        stmt = select(Author).where(Author.id == 2).options(raiseload("*"))
        author = session.scalars(stmt).one()
        log.clear()
        session.delete(author)
        session.flush()  # cascade needs Author.books and the author_tag rows: loads them anyway
        assert log.statements[0].startswith("SELECT book.id, book.title, book.author_id FROM book")
        assert any(s.startswith("DELETE FROM author") for s in log.statements)
        session.rollback()


@pytest.mark.unit
def test_lazyload_chained_options_are_sticky_on_loaded_instances(lab: Lab) -> None:
    engine, _ = lab
    with Session(engine) as session:
        stmt = (
            select(Author)
            .where(Author.id == 1)
            .options(lazyload(Author.books).raiseload(Book.reviews))
        )
        author = session.scalars(stmt).one()
        with pytest.raises(exc.InvalidRequestError, match=r"'Book\.reviews'"):
            _ = author.books[0].reviews
        session.expire_all()
        again = session.scalars(select(Author).where(Author.id == 1)).one()  # no options at all
        assert again is author
        with pytest.raises(exc.InvalidRequestError, match=r"'Book\.reviews'"):
            _ = again.books[0].reviews


@pytest.mark.unit
def test_noload_is_deprecated_and_returns_an_empty_collection(lab: Lab) -> None:
    engine, _ = lab
    with pytest.warns(exc.SADeprecationWarning, match="noload"):
        option = noload(Author.books)
    with Session(engine) as session:
        author = session.scalars(select(Author).options(option)).first()
        assert author is not None
        assert author.books == []  # silently wrong: the author has three books


@pytest.mark.unit
def test_raiseload_session_appends_raiseload_star_to_every_query(lab: Lab) -> None:
    engine, log = lab
    with RaiseloadSession(engine) as session:
        authors = session.scalars(authors_with_books("selectin")).all()
        assert sum(len(a.books) for a in authors) == 9  # explicit options still win
        before = len(log)
        with pytest.raises(exc.InvalidRequestError, match=r"'Book\.reviews'"):
            _ = authors[0].books[0].reviews
        assert len(log) == before
    with Session(engine) as session:  # the production class has no hook
        authors = session.scalars(authors_with_books("selectin")).all()
        assert len(authors[0].books[0].reviews) == 2


# --- the API ------------------------------------------------------------------------------------


async def _get(path: str, n_authors: int, session_class: type[Session]) -> tuple[object, int]:
    engine = create_async_engine()
    log = StatementLog()
    record_statements(engine.sync_engine, log)
    try:
        await create_schema(engine, n_authors)
        log.clear()
        app = create_app(engine, session_class)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(path)
        return response.json(), len(log)
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("n_authors", [3, 30])
async def test_endpoint_statement_budget_is_constant_in_n(n_authors: int) -> None:
    body, statements = await _get("/authors", n_authors, RaiseloadSession)
    assert isinstance(body, list)
    assert len(body) == n_authors
    assert body[0] == {
        "id": 1,
        "books": [
            {"title": "a1-b0", "stars": [1, 2]},
            {"title": "a1-b1", "stars": [1, 2]},
            {"title": "a1-b2", "stars": [1, 2]},
        ],
    }
    # BEGIN is not a cursor execute on aiosqlite; authors, books, reviews:
    assert statements == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raiseload_session_turns_a_forgotten_option_into_a_deterministic_error() -> None:
    with pytest.raises(exc.InvalidRequestError, match=r"'Book\.reviews' is not available") as info:
        await _get("/authors-regressed", 3, RaiseloadSession)
    assert not isinstance(info.value, exc.StatementError)  # raised before any IO was attempted
