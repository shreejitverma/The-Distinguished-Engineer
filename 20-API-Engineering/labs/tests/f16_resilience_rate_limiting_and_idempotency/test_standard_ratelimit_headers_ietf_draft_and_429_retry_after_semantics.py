"""Lab: 429, Retry-After, and draft-ietf-httpapi-ratelimit-headers-11 RateLimit fields."""

from __future__ import annotations

import email.utils
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request
from hpack import Encoder
from slowapi import Limiter, _rate_limit_exceeded_handler  # pyright: ignore[reportPrivateUsage]
from slowapi.errors import RateLimitExceeded
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse, Response
from urllib3.exceptions import InvalidHeader
from urllib3.util.retry import Retry

from api_labs.f16_resilience_rate_limiting_and_idempotency.ratelimit_headers import (
    QUOTA_EXCEEDED,
    Decision,
    PolicySpec,
    QuotaPolicy,
    RateLimitMiddleware,
    ServiceLimit,
    format_http_date,
    parse_http_date,
    parse_ratelimit,
    parse_ratelimit_policy,
    parse_retry_after,
    plan_next_request,
    utc,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import Receive, Scope, Send

NOW = utc(1_794_700_800)  # 2026-11-15T00:00:00Z


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def hello(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def client(app: RateLimitMiddleware) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def pairs(response: httpx.Response) -> list[tuple[str, str]]:
    return list(response.headers.multi_items())


# --- Field syntax: draft-11 Sections 3 and 4 on top of RFC 9651 ------------------------------


@pytest.mark.unit
def test_seed_lab_plan_members_serialize_byte_exact_and_round_trip() -> None:
    policy = QuotaPolicy("burst", quota=100, window=60)
    limit = ServiceLimit("burst", available=0, effective_window=12)
    assert policy.serialize() == '"burst";q=100;w=60'
    assert limit.serialize() == '"burst";r=0;t=12'
    assert parse_ratelimit_policy([policy.serialize()]) == (policy,)
    assert parse_ratelimit([limit.serialize()]) == (limit,)


@pytest.mark.unit
def test_every_draft_section_3_2_and_4_2_example_parses() -> None:
    assert parse_ratelimit_policy(['"permin";q=50;w=60,"perhr";q=1000;w=3600']) == (
        QuotaPolicy("permin", 50, 60),
        QuotaPolicy("perhr", 1000, 3600),
    )
    content = parse_ratelimit_policy(
        ['"peruser";q=65535;qu="content-bytes";w=10;pk=:sdfjLJUOUH==:']
    )
    assert content is not None
    assert (content[0].unit, content[0].window) == ("content-bytes", 10)
    assert parse_ratelimit(['"default";r=999;pk=:dHJpYWwxMjEzMjM=:']) == (
        ServiceLimit("default", 999, None, b"trial121323"),
    )
    assert parse_ratelimit(['"default";r=300000000;t=60;pk=:QXBwLTk5OQ==:']) == (
        ServiceLimit("default", 300_000_000, 60, b"App-999"),
    )


@pytest.mark.unit
def test_draft_pk_examples_have_nonzero_pad_bits_and_still_parse() -> None:
    # ':cHsdsRa894==:' ends in '4' (0b111000): its low 4 bits are not zero, which strict base64
    # rejects; RFC 9651 Section 4.2.7 says parsers SHOULD NOT fail on that, nor on missing '='.
    policies = parse_ratelimit_policy(['"peruser";q=100;w=60;pk=:cHsdsRa894==:'])
    assert policies is not None
    assert policies[0].partition_key == bytes.fromhex("707b1db116bcf7")
    unpadded = parse_ratelimit(['"default";r=1;pk=:dHJpYWwxMjEzMjM:'])
    assert unpadded == (ServiceLimit("default", 1, None, b"trial121323"),)


@pytest.mark.unit
def test_list_split_over_field_lines_combines_in_order() -> None:
    lines = ['"burst";q=100;w=60', '"daily";q=1000;w=86400']
    assert parse_ratelimit_policy(lines) == (
        QuotaPolicy("burst", 100, 60),
        QuotaPolicy("daily", 1000, 86400),
    )


@pytest.mark.unit
def test_member_split_across_field_lines_is_malformed() -> None:
    assert parse_ratelimit_policy(['"burst";q=100', ";w=60"]) is None


@pytest.mark.unit
def test_bare_q_parses_as_boolean_true_which_isinstance_int_would_accept() -> None:
    assert isinstance(True, int)  # the trap a naive profile check falls into
    assert parse_ratelimit_policy(['"burst";q']) is None
    assert parse_ratelimit(['"burst";r;t=5']) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        '"default";t=30',  # r is REQUIRED
        '"default";r=-1;t=30',  # non-negative
        '"default";r=1.5;t=30',  # Integer, not Decimal
        '"default";r=5;t="30"',  # t is an Integer
        '"default";r=5;t=-1',
        "default;r=5;t=30",  # policy name must be a String, not a Token
        '("default");r=5',  # Inner List
        '"default";r=5;pk="dXNlcg=="',  # pk must be a Byte Sequence
        '"default";r=5,',  # trailing comma
        '"default";r=1000000000000000',  # 16 digits
        '"d\u00e9fault";r=5',  # not ASCII
        '"a";r=5, "a";r=0',  # ambiguous duplicate policy name (lab choice)
        "",
    ],
)
def test_malformed_ratelimit_is_ignored_as_a_whole(line: str) -> None:
    assert parse_ratelimit([line]) is None


