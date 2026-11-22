"""Lab for the GraphQL September 2025 internals note: SDL, AST, validation, execution."""

from typing import Any

import httpx
import pytest
from graphql import (
    DocumentNode,
    FieldNode,
    GraphQLError,
    GraphQLObjectType,
    OperationDefinitionNode,
    parse,
    print_schema,
    resolve_schema_coordinate,
)

from api_labs.f20_graphql_json_rpc_and_event_apis.graphql_spec_engine import (
    Resolvers,
    ast_dict,
    build_lab_schema,
    document_outline,
    execute_request,
    execute_unvalidated,
    graphql_core_formatted,
    spec_collect_fields,
    validation_messages,
)
from api_labs.f20_graphql_json_rpc_and_event_apis.graphql_strawberry_app import build_app

SCHEMA = build_lab_schema()


def _query_type() -> GraphQLObjectType:
    query_type = SCHEMA.query_type
    assert query_type is not None
    return query_type


def _root_selection(document: DocumentNode) -> OperationDefinitionNode:
    operation = document.definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    return operation


@pytest.mark.unit
def test_sdl_builds_schema_and_round_trips_oneof_directive() -> None:
    printed = print_schema(SCHEMA)
    assert "input BookRef @oneOf" in printed
    assert SCHEMA.mutation_type is not None
    assert SCHEMA.subscription_type is None


@pytest.mark.unit
def test_schema_coordinates_resolve_to_schema_elements() -> None:
    resolved = resolve_schema_coordinate(SCHEMA, "Query.hello(name:)")
    assert resolved is not None
    assert resolved.kind == "FieldArgument"
    assert resolve_schema_coordinate(SCHEMA, "Query.nope") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_importing_strawberry_replaces_the_global_is_one_of_introspection_resolver() -> None:
    introspection_type = SCHEMA.type_map["__Type"]
    assert isinstance(introspection_type, GraphQLObjectType)
    resolver = introspection_type.fields["isOneOf"].resolve
    assert resolver is not None
    assert resolver.__qualname__.endswith("_extend_introspection.<locals>._resolve_is_one_of")
    response = await execute_request(SCHEMA, '{ __type(name: "BookRef") { isOneOf } }')
    assert response == {"data": {"__type": {"isOneOf": False}}}


@pytest.mark.unit
def test_document_outline_lists_executable_definitions_in_source_order() -> None:
    assert document_outline("{ a ...F } fragment F on Query { b }") == [
        "OperationDefinitionNode",
        "FragmentDefinitionNode",
    ]


@pytest.mark.unit
def test_commas_comments_and_whitespace_are_ignored_tokens() -> None:
    assert ast_dict("{a,b}") == ast_dict("{ a  # comment\n  b ,,, }")
    assert ast_dict("{a,b}") != ast_dict("{a c}")


@pytest.mark.unit
def test_parser_enforces_token_comment_and_length_limits() -> None:
    source = "{ a b c } # x\n# y"
    with pytest.raises(GraphQLError, match="more than 3 tokens"):
        parse(source, max_lexical_tokens=3)
    with pytest.raises(GraphQLError, match="more than 1 comments"):
        parse(source, max_comments=1)
    with pytest.raises(GraphQLError, match="more than 5 code points"):
        parse(source, max_source_length=5)
    assert parse(source, max_lexical_tokens=5, max_comments=2) is not None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("{ nope }", "Cannot query field 'nope' on type 'Query'"),
        ("{ a { y } }", "must not have a selection since type 'Int' has no subfields"),
        ("{ x }", "must have a selection of subfields"),
        ("{ ...Missing }", "Unknown fragment 'Missing'"),
        ("{ ...F } fragment F on Query { ...F }", "Cannot spread fragment 'F' within itself"),
        ("{ ...F } fragment F on Query { a } fragment G on Query { b }", "'G' is never used"),
        ("query($v: Int) { hello(name: $v) }", "used in position expecting type 'String'"),
        ("query($v: Int) { a }", "Variable '$v' is never used"),
        ("{ a: b a }", "Fields 'a' conflict"),
        ("{ hello(nme: 1) }", "Unknown argument 'nme'"),
        ("subscription { a }", "subscription operation is not supported"),
        ("query { a } extend type Query { c: Int }", "is not executable"),
        ("query A { a } query A { b }", "only one operation named 'A'"),
        ("{ a } query B { b }", "must be the only defined operation"),
    ],
)
def test_validation_rules_reject_invalid_documents(source: str, fragment: str) -> None:
    messages = validation_messages(SCHEMA, source)
    assert any(fragment in message for message in messages), messages


