"""PUT full replacement, omitted-field defaulting, and client-supplied IDs (RFC 9110, AIP-134)."""

import httpx
import pytest
from fastapi import FastAPI
from starlette.applications import Starlette

from api_labs.f04_rest_and_resource_modeling.put_replacement import (
    LosslessWidgetV1,
    OmittedFields,
    StoredWidget,
    WidgetService,
    WidgetV1,
    build_app,
    build_typed_body_app,
    parse_entity_tags,
    preconditions_hold,
)

FULL = {"display_name": "Gear", "color": "red", "labels": {"team": "payments"}}


def client_for(app: Starlette | FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def seeded(omitted: OmittedFields = OmittedFields.DEFAULT) -> WidgetService:
    service = WidgetService(omitted=omitted)
    service.widgets["gear"] = StoredWidget("gear", "Gear", "red", {"team": "payments"}, 1)
    return service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_creates_with_201_then_replaces_with_200() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        created = await client.put("/widgets/gear", json=FULL)
        replaced = await client.put("/widgets/gear", json={**FULL, "color": "blue"})
    assert (created.status_code, replaced.status_code) == (201, 200)
    assert created.json()["revision"] == 1
    assert replaced.json() == {**FULL, "color": "blue", "id": "gear", "revision": 2, "etag": '"r2"'}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_fields_reset_to_schema_defaults_on_replace() -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json={"display_name": "Gear"})
    assert response.status_code == 200
    assert response.json()["color"] == "black"
    assert response.json()["labels"] == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_old_client_round_trip_wipes_new_field_despite_matching_if_match() -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        fetched = await client.get("/widgets/gear")
        old = WidgetV1.model_validate(fetched.json())  # extra='ignore' drops `labels`
        old.color = "blue"
        response = await client.put(
            "/widgets/gear",
            json=old.model_dump(),
            headers={"if-match": fetched.headers["etag"]},
        )
    assert "labels" not in old.model_dump()
    assert response.status_code == 200  # the entity tag matched: no concurrent writer existed
    assert service.widgets["gear"].labels == {}  # yet the payments label is gone
    assert service.widgets["gear"].color == "blue"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reject_policy_turns_the_silent_wipe_into_a_422() -> None:
    service = seeded(OmittedFields.REJECT)
    async with client_for(build_app(service)) as client:
        fetched = await client.get("/widgets/gear")
        old = WidgetV1.model_validate(fetched.json())
        response = await client.put("/widgets/gear", json=old.model_dump())
    assert response.status_code == 422
    assert response.json()["detail"] == {"missing": ["labels"]}
    assert service.widgets["gear"].labels == {"team": "payments"}
    assert service.writes == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lossless_old_client_round_trips_members_it_does_not_know() -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        fetched = await client.get("/widgets/gear")
        old = LosslessWidgetV1.model_validate(fetched.json())
        old.color = "blue"
        response = await client.put("/widgets/gear", json=old.model_dump())
    assert response.status_code == 200
    assert service.widgets["gear"].labels == {"team": "payments"}
    assert service.widgets["gear"].color == "blue"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preserve_policy_keeps_omitted_fields_so_clearing_needs_an_explicit_empty() -> None:
    service = seeded(OmittedFields.PRESERVE)
    async with client_for(build_app(service)) as client:
        omitted = await client.put("/widgets/gear", json={"display_name": "Gear 2"})
        kept = service.widgets["gear"]
        cleared = await client.put("/widgets/gear", json={"display_name": "Gear 2", "labels": {}})
    assert omitted.status_code == 200
    assert (kept.color, kept.labels) == ("red", {"team": "payments"})
    assert cleared.status_code == 200
    assert service.widgets["gear"].labels == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preserve_policy_uses_defaults_when_creating() -> None:
    service = WidgetService(omitted=OmittedFields.PRESERVE)
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json={"display_name": "Gear"})
    assert response.status_code == 201
    assert (response.json()["color"], response.json()["labels"]) == ("black", {})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_identical_put_is_a_no_op_that_keeps_the_revision() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        first = await client.put("/widgets/gear", json=FULL)
        second = await client.put("/widgets/gear", json=FULL)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["revision"] == 1
    assert service.writes == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_put_response_has_no_etag_header_but_get_does() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        put = await client.put("/widgets/gear", json={"display_name": "Gear"})
        get = await client.get("/widgets/gear")
    assert "etag" not in put.headers  # defaults and output-only fields transformed the content
    assert put.json()["etag"] == '"r1"'
    assert get.headers["etag"] == '"r1"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_of_a_missing_widget_is_404() -> None:
    async with client_for(build_app(WidgetService())) as client:
        response = await client.get("/widgets/nope")
    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_allow_missing_decides_whether_a_typo_creates_a_stray_resource() -> None:
    strict, lenient = seeded(), seeded()
    strict.allow_missing = False
    async with client_for(build_app(strict)) as client:
        refused = await client.put("/widgets/gaer", json=FULL)
    async with client_for(build_app(lenient)) as client:
        stray = await client.put("/widgets/gaer", json=FULL)
    assert refused.status_code == 404
    assert sorted(strict.widgets) == ["gear"]
    assert stray.status_code == 201
    assert sorted(lenient.widgets) == ["gaer", "gear"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_404_is_returned_instead_of_412_when_creation_is_disallowed() -> None:
    service = WidgetService(allow_missing=False)
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json=FULL, headers={"if-match": '"r1"'})
    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_star_refuses_to_create() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json=FULL, headers={"if-match": "*"})
    assert response.status_code == 412
    assert service.widgets == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_none_match_star_makes_concurrent_creates_first_writer_wins() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        alice = await client.put("/widgets/gear", json=FULL, headers={"if-none-match": "*"})
        bob = await client.put(
            "/widgets/gear", json={"display_name": "Bob"}, headers={"if-none-match": "*"}
        )
    assert (alice.status_code, bob.status_code) == (201, 412)
    assert service.widgets["gear"].display_name == "Gear"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unconditional_second_create_silently_replaces_the_first() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        alice = await client.put("/widgets/gear", json=FULL)
        bob = await client.put("/widgets/gear", json={"display_name": "Bob"})
    assert (alice.status_code, bob.status_code) == (201, 200)
    assert service.widgets["gear"].labels == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retried_conditional_put_gets_412_although_its_change_was_applied() -> None:
    service = seeded()
    body, headers = {**FULL, "color": "blue"}, {"if-match": '"r1"'}
    async with client_for(build_app(service)) as client:
        first = await client.put("/widgets/gear", json=body, headers=headers)  # response "lost"
        retry = await client.put("/widgets/gear", json=body, headers=headers)
    assert first.status_code == 200
    assert retry.status_code == 412
    assert service.widgets["gear"].color == "blue"
    assert service.writes == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_uses_strong_comparison_and_rejects_a_weak_tag() -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        weak = await client.put("/widgets/gear", json=FULL, headers={"if-match": 'W/"r1"'})
        listed = await client.put(
            "/widgets/gear", json={**FULL, "color": "blue"}, headers={"if-match": '"r0", "r1"'}
        )
    assert weak.status_code == 412
    assert listed.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_field_lines_are_combined_into_one_list() -> None:
    service = seeded()
    headers = [("if-match", '"r0"'), ("if-match", '"r1"')]
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json=FULL | {"color": "blue"}, headers=headers)
    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_none_match_tag_list_uses_weak_comparison_on_put() -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        matched = await client.put("/widgets/gear", json=FULL, headers={"if-none-match": 'W/"r1"'})
        other = await client.put(
            "/widgets/gear", json={**FULL, "color": "blue"}, headers={"if-none-match": '"r9"'}
        )
    assert matched.status_code == 412
    assert other.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize("header", ["if-match", "if-none-match"])
