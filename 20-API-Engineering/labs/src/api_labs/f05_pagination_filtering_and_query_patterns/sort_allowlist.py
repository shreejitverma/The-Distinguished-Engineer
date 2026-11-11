"""Multi-column order_by: allowlisted keys, NULL placement, index completion, plan checks."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, cast

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from starlette.requests import Request

MAX_ORDER_BY_LENGTH: Final = 128
MAX_SORT_KEYS: Final = 3
PAGE_SIZE: Final = 20
_PREFIX_TERM: Final = re.compile(r"(-?)([a-z][a-z0-9_]{0,62})", re.ASCII)
_AIP132_TERM: Final = re.compile(r"([a-z][a-z0-9_]{0,62})(?: +(desc))?", re.ASCII)


class Direction(Enum):
    ASC = "ASC"
    DESC = "DESC"

    def flipped(self) -> Direction:
        return Direction.DESC if self is Direction.ASC else Direction.ASC


class Nulls(Enum):
    """Where NULLs go; LAST is an API promise that holds in both directions."""

    NOT_NULL = "not-null"
    LAST = "last"


@dataclass(frozen=True, slots=True)
class SortField:
    """An API sort field mapped to an application-owned SQL column name."""

    column: str
    nulls: Nulls = Nulls.NOT_NULL


@dataclass(frozen=True, slots=True)
class SortKey:
    field: str
    direction: Direction = Direction.ASC

    def flipped(self) -> SortKey:
        return SortKey(self.field, self.direction.flipped())

    def api(self) -> str:
        return ("-" if self.direction is Direction.DESC else "") + self.field


@dataclass(frozen=True, slots=True)
class Term:
    """One physical ORDER BY term; expr only ever comes from SortField.column."""

    expr: str
    direction: Direction

    def sql(self) -> str:
        return f"{self.expr} {self.direction.value}"


@dataclass(frozen=True, slots=True)
class Index:
    name: str
    keys: tuple[SortKey, ...]
    primary: bool = False  # the table's own key order; no CREATE INDEX


@dataclass(frozen=True, slots=True)
class Ordering:
    requested: tuple[SortKey, ...]
    keys: tuple[SortKey, ...]  # the requested keys completed from the serving index
    terms: tuple[Term, ...]
    index: str

    def api(self) -> str:
        return ",".join(k.api() for k in self.keys)

    def sql(self) -> str:
        return order_by_clause(self.terms)


class SortError(ValueError):
    """A client error in order_by; code is stable, detail is for humans."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _check_length(raw: str) -> None:
    if len(raw) > MAX_ORDER_BY_LENGTH:
        raise SortError("too_long", f"order_by is longer than {MAX_ORDER_BY_LENGTH} characters")


def _split(raw: str) -> list[str]:
    return [part.strip(" ") for part in raw.split(",")]


def parse_order_by(raw: str) -> tuple[SortKey, ...]:
    """Parse `-created_at,priority` (JSON:API style); an empty value means the default."""
    _check_length(raw)
    if raw == "":
        return ()
    keys: list[SortKey] = []
    for part in _split(raw):
        m = _PREFIX_TERM.fullmatch(part)
        if m is None:
            raise SortError("malformed", f"cannot parse sort term {part!r}")
        keys.append(SortKey(m[2], Direction.DESC if m[1] else Direction.ASC))
    return tuple(keys)


def parse_aip132(raw: str) -> tuple[SortKey, ...]:
    """Parse `created_at desc, priority` (AIP-132 order_by); spaces are insignificant."""
    _check_length(raw)
    if raw.strip(" ") == "":
        return ()
    keys: list[SortKey] = []
    for part in _split(raw):
        m = _AIP132_TERM.fullmatch(part)
        if m is None:
            raise SortError("malformed", f"cannot parse sort term {part!r}")
        keys.append(SortKey(m[1], Direction.DESC if m[2] else Direction.ASC))
    return tuple(keys)


def order_by_clause(terms: Iterable[Term]) -> str:
    return ", ".join(t.sql() for t in terms)


