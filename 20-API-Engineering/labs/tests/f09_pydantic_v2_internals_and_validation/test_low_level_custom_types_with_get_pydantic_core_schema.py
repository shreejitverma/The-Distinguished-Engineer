"""Unit tests for SnowflakeId: __get_pydantic_core_schema__ and __get_pydantic_json_schema__."""

from __future__ import annotations

import warnings
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    PydanticDeprecatedSince20,
    PydanticSchemaGenerationError,
    PydanticUserError,
    TypeAdapter,
    ValidationError,
)
from pydantic.errors import PydanticInvalidForJsonSchema
from pydantic_core import PydanticSerializationError, core_schema

from api_labs.f09_pydantic_v2_internals_and_validation.snowflake_custom_type import (
    JSON_SAFE_INTEGER_MAX,
    MAX_SNOWFLAKE,
    Event,
    PlainSnowflakeId,
    SnowflakeId,
    build_event_app,
)

ADAPTER = TypeAdapter(SnowflakeId)


def _identity(value: object) -> object:
    return value


def _error_types(exc: ValidationError) -> list[str]:
    return [err["type"] for err in exc.errors(include_url=False)]


@pytest.mark.unit
def test_bit_layout_exceeds_json_safe_integers_and_instances_are_immutable() -> None:
    sid = SnowflakeId.compose(timestamp_ms=1_700_000_000_000, worker=513, sequence=4095)
    assert (sid.timestamp_ms, sid.worker, sid.sequence) == (1_700_000_000_000, 513, 4095)
    assert MAX_SNOWFLAKE == 2**63 - 1
    assert MAX_SNOWFLAKE > JSON_SAFE_INTEGER_MAX == 2**53 - 1
    # The reason the wire form is a string: a binary64 double cannot hold 2**53 + 1.
    assert float(JSON_SAFE_INTEGER_MAX + 2) == float(JSON_SAFE_INTEGER_MAX + 1)
    slot_name = "_value"
    with pytest.raises(AttributeError, match="immutable"):
        setattr(sid, slot_name, 0)
    with pytest.raises(ValueError, match="needs an int"):
        SnowflakeId(True)


@pytest.mark.unit
def test_json_branch_accepts_only_canonical_decimal_strings() -> None:
    assert ADAPTER.validate_json('"0"') == SnowflakeId(0)
    assert int(ADAPTER.validate_json(f'"{MAX_SNOWFLAKE}"')) == MAX_SNOWFLAKE

    cases = {
        f'"{MAX_SNOWFLAKE + 1}"': "snowflake_out_of_range",
        '"01"': "string_pattern_mismatch",
        '"-1"': "string_pattern_mismatch",
        "1": "string_type",
        '"99999999999999999999"': "string_too_long",
    }
    for raw, expected in cases.items():
        with pytest.raises(ValidationError) as err_info:
            ADAPTER.validate_json(raw)
        assert _error_types(err_info.value) == [expected], raw

    with pytest.raises(ValidationError) as err_info:
        ADAPTER.validate_json(f'"{MAX_SNOWFLAKE + 1}"')
    ctx = err_info.value.errors(include_url=False)[0].get("ctx")
    assert ctx == {"max_value": MAX_SNOWFLAKE}


@pytest.mark.unit
def test_python_branch_keeps_instance_identity_and_collapses_union_errors() -> None:
    sid = SnowflakeId(3)
    assert ADAPTER.validate_python(sid) is sid
    assert ADAPTER.validate_python(5) == SnowflakeId(5)
    assert ADAPTER.validate_python("6") == SnowflakeId(6)
    # validate_strings is not InputType::Python, so it takes the json branch.
    assert ADAPTER.validate_strings("42") == SnowflakeId(42)

    for raw in (True, 1.0, -1, None, "007", str(MAX_SNOWFLAKE + 1)):
        with pytest.raises(ValidationError) as err_info:
            ADAPTER.validate_python(raw)
        assert _error_types(err_info.value) == ["snowflake_id_type"], raw

    assert repr(ADAPTER.validator).startswith(
        'SchemaValidator(title="json-or-python[json=function-after[_from_canonical_decimal(), '
        "constrained-str],python=union[is-instance[SnowflakeId],"
    )