@pytest.mark.asyncio
async def test_malformed_entity_tag_list_is_400(header: str) -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json=FULL, headers={header: "r1"})
    assert response.status_code == 400
    assert service.writes == 0


@pytest.mark.unit
def test_entity_tag_lists_accept_empty_elements_and_reject_bare_tokens() -> None:
    assert parse_entity_tags(' , "a" ,, W/"b" ') == ['"a"', 'W/"b"']
    assert parse_entity_tags('"a", b') is None
    # RFC 9110 Section 8.8.3: etagc includes obs-text (%x80-FF) but never DQUOTE or SP.
    assert parse_entity_tags('"r\u00e9"') == ['"r\u00e9"']
    assert parse_entity_tags('"a b"') is None
    assert parse_entity_tags('"a"b"') is None  # an inner DQUOTE (%x22) is not an etagc
    assert parse_entity_tags('"a\x7fb"') is None  # DEL is neither %x21-7E nor obs-text
    current = StoredWidget("gear", "Gear", "red", {}, 1)
    assert preconditions_hold(None, '"r9"', None) is True
    assert preconditions_hold(None, None, current) is True
    assert preconditions_hold('"r1"', None, None) is False
    # RFC 9110 Section 13.2.2: a true If-Match continues to step 3 (If-None-Match).
    assert preconditions_hold('"r1"', None, current) is True
    assert preconditions_hold('"r1"', '"r1"', current) is False
    assert preconditions_hold("*", "*", current) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("widget_id", "status"),
    [
        ("a", 201),
        ("w-1", 201),
        ("a" * 63, 201),
        ("a" * 64, 400),
        ("Gear", 400),
        ("1gear", 400),
        ("gear-", 400),
        ("ge_ar", 400),
        ("a.b", 400),  # no dot, so no `.` or `..` path segments
        ("g\u00e9ar", 400),  # ASCII only, so no Unicode confusables
    ],
)
@pytest.mark.asyncio
async def test_client_supplied_ids_follow_the_aip_122_pattern(widget_id: str, status: int) -> None:
    async with client_for(build_app(WidgetService())) as client:
        response = await client.put(f"/widgets/{widget_id}", json=FULL)
    assert response.status_code == status


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_range_on_put_is_rejected_with_400() -> None:
    service = seeded()
    async with client_for(build_app(service)) as client:
        response = await client.put(
            "/widgets/gear", json=FULL, headers={"content-range": "bytes 0-9/100"}
        )
    assert response.status_code == 400
    assert service.writes == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_output_only_members_in_the_body_are_ignored() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        response = await client.put(
            "/widgets/gear", json={**FULL, "id": "gear", "revision": 999, "etag": '"forged"'}
        )
    assert response.status_code == 201
    assert response.json()["revision"] == 1
    assert response.json()["etag"] == '"r1"'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_body_id_that_contradicts_the_target_uri_is_409() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json={**FULL, "id": "other"})
    assert response.status_code == 409
    assert service.widgets == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_member_is_422_because_the_body_forbids_extras() -> None:
    service = WidgetService()
    async with client_for(build_app(service)) as client:
        response = await client.put("/widgets/gear", json={**FULL, "is_admin": True})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.unit
