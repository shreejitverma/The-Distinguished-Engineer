"""Lab for the field-level authorization and non-null error bubbling note."""

import json
import logging
from typing import Any, ClassVar

import httpx
import pytest
import strawberry
from strawberry.exceptions import StrawberryGraphQLError
from strawberry.exceptions.permission_fail_silently_requires_optional import (
    PermissionFailSilentlyRequiresOptionalError,
)
from strawberry.extensions import MaskErrors
from strawberry.permission import BasePermission, PermissionExtension

from api_labs.f20_graphql_json_rpc_and_event_apis.graphql_field_level_authorization_and_partial_error_null_bubbling import (  # noqa: E501
    FIELD_POLICY,
    ROLE_HEADER,
    SCHEMA,
    Ctx,
    DenyByDefault,
    IsAdmin,
    Query,
    build_app,
    build_schema,
    classify,
    draft_status,
    forbidden_paths,
    run,
    run_without_error_propagation,
)

FORBIDDEN = {"code": "FORBIDDEN"}


def error_paths(body: dict[str, Any]) -> list[list[str | int]]:
    return [error["path"] for error in body["errors"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admin_sees_the_whole_object_and_the_salary_resolver_runs() -> None:
    body, trace = await run("{ strictMe { name salary } }", "admin")
    assert body == {"data": {"strictMe": {"name": "user-1", "salary": 100001}}}
    assert trace == ["resolve:name", "check:salary", "resolve:salary"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_denied_non_null_salary_wipes_the_whole_data_and_never_runs_the_resolver() -> None:
    body, trace = await run("{ strictMe { name salary } }", "analyst")
    assert body["data"] is None
    assert len(body["errors"]) == 1
    error = body["errors"][0]
    assert error["path"] == ["strictMe", "salary"]
    assert error["message"] == "salary is restricted"
    assert error["extensions"] == FORBIDDEN
    assert "resolve:salary" not in trace
    assert "resolve:name" in trace
    assert classify(body) == "total-failure"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nullable_parent_absorbs_the_bubble_and_loses_its_healthy_fields() -> None:
    body, trace = await run("{ strictLookup(id: 1) { name salary } }", "analyst")
    assert body["data"] == {"strictLookup": None}
    assert error_paths(body) == [["strictLookup", "salary"]]
    assert "resolve:name" in trace


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bubbling_stops_at_the_nearest_nullable_ancestor_not_before() -> None:
    nullable_list, _ = await run("{ strictDirectory { name salary } }", "analyst")
    assert nullable_list["data"] == {"strictDirectory": None}
    assert error_paths(nullable_list) == [["strictDirectory", 0, "salary"]]

    non_null_list, _ = await run("{ strictTeam { name salary } }", "analyst")
    assert non_null_list["data"] is None
    assert error_paths(non_null_list) == [["strictTeam", 0, "salary"]]

    nullable_items, _ = await run("{ strictRoster { name salary } }", "analyst")
    assert nullable_items["data"] == {"strictRoster": [None, None, None]}
    assert error_paths(nullable_items) == [["strictRoster", i, "salary"] for i in range(3)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nullable_salary_isolates_the_denial_and_keeps_sibling_fields() -> None:
    body, _ = await run("{ nullableMe { name salary } }", "analyst")
    assert body["data"] == {"nullableMe": {"name": "user-1", "salary": None}}
    assert error_paths(body) == [["nullableMe", "salary"]]
    assert forbidden_paths(body) == [("nullableMe", "salary")]
    assert classify(body) == "partial"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_silently_hides_the_denial_from_the_response_but_still_runs_the_check() -> None:
    body, trace = await run("{ silentMe { name salary } }", "analyst")
    assert body == {"data": {"silentMe": {"name": "user-1", "salary": None}}}
    assert "check:salary" in trace
    assert "resolve:salary" not in trace
    assert classify(body) == "ok"
    assert draft_status(body) == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_silent_denial_looks_like_a_real_null_but_a_loud_one_does_not() -> None:
    real_null, _ = await run("{ strictLookup(id: 500) { salary } }", "analyst")
    assert real_null == {"data": {"strictLookup": None}}
    assert forbidden_paths(real_null) == []

    silent, _ = await run("{ silentMe { salary } }", "analyst")
    assert silent == {"data": {"silentMe": {"salary": None}}}
    assert forbidden_paths(silent) == []

    loud, _ = await run("{ nullableMe { salary } }", "analyst")
    assert forbidden_paths(loud) == [("nullableMe", "salary")]


@strawberry.type
class FailSilentlyOnNonNull:
    @strawberry.field(extensions=[PermissionExtension([IsAdmin()], fail_silently=True)])  # type: ignore[untyped-decorator]
    def salary(self) -> int:
        return 1


@strawberry.type
class FailSilentlyQuery:
    @strawberry.field
    def user(self) -> FailSilentlyOnNonNull:
        return FailSilentlyOnNonNull()


@pytest.mark.unit
def test_fail_silently_on_a_non_null_field_is_rejected_when_the_schema_is_built() -> None:
    with pytest.raises(PermissionFailSilentlyRequiresOptionalError, match="non-optional"):
        strawberry.Schema(FailSilentlyQuery)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_non_null_field_stops_later_sync_siblings_from_resolving() -> None:
    _, siblings_first = await run("{ strictMe { name salary } }", "analyst")
    assert siblings_first == ["resolve:name", "check:salary"]

    body, failing_first = await run("{ strictMe { salary name } }", "analyst")
    assert body["data"] is None
    assert failing_first == ["check:salary"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_permissions_start_every_sibling_check_but_record_only_one_error() -> None:
    body, trace = await run("{ asyncStrictTeam(size: 3) { salary name } }", "analyst")
    assert body["data"] is None
    assert error_paths(body) == [["asyncStrictTeam", 0, "salary"]]
    assert trace.count("check:salary") == 3
    assert trace.count("resolve:name") == 3
    assert "resolve:salary" not in trace


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_count_scales_with_list_size_for_nullable_but_stays_one_for_non_null() -> None:
    nullable, nullable_trace = await run("{ nullableTeam(size: 50) { salary } }", "analyst")
    assert len(nullable["errors"]) == 50
    assert nullable_trace.count("check:salary") == 50

    strict, strict_trace = await run("{ strictTeam(size: 50) { salary } }", "analyst")
    assert len(strict["errors"]) == 1
    assert strict_trace.count("check:salary") == 1
    assert len(json.dumps(nullable)) > 10 * len(json.dumps(strict))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_denial_is_logged_at_error_level_by_strawberry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="strawberry.execution"):
        await run("{ nullableTeam(size: 5) { salary } }", "analyst")
    assert len(caplog.records) == 5

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="strawberry.execution"):
        await run("{ strictTeam(size: 5) { salary } }", "analyst")
    assert len(caplog.records) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_status_is_200_for_every_outcome_so_the_body_must_be_classified() -> None:
    cases = [
        ("admin", "{ strictMe { salary } }", "ok", 200),
        ("analyst", "{ nullableMe { name salary } }", "partial", 294),
        ("analyst", "{ strictMe { salary } }", "total-failure", 294),
        ("analyst", "{ nope }", "request-error", None),
    ]
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for role, query, outcome, recommended in cases:
            response = await client.post(
                "/graphql", json={"query": query}, headers={ROLE_HEADER: role}
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
            assert classify(response.json()) == outcome
            assert draft_status(response.json()) == recommended


@pytest.mark.unit
def test_classify_reads_the_body_shape_not_the_status() -> None:
    assert classify({"data": {"a": 1}}) == "ok"
    assert classify({"data": {"a": None}, "errors": [{"message": "x", "path": ["a"]}]}) == "partial"
    assert classify({"data": None, "errors": [{"message": "x", "path": ["a"]}]}) == "total-failure"
    assert classify({"errors": [{"message": "x"}]}) == "request-error"
    assert classify({"data": None, "errors": [{"message": "x"}]}) == "request-error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mask_errors_hides_the_message_and_drops_the_forbidden_code() -> None:
    masked = strawberry.Schema(Query, extensions=[MaskErrors])
    body, _ = await run("{ nullableMe { salary } }", "analyst", schema=masked)
    assert body["errors"] == [
        {
            "message": "Unexpected error.",
            "locations": [{"line": 1, "column": 16}],
            "path": ["nullableMe", "salary"],
        }
    ]
    assert forbidden_paths(body) == []


class NoMessage(BasePermission):
    def has_permission(self, source: Any, info: Any, **kwargs: Any) -> bool:
        return False


@pytest.mark.unit
def test_a_permission_without_a_message_raises_an_error_with_an_empty_message() -> None:
    with pytest.raises(StrawberryGraphQLError) as caught:
        NoMessage().on_unauthorized()
    assert caught.value.message == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_permission_returning_a_truthy_non_bool_fails_closed_and_names_itself() -> None:
    body, _ = await run("{ quirk }", "admin")
    assert body["data"] == {"quirk": None}
    message = body["errors"][0]["message"]
    assert "must return a boolean" in message
    assert "ReturnsTruthyString" in message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_sync_permission_returning_an_awaitable_fails_closed() -> None:
    body, _ = await run("{ awaitableQuirk }", "admin")
    assert body["data"] == {"awaitableQuirk": None}
    assert "returned an awaitable" in body["errors"][0]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_permission_directive_name_leaks_through_sdl_and_introspection() -> None:
    sdl = SCHEMA.as_str()
    assert "directive @isAdmin on FIELD_DEFINITION" in sdl
    assert "salary: Int! @isAdmin" in sdl

    body, _ = await run(
        '{ __schema { directives { name } } __type(name: "StrictUser") { fields { name } } }',
        "analyst",
    )
    assert "isAdmin" in [d["name"] for d in body["data"]["__schema"]["directives"]]
    assert "salary" in [f["name"] for f in body["data"]["__type"]["fields"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unguarded_field_is_default_allow() -> None:
    body, _ = await run("{ openMe { name salary } }", "analyst")
    assert body == {"data": {"openMe": {"name": "user-1", "salary": 100001}}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deny_by_default_guards_a_field_that_has_no_guard_of_its_own() -> None:
    schema = build_schema(deny_by_default=True)
    query = "{ openMe { __typename id name salary } }"

    analyst, analyst_trace = await run(query, "analyst", schema=schema)
    assert analyst["data"] is None
    assert analyst["errors"][0]["extensions"] == {**FORBIDDEN, "coordinate": "OpenUser.salary"}
    assert "resolve:salary" not in analyst_trace

    admin, _ = await run(query, "admin", schema=schema)
    assert admin["data"] == {
        "openMe": {"__typename": "OpenUser", "id": 1, "name": "user-1", "salary": 100001}
    }


class PolicyMissingSalary(DenyByDefault):
    policy: ClassVar = {k: v for k, v in FIELD_POLICY.items() if k != "OpenUser.salary"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deny_by_default_fails_closed_for_a_field_added_without_a_policy_entry() -> None:
    schema = strawberry.Schema(Query, extensions=[PolicyMissingSalary])
    body, trace = await run("{ openMe { name salary } }", "admin", schema=schema)
    assert body["data"] is None
    assert body["errors"][0]["extensions"]["coordinate"] == "OpenUser.salary"
    assert "resolve:salary" not in trace


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabling_error_propagation_returns_null_inside_a_non_null_field() -> None:
    query = "{ strictMe { name salary } }"
    with_directive = await run_without_error_propagation(
        "query @experimental_disableErrorPropagation " + query
    )
    assert with_directive.data == {"strictMe": {"name": "user-1", "salary": None}}
    assert [e.path for e in with_directive.errors or []] == [["strictMe", "salary"]]

    without_directive = await run_without_error_propagation(query)
    assert without_directive.data is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_strawberry_schema_rejects_the_disable_directive_as_unknown() -> None:
    body, _ = await run("query @experimental_disableErrorPropagation { strictMe { name } }")
    assert classify(body) == "request-error"
    assert "Unknown directive" in body["errors"][0]["message"]


@pytest.mark.unit
def test_ctx_is_a_fresh_trace_per_request() -> None:
    assert Ctx(role="admin").trace is not Ctx(role="admin").trace
