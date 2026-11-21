"""Redact secrets and PII from structlog events, access logs, spans, and 422 bodies.

The module has four layers, each matching one leak path:

1. ``RedactingProcessor``: a structlog processor that deep-copies the event dict,
   replaces values under sensitive keys, masks pydantic secrets, and scrubs
   free text (exception messages and the ``repr`` strings that
   ``dict_tracebacks`` renders for frame locals).
2. ``AccessLogQueryRedactor``: a ``logging.Filter`` for the ``uvicorn.access``
   record shape, which carries the raw query string in ``record.args[2]``.
3. ``record_exception_redacted``: overrides the ``exception.message`` and
   ``exception.stacktrace`` attributes the OpenTelemetry SDK derives from
   ``str(exc)``.
4. ``safe_validation_exception_handler``: a FastAPI handler that drops the
   ``input`` and ``ctx`` members FastAPI copies into the default ``422`` body.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import parse_qsl, urlencode

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, Secret, SecretBytes, SecretStr

if TYPE_CHECKING:
    from fastapi import Request
    from opentelemetry.trace import Span
    from structlog.typing import EventDict, Processor, WrappedLogger

REDACTED: Final = "[REDACTED]"
REDACTED_JWT: Final = "[REDACTED_JWT]"
MAX_DEPTH: Final = 10
MAX_SCRUB_CHARS: Final = 8192

# Exact key names (after normalisation) whose values are always secrets.
SENSITIVE_KEYS: Final = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "x_api_key",
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "session",
        "sessionid",
        "private_key",
    }
)
SENSITIVE_SUFFIXES: Final = ("_password", "_secret", "_token", "_api_key", "_apikey")

_TEXT_KEY_NAMES: Final = "|".join(sorted(SENSITIVE_KEYS, key=len, reverse=True))

# RFC 6750 Section 2.1: b64token = 1*( ALPHA / DIGIT / "-" / "." / "_" / "~" / "+" / "/" ) *"="
_AUTH_SCHEME: Final = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]+=*")
# A JWS compact serialization starts with base64url('{"') == "eyJ". The prefix form
# also matches a token that a repr truncation cut inside its payload.
JWT_PREFIX: Final = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]*){0,2}")
JWT_STRICT: Final = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# The lookbehind lets a match start only at the first character of a scheme run.
# With a plain \b instead, "a.a.a..." restarts the scan at every letter: O(n^2).
_URL_USERINFO: Final = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_KV_PAIR: Final = re.compile(rf"(?i)\b({_TEXT_KEY_NAMES})=([^&\s'\",;]+)")
# 'password': 'x'  or  "password": "x"; the closing quote is optional because
# structlog truncates a local's repr after 80 characters.
_QUOTED_PAIR: Final = re.compile(
    rf"""(?i)(['"])({_TEXT_KEY_NAMES})\1(\s*:\s*)(['"])(?:(?!\4).)*\4?"""
)


def normalize_key(key: str) -> str:
    """Lower-case, map ``-`` to ``_``, and keep the last dotted segment."""
    return key.lower().replace("-", "_").rsplit(".", 1)[-1]


def is_sensitive_key(key: str) -> bool:
    name = normalize_key(key)
    return name in SENSITIVE_KEYS or name.endswith(SENSITIVE_SUFFIXES)


def scrub_text(text: str) -> str:
    """Pattern-based scrubbing for free text; a backstop, never the primary control."""
    if len(text) > MAX_SCRUB_CHARS:
        # Bound the cost of attacker-sized strings; the dropped tail is never emitted.
        text = f"{text[:MAX_SCRUB_CHARS]}...[TRUNCATED {len(text) - MAX_SCRUB_CHARS} chars]"
    text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = JWT_PREFIX.sub(REDACTED_JWT, text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)
    text = _KV_PAIR.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return _QUOTED_PAIR.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}{REDACTED}{m.group(4)}"
        ),
        text,
    )


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except Exception as exc:  # a hostile __repr__ must not break logging
        return f"<unrepresentable {type(value).__name__}: {type(exc).__name__}>"


