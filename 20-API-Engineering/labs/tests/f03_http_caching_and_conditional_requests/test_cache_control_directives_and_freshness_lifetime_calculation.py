"""Lab: Cache-Control directives and freshness lifetime calculation (RFC 9111, RFC 8246)."""

from __future__ import annotations

import calendar
import os
from email.utils import formatdate, parsedate_to_datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from litestar.datastructures import CacheControlHeader
from litestar.exceptions import ImproperlyConfiguredException
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from api_labs.f03_http_caching_and_conditional_requests.cache_control_freshness import (
    DELTA_SECONDS_INFINITY,
    Reuse,
    Source,
    StoredResponse,
    assess,
    current_age,
    delta_seconds,
    freshness_lifetime,
    parse_cache_control,
    parse_http_date,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

T0 = calendar.timegm((2026, 11, 9, 0, 0, 0))  # 2026-11-09T00:00:00Z on the cache clock


def http_date(stamp: int) -> str:
    return formatdate(stamp, usegmt=True)


def stored(
    *headers: tuple[str, str],
    status: int = 200,
    request_time: int = T0,
    response_time: int = T0,
) -> StoredResponse:
    return StoredResponse(status, list(headers), request_time, response_time)


# --- Freshness lifetime: first match of RFC 9111 Section 4.2.1 ---


@pytest.mark.unit
def test_s_maxage_overrides_max_age_only_for_shared_caches() -> None:
    r = stored(("Cache-Control", "max-age=60, s-maxage=300"), ("Date", http_date(T0)))
    cdn = freshness_lifetime(r, shared=True)
    browser = freshness_lifetime(r, shared=False)
    assert (cdn.seconds, cdn.source) == (300, Source.S_MAXAGE)
    assert (browser.seconds, browser.source) == (60, Source.MAX_AGE)


@pytest.mark.unit
def test_max_age_beats_expires_even_when_expires_is_later() -> None:
    r = stored(
        ("Date", http_date(T0)),
        ("Expires", http_date(T0 + 86400)),
        ("Cache-Control", "max-age=0"),
    )
    lifetime = freshness_lifetime(r, shared=True)
    assert (lifetime.seconds, lifetime.source) == (0, Source.MAX_AGE)


@pytest.mark.unit
def test_expires_minus_date_uses_the_origin_clock_not_the_cache_clock() -> None:
    # The origin clock runs one hour behind the cache: Date and Expires are both "early".
    skew = 3600
    r = stored(
        ("Date", http_date(T0 - skew)),
        ("Expires", http_date(T0 - skew + 600)),
        response_time=T0,
    )
    lifetime = freshness_lifetime(r, shared=False)
    assert (lifetime.seconds, lifetime.source) == (600, Source.EXPIRES)


@pytest.mark.unit
def test_missing_or_invalid_date_falls_back_to_response_time() -> None:
    no_date = stored(("Expires", http_date(T0 + 120)), response_time=T0)
    bad_date = stored(("Date", "yesterday"), ("Expires", http_date(T0 + 120)), response_time=T0)
    assert freshness_lifetime(no_date, shared=False).seconds == 120
    assert freshness_lifetime(bad_date, shared=False).seconds == 120


@pytest.mark.unit
@pytest.mark.parametrize("value", ["0", "-1", "never", "Sun, 06 Nov 1994 08:49:37 PST", ""])
def test_invalid_expires_including_zero_means_already_expired(value: str) -> None:
    r = stored(("Date", http_date(T0)), ("Expires", value))
    lifetime = freshness_lifetime(r, shared=False)
    assert (lifetime.seconds, lifetime.source) == (0, Source.EXPIRES)


@pytest.mark.unit
@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "", "\u0661\u0662"])
def test_invalid_max_age_makes_the_response_stale(value: str) -> None:
    r = stored(("Cache-Control", f"max-age={value}"), ("Expires", http_date(T0 + 3600)))
    lifetime = freshness_lifetime(r, shared=False)
    assert (lifetime.seconds, lifetime.source) == (0, Source.MAX_AGE)


@pytest.mark.unit
def test_expires_before_date_is_already_expired_not_negative() -> None:
    # RFC 9111 Section 4.2: an explicit expiration time in the past means already stale.
    r = stored(("Date", http_date(T0)), ("Expires", http_date(T0 - 60)))
    lifetime = freshness_lifetime(r, shared=False)
    assert (lifetime.seconds, lifetime.source) == (0, Source.EXPIRES)


