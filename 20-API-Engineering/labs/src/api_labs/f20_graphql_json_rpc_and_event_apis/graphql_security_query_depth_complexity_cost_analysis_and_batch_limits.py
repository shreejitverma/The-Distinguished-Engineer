"""GraphQL demand control: depth, aliases, weighted cost, batch caps, introspection.

Every limit runs before the first resolver.
Schema-free, linear-time checks (size, tokens, depth, aliases, repeated sensitive fields) run
before validation so they also bound the cost of validation itself.
The weighted cost needs types, so it runs after validation.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLField,
    GraphQLInterfaceType,
    GraphQLNamedType,
    GraphQLObjectType,
    GraphQLResolveInfo,
    GraphQLSchema,
    GraphQLSyntaxError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
    VariableNode,
    build_schema,
    execute_sync,  # pyright: ignore[reportUnknownVariableType]
    get_named_type,
    is_composite_type,
    parse,
    validate,
)
from graphql.validation import NoSchemaIntrospectionCustomRule, specified_rules
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from starlette.requests import Request

SDL = """
type Query {
  me: User
  user(id: ID!): User
  users(first: Int = 10): [User!]!
}
type Mutation {
  login(username: String!, password: String!): LoginResult!
}
type LoginResult { ok: Boolean! }
type User {
  id: ID!
  name: String!
  friends(first: Int = 10): [User!]!
  posts(first: Int = 10): [Post!]!
}
type Post { id: ID! title: String! author: User! comments(first: Int = 10): [Comment!]! }
type Comment { id: ID! body: String! author: User! }
"""

LIST_FIELDS = frozenset({"users", "friends", "posts", "comments"})
OBJECT_FIELDS = frozenset({"me", "user", "author"})
MEDIA_TYPE = "application/graphql-response+json"
CORRECT_SECRET = "correct-horse"  # noqa: S105 - lab fixture, not a credential


def build_lab_schema() -> GraphQLSchema:
    return build_schema(SDL)


@dataclass
class Backend:
    """Stands in for the database: every resolver call and login attempt is recorded."""

    resolver_calls: int = 0
    login_attempts: list[str] = field(default_factory=lambda: list[str]())

    def resolve(self, source: object, info: GraphQLResolveInfo[Any], **args: Any) -> Any:
        self.resolver_calls += 1
        name = info.field_name
        if name == "login":
            self.login_attempts.append(str(args["username"]))
            return {"ok": args["password"] == CORRECT_SECRET}
        if name in LIST_FIELDS:
            return [{} for _ in range(min(int(args.get("first", 10)), 50))]
        if name in OBJECT_FIELDS:
            return {}
        if isinstance(source, dict):
            return cast("dict[str, Any]", source).get(name, f"{name}-value")
        return f"{name}-value"


@dataclass(frozen=True)
class Limits:
    max_body_bytes: int = 16_384
    max_source_length: int = 8_192
    max_lexical_tokens: int = 4_000
    max_comments: int = 50
    max_depth: int = 8
    max_aliases: int = 15
    max_root_fields: int = 10
    sensitive_field_limits: Mapping[str, int] = field(default_factory=lambda: {"login": 1})
    max_cost: int = 1_000
    max_validation_errors: int = 10
    allow_introspection: bool = False
    hide_suggestions: bool = True
    batching: bool = False
    max_batch_size: int = 5


@dataclass(frozen=True)
class CostModel:
    weights: Mapping[str, int] = field(default_factory=lambda: {"Mutation.login": 50})
    object_weight: int = 1
    assumed_list_size: int = 100
    list_size_args: tuple[str, ...] = ("first", "last", "limit")


class Rejected(Exception):
    def __init__(
        self, status: int, code: str, message: str, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Profile:
    depth: int
    aliases: int
    top_fields: int
    names: Mapping[str, int]


@dataclass
class Scan:
    """What one definition contains by itself, before its fragment spreads are expanded."""

    depth: int = 0
    aliases: int = 0
    top_fields: int = 0
    names: Counter[str] = field(default_factory=lambda: Counter[str]())
    spreads: list[tuple[str, int]] = field(default_factory=lambda: list[tuple[str, int]]())


def scan_selection_set(selection_set: SelectionSetNode) -> Scan:
    scan = Scan()
    stack: list[tuple[SelectionSetNode, int]] = [(selection_set, 0)]
    while stack:
        current, level = stack.pop()
        for selection in current.selections:
            if isinstance(selection, FieldNode):
                scan.depth = max(scan.depth, level + 1)
                scan.names[selection.name.value] += 1
                if selection.alias is not None:
                    scan.aliases += 1
                if level == 0:
                    scan.top_fields += 1
                if selection.selection_set is not None:
                    stack.append((selection.selection_set, level + 1))
            elif isinstance(selection, InlineFragmentNode):
                stack.append((selection.selection_set, level))
            elif isinstance(selection, FragmentSpreadNode):
                scan.spreads.append((selection.name.value, level))
    return scan


def _expand(scan: Scan, resolved: Mapping[str, Profile]) -> Profile:
    depth, aliases, top = scan.depth, scan.aliases, scan.top_fields
    names = Counter(scan.names)
    for spread, level in scan.spreads:
        target = resolved.get(spread)
        if target is None:
            continue
        depth = max(depth, level + target.depth)
        aliases += target.aliases
        if level == 0:
            top += target.top_fields
        names.update(target.names)
    return Profile(depth, aliases, top, names)


def analyze(document: DocumentNode, operation: OperationDefinitionNode) -> Profile:
    """Profile one operation with fragments expanded, in time linear in the document.

    Each fragment is scanned once and merged by memoized post-order, so a fragment DAG that
    expands to 2**n paths costs n merges.
    A spread that closes a cycle or names a missing fragment contributes nothing here;
    the standard validation rules report both.
    """
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    root = scan_selection_set(operation.selection_set)
    scans: dict[str, Scan] = {}
    state: dict[str, int] = {}
    order: list[str] = []
    stack: list[tuple[str, Iterator[str]]] = []

    def push(name: str) -> None:
        state[name] = 1
        scans[name] = scan_selection_set(fragments[name].selection_set)
        stack.append((name, iter([s for s, _ in scans[name].spreads if s in fragments])))

    for start, _ in root.spreads:
        if start not in fragments or start in state:
            continue
        push(start)
        while stack:
            name, spreads = stack[-1]
            for nxt in spreads:
                if nxt not in state:
                    push(nxt)
                    break
            else:
                stack.pop()
                state[name] = 2
                order.append(name)
    resolved: dict[str, Profile] = {}
    for name in order:
        resolved[name] = _expand(scans[name], resolved)
    return _expand(root, resolved)


def select_operation(document: DocumentNode, operation_name: str | None) -> OperationDefinitionNode:
    """GetOperation from GraphQL September 2025 Section 6.1; only this operation executes."""
    operations = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if operation_name is None:
        if len(operations) == 1:
            return operations[0]
        raise Rejected(422, "OPERATION_NAME_REQUIRED", "operationName is required")
    for operation in operations:
        if operation.name is not None and operation.name.value == operation_name:
            return operation
    raise Rejected(422, "UNKNOWN_OPERATION", f"unknown operation {operation_name!r}")


def enforce_structure(profile: Profile, limits: Limits) -> None:
    if profile.depth > limits.max_depth:
        raise Rejected(422, "DEPTH_LIMIT", f"depth {profile.depth} exceeds {limits.max_depth}")
    if profile.aliases > limits.max_aliases:
        raise Rejected(422, "ALIAS_LIMIT", f"{profile.aliases} aliases exceed {limits.max_aliases}")
    if profile.top_fields > limits.max_root_fields:
        raise Rejected(
            422,
            "ROOT_FIELD_LIMIT",
            f"{profile.top_fields} root fields exceed {limits.max_root_fields}",
        )
    enforce_sensitive(profile.names, limits)


def enforce_sensitive(names: Mapping[str, int], limits: Limits) -> None:
    for name, cap in limits.sensitive_field_limits.items():
        if names.get(name, 0) > cap:
            raise Rejected(
                422, "FIELD_REPEAT_LIMIT", f"field {name!r} appears {names[name]} times, cap {cap}"
            )


def _is_list(definition: GraphQLField) -> bool:
    rendered: str = str(definition.type)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return rendered.startswith("[")  # `[User!]!` and `[User]` are lists


class _Estimator:
    def __init__(
        self,
        schema: GraphQLSchema,
        model: CostModel,
        document: DocumentNode,
        variables: Mapping[str, Any],
    ) -> None:
        self.schema = schema
        self.model = model
        self.variables = variables
        self.fragments = {
            definition.name.value: definition
            for definition in document.definitions
            if isinstance(definition, FragmentDefinitionNode)
        }
        self.memo: dict[str, int] = {}

    def selection_set(self, node: SelectionSetNode, parent: GraphQLNamedType | None) -> int:
        total = 0
        for selection in node.selections:
            if isinstance(selection, FieldNode):
                total += self.field(selection, parent)
            elif isinstance(selection, InlineFragmentNode):
                condition = selection.type_condition
                target = self.schema.get_type(condition.name.value) if condition else parent
                total += self.selection_set(selection.selection_set, target)
            elif isinstance(selection, FragmentSpreadNode):
                total += self.fragment(selection.name.value)
        return total

    def fragment(self, name: str) -> int:
        if name not in self.memo:
            definition = self.fragments[name]
            target = self.schema.get_type(definition.type_condition.name.value)
            self.memo[name] = self.selection_set(definition.selection_set, target)
        return self.memo[name]

    def field(self, node: FieldNode, parent: GraphQLNamedType | None) -> int:
        name = node.name.value
        if name == "__typename":
            return 0
        definition = (
            parent.fields.get(name)
            if isinstance(parent, GraphQLObjectType | GraphQLInterfaceType)
            else None
        )
        if parent is None or definition is None:
            return self.model.object_weight
        named = cast("GraphQLNamedType", get_named_type(definition.type))
        default = self.model.object_weight if is_composite_type(named) else 0
        weight = self.model.weights.get(f"{parent.name}.{name}", default)
        size = self.list_size(node, definition) if _is_list(definition) else 1
        children = self.selection_set(node.selection_set, named) if node.selection_set else 0
        return size * (weight + children)

    def list_size(self, node: FieldNode, definition: GraphQLField) -> int:
        for argument in node.arguments or ():
            if argument.name.value not in self.model.list_size_args:
                continue
            value = argument.value
            if isinstance(value, IntValueNode):
                return max(int(value.value), 0)
            if isinstance(value, VariableNode):
                supplied = self.variables.get(value.name.value)
                if isinstance(supplied, int) and not isinstance(supplied, bool):
                    return max(supplied, 0)
            return self.model.assumed_list_size
        for key in self.model.list_size_args:
            declared = definition.args.get(key)
            if declared is None:
                continue
            if isinstance(declared.default_value, int):
                return max(declared.default_value, 0)
            # build_schema keeps an SDL default on the AST node and leaves default_value Undefined.
            literal = declared.ast_node.default_value if declared.ast_node else None
            if isinstance(literal, IntValueNode):
                return max(int(literal.value), 0)
        return self.model.assumed_list_size


def estimate_cost(
    schema: GraphQLSchema,
    model: CostModel,
    document: DocumentNode,
    operation: OperationDefinitionNode,
    variables: Mapping[str, Any],
) -> int:
    """Weighted cost of a validated, acyclic operation: size * (weight + children)."""
    root = schema.mutation_type if operation.operation.value == "mutation" else schema.query_type
    return _Estimator(schema, model, document, variables).selection_set(
        operation.selection_set, root
    )


@dataclass(frozen=True)
class Prepared:
    document: DocumentNode
    operation: OperationDefinitionNode
    profile: Profile
    cost: int
    operation_name: str | None
    variables: Mapping[str, Any]


def prepare(
    schema: GraphQLSchema,
    limits: Limits,
    model: CostModel,
    query: str,
    operation_name: str | None = None,
    variables: Mapping[str, Any] | None = None,
) -> Prepared:
    """Parse, bound, validate, and price one operation; raise Rejected or return it."""
    values = variables or {}
    try:
        document = parse(
            query,
            max_lexical_tokens=limits.max_lexical_tokens,
            max_comments=limits.max_comments,
            max_source_length=limits.max_source_length,
        )
    except GraphQLSyntaxError as error:
        raise Rejected(400, "PARSE_FAILED", error.message) from None
    operation = select_operation(document, operation_name)
    profile = analyze(document, operation)
    enforce_structure(profile, limits)
    rules = [*specified_rules]
    if not limits.allow_introspection:
        rules.append(NoSchemaIntrospectionCustomRule)
    errors = validate(
        schema,
        document,
        rules,
        max_errors=limits.max_validation_errors,
        hide_suggestions=limits.hide_suggestions,
    )
    if errors:
        raise Rejected(422, "VALIDATION_FAILED", "; ".join(error.message for error in errors))
    cost = estimate_cost(schema, model, document, operation, values)
    if cost > limits.max_cost:
        raise Rejected(422, "COST_LIMIT", f"cost {cost} exceeds {limits.max_cost}")
    return Prepared(document, operation, profile, cost, operation_name, values)


class CostBudget:
    """Per-client token bucket charged with estimated cost; refills continuously."""

    def __init__(
        self, capacity: float, refill_per_second: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}

    def try_spend(self, client: str, cost: float) -> int | None:
        """Return None when charged, or the whole seconds to wait before retrying."""
        now = self._clock()
        tokens, stamp = self._state.get(client, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - stamp) * self.refill_per_second)
        if cost <= tokens:
            self._state[client] = (tokens - cost, now)
            return None
        self._state[client] = (tokens, now)
        return math.ceil((cost - tokens) / self.refill_per_second)


def _reject(error: Rejected, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        _reject_body(error), status_code=error.status, media_type=MEDIA_TYPE, headers=headers
    )


async def read_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise Rejected(413, "BODY_TOO_LARGE", f"body exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise Rejected(413, "BODY_TOO_LARGE", f"body exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _entry(item: object) -> tuple[str, str | None, dict[str, Any]]:
    if not isinstance(item, dict):
        raise Rejected(422, "BAD_REQUEST", "each operation must be a JSON object")
    fields = cast("dict[str, Any]", item)
    query, name, variables = (
        fields.get("query"),
        fields.get("operationName"),
        fields.get("variables"),
    )
    if not isinstance(query, str):
        raise Rejected(422, "BAD_REQUEST", "query must be a string")
    if name is not None and not isinstance(name, str):
        raise Rejected(422, "BAD_REQUEST", "operationName must be a string")
    if variables is not None and not isinstance(variables, dict):
        raise Rejected(422, "BAD_REQUEST", "variables must be an object")
    return query, name, cast("dict[str, Any]", variables) if variables else {}


def build_app(
    schema: GraphQLSchema,
    backend: Backend,
    limits: Limits,
    budget: CostBudget,
    model: CostModel | None = None,
) -> Starlette:
    cost_model = model or CostModel()
    if limits.max_cost > budget.capacity:
        raise ValueError("max_cost above the budget capacity can never be paid")

    def run(prepared: Prepared) -> dict[str, Any]:
        result = execute_sync(
            schema,
            prepared.document,
            field_resolver=backend.resolve,
            variable_values=dict(prepared.variables),
            operation_name=prepared.operation_name,
        )
        return cast("dict[str, Any]", result.formatted)

    async def endpoint(request: Request) -> Response:
        client = request.client.host if request.client else "unknown"
        try:
            try:
                payload = json.loads(await read_body(request, limits.max_body_bytes))
            except (ValueError, RecursionError):
                raise Rejected(400, "BAD_JSON", "body is not valid JSON") from None
            items: list[Any] = payload if isinstance(payload, list) else [payload]  # pyright: ignore[reportUnknownVariableType]
            if isinstance(payload, list):
                if not limits.batching:
                    raise Rejected(400, "BATCHING_DISABLED", "batching is not enabled")
                if not 1 <= len(items) <= limits.max_batch_size:
                    raise Rejected(
                        400, "BATCH_LIMIT", f"batch size must be 1..{limits.max_batch_size}"
                    )
            prepared_items: list[Prepared | Rejected] = []
            for item in items:
                query, name, variables = _entry(item)
                try:
                    prepared_items.append(
                        prepare(schema, limits, cost_model, query, name, variables)
                    )
                except Rejected as error:
                    if not isinstance(payload, list):
                        raise
                    prepared_items.append(error)
            good = [p for p in prepared_items if isinstance(p, Prepared)]
            totals: Counter[str] = Counter()
            for prepared in good:
                totals.update(prepared.profile.names)
            enforce_sensitive(totals, limits)
            wait = budget.try_spend(client, sum(p.cost for p in good))
            if wait is not None:
                raise Rejected(
                    429, "RATE_LIMITED", f"cost budget exhausted, retry in {wait}s", wait
                )
        except Rejected as error:
            retry = None if error.retry_after is None else {"Retry-After": str(error.retry_after)}
            return _reject(error, retry)
        results = [_reject_body(p) if isinstance(p, Rejected) else run(p) for p in prepared_items]
        body: Any = results if isinstance(payload, list) else results[0]
        return JSONResponse(body, media_type=MEDIA_TYPE)

    return Starlette(routes=[Route("/graphql", endpoint, methods=["POST"])])


def _reject_body(error: Rejected) -> dict[str, Any]:
    return {"errors": [{"message": error.message, "extensions": {"code": error.code}}]}
