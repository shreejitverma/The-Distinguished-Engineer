"""Lab for the N+1 note: query counts, DataLoader batching window, and loader lifetime."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update
from strawberry.dataloader import DataLoader
from strawberry.exceptions import WrongNumberOfResultsReturned

from api_labs.f20_graphql_json_rpc_and_event_apis.the_graphql_n_plus_1_problem_and_dataloader_tick_batching_internals import (  # noqa: E501
    AUTHORS_AND_BOOKS,
    AUTHORS_BOOKS_AND_REVIEWS,
    BookRow,
    Database,
    build_app,
    build_context,
    database,
    run_query,
)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    async with database(authors=50, books_per_author=3, reviews_per_book=2) as handle:
        yield handle


class Recorder:
    """A batch function that records every batch of keys it is called with."""

    def __init__(self) -> None:
        self.batches: list[list[int]] = []

    async def __call__(self, keys: list[int]) -> list[int]:
        self.batches.append(list(keys))
        return [key * 10 for key in keys]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_resolvers_issue_one_query_per_parent(db: Database) -> None:
    data = await run_query(AUTHORS_AND_BOOKS, build_context(db.session_factory, batched=False))
    assert len(data["authors"]) == 50
    assert len(db.log.statements) == 51
    assert db.log.count_containing("FROM authors") == 1
    assert db.log.count_containing("WHERE books.author_id = ?") == 50
    assert set(db.log.parameter_counts[1:]) == {1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dataloader_collapses_the_cascade_to_two_queries(db: Database) -> None:
    data = await run_query(AUTHORS_AND_BOOKS, build_context(db.session_factory, batched=True))
    assert len(data["authors"]) == 50
    assert len(db.log.statements) == 2
    assert "FROM authors" in db.log.statements[0]
    assert "WHERE books.author_id IN (" in db.log.statements[1]
    assert db.log.parameter_counts == [0, 50]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_strategies_return_identical_data(db: Database) -> None:
    naive = await run_query(AUTHORS_AND_BOOKS, build_context(db.session_factory, batched=False))
    batched = await run_query(AUTHORS_AND_BOOKS, build_context(db.session_factory, batched=True))
    assert naive == batched
    assert [len(author["books"]) for author in batched["authors"]] == [3] * 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cost_is_one_query_per_depth_level_with_loaders(db: Database) -> None:
    naive = await run_query(
        AUTHORS_BOOKS_AND_REVIEWS, build_context(db.session_factory, batched=False)
    )
    assert len(db.log.statements) == 1 + 50 + 150
    db.log.reset()
    batched = await run_query(
        AUTHORS_BOOKS_AND_REVIEWS, build_context(db.session_factory, batched=True)
    )
    assert naive == batched
    assert len(db.log.statements) == 3
    assert "FROM authors" in db.log.statements[0]
    assert "FROM books" in db.log.statements[1]
    assert "FROM reviews" in db.log.statements[2]
    assert db.log.parameter_counts == [0, 50, 150]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_batch_size_bounds_the_in_list_in_sql() -> None:
    async with database(authors=100, books_per_author=1, reviews_per_book=0) as db:
        context = build_context(db.session_factory, batched=True, max_batch_size=40)
        await run_query(AUTHORS_AND_BOOKS, context)
        assert db.log.parameter_counts == [0, 40, 40, 20]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loads_issued_in_one_tick_coalesce_into_one_batch() -> None:
    recorder = Recorder()
    loader = DataLoader(load_fn=recorder)
    values = await asyncio.gather(*(loader.load(key) for key in range(10)))
    assert recorder.batches == [list(range(10))]
    assert values == [key * 10 for key in range(10)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_awaiting_each_load_in_a_loop_defeats_batching() -> None:
    recorder = Recorder()
    loader = DataLoader(load_fn=recorder)
    for key in range(5):
        await loader.load(key)
    assert recorder.batches == [[0], [1], [2], [3], [4]]


async def _load_after_hops(loader: DataLoader[int, int], key: int, hops: int) -> int:
    for _ in range(hops):
        await asyncio.sleep(0)
    return await loader.load(key)


async def _batches_when_second_load_arrives_after(hops: int) -> list[list[int]]:
    recorder = Recorder()
    loader = DataLoader(load_fn=recorder)
    await asyncio.gather(
        asyncio.ensure_future(_load_after_hops(loader, 0, 0)),
        asyncio.ensure_future(_load_after_hops(loader, 1, hops)),
    )
    return recorder.batches


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_window_is_two_event_loop_iterations() -> None:
    assert await _batches_when_second_load_arrives_after(0) == [[0, 1]]
    assert await _batches_when_second_load_arrives_after(1) == [[0, 1]]
    assert await _batches_when_second_load_arrives_after(2) == [[0], [1]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_eager_task_factory_shrinks_the_window_by_one_iteration() -> None:
    loop = asyncio.get_running_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        assert await _batches_when_second_load_arrives_after(0) == [[0, 1]]
        assert await _batches_when_second_load_arrives_after(1) == [[0], [1]]
    finally:
        loop.set_task_factory(None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolvers_still_batch_under_the_eager_task_factory(db: Database) -> None:
    loop = asyncio.get_running_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        await run_query(AUTHORS_AND_BOOKS, build_context(db.session_factory, batched=True))
    finally:
        loop.set_task_factory(None)
    assert db.log.parameter_counts == [0, 50]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_batch_size_splits_one_tick_into_several_batches() -> None:
    recorder = Recorder()
    loader = DataLoader(load_fn=recorder, max_batch_size=20)
    await asyncio.gather(*(loader.load(key) for key in range(50)))
    assert [len(batch) for batch in recorder.batches] == [20, 20, 10]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cache_dedupes_repeated_keys_inside_a_batch() -> None:
    shared = Recorder()
    loader = DataLoader(load_fn=shared)
    await asyncio.gather(*(loader.load(key) for key in [1, 1, 2, 2, 3]))
    assert shared.batches == [[1, 2, 3]]
    uncached = Recorder()
    plain = DataLoader(load_fn=uncached, cache=False)
    await asyncio.gather(*(plain.load(key) for key in [1, 1, 2, 2, 3]))
    assert uncached.batches == [[1, 1, 2, 2, 3]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loader_trusts_result_order_and_checks_only_the_length() -> None:
    async def reversed_results(keys: list[int]) -> list[int]:
        return [key * 10 for key in reversed(keys)]

    loader = DataLoader(load_fn=reversed_results)
    first, second = await asyncio.gather(loader.load(1), loader.load(2))
    assert (first, second) == (20, 10)

    async def too_short(keys: list[int]) -> list[int]:
        return keys[:-1]

    short = DataLoader(load_fn=too_short)
    outcomes = await asyncio.gather(short.load(1), short.load(2), return_exceptions=True)
    assert all(isinstance(outcome, WrongNumberOfResultsReturned) for outcome in outcomes)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_exception_value_fails_only_its_own_key() -> None:
    async def fn(keys: list[int]) -> list[int | BaseException]:
        return [ValueError(f"missing {key}") if key == 2 else key for key in keys]

    loader = DataLoader(load_fn=fn)
    outcomes = await asyncio.gather(
        loader.load(1), loader.load(2), loader.load(3), return_exceptions=True
    )
    assert outcomes[0] == 1
    assert outcomes[2] == 3
    assert isinstance(outcomes[1], ValueError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_batch_fails_every_key_and_stays_cached() -> None:
    calls: list[list[int]] = []

    async def fn(keys: list[int]) -> list[int]:
        calls.append(list(keys))
        raise RuntimeError("database down")

    loader = DataLoader(load_fn=fn)
    first = await asyncio.gather(loader.load(1), loader.load(2), return_exceptions=True)
    assert [type(outcome) for outcome in first] == [RuntimeError, RuntimeError]
    with pytest.raises(RuntimeError, match="database down"):
        await loader.load(1)
    assert calls == [[1, 2]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelling_every_waiter_before_dispatch_skips_the_query() -> None:
    recorder = Recorder()
    loader = DataLoader(load_fn=recorder)
    futures = [loader.load(key) for key in range(3)]
    await asyncio.sleep(0)
    for future in futures:
        assert isinstance(future, asyncio.Future)
        future.cancel()
    await asyncio.sleep(0.01)
    assert recorder.batches == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_forces_a_refetch_and_prime_skips_the_fetch() -> None:
    recorder = Recorder()
    loader = DataLoader(load_fn=recorder)
    assert await loader.load(1) == 10
    assert await loader.load(1) == 10
    assert recorder.batches == [[1]]
    loader.clear(1)
    assert await loader.load(1) == 10
    assert recorder.batches == [[1], [1]]
    loader.prime(7, 777)
    assert await loader.load(7) == 777
    assert recorder.batches == [[1], [1]]


@pytest.mark.unit
def test_a_loader_stays_bound_to_the_first_event_loop_it_serves() -> None:
    loader = DataLoader(load_fn=Recorder())

    async def use(key: int) -> int:
        return await loader.load(key)

    assert asyncio.run(use(1)) == 10
    assert asyncio.run(use(1)) == 10
    with pytest.raises(RuntimeError, match="Event loop is closed"):
        asyncio.run(use(2))


QUERY_ONE_BOOK_TITLE = "{ authors(limit: 1) { books { id title } } }"


async def _post(client: httpx.AsyncClient, query: str) -> list[dict[str, str]]:
    response = await client.post("/graphql", json={"query": query})
    assert response.status_code == 200
    body = response.json()
    assert "errors" not in body
    return body["data"]["authors"][0]["books"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_loader_per_request_never_serves_another_requests_cache(db: Database) -> None:
    app = build_app(db.session_factory, share_loaders_across_requests=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = await _post(client, QUERY_ONE_BOOK_TITLE)
        async with db.session_factory.begin() as session:
            await session.execute(
                update(BookRow).where(BookRow.id == before[0]["id"]).values(title="renamed")
            )
        db.log.reset()
        after = await _post(client, QUERY_ONE_BOOK_TITLE)
    assert before[0]["title"] != "renamed"
    assert after[0]["title"] == "renamed"
    assert db.log.count_containing("FROM books") == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_process_wide_loader_serves_stale_data_to_later_requests(db: Database) -> None:
    app = build_app(db.session_factory, share_loaders_across_requests=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = await _post(client, QUERY_ONE_BOOK_TITLE)
        async with db.session_factory.begin() as session:
            await session.execute(
                update(BookRow).where(BookRow.id == before[0]["id"]).values(title="renamed")
            )
        db.log.reset()
        after = await _post(client, QUERY_ONE_BOOK_TITLE)
    assert after[0]["title"] == before[0]["title"]
    assert db.log.count_containing("FROM books") == 0
