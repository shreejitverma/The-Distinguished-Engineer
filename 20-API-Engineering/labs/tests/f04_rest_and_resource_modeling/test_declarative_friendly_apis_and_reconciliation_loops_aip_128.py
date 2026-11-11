"""AIP-128 declarative-friendly resources: allow_missing upserts, uid, etag, and reconciling."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from api_labs.f04_rest_and_resource_modeling.declarative_friendly import (
    ANNOTATIONS_LIMIT_BYTES,
    Action,
    ClusterService,
    DeclarativeClient,
    ReplacedOutOfBandError,
    build_app,
    error_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from api_labs.f04_rest_and_resource_modeling.declarative_friendly import JSONObject

ALL = frozenset({"clusters.get", "clusters.create", "clusters.update", "clusters.delete"})
TOKENS = {
    "admin": ALL,
    "updater": frozenset({"clusters.get", "clusters.update"}),
    "reader": frozenset({"clusters.get"}),
}
ADMIN = {"authorization": "Bearer admin"}
URL = "/v1/clusters/prod-db"
DESIRED: JSONObject = {"display_name": "Prod DB", "node_count": 3}


@pytest.fixture
def service() -> ClusterService:
    return ClusterService(tokens=TOKENS, seed=128)


@pytest_asyncio.fixture
async def http(service: ClusterService) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def upsert(
    http: httpx.AsyncClient, body: JSONObject, principal: str = "admin", **params: str
) -> httpx.Response:
    params.setdefault("allow_missing", "true")
    headers = {"authorization": f"Bearer {principal}"}
    return await http.patch(URL, params=params, json=body, headers=headers)


async def get(http: httpx.AsyncClient) -> JSONObject:
    response = await http.get(URL, headers=ADMIN)
    assert response.status_code == 200, response.text
    body: JSONObject = response.json()
    return body


def settle(service: ClusterService, ticks: int = 10) -> None:
    for _ in range(ticks):
        service.tick()


# --- allow_missing upsert (AIP-134) -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allow_missing_creates_and_ignores_the_update_mask(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    response = await upsert(http, DESIRED, update_mask="display_name")
    assert response.status_code == 200
    settle(service)
    cluster = await get(http)
    # node_count was outside the mask, yet a create applies every field.
    assert (cluster["display_name"], cluster["node_count"]) == ("Prod DB", 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_of_a_missing_resource_without_allow_missing_is_not_found(
    http: httpx.AsyncClient,
) -> None:
    response = await upsert(http, DESIRED, allow_missing="false")
    assert response.status_code == 404
    assert error_status(response) == "NOT_FOUND"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upsert_create_missing_required_field_is_invalid_and_creates_nothing(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    response = await upsert(http, {"display_name": "no node_count"})
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")
    assert service.records == {}
    assert (await http.get(URL, headers=ADMIN)).status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_matching_upsert_returns_the_resource_unchanged(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    before = await get(http)
    service.tick()
    again = await upsert(http, DESIRED)
    assert again.json() == before  # same etag and update_time
    assert service.commits == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_resource_updates_only_masked_fields(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    body: JSONObject = {"display_name": "Renamed", "node_count": 7}
    response = await upsert(http, body, update_mask="display_name")
    assert response.status_code == 200
    settle(service)
    cluster = await get(http)
    assert (cluster["display_name"], cluster["node_count"]) == ("Renamed", 3)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_implied_mask_is_the_populated_fields_and_star_replaces_everything(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    await upsert(http, {"node_count": 4})  # no update_mask: only node_count is populated
    settle(service)
    implied = await get(http)
    assert (implied["display_name"], implied["node_count"]) == ("Prod DB", 4)
    await upsert(http, {"node_count": 4}, update_mask="*")  # full replacement clears the name
    assert (await get(http))["display_name"] == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_update_mask_is_a_no_op(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    response = await upsert(http, {"display_name": "ignored"}, update_mask="")
    assert response.json()["display_name"] == "Prod DB"
    assert service.commits == 1


# --- uid (AIP-148) and output-only fields (AIP-203) --------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uid_is_a_server_assigned_uuid4_that_changes_on_recreate(
    http: httpx.AsyncClient,
) -> None:
    first = (await upsert(http, DESIRED)).json()
    assert uuid.UUID(str(first["uid"])).version == 4
    await http.delete(URL, headers=ADMIN)
    second = (await upsert(http, DESIRED)).json()
    assert second["name"] == first["name"]
    assert second["uid"] != first["uid"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_output_only_fields_in_input_are_ignored_without_error(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    created = (await upsert(http, DESIRED)).json()
    epoch = "1970-01-01T00:00:00Z"
    forged = {**DESIRED, "uid": "forged", "reconciling": False, "create_time": epoch}
    response = await upsert(http, forged, update_mask="uid,create_time,node_count")
    assert response.status_code == 200
    assert response.json()["uid"] == created["uid"]
    assert response.json()["create_time"] == created["create_time"]
    assert response.json()["reconciling"] is True  # still 0 of 3 nodes
    assert service.commits == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_field_is_rejected(http: httpx.AsyncClient) -> None:
    response = await upsert(http, {**DESIRED, "is_admin": True})
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")


# --- etag (AIP-154) -----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_etag_is_quoted_and_mirrored_in_the_etag_header(http: httpx.AsyncClient) -> None:
    response = await upsert(http, DESIRED)
    etag = response.json()["etag"]
    assert re.fullmatch(r'"[0-9a-f]{16}"', str(etag))
    assert response.headers["etag"] == etag


@pytest.mark.unit
@pytest.mark.asyncio
async def test_matching_etag_permits_stale_etag_aborts_and_absent_etag_permits(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    current = await get(http)
    ok = await upsert(http, {"display_name": "A", "etag": current["etag"]})
    assert ok.status_code == 200
    stale = await upsert(http, {"display_name": "B", "etag": current["etag"]})
    assert (stale.status_code, error_status(stale)) == (409, "ABORTED")
    assert (await get(http))["display_name"] == "A"
    blind = await upsert(http, {"display_name": "C"})
    assert blind.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconciler_progress_changes_the_etag(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    created = (await upsert(http, DESIRED)).json()
    service.tick()
    progressed = await get(http)
    assert (progressed["node_count"], progressed["reconciling"]) == (1, True)
    assert progressed["etag"] != created["etag"]
    assert progressed["update_time"] == created["update_time"]  # system change, not a user one


@pytest.mark.unit
@pytest.mark.asyncio
async def test_etag_on_upsert_of_a_missing_resource_aborts(http: httpx.AsyncClient) -> None:
    response = await upsert(http, {**DESIRED, "etag": '"0123456789abcdef"'})
    assert (response.status_code, error_status(response)) == (409, "ABORTED")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_already_exists_and_aborted_share_409_but_not_status(
    http: httpx.AsyncClient,
) -> None:
    params = {"cluster_id": "prod-db"}
    first = await http.post("/v1/clusters", params=params, json=DESIRED, headers=ADMIN)
    assert first.status_code == 200
    duplicate = await http.post("/v1/clusters", params=params, json=DESIRED, headers=ADMIN)
    stale = await upsert(http, {"display_name": "x", "etag": '"0000000000000000"'})
    assert duplicate.status_code == stale.status_code == 409
    assert (error_status(duplicate), error_status(stale)) == ("ALREADY_EXISTS", "ABORTED")


# --- delete (AIP-135) ---------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_allow_missing_is_a_no_op_that_ignores_the_etag(
    http: httpx.AsyncClient,
) -> None:
    params = {"allow_missing": "true", "etag": '"stale"'}
    response = await http.delete(URL, params=params, headers=ADMIN)
    assert (response.status_code, response.json()) == (200, {})
    strict = await http.delete(URL, headers=ADMIN)
    assert (strict.status_code, error_status(strict)) == (404, "NOT_FOUND")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_with_a_stale_etag_aborts_and_keeps_the_resource(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    created = (await upsert(http, DESIRED)).json()
    service.tick()
    stale = await http.delete(URL, params={"etag": str(created["etag"])}, headers=ADMIN)
    assert (stale.status_code, error_status(stale)) == (409, "ABORTED")
    fresh = await get(http)
    done = await http.delete(URL, params={"etag": str(fresh["etag"])}, headers=ADMIN)
    assert done.status_code == 200
    assert service.records == {}


# --- validate_only (AIP-163) --------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_create_previews_without_identity_or_effect(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    response = await upsert(http, DESIRED, validate_only="true")
    assert response.status_code == 200
    assert "uid" not in response.json()
    assert response.json()["reconciling"] is True
    assert (service.records, service.commits) == ({}, 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_update_fails_exactly_when_the_real_update_would(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    created = (await upsert(http, DESIRED)).json()
    settle(service)
    current = await get(http)
    preview = await upsert(http, {"node_count": 5, "etag": current["etag"]}, validate_only="true")
    assert (preview.status_code, preview.json()["reconciling"]) == (200, True)
    assert await get(http) == current
    stale = await upsert(http, {"node_count": 5, "etag": created["etag"]}, validate_only="true")
    assert (stale.status_code, error_status(stale)) == (409, "ABORTED")
    assert service.commits == 1


# --- reconciling (AIP-128) ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_returns_current_state_while_reconciling(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    await upsert(http, {"node_count": 5})
    service.tick()
    midway = await get(http)
    assert (midway["node_count"], midway["reconciling"]) == (4, True)
    service.tick()
    done = await get(http)
    assert (done["node_count"], done["reconciling"]) == (5, False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scale_down_converges_one_node_per_tick_toward_the_intent(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    await upsert(http, {"node_count": 1})
    service.tick()
    midway = await get(http)
    assert (midway["node_count"], midway["reconciling"]) == (2, True)
    service.tick()
    done = await get(http)
    assert (done["node_count"], done["reconciling"]) == (1, False)
    service.tick()
    assert (await get(http))["node_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_system_caused_drift_also_sets_reconciling(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    settle(service)
    service.lose_nodes("prod-db", 2)
    drifted = await get(http)
    assert (drifted["node_count"], drifted["reconciling"]) == (1, True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconciler_is_level_based_and_skips_superseded_intent(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, {"node_count": 1})
    settle(service)
    await upsert(http, {"node_count": 5})
    observed: list[int] = []
    service.tick()
    observed.append(service.records["prod-db"].current_nodes)
    await upsert(http, {"node_count": 3})  # superseded before 5 was ever reached
    for _ in range(6):
        service.tick()
        observed.append(service.records["prod-db"].current_nodes)
    assert observed == [2, 3, 3, 3, 3, 3, 3]


# --- the declarative client ---------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_converge_from_nothing_is_one_write_and_rerun_is_zero(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    client = DeclarativeClient(http, "admin")
    actions = await client.converge("prod-db", DESIRED, service.tick)
    assert actions == [Action.CREATE, Action.WAIT, Action.WAIT, Action.WAIT, Action.NOOP]
    assert (client.write_requests, service.commits) == (1, 1)
    assert await client.converge("prod-db", DESIRED, service.tick) == [Action.NOOP]
    assert client.write_requests == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_waits_instead_of_rewriting_while_reconciling(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    client = DeclarativeClient(http, "admin")
    await client.converge("prod-db", DESIRED, service.tick)
    wider = {**DESIRED, "node_count": 6}
    actions = await client.converge("prod-db", wider, service.tick)
    assert actions == [Action.UPDATE, Action.WAIT, Action.WAIT, Action.WAIT, Action.NOOP]
    assert client.write_requests == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_writer_causes_aborted_then_reread_and_retry(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    client = DeclarativeClient(http, "admin")
    await client.converge("prod-db", DESIRED, service.tick)
    renamed = {**DESIRED, "display_name": "Prod DB v2"}
    plan = await client.plan("prod-db", renamed)
    assert (plan.action, plan.update_mask) == (Action.UPDATE, ("display_name",))
    await upsert(http, {"display_name": "someone else"})  # lands between read and write
    lost = await client.apply("prod-db", renamed, plan)
    assert (lost.status_code, error_status(lost)) == (409, "ABORTED")
    assert (await get(http))["display_name"] == "someone else"
    assert await client.converge("prod-db", renamed, service.tick) == [Action.UPDATE, Action.NOOP]
    assert (await get(http))["display_name"] == "Prod DB v2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_does_not_fight_system_repair(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    client = DeclarativeClient(http, "admin")
    await client.converge("prod-db", DESIRED, service.tick)
    service.lose_nodes("prod-db", 3)
    actions = await client.converge("prod-db", DESIRED, service.tick)
    assert actions == [Action.WAIT] * 3 + [Action.NOOP]
    assert client.write_requests == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_detects_out_of_band_replacement_by_uid(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    client = DeclarativeClient(http, "admin")
    await client.converge("prod-db", DESIRED, service.tick)
    await http.delete(URL, headers=ADMIN)
    await upsert(http, DESIRED)  # same name, different resource
    settle(service)
    with pytest.raises(ReplacedOutOfBandError, match="prod-db"):
        await client.plan("prod-db", DESIRED)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_destroy_is_idempotent(http: httpx.AsyncClient, service: ClusterService) -> None:
    client = DeclarativeClient(http, "admin")
    await client.converge("prod-db", DESIRED, service.tick)
    assert [(await client.destroy("prod-db")).status_code for _ in range(2)] == [200, 200]
    assert client.known_uids == {}
    assert service.commits == 2  # the create and the first delete; the repeat is a no-op


# --- authorization ------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_permission_is_checked_before_existence(http: httpx.AsyncClient) -> None:
    response = await upsert(http, DESIRED, principal="reader", allow_missing="false")
    assert (response.status_code, error_status(response)) == (403, "PERMISSION_DENIED")
    anonymous = await http.patch(URL, json=DESIRED)
    assert (anonymous.status_code, error_status(anonymous)) == (401, "UNAUTHENTICATED")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_permission_alone_can_create_through_allow_missing(
    service: ClusterService,
) -> None:
    for requires_create, expected in ((False, 200), (True, 403)):
        service = ClusterService(tokens=TOKENS, upsert_requires_create=requires_create)
        transport = httpx.ASGITransport(app=build_app(service))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await upsert(client, DESIRED, principal="updater")
        assert response.status_code == expected
        assert len(service.records) == (1 if expected == 200 else 0)


# --- limits -------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_annotation_budget_is_256_kib_of_keys_plus_values(http: httpx.AsyncClient) -> None:
    key = "deploy.example.com/run"
    exact = {key: "x" * (ANNOTATIONS_LIMIT_BYTES - len(key))}
    assert (await upsert(http, {**DESIRED, "annotations": exact})).status_code == 200
    over = {key: "x" * (ANNOTATIONS_LIMIT_BYTES - len(key) + 1)}
    response = await upsert(http, {"annotations": over}, update_mask="annotations")
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_annotation_budget_counts_utf8_bytes_not_characters(http: httpx.AsyncClient) -> None:
    key = "deploy.example.com/run"
    two_bytes = "\u00e9"  # one character, two UTF-8 bytes
    fits = (ANNOTATIONS_LIMIT_BYTES - len(key)) // 2
    assert (
        await upsert(http, {**DESIRED, "annotations": {key: two_bytes * fits}})
    ).status_code == 200
    over = {key: two_bytes * (fits + 1)}  # about 131 thousand characters but 262146 bytes
    response = await upsert(http, {"annotations": over}, update_mask="annotations")
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "cluster_id"),
    [
        ({**DESIRED, "display_name": "x" * 64}, "prod-db"),
        ({**DESIRED, "annotations": {"run": "1"}}, "prod-db"),
        ({**DESIRED, "node_count": True}, "prod-db"),
        ({**DESIRED, "annotations": ["a.example.com/k"]}, "prod-db"),
        ({**DESIRED, "annotations": {"a.example.com/k": 1}}, "prod-db"),
        (DESIRED, "Prod_DB"),
    ],
    ids=[
        "display-name-64",
        "unnamespaced-annotation",
        "bool-node-count",
        "annotations-not-object",
        "annotation-value-not-string",
        "bad-id",
    ],
)
async def test_invalid_input_is_rejected(
    http: httpx.AsyncClient, body: JSONObject, cluster_id: str
) -> None:
    response = await http.patch(
        f"/v1/clusters/{cluster_id}", params={"allow_missing": "true"}, json=body, headers=ADMIN
    )
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_method_validates_the_client_assigned_id(http: httpx.AsyncClient) -> None:
    params = {"cluster_id": "x"}
    response = await http.post("/v1/clusters", params=params, json=DESIRED, headers=ADMIN)
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "content"),
    [
        ({"update_mask": "node_count,owner"}, b'{"node_count": 2}'),
        ({"allow_missing": "yes"}, b'{"node_count": 2}'),
        ({}, b"not json"),
        ({}, b"[1, 2]"),
    ],
    ids=["unknown-mask-path", "bad-boolean", "not-json", "not-object"],
)
async def test_malformed_requests_are_invalid_argument(
    http: httpx.AsyncClient, params: dict[str, str], content: bytes
) -> None:
    await upsert(http, DESIRED)
    headers = {**ADMIN, "content-type": "application/json"}
    response = await http.patch(URL, params=params, content=content, headers=headers)
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_only_delete_keeps_the_resource(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    await upsert(http, DESIRED)
    response = await http.delete(URL, params={"validate_only": "true"}, headers=ADMIN)
    assert response.status_code == 200
    assert list(service.records) == ["prod-db"]
    assert service.commits == 1


@pytest.mark.unit
def test_error_status_reads_only_the_aip_193_envelope() -> None:
    assert error_status(httpx.Response(200, json=[1])) is None
    assert error_status(httpx.Response(200, json={"error": "flat"})) is None
    assert error_status(httpx.Response(409, json={"error": {"status": 10}})) is None
    assert error_status(httpx.Response(409, json={"error": {"status": "ABORTED"}})) == "ABORTED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_converge_rereads_after_a_race_inside_its_own_loop(service: ClusterService) -> None:
    raced: list[str] = []

    async def concurrent_writer(request: httpx.Request) -> None:
        if request.method == "PATCH" and not raced:
            raced.append("display_name")  # another writer commits between our GET and PATCH
            service.update(
                "admin",
                "prod-db",
                {"display_name": "someone else"},
                update_mask=None,
                allow_missing=False,
                validate_only=False,
            )

    transport = httpx.ASGITransport(app=build_app(service))
    hooks = {"request": [concurrent_writer]}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", event_hooks=hooks
    ) as http:
        raced.append("setup")  # the hook stays quiet while the test creates the cluster
        await upsert(http, DESIRED)
        settle(service)
        raced.clear()
        client = DeclarativeClient(http, "admin")
        renamed = {**DESIRED, "display_name": "Prod DB v2"}
        actions = await client.converge("prod-db", renamed, service.tick)
        assert actions == [Action.UPDATE, Action.UPDATE, Action.NOOP]
        assert client.write_requests == 2
        assert (await get(http))["display_name"] == "Prod DB v2"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_converge_gives_up_after_max_rounds(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    client = DeclarativeClient(http, "admin")
    with pytest.raises(RuntimeError, match="no convergence in 3 rounds") as excinfo:
        await client.converge("prod-db", DESIRED, lambda: None, max_rounds=3)
    assert str(excinfo.value).count("Action.") == 3  # CREATE, WAIT, WAIT
    assert (client.write_requests, service.records["prod-db"].current_nodes) == (1, 0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_converge_raises_on_a_non_retryable_error(http: httpx.AsyncClient) -> None:
    client = DeclarativeClient(http, "reader")
    with pytest.raises(httpx.HTTPStatusError, match="403"):
        await client.converge("prod-db", DESIRED, lambda: None)
    destroyed = await client.destroy("prod-db")
    assert (destroyed.status_code, error_status(destroyed)) == (403, "PERMISSION_DENIED")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicated_control_parameter_is_rejected_not_last_wins(
    http: httpx.AsyncClient, service: ClusterService
) -> None:
    polluted = {"allow_missing": ["false", "true"]}  # ?allow_missing=false&allow_missing=true
    response = await http.patch(URL, params=polluted, json=DESIRED, headers=ADMIN)
    assert (response.status_code, error_status(response)) == (400, "INVALID_ARGUMENT")
    assert service.records == {}
