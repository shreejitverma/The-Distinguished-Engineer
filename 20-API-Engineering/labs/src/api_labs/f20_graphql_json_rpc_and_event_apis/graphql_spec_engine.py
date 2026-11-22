"""GraphQL September 2025 internals lab: SDL, AST, validation, and execution order.

Everything here is in-process and deterministic.
The schema is built from SDL with graphql-core, the executor under test is graphql-core's
own (`graphql.execute`), and `spec_collect_fields` is a direct transcription of the
specification's `CollectFields()` algorithm over graphql-core's AST so tests can compare
the two.
"""

from __future__ import annotations

import asyncio
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, cast

from graphql import (
    BooleanValueNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    SelectionNode,
    SelectionSetNode,
    VariableNode,
    build_schema,
    execute,  # pyright: ignore[reportUnknownVariableType]
    graphql_sync,  # pyright: ignore[reportUnknownVariableType]
    parse,
    validate,
)
from graphql.type import GraphQLInterfaceType, GraphQLUnionType, is_abstract_type

if TYPE_CHECKING:
    from collections.abc import Mapping

SDL = """
type Query {
  hello(name: String = "world"): String!
  a: Int
  b: Int
  x: X
  z: Z
  books: [Book!]!
  maybeBooks: [Book]
  book(ref: BookRef!): String
}

type Mutation {
  setA: Int
  setB: Int
}

type X { y: Int }
type Z { w: Int }

type Book {
  title: String!
  author: String
}

input BookRef @oneOf {
  id: ID
  isbn: String
}
"""


def build_lab_schema() -> GraphQLSchema:
    """Build the lab schema from SDL (spec Section 3, type system definitions)."""
    return build_schema(SDL)


def document_outline(source: str) -> list[str]:
    """Return the AST node kind of each top-level definition, in source order."""
    return [type(definition).__name__ for definition in parse(source).definitions]


def ast_dict(source: str) -> dict[str, Any]:
    """Return the location-free dict form of the parsed document, for AST equality checks."""
    document = parse(source, no_location=True)
    return cast("dict[str, Any]", document.to_dict())  # pyright: ignore[reportUnknownMemberType]


def graphql_core_formatted(schema: GraphQLSchema, source: str) -> dict[str, Any]:
    """Return graphql-core's own `ExecutionResult.formatted` for a synchronous request."""
    return dict(graphql_sync(schema, source).formatted)


def execute_unvalidated(schema: GraphQLSchema, source: str, root_value: object) -> dict[str, Any]:
    """Execute without running validation, to show execution alone is still well defined."""
    outcome = execute(schema, parse(source), root_value=root_value)
    if isawaitable(outcome):
        raise TypeError("lab resolvers are synchronous here")
    return {"data": outcome.data, "errors": outcome.errors}


def validation_messages(schema: GraphQLSchema, source: str) -> list[str]:
    """Run every specified validation rule and return the messages (spec Section 5)."""
    return [error.message for error in validate(schema, parse(source))]


def _fragments(document: DocumentNode) -> dict[str, FragmentDefinitionNode]:
    return {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }


def _does_fragment_type_apply(
    schema: GraphQLSchema, object_type: GraphQLObjectType, type_name: str
) -> bool:
    fragment_type = schema.get_type(type_name)
    if isinstance(fragment_type, GraphQLObjectType):
        return fragment_type is object_type
    if isinstance(fragment_type, (GraphQLInterfaceType, GraphQLUnionType)) and is_abstract_type(
        fragment_type
    ):
        return schema.is_sub_type(fragment_type, object_type)
    return False


def _directive_if(node: SelectionNode, name: str, variables: Mapping[str, Any]) -> bool | None:
    """Return the evaluated `if` argument of directive `name`, or None if absent."""
    for directive in node.directives or ():
        if directive.name.value != name:
            continue
        for argument in directive.arguments or ():
            if argument.name.value != "if":
                continue
            value = argument.value
            if isinstance(value, BooleanValueNode):
                return value.value
            if isinstance(value, VariableNode):
                return variables.get(value.name.value, False) is True
    return None