@pytest.mark.unit
def test_one_bad_member_discards_the_valid_one_too() -> None:
    assert parse_ratelimit(['"good";r=5;t=10']) is not None
    assert parse_ratelimit(['"good";r=5;t=10, "bad";r=x']) is None


@pytest.mark.unit
def test_window_zero_is_invalid_in_policy_but_t_zero_is_valid_in_ratelimit() -> None:
    assert parse_ratelimit_policy(['"p";q=10;w=0']) is None
    assert parse_ratelimit(['"p";r=0;t=0']) == (ServiceLimit("p", 0, 0),)


@pytest.mark.unit
def test_unknown_parameters_of_every_type_are_comments() -> None:
    line = (
        '"default";r=5;t=10;acme-burst=20;acme-ratio=0.25;acme-exp=@1794700800;acme-n=%"caf%c3%a9"'
    )
    assert parse_ratelimit([line]) == (ServiceLimit("default", 5, 10),)


@pytest.mark.unit
def test_duplicate_parameter_key_last_value_wins() -> None:
    assert parse_ratelimit(['"default";r=50;r=0;t=9']) == (ServiceLimit("default", 0, 9),)


# --- Retry-After and HTTP-date: RFC 9110 Sections 10.2.3 and 5.6.7 ---------------------------


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "seconds"), [("120", 120.0), ("0", 0.0), ("007", 7.0)])
def test_delay_seconds_is_one_or_more_ascii_digits(raw: str, seconds: float) -> None:
    assert parse_retry_after([raw], now=NOW) == seconds


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["1.5", "-1", "+5", "1_000", "\u0661\u0662", "1e3", "inf", ""])
def test_values_python_numeric_parsers_accept_are_not_delay_seconds(raw: str) -> None:
    assert parse_retry_after([raw], now=NOW) is None


@pytest.mark.unit
def test_python_int_and_float_accept_what_the_abnf_forbids() -> None:
    assert int("1_000") == 1000
    assert int("\u0661\u0662") == 12
    assert float("1e3") == 1000.0
    assert float("inf") == float("inf")


@pytest.mark.unit
def test_huge_delay_seconds_is_an_absurd_wait_not_a_crash() -> None:
    # float(int("9" * 400)) overflows, and int() refuses 4300+ digits by default.
    assert parse_retry_after(["9" * 400], now=NOW) == math.inf
    assert parse_retry_after(["9" * 5000], now=NOW) == math.inf
    assert parse_retry_after(["0" * 20 + "7"], now=NOW) == 7.0  # leading zeros are not digits
    decision = plan_next_request(429, [("retry-after", "9" * 400)], now=NOW)
    assert (decision.action, decision.source) == ("give-up", "retry-after")


@pytest.mark.unit
def test_retry_after_is_a_singleton_so_two_lines_are_invalid() -> None:
    assert parse_retry_after(["5", "10"], now=NOW) is None


@pytest.mark.unit
def test_three_http_date_formats_name_the_same_instant() -> None:
    expected = utc(784_111_777)  # Sun, 06 Nov 1994 08:49:37 GMT, RFC 9110 Section 5.6.7
    for raw in (
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ):
        assert parse_http_date(raw, now=NOW) == expected
    assert format_http_date(expected) == "Sun, 06 Nov 1994 08:49:37 GMT"


@pytest.mark.unit
def test_rfc850_two_digit_year_uses_the_rolling_50_year_rule_not_pythons_fixed_pivot() -> None:
    raw = "Tuesday, 01-Jan-69 00:00:00 GMT"
    ours = parse_http_date(raw, now=NOW)
    assert ours is not None
    assert ours.year == 2069  # 43 years ahead of 2026, not more than 50
    assert email.utils.parsedate_to_datetime(raw).year == 1969  # POSIX pivot: 69-99 -> 19xx
    old = parse_http_date("Friday, 01-Jan-99 00:00:00 GMT", now=NOW)
    assert old is not None
    assert old.year == 1999


