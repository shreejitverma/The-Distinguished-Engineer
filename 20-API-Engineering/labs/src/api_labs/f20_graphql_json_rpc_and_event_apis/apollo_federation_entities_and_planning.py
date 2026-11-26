"""Apollo Federation v2 lab: two Strawberry subgraphs, SDL composition, query planning, execution.

Two independent Strawberry subgraphs expose the entity ``User`` under different ownership:
``users`` owns ``id name region``; ``orders`` contributes ``orders`` and ``taxNote`` where
``taxNote`` is ``@requires(fields: "region")`` and ``region`` is ``@external``.
A small router reads each subgraph's ``_service { sdl }``, composes the field ownership map,
plans a tree of Fetch/Flatten/Sequence/Parallel nodes, and executes ``_entities`` calls over
in-process ASGI transports.
The planner is a teaching model: it picks the first owner in name order and does not search
for the cheapest plan the way the Apollo query planner does.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import strawberry
from graphql import (
    BooleanValueNode,
    FieldNode,
    ListTypeNode,
    NamedTypeNode,
    NonNullTypeNode,
    ObjectTypeDefinitionNode,
    OperationDefinitionNode,
    OperationType,
    StringValueNode,
    TypeNode,
    parse,
    print_ast,
)
from strawberry.asgi import GraphQL
from strawberry.federation.schema_directives import Key
from strawberry.federation.types import FieldSet
from strawberry.types import Info  # noqa: TC002  (strawberry reads this annotation at runtime)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx

# ---------------------------------------------------------------------------
# Subgraph data and per-request context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRow:
    name: str
    region: str


@dataclass(frozen=True)
class OrderRow:
    id: str
    user_id: str
    total_cents: int


@dataclass
class SubgraphContext:
    """Data plus the observation log a test reads back."""

    users: dict[str, UserRow] = field(default_factory=dict)
    orders: list[OrderRow] = field(default_factory=list)
    reference_calls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Users subgraph: owns User.id, User.name, User.region
# ---------------------------------------------------------------------------


@strawberry.federation.type(keys=["id"], name="User")
class UsersUser:
    id: strawberry.ID
    name: str
    region: str

    @classmethod
    def resolve_reference(cls, info: Info, id: strawberry.ID) -> UsersUser | None:
        context: SubgraphContext = info.context
        context.reference_calls.append(str(id))
        row = context.users.get(str(id))
        return None if row is None else cls(id=id, name=row.name, region=row.region)


@strawberry.type
class UsersQuery:
    @strawberry.field
    def users(self, info: Info) -> list[UsersUser]:
        context: SubgraphContext = info.context
        return [
            UsersUser(id=strawberry.ID(k), name=v.name, region=v.region)
            for k, v in context.users.items()
        ]

    @strawberry.field
    def user(self, info: Info, id: strawberry.ID) -> UsersUser | None:
        context: SubgraphContext = info.context
        row = context.users.get(str(id))
        return None if row is None else UsersUser(id=id, name=row.name, region=row.region)


# ---------------------------------------------------------------------------
# Orders subgraph: contributes User.orders and User.taxNote (@requires region)
# ---------------------------------------------------------------------------


@strawberry.type
class Order:
    id: strawberry.ID
    total_cents: int
    buyer: OrdersUser


@strawberry.federation.type(keys=["id"], name="User")
class OrdersUser:
    id: strawberry.ID
    region: str | None = strawberry.federation.field(external=True, default=None)

    @classmethod
    def resolve_reference(
        cls,
        info: Info,
        id: strawberry.ID,
        region: str | None = None,
    ) -> OrdersUser:
        context: SubgraphContext = info.context
        context.reference_calls.append(str(id))
        return cls(id=id, region=region)

    @strawberry.federation.field  # type: ignore[call-overload]
    def orders(self, info: Info) -> list[Order]:
        context: SubgraphContext = info.context
        return [
            Order(id=strawberry.ID(o.id), total_cents=o.total_cents, buyer=self)
            for o in context.orders
            if o.user_id == str(self.id)
        ]

    @strawberry.federation.field(requires=["region"])  # type: ignore[untyped-decorator]
    def tax_note(self) -> str:
        if self.region is None:
            raise ValueError("region missing: the router did not honour @requires")
        return "VAT 20%" if self.region == "EU" else "no VAT"


@strawberry.type
class OrdersQuery:
    @strawberry.field
    def recent_orders(self, info: Info) -> list[Order]:
        context: SubgraphContext = info.context
        return [
            Order(
                id=strawberry.ID(o.id),
                total_cents=o.total_cents,
                buyer=OrdersUser(id=strawberry.ID(o.user_id)),
            )
            for o in context.orders
        ]


# ---------------------------------------------------------------------------
# A stub entity with resolvable: false, used to check Strawberry against the subgraph spec
# ---------------------------------------------------------------------------


@strawberry.federation.type(keys=[Key(fields=FieldSet("id"), resolvable=False)], name="User")
class StubUser:
    id: strawberry.ID


@strawberry.type
class StubQuery:
    @strawberry.field
    def who(self) -> StubUser:
        return StubUser(id=strawberry.ID("1"))


def build_users_schema() -> strawberry.federation.Schema:
    return strawberry.federation.Schema(query=UsersQuery)


def build_orders_schema() -> strawberry.federation.Schema:
    return strawberry.federation.Schema(query=OrdersQuery, types=[OrdersUser])


def build_stub_schema() -> strawberry.federation.Schema:
    return strawberry.federation.Schema(query=StubQuery)


class _ContextGraphQL(GraphQL[SubgraphContext, None]):
    """strawberry.asgi.GraphQL that hands every request the same SubgraphContext."""

    def __init__(self, schema: strawberry.federation.Schema, context: SubgraphContext) -> None:
        super().__init__(schema, graphql_ide=None)
        self._context = context

    async def get_context(self, request: Any, response: Any) -> SubgraphContext:
        return self._context


def build_subgraph_app(
    schema: strawberry.federation.Schema, context: SubgraphContext
) -> GraphQL[SubgraphContext, None]:
    """ASGI app for one subgraph; every request sees the same SubgraphContext."""
    return _ContextGraphQL(schema, context)


def sample_users() -> dict[str, UserRow]:
    return {"1": UserRow("Ada", "EU"), "2": UserRow("Grace", "US")}


def sample_orders() -> list[OrderRow]:
    return [
        OrderRow("o1", "1", 1200),
        OrderRow("o2", "2", 800),
        OrderRow("o3", "1", 300),
        OrderRow("o4", "2", 99),
    ]


# ---------------------------------------------------------------------------
# SDL parsing and composition
# ---------------------------------------------------------------------------


class CompositionFailedError(Exception):
    """Raised with every composition error code found, in sorted order."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = sorted(errors)
        super().__init__("; ".join(f"{code}: {message}" for code, message in self.errors))

    @property
    def codes(self) -> list[str]:
        return [code for code, _ in self.errors]