@pytest.mark.unit
def test_hook_runs_once_per_occurrence_at_build_time_never_per_validation() -> None:
    class CountingId(SnowflakeId):
        __slots__ = ()
        calls: ClassVar[list[Any]] = []

        @classmethod
        def __get_pydantic_core_schema__(
            cls, source_type: Any, handler: GetCoreSchemaHandler
        ) -> core_schema.CoreSchema:
            cls.calls.append(source_type)
            return super().__get_pydantic_core_schema__(source_type, handler)

    class Pair(BaseModel):
        left: CountingId
        right: CountingId
        by_key: dict[CountingId, int]

    assert CountingId.calls == [CountingId, CountingId, CountingId]
    payload = '{"left":"1","right":"2","by_key":{"3":4}}'
    pair = Pair.model_validate_json(payload)
    for _ in range(100):
        Pair.model_validate_json(payload)
    assert len(CountingId.calls) == 3
    # The inherited hook built is-instance[CountingId] and constructed the subclass.
    assert type(pair.left) is CountingId


@pytest.mark.unit
def test_serializer_emits_strings_in_json_mode_and_instances_in_python_mode() -> None:
    event = Event.model_validate_json(
        '{"id":"175928847299117063","shard_counts":{"7":2},"replay_of":"9223372036854775807"}'
    )
    assert event.model_dump() == {
        "id": SnowflakeId(175928847299117063),
        "parent_id": None,
        "replay_of": SnowflakeId(MAX_SNOWFLAKE),
        "shard_counts": {SnowflakeId(7): 2},
    }
    assert event.model_dump_json() == (
        '{"id":"175928847299117063","parent_id":null,'
        '"replay_of":"9223372036854775807","shard_counts":{"7":2}}'
    )
    assert event.model_dump(mode="json")["shard_counts"] == {"7": 2}
    assert Event.model_validate_json(event.model_dump_json()) == event


@pytest.mark.unit
def test_annotated_marker_wraps_the_type_schema_and_keeps_its_serializer() -> None:
    with pytest.raises(ValidationError) as err_info:
        Event.model_validate({"id": 1, "replay_of": 5})
    errors = err_info.value.errors(include_url=False)
    assert [(e["type"], e["loc"]) for e in errors] == [("snowflake_too_old", ("replay_of",))]
    assert errors[0].get("ctx") == {"minted_ms": 0, "cutoff_ms": 1_000_000}

    recent = SnowflakeId.compose(timestamp_ms=2_000_000, worker=0, sequence=0)
    event = Event.model_validate({"id": 1, "replay_of": recent})
    assert event.replay_of is recent
    assert '"replay_of":"8388608000000"' in event.model_dump_json()


@pytest.mark.unit
def test_json_schema_hook_sees_mode_specific_inner_schema() -> None:
    validation = Event.model_json_schema(mode="validation")["properties"]
    serialization = Event.model_json_schema(mode="serialization")["properties"]

    assert validation["id"] == {
        "description": "63-bit id as a decimal string (exceeds 2**53 - 1)",
        "examples": [str(MAX_SNOWFLAKE)],
        "format": "snowflake-id",
        "maxLength": 19,
        "pattern": "^(0|[1-9][0-9]{0,18})$",
        "title": "Id",
        "type": "string",
    }
    # Serialization mode reads the to-string ser schema: a bare string, no input constraints.
    assert serialization["id"] == {
        "description": "63-bit id as a decimal string (exceeds 2**53 - 1)",
        "examples": [str(MAX_SNOWFLAKE)],
        "format": "snowflake-id",
        "title": "Id",
        "type": "string",
    }
    assert validation["replay_of"]["anyOf"][0]["x-minted-after-ms"] == 1_000_000
    assert validation["shard_counts"]["patternProperties"] == {
        "^(0|[1-9][0-9]{0,18})$": {"type": "integer"}
    }


