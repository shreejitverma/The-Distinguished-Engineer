"""Lab for Google-AIP-System-Architecture-and-Core-Tenets: AIP-127 transcoding and AIP lint."""

from __future__ import annotations

import importlib
import re
from typing import Any

import pytest

from api_labs.f07_versioning_evolution_and_api_guidelines.aip_conformance import (
    FieldSpec,
    HttpRule,
    MessageSpec,
    MethodSpec,
    ResourceSpec,
    TemplateSyntaxError,
    TranscodeError,
    build_messages,
    error_envelope,
    lint_message,
    lint_method,
    lint_resource,
    lint_service,
    match_path,
    parse_template,
    pattern_wildcards,
    to_json,
    transcode,
)

_api_implementation: Any = importlib.import_module("google.protobuf.internal.api_implementation")
_descriptor_pb2: Any = importlib.import_module("google.protobuf.descriptor_pb2")
_field_mask_pb2: Any = importlib.import_module("google.protobuf.field_mask_pb2")
_json_format: Any = importlib.import_module("google.protobuf.json_format")
_timestamp_pb2: Any = importlib.import_module("google.protobuf.timestamp_pb2")

TS = "google.protobuf.Timestamp"
BOOK_SEGS = ("publishers", "*", "books", "*")
BOOK = MessageSpec(
    "Book",
    (
        FieldSpec("name", "string"),
        FieldSpec("display_name", "string"),
        FieldSpec("create_time", TS),
        FieldSpec("page_count", "int64"),
        FieldSpec("tags", "string", repeated=True),
        FieldSpec("rating", "int32", optional=True),
    ),
    resource=True,
)
CREATE = MessageSpec(
    "CreateBookRequest",
    (FieldSpec("parent", "string"), FieldSpec("book_id", "string"), FieldSpec("book", "Book")),
)
UPDATE = MessageSpec(
    "UpdateBookRequest",
    (FieldSpec("book", "Book"), FieldSpec("update_mask", "google.protobuf.FieldMask")),
)
DELETE = MessageSpec("DeleteBookRequest", (FieldSpec("name", "string"), FieldSpec("force", "bool")))
ARCHIVE = MessageSpec("ArchiveBookRequest", (FieldSpec("name", "string"),))
SEARCH = MessageSpec(
    "SearchBooksRequest",
    (
        FieldSpec("parent", "string"),
        FieldSpec("tags", "string", repeated=True),
        FieldSpec("page_size", "int32"),
        FieldSpec("read_mask", "google.protobuf.FieldMask"),
        FieldSpec("example", "Book"),
    ),
)
MESSAGE = MessageSpec("Message", (FieldSpec("message_id", "string"), FieldSpec("text", "string")))
RESHELVE = MessageSpec(
    "UpdateShelfBookRequest",
    (FieldSpec("shelf_book", "Book"), FieldSpec("update_mask", "google.protobuf.FieldMask")),
)

CREATE_RULE = HttpRule("POST", "/v1/{parent=publishers/*}/books", body="book")
UPDATE_RULE = HttpRule("PATCH", "/v1/{book.name=publishers/*/books/*}", body="book")
DELETE_RULE = HttpRule("DELETE", "/v1/{name=publishers/*/books/*}")
ARCHIVE_RULE = HttpRule("POST", "/v1/{name=publishers/*/books/*}:archive", body="*")
SEARCH_RULE = HttpRule("GET", "/v1/{parent=publishers/*}/books:search")
MESSAGE_RULE = HttpRule("PATCH", "/v1/messages/{message_id}", body="*")
RESHELVE_RULE = HttpRule("PATCH", "/v1/{shelf_book.name=publishers/*/books/*}", body="*")
GET_RULE = HttpRule("GET", "/v1/{name=publishers/*/books/*}")
LIST_RULE = HttpRule("GET", "/v1/{parent=publishers/*}/books")

BOOK_RESOURCE = ResourceSpec(
    "library.googleapis.com/Book", ("publishers/{publisher}/books/{book}",)
)
PUBLISHER_RESOURCE = ResourceSpec("library.googleapis.com/Publisher", ("publishers/{publisher}",))
CONFIG_RESOURCE = ResourceSpec("library.googleapis.com/Config", ("publishers/{publisher}/config",))


@pytest.fixture(scope="module")
def classes() -> dict[str, Any]:
    return build_messages(
        "library.v1", [BOOK, CREATE, UPDATE, DELETE, ARCHIVE, SEARCH, MESSAGE, RESHELVE]
    )


# --- path template grammar (google/api/http.proto) -------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "variables", "verb"),
    [
        ("/v1/{name=publishers/*/books/*}", {"name": BOOK_SEGS}, None),
        ("/v1/{parent=publishers/*}/books", {"parent": ("publishers", "*")}, None),
        ("/v1/{book.name=publishers/*/books/*}", {"book.name": BOOK_SEGS}, None),
        ("/v1/{name=publishers/*/books/*}:archive", {"name": BOOK_SEGS}, "archive"),
        ("/v1/messages/{message_id}", {"message_id": ("*",)}, None),
        ("/v1/{name=objects/**}", {"name": ("objects", "**")}, None),
    ],
)
def test_template_grammar_accepts_aip_examples(
    template: str, variables: dict[str, tuple[str, ...]], verb: str | None
) -> None:
    parsed = parse_template(template)
    assert {v.field_path: v.segments for v in parsed.variables()} == variables
    assert parsed.verb == verb


