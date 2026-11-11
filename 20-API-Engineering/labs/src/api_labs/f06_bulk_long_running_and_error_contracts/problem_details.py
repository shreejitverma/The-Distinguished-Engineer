"""RFC 9457 problem details: a type registry, a strict generator, and a lenient consumer."""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote, urljoin, urlsplit

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None

PROBLEM_JSON: Final = "application/problem+json"
ABOUT_BLANK: Final = "about:blank"
STANDARD_MEMBERS: Final = frozenset({"type", "title", "status", "detail", "instance"})

# RFC 9457 Section 4: extension names SHOULD start with ALPHA, use ALPHA / DIGIT / "_",
# and be three characters or longer. This lab turns the SHOULD into a registration error.
_EXTENSION_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")

# RFC 9110 Section 15.5 and 15.6 reason phrases, pinned so that an about:blank title does not
# change with the CPython release (http.HTTPStatus renamed 413, 414, 416, and 422 in 3.13).
RFC9110_PHRASES: Final[Mapping[int, str]] = {
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    411: "Length Required",
    412: "Precondition Failed",
    413: "Content Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    417: "Expectation Failed",
    421: "Misdirected Request",
    422: "Unprocessable Content",
    426: "Upgrade Required",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
    505: "HTTP Version Not Supported",
}

# RFC 3986 Section 3.5: fragment = *( pchar / "/" / "?" ).
# quote() always keeps ALPHA, DIGIT, and "-._~"; add sub-delims, ":", "@", "/", and "?".
_FRAGMENT_SAFE: Final = "!$&'()*+,;=:@/?"


@dataclass(frozen=True, slots=True)
class ProblemType:
    """What RFC 9457 Section 4 says a new problem type MUST document, plus its extensions."""

    uri: str
    title: str
    status: int
    extensions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not urlsplit(self.uri).scheme:
            raise ValueError(f"problem type URI must be absolute: {self.uri!r}")
        if not self.title:
            raise ValueError("problem type needs a title")
        if not 100 <= self.status <= 599:
            raise ValueError(f"status out of range: {self.status}")
        for name in self.extensions:
            if name in STANDARD_MEMBERS:
                raise ValueError(f"extension shadows a standard member: {name!r}")
            if _EXTENSION_NAME.fullmatch(name) is None:
                raise ValueError(f"extension name is not ALPHA *(ALPHA / DIGIT / _): {name!r}")


@dataclass(slots=True)
class ProblemRegistry:
    """One owner per type URI: a second registration of the same URI is a contract fork."""

    _types: dict[str, ProblemType] = field(default_factory=dict[str, ProblemType])

    def register(self, problem_type: ProblemType) -> ProblemType:
        if problem_type.uri in self._types:
            raise ValueError(f"problem type already registered: {problem_type.uri}")
        self._types[problem_type.uri] = problem_type
        return problem_type

    def get(self, uri: str) -> ProblemType:
        return self._types[uri]


def about_blank(status: int) -> ProblemType:
    """RFC 9457 Section 4.2.1: no semantics beyond the status code; title is the phrase."""
    title = RFC9110_PHRASES.get(status) or http.client.responses.get(status, "") or str(status)
    return ProblemType(ABOUT_BLANK, title, status)


@dataclass(frozen=True, slots=True)
class Problem:
    """One occurrence of a problem type."""

    type: ProblemType
    detail: str | None = None
    instance: str | None = None
    extensions: Mapping[str, JSONValue] = field(default_factory=dict[str, JSONValue])

    def __post_init__(self) -> None:
        undeclared = set(self.extensions) - self.type.extensions
        if undeclared:
            raise ValueError(f"undeclared extension members: {sorted(undeclared)}")

    def to_document(self) -> dict[str, JSONValue]:
        document: dict[str, JSONValue] = {
            "type": self.type.uri,
            "title": self.type.title,
            "status": self.type.status,
        }
        if self.detail is not None:
            document["detail"] = self.detail
        if self.instance is not None:
            document["instance"] = self.instance
        document.update(self.extensions)
        return document