@dataclass(frozen=True)
class SortPolicy:
    """The sort contract: which fields exist, and which orderings an index can serve."""

    fields: Mapping[str, SortField]
    indexes: tuple[Index, ...]
    default: tuple[SortKey, ...]
    tie_breaker: str = "id"
    max_keys: int = MAX_SORT_KEYS

    def __post_init__(self) -> None:
        for index in self.indexes:
            unknown = [k.field for k in index.keys if k.field not in self.fields]
            if unknown:
                raise ValueError(f"{index.name} uses fields outside the allowlist: {unknown}")
            if not index.keys or index.keys[-1].field != self.tie_breaker:
                raise ValueError(f"{index.name} must end in the unique {self.tie_breaker!r}")
        # A bad default is a server bug; never let it surface as a client 400.
        try:
            served = self.match(self.check(self.default))
        except SortError as exc:
            raise ValueError(f"default ordering is invalid: {exc.detail}") from exc
        if served is None:
            raise ValueError("no index serves the default ordering")

    def expand(self, keys: Iterable[SortKey]) -> tuple[Term, ...]:
        """Map API keys to SQL terms; a NULLS LAST field sorts on `col IS NULL` first."""
        terms: list[Term] = []
        for key in keys:
            field = self.fields[key.field]
            if field.nulls is Nulls.LAST:
                terms.append(Term(f"{field.column} IS NULL", Direction.ASC))
            terms.append(Term(field.column, key.direction))
        return tuple(terms)

    def _orientations(self, index: Index) -> list[tuple[SortKey, ...]]:
        # Scanning backward flips every term, including `col IS NULL`, which would
        # move NULLs first; so an index holding a NULLS LAST field is forward-only.
        if any(self.fields[k.field].nulls is Nulls.LAST for k in index.keys):
            return [index.keys]
        return [index.keys, tuple(k.flipped() for k in index.keys)]

    def match(self, keys: tuple[SortKey, ...]) -> Ordering | None:
        """Return the ordering an index can deliver with no sort step, or None."""
        for index in self.indexes:
            for oriented in self._orientations(index):
                if oriented[: len(keys)] == keys:
                    return Ordering(keys, oriented, self.expand(oriented), index.name)
        return None

    def check(self, keys: tuple[SortKey, ...]) -> tuple[SortKey, ...]:
        """Validate keys against the field allowlist and canonicalize them."""
        if len(keys) > self.max_keys:
            raise SortError("too_many_keys", f"at most {self.max_keys} sort keys are allowed")
        seen: set[str] = set()
        for key in keys:
            if key.field not in self.fields:
                raise SortError("unknown_field", f"{key.field!r} is not a sortable field")
            if key.field in seen:
                raise SortError("duplicate_field", f"{key.field!r} appears more than once")
            seen.add(key.field)
        # Keys after the unique tie-breaker can never change the order; refuse them
        # rather than drop them, so `id,title` is not legal while `title` is not.
        for key in keys[:-1]:
            if key.field == self.tie_breaker:
                raise SortError(
                    "keys_after_tie_breaker", f"{self.tie_breaker!r} is unique, so it must be last"
                )
        return keys

    def resolve(self, keys: tuple[SortKey, ...]) -> Ordering:
        requested = self.check(keys) or self.default
        ordering = self.match(requested)
        if ordering is None:
            supported = ", ".join(self.supported())
            raise SortError(
                "unsupported_ordering",
                f"no index serves order_by={','.join(k.api() for k in requested)}; "
                f"supported: {supported}",
            )
        return ordering

    def naive(self, keys: tuple[SortKey, ...]) -> tuple[Term, ...]:
        """Append the tie-breaker in the last key's direction: total, but often unindexed."""
        keys = self.check(keys) or self.default
        if keys[-1].field != self.tie_breaker:
            keys = (*keys, SortKey(self.tie_breaker, keys[-1].direction))
        return self.expand(keys)

    def supported(self) -> list[str]:
        """Every order_by value this policy accepts, for docs and OpenAPI enums."""
        out: list[str] = []
        for index in self.indexes:
            for oriented in self._orientations(index):
                for n in range(1, min(len(oriented), self.max_keys) + 1):
                    value = ",".join(k.api() for k in oriented[:n])
                    if value not in out:
                        out.append(value)
        return out


ASC, DESC = Direction.ASC, Direction.DESC

TASK_FIELDS: Final[Mapping[str, SortField]] = {
    "id": SortField("id"),
    "created_at": SortField("created_at"),
    "priority": SortField("priority"),
    "due_at": SortField("due_at", Nulls.LAST),
    "title": SortField("title"),  # readable and filterable, but no index: not sortable
}