@pytest.mark.unit
@pytest.mark.parametrize(
    ("template", "reason"),
    [
        ("v1/{name=books/*}", "must start with '/'"),
        ("/v1/{name=**}/books", "'**' must be the last"),
        ("/v1/{name=publishers/{p}}", "must not contain other variables"),
        ("/v1/{name=books/*", "unbalanced"),
        ("/v1/{book-name}", "invalid field path"),
        ("/v1/books:a:b", "at most one"),
        ("/v1/books}", "unbalanced '}'"),
        ("/v1/books:bad verb", "invalid verb"),
        ("/v1/bo%2Fks", "invalid segment"),
    ],
    ids=[
        "no-leading-slash",
        "dstar-not-last",
        "nested-variable",
        "unbalanced",
        "bad-ident",
        "two-verbs",
        "stray-brace",
        "bad-verb",
        "reserved-char-literal",
    ],
)
def test_template_grammar_rejects_what_http_proto_forbids(template: str, reason: str) -> None:
    with pytest.raises(TemplateSyntaxError, match=re.escape(reason)):
        parse_template(template)


@pytest.mark.unit
def test_star_matches_exactly_one_non_empty_segment() -> None:
    t = parse_template("/v1/{name=publishers/*/books/*}")
    assert match_path(t, "/v1/publishers/p1/books/b1") == {"name": "publishers/p1/books/b1"}
    assert match_path(t, "/v1/publishers/p1/books/b1/extra") is None
    assert match_path(t, "/v1/publishers/p1/books") is None
    assert match_path(t, "/v1/publishers//books/b1") is None
    assert match_path(t, "v1/publishers/p1/books/b1") is None
    assert match_path(t, "xv1/publishers/p1/books/b1") is None


@pytest.mark.unit
def test_double_star_matches_zero_or_more_segments() -> None:
    t = parse_template("/v1/{name=objects/**}")
    assert match_path(t, "/v1/objects/a/b/c") == {"name": "objects/a/b/c"}
    assert match_path(t, "/v1/objects") == {"name": "objects"}


@pytest.mark.unit
def test_custom_verb_is_required_and_stripped_before_matching() -> None:
    t = parse_template(ARCHIVE_RULE.path)
    assert t.verb == "archive"
    assert match_path(t, "/v1/publishers/p1/books/b1:archive") == {"name": "publishers/p1/books/b1"}
    assert match_path(t, "/v1/publishers/p1/books/b1") is None
    assert match_path(t, "/v1/publishers/p1/books/b1:delete") is None


@pytest.mark.unit
def test_colon_in_last_segment_is_part_of_the_id_unless_template_declares_a_verb() -> None:
    get = parse_template("/v1/{name=publishers/*/books/*}")
    archive = parse_template(ARCHIVE_RULE.path)
    path = "/v1/publishers/p1/books/b1:archive"
    assert match_path(get, path) == {"name": "publishers/p1/books/b1:archive"}
    assert match_path(archive, path) == {"name": "publishers/p1/books/b1"}


@pytest.mark.unit
def test_single_segment_variable_decodes_slash_but_multi_segment_keeps_it() -> None:
    single = parse_template("/v1/publishers/{publisher}/books/{book}")
    multi = parse_template("/v1/{name=publishers/*/books/*}")
    path = "/v1/publishers/a%2Fb/books/c%20d"
    assert match_path(single, path) == {"publisher": "a/b", "book": "c d"}
    assert match_path(multi, path) == {"name": "publishers/a%2Fb/books/c d"}
    lower = "/v1/publishers/a%2fb/books/c"
    assert match_path(single, lower) == {"publisher": "a/b", "book": "c"}
    assert match_path(multi, lower) == {"name": "publishers/a%2fb/books/c"}
    assert match_path(parse_template("/v1/{name=**}"), "/v1/a%2Fb/c%20d") == {"name": "a%2Fb/c d"}


@pytest.mark.unit
def test_hyphen_wildcard_parent_is_an_ordinary_id_to_the_matcher() -> None:
    books = parse_template(LIST_RULE.path)
    assert match_path(books, "/v1/publishers/-/books") == {"parent": "publishers/-"}
    assert match_path(parse_template(GET_RULE.path), "/v1/publishers/-/books/b1") == {
        "name": "publishers/-/books/b1"
    }


# --- transcoding HTTP/JSON onto protobuf requests -----------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("query_key", ["bookId", "book_id"], ids=["camelCase", "proto-name"])
def test_create_binds_path_body_and_query(classes: dict[str, Any], query_key: str) -> None:
    req = transcode(
        CREATE_RULE,
        classes["CreateBookRequest"],
        "POST",
        "/v1/publishers/lacroix/books",
        query=[(query_key, "les-miserables")],
        body=b'{"displayName": "Les Miserables", "pageCount": "1463"}',
    )
    assert req.parent == "publishers/lacroix"
    assert req.book_id == "les-miserables"
    assert req.book.display_name == "Les Miserables"
    assert req.book.page_count == 1463


