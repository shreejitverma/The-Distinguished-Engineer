"""AIP-165 purge lab: force=False previews, filter validation, chunked LRO deletion."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api_labs.f06_bulk_long_running_and_error_contracts.criteria_purge import (
    MAX_CONDITIONS,
    MAX_FILTER_LENGTH,
    ApiError,
    Availability,
    BookStore,
    Condition,
    PurgeBooksRequest,
    PurgeService,
    create_app,
    parse_filter,
)

BACKORDER_FILTER = 'availability = "BACKORDER"'


def seeded_store(per_publisher: int = 25) -> BookStore:
    """Books 000..N-1 under acme and globex, created in reverse name order.

    Every fifth book is in stock and the rest are backordered.
    """
    store = BookStore()
    for publisher in ("globex", "acme"):
        for i in reversed(range(per_publisher)):
            availability: Availability = "IN_STOCK" if i % 5 == 0 else "BACKORDER"
            store.create(
                f"publishers/{publisher}/books/b{i:03d}",
                availability,
                f"2026-01-{1 + i % 28:02d}T00:00:00Z",
                100 + i,
            )
    return store


def names(store: BookStore) -> list[str]:
    return [book.name for book in store.books()]


async def post_purge(service: PurgeService, publisher: str, body: dict[str, Any]) -> httpx.Response:
    app = create_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/v1/publishers/{publisher}/books:purge", json=body)


async def get_operation(service: PurgeService, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_force_false_returns_count_and_sample_and_deletes_nothing() -> None:
    store = seeded_store()
    before = names(store)
    service = PurgeService(store, sample_max=3)
    reply = await post_purge(service, "acme", {"filter": BACKORDER_FILTER, "force": False})
    assert reply.status_code == 200
    assert reply.json() == {
        "done": True,
        "response": {
            "@type": "type.googleapis.com/library.example.v1.PurgeBooksResponse",
            "purgeCount": 20,
            "purgeSample": [
                "publishers/acme/books/b001",
                "publishers/acme/books/b002",
                "publishers/acme/books/b003",
            ],
        },
    }
    assert names(store) == before
    assert service.jobs == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_force_is_a_dry_run() -> None:
    store = seeded_store()
    service = PurgeService(store)
    reply = await post_purge(service, "acme", {"filter": BACKORDER_FILTER})
    assert reply.status_code == 200
    assert reply.json()["response"]["purgeCount"] == 20
    assert len(names(store)) == 50


@pytest.mark.unit
def test_sample_is_capped_but_count_is_exact() -> None:
    store = seeded_store(per_publisher=250)
    service = PurgeService(store)
    body = service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER))
    assert body["response"]["purgeCount"] == 200
    sample: list[str] = body["response"]["purgeSample"]
    assert len(sample) == 100
    assert sample == sorted(sample)
    assert sample[0] == "publishers/acme/books/b001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_force_true_hands_off_to_a_pending_operation() -> None:
    store = seeded_store()
    service = PurgeService(store, chunk_size=8)
    reply = await post_purge(service, "acme", {"filter": BACKORDER_FILTER, "force": True})
    assert reply.status_code == 202
    assert reply.headers["location"] == "/v1/operations/purge-1"
    assert reply.json() == {
        "name": "operations/purge-1",
        "done": False,
        "metadata": {
            "@type": "type.googleapis.com/library.example.v1.PurgeBooksMetadata",
            "deletedCount": 0,
        },
    }
    assert len(names(store)) == 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_steps_delete_in_bounded_chunks_until_done() -> None:
    store = seeded_store()
    service = PurgeService(store, chunk_size=8)
    service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER, force=True))
    chunks = [service.step("operations/purge-1") for _ in range(4)]
    assert chunks == [8, 8, 4, 0]
    reply = await get_operation(service, "/v1/operations/purge-1")
    assert reply.json() == {
        "name": "operations/purge-1",
        "done": True,
        "metadata": {
            "@type": "type.googleapis.com/library.example.v1.PurgeBooksMetadata",
            "deletedCount": 20,
        },
        "response": {
            "@type": "type.googleapis.com/library.example.v1.PurgeBooksResponse",
            "purgeCount": 20,
        },
    }
    remaining = names(store)
    assert [n for n in remaining if n.startswith("publishers/acme/")] == [
        f"publishers/acme/books/b{i:03d}" for i in (0, 5, 10, 15, 20)
    ]
    assert sum(n.startswith("publishers/globex/") for n in remaining) == 25


@pytest.mark.unit
def test_operation_is_not_done_while_matches_remain() -> None:
    store = seeded_store()
    service = PurgeService(store, chunk_size=10)
    service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER, force=True))
    assert service.step("operations/purge-1") == 10
    assert service.operation("operations/purge-1")["done"] is False
    assert "response" not in service.operation("operations/purge-1")
    assert service.step("operations/purge-1") == 10
    assert service.step("operations/purge-1") == 0
    assert service.operation("operations/purge-1")["done"] is True


@pytest.mark.unit
def test_books_created_after_acceptance_survive() -> None:
    store = seeded_store()
    service = PurgeService(store, chunk_size=1000)
    service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER, force=True))
    store.create("publishers/acme/books/z-late", "BACKORDER", "2026-02-01T00:00:00Z", 1)
    service.step("operations/purge-1")
    assert "publishers/acme/books/z-late" in names(store)
    assert service.operation("operations/purge-1")["response"]["purgeCount"] == 20


@pytest.mark.unit
def test_predicate_is_rechecked_at_delete_time() -> None:
    store = seeded_store()
    service = PurgeService(store, chunk_size=1000)
    service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER, force=True))
    store.set_availability("publishers/acme/books/b007", "IN_STOCK")
    service.step("operations/purge-1")
    assert "publishers/acme/books/b007" in names(store)
    assert service.operation("operations/purge-1")["response"]["purgeCount"] == 19


@pytest.mark.unit
def test_wildcard_purges_only_the_named_parent() -> None:
    store = seeded_store()
    service = PurgeService(store)
    service.purge("publishers/acme", PurgeBooksRequest(filter="*", force=True))
    service.step("operations/purge-1")
    assert all(n.startswith("publishers/globex/") for n in names(store))
    assert len(names(store)) == 25


@pytest.mark.unit
def test_dash_parent_spans_every_publisher() -> None:
    store = seeded_store()
    service = PurgeService(store)
    body = service.purge("publishers/-", PurgeBooksRequest(filter=BACKORDER_FILTER))
    assert body["response"]["purgeCount"] == 40


@pytest.mark.unit
def test_prefix_does_not_match_a_longer_publisher_id() -> None:
    store = seeded_store()
    store.create("publishers/acme2/books/b001", "BACKORDER", "2026-01-01T00:00:00Z", 1)
    service = PurgeService(store)
    body = service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER))
    assert body["response"]["purgeCount"] == 20


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filter_text", "fragment"),
    [
        ("", "filter is required"),
        ("   ", "filter is required"),
        ('availabilty = "BACKORDER"', "unknown field 'availabilty'"),
        ('availability = "SOLD"', "not a valid availability value"),
        ('availability < "BACKORDER"', "supports only = and !="),
        ('page_count = "ten"', "is int32"),
        ("create_time < 2026", "needs a double-quoted value"),
        ('create_time < "yesterday"', "not an RFC 3339 timestamp"),
        ('create_time < "2026-01-05T00:00:00"', "must carry an offset"),
        ('availability = "BACKORDER" OR page_count > 1', "cannot parse filter"),
        ('availability = "BACKORDER" page_count > 1', "missing AND"),
        ('AND availability = "BACKORDER"', "unexpected AND"),
        ('availability = "BACKORDER" AND', "ends with AND"),
    ],
    ids=[
        "empty",
        "blank",
        "unknown-field",
        "bad-enum",
        "enum-ordering",
        "int-as-string",
        "unquoted-timestamp",
        "bad-timestamp",
        "naive-timestamp",
        "unsupported-or",
        "missing-and",
        "leading-and",
        "trailing-and",
    ],
)
async def test_invalid_filters_are_rejected_before_any_deletion(
    filter_text: str, fragment: str
) -> None:
    store = seeded_store()
    service = PurgeService(store)
    reply = await post_purge(service, "acme", {"filter": filter_text, "force": True})
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    assert error["code"] == 400
    assert fragment in error["message"]
    assert len(names(store)) == 50
    assert service.jobs == {}


@pytest.mark.unit
def test_typed_comparisons_select_the_right_books() -> None:
    store = seeded_store()
    service = PurgeService(store)
    early = (
        'create_time < "2026-01-05T00:00:00Z" AND page_count >= 101 AND availability != "IN_STOCK"'
    )
    body = service.purge("publishers/acme", PurgeBooksRequest(filter=early))
    assert body["response"]["purgeSample"] == [
        "publishers/acme/books/b001",
        "publishers/acme/books/b002",
        "publishers/acme/books/b003",
    ]
    late = 'create_time > "2026-01-23T00:00:00Z" AND page_count <= 123'
    body = service.purge("publishers/acme", PurgeBooksRequest(filter=late))
    assert body["response"]["purgeSample"] == ["publishers/acme/books/b023"]


@pytest.mark.unit
def test_filter_size_limits_are_enforced() -> None:
    at_limit = "page_count > 1".ljust(MAX_FILTER_LENGTH)
    assert len(parse_filter(at_limit).conditions) == 1
    with pytest.raises(ApiError, match=f"exceeds {MAX_FILTER_LENGTH} characters"):
        parse_filter(at_limit + " ")
    many = " AND ".join(["page_count > 1"] * (MAX_CONDITIONS + 1))
    with pytest.raises(ApiError, match=f"maximum is {MAX_CONDITIONS}"):
        parse_filter(many)
    allowed = " AND ".join(["page_count > 1"] * MAX_CONDITIONS)
    assert len(parse_filter(allowed).conditions) == MAX_CONDITIONS


@pytest.mark.unit
def test_ordering_on_an_enum_condition_raises() -> None:
    store = seeded_store(per_publisher=1)
    book = store.books()[0]
    with pytest.raises(ApiError, match="does not apply"):
        Condition("availability", "<", "IN_STOCK").matches(book)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"filter": BACKORDER_FILTER, "force": "true"}, "force"),
        ({"filter": BACKORDER_FILTER, "force": 1}, "force"),
        ({"filter": BACKORDER_FILTER, "forced": True}, "forced"),
        ({"force": True}, "filter"),
        ({"filter": BACKORDER_FILTER, "force": True, "requestId": "r" * 65}, "requestId"),
    ],
    ids=["string-true", "int-one", "typo-field", "missing-filter", "long-request-id"],
)
async def test_ambiguous_request_bodies_are_rejected(body: dict[str, Any], field: str) -> None:
    store = seeded_store()
    service = PurgeService(store)
    reply = await post_purge(service, "acme", body)
    assert reply.status_code == 400
    error = reply.json()["error"]
    assert error["status"] == "INVALID_ARGUMENT"
    assert repr(field) in error["message"]
    assert service.jobs == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_parent_is_rejected() -> None:
    service = PurgeService(seeded_store())
    reply = await post_purge(service, "Acme_Corp", {"filter": "*"})
    assert reply.status_code == 400
    assert "must be publishers/{publisher} or publishers/-" in reply.json()["error"]["message"]


@pytest.mark.unit
def test_request_id_replays_the_same_operation() -> None:
    store = seeded_store()
    service = PurgeService(store)
    request = PurgeBooksRequest.model_validate(
        {"filter": BACKORDER_FILTER, "force": True, "requestId": "3f1c0d4e"}
    )
    first = service.purge("publishers/acme", request)
    second = service.purge("publishers/acme", request)
    assert first["name"] == second["name"] == "operations/purge-1"
    assert list(service.jobs) == ["operations/purge-1"]
    third = service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER, force=True))
    assert third["name"] == "operations/purge-2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_operation_is_not_found() -> None:
    reply = await get_operation(PurgeService(seeded_store()), "/v1/operations/purge-9")
    assert reply.status_code == 404
    assert reply.json()["error"]["status"] == "NOT_FOUND"


@pytest.mark.unit
def test_step_after_done_deletes_nothing_new() -> None:
    store = seeded_store()
    service = PurgeService(store)
    service.purge("publishers/acme", PurgeBooksRequest(filter=BACKORDER_FILTER, force=True))
    assert service.step("operations/purge-1") == 20
    store.create("publishers/acme/books/b999", "BACKORDER", "2026-01-01T00:00:00Z", 1)
    assert service.step("operations/purge-1") == 0
    assert "publishers/acme/books/b999" in names(store)