@pytest.mark.unit
def test_valid_document_has_no_validation_errors() -> None:
    assert validation_messages(SCHEMA, "{ hello a ...F } fragment F on Query { b }") == []


@pytest.mark.unit
def test_oneof_literal_with_two_fields_fails_validation() -> None:
    messages = validation_messages(SCHEMA, '{ book(ref: {id: "1", isbn: "2"}) }')
    assert any("exactly one field must be specified" in message for message in messages)


@pytest.mark.unit
def test_invalid_document_is_still_executable_without_validation() -> None:
    assert validation_messages(SCHEMA, "{ nope a }")
    result = execute_unvalidated(SCHEMA, "{ nope a }", Resolvers(asynchronous=False))
    assert result == {"data": {"a": 1}, "errors": None}


@pytest.mark.unit
def test_collect_fields_matches_spec_example_201() -> None:
    document = parse("{ a { y } ...F } fragment F on Query { a { w } b }")
    selection_set = _root_selection(document).selection_set
    collected = spec_collect_fields(SCHEMA, document, _query_type(), selection_set)
    assert list(collected) == ["a", "b"]
    assert len(collected["a"]) == 2
    assert len(collected["b"]) == 1
    assert all(isinstance(node, FieldNode) for nodes in collected.values() for node in nodes)


@pytest.mark.unit
def test_collect_fields_visits_a_named_fragment_once() -> None:
    document = parse("{ ...F ...F } fragment F on Query { a }")
    selection_set = _root_selection(document).selection_set
    collected = spec_collect_fields(SCHEMA, document, _query_type(), selection_set)
    assert len(collected["a"]) == 1