@pytest.mark.unit
def test_update_path_fills_the_nested_resource_name(classes: dict[str, Any]) -> None:
    req = transcode(
        UPDATE_RULE,
        classes["UpdateBookRequest"],
        "PATCH",
        "/v1/publishers/p1/books/b1",
        query=[("updateMask", "displayName,pageCount")],
        body=b'{"displayName": "New"}',
    )
    assert req.book.name == "publishers/p1/books/b1"
    assert list(req.update_mask.paths) == ["display_name", "page_count"]


@pytest.mark.unit
def test_update_rejects_body_name_that_disagrees_with_url(classes: dict[str, Any]) -> None:
    with pytest.raises(TranscodeError, match="URL path says"):
        transcode(
            UPDATE_RULE,
            classes["UpdateBookRequest"],
            "PATCH",
            "/v1/publishers/p1/books/b1",
            body=b'{"name": "publishers/victim/books/b9"}',
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rule", "request_name", "path", "body", "error"),
    [
        (MESSAGE_RULE, "Message", "/v1/messages/123456", b'{"messageId": "9"}', "URL path says"),
        (MESSAGE_RULE, "Message", "/v1/messages/123456", b'{"message_id": "9"}', "URL path says"),
        (
            MESSAGE_RULE,
            "Message",
            "/v1/messages/123456",
            b'{"message_id": "123456", "messageId": "9"}',
            "body carries both 'message_id' and 'messageId'",
        ),
        (
            RESHELVE_RULE,
            "UpdateShelfBookRequest",
            "/v1/publishers/p1/books/b1",
            b'{"shelfBook": {"name": "publishers/victim/books/b9"}}',
            "URL path says",
        ),
        (
            RESHELVE_RULE,
            "UpdateShelfBookRequest",
            "/v1/publishers/p1/books/b1",
            b'{"shelf_book": {"name": "publishers/victim/books/b9"}}',
            "URL path says",
        ),
        (
            RESHELVE_RULE,
            "UpdateShelfBookRequest",
            "/v1/publishers/p1/books/b1",
            b'{"shelf_book": {}, "shelfBook": {"name": "publishers/victim/books/b9"}}',
            "body carries both 'shelf_book' and 'shelfBook'",
        ),
    ],
    ids=["leaf-camel", "leaf-proto", "leaf-both", "parent-camel", "parent-proto", "parent-both"],
)
def test_body_star_cannot_dodge_the_conflict_check_with_either_spelling(
    classes: dict[str, Any], rule: HttpRule, request_name: str, path: str, body: bytes, error: str
) -> None:
    with pytest.raises(TranscodeError, match=error):
        transcode(rule, classes[request_name], "PATCH", path, body=body)


@pytest.mark.unit
def test_body_star_camel_case_keys_that_agree_with_the_url_are_merged(
    classes: dict[str, Any],
) -> None:
    message = transcode(
        MESSAGE_RULE,
        classes["Message"],
        "PATCH",
        "/v1/messages/123456",
        body=b'{"messageId": "123456", "text": "Hi!"}',
    )
    assert (message.message_id, message.text) == ("123456", "Hi!")
    request = transcode(
        RESHELVE_RULE,
        classes["UpdateShelfBookRequest"],
        "PATCH",
        "/v1/publishers/p1/books/b1",
        body=b'{"shelfBook": {"displayName": "X"}, "updateMask": "displayName"}',
    )
    assert request.shelf_book.name == "publishers/p1/books/b1"
    assert request.shelf_book.display_name == "X"
    assert list(request.update_mask.paths) == ["display_name"]


@pytest.mark.unit
def test_json_null_in_a_body_means_absent_so_it_cannot_clear_a_field(
    classes: dict[str, Any],
) -> None:
    def update(body: bytes) -> Any:
        return transcode(
            UPDATE_RULE,
            classes["UpdateBookRequest"],
            "PATCH",
            "/v1/publishers/p1/books/b1",
            body=body,
        )

    nulls = update(b'{"displayName": null, "rating": null, "createTime": null, "tags": null}')
    assert nulls == update(b"{}")
    assert not nulls.book.HasField("rating")
    assert update(b'{"name": null}').book.name == "publishers/p1/books/b1"
    with pytest.raises(TranscodeError, match="null is not allowed to be used as an element"):
        update(b'{"tags": [null]}')


@pytest.mark.unit
def test_path_variable_must_walk_through_singular_message_objects(
    classes: dict[str, Any],
) -> None:
    with pytest.raises(TranscodeError, match="'name' is not a singular message field"):
        transcode(
            HttpRule("GET", "/v1/books/{name.id}"),
            classes["ArchiveBookRequest"],
            "GET",
            "/v1/books/b1",
        )
    with pytest.raises(TranscodeError, match="'tags' is not a singular message field"):
        transcode(
            HttpRule("GET", "/v1/books/{tags.id}"),
            classes["SearchBooksRequest"],
            "GET",
            "/v1/books/b1",
        )
    with pytest.raises(TranscodeError, match="'shelf_book' is not an object"):
        transcode(
            RESHELVE_RULE,
            classes["UpdateShelfBookRequest"],
            "PATCH",
            "/v1/publishers/p1/books/b1",
            body=b'{"shelfBook": 5}',
        )


