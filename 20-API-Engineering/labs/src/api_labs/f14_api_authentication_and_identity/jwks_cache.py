from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

import httpx
import jwt
from jwt.api_jwk import PyJWK

logger = logging.getLogger(__name__)


class JWKSError(Exception):
    """Base error for JWKS client failures."""


class InvalidKeyIdError(JWKSError):
    """Raised when a key identifier fails boundary validation."""


class UnknownKeyIdError(JWKSError):
    """Raised when a requested key identifier is not found in the JWK Set."""


class NegativeCachedKeyError(UnknownKeyIdError):
    """Raised when a key identifier is currently blacklisted in the negative cache."""


class RateLimitedRefreshError(JWKSError):
    """Raised when an unknown key lookup is rejected due to active cooldown."""


class JWKSFetchError(JWKSError):
    """Raised when the remote JWKS endpoint cannot be reached or returns an error."""


# \Z, not $: in Python `$` also matches before a trailing newline, so "kid\n" would pass.
_KID_PATTERN = re.compile(r"[A-Za-z0-9_\-.:=]{1,256}\Z")
_MAX_AGE_PATTERN = re.compile(r"max-age=(\d+)", re.IGNORECASE)

T = TypeVar("T")


class AsyncSingleFlight:
    """Coalesces concurrent asynchronous requests for the same resource into one execution."""

    def __init__(self) -> None:
        self._in_flight: dict[str, asyncio.Future[Any]] = {}

    async def call(self, key: str, fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        loop = asyncio.get_running_loop()
        if key in self._in_flight:
            result: T = await self._in_flight[key]
            return result

        future: asyncio.Future[T] = loop.create_future()
        self._in_flight[key] = future
        try:
            val = await fn()
            future.set_result(val)
            return val
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(key, None)


@dataclass
class CachedKeySet:
    """Represents a cached JSON Web Key Set with RFC 9111 HTTP caching metadata."""

    keys: dict[str, PyJWK]
    etag: str | None
    fetched_at: float
    expires_at: float
    stale_until: float

    def is_fresh(self, now: float) -> bool:
        return now < self.expires_at

    def is_stale_usable(self, now: float) -> bool:
        return self.expires_at <= now < self.stale_until


class AsyncJWKSClient:
    """Production-grade asynchronous JWKS client with singleflight coalescing,

    graceful key rotation, negative caching, and RFC 9111 / RFC 5861 resilience.
    """

    def __init__(
        self,
        uri: str,
        http_client: httpx.AsyncClient,
        cache_ttl: float = 300.0,
        cooldown_duration: float = 5.0,
        negative_cache_ttl: float = 10.0,
        max_negative_cache_size: int = 1000,
        stale_if_error: float = 86400.0,
        allowed_algorithms: list[str] | None = None,
    ) -> None:
        if not (uri.startswith("http://") or uri.startswith("https://")):
            raise JWKSError(f"Unsupported URI scheme: {uri!r}")

        self.uri = uri
        self.client = http_client
        self.default_cache_ttl = cache_ttl
        self.cooldown_duration = cooldown_duration
        self.negative_cache_ttl = negative_cache_ttl
        self.max_negative_cache_size = max_negative_cache_size
        self.stale_if_error = stale_if_error
        self.allowed_algorithms = allowed_algorithms or ["RS256", "ES256", "EdDSA"]

        self._cached_set: CachedKeySet | None = None
        self._negative_cache: OrderedDict[str, float] = OrderedDict()
        self._last_successful_fetch: float = 0.0
        self._single_flight = AsyncSingleFlight()
        self._fetch_count: int = 0

    @property
    def fetch_count(self) -> int:
        """Total number of network HTTP requests initiated to the JWKS endpoint."""
        return self._fetch_count

    def force_expire_cache_for_testing(self) -> None:
        """Test helper to simulate cache TTL expiration without sleeping."""
        if self._cached_set is not None:
            self._cached_set.expires_at = 0.0

    def validate_kid(self, kid: object) -> None:
        """Validate key identifier against RFC 8725 Section 3.10 boundary rules."""
        if not isinstance(kid, str) or not kid:
            raise InvalidKeyIdError("Key ID (kid) must be a non-empty string")
        if not _KID_PATTERN.match(kid):
            raise InvalidKeyIdError(
                f"Invalid kid format: {kid!r} violates allowed charset or size limits"
            )

    def _purge_expired_negative_cache(self, now: float) -> None:
        expired_keys = [k for k, exp in self._negative_cache.items() if now >= exp]
        for k in expired_keys:
            self._negative_cache.pop(k, None)

    def _record_negative_cache(self, kid: str, now: float) -> None:
        self._purge_expired_negative_cache(now)
        if len(self._negative_cache) >= self.max_negative_cache_size:
            self._negative_cache.popitem(last=False)
        self._negative_cache[kid] = now + self.negative_cache_ttl

    async def _fetch_from_network(self) -> CachedKeySet:
        """Execute single outbound HTTP request with ETag and RFC 9111 handling."""
        now = time.monotonic()
        headers: dict[str, str] = {"Accept": "application/json, application/jwk-set+json"}
        if self._cached_set and self._cached_set.etag:
            headers["If-None-Match"] = self._cached_set.etag

        self._fetch_count += 1
        try:
            resp = await self.client.get(self.uri, headers=headers)
        except httpx.RequestError as exc:
            if self._cached_set and self._cached_set.is_stale_usable(now):
                return self._cached_set
            raise JWKSFetchError(
                f"Network failure connecting to JWKS at {self.uri}: {exc}"
            ) from exc

        if resp.status_code == 304 and self._cached_set:
            cache_ctrl = resp.headers.get("Cache-Control", "")
            ttl = self._resolve_ttl(cache_ctrl)
            self._cached_set.fetched_at = now
            self._cached_set.expires_at = now + ttl
            self._cached_set.stale_until = now + ttl + self.stale_if_error
            self._last_successful_fetch = now
            return self._cached_set

        if resp.status_code != 200:
            if self._cached_set and self._cached_set.is_stale_usable(now):
                return self._cached_set
            raise JWKSFetchError(
                f"JWKS endpoint returned HTTP status {resp.status_code}: {resp.text}"
            )

        try:
            payload_data: object = resp.json()
        except Exception as exc:
            raise JWKSFetchError("JWKS endpoint returned non-JSON payload") from exc

        if not isinstance(payload_data, dict):
            raise JWKSFetchError("Malformed JWKS response: expected JSON object")

        payload: dict[str, Any] = cast("dict[str, Any]", payload_data)
        if "keys" not in payload:
            raise JWKSFetchError("Malformed JWKS response: missing 'keys' array")

        raw_keys = payload["keys"]
        if not isinstance(raw_keys, list):
            raise JWKSFetchError("Malformed JWKS response: 'keys' must be a list")
        parsed_keys: dict[str, PyJWK] = {}
        for raw_k in cast("list[object]", raw_keys):
            if not isinstance(raw_k, dict):
                continue
            raw_dict: dict[str, Any] = cast("dict[str, Any]", raw_k)
            if raw_dict.get("use") in (None, "sig"):
                kid_val = raw_dict.get("kid")
                if isinstance(kid_val, str):
                    try:
                        jwk_obj = PyJWK.from_dict(raw_dict)
                        parsed_keys[kid_val] = jwk_obj
                    except Exception as err:
                        logger.debug("Failed parsing key %s: %s", kid_val, str(err))
                        continue

        if not parsed_keys:
            raise JWKSFetchError("JWKS endpoint contained no usable signing keys")

        cache_ctrl = resp.headers.get("Cache-Control", "")
        ttl = self._resolve_ttl(cache_ctrl)
        etag = resp.headers.get("ETag")

        new_set = CachedKeySet(
            keys=parsed_keys,
            etag=etag,
            fetched_at=now,
            expires_at=now + ttl,
            stale_until=now + ttl + self.stale_if_error,
        )
        self._cached_set = new_set
        self._last_successful_fetch = now
        return new_set

    def _resolve_ttl(self, cache_control: str) -> float:
        """Honor an explicit max-age, including 0; use the default only when it is absent."""
        max_age = self._parse_max_age(cache_control)
        return self.default_cache_ttl if max_age is None else max_age

    def _parse_max_age(self, cache_control: str) -> float | None:
        match = _MAX_AGE_PATTERN.search(cache_control)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        return None

    async def get_signing_key(self, kid: str) -> PyJWK:
        """Resolve a signing key for the given kid with singleflight, negative caching,

        and thundering herd defenses.
        """
        self.validate_kid(kid)
        now = time.monotonic()

        self._purge_expired_negative_cache(now)
        if kid in self._negative_cache:
            raise NegativeCachedKeyError(f"Key ID {kid!r} is blacklisted in negative cache")

        if self._cached_set and kid in self._cached_set.keys and self._cached_set.is_fresh(now):
            return self._cached_set.keys[kid]

        time_since_last_fetch = now - self._last_successful_fetch
        in_cooldown = (
            self._last_successful_fetch > 0 and time_since_last_fetch < self.cooldown_duration
        )

        if self._cached_set and kid not in self._cached_set.keys and in_cooldown:
            msg = (
                f"Unknown key ID {kid!r} arrived during cooldown "
                f"({time_since_last_fetch:.2f}s < {self.cooldown_duration}s)"
            )
            raise RateLimitedRefreshError(msg)

        cached_set: CachedKeySet = await self._single_flight.call(
            "jwks_refresh", self._fetch_from_network
        )

        if kid in cached_set.keys:
            return cached_set.keys[kid]

        self._record_negative_cache(kid, time.monotonic())
        raise UnknownKeyIdError(f"Signing key with kid {kid!r} not found in remote JWKS")

    async def verify_token(
        self,
        token: str,
        *,
        audience: str | None = None,
        issuer: str | None = None,
        leeway: float = 0.0,
    ) -> dict[str, Any]:
        """Decode and cryptographically verify a JWT against the JWKS endpoint."""
        try:
            unverified_headers = jwt.get_unverified_header(token)
        except Exception as exc:
            raise JWKSError(f"Malformed JWT header: {exc}") from exc

        if "jku" in unverified_headers or "x5u" in unverified_headers:
            raise JWKSError(
                "In-band key URLs (jku/x5u) are strictly prohibited (RFC 8725 Section 3.10)"
            )

        kid = unverified_headers.get("kid")
        if not kid:
            raise InvalidKeyIdError("JWT header missing required 'kid' parameter")

        alg = unverified_headers.get("alg")
        if not alg or alg not in self.allowed_algorithms:
            raise JWKSError(f"Disallowed algorithm: {alg!r}")

        signing_key = await self.get_signing_key(kid)

        decode_kwargs: dict[str, Any] = {
            "algorithms": [alg],
            "leeway": leeway,
        }
        if audience is not None:
            decode_kwargs["audience"] = audience
        else:
            decode_kwargs["options"] = {"verify_aud": False}

        if issuer is not None:
            decode_kwargs["issuer"] = issuer

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                key=signing_key.key,
                **decode_kwargs,
            )
            return payload
        except jwt.PyJWTError as exc:
            raise JWKSError(f"Token verification failed: {exc}") from exc
