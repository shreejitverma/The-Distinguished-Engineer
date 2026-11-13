"""Recursive schemas, forward references, model_rebuild(), and cycle detection in pydantic-core."""

from __future__ import annotations

import gc
import json
import sys
from typing import Any

import pytest
from pydantic import (
    PydanticUndefinedAnnotation,
    PydanticUserError,
    ValidationError,
)
from pydantic_core import PydanticSerializationError

from api_labs.f09_pydantic_v2_internals_and_validation.recursive_forward_refs import (
    BINOP_COMPLETE_AT_DEFINITION,
    BINOP_REBUILD_RESULT,
    BinOp,
    Comment,
    CopyingNode,
    GraphNode,
    comment_chain,
    comment_cycle,
    define_alias_holder,
    define_deferred_thread,
    define_shadowed_holder,
    define_without_future_import,
    evaluate,
    left_deep_expr,
    rebuild_concurrently,
    recursion_guard_limit,
)


def error_types(exc: ValidationError) -> set[str]:
    return {str(e["type"]) for e in exc.errors(include_url=False)}


@pytest.mark.unit
def test_self_reference_compiles_to_definitions_and_definition_ref() -> None:
    schema: Any = Comment.__pydantic_core_schema__
    assert schema["type"] == "definitions"
    assert schema["schema"]["type"] == "definition-ref"
    assert [d["type"] for d in schema["definitions"]] == ["model"]
    assert schema["schema"]["schema_ref"] == schema["definitions"][0]["ref"]
    assert repr(Comment.__pydantic_validator__).startswith(
        'SchemaValidator(title="Comment", validator=DefinitionRef('
    )

    tree = Comment.model_validate(
        {
            "id": 1,
            "body": "a",
            "replies": [{"id": 2, "body": "b", "replies": [{"id": 3, "body": "c"}]}],
        }
    )
    assert tree.replies[0].replies[0].id == 3
    assert Comment.model_validate_json(tree.model_dump_json()) == tree


@pytest.mark.unit
def test_pep695_alias_cycle_leaves_model_incomplete_until_rebuild() -> None:
    assert BINOP_COMPLETE_AT_DEFINITION is False
    assert BINOP_REBUILD_RESULT is True
    assert BinOp.__pydantic_complete__ is True
    assert BinOp.model_rebuild() is None

    expr = BinOp.model_validate(
        {
            "op": "*",
            "left": {"kind": "binop", "op": "+", "left": {"value": 1}, "right": {"value": 2}},
            "right": {"kind": "num", "value": 3},
        }
    )
    assert evaluate(expr) == 9


@pytest.mark.unit
def test_unresolved_forward_ref_mocks_and_model_rebuild_return_values() -> None:
    thread_cls, author_cls = define_deferred_thread()
    assert thread_cls.__pydantic_complete__ is False
    assert thread_cls.__pydantic_fields_complete__ is False
    assert type(thread_cls.__dict__["__pydantic_validator__"]).__name__ == "MockValSer"

    with pytest.raises(PydanticUserError) as user_err:
        thread_cls.model_validate({"title": "t", "author": {"handle": "h"}})
    assert user_err.value.code == "class-not-fully-defined"

    assert thread_cls.model_rebuild(raise_errors=False) is False
    with pytest.raises(PydanticUndefinedAnnotation) as undefined:
        thread_cls.model_rebuild()
    assert undefined.value.code == "undefined-annotation"
    assert undefined.value.name == "Author"

    assert thread_cls.model_rebuild(_types_namespace={"Author": author_cls}) is True
    assert thread_cls.model_rebuild() is None
    assert thread_cls.model_rebuild(force=True) is True
    thread: Any = thread_cls.model_validate(
        {"title": "t", "author": {"handle": "h"}, "comments": [{"id": 1, "body": "x"}]}
    )
    assert thread.author.handle == "h"
    assert isinstance(thread.comments[0], Comment)


@pytest.mark.unit
def test_captured_parent_namespace_beats_explicit_types_namespace() -> None:
    holder_cls, later_cls = define_shadowed_holder()
    assert holder_cls.__pydantic_complete__ is False
    assert holder_cls.model_rebuild(_types_namespace={"Target": str, "Later": later_cls}) is True
    assert holder_cls.model_fields["pair"].annotation == tuple[int, later_cls]  # type: ignore[valid-type]


@pytest.mark.unit
def test_weakly_captured_local_alias_dies_with_its_frame() -> None:
    batch_cls = define_alias_holder()
    gc.collect()
    with pytest.raises(PydanticUndefinedAnnotation) as undefined:
        batch_cls.model_rebuild(_types_namespace={"Later": str})
    assert undefined.value.name == "Ids"
    assert batch_cls.model_rebuild(_types_namespace={"Later": str, "Ids": list[int]}) is True


