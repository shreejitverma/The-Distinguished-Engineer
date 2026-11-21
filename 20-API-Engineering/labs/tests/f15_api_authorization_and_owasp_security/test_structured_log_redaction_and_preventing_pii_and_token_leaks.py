"""Lab: secret and PII leaks through logs, trace attributes, and 422 bodies, and their fixes."""

# Every credential below is a deliberate, obviously fake value that the lab tries to leak.
# ruff: noqa: S105

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
import structlog
from fastapi.exceptions import RequestValidationError
from opentelemetry.instrumentation.asgi import (  # pyright: ignore[reportMissingTypeStubs]
    OpenTelemetryMiddleware,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.http import PARAMS_TO_REDACT, redact_url
from pydantic import BaseModel, SecretStr, ValidationError

from api_labs.f15_api_authorization_and_owasp_security.log_redaction import (
    JWT_PREFIX,
    JWT_STRICT,
    MAX_SCRUB_CHARS,
    REDACTED,
    AccessLogQueryRedactor,
    LoginRequest,
    RedactingProcessor,
    build_login_app,
    make_logger,
    record_exception_redacted,
    redact_value,
    safe_processors,
    scrub_text,
    unredacted_processors,
    unsafe_order_processors,
)

PLAIN = "hunter2-plain-local"
IN_DICT = "hunter2-in-dict"
# Structurally a JWT (header {"alg":"HS256"}), but the signature is not real.
# The long payload makes structlog's 80-character repr cut fall inside segment two.
JWT = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJzdWIiOiJhbGljZSIsInNjb3BlIjoicmVhZCB3cml0ZSBhZG1pbiIsImV4cCI6MTk5OTk5OTk5OX0."
    "bm90LWEtcmVhbC1zaWduYXR1cmU"
)
BEARER = "Bearer tok-EXAMPLE-NOT-A-REAL-TOKEN"


def failing_handler_with_locals() -> None:
    password = PLAIN
    body = {"username": "alice", "password": IN_DICT}
    token = JWT
    secret = SecretStr("hunter2-secretstr")
    if password and body and token and secret:  # keep all four alive in the frame
        raise RuntimeError("database unavailable")


def render_error(processors: Any) -> str:
    log = make_logger(processors)
    try:
        failing_handler_with_locals()
    except RuntimeError:
        line: str = log.error("login_failed", exc_info=True)
        return line
    raise AssertionError("unreachable")


def frame_locals(line: str) -> dict[str, str]:
    event: dict[str, Any] = json.loads(line)
    frames: list[dict[str, Any]] = event["exception"][0]["frames"]
    last: dict[str, str] = frames[-1]["locals"]
    return last


@pytest.fixture
def clean_contextvars() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# --- pydantic SecretStr and ValidationError ---------------------------------


@pytest.mark.unit
def test_secretstr_masks_repr_str_and_json_but_python_dump_keeps_the_object() -> None:
    model = LoginRequest.model_validate({"username": "alice", "password": "correct-horse-battery"})
    assert repr(model) == "LoginRequest(username='alice', password=SecretStr('**********'))"
    assert str(model.password) == "**********"
    assert model.model_dump_json() == '{"username":"alice","password":"**********"}'
    dumped = model.model_dump()
    assert isinstance(dumped["password"], SecretStr)
    assert dumped["password"].get_secret_value() == "correct-horse-battery"
    assert str(SecretStr("")) == ""  # empty secrets render as '', revealing emptiness


@pytest.mark.unit
def test_hide_input_in_errors_masks_str_but_errors_list_still_carries_input() -> None:
    with pytest.raises(ValidationError) as info:
        LoginRequest.model_validate({"username": "alice", "password": "hunter2"})
    assert "hunter2" not in str(info.value)
    assert info.value.errors()[0]["input"] == "hunter2"
    assert "hunter2" in info.value.json()
    assert "input" not in info.value.errors(include_input=False)[0]


# --- structlog dict_tracebacks and the redacting processor ------------------


