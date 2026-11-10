"""If-Match turns a silent lost update into 412, and 428 forces clients to send it."""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from api_labs.f03_http_caching_and_conditional_requests.if_match_preconditions import (
    PROBLEM_JSON,
    DocumentStore,
    EntityTag,
    MalformedPreconditionError,
    Strategy,
    Wildcard,
    WriteFailure,
    build_app,
    parse_if_match,
)

ORIGINAL = b'{"title": "Draft", "body": "v1 text"}'


@pytest.fixture
def store() -> Iterator[DocumentStore]:
    documents = DocumentStore()
    documents.create(ORIGINAL)
    yield documents
    documents.close()


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    # FastAPI 0.162.0 defaults to strict_content_type=True: no JSON media type, no JSON parse.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Content-Type": "application/json"},
    ) as client:
        yield client


def edit(content: bytes, **changes: str) -> bytes:
    """A client-side edit: change some fields of the representation it last read."""
    return json.dumps({**json.loads(content), **changes}).encode()


@pytest.mark.unit
def test_parse_if_match_tokenizes_quoted_commas_and_joins_field_lines() -> None:
    assert parse_if_match(['"a,b" , ,W/"c"', '"d"']) == (
        EntityTag("a,b"),
        EntityTag("c", weak=True),
        EntityTag("d"),
    )
    assert parse_if_match([" * "]) is Wildcard.ANY


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_lines",
    [['"a", *'], ["*", "*"], ['w/"a"'], ['"a" "b"'], ["unquoted"], ['"a"'] * 17],
)
def test_parse_if_match_rejects_values_outside_the_grammar(field_lines: list[str]) -> None:
    with pytest.raises(MalformedPreconditionError):
        parse_if_match(field_lines)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconditional_put_silently_loses_the_first_update(store: DocumentStore) -> None:
    async with client_for(build_app(store, require_if_match=False)) as client:
        alice_view = (await client.get("/documents/1")).content
        bob_view = (await client.get("/documents/1")).content
        alice = await client.put("/documents/1", content=edit(alice_view, title="Final"))
        bob = await client.put("/documents/1", content=edit(bob_view, body="v2 text"))
        final = (await client.get("/documents/1")).json()

    assert (alice.status_code, bob.status_code) == (204, 204)
    assert final == {"title": "Draft", "body": "v2 text"}  # Alice's title is gone.


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_turns_the_lost_update_into_412_and_a_clean_retry(
    store: DocumentStore,
) -> None:
    async with client_for(build_app(store, require_if_match=True)) as client:
        alice_get = await client.get("/documents/1")
        bob_get = await client.get("/documents/1")
        alice = await client.put(
            "/documents/1",
            content=edit(alice_get.content, title="Final"),
            headers={"If-Match": alice_get.headers["etag"]},
        )
        bob = await client.put(
            "/documents/1",
            content=edit(bob_get.content, body="v2 text"),
            headers={"If-Match": bob_get.headers["etag"]},
        )
        bob_reread = await client.get("/documents/1")
        bob_retry = await client.put(
            "/documents/1",
            content=edit(bob_reread.content, body="v2 text"),
            headers={"If-Match": bob_reread.headers["etag"]},
        )
        final = await client.get("/documents/1")

    assert alice_get.headers["etag"] == '"v1"'
    assert (alice.status_code, alice.headers["etag"]) == (204, '"v2"')
    assert bob.status_code == 412
    assert bob.headers["content-type"] == PROBLEM_JSON
    assert "etag" not in bob.headers
    assert (bob_retry.status_code, bob_retry.headers["etag"]) == (204, '"v3"')
    assert final.json() == {"title": "Final", "body": "v2 text"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_if_match_returns_428_with_resubmission_instructions(
    store: DocumentStore,
) -> None:
    async with client_for(build_app(store, require_if_match=True)) as client:
        response = await client.put("/documents/1", content=edit(ORIGINAL, title="Blind"))
        after = await client.get("/documents/1")

    assert response.status_code == 428
    assert response.headers["content-type"] == PROBLEM_JSON
    assert "If-Match" in response.json()["detail"]
    assert "etag" not in response.headers
    assert (after.content, after.headers["etag"]) == (ORIGINAL, '"v1"')


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("if_match", ['W/"v1"', '"v01"', '"V1"', '"v' + "9" * 5000 + '"'])
async def test_if_match_uses_octet_exact_strong_comparison(
    store: DocumentStore, if_match: str
) -> None:
    async with client_for(build_app(store, require_if_match=True)) as client:
        response = await client.put(
            "/documents/1", content=edit(ORIGINAL, title="X"), headers={"If-Match": if_match}
        )

    assert response.status_code == 412


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_field_lines_are_combined_before_evaluation(store: DocumentStore) -> None:
    async with client_for(build_app(store, require_if_match=True)) as client:
        response = await client.put(
            "/documents/1",
            content=edit(ORIGINAL, title="X"),
            headers=[("If-Match", '"v7"'), ("If-Match", '"v1"')],
        )

    assert (response.status_code, response.headers["etag"]) == (204, '"v2"')


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wildcard_matches_any_current_version_so_it_cannot_prevent_lost_updates(
    store: DocumentStore,
) -> None:
    async with client_for(build_app(store, require_if_match=True)) as client:
        first = await client.put(
            "/documents/1", content=edit(ORIGINAL, title="A"), headers={"If-Match": '"v1"'}
        )
        blind = await client.put(
            "/documents/1", content=edit(ORIGINAL, title="B"), headers={"If-Match": "*"}
        )
        missing = await client.put(
            "/documents/9", content=edit(ORIGINAL, title="C"), headers={"If-Match": "*"}
        )

    assert (first.status_code, blind.status_code, blind.headers["etag"]) == (204, 204, '"v3"')
    assert missing.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_404_outranks_a_failed_precondition(store: DocumentStore) -> None:
    async with client_for(build_app(store, require_if_match=True)) as client:
        response = await client.put(
            "/documents/9", content=edit(ORIGINAL, title="X"), headers={"If-Match": '"v1"'}
        )

    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("if_match", "content", "expected"),
    [
        (None, b'{"title": ""}', 428),
        (None, b'{"title": ', 422),
        ('"v0"', b'{"title": ""}', 412),
        ('"v1"', b'{"title": ""}', 422),
    ],
)
async def test_fastapi_order_json_decode_then_dependencies_then_body_schema(
    store: DocumentStore, if_match: str | None, content: bytes, expected: int
) -> None:
    headers = {"Content-Type": "application/json"}
    if if_match is not None:
        headers["If-Match"] = if_match
    async with client_for(build_app(store, require_if_match=True)) as client:
        response = await client.put("/documents/1", content=content, headers=headers)

    assert response.status_code == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_etag_describes_the_stored_bytes_exactly(store: DocumentStore) -> None:
    sent = b'{ "body" : "spaced",   "title": "Kept verbatim" }'
    async with client_for(build_app(store, require_if_match=True)) as client:
        put = await client.put("/documents/1", content=sent, headers={"If-Match": '"v1"'})
        get = await client.get("/documents/1")

    assert put.headers["etag"] == get.headers["etag"] == '"v2"'
    assert get.content == sent


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_updated_rows_are_split_into_missing_and_stale(store: DocumentStore) -> None:
    # Called directly, as when a row is deleted between the dependency check and the write.
    assert await store.conditional_update(9, frozenset({1}), ORIGINAL) is WriteFailure.MISSING
    assert await store.conditional_update(1, frozenset({7}), ORIGINAL) is WriteFailure.STALE
    assert await store.conditional_update(1, frozenset({1}), ORIGINAL) == 2


