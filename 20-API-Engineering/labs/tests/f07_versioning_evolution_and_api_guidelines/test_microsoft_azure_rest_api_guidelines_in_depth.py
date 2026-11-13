"""Lab for Microsoft-Azure-REST-API-Guidelines-in-Depth: api-version, ids, errors, and LROs."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.routing import Route

from api_labs.f07_versioning_evolution_and_api_guidelines.azure_guidelines import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MISSING_API_VERSION_MESSAGE,
    STATUS_MONITOR_RETENTION_SECONDS,
    AzureEdge,
    AzureLab,
    ManualClock,
    RequestLogRecord,
    create_app,
    error_response,
    list_all,
    poll_lro,
    retry_after_seconds,
    with_api_version,
)

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.routing import BaseRoute

GA = "2025-06-01"
OLD_GA = "2024-10-01"
LATEST_PREVIEW = "2026-03-01-preview"


def client_for(lab: AzureLab, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=lab.app, raise_app_exceptions=raise_app_exceptions)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def assert_no_nulls(value: Any) -> None:
    """The guidelines forbid sending ``null``; walk the whole JSON document."""
    assert value is not None
    if isinstance(value, dict):
        for item in value.values():  # pyright: ignore[reportUnknownVariableType]
            assert_no_nulls(item)
    elif isinstance(value, list):
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            assert_no_nulls(item)


def assert_azure_error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert response.headers["x-ms-error-code"] == body["error"]["code"]
    assert isinstance(body["error"]["message"], str)
    assert_no_nulls(body)
    error: dict[str, Any] = body["error"]
    return error


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_api_version_is_400_missing_api_version_parameter() -> None:
    async with client_for(create_app()) as client:
        response = await client.get("/widgets/w1")
    error = assert_azure_error(response, 400, "MissingApiVersionParameter")
    # The guideline fixes this text (anchor versioning-api-version-missing); pin the literal.
    assert MISSING_API_VERSION_MESSAGE == (
        "The api-version query parameter (?api-version=) is required for all requests"
    )
    assert error["message"] == MISSING_API_VERSION_MESSAGE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_api_version_lists_stable_versions_and_only_latest_preview() -> None:
    async with client_for(create_app()) as client:
        unknown = await client.get("/widgets/w1", params={"api-version": "2023-01-01"})
        superseded_preview = await client.get(
            "/widgets/w1", params={"api-version": "2025-09-01-preview"}
        )
    error = assert_azure_error(unknown, 400, "UnsupportedApiVersionValue")
    assert error["message"] == (
        "Unsupported api-version '2023-01-01'. "
        "The supported api-versions are '2024-10-01, 2025-06-01, 2026-03-01-preview'."
    )
    # The older preview is still served (inside its retirement window) but not advertised.
    assert_azure_error(superseded_preview, 404, "WidgetNotFound")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_api_version_is_rejected_instead_of_last_value_wins() -> None:
    query = b"api-version=1999-01-01&api-version=2025-06-01"
    scope: dict[str, Any] = {"type": "http", "query_string": query, "headers": []}
    # Starlette's MultiDict keeps the last value, so a naive gate would accept this request.
    assert Request(scope).query_params["api-version"] == GA
    async with client_for(create_app()) as client:
        response = await client.get(f"/widgets/w1?{query.decode()}")
    error = assert_azure_error(response, 400, "InvalidQueryParameterValue")
    assert error["target"] == "api-version"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_version_gates_request_schema_and_representation() -> None:
    async with client_for(create_app()) as client:
        rejected = await client.put(
            "/widgets/w1", params={"api-version": OLD_GA}, json={"color": "red", "size": "L"}
        )
        created = await client.put(
            "/widgets/w1", params={"api-version": GA}, json={"color": "red", "size": "L"}
        )
        replaced_by_old_client = await client.put(
            "/widgets/w1", params={"api-version": OLD_GA}, json={"color": "blue"}
        )
        new_view = await client.get("/widgets/w1", params={"api-version": GA})
        old_view = await client.get("/widgets/w1", params={"api-version": OLD_GA})
    error = assert_azure_error(rejected, 400, "InvalidRequestContent")
    assert error["target"] == "size"
    assert created.status_code == 201
    assert created.json() == {"name": "w1", "color": "red", "size": "L"}
    assert replaced_by_old_client.status_code == 200
    # A PUT from an older api-version resets fields it cannot know about to their defaults.
    assert new_view.json() == {"name": "w1", "color": "blue", "size": "M"}
    assert old_view.json() == {"name": "w1", "color": "blue"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_response_has_a_unique_service_generated_request_id() -> None:
    lab = create_app()
    seen: list[str] = []
    red = {"color": "red"}
    async with client_for(lab) as client:
        for i in range(20):
            for response in (
                await client.put(f"/widgets/w{i}", params={"api-version": GA}, json=red),
                await client.get(f"/widgets/missing{i}", params={"api-version": GA}),
                await client.get(f"/widgets/w{i}"),
                await client.get("/nowhere", params={"api-version": GA}),
            ):
                request_id = response.headers["x-ms-request-id"]
                assert str(uuid.UUID(request_id)) == request_id
                seen.append(request_id)
    assert len(seen) == 80
    assert len(set(seen)) == 80
    assert [record.request_id for record in lab.log] == seen


async def boom(request: Request) -> Response:
    raise RuntimeError("database connection reset")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unhandled_exception_500_still_carries_request_id_and_envelope() -> None:
    routes: list[BaseRoute] = [Route("/boom", boom)]
    lab = create_app(extra_routes=routes)
    async with client_for(lab, raise_app_exceptions=False) as client:
        response = await client.get("/boom", params={"api-version": GA})
    assert_azure_error(response, 500, "InternalServerError")
    assert "database" not in response.text
    assert response.headers["x-ms-request-id"] == lab.log[-1].request_id
    assert lab.log[-1].status == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_id_middleware_inside_starlette_misses_server_error_500() -> None:
    log: list[RequestLogRecord] = []

    async def on_unhandled(request: Request, exc: Exception) -> Response:
        return error_response(500, "InternalServerError", "The service failed unexpectedly.")

    inner = Starlette(
        routes=[Route("/boom", boom)],
        middleware=[Middleware(AzureEdge, log=log, new_id=lambda: "fixed-id")],
        exception_handlers={Exception: on_unhandled},
    )
    transport = httpx.ASGITransport(app=inner, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom", params={"api-version": GA})
    assert response.status_code == 500
    # ServerErrorMiddleware sits outside user middleware, so the id never reaches the wire.
    assert "x-ms-request-id" not in response.headers
    assert log[-1].status == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_and_correlation_ids_are_validated_logged_and_echoed_on_opt_in() -> None:
    lab = create_app()
    client_id = "9C4D50EE-2D56-4CD3-8152-34347DC9F2B0"
    correlation_id = str(uuid.UUID(int=7))
    async with client_for(lab) as client:
        silent = await client.get(
            "/widgets/w1",
            params={"api-version": GA},
            headers={
                "x-ms-client-request-id": client_id,
                "x-ms-correlation-request-id": correlation_id,
            },
        )
        echoed = await client.get(
            "/widgets/w1",
            params={"api-version": GA},
            headers={"x-ms-client-request-id": client_id, "x-ms-return-client-request-id": "true"},
        )
        braces = await client.get(
            "/widgets/w1",
            params={"api-version": GA},
            headers={"x-ms-client-request-id": "{" + client_id + "}"},
        )
    assert "x-ms-client-request-id" not in silent.headers
    assert echoed.headers["x-ms-client-request-id"] == client_id
    error = assert_azure_error(braces, 400, "InvalidHeaderValue")
    assert error["target"] == "x-ms-client-request-id"
    assert lab.log[0].client_request_id == client_id
    assert lab.log[0].correlation_request_id == correlation_id
    assert lab.log[0].request_id == silent.headers["x-ms-request-id"]
    assert lab.log[0].request_id != client_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unrecognized_and_tracing_headers_never_fail_a_request() -> None:
    async with client_for(create_app()) as client:
        response = await client.put(
            "/widgets/w1",
            params={"api-version": GA},
            json={"color": "green"},
            headers={
                "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
                "x-gateway-hop": "edge-7",
                "x-ms-useragent": "azsdk-python-lab/1.0",
            },
        )
    assert response.status_code == 201


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_is_204_whether_or_not_the_resource_exists() -> None:
    async with client_for(create_app()) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        first = await client.delete("/widgets/w1", params={"api-version": GA})
        second = await client.delete("/widgets/w1", params={"api-version": GA})
    assert (first.status_code, second.status_code) == (204, 204)
    assert first.content == second.content == b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_colon_is_reserved_for_actions_and_rejected_in_resource_ids() -> None:
    async with client_for(create_app()) as client:
        response = await client.put(
            "/widgets/w1:rebuild", params={"api-version": GA}, json={"color": "red"}
        )
    error = assert_azure_error(response, 400, "InvalidResourceId")
    assert error["target"] == "widget name"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lro_action_returns_202_status_monitor_and_operation_location() -> None:
    lab = create_app()
    async with client_for(lab) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        response = await client.post(
            "/widgets/w1:rebuild",
            params={"api-version": GA},
            json={"mode": "full"},
            headers={"Operation-Id": "op-1"},
        )
        invalid = await client.post(
            "/widgets/w1:rebuild", params={"api-version": GA}, json={"mode": "turbo"}
        )
    assert response.status_code == 202
    assert response.headers["operation-id"] == "op-1"
    assert response.headers["operation-location"] == (
        "http://test/operations/op-1?api-version=2025-06-01"
    )
    assert response.headers["retry-after"] == "2"
    assert response.json() == {"id": "op-1", "kind": "WidgetRebuild", "status": "NotStarted"}
    # Validation happens synchronously, before any status monitor exists.
    assert_azure_error(invalid, 400, "InvalidRequestContent")
    assert list(lab.store.operations) == ["op-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poller_honors_retry_after_and_may_switch_api_version() -> None:
    lab = create_app(ManualClock())
    polled: list[tuple[float, str, str | None]] = []
    poll_versions: list[str] = []

    async def sleep(seconds: float) -> None:
        lab.clock.advance(seconds)

    async def record(response: httpx.Response) -> None:
        await response.aread()
        if response.request.url.path.startswith("/operations/"):
            status = response.json()["status"]
            polled.append((lab.clock.now, status, response.headers.get("retry-after")))
            poll_versions.append(response.request.url.params["api-version"])

    async with client_for(lab) as client:
        client.event_hooks["response"] = [record]
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        initial = await client.post(
            "/widgets/w1:rebuild", params={"api-version": GA}, json={"mode": "full"}
        )
        monitor, waits = await poll_lro(client, initial, api_version=LATEST_PREVIEW, sleep=sleep)
    assert waits == [2, 2, 2]
    assert polled == [(2.0, "Running", "2"), (4.0, "Running", "2"), (6.0, "Succeeded", None)]
    assert monitor["result"] == {"widgetName": "w1", "mode": "full"}
    # The operation started under GA; every poll used the poller's own version instead.
    assert initial.headers["operation-location"].endswith(f"?api-version={GA}")
    assert poll_versions == [LATEST_PREVIEW] * 3
    assert [record.path for record in lab.log].count(f"/operations/{monitor['id']}") == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operation_id_retry_is_idempotent_and_reuse_with_new_body_conflicts() -> None:
    lab = create_app()
    async with client_for(lab) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        args: dict[str, Any] = {
            "params": {"api-version": GA},
            "headers": {"Operation-Id": "op-7"},
        }
        first = await client.post("/widgets/w1:rebuild", json={"mode": "full"}, **args)
        lab.clock.advance(1.5)
        retry = await client.post("/widgets/w1:rebuild", json={"mode": "full"}, **args)
        reuse = await client.post("/widgets/w1:rebuild", json={"mode": "incremental"}, **args)
    assert (first.status_code, retry.status_code) == (202, 202)
    assert retry.json() == {"id": "op-7", "kind": "WidgetRebuild", "status": "Running"}
    assert len(lab.store.operations) == 1
    assert_azure_error(reuse, 409, "OperationIdConflict")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_operation_reports_error_detail_without_result() -> None:
    lab = create_app()
    async with client_for(lab) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        await client.post(
            "/widgets/w1:rebuild",
            params={"api-version": GA},
            json={"mode": "full"},
            headers={"Operation-Id": "op-9"},
        )
        lab.clock.advance(3)
        await client.delete("/widgets/w1", params={"api-version": GA})
        lab.clock.advance(3)
        response = await client.get("/operations/op-9", params={"api-version": GA})
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "Failed"
    assert body["error"]["code"] == "WidgetDeleted"
    assert "result" not in body
    assert "retry-after" not in response.headers
    assert_no_nulls(body)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_status_monitor_is_retained_24_hours_after_completion() -> None:
    assert STATUS_MONITOR_RETENTION_SECONDS == 24 * 60 * 60
    lab = create_app()
    async with client_for(lab) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        await client.post(
            "/widgets/w1:rebuild",
            params={"api-version": GA},
            json={"mode": "incremental"},
            headers={"Operation-Id": "op-3"},
        )
        lab.clock.advance(2 + STATUS_MONITOR_RETENTION_SECONDS)
        kept = await client.get("/operations/op-3", params={"api-version": OLD_GA})
        lab.clock.advance(1)
        purged = await client.get("/operations/op-3", params={"api-version": OLD_GA})
    assert kept.json()["status"] == "Succeeded"
    assert_azure_error(purged, 404, "OperationNotFound")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [("17", 17), ("0", 0), (None, 2), ("1.5", 2), ("-1", 2), ("Fri, 31 Dec 1999 23:59:59 GMT", 2)],
    ids=["seconds", "zero", "absent", "fraction", "negative", "http-date"],
)
def test_retry_after_parses_only_delay_seconds(value: str | None, expected: int) -> None:
    assert retry_after_seconds(value, default=2) == expected


@pytest.mark.unit
def test_with_api_version_replaces_only_the_version_parameter() -> None:
    url = "https://svc.example/operations/op-1?api-version=2025-06-01&location=westus"
    assert with_api_version(url, LATEST_PREVIEW) == (
        "https://svc.example/operations/op-1?location=westus&api-version=2026-03-01-preview"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_ids_are_rejected_and_never_reflected() -> None:
    async with client_for(create_app()) as client:
        reflected = await client.get(
            "/widgets/w1",
            params={"api-version": GA},
            headers={
                "x-ms-client-request-id": "not-a-guid",
                "x-ms-return-client-request-id": "true",
            },
        )
        correlation = await client.get(
            "/widgets/w1",
            params={"api-version": GA},
            headers={"x-ms-correlation-request-id": "not-a-guid"},
        )
    error = assert_azure_error(reflected, 400, "InvalidHeaderValue")
    assert error["target"] == "x-ms-client-request-id"
    # The opt-in echo applies only to a validated GUID, so a rejected value is never reflected.
    assert "x-ms-client-request-id" not in reflected.headers
    error = assert_azure_error(correlation, 400, "InvalidHeaderValue")
    assert error["target"] == "x-ms-correlation-request-id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blank_version_framework_errors_and_bad_json_use_the_envelope() -> None:
    async with client_for(create_app()) as client:
        blank = await client.get("/widgets/w1?api-version=")
        no_route = await client.get("/nowhere", params={"api-version": GA})
        wrong_method = await client.post("/widgets/w1", params={"api-version": GA})
        bad_json = await client.put(
            "/widgets/w1",
            params={"api-version": GA},
            content=b"{",
            headers={"content-type": "application/json"},
        )
    # A present but empty value is an unknown version, not a missing parameter.
    error = assert_azure_error(blank, 400, "UnsupportedApiVersionValue")
    assert error["message"].startswith("Unsupported api-version ''. ")
    assert_azure_error(no_route, 404, "NotFound")
    assert_azure_error(wrong_method, 405, "MethodNotAllowed")
    assert "allow" in wrong_method.headers
    assert_azure_error(bad_json, 400, "InvalidRequestContent")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operation_id_is_length_capped_and_bound_to_its_target_resource() -> None:
    lab = create_app()
    args: dict[str, Any] = {"params": {"api-version": GA}, "json": {"mode": "full"}}
    async with client_for(lab) as client:
        for name in ("w1", "w2"):
            await client.put(f"/widgets/{name}", params={"api-version": GA}, json={"color": "red"})
        longest = await client.post(
            "/widgets/w1:rebuild", headers={"Operation-Id": "a" * 63}, **args
        )
        too_long = await client.post(
            "/widgets/w1:rebuild", headers={"Operation-Id": "a" * 64}, **args
        )
        other_widget = await client.post(
            "/widgets/w2:rebuild", headers={"Operation-Id": "a" * 63}, **args
        )
    assert longest.status_code == 202
    error = assert_azure_error(too_long, 400, "InvalidResourceId")
    assert error["target"] == "Operation-Id"
    # The same Operation-Id and body against a different resource is not a retry.
    assert_azure_error(other_widget, 409, "OperationIdConflict")
    assert list(lab.store.operations) == ["a" * 63]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poller_reads_each_retry_after_falls_back_and_ignores_status_case() -> None:
    replies = [
        httpx.Response(200, json={"id": "op-1", "status": "Running"}, headers={"retry-after": "7"}),
        httpx.Response(200, json={"id": "op-1", "status": "running"}),
        httpx.Response(200, json={"id": "op-1", "status": "succeeded"}),
    ]
    requested: list[str] = []
    slept: list[float] = []

    def monitor_stub(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return replies[len(requested) - 1]

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    location = "http://test/operations/op-1?api-version=2025-06-01"
    initial = httpx.Response(202, headers={"operation-location": location, "retry-after": "3"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(monitor_stub)) as client:
        monitor, waits = await poll_lro(
            client, initial, api_version=LATEST_PREVIEW, sleep=sleep, default_interval=5
        )
    # 3 from the 202, 7 from the first poll, then the default because the header is absent.
    assert waits == [3, 7, 5]
    assert slept == [3, 7, 5]
    assert monitor["status"] == "succeeded"
    assert requested == ["http://test/operations/op-1?api-version=2026-03-01-preview"] * 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poller_gives_up_after_max_polls() -> None:
    polls: list[str] = []

    def always_running(request: httpx.Request) -> httpx.Response:
        polls.append(request.method)
        return httpx.Response(
            200, json={"id": "op-1", "status": "Running"}, headers={"retry-after": "1"}
        )

    async def sleep(seconds: float) -> None:
        return None

    initial = httpx.Response(202, headers={"operation-location": "http://test/operations/op-1"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(always_running)) as client:
        with pytest.raises(TimeoutError, match="still running after 4 polls"):
            await poll_lro(client, initial, api_version=GA, sleep=sleep, max_polls=4)
    assert polls == ["GET"] * 4


async def put_widgets(client: httpx.AsyncClient, count: int) -> None:
    for i in range(1, count + 1):
        size = {"size": "L"} if i % 2 else {}
        body = {"color": "red", **size}
        response = await client.put(f"/widgets/w{i}", params={"api-version": GA}, json=body)
        assert response.status_code == 201


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_pages_carry_api_version_in_next_link_and_omit_it_on_the_last_page() -> None:
    assert (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE) == (2, 3)
    async with client_for(create_app()) as client:
        await put_widgets(client, 5)
        first = await client.get("/widgets", params={"api-version": GA})
        second = await client.get(first.json()["nextLink"])
        last = await client.get(second.json()["nextLink"])
        everything = await list_all(client, f"/widgets?api-version={GA}")
        empty = await client.get("/widgets", params={"api-version": GA, "continuationToken": "w5"})
        full_last = await client.get(
            "/widgets", params={"api-version": GA, "maxpagesize": "1", "continuationToken": "w4"}
        )
    assert first.status_code == 200
    assert [item["name"] for item in first.json()["value"]] == ["w1", "w2"]
    # The link is absolute and already carries every parameter the service requires.
    assert first.json()["nextLink"] == (
        "http://test/widgets?api-version=2025-06-01&maxpagesize=2&continuationToken=w2"
    )
    assert second.json()["nextLink"] == (
        "http://test/widgets?api-version=2025-06-01&maxpagesize=2&continuationToken=w4"
    )
    # The last page has no nextLink field at all: not null, not an empty string.
    assert last.json() == {"value": [{"name": "w5", "color": "red", "size": "L"}]}
    assert [item["name"] for item in everything] == ["w1", "w2", "w3", "w4", "w5"]
    assert empty.json() == {"value": []}
    # A last page that is exactly full still has no nextLink: the service looks one item ahead.
    assert full_last.json() == last.json()
    for response in (first, second, last, empty, full_last):
        assert_no_nulls(response.json())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_next_link_survives_an_api_version_swap_but_not_a_stripped_query() -> None:
    async with client_for(create_app()) as client:
        await put_widgets(client, 3)
        first = await client.get("/widgets", params={"api-version": GA})
        next_link: str = first.json()["nextLink"]
        swapped = await client.get(with_api_version(next_link, OLD_GA))
        # The @azure/arm-synapse 2.0.0 failure mode: page two requested without the query.
        stripped = await client.get(next_link.split("?", maxsplit=1)[0])
    assert first.json()["value"][0] == {"name": "w1", "color": "red", "size": "L"}
    # A poller or pager built against another version keeps the cursor and gets its own shape.
    assert swapped.json() == {"value": [{"name": "w3", "color": "red"}]}
    assert_azure_error(stripped, 400, "MissingApiVersionParameter")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maxpagesize_is_an_upper_bound_that_the_service_may_undercut() -> None:
    async with client_for(create_app()) as client:
        await put_widgets(client, 5)
        one = await client.get("/widgets", params={"api-version": GA, "maxpagesize": "1"})
        huge = await client.get("/widgets", params={"api-version": GA, "maxpagesize": "50"})
        exact = await client.get("/widgets", params={"api-version": GA, "maxpagesize": "3"})
        rest = await client.get(exact.json()["nextLink"])
    assert [item["name"] for item in one.json()["value"]] == ["w1"]
    assert one.json()["nextLink"].endswith(
        "?api-version=2025-06-01&maxpagesize=1&continuationToken=w1"
    )
    # A request for 50 is capped at the service maximum; fewer than asked is allowed.
    assert [item["name"] for item in huge.json()["value"]] == ["w1", "w2", "w3"]
    assert huge.json()["nextLink"].endswith("&maxpagesize=3&continuationToken=w3")
    assert [item["name"] for item in rest.json()["value"]] == ["w4", "w5"]
    assert "nextLink" not in rest.json()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("top=1", "top"),
        ("MaxPageSize=1", "MaxPageSize"),
        ("maxpagesize=1&maxpagesize=2", "maxpagesize"),
        ("maxpagesize=0", "maxpagesize"),
        ("maxpagesize=-1", "maxpagesize"),
        ("maxpagesize=1.5", "maxpagesize"),
        ("maxpagesize=", "maxpagesize"),
        ("continuationToken=a:b", "continuationToken"),
    ],
    ids=[
        "unsupported",
        "wrong-case",
        "repeated",
        "zero",
        "negative",
        "fraction",
        "blank",
        "bad-token",
    ],
)
async def test_list_rejects_unknown_repeated_and_malformed_query_options(
    query: str, target: str
) -> None:
    async with client_for(create_app()) as client:
        response = await client.get(f"/widgets?api-version={GA}&{query}")
    error = assert_azure_error(response, 400, "InvalidQueryParameterValue")
    assert error["target"] == target


async def start_rebuild(client: httpx.AsyncClient, op_id: str, mode: str = "full") -> None:
    await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
    response = await client.post(
        "/widgets/w1:rebuild",
        params={"api-version": GA},
        json={"mode": mode},
        headers={"Operation-Id": op_id},
    )
    assert response.status_code == 202


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_is_a_post_on_the_status_monitor_that_returns_200_and_canceled() -> None:
    lab = create_app()
    async with client_for(lab) as client:
        await start_rebuild(client, "op-c")
        lab.clock.advance(1.5)
        running = await client.get("/operations/op-c", params={"api-version": GA})
        canceled = await client.post("/operations/op-c:cancel", params={"api-version": GA})
        lab.clock.advance(60)
        later = await client.get("/operations/op-c", params={"api-version": GA})
        retry = await client.post(
            "/widgets/w1:rebuild",
            params={"api-version": GA},
            json={"mode": "full"},
            headers={"Operation-Id": "op-c"},
        )
        missing = await client.post("/operations/nope:cancel", params={"api-version": GA})
    assert running.json()["status"] == "Running"
    assert canceled.status_code == 200
    expected = {"id": "op-c", "kind": "WidgetRebuild", "status": "Canceled"}
    assert canceled.json() == expected
    assert "retry-after" not in canceled.headers
    # Canceled is terminal: it never turns into Succeeded once the scheduled work time passes.
    assert later.json() == expected
    assert "retry-after" not in later.headers
    assert (retry.status_code, retry.json()) == (202, expected)
    assert "retry-after" not in retry.headers
    assert_azure_error(missing, 404, "OperationNotFound")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_never_rewrites_a_terminal_monitor() -> None:
    lab = create_app()
    async with client_for(lab) as client:
        await start_rebuild(client, "op-done", mode="incremental")
        lab.clock.advance(2)
        too_late = await client.post("/operations/op-done:cancel", params={"api-version": GA})
        await start_rebuild(client, "op-twice")
        lab.clock.advance(1)
        once = await client.post("/operations/op-twice:cancel", params={"api-version": GA})
        lab.clock.advance(STATUS_MONITOR_RETENTION_SECONDS)
        twice = await client.post("/operations/op-twice:cancel", params={"api-version": GA})
        lab.clock.advance(1)
        purged = await client.get("/operations/op-twice", params={"api-version": GA})
    assert too_late.status_code == 200
    assert too_late.json()["status"] == "Succeeded"
    assert too_late.json()["result"] == {"widgetName": "w1", "mode": "incremental"}
    assert (
        once.json()
        == twice.json()
        == {
            "id": "op-twice",
            "kind": "WidgetRebuild",
            "status": "Canceled",
        }
    )
    # Retention runs from the moment of cancellation, not from the scheduled completion.
    assert_azure_error(purged, 404, "OperationNotFound")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_poller_stops_when_another_client_cancels_the_operation() -> None:
    lab = create_app()
    async with client_for(lab) as client:

        async def sleep(seconds: float) -> None:
            lab.clock.advance(seconds)
            if lab.clock.now == 4:
                await client.post("/operations/op-x:cancel", params={"api-version": OLD_GA})

        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        initial = await client.post(
            "/widgets/w1:rebuild",
            params={"api-version": GA},
            json={"mode": "full"},
            headers={"Operation-Id": "op-x"},
        )
        monitor, waits = await poll_lro(client, initial, api_version=GA, sleep=sleep)
    assert waits == [2, 2]
    assert monitor == {"id": "op-x", "kind": "WidgetRebuild", "status": "Canceled"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_validation_failure_is_listed_in_details_under_one_top_level_code() -> None:
    async with client_for(create_app()) as client:
        two = await client.put(
            "/widgets/w1", params={"api-version": GA}, json={"color": "purple", "size": "XL"}
        )
        one = await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "purple"})
    error = assert_azure_error(two, 400, "InvalidRequestContent")
    # The contract-frozen parts stay single-valued; the per-field list is diagnostic detail.
    assert error["target"] == "color"
    assert [(d["code"], d["target"]) for d in error["details"]] == [
        ("InvalidRequestContent", "color"),
        ("InvalidRequestContent", "size"),
    ]
    assert error["message"] == error["details"][0]["message"]
    assert all(set(d) == {"code", "message", "target"} for d in error["details"])
    assert "details" not in assert_azure_error(one, 400, "InvalidRequestContent")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_405_allow_lists_every_method_of_the_target_resource() -> None:
    async with client_for(create_app()) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
        widget = await client.post("/widgets/w1", params={"api-version": GA})
        collection = await client.put("/widgets", params={"api-version": GA}, json={})
        monitor = await client.delete("/operations/op-1", params={"api-version": GA})
        action = await client.get("/operations/op-1:cancel", params={"api-version": GA})
        head = await client.head("/widgets/w1", params={"api-version": GA})
    for response, methods in (
        (widget, {"DELETE", "GET", "HEAD", "PUT"}),
        (collection, {"GET", "HEAD"}),
        (monitor, {"GET", "HEAD"}),
    ):
        assert_azure_error(response, 405, "MethodNotAllowed")
        assert set(response.headers["allow"].split(", ")) == methods
    # ':' is not a legal id character, so a GET on the action URL is a bad id, not a 405.
    assert_azure_error(action, 400, "InvalidResourceId")
    assert head.status_code == 200


async def raw_asgi(lab: AzureLab, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    """Call the ASGI app directly; httpx's ASGITransport discards HEAD bodies on its own."""
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        messages.append(dict(message))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": f"api-version={GA}".encode(),
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    await lab.app(scope, receive, send)
    start = messages[0]
    assert start["type"] == "http.response.start"
    headers = {name.decode(): value.decode() for name, value in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], headers, body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_has_the_headers_of_get_and_no_content_at_the_asgi_layer() -> None:
    lab = create_app()
    async with client_for(lab) as client:
        await client.put("/widgets/w1", params={"api-version": GA}, json={"color": "red"})
    get = await raw_asgi(lab, "GET", "/widgets/w1")
    head = await raw_asgi(lab, "HEAD", "/widgets/w1")
    head_list = await raw_asgi(lab, "HEAD", "/widgets")
    head_missing = await raw_asgi(lab, "HEAD", "/widgets/nope")
    assert get[0] == 200
    assert get[2] == b'{"name":"w1","color":"red","size":"M"}'
    # Same status and representation metadata as GET, but the edge sends no content.
    assert (head[0], head[2]) == (200, b"")
    assert head[1]["content-length"] == get[1]["content-length"] == str(len(get[2]))
    assert head[1]["content-type"] == get[1]["content-type"] == "application/json"
    assert (head_list[0], head_list[2]) == (200, b"")
    assert (head_missing[0], head_missing[2]) == (404, b"")
    assert head_missing[1]["x-ms-error-code"] == "WidgetNotFound"
    assert {head[1]["x-ms-request-id"], get[1]["x-ms-request-id"]} == {
        record.request_id for record in lab.log[1:3]
    }