@pytest.mark.unit
def test_whitespace_around_equals_is_malformed_and_therefore_stale() -> None:
    cc = parse_cache_control(["max-age = 60"])
    assert "max-age" in cc.malformed
    r = stored(("Cache-Control", "max-age = 60"), ("Expires", http_date(T0 + 3600)))
    assert freshness_lifetime(r, shared=False).seconds == 0


@pytest.mark.unit
def test_duplicate_max_age_first_occurrence_or_stale_by_policy() -> None:
    r = stored(("Cache-Control", "max-age=600"), ("Cache-Control", "max-age=5"))
    assert freshness_lifetime(r, shared=False, duplicates="first").seconds == 600
    assert freshness_lifetime(r, shared=False, duplicates="stale").seconds == 0
    two_expires = stored(
        ("Date", http_date(T0)), ("Expires", http_date(T0 + 60)), ("Expires", http_date(T0 + 9))
    )
    assert freshness_lifetime(two_expires, shared=False, duplicates="first").seconds == 60
    assert freshness_lifetime(two_expires, shared=False, duplicates="stale").seconds == 0


@pytest.mark.unit
def test_heuristic_is_ten_percent_of_date_minus_last_modified() -> None:
    r = stored(("Date", http_date(T0)), ("Last-Modified", http_date(T0 - 10 * 86400)))
    lifetime = freshness_lifetime(r, shared=True)
    assert (lifetime.seconds, lifetime.source) == (86400, Source.HEURISTIC)


@pytest.mark.unit
def test_heuristic_only_for_heuristically_cacheable_status_or_public() -> None:
    lm = ("Last-Modified", http_date(T0 - 10 * 86400))
    for status in (201, 302, 307, 500):
        assert freshness_lifetime(stored(lm, status=status), shared=True).source is Source.NONE
    with_public = stored(lm, ("Cache-Control", "public"), status=302)
    assert freshness_lifetime(with_public, shared=True).seconds == 86400
    with_private = stored(lm, ("Cache-Control", "private"), status=302)
    assert freshness_lifetime(with_private, shared=False).seconds == 86400
    assert freshness_lifetime(with_private, shared=True).source is Source.NONE


@pytest.mark.unit
def test_no_heuristic_when_last_modified_is_later_than_date() -> None:
    # RFC 9110 Section 8.8.2.1 forbids origins to send this; the lab refuses a negative heuristic.
    r = stored(("Date", http_date(T0)), ("Last-Modified", http_date(T0 + 3600)))
    lifetime = freshness_lifetime(r, shared=True)
    assert (lifetime.seconds, lifetime.source) == (0, Source.NONE)


@pytest.mark.unit
def test_no_heuristic_when_any_explicit_expiration_is_present() -> None:
    r = stored(("Cache-Control", "max-age=0"), ("Last-Modified", http_date(T0 - 10 * 86400)))
    assert freshness_lifetime(r, shared=False).source is Source.MAX_AGE


# --- Age: RFC 9111 Section 4.2.3 ---


@pytest.mark.unit
def test_current_age_adds_age_header_response_delay_and_resident_time() -> None:
    r = stored(
        ("Date", http_date(T0 - 100)),
        ("Age", "100"),
        request_time=T0 - 2,
        response_time=T0,
    )
    # corrected_age_value = 100 + 2; apparent_age = 100; resident_time = 30.
    assert current_age(r, now=T0 + 30) == 132


@pytest.mark.unit
def test_clock_skew_conservative_age_makes_a_fresh_response_stale_on_arrival() -> None:
    r = stored(
        ("Date", http_date(T0 - 3600)),  # origin clock one hour slow
        ("Cache-Control", "max-age=600"),
        request_time=T0 - 1,
        response_time=T0,
    )
    assert current_age(r, now=T0, conservative=True) == 3600
    assert current_age(r, now=T0, conservative=False) == 1
    assert assess(r, shared=True, now=T0).reuse is Reuse.VALIDATE


