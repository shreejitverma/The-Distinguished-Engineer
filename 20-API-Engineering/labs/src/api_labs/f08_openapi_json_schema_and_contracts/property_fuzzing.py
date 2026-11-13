"""Property-based API fuzzing: Schemathesis 4.33.1 and Hypothesis 6.170.14 against a FastAPI app.

`build_app(fixed=False)` serves four operations, each with one edge-input bug that a hand-written
happy-path test never reaches; `build_app(fixed=True)` is the same contract with the bugs removed.
`ServerErrorBoundary` turns an escaped exception into a `500`, the way an ASGI server does, so the
in-process run sees the same status a deployed service returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, cast

import schemathesis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from hypothesis import find, settings
from pydantic import BaseModel, Field, field_validator
from schemathesis.engine import events, from_schema

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from hypothesis.strategies import SearchStrategy
    from schemathesis import APIOperation, Case
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_KEYS = frozenset({"tenant-a:EXAMPLE-NOT-A-REAL-KEY"})
type FuzzCase = Case[APIOperation[Any, Any, Any, Any]]
_SURROGATES = range(0xD800, 0xE000)
# Built with chr() so no "\x00" literal reaches the constant pools Schemathesis and Hypothesis
# harvest from this module's source; the fuzzer has to find NUL on its own.
NUL = chr(0)
# Module level: FastAPI resolves postponed (string) annotations against module globals.
_lenient_key = APIKeyHeader(name="X-API-Key", auto_error=False)
_strict_key = APIKeyHeader(name="X-API-Key", auto_error=True)
LenientKey = Annotated[str | None, Depends(_lenient_key)]
StrictKey = Annotated[str, Depends(_strict_key)]


class ServerErrorBoundary:
    """Contain an exception that escapes the app the way Uvicorn's `run_asgi` does.

    Starlette's `ServerErrorMiddleware` sends its own `500` and then re-raises, so without this
    wrapper an in-process client sees the exception instead of a response. Like the server, the
    boundary logs the exception (here: appends it to `errors`), keeps a response that has already
    started, and sends a plain `500` only when nothing was sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.errors: list[Exception] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            self.errors.append(exc)
            if started:
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"Internal Server Error"})


class TextColumn:
    """A PostgreSQL `text` column: the server refuses the NUL character (SQLSTATE `22021`)."""

    def __init__(self) -> None:
        self.rows: list[str] = []

    def insert(self, value: str) -> int:
        if NUL in value:
            raise ValueError('invalid byte sequence for encoding "UTF8": 0x00')
        self.rows.append(value)
        return len(self.rows)


class NewUser(BaseModel):
    name: str


class SafeNewUser(BaseModel):
    # The NUL ban is in the schema (`pattern`), so the published contract and the server agree.
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[^\x00]+$")]

    @field_validator("name")
    @classmethod
    def storable_text(cls, value: str) -> str:
        # JSON Schema cannot express "no lone surrogates"; Schemathesis never sends them anyway.
        if any(ord(char) in _SURROGATES for char in value):
            raise ValueError("lone surrogates are not encodable as UTF-8")
        return value


class CreatedUser(BaseModel):
    id: int
    name: str


async def unreflected_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """A `422` that omits `input`.

    FastAPI's default handler echoes `input`; a lone surrogate there makes Starlette's
    `JSONResponse.render` raise `UnicodeEncodeError`, which turns the `422` into a `500`.
    """
    if not isinstance(exc, RequestValidationError):
        raise TypeError(f"expected RequestValidationError, got {type(exc).__name__}")
    detail = [{"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": detail}, status_code=422)


