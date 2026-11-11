"""Lab for Resource-Revisions-and-Historical-Snapshots-AIP-162."""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from api_labs.f04_rest_and_resource_modeling.resource_revisions import (
    DEFAULT_PAGE_SIZE,
    MAX_ID_ATTEMPTS,
    MAX_PAGE_SIZE,
    PAGE_TOKEN_TTL_SECONDS,
    Book,
    RevisionStore,
    build_app,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

TOKEN_KEY = base64.urlsafe_b64encode(bytes(32))
BOOK = {"title": "Les Miserables", "author": "Hugo", "tags": ["novel"]}


def sequential_ids(start: int = 1) -> Callable[[], str]:
    counter = count(start)
    return lambda: f"{next(counter):08x}"


def scripted_ids(ids: list[str]) -> Callable[[], str]:
    it: Iterator[str] = iter(ids)
    return lambda: next(it)


def ticking_clock() -> Callable[[], datetime]:
    base = datetime(2026, 11, 10, tzinfo=UTC)
    ticks = count()
    return lambda: base + timedelta(seconds=next(ticks))


def make_client(new_revision_id: Callable[[], str] | None = None) -> httpx.AsyncClient:
    store = RevisionStore(
        token_key=TOKEN_KEY,
        new_revision_id=new_revision_id or sequential_ids(),
        clock=ticking_clock(),
    )
    transport = httpx.ASGITransport(app=build_app(store))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def create(client: httpx.AsyncClient, book_id: str = "les-miserables") -> httpx.Response:
    response = await client.post("/v1/books", params={"book_id": book_id}, json=BOOK)
    assert response.status_code == 201, response.text
    return response


def ids_of(body: dict[str, Any]) -> list[str]:
    return [r["name"].rsplit("/", 1)[1] for r in body["revisions"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_records_first_revision_and_latest_alias() -> None:
    async with make_client() as client:
        created = await create(client)
        assert created.headers["etag"] == '"00000001"'
        assert created.headers["location"] == "/v1/books/les-miserables"
        latest = (await client.get("/v1/books/les-miserables/revisions/latest")).json()
    assert latest == {
        "name": "books/les-miserables/revisions/00000001",
        "snapshot": {"name": "books/les-miserables", **BOOK},
        "create_time": "2026-11-10T00:00:00Z",
        "alternate_ids": ["latest"],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_appends_revision_and_old_snapshot_is_unchanged() -> None:
    async with make_client() as client:
        await create(client)
        patched = await client.patch("/v1/books/les-miserables", json={"title": "Les Mis"})
        assert patched.headers["etag"] == '"00000002"'
        first = (await client.get("/v1/books/les-miserables/revisions/00000001")).json()
        second = (await client.get("/v1/books/les-miserables/revisions/00000002")).json()
        latest = (await client.get("/v1/books/les-miserables/revisions/latest")).json()
    assert latest["name"] == "books/les-miserables/revisions/00000002"
    assert first["snapshot"]["title"] == "Les Miserables"
    assert first["alternate_ids"] == []
    assert second["snapshot"]["title"] == "Les Mis"
    assert second["alternate_ids"] == ["latest"]


@pytest.mark.unit
def test_snapshot_model_is_frozen() -> None:
    book = Book(title="t", author="a")
    with pytest.raises(ValidationError, match="frozen"):
        book.title = "changed"  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_noop_update_does_not_create_revision() -> None:
    async with make_client() as client:
        await create(client)
        patched = await client.patch("/v1/books/les-miserables", json={"author": "Hugo"})
        listed = (await client.get("/v1/books/les-miserables/revisions")).json()
    assert patched.headers["etag"] == '"00000001"'
    assert ids_of(listed) == ["00000001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invalid_patch_is_rejected_before_it_becomes_a_revision() -> None:
    async with make_client() as client:
        await create(client)
        patched = await client.patch("/v1/books/les-miserables", json={"title": None})
        listed = (await client.get("/v1/books/les-miserables/revisions")).json()
    assert patched.status_code == 400
    assert patched.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert ids_of(listed) == ["00000001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_defaults_to_reverse_chronological_order() -> None:
    async with make_client() as client:
        await create(client)
        for n in range(3):
            await client.patch("/v1/books/les-miserables", json={"title": f"t{n}"})
        listed = (await client.get("/v1/books/les-miserables/revisions")).json()
    assert ids_of(listed) == ["00000004", "00000003", "00000002", "00000001"]
    times = [r["create_time"] for r in listed["revisions"]]
    assert times == sorted(times, reverse=True)
    assert "next_page_token" not in listed


@pytest.mark.unit
@pytest.mark.asyncio
async def test_keyset_pages_do_not_repeat_when_revisions_arrive_mid_scan() -> None:
    async with make_client() as client:
        await create(client)
        for n in range(4):
            await client.patch("/v1/books/les-miserables", json={"title": f"t{n}"})
        page1 = (await client.get("/v1/books/les-miserables/revisions?page_size=2")).json()
        # Two writes land between page requests; an offset cursor would now repeat 00000004.
        for n in range(2):
            await client.patch("/v1/books/les-miserables", json={"title": f"late{n}"})
        token = page1["next_page_token"]
        page2 = (
            await client.get(
                "/v1/books/les-miserables/revisions",
                params={"page_size": 2, "page_token": token},
            )
        ).json()
        token = page2["next_page_token"]
        page3 = (
            await client.get(
                "/v1/books/les-miserables/revisions",
                params={"page_size": 2, "page_token": token},
            )
        ).json()
    assert ids_of(page1) == ["00000005", "00000004"]
    assert ids_of(page2) == ["00000003", "00000002"]
    assert ids_of(page3) == ["00000001"]
    assert "next_page_token" not in page3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_page_size_zero_defaults_large_coerces_negative_rejects() -> None:
    async with make_client() as client:
        await create(client)
        assert (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE) == (50, 1000)
        for n in range(1000):
            await client.patch("/v1/books/les-miserables", json={"title": f"t{n}"})
        zero = (await client.get("/v1/books/les-miserables/revisions?page_size=0")).json()
        huge = (await client.get("/v1/books/les-miserables/revisions?page_size=5000")).json()
        negative = await client.get("/v1/books/les-miserables/revisions?page_size=-1")
    assert len(zero["revisions"]) == 50
    assert len(huge["revisions"]) == 1000
    assert "next_page_token" in huge
    assert negative.status_code == 400
    assert negative.json()["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_page_token_is_bound_to_its_collection_and_tamper_evident() -> None:
    async with make_client() as client:
        await create(client, "alpha")
        await create(client, "beta")
        await client.patch("/v1/books/alpha", json={"title": "t"})
        page = (await client.get("/v1/books/alpha/revisions?page_size=1")).json()
        token: str = page["next_page_token"]
        assert "alpha" not in base64.urlsafe_b64decode(token).decode("latin-1")
        cross = await client.get("/v1/books/beta/revisions", params={"page_token": token})
        flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
        tampered = await client.get("/v1/books/alpha/revisions", params={"page_token": flipped})
        # Correctly keyed tokens minted outside the TTL window or beyond the clock skew.
        now = int(time.time())
        fernet = Fernet(TOKEN_KEY)
        stale = fernet.encrypt_at_time(b"alpha\n2", now - PAGE_TOKEN_TTL_SECONDS - 1).decode()
        future = fernet.encrypt_at_time(b"alpha\n2", now + 120).decode()
        expired = await client.get("/v1/books/alpha/revisions", params={"page_token": stale})
        early = await client.get("/v1/books/alpha/revisions", params={"page_token": future})
    assert cross.status_code == 400
    assert cross.json()["error"]["message"] == "page_token was issued for a different collection"
    assert tampered.status_code == 400
    assert tampered.json()["error"]["message"] == "invalid page_token"
    assert expired.status_code == 400
    assert expired.json()["error"]["message"] == "invalid page_token"
    assert early.status_code == 400
    assert early.json()["error"]["message"] == "invalid page_token"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_by_alias_returns_canonical_name() -> None:
    async with make_client() as client:
        await create(client)
        aliased = await client.post(
            "/v1/books/les-miserables/revisions/00000001:alias", json={"alias_id": "published"}
        )
        await client.patch("/v1/books/les-miserables", json={"title": "draft"})
        fetched = (await client.get("/v1/books/les-miserables/revisions/published")).json()
    assert aliased.status_code == 200
    assert fetched["name"] == "books/les-miserables/revisions/00000001"
    assert fetched["alternate_ids"] == ["published"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rollback_appends_new_revision_with_target_snapshot() -> None:
    async with make_client() as client:
        await create(client)
        await client.patch("/v1/books/les-miserables", json={"title": "bad edit"})
        rolled = await client.post("/v1/books/les-miserables/revisions/00000001:rollback", json={})
        current = (await client.get("/v1/books/les-miserables")).json()
        listed = (await client.get("/v1/books/les-miserables/revisions")).json()
    body = rolled.json()
    assert rolled.status_code == 200
    assert body["name"] == "books/les-miserables/revisions/00000003"
    assert body["snapshot"] == {"name": "books/les-miserables", **BOOK}
    assert body["alternate_ids"] == ["latest"]
    assert current == {"name": "books/les-miserables", **BOOK}
    assert ids_of(listed) == ["00000003", "00000002", "00000001"]
    assert listed["revisions"][1]["snapshot"]["title"] == "bad edit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_rollbacks_with_same_if_match_exactly_one_wins() -> None:
    async with make_client() as client:
        await create(client)
        patched = await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        etag = patched.headers["etag"]
        url = "/v1/books/les-miserables/revisions/00000001:rollback"
        responses = await asyncio.gather(
            client.post(url, json={}, headers={"If-Match": etag}),
            client.post(url, json={}, headers={"If-Match": etag}),
        )
        listed = (await client.get("/v1/books/les-miserables/revisions")).json()
    assert sorted(r.status_code for r in responses) == [200, 412]
    assert ids_of(listed) == ["00000003", "00000002", "00000001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_uses_strong_comparison() -> None:
    async with make_client() as client:
        await create(client)
        url = "/v1/books/les-miserables"
        weak = await client.patch(url, json={"title": "x"}, headers={"If-Match": 'W/"00000001"'})
        strong = await client.patch(
            url, json={"title": "x"}, headers={"If-Match": '"zzz", "00000001"'}
        )
        star = await client.patch(url, json={"title": "y"}, headers={"If-Match": "*"})
    assert weak.status_code == 412
    assert weak.json()["error"]["status"] == "FAILED_PRECONDITION"
    assert strong.status_code == 200
    assert star.status_code == 200
    assert star.headers["etag"] == '"00000003"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_realias_moves_alias_to_new_revision() -> None:
    async with make_client() as client:
        await create(client)
        await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        base = "/v1/books/les-miserables/revisions"
        await client.post(f"{base}/00000001:alias", json={"alias_id": "published"})
        again = await client.post(f"{base}/00000002:alias", json={"alias_id": "published"})
        fetched = (await client.get(f"{base}/published")).json()
        old = (await client.get(f"{base}/00000001")).json()
    assert again.status_code == 200
    assert fetched["name"].endswith("/00000002")
    assert old["alternate_ids"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_latest_alias_is_read_only() -> None:
    async with make_client() as client:
        await create(client)
        base = "/v1/books/les-miserables/revisions"
        assign = await client.post(f"{base}/00000001:alias", json={"alias_id": "latest"})
        delete = await client.delete(f"{base}/latest")
    assert assign.status_code == 400
    assert delete.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_alias_removes_alias_not_revision() -> None:
    async with make_client() as client:
        await create(client)
        await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        base = "/v1/books/les-miserables/revisions"
        await client.post(f"{base}/00000001:alias", json={"alias_id": "published"})
        deleted = await client.delete(f"{base}/published")
        gone = await client.get(f"{base}/published")
        kept = await client.get(f"{base}/00000001")
    assert deleted.status_code == 204
    assert gone.status_code == 404
    assert kept.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cannot_delete_the_only_revision_but_can_delete_older_ones() -> None:
    async with make_client() as client:
        await create(client)
        base = "/v1/books/les-miserables/revisions"
        only = await client.delete(f"{base}/00000001")
        await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        older = await client.delete(f"{base}/00000001")
        listed = (await client.get(base)).json()
    assert only.status_code == 400
    assert only.json()["error"]["status"] == "FAILED_PRECONDITION"
    assert older.status_code == 204
    assert ids_of(listed) == ["00000002"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revision_id_collision_is_retried() -> None:
    ids = ["c7cfa2a8", "c7cfa2a8", "0badf00d", "deadbeef", "feedface"]
    async with make_client(scripted_ids(ids)) as client:
        await create(client)
        await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        # A generated ID equal to an existing alias is also retried, so it cannot shadow it.
        base = "/v1/books/les-miserables/revisions"
        await client.post(f"{base}/c7cfa2a8:alias", json={"alias_id": "deadbeef"})
        await client.patch("/v1/books/les-miserables", json={"title": "v3"})
        listed = (await client.get(base)).json()
        aliased = (await client.get(f"{base}/deadbeef")).json()
    assert ids_of(listed) == ["feedface", "0badf00d", "c7cfa2a8"]
    assert aliased["name"] == "books/les-miserables/revisions/c7cfa2a8"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deleting_a_revision_drops_its_aliases_for_good() -> None:
    ids = ["c7cfa2a8", "d00dfeed", "c7cfa2a8"]
    async with make_client(scripted_ids(ids)) as client:
        await create(client)
        await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        base = "/v1/books/les-miserables/revisions"
        await client.post(f"{base}/c7cfa2a8:alias", json={"alias_id": "published"})
        deleted = await client.delete(f"{base}/c7cfa2a8")
        # A later revision may reuse the deleted ID; the old alias must not resurrect onto it.
        await client.patch("/v1/books/les-miserables", json={"title": "v3"})
        fetched = await client.get(f"{base}/published")
        again = await client.delete(f"{base}/published")
    assert deleted.status_code == 204
    assert fetched.status_code == 404
    assert again.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_id_allocation_gives_up_with_500_and_appends_nothing() -> None:
    ids = ["c7cfa2a8"] * (1 + MAX_ID_ATTEMPTS)
    async with make_client(scripted_ids(ids)) as client:
        await create(client)
        failed = await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        listed = (await client.get("/v1/books/les-miserables/revisions")).json()
        current = (await client.get("/v1/books/les-miserables")).json()
    assert failed.status_code == 500
    assert failed.json()["error"]["status"] == "INTERNAL"
    assert ids_of(listed) == ["c7cfa2a8"]
    assert current["title"] == "Les Miserables"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_alias_cannot_shadow_a_revision_id() -> None:
    async with make_client(scripted_ids(["c7cfa2a8", "d00dfeed"])) as client:
        await create(client)
        await client.patch("/v1/books/les-miserables", json={"title": "v2"})
        base = "/v1/books/les-miserables/revisions"
        shadow = await client.post(f"{base}/d00dfeed:alias", json={"alias_id": "c7cfa2a8"})
        still = (await client.get(f"{base}/c7cfa2a8")).json()
    assert shadow.status_code == 409
    assert shadow.json()["error"]["status"] == "ALREADY_EXISTS"
    assert still["snapshot"]["title"] == "Les Miserables"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_at_syntax_is_rejected_with_pointer_to_subcollection() -> None:
    async with make_client() as client:
        await create(client)
        legacy = await client.get("/v1/books/les-miserables@00000001")
        verb_on_get = await client.get("/v1/books/les-miserables/revisions/00000001:rollback")
    assert legacy.status_code == 400
    assert "books/{book}/revisions/{revision}" in legacy.json()["error"]["message"]
    assert verb_on_get.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revision_of_another_book_is_not_found() -> None:
    async with make_client() as client:
        await create(client, "alpha")
        await create(client, "beta")
        cross = await client.get("/v1/books/beta/revisions/00000001")
        cross_rollback = await client.post("/v1/books/beta/revisions/00000001:rollback", json={})
    assert cross.status_code == 404
    assert cross_rollback.status_code == 404