@pytest.mark.unit
def test_origin_clock_ahead_never_makes_age_negative() -> None:
    r = stored(("Date", http_date(T0 + 900)), request_time=T0, response_time=T0)
    assert current_age(r, now=T0) == 0
    assert current_age(r, now=T0, conservative=False) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [("10, 20", 10), ("-5", 0), ("abc", 0), ("  7  ", 7), ("\u0661\u0662", 0)],
)
def test_age_uses_the_first_member_and_ignores_invalid_values(value: str, expected: int) -> None:
    r = stored(("Age", value))
    assert current_age(r, now=T0) == expected


# --- delta-seconds limits: RFC 9111 Section 1.2.2 ---


@pytest.mark.unit
def test_delta_seconds_overflow_clamps_to_two_to_the_31() -> None:
    assert delta_seconds("2147483647") == 2**31 - 1
    assert delta_seconds("2147483648") == DELTA_SECONDS_INFINITY
    assert delta_seconds("9999999999") == DELTA_SECONDS_INFINITY
    assert delta_seconds("\u0661\u0662") is None  # str.isdigit() is True for Arabic-Indic digits
    assert delta_seconds("99999999999999999999") == DELTA_SECONDS_INFINITY
    assert delta_seconds("0000000000000060") == 60
    huge = "9" * 5000
    with pytest.raises(ValueError, match="4300"):
        int(huge)  # CPython's integer string conversion limit
    assert delta_seconds(huge) == DELTA_SECONDS_INFINITY
    r = stored(("Cache-Control", f"max-age={huge}"), ("Age", huge))
    assert freshness_lifetime(r, shared=False).seconds == DELTA_SECONDS_INFINITY
    assert current_age(r, now=T0) == DELTA_SECONDS_INFINITY


# --- Cache-Control syntax: RFC 9111 Section 5.2 ---


@pytest.mark.unit
def test_directive_names_are_case_insensitive_and_both_argument_forms_are_accepted() -> None:
    cc = parse_cache_control(['Max-Age="60", S-MAXAGE=120, PUBLIC'])
    assert dict(cc.directives) == {"max-age": "60", "s-maxage": "120", "public": None}
    r = stored(("cache-control", 'Max-Age="60"'))
    assert freshness_lifetime(r, shared=False).seconds == 60


@pytest.mark.unit
def test_qualified_no_cache_keeps_commas_inside_the_quoted_string() -> None:
    value = 'no-cache="Set-Cookie, X-Session", max-age=60'
    cc = parse_cache_control([value])
    assert cc.field_names("no-cache") == {"set-cookie", "x-session"}
    assert cc.directives["max-age"] == "60"
    assert [part.strip() for part in value.split(",")][1] == 'X-Session"'  # naive split breaks
    a = assess(stored(("Cache-Control", value)), shared=True, now=T0 + 10)
    assert a.reuse is Reuse.FRESH
    assert a.strip_fields == {"set-cookie", "x-session"}


@pytest.mark.unit
def test_quoted_string_backslash_escapes_a_quote_inside_the_argument() -> None:
    cc = parse_cache_control(['private="X-A\\"B", max-age=60'])
    assert cc.field_names("private") == {'x-a"b'}
    assert cc.directives["max-age"] == "60"
    assert cc.malformed == frozenset()


@pytest.mark.unit
def test_unknown_extension_directives_are_ignored() -> None:
    r = stored(("Cache-Control", 'max-age=60, community="UCI", stale-if-error=30, x-foo'))
    assert assess(r, shared=True, now=T0 + 10).reuse is Reuse.FRESH


@pytest.mark.unit
def test_cache_control_split_across_field_lines_is_one_list() -> None:
    r = stored(("Cache-Control", "max-age=60"), ("Cache-Control", "no-cache"))
    assert assess(r, shared=False, now=T0).reuse is Reuse.VALIDATE
    first_line_only = stored(("Cache-Control", "max-age=60"))
    assert assess(first_line_only, shared=False, now=T0).reuse is Reuse.FRESH


# --- Directive semantics: no-cache, no-store, private, public, must-*, immutable ---


@pytest.mark.unit
def test_unqualified_no_cache_forces_validation_even_when_fresh() -> None:
    r = stored(("Cache-Control", "max-age=3600, no-cache"))
    a = assess(r, shared=False, now=T0 + 1)
    assert a.lifetime.seconds == 3600
    assert a.reuse is Reuse.VALIDATE
    assert assess(r, shared=False, now=T0 + 1, request_lines=["max-stale"]).reuse is Reuse.VALIDATE


