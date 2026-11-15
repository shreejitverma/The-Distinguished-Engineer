"""FastAPI 0.162.0 response pipeline: serialize_response() paths, validation counts, and cost.

The apps below exercise every branch of fastapi/routing.py get_request_handler() that turns an
endpoint return value into bytes:

- Rust fast path: a response field exists and no response_class was set, so serialize_response()
  calls TypeAdapter.validate_python(from_attributes=True) and then TypeAdapter.dump_json().
- Explicit response_class: validate, then TypeAdapter.dump_python(mode="json"), then the response
  class render() (json.dumps(allow_nan=False) for JSONResponse).
- No response model: jsonable_encoder() walks the object, then JSONResponse.render().
- A returned Response instance: the whole pipeline is skipped.
"""

import asyncio
import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

from fastapi import FastAPI, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)
from starlette.types import Message, Scope

# ---------------------------------------------------------------------------
# 1. Counting how often the pipeline validates and serializes
# ---------------------------------------------------------------------------


@dataclass
class PipelineProbe:
    """Counts field validations, after-model validations, and serializations, with threads."""

    field_validations: int = 0
    model_validations: int = 0
    serializations: int = 0
    validator_threads: list[int] = field(default_factory=list[int])
    serializer_threads: list[int] = field(default_factory=list[int])

    def counts(self) -> tuple[int, int, int]:
        return (self.field_validations, self.model_validations, self.serializations)

    def reset(self) -> None:
        self.field_validations = 0
        self.model_validations = 0
        self.serializations = 0
        self.validator_threads.clear()
        self.serializer_threads.clear()


PROBE = PipelineProbe()


