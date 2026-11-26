"""Lab: field-level authorization and non-null error bubbling in Strawberry 0.340.0.

One schema exposes the same guarded `salary` field in four shapes so that a test can change
exactly one variable at a time: `StrictUser` (`Int!`), `NullableUser` (`Int`), `SilentUser`
(`Int` with `fail_silently=True`), and `OpenUser` (`Int!` with no guard at all).
Authentication is faked: the viewer role is a request header, which a real service must never trust.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal

import strawberry
from fastapi import FastAPI, Request
from graphql import (
    ExecutionResult,
    GraphQLError,
    GraphQLSchema,
    graphql,  # pyright: ignore[reportUnknownVariableType]
    specified_directives,
)
from graphql.type.directives import GraphQLDisableErrorPropagationDirective
from strawberry.extensions import FieldExtension, SchemaExtension
from strawberry.fastapi import BaseContext, GraphQLRouter
from strawberry.http import process_result
from strawberry.permission import BasePermission, PermissionExtension

ROLE_HEADER: Final = "x-viewer-role"
KNOWN_IDS: Final = range(1, 101)


@dataclass(frozen=True)
class Employee:
    id: int
    name: str
    salary: int


def employee(employee_id: int) -> Employee:
    """Deterministic fake record: `user-<id>` earning `100000 + id`."""
    return Employee(id=employee_id, name=f"user-{employee_id}", salary=100_000 + employee_id)


@dataclass
class Ctx(BaseContext):
    """Per-request context: the (fake) viewer role plus a trace of resolver and check calls."""

    role: str
    trace: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        super().__init__()


Info = strawberry.Info[Ctx, None]


def admin_only(*, fail_silently: bool = False) -> list[FieldExtension]:
    return [PermissionExtension([IsAdmin()], fail_silently=fail_silently)]


class IsAdmin(BasePermission):
    message = "salary is restricted"
    error_extensions = {"code": "FORBIDDEN"}  # noqa: RUF012

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        info.context.trace.append(f"check:{info.field_name}")
        return info.context.role == "admin"


class IsAdminAsync(BasePermission):
    """Same rule as `IsAdmin`, but `async def`, so Strawberry takes the awaitable path."""

    message = "salary is restricted"
    error_extensions = {"code": "FORBIDDEN"}  # noqa: RUF012

    async def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        info.context.trace.append(f"check:{info.field_name}")
        await asyncio.sleep(0)
        return info.context.role == "admin"


class ReturnsTruthyString(BasePermission):
    """A buggy check that returns a truthy non-bool; Strawberry 0.340.0 must fail closed."""

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        return "yes"  # type: ignore[return-value]


class ReturnsAwaitable(BasePermission):
    """GHSA-pfvf-fwfp-25mp shape: a plain `def` returning a coroutine that resolves to False."""

    def has_permission(self, source: Any, info: Info, **kwargs: Any) -> bool:
        async def verdict() -> bool:
            return False

        return verdict()  # type: ignore[return-value]


@strawberry.type
class UserBase:
    employee: strawberry.Private[Employee]

    @strawberry.field
    def id(self) -> int:
        return self.employee.id

    @strawberry.field
    def name(self, info: Info) -> str:
        info.context.trace.append("resolve:name")
        return self.employee.name


@strawberry.type(name="StrictUser")
class StrictUser(UserBase):
    @strawberry.field(extensions=admin_only())  # type: ignore[untyped-decorator]
    def salary(self, info: Info) -> int:
        info.context.trace.append("resolve:salary")
        return self.employee.salary


@strawberry.type(name="AsyncStrictUser")
class AsyncStrictUser(UserBase):
    @strawberry.field(extensions=[PermissionExtension([IsAdminAsync()])])  # type: ignore[untyped-decorator]
    async def salary(self, info: Info) -> int:
        info.context.trace.append("resolve:salary")
        return self.employee.salary


@strawberry.type(name="NullableUser")
class NullableUser(UserBase):
    @strawberry.field(extensions=admin_only())  # type: ignore[untyped-decorator]
    def salary(self, info: Info) -> int | None:
        info.context.trace.append("resolve:salary")
        return self.employee.salary


@strawberry.type(name="SilentUser")
class SilentUser(UserBase):
    @strawberry.field(extensions=admin_only(fail_silently=True))  # type: ignore[untyped-decorator]
    def salary(self, info: Info) -> int | None:
        info.context.trace.append("resolve:salary")
        return self.employee.salary


@strawberry.type(name="OpenUser")
class OpenUser(UserBase):
    """`salary` has no guard: the default-allow trap that `DenyByDefault` closes."""

    @strawberry.field
    def salary(self, info: Info) -> int:
        info.context.trace.append("resolve:salary")
        return self.employee.salary


@strawberry.type
class Query:
    @strawberry.field
    def strict_me(self) -> StrictUser:
        return StrictUser(employee=employee(1))

    @strawberry.field
    def strict_lookup(self, id: int) -> StrictUser | None:
        return StrictUser(employee=employee(id)) if id in KNOWN_IDS else None

    @strawberry.field
    def strict_team(self, size: int = 3) -> list[StrictUser]:
        return [StrictUser(employee=employee(i)) for i in range(1, size + 1)]

    @strawberry.field
    def strict_directory(self, size: int = 3) -> list[StrictUser] | None:
        return [StrictUser(employee=employee(i)) for i in range(1, size + 1)]

    @strawberry.field
    def strict_roster(self, size: int = 3) -> list[StrictUser | None]:
        return [StrictUser(employee=employee(i)) for i in range(1, size + 1)]

    @strawberry.field
    def nullable_me(self) -> NullableUser:
        return NullableUser(employee=employee(1))

    @strawberry.field
    def nullable_team(self, size: int = 3) -> list[NullableUser]:
        return [NullableUser(employee=employee(i)) for i in range(1, size + 1)]

    @strawberry.field
    def silent_me(self) -> SilentUser:
        return SilentUser(employee=employee(1))

    @strawberry.field
    def open_me(self) -> OpenUser:
        return OpenUser(employee=employee(1))

    @strawberry.field
    def async_strict_team(self, size: int = 3) -> list[AsyncStrictUser]:
        return [AsyncStrictUser(employee=employee(i)) for i in range(1, size + 1)]

    @strawberry.field(extensions=[PermissionExtension([ReturnsAwaitable()])])  # type: ignore[untyped-decorator]
    def awaitable_quirk(self) -> int | None:
        return 1

    @strawberry.field(extensions=[PermissionExtension([ReturnsTruthyString()])])  # type: ignore[untyped-decorator]
    def quirk(self) -> int | None:
        return 1


ANY_ROLE: Final = "*"
FIELD_POLICY: Final[Mapping[str, frozenset[str]]] = {
    "OpenUser.id": frozenset({ANY_ROLE}),
    "OpenUser.name": frozenset({ANY_ROLE}),
    "OpenUser.salary": frozenset({"admin"}),
}


class DenyByDefault(SchemaExtension):
    """Central field policy keyed by schema coordinate; a field with no entry is denied.

    Root fields and introspection meta-fields are exempt: a root field has no parent object
    to type-check, and `__typename` must stay resolvable.
    """

    policy: ClassVar[Mapping[str, frozenset[str]]] = FIELD_POLICY

    def resolve(
        self,
        _next: Callable[..., object],
        root: object,
        info: Any,
        *args: str,
        **kwargs: Any,
    ) -> object:
        is_root_field = info.path.prev is None
        if not is_root_field and not info.field_name.startswith("__"):
            coordinate = f"{info.parent_type.name}.{info.field_name}"
            allowed = self.policy.get(coordinate, frozenset())
            role: str = info.context.role
            if ANY_ROLE not in allowed and role not in allowed:
                raise GraphQLError(
                    "not authorized", extensions={"code": "FORBIDDEN", "coordinate": coordinate}
                )
        return _next(root, info, *args, **kwargs)


def build_schema(*, deny_by_default: bool = False) -> strawberry.Schema:
    extensions: list[type[SchemaExtension]] = [DenyByDefault] if deny_by_default else []
    return strawberry.Schema(Query, extensions=extensions)


SCHEMA: Final = build_schema()


async def run(
    query: str,
    role: str = "analyst",
    *,
    schema: strawberry.Schema = SCHEMA,
    variables: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Execute in-process; return the response body Strawberry would serialize, and the trace."""
    context = Ctx(role=role)
    result = await schema.execute(query, variable_values=variables, context_value=context)
    return dict(process_result(result)), context.trace