@pytest.mark.unit
def test_http_date_is_case_sensitive_and_gmt_only_unlike_email_utils() -> None:
    assert parse_http_date("sun, 06 Nov 1994 08:49:37 GMT", now=NOW) is None
    assert parse_http_date("Sun, 06 Nov 1994 08:49:37 +0530", now=NOW) is None
    offset = email.utils.parsedate_to_datetime("Sun, 06 Nov 1994 08:49:37 +0530")
    assert offset.utcoffset() is not None
    assert offset.utcoffset().total_seconds() == 19800  # type: ignore[union-attr]


@pytest.mark.unit
def test_email_utils_returns_naive_datetime_for_asctime_which_timestamp_reads_as_local() -> None:
    naive = email.utils.parsedate_to_datetime("Sun Nov  6 08:49:37 1994")
    assert naive.tzinfo is None
    ours = parse_http_date("Sun Nov  6 08:49:37 1994", now=NOW)
    assert ours is not None
    assert ours.utcoffset() is not None


@pytest.mark.unit
def test_http_date_measured_against_the_response_date_cancels_client_clock_skew() -> None:
    # draft-11 Appendix B.1.4: Date 09:27:00, Retry-After 09:27:05.
    skewed_now = utc(1_564_997_220 + 3600)  # client clock one hour fast
    lines = ["Mon, 05 Aug 2019 09:27:05 GMT"]
    date = parse_http_date("Mon, 05 Aug 2019 09:27:00 GMT", now=skewed_now)
    assert parse_retry_after(lines, now=skewed_now, date_header=date) == 5.0
    assert parse_retry_after(lines, now=skewed_now) == 0.0


# --- Client decisions: draft-11 Sections 7, 7.3, 8.5.1 ---------------------------------------


@pytest.mark.unit
def test_retry_after_takes_precedence_over_ratelimit() -> None:
    headers = [
        ("retry-after", "20"),
        ("ratelimit-policy", '"dynamic";q=100;w=60'),
        ("ratelimit", '"dynamic";r=15;t=40'),
    ]  # draft-11 Appendix B.3
    assert plan_next_request(429, headers, now=NOW) == Decision("wait", 20.0, "retry-after")


@pytest.mark.unit
def test_zero_available_quota_on_a_200_still_means_wait() -> None:
    headers = [("ratelimit", '"default";r=0;t=50')]  # draft-11 Appendix B.1.1
    assert plan_next_request(200, headers, now=NOW) == Decision("wait", 50.0, "ratelimit")


@pytest.mark.unit
def test_ratelimit_on_a_cached_response_is_ignored() -> None:
    headers = [("age", "30"), ("ratelimit", '"default";r=0;t=50')]
    assert plan_next_request(200, headers, now=NOW) == Decision("send", 0.0, "none")


@pytest.mark.unit
@pytest.mark.parametrize("age", ["\u00b2", "\u0663\u0660", "0", "-5", ""])
def test_age_that_is_not_positive_ascii_delta_seconds_does_not_mark_the_response_cached(
    age: str,
) -> None:
    # "\u00b2".isdigit() is True but int() rejects it; the client must not crash.
    headers = [("age", age), ("ratelimit", '"default";r=0;t=50')]
    assert plan_next_request(200, headers, now=NOW) == Decision("wait", 50.0, "ratelimit")


@pytest.mark.unit
def test_absurd_retry_after_gives_up_where_urllib3_silently_clamps_to_six_hours() -> None:
    headers = [("retry-after", "1000000")]  # the draft-11 Section 8.5.1 example value
    assert plan_next_request(429, headers, now=NOW) == Decision("give-up", 1e6, "retry-after")
    assert Retry.DEFAULT_RETRY_AFTER_MAX == 21600
    assert Retry().parse_retry_after("1000000") == 21600


@pytest.mark.unit
def test_429_without_usable_hints_falls_back_to_backoff() -> None:
    headers = [("retry-after", "soon"), ("ratelimit", '"x";r=-1')]
    assert plan_next_request(429, headers, now=NOW) == Decision("backoff", 0.0, "none")