@pytest.mark.unit
def test_no_store_is_not_storable_in_private_or_shared_caches() -> None:
    r = stored(("Cache-Control", "no-store, max-age=3600"))
    assert assess(r, shared=False, now=T0).reuse is Reuse.NOT_STORABLE
    assert assess(r, shared=True, now=T0).reuse is Reuse.NOT_STORABLE
    req = stored(("Cache-Control", "max-age=3600"))
    assert assess(req, shared=True, now=T0, request_lines=["no-store"]).reuse is Reuse.NOT_STORABLE


@pytest.mark.unit
def test_must_understand_overrides_no_store_only_for_understood_status_codes() -> None:
    cc = ("Cache-Control", "must-understand, no-store, max-age=60")
    assert assess(stored(cc, status=200), shared=True, now=T0).reuse is Reuse.FRESH
    assert assess(stored(cc, status=299), shared=True, now=T0).reuse is Reuse.NOT_STORABLE


@pytest.mark.unit
def test_private_blocks_shared_storage_but_not_private_storage() -> None:
    r = stored(("Cache-Control", "private, max-age=60"))
    assert assess(r, shared=True, now=T0).reuse is Reuse.NOT_STORABLE
    assert assess(r, shared=False, now=T0).reuse is Reuse.FRESH
    qualified = stored(("Cache-Control", 'private="Set-Cookie", max-age=60'))
    a = assess(qualified, shared=True, now=T0)
    assert a.reuse is Reuse.FRESH
    assert a.strip_fields == {"set-cookie"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("max-age=60", Reuse.NOT_STORABLE),
        ("public, max-age=60", Reuse.FRESH),
        ("s-maxage=60", Reuse.FRESH),
        ("must-revalidate, max-age=60", Reuse.FRESH),
    ],
)
def test_authorized_requests_need_an_explicit_shared_cache_permission(
    value: str, expected: Reuse
) -> None:
    r = stored(("Cache-Control", value))
    assert assess(r, shared=True, now=T0, authorized=True).reuse is expected
    assert assess(r, shared=False, now=T0, authorized=True).reuse is not Reuse.NOT_STORABLE


@pytest.mark.unit
def test_a_302_is_storable_only_with_explicit_freshness() -> None:
    bare = stored(status=302)
    with_expires = stored(("Date", http_date(T0)), ("Expires", http_date(T0 + 60)), status=302)
    assert assess(bare, shared=True, now=T0).reuse is Reuse.NOT_STORABLE
    assert assess(with_expires, shared=True, now=T0).reuse is Reuse.FRESH


@pytest.mark.unit
def test_most_restrictive_wins_when_max_age_and_no_cache_conflict() -> None:
    r = stored(("Cache-Control", "no-cache, max-age=86400"))
    assert assess(r, shared=True, now=T0).reuse is Reuse.VALIDATE


@pytest.mark.unit
def test_request_max_age_zero_and_min_fresh_force_validation() -> None:
    r = stored(("Cache-Control", "max-age=600"))
    assert assess(r, shared=True, now=T0 + 100).reuse is Reuse.FRESH
    assert assess(r, shared=True, now=T0 + 100, request_lines=["max-age=0"]).reuse is Reuse.VALIDATE
    assert assess(r, shared=True, now=T0 + 100, request_lines=["min-fresh=501"]).reuse is (
        Reuse.VALIDATE
    )
    assert assess(r, shared=True, now=T0 + 100, request_lines=["min-fresh=500"]).reuse is (
        Reuse.FRESH
    )


@pytest.mark.unit
def test_request_max_age_is_relaxed_by_max_stale() -> None:
    # Section 5.2.1.1: without max-stale the client does not want a stale response; the lab
    # reads an accompanying max-stale as lifting the request max-age bound.
    r = stored(("Cache-Control", "max-age=600"))
    both = ["max-age=0, max-stale"]
    assert assess(r, shared=True, now=T0 + 100, request_lines=both).reuse is Reuse.FRESH