@pytest.mark.parametrize("content", [b"{not json", b'["a"]'])
@pytest.mark.asyncio
async def test_non_json_and_non_object_bodies_are_400(content: bytes) -> None:
    async with client_for(build_app(WidgetService())) as client:
        response = await client.put("/widgets/gear", content=content)
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_precondition_is_reported_before_body_validation() -> None:
    lab, typed = seeded(), seeded()
    stale = {"if-match": '"r0"'}
    async with client_for(build_app(lab)) as client:
        rfc_order = await client.put("/widgets/gear", json={"color": 7}, headers=stale)
    async with client_for(build_typed_body_app(typed)) as client:
        typed_order = await client.put("/widgets/gear", json={"color": 7}, headers=stale)
    assert rfc_order.status_code == 412
    assert typed_order.status_code == 422  # FastAPI validated the body before the handler ran
    assert lab.writes == typed.writes == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_typed_body_app_fills_defaults_before_the_handler_sees_the_body() -> None:
    service = seeded()
    async with client_for(build_typed_body_app(service)) as client:
        stale = await client.put(
            "/widgets/gear", json={"display_name": "Gear"}, headers={"if-match": '"r0"'}
        )
        response = await client.put(
            "/widgets/gear", json={"display_name": "Gear"}, headers={"if-match": '"r1"'}
        )
    assert stale.status_code == 412
    assert response.status_code == 200
    assert response.json()["labels"] == {}
    assert service.widgets["gear"].color == "black"


@pytest.mark.unit
def test_pydantic_default_extra_is_ignore_so_unknown_members_vanish() -> None:
    payload = {"display_name": "Gear", "color": "red", "labels": {"team": "payments"}}
    assert WidgetV1.model_config.get("extra") is None  # unset -> pydantic's default 'ignore'
    assert WidgetV1.model_validate(payload).model_dump(exclude_none=True) == {
        "display_name": "Gear",
        "color": "red",
    }
    lossless = LosslessWidgetV1.model_validate(payload).model_dump(exclude_none=True)
    assert lossless["labels"] == {"team": "payments"}