class PlanError(Exception):
    """The operation cannot be planned against the composed supergraph."""


@dataclass(frozen=True)
class FieldDef:
    name: str
    type_name: str
    is_list: bool
    external: bool = False
    shareable: bool = False
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class KeyDef:
    fields: tuple[str, ...]
    resolvable: bool = True


@dataclass
class TypeDef:
    name: str
    fields: dict[str, FieldDef] = field(default_factory=dict)
    keys: list[KeyDef] = field(default_factory=list)
    shareable: bool = False


@dataclass
class SubgraphSchema:
    name: str
    types: dict[str, TypeDef]


def _unwrap(node: TypeNode) -> tuple[str, bool]:
    is_list = False
    while not isinstance(node, NamedTypeNode):
        if isinstance(node, ListTypeNode):
            is_list = True
            node = node.type
        elif isinstance(node, NonNullTypeNode):
            node = node.type
        else:  # pragma: no cover - the AST has only these wrappers
            raise PlanError("unsupported type node")
    return node.name.value, is_list


def _fieldset(text: str) -> tuple[str, ...]:
    if "{" in text or "(" in text:
        raise PlanError(f"nested or argument field sets are out of scope for the lab: {text!r}")
    return tuple(text.split())


def parse_subgraph_sdl(name: str, sdl: str) -> SubgraphSchema:
    """Read object types, @key, @external, @requires, and @shareable from a subgraph SDL."""
    types: dict[str, TypeDef] = {}
    for definition in parse(sdl).definitions:
        if not isinstance(definition, ObjectTypeDefinitionNode):
            continue
        type_name = definition.name.value
        if type_name.startswith("_"):
            continue
        type_def = TypeDef(type_name)
        for directive in definition.directives or ():
            directive_name = directive.name.value
            if directive_name == "shareable":
                type_def.shareable = True
            if directive_name == "key":
                arguments = {a.name.value: a.value for a in directive.arguments or ()}
                fields_node = arguments["fields"]
                if not isinstance(fields_node, StringValueNode):
                    raise PlanError("@key(fields:) must be a string")
                resolvable_node = arguments.get("resolvable")
                resolvable = not (
                    isinstance(resolvable_node, BooleanValueNode) and not resolvable_node.value
                )
                type_def.keys.append(KeyDef(_fieldset(fields_node.value), resolvable))
        for field_node in definition.fields or ():
            if field_node.name.value.startswith("_"):
                continue
            flags = {d.name.value: d for d in field_node.directives or ()}
            requires: tuple[str, ...] = ()
            if "requires" in flags:
                requires_node = (flags["requires"].arguments or ())[0].value
                if not isinstance(requires_node, StringValueNode):
                    raise PlanError("@requires(fields:) must be a string")
                requires = _fieldset(requires_node.value)
            target, is_list = _unwrap(field_node.type)
            type_def.fields[field_node.name.value] = FieldDef(
                name=field_node.name.value,
                type_name=target,
                is_list=is_list,
                external="external" in flags,
                shareable="shareable" in flags,
                requires=requires,
            )
        types[type_name] = type_def
    return SubgraphSchema(name, types)


