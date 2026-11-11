"""Status code selection invariants: 2xx choice, redirect method rewriting, 4xx precedence."""

import sys
from collections.abc import AsyncIterator
from http import HTTPStatus

import h11
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

from api_labs.f04_rest_and_resource_modeling import status_code_selection
from api_labs.f04_rest_and_resource_modeling.status_code_selection import (
    PROBLEM_JSON,
    RequestFacts,
    WidgetStore,
    build_redirect_lab_app,
    build_widget_app,
    h11_client_read,
    h11_server_bytes,
    reason_phrase,
    rfc9110_redirect_method,
    select_client_error,
    select_success_status,
)

RW = {"Authorization": "Bearer alice-rw"}
RO = {"Authorization": "Bearer alice-ro"}
BOB = {"Authorization": "Bearer bob-rw"}


def client_for(app: FastAPI, *, follow: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=follow
    )


@pytest_asyncio.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(build_widget_app(WidgetStore())) as client:
        yield client


@pytest.mark.unit
@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (RequestFacts(), None),
        (RequestFacts(well_formed=False, authenticated=False), 400),
        (RequestFacts(authenticated=False, resource_exists=False, permitted=False), 401),
        (RequestFacts(visible_to_caller=False, permitted=False), 404),
        (RequestFacts(resource_exists=False, semantically_valid=False), 404),
        (RequestFacts(permitted=False, media_type_supported=False), 403),
        (RequestFacts(media_type_supported=False, semantically_valid=False), 415),
        (RequestFacts(semantically_valid=False, conflicts_with_state=True), 422),
        (RequestFacts(conflicts_with_state=True), 409),
    ],
    ids=[
        "ok",
        "400-before-401",
        "401-before-404",
        "hidden-404-before-403",
        "404-before-422",
        "403-before-415",
        "415-before-422",
        "422-before-409",
        "409",
    ],
)
def test_client_error_precedence(facts: RequestFacts, expected: int | None) -> None:
    assert select_client_error(facts) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("enacted", "created", "returns_body", "expected"),
    [
        (False, True, True, 202),
        (True, True, False, 201),
        (True, False, True, 200),
        (True, False, False, 204),
    ],
)
def test_success_status_selection(
    enacted: bool, created: bool, returns_body: bool, expected: int
) -> None:
    assert select_success_status(enacted=enacted, created=created, returns_body=returns_body) == (
        expected
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_returns_201_with_location_that_resolves(api: httpx.AsyncClient) -> None:
    created = await api.post("/widgets", json={"name": "gear", "quantity": 3}, headers=RW)
    assert created.status_code == 201
    assert created.headers["location"] == "/widgets/w1"
    fetched = await api.get(created.headers["location"], headers=RW)
    assert fetched.status_code == 200
    assert fetched.json() == created.json()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_returns_204_without_content_length_or_body(api: httpx.AsyncClient) -> None:
    await api.post("/widgets", json={"name": "gear", "quantity": 3}, headers=RW)
    deleted = await api.delete("/widgets/w1", headers=RW)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert "content-length" not in deleted.headers
    again = await api.delete("/widgets/w1", headers=RW)
    assert again.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_rejects_204_response_model_and_strips_204_body() -> None:
    app = FastAPI()
    with pytest.raises(AssertionError, match="Status code 204 must not have a response body"):

        @app.delete("/typed", status_code=204)
        async def typed() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
            return {"deleted": "yes"}

    @app.delete("/thing", status_code=204, response_model=None)
    async def delete_thing() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"deleted": "yes"}

    async with client_for(app) as client:
        response = await client.delete("/thing")
    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_sends_explicit_204_body_bytes_without_content_length() -> None:
    app = FastAPI()

    @app.delete("/raw", status_code=204, response_class=Response)
    async def raw() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(b"deleted", status_code=204)

    async with client_for(app) as client:
        response = await client.delete("/raw")
    assert response.status_code == 204
    assert "content-length" not in response.headers
    # Starlette 1.10.0 omits Content-Length but still sends self.body; only the wire layer refuses.
    assert response.content == b"deleted"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_202_carries_a_status_monitor_not_a_result(api: httpx.AsyncClient) -> None:
    response = await api.post("/exports", headers=RW)
    assert response.status_code == 202
    assert response.json() == {
        "name": "operations/op-1",
        "state": "RUNNING",
        "monitor": "/operations/op-1",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_401_challenges_and_follows_rfc_6750_error_codes(api: httpx.AsyncClient) -> None:
    missing = await api.get("/widgets/w1")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == 'Bearer realm="widgets"'
    bad = await api.get("/widgets/w1", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == 'Bearer realm="widgets", error="invalid_token"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_403_only_for_visible_resource_with_insufficient_scope(
    api: httpx.AsyncClient,
) -> None:
    await api.post("/widgets", json={"name": "gear", "quantity": 3}, headers=RW)
    forbidden = await api.delete("/widgets/w1", headers=RO)
    assert forbidden.status_code == 403
    assert 'error="insufficient_scope"' in forbidden.headers["www-authenticate"]
    assert (await api.get("/widgets/w1", headers=RO)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_other_tenant_gets_404_identical_to_absent(api: httpx.AsyncClient) -> None:
    await api.post("/widgets", json={"name": "gear", "quantity": 3}, headers=RW)
    hidden = await api.get("/widgets/w1", headers=BOB)
    absent = await api.get("/widgets/w999", headers=BOB)
    assert hidden.status_code == absent.status_code == 404
    assert hidden.content == absent.content
    assert hidden.headers.multi_items() == absent.headers.multi_items()
    visible_delete = await api.delete("/widgets/w1", headers=RO)
    assert visible_delete.status_code == 403  # same tenant: visible, so scope decides


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_400_semantic_422_conflict_409_media_415(api: httpx.AsyncClient) -> None:
    json_headers = {**RW, "Content-Type": "application/json"}
    malformed = await api.post("/widgets", content=b'{"name": "gear",', headers=json_headers)
    invalid = await api.post("/widgets", json={"name": "gear", "quantity": -1}, headers=RW)
    first = await api.post("/widgets", json={"name": "gear", "quantity": 1}, headers=RW)
    duplicate = await api.post("/widgets", json={"name": "gear", "quantity": 2}, headers=RW)
    text_headers = {**RW, "Content-Type": "text/plain"}
    wrong_type = await api.post("/widgets", content=b"gear", headers=text_headers)
    statuses = [r.status_code for r in (malformed, invalid, first, duplicate, wrong_type)]
    assert statuses == [400, 422, 201, 409, 415]
    for response in (malformed, invalid, duplicate, wrong_type):
        assert response.headers["content-type"] == PROBLEM_JSON
        assert response.json()["status"] == response.status_code
    assert invalid.json()["title"] == "Unprocessable Content"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auth_dependency_runs_before_body_validation_but_after_json_parse(
    api: httpx.AsyncClient,
) -> None:
    invalid_body = await api.post("/widgets", json={"name": "", "quantity": -1})
    assert invalid_body.status_code == 401
    malformed = await api.post(
        "/widgets", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert malformed.status_code == 400
    read_only = await api.post("/widgets", content=b"x", headers={**RO, "Content-Type": "a/b"})
    assert read_only.status_code == 403


class Item(BaseModel):
    name: str


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_fastapi_answers_malformed_json_and_wrong_media_type_with_422() -> None:
    app = FastAPI()

    @app.post("/items")
    async def create(item: Item) -> Item:
        return item

    async with client_for(app) as client:
        malformed = await client.post(
            "/items", content=b'{"name":', headers={"Content-Type": "application/json"}
        )
        text = await client.post(
            "/items", content=b'{"name": "x"}', headers={"Content-Type": "text/plain"}
        )
    assert malformed.status_code == 422
    assert malformed.json()["detail"][0]["type"] == "json_invalid"
    assert text.status_code == 422
    assert text.json()["detail"][0]["type"] == "model_attributes_type"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_405_allow_lists_only_the_first_partially_matching_route(
    api: httpx.AsyncClient,
) -> None:
    response = await api.put("/widgets/w1", headers=RW)
    assert response.status_code == 405
    # GET and DELETE are separate routes; Starlette's Router keeps only the first partial match.
    assert response.headers["allow"] == "GET"
    assert response.json()["status"] == 405
    head = await api.head("/widgets/w1", headers=RW)
    assert head.status_code == 405
    assert head.headers["allow"] == "GET"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_308_old_path_redirects_and_get_follows(api: httpx.AsyncClient) -> None:
    await api.post("/widgets", json={"name": "gear", "quantity": 3}, headers=RW)
    moved = await api.get("/v0/widgets/w1")
    assert moved.status_code == 308
    assert moved.headers["location"] == "/widgets/w1"
    followed = await api.get("/v0/widgets/w1", headers=RW, follow_redirects=True)
    assert followed.status_code == 200
    assert [r.status_code for r in followed.history] == [308]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "status", "expected"),
    [
        ("POST", 301, "GET"),
        ("POST", 302, "GET"),
        ("POST", 303, "GET"),
        ("POST", 307, "POST"),
        ("POST", 308, "POST"),
        ("PUT", 302, "PUT"),
        ("DELETE", 303, "GET"),
        ("HEAD", 303, "HEAD"),
    ],
)
def test_rfc9110_redirect_method(method: str, status: int, expected: str) -> None:
    assert rfc9110_redirect_method(method, status) == expected


@pytest.mark.unit
def test_rfc9110_redirect_method_rejects_non_redirects() -> None:
    with pytest.raises(ValueError, match="304 is not an automatic redirect"):
        rfc9110_redirect_method("GET", 304)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "status", "expected_method", "body_kept"),
    [
        ("POST", 301, "GET", False),
        ("POST", 302, "GET", False),
        ("POST", 303, "GET", False),
        ("POST", 307, "POST", True),
        ("POST", 308, "POST", True),
        ("PUT", 301, "PUT", True),
        ("PUT", 302, "GET", False),
        ("PATCH", 303, "GET", False),
    ],
)
async def test_httpx_redirect_method_rewriting(
    method: str, status: int, expected_method: str, body_kept: bool
) -> None:
    async with client_for(build_redirect_lab_app(), follow=True) as client:
        response = await client.request(
            method,
            f"/redirect/{status}",
            content=b'{"a": 1}',
            headers={"Content-Type": "application/json"},
        )
    echoed = response.json()
    assert echoed["method"] == expected_method
    assert echoed["body"] == ('{"a": 1}' if body_kept else "")
    # httpx 0.28.1 drops Content-Length on a switch to GET but keeps Content-Type.
    assert echoed["content_type"] == "application/json"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_302_turns_put_into_get_where_rfc9110_does_not() -> None:
    async with client_for(build_redirect_lab_app(), follow=True) as client:
        response = await client.put("/redirect/302", content=b"x")
    assert response.json()["method"] == "GET"
    assert rfc9110_redirect_method("PUT", 302) == "PUT"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_strips_authorization_on_cross_origin_redirect() -> None:
    async with client_for(build_redirect_lab_app(), follow=True) as client:
        same = await client.get("/redirect/307", headers={"Authorization": "Bearer t"})
        cross = await client.get(
            "/redirect/307",
            params={"target": "http://elsewhere.test/echo"},
            headers={"Authorization": "Bearer t"},
        )
    assert same.json()["authorization"] == "Bearer t"
    assert cross.json()["authorization"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_httpx_stops_redirect_loop_after_20_hops() -> None:
    hits: list[str] = []
    async with client_for(build_redirect_lab_app(hits), follow=True) as client:
        with pytest.raises(httpx.TooManyRedirects):
            await client.get("/loop")
    assert len(hits) == 21


@pytest.mark.unit
def test_unknown_status_is_treated_as_its_class() -> None:
    response = httpx.Response(471)
    assert response.is_client_error
    assert not response.is_server_error
    assert response.reason_phrase == ""


@pytest.mark.unit
def test_h11_server_refuses_to_send_a_body_on_204() -> None:
    assert h11_server_bytes(200, b"x").endswith(b"\r\n\r\nx")
    with pytest.raises(h11.LocalProtocolError, match="Too much data"):
        h11_server_bytes(204, b"x")


@pytest.mark.unit
def test_bytes_after_a_204_desynchronize_the_next_response() -> None:
    good = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    bad_204 = b"HTTP/1.1 204 No Content\r\nContent-Length: 5\r\n\r\nhello"
    with pytest.raises(h11.RemoteProtocolError):
        h11_client_read(bad_204 + good, requests=2)
    framed_200 = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
    assert h11_client_read(framed_200 + good, requests=2) == [(200, b"hello"), (200, b"ok")]


@pytest.mark.unit
def test_reason_phrase_is_rfc9110_on_every_cpython() -> None:
    assert reason_phrase(422) == "Unprocessable Content"
    assert reason_phrase(413) == "Content Too Large"
    stdlib = "Unprocessable Content" if sys.version_info >= (3, 13) else "Unprocessable Entity"
    assert HTTPStatus(422).phrase == stdlib


@pytest.mark.unit
def test_reason_phrase_override_beats_an_rfc7231_era_stdlib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OldStatus:
        phrase = "Unprocessable Entity"

        def __init__(self, status: int) -> None:
            self.status = status

    monkeypatch.setattr(status_code_selection, "HTTPStatus", OldStatus)
    assert reason_phrase(422) == "Unprocessable Content"
    assert reason_phrase(404) == "Unprocessable Entity"  # no override: the stub answers


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/json; charset=utf-8", "Application/JSON"])
async def test_415_check_accepts_json_media_type_parameters_and_case(
    api: httpx.AsyncClient, content_type: str
) -> None:
    response = await api.post(
        "/widgets",
        content=b'{"name": "gear", "quantity": 1}',
        headers={**RW, "Content-Type": content_type},
    )
    assert response.status_code == 201


@pytest.mark.unit
@pytest.mark.asyncio
async def test_409_conflict_is_scoped_to_the_callers_tenant(api: httpx.AsyncClient) -> None:
    body = {"name": "gear", "quantity": 1}
    assert (await api.post("/widgets", json=body, headers=RW)).status_code == 201
    assert (await api.post("/widgets", json=body, headers=BOB)).status_code == 201
    assert (await api.post("/widgets", json=body, headers=BOB)).status_code == 409


@pytest.mark.unit
@pytest.mark.asyncio
async def test_202_export_requires_authentication_then_write_scope(
    api: httpx.AsyncClient,
) -> None:
    anonymous = await api.post("/exports")
    read_only = await api.post("/exports", headers=RO)
    assert (anonymous.status_code, read_only.status_code) == (401, 403)
    assert 'error="insufficient_scope"' in read_only.headers["www-authenticate"]