class ProblemError(Exception):
    """Raise from a handler; the installed exception handler renders it."""

    def __init__(self, problem: Problem, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(problem.type.uri)
        self.problem = problem
        self.headers = dict(headers or {})


def render(problem: Problem, headers: Mapping[str, str] | None = None) -> Response:
    """HTTP status and the status member come from one field (RFC 9457 Section 3.1.2)."""
    body = json.dumps(problem.to_document(), separators=(",", ":"), ensure_ascii=True)
    merged = {"Content-Language": "en", "Cache-Control": "no-store", **(headers or {})}
    return Response(
        content=body.encode("ascii"),
        status_code=problem.type.status,
        media_type=PROBLEM_JSON,
        headers=merged,
    )


def json_pointer_fragment(path: Sequence[str | int]) -> str:
    """RFC 6901 Section 3 escaping, then Section 6 URI fragment percent-encoding."""
    tokens = (str(part).replace("~", "~0").replace("/", "~1") for part in path)
    return "#" + "".join("/" + quote(token, safe=_FRAGMENT_SAFE) for token in tokens)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    detail: str
    path: tuple[str | int, ...]


def validation_problem(problem_type: ProblemType, issues: Sequence[ValidationIssue]) -> Problem:
    """RFC 9457 Section 3 example: one problem type, many occurrences in an errors[] extension."""
    errors: list[JSONValue] = [
        {"detail": issue.detail, "pointer": json_pointer_fragment(issue.path)} for issue in issues
    ]
    return Problem(problem_type, extensions={"errors": errors})


def is_json_media_type(content_type: str) -> bool:
    """True for application/json and any +json structured syntax suffix (RFC 6839 Section 3.1)."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    top, _, subtype = media_type.partition("/")
    return top == "application" and (subtype == "json" or subtype.endswith("+json"))


class ProblemParseError(ValueError):
    """The body is not a problem details object at all."""


def _reject_duplicate_names(pairs: list[tuple[str, object]]) -> dict[str, object]:
    # RFC 8259 Section 4: duplicate names make receiver behavior unpredictable.
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ProblemParseError(f"duplicate member name: {name!r}")
        result[name] = value
    return result


@dataclass(frozen=True, slots=True)
class ParsedProblem:
    http_status: int
    type: str
    title: str | None
    status: int | None
    detail: str | None
    instance: str | None
    extensions: dict[str, object]
    ignored: tuple[str, ...]

    @property
    def status_mismatch(self) -> bool:
        """RFC 9457 Section 5: a disagreement may mean an intermediary changed the status."""
        return self.status is not None and self.status != self.http_status


def _as_status(value: object) -> int | None:
    # JSON true decodes to a bool, which is an int equal to 1; the range check rejects it.
    if isinstance(value, int):
        return value if 100 <= value <= 599 else None
    if isinstance(value, float) and value.is_integer():
        return _as_status(int(value))
    return None


def _member(document: object, name: str) -> object:
    """Read one member of a decoded JSON object, or None when the value is not an object."""
    if isinstance(document, dict):
        return cast("dict[str, object]", document).get(name)
    return None


def parse_problem(body: bytes, *, base_uri: str, http_status: int) -> ParsedProblem:
    """Consumer rules: ignore mistyped members (RFC 9457 Section 3.1), default and resolve type."""
    loaded: object = json.loads(body, object_pairs_hook=_reject_duplicate_names)
    if not isinstance(loaded, dict):
        raise ProblemParseError("problem details must be a JSON object")
    # object_pairs_hook built every object, so keys are str.
    members = cast("dict[str, object]", loaded)
    ignored: list[str] = []

    def string_member(name: str) -> str | None:
        value = members.get(name)
        if value is None and name not in members:
            return None
        if isinstance(value, str):
            return value
        ignored.append(name)
        return None

    raw_type = string_member("type")
    raw_instance = string_member("instance")
    status: int | None = None
    if "status" in members:
        status = _as_status(members["status"])
        if status is None:
            ignored.append("status")
    return ParsedProblem(
        http_status=http_status,
        type=urljoin(base_uri, raw_type) if raw_type is not None else ABOUT_BLANK,
        title=string_member("title"),
        status=status,
        detail=string_member("detail"),
        instance=urljoin(base_uri, raw_instance) if raw_instance is not None else None,
        extensions={k: v for k, v in members.items() if k not in STANDARD_MEMBERS},
        ignored=tuple(ignored),
    )


OUT_OF_CREDIT: Final = ProblemType(
    "https://example.com/probs/out-of-credit",
    "You do not have enough credit.",
    403,
    frozenset({"balance", "accounts"}),
)
VALIDATION_ERROR: Final = ProblemType(
    "https://example.net/validation-error",
    "Your request is not valid.",
    422,
    frozenset({"errors"}),
)


def build_app(registry: ProblemRegistry) -> FastAPI:
    """FastAPI app whose every error path, including routing and 500, emits problem+json."""
    app = FastAPI()

    @app.exception_handler(ProblemError)
    async def on_problem(request: Request, exc: ProblemError) -> Response:
        return render(exc.problem, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        # Starlette fills a missing detail from http.client.responses; that is not occurrence data.
        auto_detail = http.client.responses.get(exc.status_code, "")
        detail = None if exc.detail == auto_detail else exc.detail
        return render(Problem(about_blank(exc.status_code), detail=detail), exc.headers)

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> Response:
        # RFC 9457 Section 5: never put the exception text or a stack dump in the body.
        return render(Problem(about_blank(500)))

    @app.post("/purchase")
    async def purchase(request: Request) -> dict[str, str]:
        out_of_credit = registry.get(OUT_OF_CREDIT.uri)
        raise ProblemError(
            Problem(
                out_of_credit,
                detail="Your current balance is 30, but that costs 50.",
                instance="/account/12345/msgs/abc",
                extensions={"balance": 30, "accounts": ["/account/12345", "/account/67890"]},
            )
        )

    @app.post("/details")
    async def details(request: Request) -> dict[str, str]:
        payload: object = await request.json()
        issues: list[ValidationIssue] = []
        age = _member(payload, "age")
        if not (isinstance(age, int) and not isinstance(age, bool) and age > 0):
            issues.append(ValidationIssue("must be a positive integer", ("age",)))
        if _member(_member(payload, "profile"), "color") not in ("green", "red", "blue"):
            issues.append(ValidationIssue("must be 'green', 'red' or 'blue'", ("profile", "color")))
        if issues:
            raise ProblemError(validation_problem(registry.get(VALIDATION_ERROR.uri), issues))
        return {"status": "ok"}

    @app.get("/items/{item_id}")
    async def get_item(item_id: int) -> dict[str, int]:
        raise StarletteHTTPException(404, detail=f"Item {item_id} does not exist")

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("connect failed: postgres://app:hunter2@db.internal:5432")

    return app