@dataclass
class Supergraph:
    """Composed ownership map: which subgraphs can resolve each type.field."""

    subgraphs: dict[str, SubgraphSchema]

    def owners(self, type_name: str, field_name: str) -> list[str]:
        found = []
        for name in sorted(self.subgraphs):
            type_def = self.subgraphs[name].types.get(type_name)
            definition = None if type_def is None else type_def.fields.get(field_name)
            if definition is not None and not definition.external:
                found.append(name)
        return found

    def is_object(self, type_name: str) -> bool:
        return any(type_name in s.types for s in self.subgraphs.values())


def compose(schemas: Mapping[str, SubgraphSchema]) -> Supergraph:
    """Apply four composition rules; the codes are the Apollo error codes of the same name."""
    errors: list[tuple[str, str]] = []
    type_names = {t for s in schemas.values() for t in s.types}
    for type_name in sorted(type_names):
        field_names = {
            f for s in schemas.values() for f in s.types.get(type_name, TypeDef(type_name)).fields
        }
        for field_name in sorted(field_names):
            defs = [
                (s.name, s.types[type_name], s.types[type_name].fields[field_name])
                for s in schemas.values()
                if type_name in s.types and field_name in s.types[type_name].fields
            ]
            resolving = [(n, t, d) for n, t, d in defs if not d.external]
            if any(d.external for _, _, d in defs) and not resolving:
                errors.append(("EXTERNAL_MISSING_ON_BASE", f"{type_name}.{field_name}"))
            is_key = all(any(field_name in k.fields for k in t.keys) for _, t, _ in resolving)
            if (
                len(resolving) > 1
                and not is_key
                and not all(d.shareable or t.shareable for _, t, d in resolving)
            ):
                names = ",".join(n for n, _, _ in resolving)
                errors.append(("INVALID_FIELD_SHARING", f"{type_name}.{field_name} in {names}"))
    for schema in schemas.values():
        for type_def in schema.types.values():
            for key in type_def.keys:
                for key_field in key.fields:
                    if key_field not in type_def.fields:
                        errors.append(
                            ("KEY_INVALID_FIELDS", f"{schema.name}:{type_def.name} key {key_field}")
                        )
            for definition in type_def.fields.values():
                for required in definition.requires:
                    target = type_def.fields.get(required)
                    if target is None or not target.external:
                        errors.append(
                            (
                                "REQUIRES_FIELDS_MISSING_EXTERNAL",
                                f"{schema.name}:{type_def.name}.{definition.name}",
                            )
                        )
    if errors:
        raise CompositionFailedError(errors)
    return Supergraph(dict(schemas))


# ---------------------------------------------------------------------------
# Client operation parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sel:
    name: str
    args: str = ""
    children: tuple[Sel, ...] = ()