@pytest.mark.unit
def test_max_stale_allows_stale_reuse_unless_a_directive_forbids_it() -> None:
    plain = stored(("Cache-Control", "max-age=60"))
    now = T0 + 90  # 30 seconds stale
    assert assess(plain, shared=True, now=now).reuse is Reuse.VALIDATE
    assert assess(plain, shared=True, now=now, request_lines=["max-stale"]).reuse is Reuse.STALE_OK
    assert assess(plain, shared=True, now=now, request_lines=["max-stale=30"]).reuse is (
        Reuse.STALE_OK
    )
    assert assess(plain, shared=True, now=now, request_lines=["max-stale=29"]).reuse is (
        Reuse.VALIDATE
    )
    for value in ("max-age=60, must-revalidate", "s-maxage=60", "max-age=60, proxy-revalidate"):
        r = stored(("Cache-Control", value))
        assert assess(r, shared=True, now=now, request_lines=["max-stale"]).reuse is (
            Reuse.VALIDATE
        )
    proxy_only = stored(("Cache-Control", "max-age=60, proxy-revalidate"))
    assert assess(proxy_only, shared=False, now=now, request_lines=["max-stale"]).reuse is (
        Reuse.STALE_OK
    )


@pytest.mark.unit
def test_immutable_skips_reload_validation_only_while_fresh_and_secure() -> None:
    r = stored(("Cache-Control", "max-age=31536000, immutable"))
    assert assess(r, shared=False, now=T0 + 10).skip_reload_validation is True
    assert assess(r, shared=False, now=T0 + 10, secure=False).skip_reload_validation is False
    assert assess(r, shared=False, now=T0 + 31536000).reuse is Reuse.VALIDATE
    forced = assess(r, shared=False, now=T0 + 10, request_lines=["no-cache"])
    assert (forced.reuse, forced.skip_reload_validation) == (Reuse.VALIDATE, False)


# --- HTTP-date parsing: RFC 9110 Section 5.6.7 and RFC 9111 Section 4.2 ---


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
        "sun, 06 nov 1994 08:49:37 gmt",
    ],
)
def test_http_date_accepts_three_formats_case_insensitively(value: str) -> None:
    assert parse_http_date(value, now=T0) == 784111777


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "Sun, 06 Nov 1994 08:49:37 PST",
        "Sun, 06 Nov 1994 08:49:37 +0000",
        "Sun, 06 Nov 1994 08:49:37 -0000",
        "Sun, 06 Nov 1994 08:49:37",
        "Sun, \u0660\u0666 Nov 1994 08:49:37 GMT",
        "Sun, 31 Feb 1994 08:49:37 GMT",
        "0",
    ],
)
def test_http_date_rejects_non_gmt_zones_unicode_digits_and_impossible_dates(value: str) -> None:
    assert parse_http_date(value, now=T0) is None


@pytest.mark.unit
def test_rfc850_two_digit_year_uses_the_fifty_year_rule_unlike_the_stdlib() -> None:
    value = "Saturday, 06-Nov-69 08:49:37 GMT"
    assert parse_http_date(value, now=T0) == calendar.timegm((2069, 11, 6, 8, 49, 37))
    assert parsedate_to_datetime(value).year == 1969  # CPython pivots at 68, not now + 50
    far = "Friday, 06-Nov-99 08:49:37 GMT"
    assert parse_http_date(far, now=T0) == calendar.timegm((1999, 11, 6, 8, 49, 37))


@pytest.mark.unit
def test_leap_second_rounds_down_to_the_earlier_second() -> None:
    value = "Wed, 31 Dec 2025 23:59:60 GMT"
    assert parse_http_date(value, now=T0) == calendar.timegm((2025, 12, 31, 23, 59, 59))


@pytest.mark.unit
def test_stdlib_parsedate_to_datetime_is_not_an_http_date_parser() -> None:
    # Naive results for asctime and unknown zones, and a non-GMT zone silently applied.
    assert parsedate_to_datetime("Sun Nov  6 08:49:37 1994").tzinfo is None
    assert parsedate_to_datetime("Sun, 06 Nov 1994 08:49:37 XYZ").tzinfo is None
    offset = parsedate_to_datetime("Sun, 06 Nov 1994 08:49:37 PST").utcoffset()
    assert offset is not None
    assert offset.total_seconds() == -8 * 3600
    with pytest.raises(ValueError, match="Invalid date"):
        parsedate_to_datetime("0")