@pytest.mark.unit
def test_python_input_depth_budget_counts_definition_ref_traversals() -> None:
    assert recursion_guard_limit() == 255

    Comment.model_validate(comment_chain(255))
    with pytest.raises(ValidationError) as too_deep:
        Comment.model_validate(comment_chain(256))
    (error,) = too_deep.value.errors(include_url=False)
    assert error["type"] == "recursion_depth_exceeded"
    assert len(error["loc"]) == 2 * 255

    # Each BinOp level crosses two definition-refs (the Expr alias and BinOp itself).
    BinOp.model_validate(left_deep_expr(127))
    with pytest.raises(ValidationError) as ast_too_deep:
        BinOp.model_validate(left_deep_expr(128))
    assert "recursion_depth_exceeded" in error_types(ast_too_deep.value)


@pytest.mark.unit
def test_json_input_has_no_identity_and_is_bounded_by_jiter() -> None:
    Comment.model_validate_json(json.dumps(comment_chain(100)))
    with pytest.raises(ValidationError) as too_deep:
        Comment.model_validate_json(json.dumps(comment_chain(101)))
    (error,) = too_deep.value.errors(include_url=False)
    assert error["type"] == "json_invalid"
    assert "recursion limit exceeded" in error["msg"]


@pytest.mark.unit
def test_cyclic_python_input_raises_recursion_loop_and_dag_is_not_a_cycle() -> None:
    with pytest.raises(ValidationError) as cyclic:
        Comment.model_validate(comment_cycle())
    (error,) = cyclic.value.errors(include_url=False)
    assert error["type"] == "recursion_loop"
    assert error["loc"] == ("replies", 0, "replies", 0, "replies", 0)

    shared: dict[str, Any] = {"id": 9, "body": "shared"}
    dag = Comment.model_validate({"id": 0, "body": "root", "replies": [shared, shared]})
    assert dag.replies[0] == dag.replies[1]
    assert dag.replies[0] is not dag.replies[1]


@pytest.mark.unit
def test_identity_changing_before_validator_turns_cycle_into_depth_error() -> None:
    node: dict[str, Any] = {"id": 1, "children": []}
    node["children"].append(node)
    with pytest.raises(ValidationError) as degraded:
        CopyingNode.model_validate(node)
    (error,) = degraded.value.errors(include_url=False)
    assert error["type"] == "recursion_depth_exceeded"
    assert len(error["loc"]) == 2 * 255


@pytest.mark.unit
def test_wrap_validator_drops_back_edges() -> None:
    graph: dict[str, Any] = {"id": 1, "children": [{"id": 2, "children": [{"id": 3}]}]}
    graph["children"][0]["children"][0]["children"] = [graph]
    tree = GraphNode.model_validate(graph)
    assert tree.model_dump() == {
        "id": 1,
        "children": [{"id": 2, "children": [{"id": 3, "children": []}]}],
    }


@pytest.mark.unit
def test_serializer_guard_rejects_cyclic_and_too_deep_instance_graphs() -> None:
    looped = Comment(id=1, body="self")
    looped.replies.append(looped)
    assert Comment.model_validate(looped) is looped
    with pytest.raises(ValueError, match=r"^Circular reference detected \(id repeated\)$"):
        looped.model_dump()
    with pytest.raises(PydanticSerializationError, match="Circular reference detected"):
        looped.model_dump_json()

    def instance_chain(depth: int) -> Comment:
        root = Comment.model_construct(id=0, body="root", replies=[])
        node = root
        for i in range(1, depth):
            child = Comment.model_construct(id=i, body="r", replies=[])
            node.replies.append(child)
            node = child
        return root

    instance_chain(255).model_dump()
    with pytest.raises(ValueError, match=r"^Maximum serialization depth exceeded$"):
        instance_chain(256).model_dump()


@pytest.mark.unit
def test_concurrent_model_rebuild_completes_exactly_once() -> None:
    thread_cls, author_cls = define_deferred_thread()
    results = rebuild_concurrently(thread_cls, {"Author": author_cls}, workers=8)
    assert sorted(results, key=repr) == [None] * 7 + [True]
    assert thread_cls.__pydantic_complete__ is True


@pytest.mark.unit
def test_unquoted_self_reference_depends_on_pep_649() -> None:
    if sys.version_info >= (3, 14):
        node_cls = define_without_future_import()
        assert node_cls.__pydantic_complete__ is True
        node: Any = node_cls.model_validate({"value": 1, "children": [{"value": 2}]})
        assert node.children[0].value == 2
    else:
        with pytest.raises(NameError, match="'Node' is not defined"):
            define_without_future_import()