def parse_operation(text: str) -> tuple[Sel, ...]:
    """Parse one anonymous query of plain fields; fragments, aliases, variables are rejected."""
    document = parse(text)
    if len(document.definitions) != 1 or not isinstance(
        document.definitions[0], OperationDefinitionNode
    ):
        raise PlanError("exactly one operation is supported")
    operation = document.definitions[0]
    if operation.operation is not OperationType.QUERY or operation.variable_definitions:
        raise PlanError("only variable-free queries are supported")

    def convert(node: FieldNode) -> Sel:
        if node.alias is not None or node.directives:
            raise PlanError("aliases and directives are out of scope")
        args = "(" + ", ".join(print_ast(a) for a in node.arguments) + ")" if node.arguments else ""
        children: list[Sel] = []
        for child in node.selection_set.selections if node.selection_set else ():
            if not isinstance(child, FieldNode):
                raise PlanError("fragments are out of scope")
            children.append(convert(child))
        return Sel(node.name.value, args, tuple(children))

    selections = []
    for item in operation.selection_set.selections:
        if not isinstance(item, FieldNode):
            raise PlanError("fragments are out of scope")
        selections.append(convert(item))
    return tuple(selections)


# ---------------------------------------------------------------------------
# Query plan
# ---------------------------------------------------------------------------


@dataclass
class SelNode:
    name: str
    args: str = ""
    children: dict[str, SelNode] = field(default_factory=dict)

    def child(self, name: str, args: str = "") -> SelNode:
        existing = self.children.get(name)
        if existing is None:
            existing = SelNode(name, args)
            if name == "__typename":
                self.children = {name: existing, **self.children}
            else:
                self.children[name] = existing
        elif existing.args != args:
            raise PlanError(f"conflicting arguments for {name}")
        return existing

    def render(self) -> str:
        body = ""
        if self.children:
            body = " { " + " ".join(c.render() for c in self.children.values()) + " }"
        return f"{self.name}{self.args}{body}"


@dataclass
class FetchGroup:
    id: int
    subgraph: str
    path: tuple[str, ...]
    type_name: str
    root: SelNode = field(default_factory=lambda: SelNode(""))
    key: tuple[str, ...] = ()
    extra_inputs: list[str] = field(default_factory=list)
    depends_on: set[int] = field(default_factory=set)

    @property
    def is_entity(self) -> bool:
        return bool(self.key)

    @property
    def inputs(self) -> tuple[str, ...]:
        return (*self.key, *self.extra_inputs)


@dataclass
class _Cursor:
    group: FetchGroup
    node: SelNode
    type_name: str
    path: tuple[str, ...]


@dataclass
class QueryPlan:
    groups: list[FetchGroup]
    levels: list[list[FetchGroup]]

    def render(self) -> str:
        lines = ["QueryPlan {"]
        sequence = len(self.levels) > 1
        indent = "    " if sequence else "  "
        if sequence:
            lines.append("  Sequence {")
        for level in self.levels:
            if len(level) > 1:
                lines.append(f"{indent}Parallel {{")
                for group in level:
                    lines.extend(_render_group(group, indent + "  "))
                lines.append(f"{indent}}},")
            else:
                lines.extend(_render_group(level[0], indent))
        if sequence:
            lines.append("  },")
        lines.append("}")
        return "\n".join(lines)


def _render_group(group: FetchGroup, indent: str) -> list[str]:
    if not group.is_entity:
        body = " ".join(c.render() for c in group.root.children.values())
        return [f'{indent}Fetch(service: "{group.subgraph}") {{ {{ {body} }} }},']
    rep = " ".join(("__typename", *group.inputs))
    body = " ".join(c.render() for c in group.root.children.values())
    path = ".".join(group.path)
    return [
        f'{indent}Flatten(path: "{path}") {{',
        f'{indent}  Fetch(service: "{group.subgraph}") {{',
        f"{indent}    {{ ... on {group.type_name} {{ {rep} }} }} =>",
        f"{indent}    {{ ... on {group.type_name} {{ {body} }} }}",
        f"{indent}  }},",
        f"{indent}}},",
    ]


