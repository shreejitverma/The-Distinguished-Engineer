"""SnowflakeId: a custom type validated in pydantic-core except for one constructor call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic_core import PydanticCustomError, core_schema

if TYPE_CHECKING:
    from pydantic.json_schema import JsonSchemaValue

# Lab-defined 63-bit layout: 41-bit millisecond timestamp | 10-bit worker | 12-bit sequence.
TIMESTAMP_BITS: Final = 41
WORKER_BITS: Final = 10
SEQUENCE_BITS: Final = 12
MAX_SNOWFLAKE: Final = (1 << (TIMESTAMP_BITS + WORKER_BITS + SEQUENCE_BITS)) - 1
JSON_SAFE_INTEGER_MAX: Final = (1 << 53) - 1
# Canonical decimal: no sign, no leading zeros, at most 19 digits (len(str(2**63 - 1)) == 19).
CANONICAL_DECIMAL: Final = r"^(0|[1-9][0-9]{0,18})$"


class SnowflakeId:
    """Immutable 63-bit identifier that crosses JSON as a decimal string, never as a number."""

    __slots__ = ("_value",)
    _value: int

    def __init__(self, value: int) -> None:
        if type(value) is not int or not 0 <= value <= MAX_SNOWFLAKE:
            msg = f"SnowflakeId needs an int in [0, {MAX_SNOWFLAKE}], got {value!r}"
            raise ValueError(msg)
        object.__setattr__(self, "_value", value)

    @classmethod
    def compose(cls, timestamp_ms: int, worker: int, sequence: int) -> SnowflakeId:
        """Pack the three fields; each must fit its bit width."""
        if not 0 <= timestamp_ms < (1 << TIMESTAMP_BITS):
            raise ValueError("timestamp_ms out of range")
        if not 0 <= worker < (1 << WORKER_BITS):
            raise ValueError("worker out of range")
        if not 0 <= sequence < (1 << SEQUENCE_BITS):
            raise ValueError("sequence out of range")
        packed = (timestamp_ms << (WORKER_BITS + SEQUENCE_BITS)) | (worker << SEQUENCE_BITS)
        return cls(packed | sequence)

    @property
    def timestamp_ms(self) -> int:
        return self._value >> (WORKER_BITS + SEQUENCE_BITS)

    @property
    def worker(self) -> int:
        return (self._value >> SEQUENCE_BITS) & ((1 << WORKER_BITS) - 1)

    @property
    def sequence(self) -> int:
        return self._value & ((1 << SEQUENCE_BITS) - 1)

    def __int__(self) -> int:
        return self._value

    def __str__(self) -> str:
        return str(self._value)

    def __repr__(self) -> str:
        return f"SnowflakeId({self._value})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SnowflakeId) and other._value == self._value

    def __lt__(self, other: SnowflakeId) -> bool:
        return self._value < other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SnowflakeId is immutable")

    @classmethod
    def _from_valid_int(cls, value: int) -> SnowflakeId:
        # pydantic-core already enforced strict int and 0 <= value <= MAX_SNOWFLAKE.
        instance = object.__new__(cls)
        object.__setattr__(instance, "_value", value)
        return instance

    @classmethod
    def _from_canonical_decimal(cls, text: str) -> SnowflakeId:
        # pydantic-core already enforced the canonical-decimal pattern; only the
        # 19-digit range [2**63, 10**19 - 1] can still be out of bounds here.
        value = int(text)
        if value > MAX_SNOWFLAKE:
            raise PydanticCustomError(
                "snowflake_out_of_range",
                "SnowflakeId must be <= {max_value}",
                {"max_value": MAX_SNOWFLAKE},
            )
        return cls._from_valid_int(value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        from_decimal = core_schema.no_info_after_validator_function(
            cls._from_canonical_decimal,
            core_schema.str_schema(pattern=CANONICAL_DECIMAL, max_length=19, strict=True),
        )
        from_int = core_schema.no_info_after_validator_function(
            cls._from_valid_int,
            core_schema.int_schema(ge=0, le=MAX_SNOWFLAKE, strict=True),
        )
        python_side = core_schema.union_schema(
            [core_schema.is_instance_schema(cls), from_int, from_decimal],
            mode="left_to_right",
            custom_error_type="snowflake_id_type",
            custom_error_message=(
                "Input should be a SnowflakeId, an int, or a canonical decimal string"
            ),
        )
        return core_schema.json_or_python_schema(
            json_schema=from_decimal,
            python_schema=python_side,
            serialization=core_schema.to_string_ser_schema(when_used="json-unless-none"),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler.resolve_ref_schema(handler(schema))
        json_schema["format"] = "snowflake-id"
        json_schema["description"] = "63-bit id as a decimal string (exceeds 2**53 - 1)"
        json_schema["examples"] = [str(MAX_SNOWFLAKE)]
        return json_schema


@dataclass(frozen=True, slots=True)
class MintedAfter:
    """Annotated marker: wraps the inner schema and rejects ids minted before a cutoff."""

    cutoff_ms: int

    def __get_pydantic_core_schema__(
        self, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(self._check, handler(source_type))

    def __get_pydantic_json_schema__(
        self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        json_schema["x-minted-after-ms"] = self.cutoff_ms
        return json_schema

    def _check(self, value: SnowflakeId) -> SnowflakeId:
        if value.timestamp_ms < self.cutoff_ms:
            raise PydanticCustomError(
                "snowflake_too_old",
                "SnowflakeId minted at {minted_ms} ms, before {cutoff_ms} ms",
                {"minted_ms": value.timestamp_ms, "cutoff_ms": self.cutoff_ms},
            )
        return value


class PlainSnowflakeId:
    """Anti-pattern: one Python plain validator owns every input; pydantic-core does nothing."""

    __slots__ = ("value",)

    def __init__(self, value: int) -> None:
        self.value = value

    @classmethod
    def _coerce(cls, raw: Any) -> PlainSnowflakeId:
        if isinstance(raw, cls):
            return raw
        return cls(int(raw))

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(cls._coerce)


RECENT_CUTOFF_MS: Final = 1_000_000


class Event(BaseModel):
    """Wire model: SnowflakeId fields validate natively from JSON strings and Python instances."""

    model_config = ConfigDict(frozen=True)

    id: SnowflakeId
    parent_id: SnowflakeId | None = None
    replay_of: Annotated[SnowflakeId, MintedAfter(RECENT_CUTOFF_MS)] | None = None
    shard_counts: dict[SnowflakeId, int] = Field(default_factory=dict[SnowflakeId, int])


def build_event_app() -> FastAPI:
    """Path parameters arrive as Python str, so they exercise the python_schema branch."""
    app = FastAPI()

    @app.get("/v1/events/{event_id}")
    async def get_event(event_id: SnowflakeId) -> Event:
        return Event(id=event_id, parent_id=SnowflakeId(int(event_id) - 1))

    return app
