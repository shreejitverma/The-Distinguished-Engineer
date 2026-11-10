"""Update masks (AIP-134, AIP-161) versus Pydantic `exclude_unset`, cross-checked with protobuf."""

import importlib
from typing import Any, Self

import httpx
import pytest
from pydantic import BaseModel, model_validator

from api_labs.f04_rest_and_resource_modeling.field_masks import (
    Author,
    Book,
    BookPatch,
    InvalidFieldMaskError,
    apply_update_mask,
    build_app,
    canonical_mask,
    implied_mask_populated,
    mask_from_fields_set,
    naive_model_copy_update,
    parse_update_mask,
    project,
)


def _stored() -> Book:
    return Book(
        name="publishers/p1/books/b1",
        title="Mary Poppins",
        rating=4,
        author=Author(given_name="Pamela", family_name="Travers"),
        tags=["classic"],
        update_time=7,
    )


async def _patch(body: dict[str, object], query: str = "") -> tuple[httpx.Response, Book]:
    store = {"b1": _stored()}
    app = build_app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(f"/v1/books/b1{query}", json=body)
    return response, store["b1"]


# ---------------------------------------------------------------- Pydantic presence tracking


@pytest.mark.unit
def test_fields_set_separates_omitted_from_explicit_null() -> None:
    omitted = BookPatch.model_validate({})
    explicit_null = BookPatch.model_validate({"rating": None})

    assert omitted.rating is None
    assert explicit_null.rating is None
    assert omitted.model_fields_set == set()
    assert explicit_null.model_fields_set == {"rating"}
    assert explicit_null.model_dump(exclude_unset=True) == {"rating": None}
    # exclude_none erases the "clear this field" intent.
    assert explicit_null.model_dump(exclude_none=True) == {}


@pytest.mark.unit
def test_mask_from_fields_set_recurses_into_nested_models() -> None:
    patch = BookPatch.model_validate(
        {"author": {"given_name": "P. L."}, "rating": None, "tags": []}
    )
    assert mask_from_fields_set(patch) == ("author.given_name", "rating", "tags")

    empty_object = BookPatch.model_validate({"author": {}})
    assert mask_from_fields_set(empty_object) == ("author",)


@pytest.mark.unit
def test_aip134_populated_implied_mask_cannot_express_a_clear() -> None:
    body: dict[str, object] = {
        "title": "Mary Poppins",
        "rating": None,
        "tags": [],
        "author": {"given_name": "", "family_name": "Travers"},
    }
    assert implied_mask_populated(body) == ("author.family_name", "title")
    assert mask_from_fields_set(BookPatch.model_validate(body)) == (
        "author.family_name",
        "author.given_name",
        "rating",
        "tags",
        "title",
    )


@pytest.mark.unit
def test_validator_assignment_marks_a_field_as_set() -> None:
    class UnsafeSlugPatch(BaseModel):
        title: str | None = None
        slug: str | None = None

        @model_validator(mode="after")
        def derive_slug(self) -> Self:
            self.slug = self.title.lower() if self.title else None
            return self

    class SafeSlugPatch(UnsafeSlugPatch):
        @model_validator(mode="after")
        def derive_slug(self) -> Self:
            if "title" in self.model_fields_set and self.title:
                self.slug = self.title.lower()
            return self

    assert mask_from_fields_set(UnsafeSlugPatch.model_validate({})) == ("slug",)
    assert mask_from_fields_set(SafeSlugPatch.model_validate({})) == ()
    assert mask_from_fields_set(SafeSlugPatch.model_validate({"title": "X"})) == ("slug", "title")


@pytest.mark.unit
def test_naive_model_copy_update_skips_validation_and_replaces_nested_objects() -> None:
    patch = BookPatch.model_validate({"title": None, "author": {"given_name": "P. L."}})

    naive = naive_model_copy_update(_stored(), patch)
    assert naive.__dict__["title"] is None  # a `str` field now holds None
    assert naive.__dict__["author"] == {"given_name": "P. L."}  # a dict, family_name lost
    assert not isinstance(naive.__dict__["author"], Author)

    with pytest.raises(ValueError, match="title"):
        apply_update_mask(_stored(), patch, mask_from_fields_set(patch))
    leaf_only = BookPatch.model_validate({"author": {"given_name": "P. L."}})
    masked = apply_update_mask(_stored(), leaf_only, mask_from_fields_set(leaf_only))
    assert masked.author == Author(given_name="P. L.", family_name="Travers")


# ---------------------------------------------------------------- mask parsing and validation


@pytest.mark.unit
def test_parse_update_mask_distinguishes_absent_from_empty() -> None:
    assert parse_update_mask(None) is None
    assert parse_update_mask("") == ()
    assert parse_update_mask("title,author.givenName") == ("title", "author.given_name")
    with pytest.raises(InvalidFieldMaskError):
        parse_update_mask("author.given_name")


