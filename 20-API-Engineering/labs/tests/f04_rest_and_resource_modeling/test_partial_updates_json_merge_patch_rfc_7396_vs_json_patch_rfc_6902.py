"""Lab: JSON Merge Patch (RFC 7396) vs JSON Patch (RFC 6902) behind an atomic HTTP PATCH."""

from __future__ import annotations

import copy
import json
import sys
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Body, FastAPI
from hypothesis import given
from hypothesis import strategies as st

from api_labs.f04_rest_and_resource_modeling.partial_updates import (
    ACCEPT_PATCH,
    JSON,
    JSON_PATCH,
    MAX_DEPTH,
    MAX_OPERATIONS,
    MERGE_PATCH,
    BodyError,
    PatchError,
    WidgetStore,
    apply_json_patch,
    build_app,
    json_depth,
    json_equal,
    merge_patch,
    parse_json_strict,
    parse_pointer,
)

BASE = "http://test"
ARABIC_INDIC_ONE = chr(0x0661)  # str.isdigit() is True and int() returns 1


def seeded_store() -> WidgetStore:
    widget: dict[str, JSON] = {
        "id": "w1",
        "name": "gear",
        "tags": ["metal"],
        "labels": {"team": "core"},
        "note": "first",
    }
    return WidgetStore(documents={"w1": widget})


def client_for(store: WidgetStore) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(store)), base_url=BASE)


def as_json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


# RFC 7396 Appendix A, every row: (original, patch, result).
MERGE_VECTORS: list[tuple[JSON, JSON, JSON]] = [
    ({"a": "b"}, {"a": "c"}, {"a": "c"}),
    ({"a": "b"}, {"b": "c"}, {"a": "b", "b": "c"}),
    ({"a": "b"}, {"a": None}, {}),
    ({"a": "b", "b": "c"}, {"a": None}, {"b": "c"}),
    ({"a": ["b"]}, {"a": "c"}, {"a": "c"}),
    ({"a": "c"}, {"a": ["b"]}, {"a": ["b"]}),
    ({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}, {"a": {"b": "d"}}),
    ({"a": [{"b": "c"}]}, {"a": [1]}, {"a": [1]}),
    (["a", "b"], ["c", "d"], ["c", "d"]),
    ({"a": "b"}, ["c"], ["c"]),
    ({"a": "foo"}, None, None),
    ({"a": "foo"}, "bar", "bar"),
    ({"e": None}, {"a": 1}, {"e": None, "a": 1}),
    ([1, 2], {"a": "b", "c": None}, {"a": "b"}),
    ({}, {"a": {"bb": {"ccc": None}}}, {"a": {"bb": {}}}),
]


@pytest.mark.unit
@pytest.mark.parametrize(("original", "patch", "expected"), MERGE_VECTORS)
def test_merge_patch_matches_rfc_7396_appendix_a(
    original: JSON, patch: JSON, expected: JSON
) -> None:
    assert as_json(merge_patch(original, patch)) == as_json(expected)


@pytest.mark.unit
def test_merge_patch_null_deletes_so_only_json_patch_can_store_null() -> None:
    target: JSON = {"note": "first", "tags": ["a", "b", "c"]}
    assert merge_patch(target, {"note": None}) == {"tags": ["a", "b", "c"]}
    assert merge_patch(target, {"tags": ["z"]}) == {"note": "first", "tags": ["z"]}
    stored_null = apply_json_patch(target, [{"op": "replace", "path": "/note", "value": None}])
    assert stored_null == {"note": None, "tags": ["a", "b", "c"]}


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=3),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=2), inner, max_size=3)
    ),
    max_leaves=12,
)


@pytest.mark.unit
@given(target=json_values, patch=json_values)
def test_merge_patch_is_idempotent_and_never_mutates_inputs(target: JSON, patch: JSON) -> None:
    target_before, patch_before = copy.deepcopy(target), copy.deepcopy(patch)
    once = merge_patch(target, patch)
    assert as_json(merge_patch(once, patch)) == as_json(once)
    assert as_json(target) == as_json(target_before)
    assert as_json(patch) == as_json(patch_before)