@pytest.mark.unit
def test_dict_tracebacks_renders_frame_locals_by_default() -> None:
    locals_ = frame_locals(render_error(unredacted_processors()))
    assert locals_["password"] == repr(PLAIN)
    assert IN_DICT in locals_["body"]
    assert locals_["secret"] == "SecretStr('**********')"


@pytest.mark.unit
def test_safe_pipeline_redacts_locals_by_name_and_by_repr_pattern() -> None:
    line = render_error(safe_processors())
    for leaked in (PLAIN, IN_DICT, JWT[:40], "hunter2-secretstr"):
        assert leaked not in line
    locals_ = frame_locals(line)
    assert locals_["password"] == REDACTED
    assert locals_["body"] == "{'username': 'alice', 'password': '[REDACTED]'}"
    assert locals_["token"] == REDACTED
    assert json.loads(line)["exception"][0]["exc_type"] == "RuntimeError"


@pytest.mark.unit
def test_redaction_before_dict_tracebacks_lets_locals_escape() -> None:
    line = render_error(unsafe_order_processors())
    assert PLAIN in line
    assert IN_DICT in line


@pytest.mark.unit
def test_truncated_jwt_in_a_local_evades_strict_regex_but_not_prefix_regex() -> None:
    rendered = frame_locals(render_error(unredacted_processors()))["token"]
    assert rendered.endswith("'+" + str(len(JWT) - 80))
    assert JWT_STRICT.search(rendered) is None
    assert JWT_PREFIX.search(rendered) is not None
    assert scrub_text(rendered) == "'[REDACTED_JWT]'+" + str(len(JWT) - 80)


@pytest.mark.unit
def test_exception_group_members_are_walked_and_redacted() -> None:
    def run(processors: Any) -> str:
        log = make_logger(processors)
        try:
            errors: list[Exception] = []
            try:
                failing_handler_with_locals()
            except RuntimeError as exc:
                errors.append(exc)
            raise ExceptionGroup("batch failed", errors)
        except ExceptionGroup:
            line: str = log.error("batch_failed", exc_info=True)
            return line

    assert PLAIN in run(unredacted_processors())
    safe = run(safe_processors())
    assert PLAIN not in safe
    assert json.loads(safe)["exception"][0]["is_group"] is True


@pytest.mark.unit
@pytest.mark.usefixtures("clean_contextvars")
def test_contextvars_and_header_names_are_matched_after_normalisation() -> None:
    structlog.contextvars.bind_contextvars(authorization=BEARER)
    headers = {"Set-Cookie": "sid=s3cr3t-cookie", "X-API-Key": "k-EXAMPLE", "Accept": "json"}
    line: str = make_logger(safe_processors()).info("request", headers=headers)
    event: dict[str, Any] = json.loads(line)
    assert event["authorization"] == REDACTED
    assert event["headers"] == {"Set-Cookie": REDACTED, "X-API-Key": REDACTED, "Accept": "json"}
    unsafe: str = make_logger(unredacted_processors()).info("request")
    assert BEARER in unsafe


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "upstream said Authorization: " + BEARER,
            "upstream said Authorization: Bearer [REDACTED]",
        ),
        ("Basic YWxpY2U6aHVudGVyMg==", "Basic [REDACTED]"),
        ("GET https://alice:hunter2@db.internal/x", "GET https://[REDACTED]@db.internal/x"),
        ("/cb?code=1&access_token=tok-abc&state=s", "/cb?code=1&access_token=[REDACTED]&state=s"),
        ('{"client_secret": "cs-EXAMPLE"}', '{"client_secret": "[REDACTED]"}'),
        ("plain message, nothing secret", "plain message, nothing secret"),
    ],
)
def test_free_text_scrubber_patterns(raw: str, expected: str) -> None:
    assert scrub_text(raw) == expected


