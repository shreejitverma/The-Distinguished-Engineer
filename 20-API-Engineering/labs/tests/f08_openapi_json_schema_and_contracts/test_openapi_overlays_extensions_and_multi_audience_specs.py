"""Lab: OpenAPI extensions, Overlay 1.0 to 1.2 actions, and public-portal publication."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from pydantic import BaseModel
from referencing.exceptions import PointerToNowhere

from api_labs.f08_openapi_json_schema_and_contracts.multi_audience_specs import (
    LAB_OPERATOR_KEY,
    PUBLIC_PORTAL_OVERLAY,
    JsonPath,
    JsonPathSyntaxError,
    JsonPathUnsupportedError,
    OverlayError,
    apply_overlay,
    build_app,
    build_public_spec,
    dangling_required,
    digest,
    extension_inventory,
    find_leaks,
    normalized_path,
    prune_for_publication,
)

ADMIN_PATH = "/admin/orders/{order_id}/refund-override"
OK: dict[str, Any] = {"200": {"description": "OK"}}


def paths_of(nodes: list[Any]) -> list[str]:
    return [normalized_path(n.location) for n in nodes]


def overlay(*actions: dict[str, Any], version: str = "1.2.0") -> dict[str, Any]:
    return {"overlay": version, "info": {"title": "t", "version": "1"}, "actions": list(actions)}


def spec(paths: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": paths, **extra}


def traits_doc() -> dict[str, Any]:
    # Overlay 1.1.0 Section 4.5.5 and 1.2.0 Section 4.6.5 source description.
    return spec(
        {
            "/items": {"get": {"x-oai-traits": ["paged"], "responses": OK}},
            "/items/{id}/subitems": {
                "get": {
                    "x-oai-traits": ["paged"],
                    "parameters": [{"name": "id", "in": "path", "required": True}],
                    "responses": OK,
                }
            },
            "/other": {"get": {"responses": OK}},
        }
    )


def copy_doc() -> dict[str, Any]:
    # Overlay 1.2.0 Section 4.6.6 source description.
    return spec(
        {"/items": {"get": {"responses": OK}}, "/some-items": {"delete": {"responses": OK}}}
    )


@pytest.fixture
def app() -> FastAPI:
    return build_app()


# ---------------------------------------------------------------- RFC 9535 JSONPath


@pytest.mark.unit
def test_overlay_1_0_traits_example_is_not_well_formed_rfc_9535() -> None:
    # Overlay 1.0.0 Section 4.5.5 uses a hyphenated shorthand; RFC 9535 Section 2.5.1.1
    # member-name-shorthand allows only ALPHA, DIGIT, '_', and non-ASCII.
    with pytest.raises(JsonPathSyntaxError, match="'-' is not allowed"):
        JsonPath("$.paths.*.get[?@.x-oai-traits.paged]")


@pytest.mark.unit
def test_overlay_1_1_traits_example_selects_both_paged_operations() -> None:
    nodes = JsonPath("$.paths[?(@.get['x-oai-traits'][?(@ == 'paged')])].get").find(traits_doc())
    assert paths_of(nodes) == [
        "$['paths']['/items']['get']",
        "$['paths']['/items/{id}/subitems']['get']",
    ]


@pytest.mark.unit
def test_existence_test_matches_a_false_valued_extension() -> None:
    doc = spec({"/a": {"get": {"x-internal": False}, "post": {"x-internal": True}, "put": {}}})
    exists = JsonPath("$.paths.*[?@['x-internal']]").find(doc)
    equals_true = JsonPath("$.paths.*[?@['x-internal'] == true]").find(doc)
    assert paths_of(exists) == ["$['paths']['/a']['get']", "$['paths']['/a']['post']"]
    assert paths_of(equals_true) == ["$['paths']['/a']['post']"]


@pytest.mark.unit
def test_json_true_is_not_equal_to_the_number_one() -> None:
    doc = spec({"/a": {"post": {"x-internal": True}, "get": {"x-internal": 1}}})
    assert paths_of(JsonPath("$.paths.*[?@['x-internal'] == 1]").find(doc)) == [
        "$['paths']['/a']['get']"
    ]


@pytest.mark.unit
def test_comparisons_with_missing_members_follow_rfc_9535() -> None:
    rows = [{"a": 1}, {"b": 2}, {}]
    assert paths_of(JsonPath("$[?@.a == @.c]").find(rows)) == ["$[1]", "$[2]"]
    assert paths_of(JsonPath("$[?@.a == null]").find(rows)) == []
    assert paths_of(JsonPath("$[?@.a != 2]").find(rows)) == ["$[0]", "$[1]", "$[2]"]


@pytest.mark.unit
def test_wildcard_selects_member_values_and_descendants_find_refs() -> None:
    doc = {"info": {"title": "t", "version": "1"}, "a": [{"$ref": "#/x"}, {"b": {"$ref": "#/y"}}]}
    assert [n.value for n in JsonPath("$.info.*").find(doc)] == ["t", "1"]
    assert [n.value for n in JsonPath("$..['$ref']").find(doc)] == ["#/x", "#/y"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    ["$.a.", "$[01]", "$['a", "$.a ", "$[?@.a == 1 &&]", "$[?!@.a == 1]", "$[?@..a == 1]"],
    ids=["dangling-dot", "leading-zero", "unterminated", "trailing-blank", "dangling-and",
         "not-comparison", "non-singular-comparison"],
)  # fmt: skip
def test_malformed_queries_raise_before_evaluation(query: str) -> None:
    with pytest.raises(JsonPathSyntaxError):
        JsonPath(query)


@pytest.mark.unit
@pytest.mark.parametrize("query", ["$.tags[0:2]", "$.paths[?length(@) > 1]"])
def test_slices_and_functions_are_reported_as_unsupported(query: str) -> None:
    with pytest.raises(JsonPathUnsupportedError):
        JsonPath(query)


@pytest.mark.unit
def test_normalized_paths_use_bracket_notation_and_escapes() -> None:
    doc = spec({"/v1/orders": {"post": {}}})
    assert paths_of(JsonPath("$.paths['/v1/orders'].post").find(doc)) == [
        "$['paths']['/v1/orders']['post']"
    ]
    assert normalized_path(("it's", "a\nb", 0)) == "$['it\\'s']['a\\nb'][0]"


# ---------------------------------------------------------------- Overlay semantics


@pytest.mark.unit
def test_actions_apply_in_order_so_remove_then_recreate_works() -> None:
    doc = spec({"/a": {"get": {"summary": "old", "x-internal": True}}})
    result = apply_overlay(
        doc,
        overlay(
            {"target": "$.paths['/a'].get", "remove": True},
            {"target": "$.paths['/a']", "update": {"get": {"summary": "new"}}},
        ),
    )
    assert result.document["paths"]["/a"] == {"get": {"summary": "new"}}


@pytest.mark.unit
def test_update_merges_objects_concatenates_arrays_and_keeps_null() -> None:
    doc = spec({}, **{"x-t": {"a": 1, "b": [1], "c": {"d": 1}, "keep": True}})
    update = {"a": 2, "b": [2], "c": {"e": 2}, "new": None}
    result = apply_overlay(doc, overlay({"target": "$['x-t']", "update": update}))
    # Unlike RFC 7396 merge patch, null is inserted, not a deletion, and arrays concatenate.
    assert result.document["x-t"] == {
        "a": 2,
        "b": [1, 2],
        "c": {"d": 1, "e": 2},
        "keep": True,
        "new": None,
    }


@pytest.mark.unit
def test_structured_example_null_members_are_incompatible_with_objects() -> None:
    # Overlay 1.2.0 Section 4.6.1 sends `components:` and `tags:` with empty (null) values.
    doc = spec({}, components={"schemas": {}}, tags=[{"name": "a"}])
    update = {"info": {"x-overlay-applied": "structured-overlay"}, "components": None, "tags": None}
    with pytest.raises(OverlayError, match="incompatible merge of primitive into object"):
        apply_overlay(doc, overlay({"target": "$", "update": update}))


@pytest.mark.unit
def test_array_targets_append_an_object_or_concatenate_an_array() -> None:
    doc = spec({"/a": {"get": {"parameters": [{"name": "id", "in": "path"}]}}})
    target = "$.paths.*.get.parameters"
    one = apply_overlay(doc, overlay({"target": target, "update": {"name": "q", "in": "query"}}))
    two = apply_overlay(doc, overlay({"target": target, "update": [{"name": "x"}, {"name": "y"}]}))
    assert [p["name"] for p in one.document["paths"]["/a"]["get"]["parameters"]] == ["id", "q"]
    assert [p["name"] for p in two.document["paths"]["/a"]["get"]["parameters"]] == [
        "id",
        "x",
        "y",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version", "accepted"),
    [("1.0.0", False), ("1.1.0", True), ("1.1.7", True), ("1.2.0", True)],
)
def test_primitive_targets_need_overlay_1_1(version: str, accepted: bool) -> None:
    doc = spec({"/a": {"get": {"summary": "old"}}})
    action = {"target": "$.paths['/a'].get.summary", "update": "new"}
    if accepted:
        result = apply_overlay(doc, overlay(action, version=version))
        assert result.document["paths"]["/a"]["get"]["summary"] == "new"
    else:
        with pytest.raises(OverlayError, match=r"1\.0 targets must be objects or arrays"):
            apply_overlay(doc, overlay(action, version=version))


@pytest.mark.unit
@pytest.mark.parametrize("version", ["1.3.0", "2.0.0", "1.2", 1.2])
def test_unknown_overlay_versions_are_rejected(version: object) -> None:
    doc = spec({})
    bad = overlay({"target": "$", "update": {}}) | {"overlay": version}
    with pytest.raises(OverlayError, match="unsupported overlay version"):
        apply_overlay(doc, bad)


@pytest.mark.unit
def test_zero_match_is_a_reported_noop_and_fatal_in_strict_mode() -> None:
    doc = spec({"/a": {"get": {}}})
    typo = overlay({"target": "$.paths.*[?@['x-internl'] == true]", "remove": True})
    result = apply_overlay(doc, typo)
    assert result.document == doc
    assert [a.index for a in result.unmatched()] == [0]
    with pytest.raises(OverlayError, match="selected nothing"):
        apply_overlay(doc, typo, strict=True)


@pytest.mark.unit
def test_removing_several_array_elements_deletes_the_right_ones() -> None:
    names = ["a", "dummy", "b", "dummy", "c"]
    doc = spec({"/a": {"get": {"parameters": [{"name": n} for n in names], "tags": names}}})
    result = apply_overlay(
        doc,
        overlay(
            {"target": "$.paths.*.get.parameters[?@.name == 'dummy']", "remove": True},
            {"target": "$.paths.*.get.tags[?@ == 'dummy']", "remove": True},
        ),
    )
    get = result.document["paths"]["/a"]["get"]
    assert [p["name"] for p in get["parameters"]] == ["a", "b", "c"]
    assert get["tags"] == ["a", "b", "c"]


@pytest.mark.unit
def test_one_update_is_copied_into_each_selected_node() -> None:
    doc = spec({"/a": {"get": {"parameters": []}, "post": {"parameters": []}}})
    ov = overlay({"target": "$.paths.*.*.parameters", "update": {"name": "trace"}})
    result = apply_overlay(doc, ov)
    get_param = result.document["paths"]["/a"]["get"]["parameters"][0]
    post_param = result.document["paths"]["/a"]["post"]["parameters"][0]
    get_param["name"] = "changed"
    assert post_param == {"name": "trace"}
    assert ov["actions"][0]["update"] == {"name": "trace"}


@pytest.mark.unit
def test_apply_overlay_never_mutates_its_input() -> None:
    doc = traits_doc()
    before = digest(doc)
    apply_overlay(doc, overlay({"target": "$.paths['/items']", "remove": True}))
    assert digest(doc) == before


@pytest.mark.unit
def test_aliased_input_nodes_are_detached_before_actions_run() -> None:
    # A YAML loader returns one shared object for an anchor and its aliases; copy.deepcopy
    # keeps that sharing, so the update below would also land on /b without detach().
    shared: dict[str, Any] = {"200": {"description": "OK"}}
    doc = spec({"/a": {"get": {"responses": shared}}, "/b": {"get": {"responses": shared}}})
    clone = copy.deepcopy(doc)
    assert clone["paths"]["/a"]["get"]["responses"] is clone["paths"]["/b"]["get"]["responses"]
    ov = overlay({"target": "$.paths['/a'].get.responses", "update": {"404": {"description": "x"}}})
    result = apply_overlay(doc, ov).document
    assert set(result["paths"]["/a"]["get"]["responses"]) == {"200", "404"}
    assert set(result["paths"]["/b"]["get"]["responses"]) == {"200"}


@pytest.mark.unit
def test_copy_then_remove_moves_a_path_item() -> None:
    # Overlay 1.2.0 Section 4.6.6.3, the move example.
    result = apply_overlay(
        copy_doc(),
        overlay(
            {"target": "$.paths", "update": {"/new-items": {}}},
            {"target": '$.paths["/new-items"]', "copy": '$.paths["/items"]'},
            {"target": '$.paths["/items"]', "remove": True},
            version="1.1.0",
        ),
    )
    assert result.document["paths"] == {
        "/some-items": {"delete": {"responses": OK}},
        "/new-items": {"get": {"responses": OK}},
    }


@pytest.mark.unit
def test_copy_rules() -> None:
    doc = copy_doc()
    many = overlay({"target": "$.paths['/items']", "copy": "$.paths.*"})
    with pytest.raises(OverlayError, match="exactly one node"):
        apply_overlay(doc, many)
    with pytest.raises(OverlayError, match=r"copy needs overlay 1\.1"):
        apply_overlay(doc, many | {"overlay": "1.0.0"})
    both = overlay({"target": "$.paths['/items']", "copy": "$.paths['/x']", "update": {}})
    with pytest.raises(OverlayError, match="mutually exclusive"):
        apply_overlay(doc, both)


@pytest.mark.unit
def test_reusable_actions_resolve_escaped_keys_and_reference_fields_win() -> None:
    not_found: dict[str, Any] = {
        "404": {"description": "Not Found", "content": {"application/json": {"schema": {}}}}
    }
    ov = overlay(
        {"$ref": "#/components/actions/error~1404", "target": "$.paths['/items'].get.responses"},
        {
            "$ref": "#/components/actions/error~1404",
            "target": "$.paths['/some-items'].delete.responses",
            "update": {"410": {"description": "Gone"}},
        },
    )
    ov["components"] = {"actions": {"error/404": {"fields": {"update": not_found}}}}
    result = apply_overlay(copy_doc(), ov)
    assert set(result.document["paths"]["/items"]["get"]["responses"]) == {"200", "404"}
    assert set(result.document["paths"]["/some-items"]["delete"]["responses"]) == {"200", "410"}
    with pytest.raises(OverlayError, match=r"need overlay 1\.2"):
        apply_overlay(copy_doc(), ov | {"overlay": "1.1.0"})


@pytest.mark.unit
def test_selected_nodes_of_mixed_kinds_are_rejected() -> None:
    doc = spec({}, **{"x-mixed": {"s": "text", "o": {}}})
    with pytest.raises(OverlayError, match="mix"):
        apply_overlay(doc, overlay({"target": "$['x-mixed'].*", "update": {"a": 1}}))


# ---------------------------------------------------------------- FastAPI and publication


@pytest.mark.unit
def test_openapi_extra_lands_on_the_operation(app: FastAPI) -> None:
    internal = app.openapi()
    assert internal["paths"][ADMIN_PATH]["post"]["x-internal"] is True
    assert "/admin/debug/config" not in internal["paths"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_route_hidden_from_the_schema_is_still_served(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.get("/admin/debug/config")
        operator = await client.get(
            "/admin/debug/config", headers={"X-Admin-Token": LAB_OPERATOR_KEY}
        )
        served = (await client.get("/openapi.json")).json()
    assert anonymous.status_code == 401
    assert operator.status_code == 200
    assert operator.json() == {"feature_flags": ["refund-override"]}
    assert "/admin/debug/config" not in served["paths"]


@pytest.mark.unit
def test_fastapi_hides_models_but_still_emits_tag_metadata() -> None:
    class Secret(BaseModel):
        value: str

    hidden = FastAPI(openapi_tags=[{"name": "admin", "description": "Operator tooling."}])

    @hidden.get("/admin/secret", tags=["admin"], include_in_schema=False)
    async def secret() -> Secret:  # pyright: ignore[reportUnusedFunction]
        return Secret(value="x")

    generated = hidden.openapi()
    assert generated["paths"] == {}
    assert "components" not in generated
    assert generated["tags"] == [{"name": "admin", "description": "Operator tooling."}]


@pytest.mark.unit
def test_removing_operations_leaves_orphans_until_pruned(app: FastAPI) -> None:
    overlaid = apply_overlay(app.openapi(), PUBLIC_PORTAL_OVERLAY, strict=True).document
    assert overlaid["paths"][ADMIN_PATH] == {}
    assert {"AdminAuditEntry", "OperatorIdentity"} <= overlaid["components"]["schemas"].keys()
    assert "AdminToken" in overlaid["components"]["securitySchemes"]
    public, removed = prune_for_publication(overlaid)
    assert removed == (
        "/paths/~1admin~1orders~1{order_id}~1refund-override",
        "/components/schemas/AdminAuditEntry",
        "/components/schemas/OperatorIdentity",
        "/components/securitySchemes/AdminToken",
    )
    assert sorted(public["components"]["schemas"]) == [
        "HTTPValidationError",
        "Money",
        "Order",
        "OrderCreate",
        "ValidationError",
    ]


@pytest.mark.unit
def test_pruning_drops_unused_tags_but_keeps_parents_of_used_ones() -> None:
    # OpenAPI 3.2.1 Section 4.22.2 example: `partner` is nested under `external`.
    tags = [
        {"name": "admin", "kind": "audience"},
        {"name": "partner", "parent": "external", "kind": "audience"},
        {"name": "external", "kind": "audience"},
    ]
    doc = spec({"/p": {"get": {"tags": ["partner"], "responses": OK}}}, tags=tags)
    doc["openapi"] = "3.2.0"
    pruned, removed = prune_for_publication(doc)
    assert [t["name"] for t in pruned["tags"]] == ["partner", "external"]
    assert removed == ("/tags/0",)
    validate(pruned)


@pytest.mark.unit
def test_pruning_keeps_the_whole_tag_ancestor_chain_and_webhook_tags() -> None:
    tags = [
        {"name": "partner", "parent": "external", "kind": "audience"},
        {"name": "external", "parent": "everyone", "kind": "audience"},
        {"name": "everyone", "kind": "audience"},
        {"name": "events", "kind": "nav"},
        {"name": "admin", "kind": "audience"},
    ]
    hook = {"post": {"tags": ["events"], "responses": OK}}
    doc = spec({"/p": {"get": {"tags": ["partner"], "responses": OK}}}, tags=tags)
    doc |= {"openapi": "3.2.0", "webhooks": {"orderShipped": hook}}
    pruned, removed = prune_for_publication(doc)
    assert [t["name"] for t in pruned["tags"]] == ["partner", "external", "everyone", "events"]
    assert removed == ("/tags/4",)
    validate(pruned)
    # Keeping only direct parents would drop `everyone`; openapi-spec-validator 0.9.0
    # then rejects the 3.2 description (OpenAPIV32TagsValidator).
    broken = pruned | {"tags": pruned["tags"][:2] + pruned["tags"][3:]}
    with pytest.raises(OpenAPIValidationError, match="unknown parent tag 'everyone'"):
        validate(broken)


@pytest.mark.unit
def test_pruning_keeps_allof_children_of_a_discriminated_parent() -> None:
    # OpenAPI 3.2.1 Section 4.25.2: children point at the parent, never the reverse.
    pet_ref = {"$ref": "#/components/schemas/Pet"}
    schemas = {
        "Pet": {"type": "object", "discriminator": {"propertyName": "petType"}},
        "Cat": {"allOf": [pet_ref, {"properties": {"meows": {"type": "boolean"}}}]},
        "Dog": {"allOf": [pet_ref, {"$ref": "#/components/schemas/Collar"}]},
        "Collar": {"type": "object"},
        "Plain": {"allOf": [{"$ref": "#/components/schemas/Collar"}]},
    }
    body = {"description": "OK", "content": {"application/json": {"schema": pet_ref}}}
    doc = spec({"/pets": {"get": {"responses": {"200": body}}}}, components={"schemas": schemas})
    pruned, removed = prune_for_publication(doc)
    assert sorted(pruned["components"]["schemas"]) == ["Cat", "Collar", "Dog", "Pet"]
    assert removed == ("/components/schemas/Plain",)


@pytest.mark.unit
def test_pruning_follows_default_mapping_and_refuses_relative_uri_targets() -> None:
    def pets(discriminator: dict[str, Any]) -> dict[str, Any]:
        pet = {"oneOf": [{"$ref": "#/components/schemas/Cat"}], "discriminator": discriminator}
        ok = {"description": "OK", "content": {"application/json": {"schema": pet}}}
        schemas = {"Cat": {}, "OtherPet": {}, "Unused": {}}
        return spec({"/pets": {"get": {"responses": {"200": ok}}}}, components={"schemas": schemas})

    pruned, _ = prune_for_publication(pets({"propertyName": "k", "defaultMapping": "OtherPet"}))
    assert sorted(pruned["components"]["schemas"]) == ["Cat", "OtherPet"]
    with pytest.raises(ValueError, match="bundle external"):
        prune_for_publication(pets({"propertyName": "k", "mapping": {"dog": "./Dog"}}))


@pytest.mark.unit
def test_removing_an_internal_property_leaves_it_required_until_required_is_edited() -> None:
    order = {
        "type": "object",
        "required": ["id", "operator_note"],
        "properties": {"id": {"type": "string"}, "operator_note": {"x-internal": True}},
    }
    doc = spec({}, components={"schemas": {"Order": order}})
    strip = {"target": "$..properties[?@['x-internal'] == true]", "remove": True}
    half = apply_overlay(doc, overlay(strip)).document
    assert half["components"]["schemas"]["Order"]["properties"] == {"id": {"type": "string"}}
    assert dangling_required(half) == ["$['components']['schemas']['Order']['required'][1]"]
    fix = {"target": "$..required[?@ == 'operator_note']", "remove": True}
    full = apply_overlay(doc, overlay(strip, fix)).document
    assert full["components"]["schemas"]["Order"]["required"] == ["id"]
    assert dangling_required(full) == []


@pytest.mark.unit
def test_validator_rejects_an_extension_beside_a_reference_object_ref() -> None:
    # OpenAPI 3.1.2 Section 4.8.23: added properties on a Reference Object SHALL be
    # ignored; the 3.1 meta-schema in openapi-spec-validator 0.9.0 rejects them outright.
    param = {"name": "p", "in": "query", "schema": {"type": "string"}}
    ref = {"$ref": "#/components/parameters/P", "x-internal": True}
    doc = spec(
        {"/a": {"get": {"parameters": [ref], "responses": OK}}},
        components={"parameters": {"P": param}},
    )
    with pytest.raises(OpenAPIValidationError, match="'x-internal' was unexpected"):
        validate(doc)
    del ref["x-internal"]
    param["x-internal"] = True
    validate(doc)


@pytest.mark.unit
def test_pruning_keeps_discriminator_mapping_targets() -> None:
    pet = {
        "type": "object",
        "discriminator": {
            "propertyName": "kind",
            "mapping": {"cat": "#/components/schemas/Cat", "dog": "Dog"},
        },
    }
    doc = spec(
        {
            "/pets": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                            },
                        }
                    }
                }
            }
        },
        components={"schemas": {"Pet": pet, "Cat": {}, "Dog": {}, "Unused": {}}},
    )
    pruned, removed = prune_for_publication(doc)
    assert sorted(pruned["components"]["schemas"]) == ["Cat", "Dog", "Pet"]
    assert removed == ("/components/schemas/Unused",)


@pytest.mark.unit
def test_public_spec_is_valid_leak_free_and_annotated(app: FastAPI) -> None:
    public, overlaid, _ = build_public_spec(app)
    validate(public)  # raises on an invalid description or a dangling $ref
    assert find_leaks(public, ("admin", "internal", "operator")) == []
    assert find_leaks(app.openapi(), ("admin",)) != []
    assert overlaid.unmatched() == ()
    operations = [op for item in public["paths"].values() for op in item.values()]
    assert {op["operationId"] for op in operations} == {"getOrder", "createOrder"}
    assert all(op["x-acme-rate-limit"]["quota"] == 100 for op in operations)
    create = public["paths"]["/v1/orders"]["post"]
    assert create["x-codeSamples"][0]["lang"] == "Python"
    assert "RateLimit-Policy" in create["responses"]["201"]["headers"]
    assert (
        "RateLimit-Policy"
        in public["paths"]["/v1/orders/{order_id}"]["get"]["responses"]["200"]["headers"]
    )
    assert "headers" not in create["responses"]["422"]


@pytest.mark.unit
def test_publication_is_deterministic_and_leaves_the_served_schema_alone(app: FastAPI) -> None:
    before = digest(app.openapi())
    first, _, _ = build_public_spec(app)
    second, _, _ = build_public_spec(build_app())
    assert digest(first) == digest(second)
    assert digest(app.openapi()) == before
    assert app.openapi()["paths"][ADMIN_PATH]["post"]["operationId"] == "adminRefundOverride"


# ---------------------------------------------------------------- extensions and validator


@pytest.mark.unit
def test_extension_inventory_is_position_aware() -> None:
    doc = spec(
        {
            "/a": {
                "get": {
                    "x-internal": True,
                    "X-Internal": True,
                    "responses": {
                        "200": {
                            "description": "OK",
                            "headers": {"x-request-id": {"schema": {"type": "string"}}},
                            "content": {
                                "application/json": {
                                    "schema": {"properties": {"x-legacy": {"type": "string"}}}
                                }
                            },
                        }
                    },
                }
            }
        },
        **{"x-oai-example": 1},
    )
    report = extension_inventory(doc)
    assert sorted(report["extensions"]) == [
        "$['paths']['/a']['get']['x-internal']",
        "$['x-oai-example']",
    ]
    assert report["reserved"] == ["$['x-oai-example']"]
    assert report["case_mismatch"] == ["$['paths']['/a']['get']['X-Internal']"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "valid"),
    [("x-internal", True), ("internal", False), ("X-Internal", False)],
)
def test_openapi_spec_validator_enforces_the_case_sensitive_x_prefix(
    field: str, valid: bool
) -> None:
    doc = spec({"/a": {"get": {"responses": OK, field: True}}})
    if valid:
        validate(doc)
    else:
        with pytest.raises(OpenAPIValidationError, match="Unevaluated properties"):
            validate(doc)


@pytest.mark.unit
def test_openapi_spec_validator_0_9_0_crashes_on_a_paths_level_extension() -> None:
    # OpenAPI 3.1.2 Section 4.8.8 allows extensions on the Paths Object; 0.9.0's
    # PathsValidator iterates every key as a path and raises KeyError.
    doc = spec({"x-owner": "team-orders", "/a": {"get": {"responses": OK}}})
    with pytest.raises(KeyError, match="x-owner"):
        validate(doc)


@pytest.mark.unit
def test_a_dangling_ref_raises_pointer_to_nowhere_not_a_validation_error() -> None:
    schema_ref = {"schema": {"$ref": "#/components/schemas/Gone"}}
    response = {"description": "OK", "content": {"application/json": schema_ref}}
    doc = spec({"/a": {"get": {"responses": {"200": response}}}})
    assert not issubclass(PointerToNowhere, OpenAPIValidationError)
    with pytest.raises(PointerToNowhere):
        validate(copy.deepcopy(doc))