# RFC 6902 Appendix A examples that succeed: (section, document, patch, result).
JSON_PATCH_VECTORS: list[tuple[str, JSON, JSON, JSON]] = [
    (
        "A.1",
        {"foo": "bar"},
        [{"op": "add", "path": "/baz", "value": "qux"}],
        {"baz": "qux", "foo": "bar"},
    ),
    (
        "A.2",
        {"foo": ["bar", "baz"]},
        [{"op": "add", "path": "/foo/1", "value": "qux"}],
        {"foo": ["bar", "qux", "baz"]},
    ),
    ("A.3", {"baz": "qux", "foo": "bar"}, [{"op": "remove", "path": "/baz"}], {"foo": "bar"}),
    (
        "A.4",
        {"foo": ["bar", "qux", "baz"]},
        [{"op": "remove", "path": "/foo/1"}],
        {"foo": ["bar", "baz"]},
    ),
    (
        "A.5",
        {"baz": "qux", "foo": "bar"},
        [{"op": "replace", "path": "/baz", "value": "boo"}],
        {"baz": "boo", "foo": "bar"},
    ),
    (
        "A.6",
        {"foo": {"bar": "baz", "waldo": "fred"}, "qux": {"corge": "grault"}},
        [{"op": "move", "from": "/foo/waldo", "path": "/qux/thud"}],
        {"foo": {"bar": "baz"}, "qux": {"corge": "grault", "thud": "fred"}},
    ),
    (
        "A.7",
        {"foo": ["all", "grass", "cows", "eat"]},
        [{"op": "move", "from": "/foo/1", "path": "/foo/3"}],
        {"foo": ["all", "cows", "eat", "grass"]},
    ),
    (
        "A.8",
        {"baz": "qux", "foo": ["a", 2, "c"]},
        [
            {"op": "test", "path": "/baz", "value": "qux"},
            {"op": "test", "path": "/foo/1", "value": 2},
        ],
        {"baz": "qux", "foo": ["a", 2, "c"]},
    ),
    (
        "A.10",
        {"foo": "bar"},
        [{"op": "add", "path": "/child", "value": {"grandchild": {}}}],
        {"foo": "bar", "child": {"grandchild": {}}},
    ),
    (
        "A.11",
        {"foo": "bar"},
        [{"op": "add", "path": "/baz", "value": "qux", "xyz": 123}],
        {"foo": "bar", "baz": "qux"},
    ),
    ("A.14", {"/": 9, "~1": 10}, [{"op": "test", "path": "/~01", "value": 10}], {"/": 9, "~1": 10}),
    (
        "A.16",
        {"foo": ["bar"]},
        [{"op": "add", "path": "/foo/-", "value": ["abc", "def"]}],
        {"foo": ["bar", ["abc", "def"]]},
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("section", "document", "patch", "expected"),
    JSON_PATCH_VECTORS,
    ids=[v[0] for v in JSON_PATCH_VECTORS],
)
def test_json_patch_matches_rfc_6902_appendix_a(
    section: str, document: JSON, patch: JSON, expected: JSON
) -> None:
    assert as_json(apply_json_patch(document, patch)) == as_json(expected), section


@pytest.mark.unit
@pytest.mark.parametrize(
    ("document", "patch"),
    [
        ({"baz": "qux"}, [{"op": "test", "path": "/baz", "value": "bar"}]),  # A.9
        ({"foo": "bar"}, [{"op": "add", "path": "/baz/bat", "value": "qux"}]),  # A.12
        ({"/": 9, "~1": 10}, [{"op": "test", "path": "/~01", "value": "10"}]),  # A.15
        ({"foo": ["bar"]}, [{"op": "remove", "path": "/foo/1"}]),
        ({"foo": ["bar"]}, [{"op": "add", "path": "/foo/2", "value": "x"}]),
    ],
    ids=[
        "A.9-test-fails",
        "A.12-missing-parent",
        "A.15-string-vs-number",
        "remove-out-of-range",
        "add-past-end",
    ],
)
def test_json_patch_state_errors_are_conflicts(document: JSON, patch: JSON) -> None:
    with pytest.raises(PatchError) as caught:
        apply_json_patch(document, patch)
    assert caught.value.kind == "conflict"


@pytest.mark.unit
def test_pointer_decodes_tilde_one_before_tilde_zero() -> None:
    assert parse_pointer("/~01") == ["~1"]
    assert parse_pointer("/a~1b/m~0n") == ["a/b", "m~n"]
    assert parse_pointer("") == []
    assert parse_pointer("/") == [""]
    for bad in ("a/b", "/~2", "/x~"):
        with pytest.raises(ValueError, match="pointer"):
            parse_pointer(bad)


@pytest.mark.unit
@pytest.mark.parametrize(
    "token",
    ["01", ARABIC_INDIC_ONE, "1.0", "-1"],
    ids=["leading-zero", "arabic-indic-one", "float", "negative"],
)
def test_array_index_accepts_only_rfc_6901_ascii_digits(token: str) -> None:
    assert token.isdigit() == (token in {"01", ARABIC_INDIC_ONE})  # isdigit() accepts two of these
    with pytest.raises(PatchError) as caught:
        apply_json_patch({"tags": ["a", "b"]}, [{"op": "remove", "path": f"/tags/{token}"}])
    assert caught.value.kind == "conflict"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        {"op": "add", "path": "/tags/{huge}", "value": "x"},
        {"op": "remove", "path": "/tags/{huge}"},
        {"op": "test", "path": "/tags/{huge}", "value": "metal"},
        {"op": "copy", "from": "/tags/{huge}", "path": "/note"},
    ],
    ids=["add", "remove", "test", "copy-from"],
)
async def test_index_longer_than_int_str_digit_limit_is_409_not_500(
    operation: dict[str, str],
) -> None:
    huge = "1" * (sys.get_int_max_str_digits() + 1)  # int() of this raises ValueError
    with pytest.raises(ValueError, match="digits"):
        int(huge)
    store = seeded_store()
    op = {key: value.replace("{huge}", huge) for key, value in operation.items()}
    async with client_for(store) as client:
        response = await client.patch(
            "/widgets/w1", content=json.dumps([op]), headers={"Content-Type": JSON_PATCH}
        )
    assert response.status_code == 409
    assert "out of range" in response.json()["detail"]
    assert store.writes == 0