class Planner:
    """Builds fetch groups: one root group per owner and one entity group per (subgraph, path)."""

    def __init__(self, supergraph: Supergraph) -> None:
        self.supergraph = supergraph
        self.groups: list[FetchGroup] = []

    def plan(self, selections: tuple[Sel, ...]) -> QueryPlan:
        self.groups = []
        for sel in selections:
            owners = self.supergraph.owners("Query", sel.name)
            if not owners:
                raise PlanError(f"Query.{sel.name} is not defined by any subgraph")
            group = self._group(owners[0], (), "Query", key=())
            cursor = _Cursor(group, group.root, "Query", ())
            self._place(cursor, sel)
        return QueryPlan(self.groups, self._levels())

    def _group(
        self, subgraph: str, path: tuple[str, ...], type_name: str, key: tuple[str, ...]
    ) -> FetchGroup:
        for group in self.groups:
            if (group.subgraph, group.path, group.type_name) == (subgraph, path, type_name):
                return group
        group = FetchGroup(len(self.groups), subgraph, path, type_name, key=key)
        self.groups.append(group)
        return group

    def _local(self, subgraph: str, type_name: str, name: str) -> FieldDef | None:
        type_def = self.supergraph.subgraphs[subgraph].types.get(type_name)
        definition = None if type_def is None else type_def.fields.get(name)
        if definition is None or definition.external or definition.requires:
            return None
        return definition

    def _add_local(self, cursor: _Cursor, sel: Sel) -> None:
        schema = self.supergraph.subgraphs[cursor.group.subgraph]
        definition = schema.types[cursor.type_name].fields[sel.name]
        node = cursor.node.child(sel.name, sel.args)
        if not sel.children:
            return
        if not self.supergraph.is_object(definition.type_name):
            raise PlanError(f"{sel.name} is a leaf field")
        path = (*cursor.path, sel.name, *(("@",) if definition.is_list else ()))
        child_cursor = _Cursor(cursor.group, node, definition.type_name, path)
        for child in sel.children:
            self._place(child_cursor, child)

    def _place(self, cursor: _Cursor, sel: Sel) -> FetchGroup:
        """Put one field where it can be resolved; return the group that provides it."""
        if self._local(cursor.group.subgraph, cursor.type_name, sel.name) is not None:
            self._add_local(cursor, sel)
            return cursor.group
        owners = self.supergraph.owners(cursor.type_name, sel.name)
        if not owners:
            raise PlanError(f"{cursor.type_name}.{sel.name} is not defined by any subgraph")
        owner = owners[0]
        owner_type = self.supergraph.subgraphs[owner].types[cursor.type_name]
        key = next(
            (
                k
                for k in owner_type.keys
                if k.resolvable
                and all(
                    self._local(cursor.group.subgraph, cursor.type_name, f) is not None
                    for f in k.fields
                )
            ),
            None,
        )
        if key is None:
            raise PlanError(
                f"no key of {owner}:{cursor.type_name} is resolvable from {cursor.group.subgraph}"
            )
        cursor.node.child("__typename")
        for key_field in key.fields:
            cursor.node.child(key_field)
        entity = self._group(owner, cursor.path, cursor.type_name, key=key.fields)
        entity.depends_on.add(cursor.group.id)
        definition = owner_type.fields[sel.name]
        for required in definition.requires:
            provider = self._provide(cursor, required)
            if provider.id != entity.id:
                entity.depends_on.add(provider.id)
            if required not in entity.extra_inputs:
                entity.extra_inputs.append(required)
        self._add_local(_Cursor(entity, entity.root, cursor.type_name, cursor.path), sel)
        return entity

    def _provide(self, cursor: _Cursor, required: str) -> FetchGroup:
        return self._place(cursor, Sel(required))

    def _levels(self) -> list[list[FetchGroup]]:
        by_id = {g.id: g for g in self.groups}
        level_of: dict[int, int] = {}

        def level(group: FetchGroup, trail: tuple[int, ...]) -> int:
            if group.id in trail:
                raise PlanError("cyclic fetch dependencies")
            if group.id not in level_of:
                deps = [level(by_id[d], (*trail, group.id)) for d in group.depends_on]
                level_of[group.id] = 1 + max(deps) if deps else 0
            return level_of[group.id]

        for group in self.groups:
            level(group, ())
        depth = max(level_of.values()) + 1
        return [[g for g in self.groups if level_of[g.id] == n] for n in range(depth)]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchRecord:
    subgraph: str
    entity: bool
    representations: int


@dataclass
class RouterResult:
    data: dict[str, Any]
    errors: list[dict[str, Any]]
    plan: QueryPlan
    fetches: list[FetchRecord]


