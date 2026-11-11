"""Resource hierarchy invariants from AIP-121, AIP-122, AIP-123, AIP-135, AIP-156, and AIP-159."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from api_labs.f04_rest_and_resource_modeling.resource_hierarchy import (
    BOOK,
    CONFIG,
    PatternError,
    ResourcePattern,
    build_app,
)

if TYPE_CHECKING:
    from starlette.requests import Request


def client_for(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def seed(client: httpx.AsyncClient) -> None:
    for publisher in ("p1", "p2"):
        response = await client.post("/v1/publishers", params={"publisher_id": publisher})
        assert response.status_code == 201
    response = await client.post(
        "/v1/publishers/p1/books", params={"book_id": "b1"}, json={"title": "Les Miserables"}
    )
    assert response.status_code == 201


@pytest.mark.unit
def test_pattern_grammar_accepts_collections_and_singletons() -> None:
    assert BOOK.variables == ("publisher", "book")
    assert not BOOK.is_singleton
    assert CONFIG.is_singleton
    assert BOOK.match("publishers/p1/books/b1") == {"publisher": "p1", "book": "b1"}
    assert CONFIG.match("publishers/p1/config") == {"publisher": "p1"}
    assert CONFIG.match("publishers/p1/configs") is None
    assert BOOK.render(publisher="p1", book="b1") == "publishers/p1/books/b1"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("/publishers/{publisher}", "leading slash"),
        ("Publishers/{publisher}", "collection ID"),
        ("publishers/books", "must be a {variable}"),
        ("publishers/{publisher}/{book}", "collection ID"),
        ("people/{person}/people/{friend}", "duplicate collection ID"),
        ("publishers/{book}/books/{book}", "duplicate variable"),
        ("publishers/{publisher_id}", "_id suffix"),
    ],
)
def test_pattern_grammar_rejects_aip_violations(pattern: str, reason: str) -> None:
    with pytest.raises(PatternError, match=reason):
        ResourcePattern.parse(pattern)


@pytest.mark.unit
def test_depth_policy_boundary_and_worst_case_name_length() -> None:
    four = "orgs/{org}/projects/{project}/datasets/{dataset}/tables/{table}"
    pattern = ResourcePattern.parse(four)
    with pytest.raises(PatternError, match="5 collections > 4"):
        ResourcePattern.parse(four + "/columns/{column}")
    # 4 x 63-character IDs + collection IDs + 7 separators.
    assert pattern.max_name_length() == 4 * 63 + len("orgsprojectsdatasetstables") + 7
    assert pattern.max_name_length() == 285
    assert len("/v1/") + pattern.max_name_length() < 8000


@pytest.mark.unit
@pytest.mark.parametrize(
    ("resource_id", "valid"),
    [
        ("a" * 63, True),
        ("a" * 64, False),
        ("les-miserables", True),
        ("Les-Miserables", False),
        ("trailing-", False),
        ("9lives", False),
        ("-", False),
        ("b1:move", False),
    ],
)
def test_resource_id_rfc_1034_grammar(resource_id: str, valid: bool) -> None:
    assert (BOOK.match(f"publishers/p1/books/{resource_id}") is not None) is valid


@pytest.mark.unit
def test_wildcard_is_matched_only_when_allowed() -> None:
    assert BOOK.match("publishers/-/books/b1") is None
    assert BOOK.match("publishers/-/books/b1", allow_wildcard=True) == {
        "publisher": "-",
        "book": "b1",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_returns_201_location_and_implicit_singleton() -> None:
    async with client_for(build_app()) as client:
        response = await client.post("/v1/publishers", params={"publisher_id": "p1"})
        assert response.status_code == 201
        assert response.headers["location"] == "/v1/publishers/p1"
        assert response.json() == {"name": "publishers/p1"}
        config = await client.get("/v1/publishers/p1/config")
        assert config.status_code == 200
        assert config.json() == {"name": "publishers/p1/config", "language": "en"}
        duplicate = await client.post("/v1/publishers", params={"publisher_id": "p1"})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["status"] == "ALREADY_EXISTS"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_singleton_has_no_create_or_delete_and_answers_405_with_allow() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        for method in ("POST", "DELETE", "PUT"):
            response = await client.request(method, "/v1/publishers/p1/config")
            assert response.status_code == 405
            # Starlette builds Allow from a set, so the order varies with PYTHONHASHSEED.
            assert allow_set(response) == {"GET", "HEAD", "PATCH"}
        patched = await client.patch("/v1/publishers/p1/config", json={"language": "fr"})
        assert patched.json() == {"name": "publishers/p1/config", "language": "fr"}


def allow_set(response: httpx.Response) -> set[str]:
    return {token.strip() for token in response.headers["allow"].split(",")}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_route_per_path_makes_allow_list_every_method() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        response = await client.put("/v1/publishers/p1/books/b1")
        assert response.status_code == 405
        assert allow_set(response) == {"GET", "HEAD", "PATCH", "DELETE"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_split_routes_on_one_path_truncate_allow_to_the_first_route() -> None:
    async def endpoint(request: Request) -> Response:
        return Response(request.method)

    path = "/v1/publishers/{publisher}"
    app = Starlette(
        routes=[
            Route(path, endpoint, methods=["GET"]),
            Route(path, endpoint, methods=["DELETE"]),
        ]
    )
    async with client_for(app) as client:
        assert (await client.delete("/v1/publishers/p1")).status_code == 200
        response = await client.put("/v1/publishers/p1")
        assert response.status_code == 405
        # DELETE is supported, yet Allow omits it: Router.app keeps only the first PARTIAL.
        assert allow_set(response) == {"GET", "HEAD"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_parent_with_children_fails_until_force_cascades() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        refused = await client.delete("/v1/publishers/p1")
        assert refused.status_code == 400
        assert refused.json()["error"]["status"] == "FAILED_PRECONDITION"
        assert (await client.get("/v1/publishers/p1/books/b1")).status_code == 200
        forced = await client.delete("/v1/publishers/p1", params={"force": "true"})
        assert forced.status_code == 204
        assert (await client.get("/v1/publishers/p1/books/b1")).status_code == 404
        assert (await client.get("/v1/publishers/p1/config")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_singleton_child_alone_does_not_block_parent_delete() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        response = await client.delete("/v1/publishers/p2")
        assert response.status_code == 204
        assert (await client.get("/v1/publishers/p2/config")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wildcard_list_returns_canonical_names_and_wildcard_get_is_refused() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        await client.post("/v1/publishers/p2/books", params={"book_id": "b1"}, json={})
        listed = await client.get("/v1/publishers/-/books")
        assert listed.status_code == 200
        names = [book["name"] for book in listed.json()["books"]]
        assert names == ["publishers/p1/books/b1", "publishers/p2/books/b1"]
        only_p2 = await client.get("/v1/publishers/p2/books")
        assert [book["name"] for book in only_p2.json()["books"]] == ["publishers/p2/books/b1"]
        ambiguous = await client.get("/v1/publishers/-/books/b1")
        assert ambiguous.status_code == 400
        assert "per-parent" in ambiguous.json()["error"]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_cannot_change_the_resource_name() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        renamed = await client.patch(
            "/v1/publishers/p1/books/b1", json={"name": "publishers/p2/books/b1"}
        )
        assert renamed.status_code == 400
        assert (await client.get("/v1/publishers/p2/books/b1")).status_code == 404
        retitled = await client.patch("/v1/publishers/p1/books/b1", json={"title": "Notre-Dame"})
        assert retitled.json() == {"name": "publishers/p1/books/b1", "title": "Notre-Dame"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_move_produces_a_new_name_and_the_old_name_is_gone() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        moved = await client.post(
            "/v1/publishers/p1/books/b1:move", json={"destination_parent": "publishers/p2"}
        )
        assert moved.status_code == 200
        assert moved.json() == {"name": "publishers/p2/books/b1", "title": "Les Miserables"}
        assert (await client.get("/v1/publishers/p1/books/b1")).status_code == 404
        assert (await client.get("/v1/publishers/p2/books/b1")).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_move_rejects_collisions_and_missing_destinations() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        await client.post("/v1/publishers/p2/books", params={"book_id": "b1"}, json={})
        collision = await client.post(
            "/v1/publishers/p1/books/b1:move", json={"destination_parent": "publishers/p2"}
        )
        assert collision.status_code == 409
        assert (await client.get("/v1/publishers/p1/books/b1")).status_code == 200
        missing = await client.post(
            "/v1/publishers/p1/books/b1:move", json={"destination_parent": "publishers/p9"}
        )
        assert missing.status_code == 404
        malformed = await client.post(
            "/v1/publishers/p1/books/b1:move", json={"destination_parent": "p2"}
        )
        assert malformed.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_on_a_custom_method_url_hits_the_get_route_not_405() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        # `{book}` compiles to `[^/]+`, so `b1:move` is a FULL match for the Get route.
        response = await client.get("/v1/publishers/p1/books/b1:move")
        assert response.status_code == 400
        assert "invalid resource ID" in response.json()["error"]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_percent_encoded_slash_reroutes_unless_guarded() -> None:
    target = "/v1/publishers/p1%2Fbooks%2Fb1"
    async with client_for(build_app(guard_encoded_slash=False)) as client:
        await seed(client)
        # ASGI `path` is percent-decoded: a publisher Get becomes a book Get.
        rerouted = await client.get(target)
        assert rerouted.status_code == 200
        assert rerouted.json()["name"] == "publishers/p1/books/b1"
    async with client_for(build_app()) as client:
        await seed(client)
        guarded = await client.get(target)
        assert guarded.status_code == 400
        assert guarded.json()["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lower_case_percent_encoded_slash_is_decoded_and_guarded_too() -> None:
    # Percent-encoding hex digits are case-insensitive (RFC 3986 Section 2.1), so `%2f` is `/`.
    target = "/v1/publishers/p1%2fbooks%2fb1"
    async with client_for(build_app(guard_encoded_slash=False)) as client:
        await seed(client)
        rerouted = await client.get(target)
        assert rerouted.status_code == 200
        assert rerouted.json()["name"] == "publishers/p1/books/b1"
    async with client_for(build_app()) as client:
        await seed(client)
        guarded = await client.get(target)
        assert guarded.status_code == 400
        assert guarded.json()["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.unit
def test_httpx_removes_dot_segments_before_the_request_is_sent() -> None:
    url = httpx.URL("http://test/v1/publishers/p2/../p1/books/b1")
    assert url.path == "/v1/publishers/p1/books/b1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_singleton_update_cannot_change_the_name() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        renamed = await client.patch(
            "/v1/publishers/p1/config", json={"name": "publishers/p2/config", "language": "fr"}
        )
        assert renamed.status_code == 400
        unchanged = await client.get("/v1/publishers/p1/config")
        assert unchanged.json() == {"name": "publishers/p1/config", "language": "en"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("publisher_id", ["", "Bad", "9lives", "trailing-", "a" * 64, "p1/b"])
async def test_create_rejects_ids_outside_the_aip_122_grammar(publisher_id: str) -> None:
    async with client_for(build_app()) as client:
        response = await client.post("/v1/publishers", params={"publisher_id": publisher_id})
        assert response.status_code == 400
        assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
        # Nothing was stored for the rejected ID.
        assert (await client.get("/v1/publishers/p1")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_child_operations_require_an_existing_parent() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        created = await client.post("/v1/publishers/p9/books", params={"book_id": "b1"}, json={})
        assert created.status_code == 404
        assert created.json()["error"]["status"] == "NOT_FOUND"
        assert (await client.get("/v1/publishers/-/books")).status_code == 200
        assert (await client.get("/v1/publishers/p9/books")).status_code == 404
        assert (await client.get("/v1/publishers/p9/books/b1")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"[1, 2]", b'"text"', b"{not json"])
async def test_bodies_that_are_not_json_objects_are_rejected(content: bytes) -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        created = await client.post(
            "/v1/publishers/p1/books", params={"book_id": "b2"}, content=content
        )
        assert created.status_code == 400
        assert (await client.get("/v1/publishers/p1/books/b2")).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_is_served_by_the_get_handler() -> None:
    async with client_for(build_app()) as client:
        await seed(client)
        got = await client.get("/v1/publishers/p1/books/b1")
        head = await client.head("/v1/publishers/p1/books/b1")
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == got.headers["content-length"]
        assert (await client.head("/v1/publishers/p1/books/b9")).status_code == 404