@pytest.mark.unit
def test_get_and_delete_bindings_refuse_a_body(classes: dict[str, Any]) -> None:
    with pytest.raises(TranscodeError, match="defines no HTTP body"):
        transcode(
            DELETE_RULE,
            classes["DeleteBookRequest"],
            "DELETE",
            "/v1/publishers/p/books/b",
            body=b"{}",
        )


@pytest.mark.unit
def test_unknown_query_parameter_is_rejected(classes: dict[str, Any]) -> None:
    with pytest.raises(TranscodeError, match="has no field 'etag'"):
        transcode(
            DELETE_RULE,
            classes["DeleteBookRequest"],
            "DELETE",
            "/v1/publishers/p/books/b",
            query=[("etag", "x")],
        )


@pytest.mark.unit
def test_body_star_leaves_no_room_for_query_parameters(classes: dict[str, Any]) -> None:
    with pytest.raises(TranscodeError, match="no fields for URL query"):
        transcode(
            ARCHIVE_RULE,
            classes["ArchiveBookRequest"],
            "POST",
            "/v1/publishers/p/books/b:archive",
            query=[("name", "x")],
            body=b"{}",
        )


@pytest.mark.unit
def test_bool_query_text_is_coerced_strictly(classes: dict[str, Any]) -> None:
    cls = classes["DeleteBookRequest"]
    path = "/v1/publishers/p/books/b"
    assert transcode(DELETE_RULE, cls, "DELETE", path, query=[("force", "true")]).force is True
    with pytest.raises(TranscodeError, match="expected true or false"):
        transcode(DELETE_RULE, cls, "DELETE", path, query=[("force", "1")])


@pytest.mark.unit
def test_wrong_http_method_does_not_match(classes: dict[str, Any]) -> None:
    with pytest.raises(TranscodeError, match="does not match"):
        transcode(DELETE_RULE, classes["DeleteBookRequest"], "GET", "/v1/publishers/p/books/b")


@pytest.mark.unit
def test_query_binds_repeated_nested_and_well_known_type_fields(classes: dict[str, Any]) -> None:
    req = transcode(
        SEARCH_RULE,
        classes["SearchBooksRequest"],
        "GET",
        "/v1/publishers/p1/books:search",
        query=[
            ("tags", "a"),
            ("tags", "b"),
            ("pageSize", "10"),
            ("readMask", "displayName,createTime"),
            ("example.displayName", "X"),
        ],
    )
    assert list(req.tags) == ["a", "b"]
    assert req.page_size == 10
    assert list(req.read_mask.paths) == ["display_name", "create_time"]
    assert req.example.display_name == "X"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "error"),
    [
        ([("pageSize", "1"), ("pageSize", "2")], "repeated for a singular field"),
        ([("pageSize.x", "1")], "walks through a scalar"),
        ([("example", "x")], "targets a message field"),
        ([("pageSize", "ten")], r"invalid literal for int\(\) with base 10: 'ten'"),
        ([("tags.x", "1")], "walks through a scalar"),
    ],
    ids=["singular-twice", "through-scalar", "message-leaf", "bad-int", "through-repeated"],
)
def test_query_parameters_that_cannot_map_are_rejected(
    classes: dict[str, Any], query: list[tuple[str, str]], error: str
) -> None:
    with pytest.raises(TranscodeError, match=error):
        transcode(
            SEARCH_RULE,
            classes["SearchBooksRequest"],
            "GET",
            "/v1/publishers/p1/books:search",
            query=query,
        )


@pytest.mark.unit
def test_body_star_must_be_a_json_object(classes: dict[str, Any]) -> None:
    with pytest.raises(TranscodeError, match="must be a JSON object"):
        transcode(
            ARCHIVE_RULE,
            classes["ArchiveBookRequest"],
            "POST",
            "/v1/publishers/p/books/b:archive",
            body=b"[1]",
        )


# --- ProtoJSON parity (protobuf 7.36.2 json_format) -----------------------------------------


@pytest.mark.unit
def test_response_uses_lower_camel_keys_int64_strings_and_z_timestamps(
    classes: dict[str, Any],
) -> None:
    book = classes["Book"](name="publishers/p/books/b", display_name="X", page_count=2**60)
    book.create_time.FromJsonString("2026-11-11T09:30:00+05:30")
    assert to_json(book) == {
        "name": "publishers/p/books/b",
        "displayName": "X",
        "createTime": "2026-11-11T04:00:00Z",
        "pageCount": "1152921504606846976",
    }


