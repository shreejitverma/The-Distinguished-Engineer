"""Recursive schemas, forward references, model_rebuild(), and pydantic-core cycle guards."""

from __future__ import annotations

import importlib
import threading
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)


class Comment(BaseModel):
    """Self-referential tree node: `replies` names the class that is still being created."""

    id: int
    body: str
    replies: list[Comment] = []


class Num(BaseModel):
    """AST leaf."""

    kind: Literal["num"] = "num"
    value: int


type Expr = Num | BinOp


class BinOp(BaseModel):
    """AST branch that recurses through the PEP 695 alias `Expr`."""

    kind: Literal["binop"] = "binop"
    op: Literal["+", "*"]
    left: Expr
    right: Expr


# `Expr.__value__` raises NameError while `BinOp` is being created (the name is not bound yet),
# so the class is left incomplete with mock validators until the explicit rebuild below.
BINOP_COMPLETE_AT_DEFINITION: bool = BinOp.__pydantic_complete__
BINOP_REBUILD_RESULT: bool | None = BinOp.model_rebuild()


def evaluate(expr: Expr) -> int:
    """Fold a validated AST."""
    if isinstance(expr, Num):
        return expr.value
    left, right = evaluate(expr.left), evaluate(expr.right)
    return left + right if expr.op == "+" else left * right


def _is_single_recursion_loop(exc: ValidationError) -> bool:
    errors = exc.errors(include_url=False)
    return len(errors) == 1 and errors[0]["type"] == "recursion_loop"


class GraphNode(BaseModel):
    """Accepts cyclic input by dropping the back edge that closes each cycle."""

    id: int
    children: list[GraphNode] = []

    @field_validator("children", mode="wrap")
    @classmethod
    def drop_back_edges(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        try:
            return handler(value)
        except ValidationError as exc:
            if not (_is_single_recursion_loop(exc) and isinstance(value, list)):
                raise
        items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        kept: list[Any] = []
        for child in items:
            try:
                kept.extend(handler([child]))
            except ValidationError as exc:
                if not _is_single_recursion_loop(exc):
                    raise
        return kept


class CopyingNode(BaseModel):
    """A before-validator that copies children gives every level a new id()."""

    id: int
    children: list[CopyingNode] = []

    @model_validator(mode="before")
    @classmethod
    def copy_children(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = dict(cast("dict[str, Any]", data))
        children = cast("list[Any]", fields.get("children", []))
        fields["children"] = [
            dict(cast("dict[str, Any]", c)) if isinstance(c, dict) else c for c in children
        ]
        return fields


def recursion_guard_limit() -> int:
    """Read the compile-time RECURSION_GUARD_LIMIT that pydantic-core exports."""
    core = importlib.import_module("pydantic_core._pydantic_core")
    return int(vars(core)["_recursion_limit"])


def comment_chain(depth: int) -> dict[str, Any]:
    """Build `depth` nested comment dicts, each the single reply of its parent."""
    root: dict[str, Any] = {"id": 0, "body": "root", "replies": []}
    node = root
    for i in range(1, depth):
        child: dict[str, Any] = {"id": i, "body": f"r{i}", "replies": []}
        node["replies"].append(child)
        node = child
    return root


def comment_cycle() -> dict[str, Any]:
    """Build the cyclic dict graph 1 -> 2 -> 3 -> 1."""
    first: dict[str, Any] = {"id": 1, "body": "a", "replies": []}
    second: dict[str, Any] = {"id": 2, "body": "b", "replies": []}
    third: dict[str, Any] = {"id": 3, "body": "c", "replies": [first]}
    first["replies"].append(second)
    second["replies"].append(third)
    return first


def left_deep_expr(depth: int) -> dict[str, Any]:
    """Build ((1 + 1) + 1) ... with `depth` BinOp levels."""
    expr: dict[str, Any] = {"kind": "num", "value": 1}
    for _ in range(depth):
        expr = {"kind": "binop", "op": "+", "left": expr, "right": {"kind": "num", "value": 1}}
    return expr


def define_deferred_thread() -> tuple[type[BaseModel], type[BaseModel]]:
    """Return (Thread, Author); Thread names Author before Author exists."""

    class Thread(BaseModel):
        title: str
        author: Author
        comments: list[Comment] = []

    class Author(BaseModel):
        handle: str

    return Thread, Author


def define_shadowed_holder() -> tuple[type[BaseModel], type[BaseModel]]:
    """Return (Holder, Later); `Target` is captured in Holder's parent namespace as int."""
    Target = int

    class Holder(BaseModel):
        pair: tuple[Target, Later]  # type: ignore[valid-type]  # pyright: ignore[reportInvalidTypeForm]

    class Later(BaseModel):
        note: str

    return Holder, Later


def define_alias_holder() -> type[BaseModel]:
    """Return Batch; its parent namespace holds only a weak reference to the local alias."""
    Ids = list[int]

    class Batch(BaseModel):
        ids: Ids | Later  # pyright: ignore[reportInvalidTypeForm]

    class Later(BaseModel):
        note: str

    return Batch


def rebuild_concurrently(
    model: type[BaseModel], namespace: dict[str, Any], workers: int
) -> list[bool | None]:
    """Call model_rebuild() from `workers` threads released together by a barrier."""
    barrier = threading.Barrier(workers)
    results: list[bool | None] = []
    results_lock = threading.Lock()

    def run() -> None:
        barrier.wait()
        outcome = model.model_rebuild(_types_namespace=namespace)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


UNQUOTED_SELF_REFERENCE = (
    "from pydantic import BaseModel\n"
    "class Node(BaseModel):\n"
    "    value: int\n"
    "    children: list[Node] = []\n"
)


def define_without_future_import() -> type[BaseModel]:
    """Execute a self-referencing model compiled without `from __future__ import annotations`."""
    namespace: dict[str, Any] = {"__name__": "pep649_lab"}
    code = compile(UNQUOTED_SELF_REFERENCE, "<pep649-lab>", "exec", dont_inherit=True)
    exec(code, namespace)  # noqa: S102 - fixed, module-local source
    node_cls: type[BaseModel] = namespace["Node"]
    return node_cls