@pytest.mark.unit
def test_partition_key_filters_out_another_partitions_exhaustion() -> None:
    headers = [("ratelimit", '"user";r=0;t=30;pk=:b3RoZXI=:, "app";r=9;t=30;pk=:bWU=:')]
    assert plan_next_request(200, headers, now=NOW, partition_key=b"me").action == "send"
    assert plan_next_request(200, headers, now=NOW).action == "wait"


@pytest.mark.unit
def test_urllib3_parse_retry_after_edges() -> None:
    assert Retry().parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT") == 0
    with pytest.raises(InvalidHeader):
        Retry().parse_retry_after("1.5")


# --- Server: RateLimitMiddleware ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_seed_plan_200_then_429_with_retry_after_and_problem_details() -> None:
    clock = FakeClock()
    app = RateLimitMiddleware(hello, policies=[PolicySpec("burst", 100, 60)], clock=clock)
    async with client(app) as c:
        first = await c.get("/")
        clock.now = 48
        for _ in range(98):
            await c.get("/")
        last_ok = await c.get("/")
        rejected = await c.get("/")
    assert first.headers["ratelimit"] == '"burst";r=99;t=60'
    assert last_ok.status_code == 200
    assert last_ok.headers["ratelimit-policy"] == '"burst";q=100;w=60'
    assert last_ok.headers["ratelimit"] == '"burst";r=0;t=12'
    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "12"
    assert rejected.headers["ratelimit"] == '"burst";r=0;t=12'
    assert rejected.headers["content-type"] == "application/problem+json"
    assert rejected.json() == {
        "type": QUOTA_EXCEEDED,
        "title": "Quota Exceeded",
        "status": 429,
        "violated-policies": ["burst"],
    }
    assert plan_next_request(429, pairs(rejected), now=NOW) == Decision("wait", 12.0, "retry-after")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rejected_requests_do_not_consume_quota_or_extend_the_window() -> None:
    clock = FakeClock()
    app = RateLimitMiddleware(hello, policies=[PolicySpec("burst", 2, 10)], clock=clock)
    async with client(app) as c:
        for _ in range(2):
            await c.get("/")
        clock.now = 5
        statuses = [(await c.get("/")).status_code for _ in range(50)]
        clock.now = 10
        fresh = await c.get("/")
    assert set(statuses) == {429}
    assert fresh.status_code == 200
    assert fresh.headers["ratelimit"] == '"burst";r=1;t=10'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_effective_window_rounds_up_so_it_never_reads_zero_inside_the_window() -> None:
    clock = FakeClock()
    app = RateLimitMiddleware(hello, policies=[PolicySpec("burst", 1, 60)], clock=clock)
    async with client(app) as c:
        await c.get("/")
        clock.now = 59.2
        response = await c.get("/")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.headers["ratelimit"] == '"burst";r=0;t=1'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_policies_report_both_and_name_only_the_violated_one() -> None:
    clock = FakeClock()
    policies = [PolicySpec("burst", 3, 10), PolicySpec("daily", 5, 86400)]
    app = RateLimitMiddleware(hello, policies=policies, clock=clock)
    async with client(app) as c:
        for _ in range(3):
            await c.get("/")
        clock.now = 4
        burst_hit = await c.get("/")
        clock.now = 10
        for _ in range(2):
            await c.get("/")
        clock.now = 11
        daily_hit = await c.get("/")
    assert burst_hit.headers["ratelimit-policy"] == '"burst";q=3;w=10, "daily";q=5;w=86400'
    assert burst_hit.headers["ratelimit"] == '"burst";r=0;t=6, "daily";r=2;t=86396'
    assert burst_hit.json()["violated-policies"] == ["burst"]
    assert burst_hit.headers["retry-after"] == "6"
    assert daily_hit.json()["violated-policies"] == ["daily"]
    assert daily_hit.headers["retry-after"] == "86389"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jitter_only_ever_pushes_retry_after_past_the_window_end() -> None:
    clock = FakeClock()
    app = RateLimitMiddleware(
        hello, policies=[PolicySpec("burst", 1, 60)], clock=clock, jitter=lambda: 7
    )
    async with client(app) as c:
        await c.get("/")
        rejected = await c.get("/")
    limits = parse_ratelimit(rejected.headers.get_list("ratelimit"))
    assert limits == (ServiceLimit("burst", 0, 60),)
    assert rejected.headers["retry-after"] == "67"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_negative_jitter_cannot_pull_retry_after_before_the_window_end() -> None:
    clock = FakeClock()
    app = RateLimitMiddleware(
        hello, policies=[PolicySpec("burst", 1, 60)], clock=clock, jitter=lambda: -30
    )
    async with client(app) as c:
        await c.get("/")
        rejected = await c.get("/")
    assert rejected.headers["retry-after"] == "60"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_keys_get_separate_partitions_and_a_predictable_non_secret_pk() -> None:
    clock = FakeClock()
    app = RateLimitMiddleware(hello, policies=[PolicySpec("user", 1, 60)], clock=clock)
    key_a = "key-a-EXAMPLE-NOT-A-REAL-KEY"
    async with client(app) as c:
        a1 = await c.get("/", headers={"x-api-key": key_a})
        b1 = await c.get("/", headers={"x-api-key": "key-b-EXAMPLE-NOT-A-REAL-KEY"})
        a2 = await c.get("/", headers={"x-api-key": key_a})
    assert (a1.status_code, b1.status_code, a2.status_code) == (200, 200, 429)
    expected_pk = hashlib.sha256(b"ratelimit-pk-v1:" + key_a.encode()).digest()[:9]
    limits = parse_ratelimit(a1.headers.get_list("ratelimit"))
    assert limits == (ServiceLimit("user", 0, 60, expected_pk),)
    assert key_a not in str(a1.headers)
    assert plan_next_request(200, pairs(b1), now=NOW, partition_key=expected_pk).action == "send"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_appends_so_an_upstream_ratelimit_survives() -> None:
    async def upstream(scope: Scope, receive: Receive, send: Send) -> None:
        headers = [(b"ratelimit", b'"backend";r=0;t=30')]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"ok"})

    app = RateLimitMiddleware(upstream, policies=[PolicySpec("edge", 10, 60)], clock=FakeClock())
    async with client(app) as c:
        response = await c.get("/")
    assert response.headers.get_list("ratelimit") == ['"backend";r=0;t=30', '"edge";r=9;t=60']
    assert parse_ratelimit(response.headers.get_list("ratelimit")) == (
        ServiceLimit("backend", 0, 30),
        ServiceLimit("edge", 9, 60),
    )
    assert plan_next_request(200, pairs(response), now=NOW) == Decision("wait", 30.0, "ratelimit")