@pytest.mark.unit
def test_protojson_omits_default_values_unless_the_field_has_presence(
    classes: dict[str, Any],
) -> None:
    book = classes["Book"](page_count=0)
    assert to_json(book) == {}
    assert _json_format.MessageToDict(book, always_print_fields_with_no_presence=True) == {
        "name": "",
        "displayName": "",
        "pageCount": "0",
        "tags": [],
    }
    assert not book.HasField("rating")
    file = _descriptor_pb2.FileDescriptorProto.FromString(book.DESCRIPTOR.file.serialized_pb)
    declared = next(m for m in file.message_type if m.name == "Book")
    assert [f.name for f in declared.field if f.proto3_optional] == ["rating"]
    rated = classes["Book"](rating=0)
    assert rated.HasField("rating")
    assert to_json(rated) == {"rating": 0}
    assert rated != book


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("2026-11-11T00:00:00", "time data '2026-11' does not match"),
        ("2026-11-11T00:00:00z", "time data '2026-11' does not match"),
        ("2026-11-11t00:00:00Z", "lowercase 't' is not accepted"),
    ],
    ids=["no-offset", "lowercase-z", "lowercase-t"],
)
def test_timestamp_parser_is_stricter_than_rfc_3339(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _timestamp_pb2.Timestamp().FromJsonString(text)


@pytest.mark.unit
def test_timestamp_json_takes_at_most_nine_fraction_digits_and_emits_0_3_6_or_9() -> None:
    stamp = _timestamp_pb2.Timestamp()
    with pytest.raises(ValueError, match="more than 9 fractional digits"):
        stamp.FromJsonString("2026-11-11T00:00:00.123456789012Z")
    rendered = [
        _timestamp_pb2.Timestamp(seconds=0, nanos=nanos).ToJsonString()
        for nanos in (0, 5_000_000, 5_000, 5)
    ]
    assert rendered == [
        "1970-01-01T00:00:00Z",
        "1970-01-01T00:00:00.005Z",
        "1970-01-01T00:00:00.000005Z",
        "1970-01-01T00:00:00.000000005Z",
    ]


@pytest.mark.unit
def test_protojson_itself_rejects_a_quoted_bool(classes: dict[str, Any]) -> None:
    with pytest.raises(_json_format.ParseError) as caught:
        _json_format.ParseDict({"force": "true"}, classes["DeleteBookRequest"]())
    assert str(caught.value) == (
        "Failed to parse force field: Expected true or false without quotes"
        " at DeleteBookRequest.force."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "doc",
    [
        {"messageId": "first", "message_id": "second"},
        {"message_id": "first", "messageId": "second"},
    ],
    ids=["camel-then-proto", "proto-then-camel"],
)
def test_protojson_lets_the_later_spelling_of_one_field_win(
    classes: dict[str, Any], doc: dict[str, str]
) -> None:
    assert _json_format.ParseDict(doc, classes["Message"]()).message_id == "second"


@pytest.mark.unit
def test_parse_dict_accepts_100_nested_messages_and_rejects_101() -> None:
    node = build_messages("depth.v1", [MessageSpec("Node", (FieldSpec("child", "Node"),))])["Node"]

    def nested(messages: int) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        for _ in range(messages - 1):
            doc = {"child": doc}
        return doc

    _json_format.ParseDict(nested(100), node())
    with pytest.raises(
        _json_format.ParseError, match=r"Message too deep\. Max recursion depth is 100"
    ):
        _json_format.ParseDict(nested(101), node())


@pytest.mark.unit
def test_digit_leading_word_gets_a_json_name_that_drops_the_underscore() -> None:
    spec = MessageSpec("Odd", (FieldSpec("foo_1bar", "string"), FieldSpec("page_count", "int64")))
    descriptor = build_messages("odd.v1", [spec])["Odd"].DESCRIPTOR
    assert [(f.name, f.json_name) for f in descriptor.fields] == [
        ("foo_1bar", "foo1bar"),
        ("page_count", "pageCount"),
    ]


@pytest.mark.unit
def test_colliding_json_names_fail_on_upb_and_lose_data_on_the_pure_python_backend() -> None:
    spec = MessageSpec("Clash", (FieldSpec("a_b", "string"), FieldSpec("a__b", "string")))
    backend = _api_implementation.Type()
    assert backend in {"upb", "python"}
    if backend == "upb":
        with pytest.raises(TypeError, match=r"duplicate json_name \(aB\)"):
            build_messages("clash.v1", [spec])
        return
    clash = build_messages("clash.v1", [spec])["Clash"]
    assert [(f.name, f.json_name) for f in clash.DESCRIPTOR.fields] == [
        ("a_b", "aB"),
        ("a__b", "aB"),
    ]
    parsed = _json_format.ParseDict({"aB": "x"}, clash())
    assert (parsed.a_b, parsed.a__b) == ("", "x")
    assert to_json(clash(a_b="1", a__b="2")) == {"aB": "2"}


@pytest.mark.unit
def test_field_mask_json_round_trip_breaks_when_a_word_starts_with_a_digit() -> None:
    ok = _field_mask_pb2.FieldMask(paths=["display_name", "create_time"])
    assert ok.ToJsonString() == "displayName,createTime"
    with pytest.raises(ValueError, match='character after a "_" must be a lowercase letter'):
        _field_mask_pb2.FieldMask(paths=["foo_1bar"]).ToJsonString()
    back = _field_mask_pb2.FieldMask()
    back.FromJsonString("foo1bar")
    assert list(back.paths) == ["foo1bar"]


@pytest.mark.unit
def test_update_mask_wildcard_is_an_api_convention_not_a_field_path(
    classes: dict[str, Any],
) -> None:
    descriptor = classes["Book"].DESCRIPTOR
    assert _field_mask_pb2.FieldMask(paths=["display_name"]).IsValidForDescriptor(descriptor)
    assert not _field_mask_pb2.FieldMask(paths=["*"]).IsValidForDescriptor(descriptor)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("OK", 200),
        ("CANCELLED", 499),
        ("UNKNOWN", 500),
        ("INVALID_ARGUMENT", 400),
        ("DEADLINE_EXCEEDED", 504),
        ("NOT_FOUND", 404),
        ("ALREADY_EXISTS", 409),
        ("PERMISSION_DENIED", 403),
        ("UNAUTHENTICATED", 401),
        ("RESOURCE_EXHAUSTED", 429),
        ("FAILED_PRECONDITION", 400),
        ("ABORTED", 409),
        ("OUT_OF_RANGE", 400),
        ("UNIMPLEMENTED", 501),
        ("INTERNAL", 500),
        ("UNAVAILABLE", 503),
        ("DATA_LOSS", 500),
    ],
)
def test_error_envelope_carries_http_status_and_enum_name(code: str, status: int) -> None:
    assert error_envelope(code, "m") == (
        status,
        {"error": {"code": status, "message": "m", "status": code}},
    )