@pytest.mark.unit
def test_test_op_compares_json_types_not_python_equality() -> None:
    one: JSON = 1
    assert one == True  # noqa: E712 - Python equality conflates bool and int
    assert [one] == [True]
    assert not json_equal(True, 1)
    assert not json_equal([1], [True])
    assert json_equal(1, 1.0)
    assert json_equal({"a": [1, None]}, {"a": [1.0, None]})
    with pytest.raises(PatchError, match="test failed"):
        apply_json_patch({"count": 1}, [{"op": "test", "path": "/count", "value": True}])


@pytest.mark.unit
def test_move_into_own_child_is_malformed_and_copy_does_not_alias() -> None:
    with pytest.raises(PatchError) as caught:
        apply_json_patch({"a": {"b": 1}}, [{"op": "move", "from": "/a", "path": "/a/b"}])
    assert caught.value.kind == "malformed"
    result = apply_json_patch(
        {"a": {"x": 1}},
        [{"op": "copy", "from": "/a", "path": "/b"}, {"op": "replace", "path": "/b/x", "value": 2}],
    )
    assert result == {"a": {"x": 1}, "b": {"x": 2}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_json_patch_changes_nothing_and_returns_409() -> None:
    store = seeded_store()
    async with client_for(store) as client:
        before = await client.get("/widgets/w1")
        # RFC 6902 Section 5: a later failing test aborts the earlier replace.
        response = await client.patch(
            "/widgets/w1",
            content=json.dumps(
                [
                    {"op": "replace", "path": "/name", "value": "sprocket"},
                    {"op": "add", "path": "/tags/-", "value": "steel"},
                    {"op": "test", "path": "/name", "value": "gear"},
                ]
            ),
            headers={"Content-Type": JSON_PATCH},
        )
        after = await client.get("/widgets/w1")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "operation 2: test failed" in response.json()["detail"]
    assert after.json() == before.json()
    assert after.headers["etag"] == before.headers["etag"]
    assert store.writes == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_type_selects_the_algorithm_and_unknown_types_get_415() -> None:
    store = seeded_store()
    ops = json.dumps([{"op": "add", "path": "/tags/-", "value": "steel"}])
    async with client_for(store) as client:
        plain = await client.patch(
            "/widgets/w1", content=ops, headers={"Content-Type": "application/json"}
        )
        as_merge = await client.patch(
            "/widgets/w1", content=ops, headers={"Content-Type": MERGE_PATCH}
        )
        as_ops = await client.patch(
            "/widgets/w1", content=ops, headers={"Content-Type": f"{JSON_PATCH}; charset=utf-8"}
        )
    assert plain.status_code == 415
    assert plain.headers["accept-patch"] == ACCEPT_PATCH
    # An array is a non-object merge patch: it would replace the whole resource.
    assert as_merge.status_code == 422
    assert "not a JSON object" in as_merge.json()["detail"]
    assert as_ops.status_code == 200
    assert as_ops.json()["tags"] == ["metal", "steel"]
    assert as_ops.headers["accept-patch"] == ACCEPT_PATCH
    assert store.writes == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merge_patch_deleting_required_field_or_changing_id_returns_422() -> None:
    store = seeded_store()
    async with client_for(store) as client:
        no_name = await client.patch(
            "/widgets/w1", content=b'{"name": null}', headers={"Content-Type": MERGE_PATCH}
        )
        new_id = await client.patch(
            "/widgets/w1", content=b'{"id": "w2"}', headers={"Content-Type": MERGE_PATCH}
        )
        clear_note = await client.patch(
            "/widgets/w1", content=b'{"note": null}', headers={"Content-Type": MERGE_PATCH}
        )
    assert (no_name.status_code, new_id.status_code) == (422, 422)
    assert "invalid: 1 error(s)" in no_name.json()["detail"]
    assert "'id' is immutable" in new_id.json()["detail"]
    assert clear_note.status_code == 200
    assert "note" not in clear_note.json()
    assert store.writes == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_names_nan_and_deep_nesting_are_400_not_500() -> None:
    duplicate_op = (
        b'[{"op": "add", "path": "/name", "value": "x", "op": "remove"}]'  # RFC 6902 A.13
    )
    assert json.loads(duplicate_op)[0]["op"] == "remove"  # the stdlib default keeps the last name
    bodies = {
        "duplicate": (JSON_PATCH, duplicate_op),
        "nan": (MERGE_PATCH, b'{"name": NaN}'),
        "infinity": (MERGE_PATCH, b'{"name": Infinity}'),
        "negative-infinity": (JSON_PATCH, b'[{"op": "add", "path": "/n", "value": -Infinity}]'),
        "too-deep": (MERGE_PATCH, b'{"labels": ' + b'{"a": ' * 40 + b"1" + b"}" * 41),
        "stack-bomb": (MERGE_PATCH, b"[" * 1_000_000 + b"]" * 1_000_000),
        "not-utf8": (MERGE_PATCH, b'{"name": "\xff"}'),
    }
    store = seeded_store()
    async with client_for(store) as client:
        for label, (media_type, body) in bodies.items():
            response = await client.patch(
                "/widgets/w1", content=body, headers={"Content-Type": media_type}
            )
            assert response.status_code == 400, label
    assert store.writes == 0
    assert store.documents["w1"]["name"] == "gear"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operation_count_limit_returns_422_before_any_work() -> None:
    ops = [{"op": "test", "path": "/name", "value": "gear"}] * 101
    store = seeded_store()
    async with client_for(store) as client:
        response = await client.patch(
            "/widgets/w1", content=json.dumps(ops), headers={"Content-Type": JSON_PATCH}
        )
    assert response.status_code == 422
    assert "101 operations exceed 100" in response.json()["detail"]


@pytest.mark.unit
def test_operation_and_depth_limits_are_inclusive() -> None:
    document: JSON = {"name": "gear"}
    at_limit: list[JSON] = [
        {"op": "test", "path": "/name", "value": "gear"} for _ in range(MAX_OPERATIONS)
    ]
    assert apply_json_patch(document, at_limit) == document
    nested = b'{"a": ' * (MAX_DEPTH - 1) + b"{}" + b"}" * (MAX_DEPTH - 1)
    assert json_depth(parse_json_strict(nested)) == MAX_DEPTH
    with pytest.raises(BodyError, match="nesting deeper than 32"):
        parse_json_strict(b"[" + nested + b"]")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_merge_patch_loses_concurrent_array_appends_unless_if_match_is_sent() -> None:
    store = seeded_store()
    async with client_for(store) as client:
        etag = (await client.get("/widgets/w1")).headers["etag"]
        # Two clients read tags == ["metal"] and each send the whole new array.
        await client.patch(
            "/widgets/w1",
            content=b'{"tags": ["metal", "red"]}',
            headers={"Content-Type": MERGE_PATCH},
        )
        await client.patch(
            "/widgets/w1",
            content=b'{"tags": ["metal", "blue"]}',
            headers={"Content-Type": MERGE_PATCH},
        )
        lost = (await client.get("/widgets/w1")).json()["tags"]
    assert lost == ["metal", "blue"]

    guarded = seeded_store()
    async with client_for(guarded) as client:
        first = await client.patch(
            "/widgets/w1",
            content=b'{"tags": ["metal", "red"]}',
            headers={"Content-Type": MERGE_PATCH, "If-Match": etag},
        )
        second = await client.patch(
            "/widgets/w1",
            content=b'{"tags": ["metal", "blue"]}',
            headers={"Content-Type": MERGE_PATCH, "If-Match": etag},
        )
        unsupported = await client.patch(
            "/widgets/w1", content=b"{}", headers={"Content-Type": "text/plain", "If-Match": etag}
        )
    assert (first.status_code, second.status_code) == (200, 412)
    assert unsupported.status_code == 415  # RFC 9110 Section 13.2.1: 415 wins over 412
    assert guarded.documents["w1"]["tags"] == ["metal", "red"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_patch_append_composes_but_is_not_idempotent() -> None:
    store = seeded_store()
    append = json.dumps([{"op": "add", "path": "/tags/-", "value": "red"}])
    async with client_for(store) as client:
        await client.patch("/widgets/w1", content=append, headers={"Content-Type": JSON_PATCH})
        await client.patch(
            "/widgets/w1",
            content=json.dumps([{"op": "add", "path": "/tags/-", "value": "blue"}]),
            headers={"Content-Type": JSON_PATCH},
        )
        retried = await client.patch(
            "/widgets/w1", content=append, headers={"Content-Type": JSON_PATCH}
        )
    assert retried.json()["tags"] == ["metal", "red", "blue", "red"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_body_parses_any_plus_json_subtype_but_not_text_plain() -> None:
    app = FastAPI()

    @app.patch("/echo")
    async def echo(patch: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        return patch

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        merge = await client.patch(
            "/echo", content=b'{"a": null}', headers={"Content-Type": MERGE_PATCH}
        )
        text = await client.patch(
            "/echo", content=b'{"a": null}', headers={"Content-Type": "text/plain"}
        )
    assert merge.status_code == 200
    assert merge.json() == {"a": None}
    assert text.status_code == 422


@pytest.mark.unit
@pytest.mark.parametrize(
    "patch",
    [
        {"op": "add", "path": "/a", "value": 1},
        ["add"],
        [{"op": "jump", "path": "/a"}],
        [{"op": "remove"}],
        [{"op": "remove", "path": "/~2"}],
        [{"op": "add", "path": "/a"}],
        [{"op": "replace", "path": "/a"}],
        [{"op": "test", "path": "/a"}],
        [{"op": "copy", "path": "/b"}],
    ],
    ids=[
        "not-array",
        "op-not-object",
        "unknown-op",
        "no-path",
        "bad-escape",
        "add-no-value",
        "replace-no-value",
        "test-no-value",
        "no-from",
    ],
)
def test_json_patch_document_errors_are_malformed(patch: JSON) -> None:
    with pytest.raises(PatchError) as caught:
        apply_json_patch({"a": 1}, patch)
    assert caught.value.kind == "malformed"


@pytest.mark.unit
def test_root_pointer_and_scalar_parents() -> None:
    assert apply_json_patch({"a": 1}, [{"op": "add", "path": "", "value": [1]}]) == [1]
    replaced = apply_json_patch({"a": 1}, [{"op": "replace", "path": "", "value": {"b": 2}}])
    assert replaced == {"b": 2}
    assert apply_json_patch({"a": 1}, [{"op": "move", "from": "", "path": ""}]) == {"a": 1}
    conflicts: list[JSON] = [
        [{"op": "remove", "path": ""}],
        [{"op": "remove", "path": "/missing"}],
        [{"op": "remove", "path": "/a/b"}],
        [{"op": "test", "path": "/a/b/c", "value": 1}],
    ]
    for patch in conflicts:
        with pytest.raises(PatchError) as caught:
            apply_json_patch({"a": 1}, patch)
        assert caught.value.kind == "conflict"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_resource_is_404_and_if_match_accepts_star_and_lists() -> None:
    store = seeded_store()
    async with client_for(store) as client:
        missing_get = await client.get("/widgets/nope")
        missing_patch = await client.patch(
            "/widgets/nope", content=b"{}", headers={"Content-Type": MERGE_PATCH}
        )
        etag = (await client.get("/widgets/w1")).headers["etag"]
        star = await client.patch(
            "/widgets/w1",
            content=b'{"name": "cog"}',
            headers={"Content-Type": MERGE_PATCH, "If-Match": "*"},
        )
        stale_in_list = await client.patch(
            "/widgets/w1",
            content=b'{"name": "cam"}',
            headers={"Content-Type": MERGE_PATCH, "If-Match": f'"other", {etag}'},
        )
        current_in_list = await client.patch(
            "/widgets/w1",
            content=b'{"name": "cam"}',
            headers={"Content-Type": MERGE_PATCH, "If-Match": f'"other", {star.headers["etag"]}'},
        )
    assert (missing_get.status_code, missing_patch.status_code) == (404, 404)
    assert (star.status_code, stale_in_list.status_code, current_in_list.status_code) == (
        200,
        412,
        200,
    )
    assert store.documents["w1"]["name"] == "cam"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weak_etag_fails_if_match_and_unknown_members_are_422() -> None:
    store = seeded_store()
    async with client_for(store) as client:
        etag = (await client.get("/widgets/w1")).headers["etag"]
        # RFC 9110 Section 13.1.1: If-Match uses the strong comparison, so W/ never matches.
        weak = await client.patch(
            "/widgets/w1",
            content=b'{"name": "cog"}',
            headers={"Content-Type": MERGE_PATCH, "If-Match": f"W/{etag}"},
        )
        # Media types are case-insensitive, and extra="forbid" rejects members not in the schema.
        extra = await client.patch(
            "/widgets/w1",
            content=b'{"owner": "eve"}',
            headers={"Content-Type": "Application/Merge-Patch+JSON", "If-Match": etag},
        )
    assert weak.status_code == 412
    assert extra.status_code == 422
    assert "invalid: 1 error(s)" in extra.json()["detail"]
    assert store.writes == 0
    assert "owner" not in store.documents["w1"]