def build_app(*, fixed: bool, document_400: bool = True) -> FastAPI:
    """Build the lab API; `fixed=False` keeps the bugs, `fixed=True` removes them.

    `document_400=False` drops the `400` from `POST /users`, so the contract no longer covers the
    status FastAPI returns for a body that is not parseable JSON.
    """
    app = FastAPI(title="fuzz-lab", version="1")
    table = TextColumn()
    unauthorized: dict[int | str, dict[str, Any]] = {401: {"description": "Missing or bad key"}}
    # FastAPI answers an unparseable body (e.g. invalid UTF-8) with 400 but documents only 422.
    unparseable: dict[int | str, dict[str, Any]] = (
        {400: {"description": "Body is not JSON"}} if document_400 else {}
    )

    if fixed:
        app.add_exception_handler(RequestValidationError, unreflected_validation_error)

        @app.get("/ratio")
        def ratio_fixed(
            x: Annotated[float, Query(ge=0.001, le=1000.0, allow_inf_nan=False)],
        ) -> dict[str, float]:
            return {"inverse": 1.0 / x}

        @app.post("/users", status_code=201, responses=unparseable)
        def create_fixed(user: SafeNewUser) -> CreatedUser:
            return CreatedUser(id=table.insert(user.name), name=user.name)

        @app.get("/me", responses=unauthorized)
        def me_fixed(key: StrictKey) -> dict[str, str]:
            if key not in API_KEYS:
                raise HTTPException(401, "Bad key", headers={"WWW-Authenticate": "APIKey"})
            return {"tenant": key.split(":")[0]}

    else:

        @app.get("/ratio")
        def ratio(x: float) -> dict[str, float]:
            # x == 0.0 raises; a subnormal x overflows to inf, which FastAPI writes as null.
            return {"inverse": 1.0 / x}

        @app.post("/users", status_code=201, responses=unparseable)
        def create(user: NewUser) -> CreatedUser:
            # NUL fails in the store; a lone surrogate fails when the response is serialized.
            return CreatedUser(id=table.insert(user.name), name=user.name)

        @app.get("/me", responses=unauthorized)
        def me(key: LenientKey) -> dict[str, str]:
            # auto_error=False hands a missing or empty key to the handler as None.
            return {"tenant": key.split(":")[0]}  # type: ignore[union-attr]  # pyright: ignore[reportOptionalMemberAccess]

    return app


def load_schema(app: ASGIApp, *, phases: tuple[str, ...], max_examples: int, seed: int) -> Any:
    """Load the app's OpenAPI document with a hermetic, deterministic Schemathesis config."""
    all_phases = ("examples", "coverage", "fuzzing", "stateful")
    config = schemathesis.Config.from_dict(  # pyright: ignore[reportUnknownMemberType]
        {
            "seed": seed,
            "cache": {"enabled": False},
            "generation": {
                "deterministic": True,
                "database": "none",
                "max-examples": max_examples,
            },
            "phases": {name: {"enabled": name in phases} for name in all_phases},
        }
    )
    return schemathesis.openapi.from_asgi("/openapi.json", app, config=config)


@dataclass(frozen=True, slots=True)
class Finding:
    phase: str
    operation: str
    code: str
    check: str


def run_engine(
    app: ASGIApp, *, phases: tuple[str, ...], max_examples: int = 50, seed: int = 1
) -> list[Finding]:
    """Run the Schemathesis engine (what `schemathesis run` drives) and collect failed checks."""
    schema = load_schema(app, phases=phases, max_examples=max_examples, seed=seed)
    found: set[Finding] = set()
    for event in from_schema(schema).execute():
        if isinstance(event, events.FatalError):
            raise RuntimeError(f"Schemathesis engine failed: {event.exception!r}")
        if not isinstance(event, events.ScenarioFinished):
            continue
        for nodes in event.recorder.checks.values():
            for node in nodes:
                if node.failure_info is not None:
                    code = node.failure_info.failure.code
                    found.add(Finding(str(event.phase), str(event.label), code, node.name))
    return sorted(found, key=lambda f: (f.operation, f.phase, f.code, f.check))


def search_settings(max_examples: int) -> settings:
    """Hypothesis settings for a reproducible search: fixed seed derivation, no example database."""
    return settings(max_examples=max_examples, derandomize=True, database=None, deadline=None)


def minimal_failing_case(
    app: ASGIApp, path: str, method: str, *, max_examples: int = 500
) -> FuzzCase:
    """Search one operation for a `500` and return the case Hypothesis shrinks it to."""
    schema = load_schema(app, phases=("fuzzing",), max_examples=max_examples, seed=1)
    strategy: SearchStrategy[FuzzCase] = schema[path][method].as_strategy()

    def answers_500(case: FuzzCase) -> bool:
        return case.call().status_code == 500

    return find(strategy, answers_500, settings=search_settings(max_examples))


def minimal_failing_text(
    alphabet: SearchStrategy[str], fails: Callable[[str], bool], *, max_examples: int = 2000
) -> str:
    """Shrink to the smallest single character from `alphabet` for which `fails` holds."""
    return find(alphabet, fails, settings=search_settings(max_examples))


def headers_of(case: FuzzCase) -> dict[str, str]:
    """The generated headers as a plain dict; `Case.headers` is an untyped `CaseInsensitiveDict`."""
    raw = cast("object", case.headers)  # pyright: ignore[reportUnknownMemberType]
    return dict(cast("Mapping[str, str]", raw or {}))
