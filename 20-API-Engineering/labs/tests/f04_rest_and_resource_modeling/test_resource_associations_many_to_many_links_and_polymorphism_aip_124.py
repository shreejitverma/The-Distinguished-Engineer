"""Many-to-many links, canonical names, and polymorphic resources (AIP-124, AIP-144, AIP-159)."""

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from api_labs.f04_rest_and_resource_modeling.resource_associations import (
    LEGACY_UNION,
    MAX_AUTHORS,
    TAGGED_BOOK,
    UNTAGGED_BOOK,
    Catalog,
    EBook,
    LegacyEBook,
    LegacyPrint,
    LinkSemantics,
    create_app,
)

AUTHORS = {"authors/ada", "authors/grace", "authors/linus"}
EBOOK: dict[str, Any] = {"kind": "ebook", "title": "Specs", "file_bytes": 4096}


def _client(catalog: Catalog, semantics: LinkSemantics = "aip144") -> httpx.AsyncClient:
    app = create_app(catalog, link_semantics=semantics)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _seed(client: httpx.AsyncClient, publisher: str, book: str, authors: list[str]) -> None:
    response = await client.post(
        f"/v1/publishers/{publisher}/books",
        params={"book_id": book},
        json={**EBOOK, "authors": authors},
    )
    assert response.status_code == 201, response.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_returns_201_with_canonical_location() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        response = await client.post(
            "/v1/publishers/acme/books",
            params={"book_id": "specs"},
            json={**EBOOK, "authors": ["authors/ada"]},
        )
    assert response.status_code == 201
    assert response.headers["location"] == "/v1/publishers/acme/books/specs"
    assert response.json() == {
        "name": "publishers/acme/books/specs",
        "etag": '"1"',
        "title": "Specs",
        "authors": ["authors/ada"],
        "kind": "ebook",
        "file_bytes": 4096,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whole_list_replace_without_etag_loses_a_concurrent_update() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", ["authors/ada"])
        url = "/v1/publishers/acme/books/specs"
        alice = (await client.get(url)).json()["authors"]
        bob = (await client.get(url)).json()["authors"]
        await client.patch(url, json={"authors": [*alice, "authors/grace"]})
        last = await client.patch(url, json={"authors": [*bob, "authors/linus"]})
    assert last.status_code == 200
    assert last.json()["authors"] == ["authors/ada", "authors/linus"]  # grace is silently lost


@pytest.mark.unit
@pytest.mark.asyncio
async def test_whole_list_replace_with_stale_etag_is_409_aborted() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", ["authors/ada"])
        url = "/v1/publishers/acme/books/specs"
        alice = (await client.get(url)).json()
        bob = (await client.get(url)).json()
        first = await client.patch(
            url, json={"authors": [*alice["authors"], "authors/grace"], "etag": alice["etag"]}
        )
        second = await client.patch(
            url, json={"authors": [*bob["authors"], "authors/linus"], "etag": bob["etag"]}
        )
        final = (await client.get(url)).json()
    assert first.status_code == 200
    assert first.json()["etag"] == '"2"'
    assert second.status_code == 409
    assert second.json()["error"]["status"] == "ABORTED"
    assert final["authors"] == ["authors/ada", "authors/grace"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_add_author_custom_methods_compose_without_etags() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", ["authors/ada"])
        url = "/v1/publishers/acme/books/specs:addAuthor"
        first = await client.post(url, json={"author": "authors/grace"})
        second = await client.post(url, json={"author": "authors/linus"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["authors"] == ["authors/ada", "authors/grace", "authors/linus"]
    assert second.json()["etag"] == '"3"'


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("semantics", "add_status", "remove_status"),
    [("aip144", 409, 404), ("jsonapi", 200, 200)],
    ids=["aip144-strict", "jsonapi-idempotent"],
)
async def test_retried_add_and_remove_depend_on_link_semantics(
    semantics: LinkSemantics, add_status: int, remove_status: int
) -> None:
    async with _client(Catalog(authors=set(AUTHORS)), semantics) as client:
        await _seed(client, "acme", "specs", ["authors/ada"])
        base = "/v1/publishers/acme/books/specs"
        await client.post(f"{base}:addAuthor", json={"author": "authors/grace"})
        retried_add = await client.post(f"{base}:addAuthor", json={"author": "authors/grace"})
        await client.post(f"{base}:removeAuthor", json={"author": "authors/grace"})
        retried_remove = await client.post(f"{base}:removeAuthor", json={"author": "authors/grace"})
        final = (await client.get(base)).json()
    assert retried_add.status_code == add_status
    assert retried_remove.status_code == remove_status
    assert final["authors"] == ["authors/ada"]
    assert final["etag"] == '"3"'  # two real mutations; a no-op retry must not bump the revision


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_field_is_bounded_unique_and_well_formed() -> None:
    catalog = Catalog(authors={f"authors/a{i}" for i in range(MAX_AUTHORS + 1)})
    async with _client(catalog) as client:
        too_many = await client.post(
            "/v1/publishers/acme/books",
            params={"book_id": "big"},
            json={**EBOOK, "authors": [f"authors/a{i}" for i in range(MAX_AUTHORS + 1)]},
        )
        duplicate = await client.post(
            "/v1/publishers/acme/books",
            params={"book_id": "dup"},
            json={**EBOOK, "authors": ["authors/a1", "authors/a1"]},
        )
        bad_name = await client.post(
            "/v1/publishers/acme/books",
            params={"book_id": "bad"},
            json={**EBOOK, "authors": ["users/a1"]},
        )
        trailing_hyphen = await client.post(
            "/v1/publishers/acme/books", params={"book_id": "bad-"}, json=EBOOK
        )
        await _seed(client, "acme", "full", [f"authors/a{i}" for i in range(MAX_AUTHORS)])
        overflow = await client.post(
            "/v1/publishers/acme/books/full:addAuthor", json={"author": f"authors/a{MAX_AUTHORS}"}
        )
    assert MAX_AUTHORS == 100
    assert too_many.status_code == 422
    assert too_many.json()["detail"][0]["type"] == "too_long"
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"][0]["type"] == "value_error"
    assert bad_name.status_code == 422
    assert bad_name.json()["detail"][0]["type"] == "string_pattern_mismatch"
    assert trailing_hyphen.status_code == 422
    assert overflow.status_code == 400
    assert overflow.json()["error"]["status"] == "FAILED_PRECONDITION"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dangling_reference_rejected_on_write_but_survives_target_delete() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        dangling = await client.post(
            "/v1/publishers/acme/books",
            params={"book_id": "ghost"},
            json={**EBOOK, "authors": ["authors/nobody"]},
        )
        await _seed(client, "acme", "specs", ["authors/ada"])
        deleted = await client.delete("/v1/authors/ada")
        after = (await client.get("/v1/publishers/acme/books/specs")).json()
        re_add = await client.post(
            "/v1/publishers/acme/books/specs:addAuthor", json={"author": "authors/ada"}
        )
    assert dangling.status_code == 400
    assert dangling.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert deleted.status_code == 204
    assert after["authors"] == ["authors/ada"]  # weak reference: the name outlives its target
    assert re_add.status_code == 409  # the stale name still occupies the slot


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reverse_lookup_is_a_filter_across_parents_with_canonical_names() -> None:
    async with _client(Catalog(authors={*AUTHORS, "authors/adam"})) as client:
        await _seed(client, "acme", "specs", ["authors/ada", "authors/grace"])
        await _seed(client, "zeta", "notes", ["authors/ada"])
        await _seed(client, "zeta", "other", ["authors/linus"])
        await _seed(client, "zeta", "lookalike", ["authors/adam"])  # "authors/ada" is a substring
        response = await client.get(
            "/v1/publishers/-/books", params={"filter": 'authors:"authors/ada"'}
        )
        scoped = await client.get(
            "/v1/publishers/zeta/books", params={"filter": 'authors:"authors/ada"'}
        )
        unsupported = await client.get("/v1/publishers/-/books", params={"filter": 'title="Specs"'})
    assert response.status_code == 200
    assert [b["name"] for b in response.json()["books"]] == [
        "publishers/acme/books/specs",
        "publishers/zeta/books/notes",
    ]
    assert [b["name"] for b in scoped.json()["books"]] == ["publishers/zeta/books/notes"]
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wildcard_get_returns_canonical_name_and_ids_stay_globally_unique() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", [])
        aliased = await client.get("/v1/publishers/-/books/specs")
        collision = await client.post(
            "/v1/publishers/zeta/books", params={"book_id": "specs"}, json=EBOOK
        )
        wildcard_write = await client.post(
            "/v1/publishers/-/books", params={"book_id": "new"}, json=EBOOK
        )
    assert aliased.status_code == 200
    assert aliased.json()["name"] == "publishers/acme/books/specs"
    assert collision.status_code == 409
    assert wildcard_write.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PATCH", "/v1/publishers/-/books/specs", {"authors": ["authors/grace"]}),
        ("POST", "/v1/publishers/-/books/specs:addAuthor", {"author": "authors/grace"}),
        ("POST", "/v1/publishers/-/books/specs:removeAuthor", {"author": "authors/ada"}),
    ],
    ids=["patch", "add-author", "remove-author"],
)
async def test_wildcard_parent_rejects_link_mutations(
    method: str, path: str, body: dict[str, Any]
) -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", ["authors/ada"])
        response = await client.request(method, path, json=body)
        after = await client.get("/v1/publishers/acme/books/specs")
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert after.json()["authors"] == ["authors/ada"]
    assert after.json()["etag"] == '"1"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_association_subresource_carries_metadata_and_rejects_duplicate_pair() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", ["authors/ada"])
        base = "/v1/publishers/acme/books/specs/contributors"
        created = await client.post(
            base, params={"contributor_id": "c1"}, json={"author": "authors/ada", "role": "editor"}
        )
        other_role = await client.post(
            base,
            params={"contributor_id": "c2"},
            json={"author": "authors/ada", "role": "translator"},
        )
        same_pair = await client.post(
            base, params={"contributor_id": "c3"}, json={"author": "authors/ada", "role": "editor"}
        )
        orphan = await client.post(
            "/v1/publishers/acme/books/missing/contributors",
            params={"contributor_id": "c4"},
            json={"author": "authors/ada", "role": "editor"},
        )
        await _seed(client, "acme", "other", ["authors/ada"])
        other_book = await client.post(
            "/v1/publishers/acme/books/other/contributors",
            params={"contributor_id": "c1"},
            json={"author": "authors/ada", "role": "editor"},
        )
        listed = (await client.get(base)).json()["contributors"]
    assert other_book.status_code == 201  # uniqueness is per book, not global
    assert created.status_code == 201
    assert created.headers["location"] == "/v1/publishers/acme/books/specs/contributors/c1"
    assert other_role.status_code == 201
    assert same_pair.status_code == 409
    assert orphan.status_code == 404
    assert [(c["name"].rsplit("/", 1)[1], c["role"]) for c in listed] == [
        ("c1", "editor"),
        ("c2", "translator"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"kind": "print", "title": "P", "page_count": 320}, "page_count"),
        ({"kind": "ebook", "title": "E", "file_bytes": 4096}, "file_bytes"),
        ({"kind": "audio", "title": "A", "duration_seconds": 3600}, "duration_seconds"),
    ],
    ids=["print", "ebook", "audio"],
)
async def test_discriminator_dispatches_each_kind(payload: dict[str, Any], field: str) -> None:
    async with _client(Catalog()) as client:
        response = await client.post(
            "/v1/publishers/acme/books", params={"book_id": "b"}, json=payload
        )
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == payload["kind"]
    assert body[field] == payload[field]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bad_or_missing_tag_yields_exactly_one_error() -> None:
    async with _client(Catalog()) as client:
        url = "/v1/publishers/acme/books"
        unknown = await client.post(url, params={"book_id": "b"}, json={"kind": "vinyl"})
        missing = await client.post(url, params={"book_id": "b"}, json={"title": "T"})
        crossed = await client.post(url, params={"book_id": "b"}, json={**EBOOK, "page_count": 10})
    assert unknown.status_code == 422
    assert [(e["type"], e["loc"]) for e in unknown.json()["detail"]] == [
        ("union_tag_invalid", ["body"])
    ]
    assert [(e["type"], e["loc"]) for e in missing.json()["detail"]] == [
        ("union_tag_not_found", ["body"])
    ]
    assert [(e["type"], e["loc"]) for e in crossed.json()["detail"]] == [
        ("extra_forbidden", ["body", "ebook", "page_count"])
    ]