class Item(BaseModel):
    """A response model whose validators and serializer report every call to PROBE."""

    id: int
    name: str
    price: float

    @field_validator("price")
    @classmethod
    def _count_field_validation(cls, value: float) -> float:
        PROBE.field_validations += 1
        PROBE.validator_threads.append(threading.get_ident())
        return value

    @model_validator(mode="after")
    def _count_model_validation(self) -> Self:
        PROBE.model_validations += 1
        return self

    @model_serializer(mode="wrap")
    def _count_serialization(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        PROBE.serializations += 1
        PROBE.serializer_threads.append(threading.get_ident())
        dumped: dict[str, Any] = handler(self)
        return dumped


class RevalidatingItem(Item):
    """Same fields, but Pydantic re-runs validation on instances it is handed."""

    model_config = ConfigDict(revalidate_instances="always")


ITEM_PAYLOAD: dict[str, Any] = {"id": 7, "name": "widget", "price": 9.5}


def build_counting_app() -> FastAPI:
    """One route per pipeline branch, all returning the same logical item."""
    app = FastAPI()

    @app.get("/dict", response_model=Item)
    async def from_dict() -> dict[str, Any]:
        return dict(ITEM_PAYLOAD)

    @app.get("/sync-dict", response_model=Item)
    def from_dict_sync() -> dict[str, Any]:
        return dict(ITEM_PAYLOAD)

    @app.get("/model")
    async def from_model() -> Item:
        return Item(**ITEM_PAYLOAD)

    @app.get("/revalidate")
    async def from_revalidating_model() -> RevalidatingItem:
        return RevalidatingItem(**ITEM_PAYLOAD)

    @app.get("/no-model", response_model=None)
    async def without_response_model() -> Item:
        return Item(**ITEM_PAYLOAD)

    @app.get("/prebuilt")
    async def prebuilt() -> Response:
        body = Item(**ITEM_PAYLOAD).model_dump_json()
        return Response(content=body, media_type="application/json")

    return app


# ---------------------------------------------------------------------------
# 2. Wire-format differences between the branches
# ---------------------------------------------------------------------------


class Reading(BaseModel):
    sensor: str
    value: float


class Money(BaseModel):
    amount: Decimal
    at: datetime


NAN_READING = Reading(sensor="t1", value=math.nan)
MONEY_RAW: dict[str, Any] = {
    "amount": Decimal("12345678901234567.89"),
    "at": datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
}


def build_wire_app(*, default_response_class: type[Response] | None = None) -> FastAPI:
    """Routes whose bytes differ only because a different serializer ran."""
    app = (
        FastAPI()
        if default_response_class is None
        else FastAPI(default_response_class=default_response_class)
    )

    @app.get("/reading")
    async def reading() -> Reading:
        return NAN_READING

    @app.get("/reading-explicit-json", response_class=JSONResponse)
    async def reading_explicit_json() -> Reading:
        return NAN_READING

    @app.get("/money-model")
    async def money_model() -> Money:
        return Money.model_validate(MONEY_RAW)

    @app.get("/money-any")
    async def money_any() -> Any:
        return dict(MONEY_RAW)

    @app.get("/money-no-model")
    async def money_no_model():  # type: ignore[no-untyped-def]
        return dict(MONEY_RAW)

    return app


# ---------------------------------------------------------------------------
# 3. What response filtering does and does not protect
# ---------------------------------------------------------------------------


class UserOut(BaseModel):
    id: int
    email: str


class UserInDB(UserOut):
    hashed_password: str


class PolyUserOut(BaseModel):
    model_config = ConfigDict(polymorphic_serialization=True)

    id: int
    email: str


class PolyUserInDB(PolyUserOut):
    hashed_password: str


SECRET = "argon2-EXAMPLE-NOT-A-REAL-HASH"  # noqa: S105 - placeholder, not a credential


class UserRow:
    """An ORM-shaped object: plain attributes plus SQLAlchemy-style private state."""

    def __init__(self) -> None:
        self.id = 1
        self.email = "ada@example.test"
        self.hashed_password = SECRET
        self._sa_instance_state = object()


def build_exposure_app() -> FastAPI:
    app = FastAPI()

    @app.get("/subclass")
    async def subclass() -> UserOut:
        return UserInDB(id=1, email="ada@example.test", hashed_password=SECRET)

    @app.get("/orm", response_model=UserOut)
    async def orm() -> object:
        return UserRow()

    @app.get("/orm-no-model", response_model=None)
    async def orm_no_model() -> object:
        return UserRow()

    @app.get("/bypass")
    async def bypass() -> Response:
        user = UserInDB(id=1, email="ada@example.test", hashed_password=SECRET)
        return Response(content=user.model_dump_json(), media_type="application/json")

    @app.get("/polymorphic")
    async def polymorphic() -> PolyUserOut:
        return PolyUserInDB(id=1, email="ada@example.test", hashed_password=SECRET)

    @app.get("/broken", response_model=UserOut)
    async def broken() -> dict[str, Any]:
        return {"id": 1, "hashed_password": SECRET}

    return app


# ---------------------------------------------------------------------------
# 4. Cost of each branch, end to end through the ASGI app
# ---------------------------------------------------------------------------


class BenchItem(BaseModel):
    id: int
    name: str
    price: float
    tags: list[str]
    created: datetime


BENCH_ADAPTER = TypeAdapter(list[BenchItem])


def make_rows(count: int) -> list[dict[str, Any]]:
    """Deterministic rows; naive datetimes so every encoder prints them identically."""
    return [
        {
            "id": i,
            "name": f"item-{i}",
            "price": i + 0.25,
            "tags": ["a", "b", "c"],
            "created": datetime(2026, 1, 1, 0, 0, i % 60),
        }
        for i in range(count)
    ]


BENCH_PATHS: tuple[str, ...] = (
    "/prebuilt-response",
    "/fast-from-models",
    "/fast-from-dicts",
    "/double-validation",
    "/explicit-json-response",
    "/no-model-dicts",
    "/no-model-models",
)


def build_benchmark_app(rows: list[dict[str, Any]]) -> FastAPI:
    models = BENCH_ADAPTER.validate_python(rows)
    app = FastAPI()

    @app.get("/prebuilt-response")
    async def prebuilt_response() -> Response:
        return Response(content=BENCH_ADAPTER.dump_json(models), media_type="application/json")

    @app.get("/fast-from-models")
    async def fast_from_models() -> list[BenchItem]:
        return models

    @app.get("/fast-from-dicts", response_model=list[BenchItem])
    async def fast_from_dicts() -> list[dict[str, Any]]:
        return rows

    @app.get("/double-validation")
    async def double_validation() -> list[BenchItem]:
        return [BenchItem.model_validate(row) for row in rows]

    @app.get("/explicit-json-response", response_class=JSONResponse, response_model=list[BenchItem])
    async def explicit_json_response() -> list[dict[str, Any]]:
        return rows

    @app.get("/no-model-dicts", response_model=None)
    async def no_model_dicts() -> list[dict[str, Any]]:
        return rows

    @app.get("/no-model-models", response_model=None)
    async def no_model_models() -> list[BenchItem]:
        return models

    return app


async def call_asgi(app: FastAPI, path: str) -> tuple[int, bytes]:
    """Drive one GET through the ASGI app with no client library or socket in the way."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"bench")],
        "server": ("bench", 80),
        "client": ("127.0.0.1", 50000),
    }
    status = 0
    chunks: list[bytes] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
        elif message["type"] == "http.response.body":
            chunks.append(bytes(message.get("body", b"")))

    await app(scope, receive, send)
    return status, b"".join(chunks)


@dataclass(frozen=True)
class BenchResult:
    path: str
    median_us: float
    ratio_to_prebuilt: float
    body_bytes: int


async def run_asgi_benchmark(
    *, item_count: int, requests_per_round: int, rounds: int
) -> list[BenchResult]:
    """Median microseconds per request for each branch, rounds interleaved to share drift.

    Raises RuntimeError if any branch returns a non-200 status or different bytes, so a
    timing is only reported for branches that produce the identical wire payload.
    """
    app = build_benchmark_app(make_rows(item_count))
    reference: bytes | None = None
    for path in BENCH_PATHS:
        status, body = await call_asgi(app, path)
        if status != 200:
            raise RuntimeError(f"{path} returned {status}")
        if reference is None:
            reference = body
        elif body != reference:
            raise RuntimeError(f"{path} body differs from {BENCH_PATHS[0]}")
    if reference is None:
        raise RuntimeError("no benchmark paths")

    samples: dict[str, list[float]] = {path: [] for path in BENCH_PATHS}
    for _ in range(rounds):
        for path in BENCH_PATHS:
            start = time.perf_counter_ns()
            for _ in range(requests_per_round):
                await call_asgi(app, path)
            elapsed = time.perf_counter_ns() - start
            samples[path].append(elapsed / requests_per_round / 1000.0)

    medians = {path: statistics.median(values) for path, values in samples.items()}
    base = medians[BENCH_PATHS[0]]
    return [
        BenchResult(path, medians[path], medians[path] / base, len(reference))
        for path in BENCH_PATHS
    ]


def run_serializer_microbench(*, item_count: int, loops: int, rounds: int) -> dict[str, float]:
    """Median microseconds per call for the serializer work alone, outside FastAPI."""
    import json

    rows = make_rows(item_count)
    models = BENCH_ADAPTER.validate_python(rows)

    def dump_json_only() -> bytes:
        return BENCH_ADAPTER.dump_json(models)

    def validate_then_dump_json() -> bytes:
        return BENCH_ADAPTER.dump_json(BENCH_ADAPTER.validate_python(rows, from_attributes=True))

    def validate_dump_python_json_dumps() -> bytes:
        value = BENCH_ADAPTER.validate_python(rows, from_attributes=True)
        data = BENCH_ADAPTER.dump_python(value, mode="json")
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()

    def jsonable_encoder_json_dumps() -> bytes:
        data = jsonable_encoder(rows)
        return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()

    candidates = {
        "dump_json(models)": dump_json_only,
        "validate_python + dump_json": validate_then_dump_json,
        "validate_python + dump_python(json) + json.dumps": validate_dump_python_json_dumps,
        "jsonable_encoder + json.dumps": jsonable_encoder_json_dumps,
    }
    reference = dump_json_only()
    for name, fn in candidates.items():
        if fn() != reference:
            raise RuntimeError(f"{name} produced different bytes")
    out: dict[str, float] = {}
    for name, fn in candidates.items():
        per_round: list[float] = []
        for _ in range(rounds):
            start = time.perf_counter_ns()
            for _ in range(loops):
                fn()
            per_round.append((time.perf_counter_ns() - start) / loops / 1000.0)
        out[name] = statistics.median(per_round)
    return out


def main() -> None:
    import platform
    import sys

    print(f"python {sys.version.split()[0]} {platform.system()} {platform.machine()}")
    for count in (1, 100, 1000):
        results = asyncio.run(
            run_asgi_benchmark(item_count=count, requests_per_round=200, rounds=7)
        )
        print(f"asgi items={count} body={results[0].body_bytes}B")
        for r in results:
            print(f"  {r.path:<24} {r.median_us:10.1f} us  x{r.ratio_to_prebuilt:.2f}")
    for count in (100, 1000):
        micro = run_serializer_microbench(item_count=count, loops=200, rounds=7)
        print(f"serializer-only items={count}")
        for name, us in micro.items():
            print(f"  {name:<50} {us:10.1f} us")


if __name__ == "__main__":
    main()