@pytest.mark.unit
def test_scrubber_caps_attacker_sized_text_and_stays_linear_on_scheme_runs() -> None:
    huge = "a" * (MAX_SCRUB_CHARS + 5) + "?password=late-secret"
    out = scrub_text(huge)
    assert out.endswith(f"...[TRUNCATED {len(huge) - MAX_SCRUB_CHARS} chars]")
    assert "late-secret" not in out
    # With a bare \b anchor this 8000-char run cost about 0.1 s per call in the lab probe.
    adversarial = "a." * 4000
    started = time.perf_counter()
    assert scrub_text(adversarial) == adversarial
    assert time.perf_counter() - started < 0.02


@pytest.mark.unit
def test_redaction_returns_a_copy_and_never_mutates_the_callers_dict() -> None:
    payload: dict[str, Any] = {"user": {"password": "hunter2", "name": "alice"}}
    result = redact_value({"payload": payload})
    assert result == {"payload": {"user": {"password": REDACTED, "name": "alice"}}}
    assert payload["user"]["password"] == "hunter2"


@pytest.mark.unit
def test_pydantic_models_and_secret_values_are_masked() -> None:
    class Upstream(BaseModel):
        url: str
        api_key: str

    event = {
        "login": LoginRequest.model_validate(
            {"username": "alice", "password": "correct-horse-battery"}
        ),
        "upstream": Upstream(url="https://svc.internal", api_key="k-EXAMPLE"),
        "raw_secret": SecretStr("s"),
    }
    assert redact_value(event) == {
        "login": {"username": "alice", "password": REDACTED},
        "upstream": {"url": "https://svc.internal", "api_key": REDACTED},
        "raw_secret": REDACTED,
    }


@pytest.mark.unit
def test_str_subclass_that_masks_its_repr_is_rendered_through_repr() -> None:
    class MaskedStr(str):
        __slots__ = ()

        def __repr__(self) -> str:
            return "MaskedStr('***')"

    assert redact_value({"note": MaskedStr("raw-secret-value")}) == {"note": "MaskedStr('***')"}


@pytest.mark.unit
def test_hostile_repr_is_contained_and_a_broken_mapping_fails_closed() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise ValueError("boom")

    assert redact_value(Hostile()) == "<unrepresentable Hostile: ValueError>"

    class BrokenMapping(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("cannot iterate")

        def __len__(self) -> int:
            return 1

    out = RedactingProcessor()(None, "info", {"event": "x", "bad": BrokenMapping()})
    assert out == {"event": "log_redaction_failed", "error_type": "RuntimeError"}


@pytest.mark.unit
def test_json_renderer_escapes_crlf_and_ansi_injection() -> None:
    hostile = 'alice\r\n{"level": "info", "event": "forged"}\x1b[2J'
    line: str = make_logger(safe_processors()).info("login", user=hostile)
    assert "\n" not in line
    assert "\x1b" not in line
    assert json.loads(line)["user"] == hostile


# --- uvicorn access log ------------------------------------------------------


@pytest.mark.unit
def test_access_log_filter_redacts_query_values_including_percent_encoded_names() -> None:
    records: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("lab.uvicorn.access")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = ListHandler()
    logger.addHandler(handler)
    redactor = AccessLogQueryRedactor()
    try:
        # Same format string and argument tuple as uvicorn 0.54.0 h11_impl.py.
        args = ("127.0.0.1:50000", "GET", "/cb?access%5Ftoken=tok-abc&page=2", "1.1", 200)
        logger.info('%s - "%s %s HTTP/%s" %d', *args)
        logger.addFilter(redactor)
        logger.info('%s - "%s %s HTTP/%s" %d', *args)
    finally:
        logger.removeFilter(redactor)
        logger.removeHandler(handler)
    assert "tok-abc" in records[0]
    assert scrub_text(records[0]) == records[0]  # the regex never sees "access_token"
    assert (
        records[1] == '127.0.0.1:50000 - "GET /cb?access_token=%5BREDACTED%5D&page=2 HTTP/1.1" 200'
    )


# --- FastAPI 422 bodies ------------------------------------------------------


async def post_login(safe_errors: bool, body: dict[str, Any]) -> httpx.Response:
    app = build_login_app(safe_errors=safe_errors)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/login", json=body)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_422_echoes_password_despite_secretstr_and_hide_input() -> None:
    short = await post_login(False, {"username": "alice", "password": "hunter2"})
    assert short.status_code == 422
    assert short.json()["detail"][0]["input"] == "hunter2"
    missing = await post_login(False, {"password": "correct-horse-battery"})
    assert missing.json()["detail"][0]["type"] == "missing"
    assert missing.json()["detail"][0]["input"] == {"password": "correct-horse-battery"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_422_handler_keeps_type_loc_msg_and_drops_input() -> None:
    response = await post_login(True, {"password": "correct-horse-battery"})
    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "missing", "loc": ["body", "username"], "msg": "Field required"}]
    }
    short = await post_login(True, {"username": "alice", "password": "hunter2"})
    assert "hunter2" not in short.text


