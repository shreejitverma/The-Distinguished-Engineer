from __future__ import annotations

import asyncio
import base64
from typing import Any, cast

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api_labs.f14_api_authentication_and_identity.jwks_cache import (
    AsyncJWKSClient,
    InvalidKeyIdError,
    JWKSError,
    NegativeCachedKeyError,
    RateLimitedRefreshError,
    UnknownKeyIdError,
)


def _int_to_b64(val: int) -> str:
    byte_len = (val.bit_length() + 7) // 8
    b = val.to_bytes(byte_len, byteorder="big")
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _generate_rsa_key_pair(kid: str) -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pn = priv.public_key().public_numbers()
    jwk_dict: dict[str, Any] = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_b64(pn.n),
        "e": _int_to_b64(pn.e),
    }
    return priv, jwk_dict


def _sign_jwt(
    priv_key: rsa.RSAPrivateKey,
    kid: str,
    payload: dict[str, Any],
    headers: dict[str, Any] | None = None,
) -> str:
    h = {"kid": kid, "alg": "RS256"}
    if headers:
        h.update(headers)
    token = jwt.encode(payload, priv_key, algorithm="RS256", headers=h)
    return token


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jwks_cache_hit_and_ttl() -> None:
    _priv1, jwk1 = _generate_rsa_key_pair("key-2026-01")
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"keys": [jwk1]},
            headers={"Cache-Control": "public, max-age=300", "ETag": '"etag-v1"'},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.example.com/.well-known/jwks.json",
            http_client,
            cache_ttl=300.0,
        )

        key1 = await client.get_signing_key("key-2026-01")
        assert key1.key_id == "key-2026-01"
        assert call_count == 1
        assert client.fetch_count == 1

        # Second lookup hits positive cache without HTTP call
        key2 = await client.get_signing_key("key-2026-01")
        assert key2.key_id == "key-2026-01"
        assert call_count == 1
        assert client.fetch_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graceful_key_rotation_overlap() -> None:
    priv_old, jwk_old = _generate_rsa_key_pair("kid-2026-alpha")
    priv_new, jwk_new = _generate_rsa_key_pair("kid-2026-beta")

    # In Phase 2 of rotation, IDP publishes both keys in the JWKS
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"keys": [jwk_old, jwk_new]},
            headers={"Cache-Control": "public, max-age=600"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://auth.enterprise.test/jwks",
            http_client,
        )

        token_old = _sign_jwt(
            priv_old,
            "kid-2026-alpha",
            {"sub": "user_old", "aud": "https://api.test"},
        )
        token_new = _sign_jwt(
            priv_new,
            "kid-2026-beta",
            {"sub": "user_new", "aud": "https://api.test"},
        )

        payload_old = await client.verify_token(token_old, audience="https://api.test")
        payload_new = await client.verify_token(token_new, audience="https://api.test")

        assert payload_old["sub"] == "user_old"
        assert payload_new["sub"] == "user_new"
        assert client.fetch_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_singleflight_thundering_herd_coalescing() -> None:
    _priv1, jwk1 = _generate_rsa_key_pair("key-heavy-load")
    requests_received = 0

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_received
        requests_received += 1
        await asyncio.sleep(0.05)  # simulate network latency
        return httpx.Response(
            200,
            json={"keys": [jwk1]},
            headers={"Cache-Control": "public, max-age=60"},
        )

    transport = httpx.MockTransport(slow_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.stampede.test/jwks",
            http_client,
        )

        # 50 concurrent coroutines attempting to resolve the same un-cached key
        tasks = [client.get_signing_key("key-heavy-load") for _ in range(50)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 50
        assert all(r.key_id == "key-heavy-load" for r in results)
        assert requests_received == 1
        assert client.fetch_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_kid_negative_cache_blocks_storm() -> None:
    _, jwk_legit = _generate_rsa_key_pair("legit-key")
    server_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal server_requests
        server_requests += 1
        return httpx.Response(200, json={"keys": [jwk_legit]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.test/jwks",
            http_client,
            negative_cache_ttl=30.0,
            cooldown_duration=0.1,
        )

        # First request with unknown kid triggers fetch, fails, and negative-caches
        with pytest.raises(UnknownKeyIdError):
            await client.get_signing_key("forged-attacker-kid-999")
        assert server_requests == 1
        assert client.fetch_count == 1

        # Subsequent 20 requests with same unknown kid hit negative cache (0 HTTP calls)
        for _ in range(20):
            with pytest.raises(NegativeCachedKeyError):
                await client.get_signing_key("forged-attacker-kid-999")

        assert server_requests == 1
        assert client.fetch_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_kid_cooldown_throttles_varying_kids() -> None:
    _, jwk_legit = _generate_rsa_key_pair("legit-key-01")
    server_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal server_requests
        server_requests += 1
        return httpx.Response(200, json={"keys": [jwk_legit]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.test/jwks",
            http_client,
            cooldown_duration=5.0,  # 5-second cooldown
        )

        # Initial request with random kid triggers 1 fetch
        with pytest.raises(UnknownKeyIdError):
            await client.get_signing_key("attacker-random-0001")
        assert server_requests == 1

        # Attacker rotates to different unknown kids within the 5s cooldown window
        for i in range(2, 25):
            with pytest.raises(RateLimitedRefreshError):
                await client.get_signing_key(f"attacker-random-{i:04d}")

        # IDP was completely shielded: server received exactly 1 request
        assert server_requests == 1
        assert client.fetch_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conditional_revalidation_304() -> None:
    _priv1, jwk1 = _generate_rsa_key_pair("revalidated-key")
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.headers.get("If-None-Match", "none"))
        if request.headers.get("If-None-Match") == '"version-123"':
            return httpx.Response(
                304,
                headers={"Cache-Control": "public, max-age=300"},
            )
        return httpx.Response(
            200,
            json={"keys": [jwk1]},
            headers={"ETag": '"version-123"', "Cache-Control": "public, max-age=1"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.test/jwks",
            http_client,
            cache_ttl=0.01,  # immediate expiry for test
            cooldown_duration=0.0,
        )

        k1 = await client.get_signing_key("revalidated-key")
        assert k1.key_id == "revalidated-key"
        assert call_log[0] == "none"

        # Force expiration of cached set
        client.force_expire_cache_for_testing()

        # Next lookup performs conditional GET with If-None-Match
        k2 = await client.get_signing_key("revalidated-key")
        assert k2.key_id == "revalidated-key"
        assert len(call_log) == 2
        assert call_log[1] == '"version-123"'
        assert client.fetch_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_if_error_resilience() -> None:
    priv1, jwk1 = _generate_rsa_key_pair("stale-key-1")
    idp_status = 200

    def handler(request: httpx.Request) -> httpx.Response:
        if idp_status == 500:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(
            200,
            json={"keys": [jwk1]},
            headers={"Cache-Control": "public, max-age=1"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.test/jwks",
            http_client,
            cache_ttl=0.01,
            stale_if_error=3600.0,
            cooldown_duration=0.0,
        )

        # Initial fetch succeeds
        k = await client.get_signing_key("stale-key-1")
        assert k.key_id == "stale-key-1"

        # Expire the cache and make IDP fail with 500
        client.force_expire_cache_for_testing()
        idp_status = 500

        # Resilient client uses stale cached key rather than failing
        k_stale = await client.get_signing_key("stale-key-1")
        assert k_stale.key_id == "stale-key-1"

        token = _sign_jwt(priv1, "stale-key-1", {"sub": "resilient_user"})
        payload = await client.verify_token(token)
        assert payload["sub"] == "resilient_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_kid_and_header_rejection() -> None:
    priv1, jwk1 = _generate_rsa_key_pair("clean-key")

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"keys": [jwk1]}))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = AsyncJWKSClient(
            "https://idp.test/jwks",
            http_client,
        )

        # Path traversal and injection attempts in kid
        with pytest.raises(InvalidKeyIdError):
            await client.get_signing_key("../../etc/passwd")

        with pytest.raises(InvalidKeyIdError):
            await client.get_signing_key("key' OR 1=1--")

        with pytest.raises(InvalidKeyIdError):
            await client.get_signing_key("key\x00nullbyte")

        # A trailing newline must not slip past the end anchor
        with pytest.raises(InvalidKeyIdError):
            await client.get_signing_key("clean-key\n")

        # Non-string kid values are rejected, not leaked as TypeError
        with pytest.raises(InvalidKeyIdError):
            await client.get_signing_key(cast("str", 123))

        with pytest.raises(InvalidKeyIdError):
            await client.get_signing_key("k" * 257)

        # In-band key URLs (jku / x5u) strictly rejected per RFC 8725 Section 3.10
        evil_token = _sign_jwt(
            priv1,
            "clean-key",
            {"sub": "attacker"},
            headers={"jku": "https://evil.attacker.com/keys.json"},
        )
        with pytest.raises(JWKSError, match="In-band key URLs"):
            await client.verify_token(evil_token)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_age_zero_is_honored_not_replaced_by_default_ttl() -> None:
    _priv, jwk = _generate_rsa_key_pair("key-zero")
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"keys": [jwk]},
            headers={"Cache-Control": "max-age=0"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AsyncJWKSClient("https://idp.example.com/jwks", http_client, cache_ttl=300.0)

        await client.get_signing_key("key-zero")
        await client.get_signing_key("key-zero")

        # max-age=0 means stale immediately, so the known kid refreshes on the next lookup.
        assert call_count == 2