@pytest.mark.unit
def test_plain_validator_anti_pattern_loses_json_schema_strictness_and_serialization() -> None:
    adapter = TypeAdapter(PlainSnowflakeId)
    with pytest.raises(PydanticInvalidForJsonSchema, match="PlainValidatorFunctionSchema"):
        adapter.json_schema()

    strict_adapter = TypeAdapter(PlainSnowflakeId, config=ConfigDict(strict=True))
    assert strict_adapter.validate_python("5").value == 5

    with pytest.raises(ValidationError) as err_info:
        adapter.validate_python("abc")
    assert _error_types(err_info.value) == ["value_error"]
    # TypeError is not converted to a ValidationError: it escapes as a 500-class bug.
    with pytest.raises(TypeError):
        adapter.validate_python([1])

    with pytest.raises(PydanticSerializationError, match="Unable to serialize unknown type"):
        adapter.dump_json(PlainSnowflakeId(1))


@pytest.mark.unit
def test_handler_misuse_fails_at_schema_build_time() -> None:
    class CallsHandlerOnItself:
        @classmethod
        def __get_pydantic_core_schema__(
            cls, source_type: Any, handler: GetCoreSchemaHandler
        ) -> core_schema.CoreSchema:
            return handler(cls)

    with pytest.raises(PydanticSchemaGenerationError, match=r"handler\.generate_schema"):
        TypeAdapter(CallsHandlerOnItself)

    class RegeneratesItself:
        @classmethod
        def __get_pydantic_core_schema__(
            cls, source_type: Any, handler: GetCoreSchemaHandler
        ) -> core_schema.CoreSchema:
            return handler.generate_schema(cls)

    with pytest.raises(RecursionError):
        TypeAdapter(RegeneratesItself)


@pytest.mark.unit
def test_v1_hooks_warn_or_fail() -> None:
    class V1Validators:
        @classmethod
        def __get_validators__(cls) -> Any:
            yield _identity

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        TypeAdapter(V1Validators)
    assert [w.category for w in caught] == [PydanticDeprecatedSince20]

    class V1ModifySchema:
        @classmethod
        def __get_pydantic_core_schema__(
            cls, source_type: Any, handler: GetCoreSchemaHandler
        ) -> core_schema.CoreSchema:
            return core_schema.int_schema()

        @classmethod
        def __modify_schema__(cls, field_schema: dict[str, Any]) -> None:
            field_schema["format"] = "legacy"

    with pytest.raises(PydanticUserError) as err_info:
        TypeAdapter(V1ModifySchema)
    assert err_info.value.code == "custom-json-schema"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fastapi_path_parameter_uses_python_branch_and_openapi_hook() -> None:
    app = build_event_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get(f"/v1/events/{MAX_SNOWFLAKE}")
        assert ok.status_code == 200
        assert ok.json() == {
            "id": "9223372036854775807",
            "parent_id": "9223372036854775806",
            "replay_of": None,
            "shard_counts": {},
        }

        bad = await client.get("/v1/events/007")
        assert bad.status_code == 422
        detail = bad.json()["detail"]
        assert [(d["type"], d["loc"], d["input"]) for d in detail] == [
            ("snowflake_id_type", ["path", "event_id"], "007")
        ]

        openapi = (await client.get("/openapi.json")).json()
    param = openapi["paths"]["/v1/events/{event_id}"]["get"]["parameters"][0]
    assert param["schema"]["format"] == "snowflake-id"
    assert param["schema"]["pattern"] == "^(0|[1-9][0-9]{0,18})$"