async def race_two_writers(strategy: Strategy) -> tuple[list[int], dict[str, str], str]:
    """Both writers hold ETag "v1"; a barrier parks each one between its check and its write."""
    barrier = asyncio.Barrier(2)

    async def pause() -> None:
        await barrier.wait()

    documents = DocumentStore(pause=pause)
    documents.create(ORIGINAL)
    try:
        app = build_app(documents, require_if_match=True, strategy=strategy)
        async with client_for(app) as client:
            responses = await asyncio.gather(
                client.put(
                    "/documents/1",
                    content=edit(ORIGINAL, title="Alice"),
                    headers={"If-Match": '"v1"'},
                ),
                client.put(
                    "/documents/1",
                    content=edit(ORIGINAL, body="Bob"),
                    headers={"If-Match": '"v1"'},
                ),
            )
            final = await client.get("/documents/1")
    finally:
        documents.close()
    return sorted(r.status_code for r in responses), final.json(), final.headers["etag"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_then_write_loses_an_update_even_with_if_match() -> None:
    statuses, final, etag = await race_two_writers("check_then_write")

    assert statuses == [204, 204]  # Both callers were told their write succeeded.
    assert etag == '"v3"'
    assert final in ({"title": "Alice", "body": "v1 text"}, {"title": "Draft", "body": "Bob"})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conditional_update_statement_admits_exactly_one_writer() -> None:
    statuses, final, etag = await race_two_writers("conditional_update")

    assert statuses == [204, 412]
    assert etag == '"v2"'
    assert final in ({"title": "Alice", "body": "v1 text"}, {"title": "Draft", "body": "Bob"})