# --- the AIP linter -------------------------------------------------------------------------


@pytest.mark.unit
def test_compliant_library_service_has_no_findings() -> None:
    methods = [
        MethodSpec("GetBook", HttpRule("GET", "/v1/{name=publishers/*/books/*}")),
        MethodSpec("ListBooks", HttpRule("GET", "/v1/{parent=publishers/*}/books")),
        MethodSpec("CreateBook", CREATE_RULE, resource_field="book"),
        MethodSpec("UpdateBook", UPDATE_RULE, resource_field="book"),
        MethodSpec("DeleteBook", DELETE_RULE),
        MethodSpec("ArchiveBook", ARCHIVE_RULE),
        MethodSpec(
            "TranslateText", HttpRule("POST", "/v1/{project=projects/*}:translateText", "*")
        ),
        MethodSpec("SortBooks", HttpRule("POST", "/v1/{parent=publishers/*}/books:sort", "*")),
        MethodSpec("SortBooks", HttpRule("POST", "/v1/books:sort", "*")),
        MethodSpec("SearchBooks", SEARCH_RULE),
        MethodSpec("ListenBook", HttpRule("POST", "/v1/{name=publishers/*/books/*}:listen", "*")),
        MethodSpec("GetBook", HttpRule("GET", "/v1/{name=publishers/*/books/-}")),
        MethodSpec("GetBook", GET_RULE, resource=BOOK_RESOURCE),
        MethodSpec("ListBooks", LIST_RULE, resource=BOOK_RESOURCE),
        MethodSpec("CreateBook", CREATE_RULE, resource_field="book", resource=BOOK_RESOURCE),
        MethodSpec("UpdateBook", UPDATE_RULE, resource_field="book", resource=BOOK_RESOURCE),
        MethodSpec("DeleteBook", DELETE_RULE, resource=BOOK_RESOURCE),
        MethodSpec("ArchiveBook", ARCHIVE_RULE, resource=BOOK_RESOURCE),
        MethodSpec(
            "GetPublisher", HttpRule("GET", "/v1/{name=publishers/*}"), None, PUBLISHER_RESOURCE
        ),
        MethodSpec("ListPublishers", HttpRule("GET", "/v1/publishers"), None, PUBLISHER_RESOURCE),
        MethodSpec(
            "CreatePublisher",
            HttpRule("POST", "/v1/publishers", "publisher"),
            "publisher",
            PUBLISHER_RESOURCE,
        ),
    ]
    assert [f for m in methods for f in lint_method(m)] == []
    assert [f for s in (BOOK, CREATE, UPDATE, DELETE) for f in lint_message(s)] == []
    assert lint_service(methods) == []
    assert [
        f for r in (BOOK_RESOURCE, PUBLISHER_RESOURCE, CONFIG_RESOURCE) for f in lint_resource(r)
    ] == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "rules"),
    [
        (
            MethodSpec("GetBook", HttpRule("POST", "/v1/{name=publishers/*/books/*}", "*")),
            ["core::0131::http-method", "core::0131::http-body"],
        ),
        (
            MethodSpec(
                "UpdateBook", HttpRule("PUT", "/v1/{name=publishers/*/books/*}", "book"), "book"
            ),
            ["core::0134::http-method", "core::0134::http-uri-name"],
        ),
        (
            MethodSpec(
                "CreateBook", HttpRule("POST", "/v1/{parent=publishers/*}/books", "*"), "book"
            ),
            ["core::0133::http-body"],
        ),
        (
            MethodSpec("DeleteBook", HttpRule("DELETE", "/v1/{name=publishers/*/books/*}", "*")),
            ["core::0135::http-body"],
        ),
        (
            MethodSpec(
                "ArchiveBook", HttpRule("PATCH", "/v1/{name=publishers/*/books/*}:arc", "*")
            ),
            ["core::0136::http-method", "core::0136::http-uri-suffix"],
        ),
        (
            MethodSpec("ArchiveBook", HttpRule("POST", "/v1/{name=publishers/*/books/*}", "*")),
            ["core::0136::http-uri-suffix"],
        ),
        (
            MethodSpec("ListBooks", HttpRule("GET", "v1/{parent=publishers/*}/books")),
            ["core::0127::http-template-syntax"],
        ),
        (
            MethodSpec(
                "SearchBooks", HttpRule("GET", "/v1/{parent=publishers/*}/books:search", "*")
            ),
            ["core::0136::http-body"],
        ),
        (
            MethodSpec("SortBooks", HttpRule("POST", "/v1/books:sortBooks", "*")),
            ["core::0136::http-uri-suffix"],
        ),
        (
            MethodSpec(
                "TranslateText", HttpRule("POST", "/v1/{project=projects/*}:translate", "*")
            ),
            ["core::0136::http-uri-suffix"],
        ),
        (
            MethodSpec("GetBook", HttpRule("GET", "/v1/publishers/{publisher}/books/{book}")),
            ["core::0131::http-uri-name"],
        ),
        (
            MethodSpec("DeleteBook", HttpRule("DELETE", "/v1/publishers/{publisher}/books/{book}")),
            ["core::0135::http-uri-name"],
        ),
        (
            MethodSpec("ListBooks", HttpRule("GET", "/v1/publishers/{publisher}/books")),
            ["core::0132::http-uri-parent"],
        ),
        (
            MethodSpec("ListBooks", HttpRule("GET", "/v1/books"), None, BOOK_RESOURCE),
            ["core::0132::http-uri-parent"],
        ),
        (
            MethodSpec("CreateBook", HttpRule("POST", "/v1/books", "book"), "book"),
            ["core::0133::http-uri-parent"],
        ),
        (
            MethodSpec(
                "CreateBook",
                HttpRule("POST", "/v1/{parent=publishers/*}/books/{book_id}", "book"),
                "book",
            ),
            ["core::0133::http-uri-parent"],
        ),
        (
            MethodSpec("ListBooks", HttpRule("GET", "/v1/{parent=publishers/-}/books")),
            ["core::0159::hardcoded-hyphen"],
        ),
        (
            MethodSpec(
                "GetBook", HttpRule("GET", "/v1/{name=publishers/*/book/*}"), None, BOOK_RESOURCE
            ),
            ["core::0127::http-template-pattern"],
        ),
        (
            MethodSpec(
                "UpdateBook",
                HttpRule("PATCH", "/v1/{book.name=publishers/*}", "book"),
                "book",
                BOOK_RESOURCE,
            ),
            ["core::0127::http-template-pattern"],
        ),
        (
            MethodSpec(
                "ArchiveBook",
                HttpRule("POST", "/v1/{name=shelves/*/books/*}:archive", "*"),
                None,
                BOOK_RESOURCE,
            ),
            ["core::0127::http-template-pattern"],
        ),
    ],
    ids=[
        "get-post",
        "update-put",
        "create-star",
        "delete-body",
        "custom-patch",
        "no-verb",
        "syntax",
        "custom-get-body",
        "collection-verbnoun",
        "stateless-verb-only",
        "get-id-fragments",
        "delete-id-fragments",
        "list-id-fragment",
        "list-nested-no-parent",
        "create-no-parent",
        "create-two-variables",
        "hard-coded-hyphen",
        "get-wrong-collection",
        "update-parent-pattern",
        "custom-wrong-pattern",
    ],
)
def test_method_lint_flags_each_violation(method: MethodSpec, rules: list[str]) -> None:
    assert [f.rule for f in lint_method(method)] == rules


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "rules"),
    [
        (FieldSpec("displayName", "string"), ["core::0140::lower-snake"]),
        (FieldSpec("foo_1bar", "string"), ["core::0140::numbers"]),
        (FieldSpec("page__count", "int64"), ["core::0140::underscores"]),
        (FieldSpec("created_time", TS), ["core::0142::time-field-names"]),
        (FieldSpec("publish", TS), ["core::0142::time-field-names"]),
        (FieldSpec("launch_times", TS, repeated=True), []),
        (FieldSpec("expire_time", "string"), ["core::0142::time-field-type"]),
        (FieldSpec("first_name", "string"), ["core::0148::human-names"]),
    ],
    ids=["camel", "digit-word", "double-underscore", "past-tense", "no-suffix", "ok-plural",
         "string-time", "first-name"],
)  # fmt: skip
def test_message_lint_flags_each_violation(field: FieldSpec, rules: list[str]) -> None:
    spec = MessageSpec("Book", (FieldSpec("name", "string"), field), resource=True)
    assert [f.rule for f in lint_message(spec)] == rules


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields",
    [
        (FieldSpec("name", "int64"),),
        (FieldSpec("name", "string", repeated=True),),
        (FieldSpec("display_name", "string"),),
    ],
    ids=["int64-name", "repeated-name", "no-name"],
)
def test_resource_without_singular_string_name_is_flagged(fields: tuple[FieldSpec, ...]) -> None:
    spec = MessageSpec("Book", fields, resource=True)
    assert [f.rule for f in lint_message(spec)] == ["core::0123::resource-name-field"]
    assert lint_message(MessageSpec("Book", fields)) == []