# --- In-process ASGI: what a Starlette app actually emits ---


def _app(asset: Path) -> Starlette:
    async def file_endpoint(request: Request) -> FileResponse:
        return FileResponse(asset)

    async def profile(request: Request) -> JSONResponse:
        return JSONResponse({"user": 1}, headers={"Cache-Control": "private, max-age=60"})

    async def feed(request: Request) -> JSONResponse:
        return JSONResponse([], headers={"Cache-Control": "public, max-age=60, s-maxage=300"})

    async def split(request: Request) -> Response:
        response = Response(b"{}", media_type="application/json")
        response.headers.append("Cache-Control", "max-age=60")
        response.headers.append("Cache-Control", "no-cache")
        return response

    return Starlette(
        routes=[
            Route("/asset", file_endpoint),
            Route("/profile", profile),
            Route("/feed", feed),
            Route("/split", split),
        ]
    )


async def _fetch(app: Starlette, path: str, *, at: int) -> StoredResponse:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    return StoredResponse(response.status_code, response.headers.multi_items(), at, at)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_file_response_has_no_cache_control_so_freshness_is_heuristic(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "app.css"
    asset.write_bytes(b"body{}")
    os.utime(asset, (T0 - 10 * 86400, T0 - 10 * 86400))
    r = await _fetch(_app(asset), "/asset", at=T0)
    names = {name.lower() for name, _ in r.headers}
    assert "cache-control" not in names
    assert "expires" not in names
    assert "date" not in names  # no server in the loop, so the cache uses response_time
    assert "last-modified" in names
    lifetime = freshness_lifetime(r, shared=True)
    assert (lifetime.seconds, lifetime.source) == (86400, Source.HEURISTIC)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_private_max_age_is_fresh_in_a_browser_and_not_storable_in_a_cdn(
    tmp_path: Path,
) -> None:
    r = await _fetch(_app(tmp_path), "/profile", at=T0)
    assert assess(r, shared=True, now=T0).reuse is Reuse.NOT_STORABLE
    assert assess(r, shared=False, now=T0 + 59).reuse is Reuse.FRESH
    # Section 4.2: fresh means freshness_lifetime > current_age, so age 60 is already stale.
    assert assess(r, shared=False, now=T0 + 60).reuse is Reuse.VALIDATE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_starlette_s_maxage_gives_the_cdn_a_longer_lifetime_than_the_browser(
    tmp_path: Path,
) -> None:
    r = await _fetch(_app(tmp_path), "/feed", at=T0)
    assert assess(r, shared=False, now=T0 + 120).reuse is Reuse.VALIDATE
    assert assess(r, shared=True, now=T0 + 120).reuse is Reuse.FRESH
    stale = ["max-stale"]
    assert assess(r, shared=False, now=T0 + 400, request_lines=stale).reuse is Reuse.STALE_OK
    assert assess(r, shared=True, now=T0 + 400, request_lines=stale).reuse is Reuse.VALIDATE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_two_cache_control_lines_from_starlette_reach_the_cache_as_one_list(
    tmp_path: Path,
) -> None:
    r = await _fetch(_app(tmp_path), "/split", at=T0)
    assert r.lines("cache-control") == ["max-age=60", "no-cache"]
    assert httpx.Headers(list(r.headers))["cache-control"] == "max-age=60, no-cache"
    assert assess(r, shared=False, now=T0).reuse is Reuse.VALIDATE


# --- Litestar 2.25.0 CacheControlHeader: a typed helper that is not an RFC 9111 parser ---


@pytest.mark.unit
def test_litestar_serializes_a_false_boolean_directive_as_present() -> None:
    assert CacheControlHeader(max_age=60, public=False).to_header() == "max-age=60, public"
    assert CacheControlHeader(no_store=False).to_header() == "no-store"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    ["Max-Age=60", 'max-age="60"', "max-age=60, stale-if-error=30", 'no-cache="Set-Cookie"'],
)
def test_litestar_from_header_rejects_values_that_rfc_9111_requires_caches_to_accept(
    value: str,
) -> None:
    with pytest.raises(ImproperlyConfiguredException):
        CacheControlHeader.from_header(value)
    assert parse_cache_control([value]).malformed == frozenset()
