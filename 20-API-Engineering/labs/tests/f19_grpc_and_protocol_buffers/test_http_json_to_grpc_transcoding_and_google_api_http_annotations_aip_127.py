"""Lab for HTTP/JSON to gRPC transcoding with google.api.http annotations (AIP-127)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from api_labs.f19_grpc_and_protocol_buffers.http_json_to_grpc_transcoding import (
    HTTP_STATUS_BY_CODE,
    LIBRARY_METHODS,
    LIBRARY_PACKAGE,
    LIBRARY_SERVICE,
    GrpcCode,
    HttpRule,
    RpcMethod,
    TemplateError,
    Transcoder,
    TranscoderOptions,
    TranscodingError,
    UnescapeSpec,
    apply_update,
    build_gateway_app,
    build_library_transcoder,
    build_pool,
    convert_grpc_status,
    frame_grpc_message,
    grpc_code_for_http,
    http_status_for,
    lint_rule,
    new_message,
    parse_template,
    render_response,
    split_request_path,
    url_unescape,
)

ENVOY_DETAILS_BIN = "CAUaMwoqdHlwZS5nb29nbGVhcGlzLmNvbS9nb29nbGUucnBjLlJlcXVlc3RJbmZvEgUKA3ItMQ"


def _method(name: str, request: str, rule: HttpRule) -> RpcMethod:
    return RpcMethod(
        LIBRARY_SERVICE, name, f"{LIBRARY_PACKAGE}.{request}", f"{LIBRARY_PACKAGE}.Book", rule
    )


def _transcoder(*methods: RpcMethod, options: TranscoderOptions | None = None) -> Transcoder:
    transcoder = Transcoder(build_pool(), options or TranscoderOptions())
    for method in methods:
        transcoder.register(method)
    return transcoder


def _fail(
    transcoder: Transcoder, http_method: str, target: str, body: bytes = b""
) -> TranscodingError:
    with pytest.raises(TranscodingError) as caught:
        transcoder.transcode(http_method, target, body)
    return caught.value


# ---------------------------------------------------------------------------
# Path templates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_variable_is_a_single_star_segment() -> None:
    template = parse_template("/v1/{name}")
    assert template.segments == ("v1", "*")
    assert template.variables[0].field_path == ("name",)
    assert (template.variables[0].start, template.variables[0].end) == (1, 2)


@pytest.mark.unit
def test_variable_with_a_pattern_records_the_segment_range_and_nested_field_path() -> None:
    template = parse_template("/v1/{book.name=publishers/*/books/*}:archive")
    assert template.segments == ("v1", "publishers", "*", "books", "*")
    assert template.verb == "archive"
    variable = template.variables[0]
    assert variable.field_path == ("book", "name")
    assert (variable.start, variable.end) == (1, 5)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "v1/books",
        "/v1/{a={b}}",
        "/v1/{=books/*}",
        "/v1/{a.}",
        "/v1/{a",
        "/v1/**/books",
        "/v1/{a=**}/{b}",
        "/v1/books:",
        "/v1/a*b",
        "/v1//books",
    ],
)
def test_malformed_templates_are_rejected(text: str) -> None:
    with pytest.raises(TemplateError):
        parse_template(text)


@pytest.mark.unit
def test_literals_after_double_star_need_the_envoy_extension_and_never_allow_wildcards() -> None:
    with pytest.raises(TemplateError):
        parse_template("/v1/{name=**}/history")
    template = parse_template("/v1/{name=**}/history", literals_after_double_star=True)
    assert template.segments == ("v1", "**", "history")
    assert template.variables[0].has_double_star
    with pytest.raises(TemplateError):
        parse_template("/v1/**/*", literals_after_double_star=True)


@pytest.mark.unit
def test_root_template_has_no_segments() -> None:
    assert parse_template("/").segments == ()


# ---------------------------------------------------------------------------
# Percent decoding and request path splitting
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (UnescapeSpec.ALL_CHARACTERS_EXCEPT_RESERVED, "%2f%23/ %23"),
        (UnescapeSpec.ALL_CHARACTERS_EXCEPT_SLASH, "%2f#/ %23"),
        (UnescapeSpec.ALL_CHARACTERS, "/#/ %23"),
    ],
)
def test_unescape_specs_match_the_envoy_documented_examples(
    spec: UnescapeSpec, expected: str
) -> None:
    assert url_unescape("%2f%23/%20%2523", spec) == expected


@pytest.mark.unit
def test_unescape_edge_cases() -> None:
    assert url_unescape("a+b") == "a+b"
    assert url_unescape("a+b", plus_as_space=True) == "a b"
    assert url_unescape("%zz%4") == "%zz%4"
    assert url_unescape("%C3%A9") == "\u00e9"
    with pytest.raises(TranscodingError) as caught:
        url_unescape("%FF")
    assert caught.value.code is GrpcCode.INVALID_ARGUMENT


@pytest.mark.unit
def test_custom_verb_is_taken_only_when_registered_and_after_the_last_slash() -> None:
    verbs = frozenset({"archive"})
    assert split_request_path("/v1/books/42:archive?x=1", verbs) == (
        ["v1", "books", "42"],
        "archive",
    )
    assert split_request_path("/v1/books/42:other", verbs) == (["v1", "books", "42:other"], "")
    assert split_request_path("/v1/a:archive/b", verbs) == (["v1", "a:archive", "b"], "")
    assert split_request_path("/v1/books/42:other", verbs, match_unregistered_custom_verb=True) == (
        ["v1", "books", "42"],
        "other",
    )


@pytest.mark.unit
def test_only_trailing_empty_segments_are_dropped() -> None:
    assert split_request_path("/v1/books//", frozenset()) == (["v1", "books"], "")
    assert split_request_path("/v1//books", frozenset()) == (["v1", "", "books"], "")
    assert split_request_path("/", frozenset()) == ([], "")


# ---------------------------------------------------------------------------
# Matching and registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_literal_segment_beats_a_variable_regardless_of_registration_order() -> None:
    get = _method("GetBook", "GetBookRequest", HttpRule("GET", "/v1/{name=books/*}"))
    featured = _method("ListBooks", "ListBooksRequest", HttpRule("GET", "/v1/books/featured"))
    for methods in ((get, featured), (featured, get)):
        transcoder = _transcoder(*methods)
        found = transcoder.lookup("GET", "/v1/books/featured")
        assert found is not None
        assert found[0].method.name == "ListBooks"
        other = transcoder.lookup("GET", "/v1/books/42")
        assert other is not None
        assert other[0].method.name == "GetBook"


@pytest.mark.unit
def test_star_matches_one_segment_and_never_crosses_a_slash() -> None:
    transcoder = build_library_transcoder()
    assert transcoder.lookup("GET", "/v1/books/42") is not None
    assert transcoder.lookup("GET", "/v1/books/42/pages") is None
    assert _fail(transcoder, "GET", "/v1/books/42/pages").http_status == 404


@pytest.mark.unit
def test_double_star_matches_zero_or_more_segments() -> None:
    transcoder = _transcoder(_method("GetBook", "GetBookRequest", HttpRule("GET", "/v1/{name=**}")))
    assert transcoder.transcode("GET", "/v1/a/b/c").message.name == "a/b/c"
    assert transcoder.transcode("GET", "/v1/a").message.name == "a"
    assert transcoder.transcode("GET", "/v1").message.name == ""


@pytest.mark.unit
def test_http_method_is_part_of_the_route_key() -> None:
    transcoder = build_library_transcoder()
    assert transcoder.lookup("GET", "/v1/books/42") is not None
    assert transcoder.lookup("DELETE", "/v1/books/42") is not None
    assert transcoder.lookup("PUT", "/v1/books/42") is None


@pytest.mark.unit
def test_two_rules_with_the_same_shape_make_the_route_ambiguous() -> None:
    by_name = _method("GetBook", "GetBookRequest", HttpRule("GET", "/v1/{name=books/*}"))
    by_parent = _method("ListBooks", "ListBooksRequest", HttpRule("GET", "/v1/{parent=books/*}"))
    assert _transcoder(by_name).lookup("GET", "/v1/books/42") is not None
    assert _transcoder(by_name, by_parent).lookup("GET", "/v1/books/42") is None


@pytest.mark.unit
def test_registered_custom_verb_routes_and_unregistered_colon_stays_in_the_segment() -> None:
    transcoder = build_library_transcoder()
    archived = transcoder.transcode("POST", "/v1/books/42:archive", b"{}")
    assert archived.method.name == "ArchiveBook"
    assert archived.message.name == "books/42"
    assert _fail(transcoder, "GET", "/v1/books/42:archive").http_status == 404
    colon = transcoder.transcode("GET", "/v1/books/42:other")
    assert colon.method.name == "GetBook"
    assert colon.message.name == "books/42:other"
    strict = build_library_transcoder(TranscoderOptions(match_unregistered_custom_verb=True))
    assert _fail(strict, "GET", "/v1/books/42:other").http_status == 404


@pytest.mark.unit
def test_registration_rejects_invalid_rules() -> None:
    cases = [
        _method("GetBook", "GetBookRequest", HttpRule("GET", "/v1/{missing=books/*}")),
        _method("ListBooks", "ListBooksRequest", HttpRule("GET", "/v1/{filter_tags}")),
        _method("ListBooks", "ListBooksRequest", HttpRule("GET", "/v1/{filter_book}")),
        _method("CreateBook", "CreateBookRequest", HttpRule("POST", "/v1/books", body="nope")),
        _method(
            "CreateBook",
            "CreateBookRequest",
            HttpRule(
                "POST",
                "/v1/books",
                body="book",
                additional_bindings=(
                    HttpRule(
                        "POST",
                        "/v2/books",
                        body="book",
                        additional_bindings=(HttpRule("POST", "/v3/books", body="book"),),
                    ),
                ),
            ),
        ),
    ]
    for case in cases:
        with pytest.raises(TemplateError):
            _transcoder(case)


@pytest.mark.unit
def test_additional_bindings_reach_the_same_rpc() -> None:
    transcoder = build_library_transcoder()
    flat = transcoder.transcode("POST", "/v1/books?bookId=b1", b'{"title": "Dune"}')
    nested = transcoder.transcode("POST", "/v1/publishers/p9/books?bookId=b1", b'{"title": "Dune"}')
    assert flat.method.name == nested.method.name == "CreateBook"
    assert flat.message.parent == ""
    assert nested.message.parent == "publishers/p9"
    assert nested.message.book.title == "Dune"
    assert nested.message.book_id == "b1"


# ---------------------------------------------------------------------------
# Bindings: the seed example and the weaving rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_patch_with_update_mask_query_becomes_an_update_book_request() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("PATCH", "/v1/books/42?updateMask=title", b'{"title": "Dune"}')
    assert result.method.grpc_path == "/library.v1.Library/UpdateBook"
    assert result.message.book.name == "books/42"
    assert result.message.book.title == "Dune"
    assert list(result.message.update_mask.paths) == ["title"]
    assert result.wire.hex() == "0a100a08626f6f6b732f3432120444756e6512070a057469746c65"
    assert result.frame.hex() == "000000001b" + result.wire.hex()
    assert [(b.field_path, b.source) for b in result.bindings] == [
        (("book", "name"), "path"),
        (("updateMask",), "query"),
    ]


@pytest.mark.unit
def test_update_mask_is_converted_from_json_camel_case_to_proto_snake_case() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("PATCH", "/v1/books/42?updateMask=pageCount,title", b"{}")
    assert list(result.message.update_mask.paths) == ["page_count", "title"]


@pytest.mark.unit
def test_an_empty_update_mask_is_present_but_an_omitted_one_is_not() -> None:
    transcoder = build_library_transcoder()
    empty = transcoder.transcode("PATCH", "/v1/books/42?updateMask=", b'{"title": "x"}')
    omitted = transcoder.transcode("PATCH", "/v1/books/42", b'{"title": "x"}')
    assert empty.message.HasField("update_mask")
    assert not omitted.message.HasField("update_mask")


@pytest.mark.unit
def test_the_body_value_wins_over_a_path_binding_by_default() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("PATCH", "/v1/books/42", b'{"name": "books/7", "title": "x"}')
    assert result.message.book.name == "books/7"


@pytest.mark.unit
def test_reject_binding_body_field_collisions_rejects_only_differing_values() -> None:
    strict = build_library_transcoder(TranscoderOptions(reject_binding_body_field_collisions=True))
    error = _fail(strict, "PATCH", "/v1/books/42", b'{"name": "books/7"}')
    assert error.code is GrpcCode.INVALID_ARGUMENT
    assert "conflicting" in error.message
    same = strict.transcode("PATCH", "/v1/books/42", b'{"name": "books/42"}')
    assert same.message.book.name == "books/42"


@pytest.mark.unit
def test_a_collision_is_detected_whichever_spelling_the_body_uses() -> None:
    strict = build_library_transcoder(TranscoderOptions(reject_binding_body_field_collisions=True))
    for body in (b'{"pageCount": "7"}', b'{"page_count": "7"}'):
        error = _fail(strict, "PATCH", "/v1/books/42?book.pageCount=8", body)
        assert error.code is GrpcCode.INVALID_ARGUMENT
    equal = strict.transcode("PATCH", "/v1/books/42?book.pageCount=7", b'{"page_count": 7}')
    assert equal.message.book.page_count == 7


@pytest.mark.unit
def test_repeated_fields_append_the_url_values_after_the_body_values() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode(
        "PATCH", "/v1/books/42?book.tags=y&book.tags=z", b'{"tags": ["x"]}'
    )
    assert list(result.message.book.tags) == ["x", "y", "z"]
    listing = transcoder.transcode("GET", "/v1/books?filterTags=a&filterTags=b")
    assert list(listing.message.filter_tags) == ["a", "b"]


@pytest.mark.unit
def test_nested_message_fields_bind_by_dotted_path_in_either_spelling() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode(
        "GET", "/v1/books?filterBook.pageCount=9&filter_book.genre=FICTION&pageSize=5"
    )
    assert result.message.filter_book.page_count == 9
    assert result.message.filter_book.genre == 1
    assert result.message.page_size == 5


@pytest.mark.unit
def test_unknown_body_fields_are_dropped_but_unknown_query_parameters_are_errors() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("PATCH", "/v1/books/42", b'{"title": "x", "titel": "typo"}')
    assert result.message.book.title == "x"
    error = _fail(transcoder, "GET", "/v1/books?bogus=1")
    assert error.code is GrpcCode.INVALID_ARGUMENT
    assert error.http_status == 400
    lenient = build_library_transcoder(TranscoderOptions(ignore_unknown_query_parameters=True))
    assert lenient.transcode("GET", "/v1/books?bogus=1&pageSize=2").message.page_size == 2


@pytest.mark.unit
def test_scalar_conversion_is_strict_and_keeps_64_bit_precision() -> None:
    transcoder = build_library_transcoder()
    big = transcoder.transcode("PATCH", "/v1/books/42?book.pageCount=9007199254740993", b"{}")
    assert big.message.book.page_count == 9007199254740993
    flag = transcoder.transcode("PATCH", "/v1/books/42?allowMissing=true", b"{}")
    assert flag.message.allow_missing is True
    by_number = transcoder.transcode("GET", "/v1/books?filterBook.genre=2")
    assert by_number.message.filter_book.genre == 2
    for target in (
        "/v1/books?pageSize=abc",
        "/v1/books?pageSize=2147483648",
        "/v1/books?filterBook.genre=POETRY",
        "/v1/books?filterBook=x",
        "/v1/books?filterBook.tags.x=1",
    ):
        assert _fail(transcoder, "GET", target).code is GrpcCode.INVALID_ARGUMENT
    assert _fail(transcoder, "PATCH", "/v1/books/42?allowMissing=yes", b"{}").http_status == 400


@pytest.mark.unit
def test_an_unknown_enum_name_in_the_body_is_silently_dropped_by_the_python_runtime() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("PATCH", "/v1/books/42", b'{"genre": "POETRY", "title": "x"}')
    assert result.message.book.genre == 0
    assert result.message.book.title == "x"
    strict = transcoder.pool.FindMessageTypeByName("library.v1.Book")
    assert strict.fields_by_name["genre"].enum_type.values_by_name.get("POETRY") is None


@pytest.mark.unit
def test_query_pairs_without_equals_or_name_are_skipped_silently() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("GET", "/v1/books?flag&=x&pageSize=3&")
    assert result.message.page_size == 3
    assert [b.field_path for b in result.bindings] == [("pageSize",)]


@pytest.mark.unit
def test_plus_in_a_query_value_is_a_space_only_when_enabled() -> None:
    plain = build_library_transcoder()
    assert plain.transcode("GET", "/v1/books?pageToken=a+b").message.page_token == "a+b"  # noqa: S105
    plus = build_library_transcoder(TranscoderOptions(query_param_unescape_plus=True))
    assert plus.transcode("GET", "/v1/books?pageToken=a+b%2Bc").message.page_token == "a b+c"  # noqa: S105


@pytest.mark.unit
def test_body_star_maps_the_whole_body_and_still_accepts_query_bindings() -> None:
    transcoder = build_library_transcoder()
    result = transcoder.transcode("POST", "/v1/books/42:archive", b'{"reason": "obsolete"}')
    assert result.message.name == "books/42"
    assert result.message.reason == "obsolete"
    both = transcoder.transcode("POST", "/v1/books/42:archive?reason=q", b"{}")
    assert both.message.reason == "q"
    wins = transcoder.transcode("POST", "/v1/books/42:archive?reason=q", b'{"reason": "body"}')
    assert wins.message.reason == "body"


@pytest.mark.unit
def test_body_problems_are_invalid_argument() -> None:
    transcoder = build_library_transcoder()
    assert _fail(transcoder, "GET", "/v1/books/42", b'{"a": 1}').code is GrpcCode.INVALID_ARGUMENT
    assert (
        _fail(transcoder, "PATCH", "/v1/books/42", b"{not json").code is GrpcCode.INVALID_ARGUMENT
    )
    assert _fail(transcoder, "PATCH", "/v1/books/42", b"[1]").code is GrpcCode.INVALID_ARGUMENT
    assert _fail(transcoder, "PATCH", "/v1/books/42", b'{"pageCount": "x"}').http_status == 400


@pytest.mark.unit
def test_percent_encoded_slash_stays_encoded_in_multi_segment_variables_by_default() -> None:
    target = "/v1/books/a%2Fb"
    default = build_library_transcoder().transcode("GET", target)
    assert default.message.name == "books/a%2Fb"
    except_slash = build_library_transcoder(
        TranscoderOptions(url_unescape_spec=UnescapeSpec.ALL_CHARACTERS_EXCEPT_SLASH)
    ).transcode("GET", target)
    assert except_slash.message.name == "books/a%2Fb"
    everything = build_library_transcoder(
        TranscoderOptions(url_unescape_spec=UnescapeSpec.ALL_CHARACTERS)
    ).transcode("GET", target)
    assert everything.message.name == "books/a/b"


@pytest.mark.unit
def test_single_segment_variables_are_always_fully_decoded() -> None:
    transcoder = _transcoder(
        _method("GetBook", "GetBookRequest", HttpRule("GET", "/v1/books/{name}"))
    )
    assert transcoder.transcode("GET", "/v1/books/a%2Fb%20c").message.name == "a/b c"
    assert transcoder.lookup("GET", "/v1/books/a%2Fb") is not None


# ---------------------------------------------------------------------------
# AIP-127 linting
# ---------------------------------------------------------------------------


def _request(name: str) -> Any:
    return build_pool().FindMessageTypeByName(f"library.v1.{name}")


@pytest.mark.unit
def test_the_library_contract_passes_the_aip_127_lint() -> None:
    for method in LIBRARY_METHODS:
        assert method.rule is not None
        assert lint_rule(method.rule, _request(method.request_type.rsplit(".", 1)[1])) == []


@pytest.mark.unit
def test_lint_flags_each_aip_127_violation() -> None:
    update = _request("UpdateBookRequest")
    create = _request("CreateBookRequest")
    cases: list[tuple[HttpRule, Any, str]] = [
        (HttpRule("GET", "/v1/{name=books/*}", body="book"), update, "GET must not define a body"),
        (HttpRule("DELETE", "/v1/{name=books/*}", body="book"), update, "DELETE must not define"),
        (HttpRule("PATCH", "/v1/{book.name=books/*}", body="book.title"), update, "nested field"),
        (HttpRule("PATCH", "/v1/{book=books/*}", body="book"), update, "same as a URI parameter"),
        (HttpRule("PUT", "/v1/{book.name=books/*}", body="book"), update, "should not use put"),
        (HttpRule("*", "/v1/books", body="book"), create, "should not use *"),
        (HttpRule("POST", "/v1/books", body="missing"), create, "not in the request"),
        (HttpRule("POST", "/v1/books", body="books"), _request("ListBooksResponse"), "repeated"),
        (
            HttpRule(
                "POST",
                "/v1/books",
                body="book",
                additional_bindings=(HttpRule("POST", "/v2/books", body="*"),),
            ),
            create,
            "identical",
        ),
        (
            HttpRule(
                "POST",
                "/v1/books",
                body="book",
                additional_bindings=(
                    HttpRule(
                        "POST",
                        "/v2/books",
                        body="book",
                        additional_bindings=(HttpRule("POST", "/v3/books", body="book"),),
                    ),
                ),
            ),
            create,
            "must not nest",
        ),
    ]
    for rule, request, fragment in cases:
        problems = lint_rule(rule, request)
        assert any(fragment in problem for problem in problems), (rule, problems)


@pytest.mark.unit
def test_lint_rejects_any_annotation_on_a_bidirectional_streaming_rpc() -> None:
    rule = HttpRule("POST", "/v1/books:chat", body="*")
    problems = lint_rule(rule, _request("GetBookRequest"), bidi_streaming=True)
    assert problems == [
        "bidirectional streaming RPCs should not include a google.api.http annotation"
    ]


# ---------------------------------------------------------------------------
# Response side
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grpc_code_to_http_status_table_matches_google_rpc_code_proto() -> None:
    expected = {
        0: 200,
        1: 499,
        2: 500,
        3: 400,
        4: 504,
        5: 404,
        6: 409,
        7: 403,
        8: 429,
        9: 400,
        10: 409,
        11: 400,
        12: 501,
        13: 500,
        14: 503,
        15: 500,
        16: 401,
    }
    assert {int(code): status for code, status in HTTP_STATUS_BY_CODE.items()} == expected
    assert http_status_for(99) == 500
    assert http_status_for(-1) == 500


@pytest.mark.unit
def test_the_client_side_http_to_grpc_table_is_not_the_inverse() -> None:
    assert grpc_code_for_http(404) is GrpcCode.UNIMPLEMENTED
    assert http_status_for(GrpcCode.NOT_FOUND) == 404
    assert grpc_code_for_http(http_status_for(GrpcCode.NOT_FOUND)) is not GrpcCode.NOT_FOUND
    assert grpc_code_for_http(200) is GrpcCode.UNKNOWN
    assert grpc_code_for_http(409) is GrpcCode.UNKNOWN
    assert grpc_code_for_http(429) is GrpcCode.UNAVAILABLE


@pytest.mark.unit
def test_status_details_trailer_is_decoded_into_the_envoy_documented_json() -> None:
    pool = build_pool()
    converted = convert_grpc_status(pool, 5, None, ENVOY_DETAILS_BIN)
    assert converted is not None
    status, body = converted
    assert status == 404
    assert json.loads(body) == {
        "code": 5,
        "details": [{"@type": "type.googleapis.com/google.rpc.RequestInfo", "requestId": "r-1"}],
    }
    assert body == (
        '{"code":5,"details":[{"@type":"type.googleapis.com/google.rpc.RequestInfo",'
        '"requestId":"r-1"}]}'
    )


@pytest.mark.unit
def test_status_is_built_from_grpc_status_and_percent_decoded_grpc_message() -> None:
    pool = build_pool()
    converted = convert_grpc_status(pool, 3, "bad%20name")
    assert converted == (400, '{"code":3,"message":"bad name"}')


@pytest.mark.unit
def test_nothing_is_converted_when_a_body_exists_or_the_status_is_ok_or_unknown() -> None:
    pool = build_pool()
    assert convert_grpc_status(pool, 5, "x", has_body=True) is None
    assert convert_grpc_status(pool, 0) is None
    assert convert_grpc_status(pool, 99) is None


@pytest.mark.unit
def test_response_body_selects_a_field_and_int64_is_rendered_as_a_string() -> None:
    transcoder = build_library_transcoder()
    response = new_message(transcoder.pool, "library.v1.ListBooksResponse")
    book = response.books.add()
    book.name = "books/1"
    book.page_count = 7
    list_rule = LIBRARY_METHODS[1].rule
    assert list_rule is not None
    assert render_response(list_rule, response) == '[{"name":"books/1","pageCount":"7"}]'
    assert render_response(HttpRule("GET", "/v1/books"), response) == (
        '{"books":[{"name":"books/1","pageCount":"7"}]}'
    )
    empty = new_message(transcoder.pool, "library.v1.ListBooksResponse")
    assert render_response(list_rule, empty) == "[]"


@pytest.mark.unit
def test_grpc_message_frame_is_a_zero_flag_and_a_big_endian_length() -> None:
    payload = b"x" * 300
    frame = frame_grpc_message(payload)
    assert frame[:5] == bytes([0, 0, 0, 1, 44])
    assert frame[5:] == payload
    assert frame_grpc_message(b"") == b"\x00\x00\x00\x00\x00"


# ---------------------------------------------------------------------------
# AIP-134 update semantics
# ---------------------------------------------------------------------------


def _stored(transcoder: Transcoder) -> Any:
    book = transcoder.transcode("GET", "/v1/books").message.filter_book
    book.name = "books/42"
    book.title = "Old"
    book.author = "Herbert"
    book.tags.extend(["a", "b"])
    return book


@pytest.mark.unit
def test_update_with_a_mask_changes_only_the_masked_fields() -> None:
    transcoder = build_library_transcoder()
    stored = _stored(transcoder)
    request = transcoder.transcode(
        "PATCH", "/v1/books/42?updateMask=title", b'{"title": "Dune", "author": "ignored"}'
    ).message
    updated = apply_update(stored, request)
    assert (updated.title, updated.author, updated.name) == ("Dune", "Herbert", "books/42")
    assert list(updated.tags) == ["a", "b"]


@pytest.mark.unit
def test_an_omitted_mask_updates_populated_fields_and_an_empty_mask_is_a_no_op() -> None:
    transcoder = build_library_transcoder()
    stored = _stored(transcoder)
    omitted = transcoder.transcode("PATCH", "/v1/books/42", b'{"title": "Dune"}').message
    assert apply_update(stored, omitted).title == "Dune"
    assert apply_update(stored, omitted).author == "Herbert"
    empty = transcoder.transcode("PATCH", "/v1/books/42?updateMask=", b'{"title": "Dune"}').message
    assert apply_update(stored, empty).title == "Old"


@pytest.mark.unit
def test_star_mask_replaces_the_resource_but_the_runtime_does_not_know_it() -> None:
    transcoder = build_library_transcoder()
    stored = _stored(transcoder)
    request = transcoder.transcode(
        "PATCH", "/v1/books/42?updateMask=*", b'{"title": "Dune"}'
    ).message
    replaced = apply_update(stored, request)
    assert (replaced.title, replaced.author, replaced.name) == ("Dune", "", "books/42")
    assert list(replaced.tags) == []
    assert not request.update_mask.IsValidForDescriptor(stored.DESCRIPTOR)


@pytest.mark.unit
def test_update_replaces_repeated_fields_and_rejects_unknown_mask_paths() -> None:
    transcoder = build_library_transcoder()
    stored = _stored(transcoder)
    request = transcoder.transcode(
        "PATCH", "/v1/books/42?updateMask=tags", b'{"tags": ["z"]}'
    ).message
    assert list(apply_update(stored, request).tags) == ["z"]
    bad = transcoder.transcode("PATCH", "/v1/books/42?updateMask=ghost", b"{}").message
    with pytest.raises(TranscodingError) as caught:
        apply_update(stored, bad)
    assert caught.value.code is GrpcCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# End to end through an ASGI gateway
# ---------------------------------------------------------------------------


def _gateway() -> httpx.AsyncClient:
    transcoder = build_library_transcoder()

    def update_book(request: Any) -> Any:
        if request.book.name == "books/404":
            raise TranscodingError(GrpcCode.NOT_FOUND, "no such book")
        return apply_update(_stored(transcoder), request)

    def get_book(request: Any) -> Any:
        stored = _stored(transcoder)
        stored.name = request.name
        return stored

    app = build_gateway_app(transcoder, {"UpdateBook": update_book, "GetBook": get_book})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_serves_the_patch_example_and_maps_errors_to_http_statuses() -> None:
    async with _gateway() as client:
        ok = await client.patch("/v1/books/42?updateMask=title", json={"title": "Dune"})
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "application/json"
        assert ok.json() == {
            "name": "books/42",
            "title": "Dune",
            "author": "Herbert",
            "tags": ["a", "b"],
        }

        missing = await client.patch("/v1/books/404?updateMask=title", json={"title": "x"})
        assert missing.status_code == 404
        assert missing.json() == {"code": 5, "message": "no such book"}

        bad_mask = await client.patch("/v1/books/42?updateMask=ghost", json={})
        assert bad_mask.status_code == 400

        unrouted = await client.get("/v2/books/1")
        assert unrouted.status_code == 404

        unimplemented = await client.delete("/v1/books/42")
        assert unimplemented.status_code == 501


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gateway_keeps_percent_encoded_slash_out_of_routing() -> None:
    async with _gateway() as client:
        reply = await client.get("/v1/books/a%2Fb")
        assert reply.status_code == 200
        assert reply.json()["name"] == "books/a%2Fb"