# --- resource annotations (AIP-123) and required methods (AIP-121) ---------------------------


@pytest.mark.unit
def test_http_template_is_the_resource_pattern_with_wildcards() -> None:
    assert pattern_wildcards(BOOK_RESOURCE.patterns[0]) == "/".join(BOOK_SEGS)
    name = parse_template(GET_RULE.path).variables()[0]
    assert name.segments == tuple(pattern_wildcards(BOOK_RESOURCE.patterns[0]).split("/"))
    multi = ResourceSpec(
        "library.googleapis.com/Book", ("shelves/{shelf}/books/{book}", "books/{book}")
    )
    for path in ("/v1/{name=books/*}", "/v1/{name=shelves/*/books/*}", "/v1/{name=**}"):
        assert lint_method(MethodSpec("GetBook", HttpRule("GET", path), None, multi)) == []
    for path in ("/v1/{name=books/*/*}", "/v1/{name=shelves/*/pages/*}", "/v1/{name=b.oks/*}"):
        assert [
            f.rule for f in lint_method(MethodSpec("GetBook", HttpRule("GET", path), None, multi))
        ] == ["core::0127::http-template-pattern"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("patterns", "rules"),
    [
        (("publishers/{publisher}/books/{book}",), []),
        (("publishers/{publisher}/config",), []),
        ((), ["core::0123::resource-pattern"]),
        (("book_shelves/{book_shelf}",), ["core::0123::resource-pattern"]),
        (("publishers/{publisher_id}/books/{book}",), ["core::0123::resource-variables"]),
        (("publishers/{Publisher}/books/{book}",), ["core::0123::resource-variables"]),
        (
            ("books/{book}", "book shelves/{shelfId}/books/{book}"),
            ["core::0123::resource-pattern", "core::0123::resource-variables"],
        ),
    ],
    ids=["ok", "ok-singleton", "none", "snake-collection", "id-suffix", "upper-variable", "second"],
)
def test_resource_pattern_lint_flags_each_violation(
    patterns: tuple[str, ...], rules: list[str]
) -> None:
    resource = ResourceSpec("library.googleapis.com/Book", patterns)
    assert [f.rule for f in lint_resource(resource)] == rules