@pytest.mark.unit
def test_request_validation_error_str_prints_every_error_dict_including_input() -> None:
    exc = RequestValidationError(
        [{"type": "too_short", "loc": ("body", "password"), "input": "hunter2"}]
    )
    assert "'input': 'hunter2'" in str(exc)
    # The text scrubber cannot tell that this "input" belonged to a password field.
    assert "hunter2" in scrub_text(str(exc))


# --- OpenTelemetry -----------------------------------------------------------


async def traced_request(url: str, headers: dict[str, str], sanitize: list[str]) -> dict[str, Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    traced = OpenTelemetryMiddleware(
        app,
        tracer_provider=provider,
        http_capture_headers_server_request=["authorization", "cookie"],
        http_capture_headers_sanitize_fields=sanitize,
        exclude_spans=["receive", "send"],
    )
    transport = httpx.ASGITransport(app=traced)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(url, headers=headers)
    (span,) = exporter.get_finished_spans()
    return dict(span.attributes or {})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_instrumentation_redacts_sig_but_keeps_access_token_in_http_url() -> None:
    attrs = await traced_request("/items?access_token=tok-abc&sig=s-abc", {}, [])
    assert attrs["http.url"] == "http://test/items?access_token=tok-abc&sig=REDACTED"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asgi_header_sanitizer_is_a_denylist_that_misses_unlisted_headers() -> None:
    headers = {"Authorization": BEARER, "Cookie": "sid=s3cr3t-cookie"}
    attrs = await traced_request("/items", headers, ["authorization"])
    assert attrs["http.request.header.authorization"] == ("[REDACTED]",)
    assert attrs["http.request.header.cookie"] == ("sid=s3cr3t-cookie",)


@pytest.mark.unit
def test_python_contrib_redacts_four_query_keys_while_semconv_lists_seven() -> None:
    assert PARAMS_TO_REDACT == ["AWSAccessKeyId", "Signature", "sig", "X-Goog-Signature"]
    url = "https://b.s3.amazonaws.com/k?X-Amz-Signature=abc&Signature=def"
    assert redact_url(url) == "https://b.s3.amazonaws.com/k?X-Amz-Signature=abc&Signature=REDACTED"


@pytest.mark.unit
def test_record_exception_copies_str_exc_unless_attributes_override_it() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("lab")
    exc = PermissionError("upstream rejected Authorization: " + BEARER)
    with tracer.start_as_current_span("default") as span:
        span.record_exception(exc)
    with tracer.start_as_current_span("redacted") as span:
        record_exception_redacted(span, exc)
    default, redacted = exporter.get_finished_spans()
    default_attrs = dict(default.events[0].attributes or {})
    redacted_attrs = dict(redacted.events[0].attributes or {})
    assert BEARER in str(default_attrs["exception.message"])
    assert BEARER in str(default_attrs["exception.stacktrace"])
    assert (
        redacted_attrs["exception.message"] == "upstream rejected Authorization: Bearer [REDACTED]"
    )
    assert BEARER not in str(redacted_attrs["exception.stacktrace"])