def _collect(data: dict[str, Any], path: tuple[str, ...]) -> list[dict[str, Any]]:
    nodes: list[Any] = [data]
    for element in path:
        following: list[Any] = []
        for node in nodes:
            if element == "@":
                following.extend(node if isinstance(node, list) else [])
            elif isinstance(node, dict) and node.get(element) is not None:
                following.append(node[element])
        nodes = following
    return [n for n in nodes if isinstance(n, dict)]


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _merge(existing, value)
        elif isinstance(existing, list) and isinstance(value, list) and len(existing) == len(value):
            for old, new in zip(existing, value, strict=True):
                if isinstance(old, dict) and isinstance(new, dict):
                    _merge(old, new)
        else:
            target[key] = value


def _project(value: Any, selections: tuple[Sel, ...]) -> Any:
    if isinstance(value, list):
        return [_project(v, selections) for v in value]
    if not isinstance(value, dict):
        return value
    return {
        s.name: _project(value.get(s.name), s.children) if s.children else value.get(s.name)
        for s in selections
    }


class Router:
    """Composes from `_service { sdl }`, plans, and executes over per-subgraph httpx clients."""

    def __init__(self, clients: Mapping[str, httpx.AsyncClient]) -> None:
        self.clients = dict(clients)
        self.supergraph: Supergraph | None = None

    async def compose_from_subgraphs(self) -> Supergraph:
        schemas: dict[str, SubgraphSchema] = {}
        for name, client in sorted(self.clients.items()):
            payload = await self._post(client, "{ _service { sdl } }", None)
            schemas[name] = parse_subgraph_sdl(name, payload["data"]["_service"]["sdl"])
        self.supergraph = compose(schemas)
        return self.supergraph

    def plan(self, query: str) -> QueryPlan:
        if self.supergraph is None:
            raise PlanError("compose_from_subgraphs() has not run")
        return Planner(self.supergraph).plan(parse_operation(query))

    async def execute(self, query: str) -> RouterResult:
        selections = parse_operation(query)
        plan = self.plan(query)
        data: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        fetches: list[FetchRecord] = []
        for level in plan.levels:
            async with asyncio.TaskGroup() as group:
                for fetch in level:
                    group.create_task(self._run(fetch, data, errors, fetches))
        return RouterResult(_project(data, selections), errors, plan, fetches)

    async def _post(
        self, client: httpx.AsyncClient, query: str, variables: dict[str, Any] | None
    ) -> dict[str, Any]:
        response = await client.post("/", json={"query": query, "variables": variables})
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    async def _run(
        self,
        fetch: FetchGroup,
        data: dict[str, Any],
        errors: list[dict[str, Any]],
        fetches: list[FetchRecord],
    ) -> None:
        client = self.clients[fetch.subgraph]
        if not fetch.is_entity:
            body = " ".join(c.render() for c in fetch.root.children.values())
            payload = await self._post(client, f"query {{ {body} }}", None)
            fetches.append(FetchRecord(fetch.subgraph, False, 0))
            errors.extend({"subgraph": fetch.subgraph, **e} for e in payload.get("errors", []))
            if payload.get("data"):
                _merge(data, payload["data"])
            return
        targets = _collect(data, fetch.path)
        unique: dict[str, int] = {}
        representations: list[dict[str, Any]] = []
        owners_of: list[list[dict[str, Any]]] = []
        for target in targets:
            if any(target.get(name) is None for name in fetch.inputs):
                continue
            rep = {"__typename": fetch.type_name, **{name: target[name] for name in fetch.inputs}}
            marker = json.dumps(rep, sort_keys=True)
            if marker not in unique:
                unique[marker] = len(representations)
                representations.append(rep)
                owners_of.append([])
            owners_of[unique[marker]].append(target)
        if not representations:
            return
        body = " ".join(c.render() for c in fetch.root.children.values())
        query = (
            "query($representations: [_Any!]!) { _entities(representations: $representations) "
            f"{{ ... on {fetch.type_name} {{ {body} }} }} }}"
        )
        payload = await self._post(client, query, {"representations": representations})
        fetches.append(FetchRecord(fetch.subgraph, True, len(representations)))
        errors.extend({"subgraph": fetch.subgraph, **e} for e in payload.get("errors", []))
        results = (payload.get("data") or {}).get("_entities") or []
        for index, result in enumerate(results):
            if isinstance(result, dict):
                for target in owners_of[index]:
                    _merge(target, result)