async def run_without_error_propagation(query: str, role: str = "analyst") -> ExecutionResult:
    """Run `query` through graphql-core with `@experimental_disableErrorPropagation` declared.

    That directive is defined by graphql-core 3.3.2 but is not in `specified_directives`, so the
    schema must list it; it is not part of the September 2025 specification.
    """
    core: GraphQLSchema = SCHEMA._schema  # pyright: ignore[reportPrivateUsage]
    schema = GraphQLSchema(
        query=core.query_type,
        directives=[*specified_directives, GraphQLDisableErrorPropagationDirective],
    )
    return await graphql(schema, query, context_value=Ctx(role=role))


def build_app(schema: strawberry.Schema = SCHEMA) -> FastAPI:
    """Mount `schema` at `/graphql`; the viewer role is read from the lab-only header."""

    async def get_context(request: Request) -> Ctx:
        return Ctx(role=request.headers.get(ROLE_HEADER, "analyst"))

    app = FastAPI()
    app.include_router(GraphQLRouter(schema, context_getter=get_context), prefix="/graphql")
    return app


Outcome = Literal["ok", "partial", "total-failure", "request-error"]


def classify(body: Mapping[str, Any]) -> Outcome:
    """Classify a response body the way a client or monitor must, because the status cannot.

    Strawberry 0.340.0 serializes `"data": null` for request errors too, so a request error is
    recognized by an `errors` list in which some entry has no `path`.
    """
    errors: list[dict[str, Any]] = body.get("errors") or []
    if not errors:
        return "ok"
    if any("path" not in error for error in errors):
        return "request-error"
    return "partial" if body.get("data") is not None else "total-failure"


def draft_status(body: Mapping[str, Any]) -> int | None:
    """Status code the GraphQL-over-HTTP draft (5.4) recommends; `None` for a request error.

    A request error needs a 4xx whose exact value (400 versus 422) depends on its cause.
    """
    outcome = classify(body)
    if outcome == "request-error":
        return None
    return 200 if outcome == "ok" else 294


def forbidden_paths(body: Mapping[str, Any]) -> list[tuple[str | int, ...]]:
    """Response paths whose `null` is a denial, not a missing value."""
    errors: list[dict[str, Any]] = body.get("errors") or []
    return [
        tuple(error["path"])
        for error in errors
        if error.get("extensions", {}).get("code") == "FORBIDDEN" and "path" in error
    ]