@pytest.mark.unit
def test_starlette_setitem_removes_the_upstream_limit_append_keeps_it() -> None:
    upstream = [(b"ratelimit", b'"backend";r=0;t=30')]
    replaced = MutableHeaders(raw=list(upstream))
    replaced["ratelimit"] = '"edge";r=9;t=60'
    appended = MutableHeaders(raw=list(upstream))
    appended.append("ratelimit", '"edge";r=9;t=60')
    assert replaced.getlist("ratelimit") == ['"edge";r=9;t=60']
    assert appended.getlist("ratelimit") == ['"backend";r=0;t=30', '"edge";r=9;t=60']


# --- Ecosystem behavior at the pinned releases ----------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_slowapi_emits_legacy_headers_and_retry_after_even_on_200() -> None:
    def one_client(request: Request) -> str:
        return "one-client"

    def on_limit(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RateLimitExceeded)
        return _rate_limit_exceeded_handler(request, exc)

    async def root(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    limiter = Limiter(key_func=one_client, headers_enabled=True)
    decorate = cast(
        "Callable[[Callable[..., Any]], Callable[..., Any]]",
        limiter.limit("1/minute"),  # pyright: ignore[reportUnknownMemberType]
    )
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, on_limit)
    app.get("/")(decorate(root))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        ok = await c.get("/")
        limited = await c.get("/")
    assert ok.status_code == 200
    assert "retry-after" in ok.headers
    reset = ok.headers["x-ratelimit-reset"]
    assert "." in reset  # a fractional epoch timestamp, str(1 + reset_time)
    assert float(reset) > 1_000_000_000  # absolute epoch seconds, not a delta
    assert int(ok.headers["retry-after"]) <= 61  # a delta, on a 200
    assert "ratelimit" not in ok.headers
    assert limited.status_code == 429
    assert json.loads(limited.text) == {"error": "Rate limit exceeded: 1 per 1 minute"}


@pytest.mark.unit
def test_hpack_indexes_the_stable_policy_and_re_sends_the_changing_limit() -> None:
    encoder = Encoder()
    policy = ("ratelimit-policy", '"burst";q=100;w=60')
    first = encoder.encode([policy, ("ratelimit", '"burst";r=99;t=60')])
    second_policy = encoder.encode([policy])
    second_limit = encoder.encode([("ratelimit", '"burst";r=98;t=59')])
    assert len(second_policy) == 1  # one indexed-field byte from the dynamic table
    assert len(second_limit) > 10  # new literal value each response
    assert (len(first), len(second_policy), len(second_limit)) == (52, 1, 16)