@pytest.mark.unit
def test_canonical_mask_removes_covered_paths_and_sorts() -> None:
    raw = ("title", "author.given_name", "author", "author.family_name", "title")
    assert canonical_mask(raw) == ("author", "title")


@pytest.mark.unit
def test_canonical_mask_covers_only_at_a_segment_boundary() -> None:
    # "tag" is a string prefix of "tags" but not an ancestor path of it.
    assert canonical_mask(("tags", "tag")) == ("tag", "tags")
    assert canonical_mask(("tag.x", "tag")) == ("tag",)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mask", "detail"),
    [
        ("isbn", "unknown field 'isbn'"),
        ("tags.0", "'tags' is not a singular message field"),
        ("title.length", "'title' is not a singular message field"),
        ("author.middleName", "unknown field 'middle_name'"),
        ("*,title", "the wildcard must be the only path"),
    ],
    ids=["unknown", "repeated-index", "scalar-traversal", "nested-unknown", "wildcard-mixed"],
)
@pytest.mark.asyncio
async def test_unmappable_paths_return_400(mask: str, detail: str) -> None:
    response, stored = await _patch({"title": "New"}, f"?updateMask={mask}")
    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert stored == _stored()


# ---------------------------------------------------------------- update semantics over ASGI


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_mask_ignores_unmasked_body_values() -> None:
    response, stored = await _patch({"title": "New", "rating": 1}, "?updateMask=title")
    assert response.status_code == 200
    assert stored.title == "New"
    assert stored.rating == 4
    assert stored.update_time == 8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_masked_path_absent_from_body_clears_to_default() -> None:
    response, stored = await _patch({}, "?updateMask=rating,tags,author.givenName")
    assert response.status_code == 200
    assert stored.rating is None
    assert stored.tags == []
    assert stored.author == Author(given_name="", family_name="Travers")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_mask_uses_fields_set_so_null_clears_and_omission_keeps() -> None:
    response, stored = await _patch({"rating": None})
    assert response.status_code == 200
    assert stored.rating is None
    assert stored.title == "Mary Poppins"
    assert stored.tags == ["classic"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_mask_is_a_noop() -> None:
    response, stored = await _patch({"title": "New", "rating": 1}, "?updateMask=")
    assert response.status_code == 200
    assert stored == _stored()
    assert response.json()["update_time"] == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wildcard_is_full_replacement() -> None:
    response, stored = await _patch({"title": "New"}, "?updateMask=*")
    assert response.status_code == 200
    assert stored == Book(name="publishers/p1/books/b1", title="New", update_time=8)

    response, stored = await _patch({"rating": 5}, "?updateMask=*")
    assert response.status_code == 400
    assert response.json()["detail"] == "INVALID_ARGUMENT: ['title']"
    assert stored == _stored()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_message_path_replaces_subtree_but_leaf_path_merges() -> None:
    body: dict[str, object] = {"author": {"given_name": "P. L."}}
    _, leaf = await _patch(body, "?updateMask=author.givenName")
    _, subtree = await _patch(body, "?updateMask=author")
    assert leaf.author == Author(given_name="P. L.", family_name="Travers")
    assert subtree.author == Author(given_name="P. L.", family_name="")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "loc"),
    [
        ({"name": "publishers/p1/books/other"}, ["body", "name"]),
        ({"author": {"middle_name": "X"}}, ["body", "author", "middle_name"]),
    ],
    ids=["identifier-in-body", "unknown-nested-key"],
)
@pytest.mark.asyncio
async def test_unknown_body_keys_are_rejected_with_422(
    body: dict[str, object], loc: list[str]
) -> None:
    response, stored = await _patch(body, "?updateMask=title")
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert [(err["type"], err["loc"]) for err in errors] == [("extra_forbidden", loc)]
    assert stored == _stored()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_book_returns_404_not_found() -> None:
    store = {"b1": _stored()}
    transport = httpx.ASGITransport(app=build_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch("/v1/books/missing?updateMask=title", json={"title": "New"})
    assert response.status_code == 404
    assert response.json() == {"detail": "NOT_FOUND"}
    assert store == {"b1": _stored()}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_output_only_and_identifier_paths_are_ignored() -> None:
    response, stored = await _patch({"update_time": 999}, "?updateMask=updateTime,name")
    assert response.status_code == 200
    assert stored == _stored()

    response, stored = await _patch({"update_time": 999, "title": "New"})
    assert response.status_code == 200
    assert stored.update_time == 8
    assert stored.title == "New"


@pytest.mark.unit
@pytest.mark.parametrize(
    "mask",
    [("title",), ("author",), ("author.family_name",), ("rating", "tags"), ("*",)],
    ids=["scalar", "message", "leaf", "pair", "wildcard"],
)
def test_aip161_read_then_write_with_same_mask_is_a_noop(mask: tuple[str, ...]) -> None:
    stored = _stored()
    read = project(stored.model_dump(), mask if mask != ("*",) else tuple(Book.model_fields))
    written = apply_update_mask(stored, BookPatch.model_validate(read, extra="ignore"), mask)
    assert written.model_dump(exclude={"update_time"}) == stored.model_dump(exclude={"update_time"})


@pytest.mark.unit
def test_aip161_write_then_read_with_same_mask_returns_what_was_sent() -> None:
    patch = BookPatch.model_validate({"author": {"given_name": "P. L."}})
    written = apply_update_mask(_stored(), patch, ("author",))
    # The sent author, with unset subfields at their defaults, not merged with the old value.
    assert project(written.model_dump(), ("author",)) == {
        "author": {"given_name": "P. L.", "family_name": ""}
    }


# ---------------------------------------------------------------- protobuf reference behavior


def _pb(name: str) -> Any:
    return importlib.import_module(name)


def _proto_book_class() -> Any:
    descriptor_pb2 = _pb("google.protobuf.descriptor_pb2")
    field = descriptor_pb2.FieldDescriptorProto
    fdp = descriptor_pb2.FileDescriptorProto(name="lab_book.proto", package="lab", syntax="proto3")
    author = fdp.message_type.add(name="Author")
    author.field.add(name="given_name", number=1, type=field.TYPE_STRING, json_name="givenName")
    author.field.add(name="family_name", number=2, type=field.TYPE_STRING, json_name="familyName")
    book = fdp.message_type.add(name="Book")
    book.field.add(name="title", number=1, type=field.TYPE_STRING)
    book.field.add(name="rating", number=2, type=field.TYPE_INT32)
    book.field.add(name="author", number=3, type=field.TYPE_MESSAGE, type_name=".lab.Author")
    book.field.add(name="tags", number=4, type=field.TYPE_STRING, label=field.LABEL_REPEATED)
    pool = _pb("google.protobuf.descriptor_pool").DescriptorPool()
    pool.Add(fdp)
    return _pb("google.protobuf.message_factory").GetMessageClass(
        pool.FindMessageTypeByName("lab.Book")
    )


def _field_mask(*paths: str) -> Any:
    return _pb("google.protobuf.field_mask_pb2").FieldMask(paths=list(paths))


@pytest.mark.unit
def test_lab_parser_and_canonical_form_match_protobuf_field_mask() -> None:
    fm = _field_mask()
    fm.FromJsonString("title,author.givenName")
    assert tuple(fm.paths) == parse_update_mask("title,author.givenName")
    fm.FromJsonString("")
    assert tuple(fm.paths) == parse_update_mask("")
    with pytest.raises(ValueError, match="must not contain"):
        fm.FromJsonString("author.given_name")

    raw = ("title", "author.given_name", "author", "author.family_name")
    canonical = _field_mask()
    canonical.CanonicalFormFromMask(_field_mask(*raw))
    assert tuple(canonical.paths) == canonical_mask(raw)


@pytest.mark.unit
def test_protobuf_validator_rejects_wildcard_and_repeated_index() -> None:
    descriptor = _proto_book_class().DESCRIPTOR
    assert _field_mask("author.given_name", "tags").IsValidForDescriptor(descriptor)
    assert not _field_mask("*").IsValidForDescriptor(descriptor)
    assert not _field_mask("tags.0").IsValidForDescriptor(descriptor)


@pytest.mark.unit
def test_protobuf_merge_message_appends_repeated_fields_unless_told_to_replace() -> None:
    book_cls = _proto_book_class()
    source = book_cls(tags=["new"])

    appended = book_cls(tags=["classic"])
    _field_mask("tags").MergeMessage(source, appended)
    assert list(appended.tags) == ["classic", "new"]

    replaced = book_cls(tags=["classic"])
    _field_mask("tags").MergeMessage(source, replaced, replace_repeated_field=True)
    assert list(replaced.tags) == ["new"]

    patch = BookPatch.model_validate({"tags": ["new"]})
    assert apply_update_mask(_stored(), patch, ("tags",)).tags == ["new"]


@pytest.mark.unit
def test_protobuf_merge_message_cannot_clear_a_submessage_by_default() -> None:
    book_cls = _proto_book_class()
    source = book_cls()  # author unset: the client wants to clear it

    merged = book_cls()
    merged.author.given_name = "Pamela"
    _field_mask("author").MergeMessage(source, merged)
    assert merged.HasField("author")
    assert merged.author.given_name == "Pamela"

    replaced = book_cls()
    replaced.author.given_name = "Pamela"
    _field_mask("author").MergeMessage(source, replaced, replace_message_field=True)
    assert not replaced.HasField("author")

    cleared = apply_update_mask(_stored(), BookPatch.model_validate({"author": None}), ("author",))
    assert cleared.author == Author()


@pytest.mark.unit
def test_protobuf_merge_message_raises_key_error_on_unknown_path() -> None:
    book_cls = _proto_book_class()
    with pytest.raises(KeyError, match="isbn"):
        _field_mask("isbn").MergeMessage(book_cls(), book_cls())