@pytest.mark.unit
def test_resource_shape_is_read_from_its_patterns() -> None:
    assert (BOOK_RESOURCE.has_parent, BOOK_RESOURCE.singleton) == (True, False)
    assert (PUBLISHER_RESOURCE.has_parent, PUBLISHER_RESOURCE.singleton) == (False, False)
    assert (CONFIG_RESOURCE.has_parent, CONFIG_RESOURCE.singleton) == (False, True)
    assert not ResourceSpec("library.googleapis.com/Book", ()).singleton


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rpcs", "resource", "rules"),
    [
        (("GetBook", "ListBooks", "CreateBook"), BOOK_RESOURCE, []),
        (
            ("CreateBook",),
            BOOK_RESOURCE,
            ["core::0121::resource-must-support-get", "core::0121::resource-must-support-list"],
        ),
        (("ListBooks",), BOOK_RESOURCE, ["core::0121::resource-must-support-get"]),
        (("UpdateBook", "ListBooks"), BOOK_RESOURCE, ["core::0121::resource-must-support-get"]),
        (("GetBook",), BOOK_RESOURCE, ["core::0121::resource-must-support-list"]),
        (("GetBook", "UpdateBook"), BOOK_RESOURCE, ["core::0121::resource-must-support-list"]),
        (
            ("UpdateBook",),
            BOOK_RESOURCE,
            ["core::0121::resource-must-support-get", "core::0121::resource-must-support-list"],
        ),
        (("GetConfig", "UpdateConfig"), CONFIG_RESOURCE, []),
        (("UpdateConfig",), CONFIG_RESOURCE, ["core::0121::resource-must-support-get"]),
        (("DeleteBook", "ArchiveBook"), BOOK_RESOURCE, []),
    ],
    ids=[
        "complete",
        "create-only",
        "list-only",
        "update-and-list",
        "get-only",
        "get-and-update",
        "update-only",
        "singleton-needs-no-list",
        "singleton-needs-get",
        "delete-and-custom-do-not-count",
    ],
)
def test_resource_must_support_get_and_list_unless_singleton(
    rpcs: tuple[str, ...], resource: ResourceSpec, rules: list[str]
) -> None:
    methods = [MethodSpec(rpc, GET_RULE, resource=resource) for rpc in rpcs]
    assert [f.rule for f in lint_service(methods)] == rules
    assert {f.target for f in lint_service(methods)} <= {resource.type}


@pytest.mark.unit
def test_service_lint_tracks_each_resource_separately_and_ignores_unannotated_methods() -> None:
    methods = [
        MethodSpec("GetBook", GET_RULE, resource=BOOK_RESOURCE),
        MethodSpec("ListBooks", LIST_RULE, resource=BOOK_RESOURCE),
        MethodSpec("CreatePublisher", HttpRule("POST", "/v1/publishers", "publisher"), "publisher"),
        MethodSpec("ListPublishers", HttpRule("GET", "/v1/publishers"), None, PUBLISHER_RESOURCE),
    ]
    assert [(f.rule, f.target) for f in lint_service(methods)] == [
        ("core::0121::resource-must-support-get", "library.googleapis.com/Publisher")
    ]