def spec_collect_fields(
    schema: GraphQLSchema,
    document: DocumentNode,
    object_type: GraphQLObjectType,
    selection_set: SelectionSetNode,
    variables: Mapping[str, Any] | None = None,
    visited_fragments: set[str] | None = None,
) -> dict[str, list[FieldNode]]:
    """Transcribe `CollectFields(objectType, selectionSet, variableValues, visitedFragments)`.

    Returns an ordered map from response name to the field set that shares it
    (spec Section 6.3.2).
    """
    values = variables or {}
    visited: set[str] = set() if visited_fragments is None else visited_fragments
    fragments = _fragments(document)
    collected: dict[str, list[FieldNode]] = {}

    for selection in selection_set.selections:
        if _directive_if(selection, "skip", values) is True:
            continue
        if _directive_if(selection, "include", values) is False:
            continue
        if isinstance(selection, FieldNode):
            response_name = selection.alias.value if selection.alias else selection.name.value
            collected.setdefault(response_name, []).append(selection)
        elif isinstance(selection, FragmentSpreadNode):
            name = selection.name.value
            if name in visited:
                continue
            visited.add(name)
            fragment = fragments.get(name)
            if fragment is None:
                continue
            condition = fragment.type_condition.name.value
            if not _does_fragment_type_apply(schema, object_type, condition):
                continue
            nested = spec_collect_fields(
                schema, document, object_type, fragment.selection_set, values, visited
            )
            for response_name, nested_fields in nested.items():
                collected.setdefault(response_name, []).extend(nested_fields)
        elif isinstance(selection, InlineFragmentNode):
            if selection.type_condition is not None and not _does_fragment_type_apply(
                schema, object_type, selection.type_condition.name.value
            ):
                continue
            nested = spec_collect_fields(
                schema, document, object_type, selection.selection_set, values, visited
            )
            for response_name, nested_fields in nested.items():
                collected.setdefault(response_name, []).extend(nested_fields)
    return collected


class Resolvers:
    """Root value whose methods record when each resolver starts and ends.

    graphql-core's default resolver calls a callable attribute as `fn(info, **args)`.
    In `asynchronous` mode each resolver yields to the event loop once, which is what makes
    sibling fields observably overlap.
    """

    def __init__(self, *, asynchronous: bool) -> None:
        self.asynchronous = asynchronous
        self.trace: list[str] = []

    def _step(self, name: str, value: Any) -> Any:
        if not self.asynchronous:
            self.trace.append(name)
            return value

        async def run() -> Any:
            self.trace.append(f"{name}:start")
            await asyncio.sleep(0)
            self.trace.append(f"{name}:end")
            return value

        return run()

    def a(self, _info: Any) -> Any:
        return self._step("a", 1)

    def b(self, _info: Any) -> Any:
        return self._step("b", 2)

    def setA(self, _info: Any) -> Any:
        return self._step("setA", 1)

    def setB(self, _info: Any) -> Any:
        return self._step("setB", 2)

    def x(self, _info: Any) -> Any:
        return self._step("x", self)

    def y(self, _info: Any) -> Any:
        return self._step("y", 3)

    def z(self, _info: Any) -> Any:
        return self._step("z", self)

    def w(self, _info: Any) -> Any:
        return self._step("w", 4)

    def hello(self, _info: Any, name: str) -> str:
        return f"hello {name}"

    def books(self, _info: Any) -> list[Book]:
        return [Book("Dune"), Book(None), Book("Emma")]

    def maybeBooks(self, _info: Any) -> list[Book]:
        return [Book("Dune"), Book(None), Book("Emma")]

    def book(self, _info: Any, ref: Mapping[str, str]) -> str:
        return next(iter(ref.values()))


class Book:
    """A book whose `title` resolver raises when no title is set."""

    def __init__(self, title: str | None) -> None:
        self._title = title

    def title(self, _info: Any) -> str:
        if self._title is None:
            raise ValueError("title unavailable")
        return self._title

    def author(self, _info: Any) -> str:
        return "anon"


async def execute_request(
    schema: GraphQLSchema,
    source: str,
    *,
    root_value: object | None = None,
    variable_values: Mapping[str, Any] | None = None,
    operation_name: str | None = None,
) -> dict[str, Any]:
    """Run parse -> validate -> execute and return a spec-shaped response map.

    A request error result has `errors` and no `data` key (spec Section 7.1.3).
    An execution result always has `data` (spec Section 7.1.1).
    Variable coercion failures surface from `graphql.execute` as `data=None` with errors
    that carry no `path`, which is how this helper tells them from execution errors.
    """
    try:
        document = parse(source)
    except GraphQLError as error:
        return {"errors": [error.formatted]}
    validation_errors = validate(schema, document)
    if validation_errors:
        return {"errors": [error.formatted for error in validation_errors]}
    outcome = execute(
        schema,
        document,
        root_value=root_value,
        variable_values=dict(variable_values) if variable_values is not None else None,
        operation_name=operation_name,
    )
    result = await outcome if isawaitable(outcome) else outcome
    errors = result.errors or []
    if result.data is None and errors and all(error.path is None for error in errors):
        return {"errors": [error.formatted for error in errors]}
    response: dict[str, Any] = {"data": result.data}
    if errors:
        response["errors"] = [error.formatted for error in errors]
    return response