@pytest.mark.unit
def test_collect_fields_applies_skip_include_and_type_conditions() -> None:
    document = parse(
        "query($s: Boolean!, $i: Boolean!) { a @skip(if: $s) b @include(if: $i) "
        "... on Query { hello } ... on Book { title } }"
    )
    selection_set = _root_selection(document).selection_set
    both_off = spec_collect_fields(
        SCHEMA, document, _query_type(), selection_set, {"s": True, "i": False}
    )
    assert list(both_off) == ["hello"]
    both_on = spec_collect_fields(
        SCHEMA, document, _query_type(), selection_set, {"s": False, "i": True}
    )
    assert list(both_on) == ["a", "b", "hello"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_response_key_order_follows_collect_fields_order() -> None:
    source = "{ b a ...F } fragment F on Query { hello b }"
    document = parse(source)
    selection_set = _root_selection(document).selection_set
    expected = list(spec_collect_fields(SCHEMA, document, _query_type(), selection_set))
    response = await execute_request(SCHEMA, source, root_value=Resolvers(asynchronous=False))
    assert expected == ["b", "a", "hello"]
    assert list(response["data"]) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_query_root_fields_execute_concurrently() -> None:
    root = Resolvers(asynchronous=True)
    await execute_request(SCHEMA, "{ a b }", root_value=root)
    assert root.trace == ["a:start", "b:start", "a:end", "b:end"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mutation_root_fields_execute_serially() -> None:
    root = Resolvers(asynchronous=True)
    response = await execute_request(SCHEMA, "mutation { setA setB }", root_value=root)
    assert root.trace == ["setA:start", "setA:end", "setB:start", "setB:end"]
    assert response == {"data": {"setA": 1, "setB": 2}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_children_start_when_their_parent_completes_not_level_by_level() -> None:
    root = Resolvers(asynchronous=True)
    await execute_request(SCHEMA, "{ x { y } z { w } }", root_value=root)
    trace = root.trace
    assert trace.index("x:start") < trace.index("y:start")
    assert trace.index("z:start") < trace.index("y:start")
    assert trace.index("y:start") < trace.index("z:end")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_resolvers_complete_depth_first() -> None:
    root = Resolvers(asynchronous=False)
    await execute_request(SCHEMA, "{ x { y } z { w } }", root_value=root)
    assert root.trace == ["x", "y", "z", "w"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nullable_list_item_absorbs_a_failing_field_error() -> None:
    response = await execute_request(
        SCHEMA, "{ maybeBooks { title } }", root_value=Resolvers(asynchronous=False)
    )
    assert response["data"] == {"maybeBooks": [{"title": "Dune"}, None, {"title": "Emma"}]}
    assert [error["path"] for error in response["errors"]] == [["maybeBooks", 1, "title"]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_null_chain_bubbles_error_to_data_null() -> None:
    response = await execute_request(
        SCHEMA, "{ books { title } }", root_value=Resolvers(asynchronous=False)
    )
    assert response["data"] is None
    assert [error["path"] for error in response["errors"]] == [["books", 1, "title"]]
    assert len(response["errors"]) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_error_result_has_no_data_key_but_execution_result_does() -> None:
    root = Resolvers(asynchronous=False)
    syntax = await execute_request(SCHEMA, "{ a", root_value=root)
    invalid = await execute_request(SCHEMA, "{ nope }", root_value=root)
    coercion = await execute_request(
        SCHEMA, "query($n: Int!) { a }", root_value=root, variable_values={}
    )
    ambiguous = await execute_request(SCHEMA, "query A { a } query B { b }", root_value=root)
    for response in (syntax, invalid, coercion, ambiguous):
        assert "data" not in response
        assert response["errors"]
    ok = await execute_request(SCHEMA, "{ a }", root_value=root)
    assert ok == {"data": {"a": 1}}


@pytest.mark.unit
def test_graphql_core_formatted_result_keeps_data_null_on_request_error() -> None:
    formatted = graphql_core_formatted(SCHEMA, "{ a")
    assert "data" in formatted
    assert formatted["data"] is None
    assert formatted["errors"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operation_name_selects_among_multiple_operations() -> None:
    root = Resolvers(asynchronous=False)
    source = "query A { a } query B { b }"
    assert await execute_request(SCHEMA, source, root_value=root, operation_name="B") == {
        "data": {"b": 2}
    }
    missing = await execute_request(SCHEMA, source, root_value=root)
    assert "operation name" in missing["errors"][0]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_variable_uses_argument_default_but_explicit_null_does_not() -> None:
    root = Resolvers(asynchronous=False)
    source = "query($n: String) { hello(name: $n) }"
    omitted = await execute_request(SCHEMA, source, root_value=root, variable_values={})
    explicit = await execute_request(SCHEMA, source, root_value=root, variable_values={"n": None})
    assert omitted == {"data": {"hello": "hello world"}}
    assert explicit == {"data": {"hello": "hello None"}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oneof_variable_with_two_keys_is_a_request_error() -> None:
    response = await execute_request(
        SCHEMA,
        "query($r: BookRef!) { book(ref: $r) }",
        root_value=Resolvers(asynchronous=False),
        variable_values={"r": {"id": "1", "isbn": "2"}},
    )
    assert "data" not in response
    assert "exactly one field must be specified" in response["errors"][0]["message"]
    good = await execute_request(
        SCHEMA,
        "query($r: BookRef!) { book(ref: $r) }",
        root_value=Resolvers(asynchronous=False),
        variable_values={"r": {"id": "1"}},
    )
    assert good == {"data": {"book": "1"}}


async def _post(body: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/graphql", json=body, **kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strawberry_validation_error_is_http_200_with_data_null() -> None:
    response = await _post({"query": "{ nope }"})
    body = response.json()
    assert response.status_code == 200
    assert "data" in body
    assert body["data"] is None
    assert "Cannot query field 'nope'" in body["errors"][0]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strawberry_partial_success_and_non_null_bubbling_over_http() -> None:
    partial = (await _post({"query": "{ maybe hello }"})).json()
    assert partial["data"] == {"maybe": None, "hello": "hi"}
    assert partial["errors"][0]["path"] == ["maybe"]
    bubbled = await _post({"query": "{ must hello }"})
    assert bubbled.status_code == 200
    assert bubbled.json()["data"] is None
    assert bubbled.json()["errors"][0]["path"] == ["must"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_strawberry_rejects_malformed_http_bodies_with_400() -> None:
    assert (await _post({})).status_code == 400
    assert (await _post({"query": "{ hello }", "variables": "x"})).status_code == 400
    raw = await _post(None, content="NONSENSE", headers={"content-type": "application/json"})
    assert raw.status_code == 400