@pytest.mark.unit
def test_untagged_union_reports_errors_for_every_member() -> None:
    payload = {"kind": "vinyl", "title": "T"}
    with pytest.raises(ValidationError) as tagged:
        TAGGED_BOOK.validate_python(payload)
    with pytest.raises(ValidationError) as untagged:
        UNTAGGED_BOOK.validate_python(payload)
    assert tagged.value.error_count() == 1
    assert untagged.value.error_count() == 6
    assert {e["loc"][0] for e in untagged.value.errors()} == {"PrintBook", "EBook", "AudioBook"}
    assert isinstance(TAGGED_BOOK.validate_python(EBOOK), EBook)


@pytest.mark.unit
def test_legacy_untagged_union_picks_type_by_field_count_and_order() -> None:
    assert type(LEGACY_UNION.validate_python({"title": "t"})) is LegacyPrint
    assert type(LEGACY_UNION.validate_python({"title": "t", "file_bytes": 5})) is LegacyEBook
    # The client's "kind" is an ignored extra key, so it cannot steer the choice.
    assert type(LEGACY_UNION.validate_python({"title": "t", "kind": "ebook"})) is LegacyPrint


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openapi_publishes_discriminator_mapping() -> None:
    async with _client(Catalog()) as client:
        spec = (await client.get("/openapi.json")).json()
    schema = spec["paths"]["/v1/publishers/{publisher}/books"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert schema["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "print": "#/components/schemas/PrintBook",
            "ebook": "#/components/schemas/EBook",
            "audio": "#/components/schemas/AudioBook",
        },
    }
    assert len(schema["oneOf"]) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kind_is_not_updatable_through_the_authors_update() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", [])
        response = await client.patch(
            "/v1/publishers/acme/books/specs", json={"authors": [], "kind": "print"}
        )
        after = (await client.get("/v1/publishers/acme/books/specs")).json()
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    assert after["kind"] == "ebook"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url", "body", "code", "status"),
    [
        ("GET", "/v1/publishers/acme/books/missing", None, 404, "NOT_FOUND"),
        ("PATCH", "/v1/publishers/acme/books/missing", {"authors": []}, 404, "NOT_FOUND"),
        (
            "PATCH",
            "/v1/publishers/acme/books/specs",
            {"authors": ["authors/nobody"]},
            400,
            "INVALID_ARGUMENT",
        ),
        (
            "POST",
            "/v1/publishers/acme/books/missing:addAuthor",
            {"author": "authors/ada"},
            404,
            "NOT_FOUND",
        ),
        (
            "POST",
            "/v1/publishers/acme/books/specs:addAuthor",
            {"author": "authors/nobody"},
            400,
            "INVALID_ARGUMENT",
        ),
        (
            "POST",
            "/v1/publishers/acme/books/missing:removeAuthor",
            {"author": "authors/ada"},
            404,
            "NOT_FOUND",
        ),
        (
            "POST",
            "/v1/publishers/-/books/specs/contributors?contributor_id=c1",
            {"author": "authors/ada", "role": "editor"},
            400,
            "INVALID_ARGUMENT",
        ),
        (
            "POST",
            "/v1/publishers/acme/books/specs/contributors?contributor_id=c1",
            {"author": "authors/nobody", "role": "editor"},
            400,
            "INVALID_ARGUMENT",
        ),
        ("GET", "/v1/publishers/acme/books/missing/contributors", None, 404, "NOT_FOUND"),
        ("GET", "/v1/publishers/-/books/missing", None, 404, "NOT_FOUND"),
        ("DELETE", "/v1/authors/nobody", None, 404, "NOT_FOUND"),
    ],
)
async def test_error_paths_use_google_rpc_status_and_http_mapping(
    method: str, url: str, body: dict[str, Any] | None, code: int, status: str
) -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", [])
        response = await client.request(method, url, json=body)
    assert response.status_code == code
    assert response.json()["error"] == {
        "code": code,
        "status": status,
        "message": response.json()["error"]["message"],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unfiltered_list_is_scoped_to_one_parent() -> None:
    async with _client(Catalog(authors=set(AUTHORS))) as client:
        await _seed(client, "acme", "specs", [])
        await _seed(client, "zeta", "notes", [])
        scoped = (await client.get("/v1/publishers/zeta/books")).json()["books"]
        everything = (await client.get("/v1/publishers/-/books")).json()["books"]
    assert [b["name"] for b in scoped] == ["publishers/zeta/books/notes"]
    assert [b["name"] for b in everything] == [
        "publishers/acme/books/specs",
        "publishers/zeta/books/notes",
    ]