TASK_INDEXES: Final = (
    Index("pk", (SortKey("id"),), primary=True),
    Index("ix_tasks_created", (SortKey("created_at"), SortKey("id"))),
    Index("ix_tasks_priority_created", (SortKey("priority"), SortKey("created_at"), SortKey("id"))),
    Index("ix_tasks_due_asc", (SortKey("due_at"), SortKey("id"))),
    Index("ix_tasks_due_desc", (SortKey("due_at", DESC), SortKey("id", DESC))),
)

TASK_POLICY: Final = SortPolicy(
    TASK_FIELDS, TASK_INDEXES, default=(SortKey("created_at", DESC), SortKey("id", DESC))
)

TASKS_DDL: Final = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    due_at INTEGER,
    title TEXT NOT NULL,
    api_key TEXT NOT NULL
)
"""

type TaskRow = tuple[int, int, int, int | None, str, str]


def index_ddl(policy: SortPolicy, index: Index) -> str:
    return f"CREATE INDEX {index.name} ON tasks ({order_by_clause(policy.expand(index.keys))})"


def make_tasks(n: int) -> list[TaskRow]:
    """Deterministic rows: 5 priorities, created_at ties in threes, every 4th due_at NULL."""
    rows: list[TaskRow] = []
    for i in range(1, n + 1):
        created = 1_700_000_000 + ((i * 7919) % n) // 3
        due = None if i % 4 == 0 else 1_800_000_000 + (i * 104_729) % n
        secret = f"key-{(i * 2_654_435_761) % n:06d}"
        rows.append((i, created, i % 5, due, f"task {(i * 31) % n:05d}", secret))
    return rows


def create_store(rows: Iterable[TaskRow], policy: SortPolicy = TASK_POLICY) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(TASKS_DDL)
    for index in policy.indexes:
        if not index.primary:
            conn.execute(index_ddl(policy, index))
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", rows)
    return conn


def list_sql(terms: Iterable[Term]) -> str:
    # Every Term.expr comes from SortField.column, never from the request; that
    # is the allowlist, and it is why splicing the ORDER BY text here is safe.
    clause = order_by_clause(terms)
    return f"SELECT id, created_at, priority, due_at, title FROM tasks ORDER BY {clause} LIMIT ?"  # noqa: S608


def query_plan(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> list[str]:
    cursor = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)
    return [row[3] for row in cast("list[tuple[int, int, int, str]]", cursor.fetchall())]


def needs_sort(plan: list[str]) -> bool:
    return any(line.startswith("USE TEMP B-TREE") for line in plan)


def vm_steps(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    """Count SQLite VM instructions for one statement (progress handler, N = 1)."""
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        conn.execute(sql, params).fetchall()
    finally:
        conn.set_progress_handler(None, 1)
    return steps


def fetch_ids(conn: sqlite3.Connection, terms: Iterable[Term], limit: int) -> list[int]:
    rows = cast("list[tuple[int, ...]]", conn.execute(list_sql(terms), (limit,)).fetchall())
    return [r[0] for r in rows]


def _problem(status: int, code: str, detail: str) -> JSONResponse:
    body = {
        "type": f"/problems/{code}",
        "title": "Invalid order_by",
        "status": status,
        "detail": detail,
        "code": code,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def create_app(
    conn: sqlite3.Connection,
    policy: SortPolicy = TASK_POLICY,
    parse: Callable[[str], tuple[SortKey, ...]] = parse_order_by,
) -> Starlette:
    async def list_tasks(request: Request) -> JSONResponse:
        values = request.query_params.getlist("order_by")
        if len(values) > 1:
            return _problem(400, "repeated_parameter", "order_by may appear at most once")
        try:
            ordering = policy.resolve(parse(values[0] if values else ""))
        except SortError as exc:
            return _problem(400, exc.code, exc.detail)
        rows = cast(
            "list[tuple[int, int, int, int | None, str]]",
            conn.execute(list_sql(ordering.terms), (PAGE_SIZE,)).fetchall(),
        )
        items = [
            {"id": r[0], "created_at": r[1], "priority": r[2], "due_at": r[3], "title": r[4]}
            for r in rows
        ]
        return JSONResponse({"order_by": ordering.api(), "items": items})

    return Starlette(routes=[Route("/tasks", list_tasks)])