def redact_value(value: object, depth: int = 0) -> object:
    """Return a redacted deep copy; never mutates the caller's objects."""
    if depth > MAX_DEPTH:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, SecretStr | SecretBytes | Secret):
        return REDACTED
    if type(value) is str:
        # Exact type only: a str subclass may mask itself in __repr__ (see the last line).
        return scrub_text(value)
    if isinstance(value, bytes | bytearray):
        return scrub_text(bytes(value).decode("utf-8", "replace"))
    if isinstance(value, BaseModel):
        # mode="json" turns SecretStr into "**********"; key rules then cover plain str fields.
        return redact_value(value.model_dump(mode="json"), depth + 1)
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, item in cast("Mapping[object, object]", value).items():
            key = str(raw_key)
            out[key] = REDACTED if is_sensitive_key(key) else redact_value(item, depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(item, depth + 1) for item in cast("Iterable[object]", value)]
    if isinstance(value, BaseException):
        return scrub_text(f"{type(value).__name__}: {value}")
    return scrub_text(_safe_repr(value))


class RedactingProcessor:
    """structlog processor; place it after ``dict_tracebacks`` and before the renderer."""

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        try:
            redacted = redact_value(event_dict)
        except Exception as exc:
            # Fail closed: drop the whole event rather than emit it unredacted.
            return {"event": "log_redaction_failed", "error_type": type(exc).__name__}
        assert isinstance(redacted, dict)  # noqa: S101 - a Mapping always maps to a dict
        return redacted  # pyright: ignore[reportUnknownVariableType]


def safe_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.dict_tracebacks,
        RedactingProcessor(),
        structlog.processors.JSONRenderer(sort_keys=True),
    ]


def unsafe_order_processors() -> list[Processor]:
    """Same processors, redaction before ``dict_tracebacks``: frame locals escape it."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        RedactingProcessor(),
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(sort_keys=True),
    ]


def unredacted_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(sort_keys=True),
    ]


def make_logger(processors: list[Processor]) -> Any:
    """A logger whose methods return the rendered line instead of writing it."""
    return structlog.wrap_logger(structlog.testing.ReturnLogger(), processors=processors)


def redact_query(path_with_query: str) -> str:
    """Decode query names with ``parse_qsl`` so ``access%5Ftoken`` is caught too."""
    path, sep, query = path_with_query.partition("?")
    if not sep:
        return path_with_query
    pairs = parse_qsl(query, keep_blank_values=True)
    return path + "?" + urlencode([(k, REDACTED if is_sensitive_key(k) else v) for k, v in pairs])


class AccessLogQueryRedactor(logging.Filter):
    """Rewrite ``record.args[2]`` of the 5-tuple uvicorn 0.54.0 passes to ``uvicorn.access``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], redact_query(args[2]), args[3], args[4])
        return True


def record_exception_redacted(span: Span, exc: BaseException) -> None:
    """The SDK merges caller attributes over its defaults, so these replace them."""
    span.record_exception(
        exc,
        attributes={
            "exception.message": scrub_text(str(exc)),
            "exception.stacktrace": scrub_text("".join(traceback.format_exception(exc))),
        },
    )


class LoginRequest(BaseModel):
    # hide_input_in_errors masks str(ValidationError) only; errors() still has "input".
    model_config = ConfigDict(hide_input_in_errors=True)
    username: str
    password: SecretStr = Field(min_length=12)


def redacted_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Keep only ``type``, ``loc``, and ``msg``; ``input`` and ``ctx`` can echo the body."""
    return [{"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]} for e in errors]


async def safe_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    return JSONResponse(
        status_code=422, content={"detail": redacted_validation_errors(exc.errors())}
    )


def build_login_app(*, safe_errors: bool) -> FastAPI:
    app = FastAPI()
    if safe_errors:
        app.add_exception_handler(RequestValidationError, safe_validation_exception_handler)

    @app.post("/login")
    async def login(body: LoginRequest) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"user": body.username}

    return app
