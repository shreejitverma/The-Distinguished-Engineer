"""Path tenant vs token tenant, uniform denials, and region routing for a two-cell API."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

import httpx
import jwt
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api_labs.f07_versioning_evolution_and_api_guidelines.tenant_scope import (
    AUDIENCE,
    ISSUER,
    LAB_HS256_KEY,
    REGION_HOSTS,
    RESOURCE_ID,
    DocumentStore,
    GuardRejectedError,
    Recorder,
    TenantContext,
    UntrustedRedirectError,
    build_cell_app,
    current_tenant,
    mint_token,
    permission_denied,
    send_region_aware,
)

NOW = 1_800_000_000
DIRECTORY: Mapping[str, str] = {"acme": "us", "globex": "eu"}
ACME_README = "/v1/orgs/acme/projects/web/documents/readme"
GLOBEX_README = "/v1/orgs/globex/projects/web/documents/readme"
DENIED = bytes(permission_denied().body)
Enforcement = Literal["middleware", "dependency"]


@dataclass
class World:
    recorder: Recorder
    store: DocumentStore
    us: FastAPI
    eu: FastAPI


def make_world(
    enforcement: Enforcement = "middleware", directory: Mapping[str, str] = DIRECTORY
) -> World:
    recorder = Recorder()
    store = DocumentStore(recorder)
    store.rows.update(
        {
            ("acme", "web", "readme"): "acme web readme",
            ("acme", "ml", "weights"): "acme ml weights",
            ("globex", "web", "readme"): "globex web readme",
        }
    )
    us, eu = (
        build_cell_app(
            region=region,
            directory=directory,
            store=store,
            clock=lambda: NOW,
            enforcement=enforcement,
        )
        for region in ("us", "eu")
    )
    return World(recorder=recorder, store=store, us=us, eu=eu)


def token(
    org: object = "acme",
    projects: object = ("web",),
    *,
    ttl: int = 300,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    key: bytes = LAB_HS256_KEY,
    typ: str = "at+jwt",
    issued_at: object = NOW,
    not_before: object | None = None,
) -> str:
    listed = list(cast("tuple[str, ...]", projects)) if isinstance(projects, tuple) else projects
    return mint_token(
        org=org,
        projects=listed,
        now=NOW,
        ttl=ttl,
        issuer=issuer,
        audience=audience,
        key=key,
        typ=typ,
        issued_at=issued_at,
        not_before=not_before,
    )


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def client_for(app: ASGIApp, region: str = "us") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"https://{REGION_HOSTS[region]}"
    )


@dataclass
class Spy:
    """Outermost ASGI wrapper: records scopes and the request body bytes the app pulled."""

    app: ASGIApp
    body_bytes: int = 0
    scopes: list[Scope] = field(default_factory=list[Scope])
    drop_raw_path: bool = False
    override: dict[str, object] = field(default_factory=dict[str, object])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.drop_raw_path:
            scope["raw_path"] = None
        scope.update(self.override)
        self.scopes.append(dict(scope))

        async def counting_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self.body_bytes += len(message.get("body", b""))
            return message

        await self.app(scope, counting_receive, send)


@dataclass
class Scripted:
    """ASGI app that answers every request with one fixed status and header set."""

    status: int
    headers: list[tuple[bytes, bytes]] = field(default_factory=list[tuple[bytes, bytes]])
    hosts: list[str] = field(default_factory=list[str])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.hosts.append(dict(scope["headers"])[b"host"].decode())
        await send({"type": "http.response.start", "status": self.status, "headers": self.headers})
        await send({"type": "http.response.body", "body": b""})


def two_cell_client(
    world: World, *, follow_redirects: bool, us: ASGIApp | None = None
) -> tuple[httpx.AsyncClient, Scripted]:
    """Route by host to the two cells; any other origin lands on a recording dead end."""
    elsewhere = Scripted(status=502)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=elsewhere),
        mounts={
            "all://us.api.lab.test": httpx.ASGITransport(app=us or world.us),
            "all://eu.api.lab.test": httpx.ASGITransport(app=world.eu),
        },
        follow_redirects=follow_redirects,
    )
    return client, elsewhere


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", ["middleware", "dependency"])
async def test_matching_path_and_token_reach_the_handler_with_the_canonical_name(
    enforcement: Enforcement,
) -> None:
    world = make_world(enforcement)

    async with client_for(world.us) as client:
        response = await client.get(ACME_README, headers=bearer(token()))

    assert response.status_code == 200
    assert response.json() == {
        "name": "orgs/acme/projects/web/documents/readme",
        "body": "acme web readme",
    }
    assert world.recorder.handler_calls == ["get_document"]
    assert world.recorder.store_reads == [("acme", "web", "readme")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_document_in_an_authorized_project_is_404_after_the_handler_ran() -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get(
            "/v1/orgs/acme/projects/web/documents/nosuch", headers=bearer(token())
        )

    assert response.status_code == 404
    assert response.json()["type"] == "https://api.lab.test/problems/not-found"
    assert world.recorder.handler_calls == ["get_document"]
    assert world.recorder.store_reads == [("acme", "web", "nosuch")]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", ["middleware", "dependency"])
async def test_path_org_that_differs_from_token_org_is_rejected_before_the_handler(
    enforcement: Enforcement,
) -> None:
    world = make_world(enforcement)

    async with client_for(world.us) as client:
        response = await client.get(GLOBEX_README, headers=bearer(token(org="acme")))

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "https://api.lab.test/problems/permission-denied",
        "title": "Permission denied",
        "status": 403,
        "detail": "Permission denied on the requested resource (or it might not exist).",
    }
    assert world.recorder.handler_calls == []
    assert world.recorder.store_reads == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        GLOBEX_README,
        "/v1/orgs/nosuch/projects/web/documents/readme",
        "/v1/orgs/Globex/projects/web/documents/readme",
        "/v1/orgs/globex",
        "/v1/orgs/acme/projects/ml/documents/weights",
        "/v1/orgs/acme/projects/nosuch/documents/readme",
    ],
    ids=[
        "existing-org-homed-in-eu",
        "missing-org",
        "malformed-org",
        "org-root",
        "unlisted-project",
        "missing-project",
    ],
)
async def test_every_tenant_denial_is_the_same_403_bytes_so_nothing_is_an_oracle(path: str) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get(path, headers=bearer(token(org="acme", projects=("web",))))

    assert response.status_code == 403
    assert response.content == DENIED
    assert "location" not in response.headers
    assert world.recorder.handler_calls == []


VALID_CLAIMS: Mapping[str, object] = {
    "iss": ISSUER,
    "aud": AUDIENCE,
    "sub": "user-1",
    "client_id": "lab-client",
    "iat": NOW,
    "jti": "handmade",
    "exp": NOW + 300,
    "org_id": "acme",
    "projects": ["web"],
}


def _unsigned_token() -> str:
    return jwt.encode(dict(VALID_CLAIMS), "", algorithm="none", headers={"typ": "at+jwt"})


def _token_without(claim: str) -> str:
    claims = {name: value for name, value in VALID_CLAIMS.items() if name != claim}
    return jwt.encode(claims, LAB_HS256_KEY, algorithm="HS256", headers={"typ": "at+jwt"})


INVALID_TOKENS: dict[str, str] = {
    "wrong-key": token(key=b"another-hs256-key-EXAMPLE-NOT-A-REAL-KEY"),
    "wrong-audience": token(audience="https://billing.lab.test"),
    "wrong-issuer": token(issuer="https://issuer.other.test"),
    "expired-at-exp": token(ttl=0),
    "id-token-typ": token(typ="JWT"),
    "not-yet-valid-nbf": token(not_before=NOW + 1),
    "nbf-is-a-string": token(not_before="soon"),
    "issued-in-the-future": token(issued_at=NOW + 1),
    "iat-is-a-bool": token(issued_at=True),
    "org-id-is-a-list": token(org=["acme", "globex"]),
    "org-id-is-uppercase": token(org="Acme"),
    "org-id-is-null": token(org=None),
    "projects-is-a-string": token(projects="web"),
    "projects-holds-an-int": token(projects=[7]),
    "projects-holds-a-wildcard": token(projects=["-"]),
    "alg-none": _unsigned_token(),
    "missing-sub": _token_without("sub"),
    "missing-exp": _token_without("exp"),
    "missing-client-id": _token_without("client_id"),
    "missing-iat": _token_without("iat"),
    "missing-jti": _token_without("jti"),
    "not-a-jwt": "opaque-EXAMPLE-NOT-A-REAL-TOKEN",
}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(INVALID_TOKENS))
async def test_invalid_token_is_401_invalid_token_and_never_reaches_the_handler(case: str) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get(ACME_README, headers=bearer(INVALID_TOKENS[case]))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert response.headers["content-type"] == "application/problem+json"
    assert world.recorder.handler_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Basic dXNlcjpwYXNz")],
        [("Authorization", "Bearer")],
        [("Authorization", f"Bearer {token()} trailing")],
        [("Authorization", f"Bearer {token()}"), ("Authorization", f"Bearer {token()}")],
    ],
    ids=["absent", "basic-scheme", "empty-credentials", "extra-field", "duplicated-header"],
)
async def test_missing_or_malformed_credentials_get_a_bare_bearer_challenge(
    headers: list[tuple[str, str]],
) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get(ACME_README, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert world.recorder.handler_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_token_one_second_before_exp_is_accepted_and_scheme_is_case_insensitive() -> None:
    world = make_world()
    headers = {"Authorization": f"bearer {token(ttl=1)}"}

    async with client_for(world.us) as client:
        response = await client.get(ACME_README, headers=headers)

    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        token(typ="application/at+jwt"),
        token(issued_at=NOW - 3600),
        token(not_before=NOW),
        token(not_before=NOW - 3600),
    ],
    ids=["long-typ", "issued-an-hour-ago", "nbf-is-now", "nbf-in-the-past"],
)
async def test_time_claims_are_judged_by_the_injected_clock_and_both_typ_forms_pass(
    value: str,
) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get(ACME_README, headers=bearer(value))

    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handmade_token_with_every_required_claim_and_no_projects_claim_is_accepted() -> None:
    world = make_world()
    handmade = _token_without("projects")

    async with client_for(world.us) as client:
        allowed = await client.get("/v1/orgs/acme/projects/-/documents", headers=bearer(handmade))
        denied = await client.get(ACME_README, headers=bearer(handmade))

    assert allowed.status_code == 200
    assert allowed.json() == {"documents": []}
    assert (denied.status_code, denied.content) == (403, DENIED)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enforcement", "expected_body_bytes"),
    [("middleware", 0), ("dependency", 1_048_607)],
)
@pytest.mark.parametrize(
    "collection",
    ["/v1/orgs/globex/projects/web/documents", "/v1/orgs/acme/projects/ml/documents"],
    ids=["other-org", "unlisted-project"],
)
async def test_middleware_denies_before_the_body_is_read_and_a_dependency_reads_all_of_it(
    enforcement: Enforcement, expected_body_bytes: int, collection: str
) -> None:
    world = make_world(enforcement)
    spy = Spy(world.us)
    content = b'{"document_id":"big","body":"' + b"x" * 1_048_576 + b'"}'

    async with client_for(spy) as client:
        response = await client.post(
            collection,
            headers={**bearer(token(org="acme")), "Content-Type": "application/json"},
            content=content,
        )

    assert len(content) == 1_048_607
    assert response.status_code == 403
    assert response.content == DENIED
    assert spy.body_bytes == expected_body_bytes
    assert world.recorder.handler_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enforcement", "expected_status"), [("middleware", 403), ("dependency", 422)]
)
async def test_dependency_guard_validates_json_before_it_authorizes_and_answers_422(
    enforcement: Enforcement, expected_status: int
) -> None:
    world = make_world(enforcement)

    async with client_for(world.us) as client:
        response = await client.post(
            "/v1/orgs/globex/projects/web/documents",
            headers={**bearer(token(org="acme")), "Content-Type": "application/json"},
            content=b'{"document_id": ',
        )

    assert response.status_code == expected_status
    assert world.recorder.handler_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", ["middleware", "dependency"])
async def test_wrong_cell_answers_a_member_with_307_to_the_home_region_and_keeps_the_query(
    enforcement: Enforcement,
) -> None:
    world = make_world(enforcement)

    async with client_for(world.us) as client:
        response = await client.get(
            GLOBEX_README + "?view=full", headers=bearer(token(org="globex"))
        )

    assert response.status_code == 307
    assert response.headers["location"] == f"https://eu.api.lab.test{GLOBEX_README}?view=full"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": "https://api.lab.test/problems/wrong-region",
        "title": "Organization is homed in another region",
        "status": 307,
        "home_region": "eu",
    }
    assert world.recorder.handler_calls == []
    assert world.recorder.store_reads == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_denial_precedes_region_routing_for_a_token_from_another_region() -> None:
    world = make_world()

    async with client_for(world.eu, "eu") as client:
        foreign = await client.get(GLOBEX_README, headers=bearer(token(org="acme")))
        own = await client.get(ACME_README, headers=bearer(token(org="acme")))

    assert (foreign.status_code, foreign.content) == (403, DENIED)
    assert "location" not in foreign.headers
    assert own.status_code == 307
    assert own.headers["location"] == f"https://us.api.lab.test{ACME_README}"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drop_raw_path", "expected_location"),
    [
        (False, "https://eu.api.lab.test/v1/orgs/globex%2Fprojects%2Fweb/documents/caf%C3%A9"),
        (True, "https://eu.api.lab.test/v1/orgs/globex/projects/web/documents/caf%C3%A9"),
    ],
    ids=["raw-path-kept", "raw-path-missing"],
)
async def test_redirect_location_is_built_from_raw_path_and_re_encoded_without_it(
    drop_raw_path: bool, expected_location: str
) -> None:
    world = make_world()
    spy = Spy(world.us, drop_raw_path=drop_raw_path)

    async with client_for(spy) as client:
        response = await client.get(
            "/v1/orgs/globex%2Fprojects%2Fweb/documents/caf%C3%A9",
            headers=bearer(token(org="globex")),
        )

    assert response.status_code == 307
    assert response.headers["location"] == expected_location


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected_location"),
    [
        (
            {"raw_path": b"/v1/documents/caf\xc3\xa9", "query_string": b"q=\xc3\xa9&x=a%20b"},
            "https://eu.api.lab.test/v1/documents/caf%C3%A9?q=%C3%A9&x=a%20b",
        ),
        ({"raw_path": b"/v1/documents/a b"}, "https://eu.api.lab.test/v1/documents/a%20b"),
    ],
    ids=["non-ascii-bytes", "raw-space"],
)
async def test_redirect_location_percent_encodes_raw_bytes_a_server_passed_through(
    override: dict[str, object], expected_location: str
) -> None:
    world = make_world()
    spy = Spy(world.us, override=override)

    async with client_for(spy) as client:
        response = await client.get("/v1/documents/x", headers=bearer(token(org="globex")))

    assert response.status_code == 307
    assert response.headers["location"] == expected_location


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("raw_path", [b"@evil.test/v1/documents/x", b".evil.test/x", b""])
async def test_request_target_without_a_leading_slash_never_becomes_a_location(
    raw_path: bytes,
) -> None:
    world = make_world()
    spy = Spy(world.us, override={"raw_path": raw_path, "path": raw_path.decode()})

    async with client_for(spy) as client:
        response = await client.get("/v1/documents/x", headers=bearer(token(org="globex")))

    assert response.status_code == 400
    assert response.json()["type"] == "https://api.lab.test/problems/bad-request-target"
    assert "location" not in response.headers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_naive_redirect_following_drops_the_bearer_token_and_ends_in_401() -> None:
    world = make_world()
    client, elsewhere = two_cell_client(world, follow_redirects=True)

    async with client:
        response = await client.get(
            f"https://us.api.lab.test{GLOBEX_README}", headers=bearer(token(org="globex"))
        )

    assert [hop.status_code for hop in response.history] == [307]
    assert "authorization" in response.history[0].request.headers
    assert response.request.url.host == "eu.api.lab.test"
    assert "authorization" not in response.request.headers
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert world.recorder.handler_calls == []
    assert elsewhere.hosts == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_region_aware_client_resends_credentials_and_body_to_the_home_region() -> None:
    world = make_world()
    client, elsewhere = two_cell_client(world, follow_redirects=False)
    globex = token(org="globex")

    async with client:
        fetched = await send_region_aware(
            client, "GET", f"https://us.api.lab.test{GLOBEX_README}", token=globex
        )
        created = await send_region_aware(
            client,
            "POST",
            "https://us.api.lab.test/v1/orgs/globex/projects/web/documents",
            token=globex,
            payload={"document_id": "notes", "body": "written in eu"},
        )
        direct = await send_region_aware(
            client, "GET", f"https://us.api.lab.test{ACME_README}", token=token()
        )

    assert fetched.status_code == 200
    assert fetched.request.url.host == "eu.api.lab.test"
    assert fetched.json()["name"] == "orgs/globex/projects/web/documents/readme"
    assert created.status_code == 201
    assert created.json() == {"name": "orgs/globex/projects/web/documents/notes"}
    assert world.store.rows["globex", "web", "notes"] == "written in eu"
    assert direct.status_code == 200
    assert direct.request.url.host == "us.api.lab.test"
    assert elsewhere.hosts == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        f"https://evil.test{GLOBEX_README}",
        f"http://eu.api.lab.test{GLOBEX_README}",
        f"https://eu.api.lab.test.evil.test{GLOBEX_README}",
        f"https://eu.api.lab.test:8443{GLOBEX_README}",
        f"https://user:pass@eu.api.lab.test{GLOBEX_README}",
        None,
    ],
    ids=[
        "unknown-host",
        "scheme-downgrade",
        "lookalike-suffix",
        "other-port",
        "userinfo",
        "missing-location",
    ],
)
async def test_region_aware_client_refuses_a_redirect_outside_the_regional_origins(
    location: str | None,
) -> None:
    world = make_world()
    headers = [] if location is None else [(b"location", location.encode())]
    hostile = Scripted(status=307, headers=headers)
    client, elsewhere = two_cell_client(world, follow_redirects=False, us=hostile)

    async with client:
        with pytest.raises(UntrustedRedirectError):
            await send_region_aware(
                client, "GET", f"https://us.api.lab.test{GLOBEX_README}", token=token(org="globex")
            )

    assert hostile.hosts == ["us.api.lab.test"]
    assert elsewhere.hosts == []
    assert world.recorder.handler_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [ACME_README, "/healthz"])
async def test_host_naming_another_cell_is_421_before_authentication(path: str) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get(path, headers={"Host": "eu.api.lab.test"})

    assert response.status_code == 421
    assert response.json()["type"] == "https://api.lab.test/problems/misdirected-request"
    assert "www-authenticate" not in response.headers
    assert world.recorder.handler_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["US.API.LAB.TEST", "us.api.lab.test:443"])
async def test_public_path_needs_no_token_and_host_match_ignores_case_and_default_port(
    host: str,
) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get("/healthz", headers={"Host": host})

    assert response.status_code == 200
    assert response.json() == {"region": "us"}


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["us.api.lab.test:8443", "us.api.lab.test.evil.test", ""])
async def test_host_with_another_port_or_suffix_is_421(host: str) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get("/healthz", headers={"Host": host})

    assert response.status_code == 421


@pytest.mark.unit
@pytest.mark.asyncio
async def test_percent_encoded_slash_is_decoded_before_the_guard_and_the_router_see_it() -> None:
    world = make_world()
    spy = Spy(world.us)

    async with client_for(spy) as client:
        own = await client.get(
            "/v1/orgs/acme%2Fprojects%2Fweb/documents/readme", headers=bearer(token())
        )
        foreign = await client.get(
            "/v1/orgs/globex%2Fprojects%2Fweb/documents/readme", headers=bearer(token())
        )
        twice = await client.get(
            "/v1/orgs/acme%252Fprojects%252Fweb/documents/readme", headers=bearer(token())
        )

    assert spy.scopes[0]["raw_path"] == b"/v1/orgs/acme%2Fprojects%2Fweb/documents/readme"
    assert spy.scopes[0]["path"] == ACME_README
    assert own.status_code == 200
    assert own.json()["name"] == "orgs/acme/projects/web/documents/readme"
    assert (foreign.status_code, foreign.content) == (403, DENIED)
    assert spy.scopes[2]["path"] == "/v1/orgs/acme%2Fprojects%2Fweb/documents/readme"
    assert (twice.status_code, twice.content) == (403, DENIED)
    assert world.recorder.handler_calls == ["get_document"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projects", "expected"),
    [
        (("web",), ["orgs/acme/projects/web/documents/readme"]),
        (
            ("web", "ml"),
            [
                "orgs/acme/projects/ml/documents/weights",
                "orgs/acme/projects/web/documents/readme",
            ],
        ),
        ((), []),
    ],
    ids=["one-project", "two-projects", "no-projects"],
)
async def test_wildcard_project_lists_only_the_projects_in_the_token(
    projects: tuple[str, ...], expected: list[str]
) -> None:
    world = make_world()

    async with client_for(world.us) as client:
        listed = await client.get(
            "/v1/orgs/acme/projects/-/documents", headers=bearer(token(projects=projects))
        )

    assert listed.status_code == 200
    assert listed.json() == {"documents": expected}
    assert world.recorder.handler_calls == ["list_across_projects"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_selector_header_chooses_among_token_projects_and_never_grants_one() -> None:
    world = make_world()
    both = bearer(token(projects=("web", "ml")))

    async with client_for(world.us) as client:
        chosen = await client.get("/v1/documents/weights", headers={**both, "Lab-Project": "ml"})
        ambiguous = await client.get("/v1/documents/weights", headers=both)
        ungranted = await client.get(
            "/v1/documents/readme", headers={**bearer(token()), "Lab-Project": "ml"}
        )
        defaulted = await client.get("/v1/documents/readme", headers=bearer(token()))
        missing = await client.get("/v1/documents/nosuch", headers=bearer(token()))

    assert chosen.status_code == 200
    assert chosen.json()["name"] == "orgs/acme/projects/ml/documents/weights"
    assert ambiguous.status_code == 400
    assert ambiguous.json()["type"] == "https://api.lab.test/problems/ambiguous-project"
    assert (ungranted.status_code, ungranted.content) == (403, DENIED)
    assert defaulted.status_code == 200
    assert defaulted.json()["name"] == "orgs/acme/projects/web/documents/readme"
    assert missing.status_code == 404
    assert world.recorder.store_reads == [
        ("acme", "ml", "weights"),
        ("acme", "web", "readme"),
        ("acme", "web", "nosuch"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_selector_route_is_region_routed_by_the_token_org() -> None:
    world = make_world()

    async with client_for(world.us) as client:
        response = await client.get("/v1/documents/readme", headers=bearer(token(org="globex")))

    assert response.status_code == 307
    assert response.headers["location"] == "https://eu.api.lab.test/v1/documents/readme"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_project_and_document_ids_in_two_orgs_never_collide() -> None:
    world = make_world()

    async with client_for(world.us) as us, client_for(world.eu, "eu") as eu:
        acme = await us.get(ACME_README, headers=bearer(token(org="acme")))
        globex = await eu.get(GLOBEX_README, headers=bearer(token(org="globex")))

    assert acme.json()["body"] == "acme web readme"
    assert globex.json()["body"] == "globex web readme"
    assert world.recorder.store_reads == [("acme", "web", "readme"), ("globex", "web", "readme")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_stores_under_the_token_org_validates_the_id_and_reports_conflicts() -> None:
    world = make_world()
    collection = "/v1/orgs/acme/projects/web/documents"

    async with client_for(world.us) as client:
        created = await client.post(
            collection, headers=bearer(token()), json={"document_id": "notes", "body": "first"}
        )
        duplicate = await client.post(
            collection, headers=bearer(token()), json={"document_id": "notes", "body": "second"}
        )
        invalid = await client.post(
            collection, headers=bearer(token()), json={"document_id": "Bad_ID", "body": "x"}
        )
        extra = await client.post(
            collection,
            headers=bearer(token()),
            json={"document_id": "other", "body": "x", "org": "globex"},
        )

    assert created.status_code == 201
    assert created.json() == {"name": "orgs/acme/projects/web/documents/notes"}
    assert duplicate.status_code == 409
    assert duplicate.json()["type"] == "https://api.lab.test/problems/already-exists"
    assert invalid.status_code == 422
    assert extra.status_code == 422
    assert world.store.rows["acme", "web", "notes"] == "first"
    assert [key for key in world.store.rows if key[2] in {"Bad_ID", "other"}] == []
    assert world.recorder.handler_calls == ["create_document", "create_document"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "directory",
    [{"acme": "us"}, {"acme": "us", "ghost": "mars"}],
    ids=["org-not-in-directory", "home-region-unknown"],
)
async def test_org_without_a_routable_home_region_is_denied_closed(
    directory: Mapping[str, str],
) -> None:
    world = make_world(directory=directory)

    async with client_for(world.us) as client:
        response = await client.get(
            "/v1/orgs/ghost/projects/web/documents/readme", headers=bearer(token(org="ghost"))
        )

    assert (response.status_code, response.content) == (403, DENIED)
    assert world.recorder.handler_calls == []


@pytest.mark.unit
def test_current_tenant_fails_closed_when_no_guard_ran() -> None:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    with pytest.raises(RuntimeError, match="tenant guard did not run"):
        current_tenant(request)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path_params", "allowed"),
    [
        ({}, True),
        ({"org": "acme"}, True),
        ({"org": "acme", "project": "web"}, True),
        ({"org": "globex", "project": "web"}, False),
        ({"org": "acme", "project": "ml"}, False),
        ({"org": "acme", "project": "-"}, False),
    ],
    ids=["flat", "org", "org-project", "other-org", "unlisted-project", "wildcard"],
)
def test_current_tenant_rechecks_the_router_parameters_against_the_context(
    path_params: dict[str, str], allowed: bool
) -> None:
    ctx = TenantContext(subject="user-1", org="acme", projects=frozenset({"web"}))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "state": {"tenant": ctx},
            "path_params": path_params,
        }
    )

    if allowed:
        assert current_tenant(request) is ctx
    else:
        with pytest.raises(GuardRejectedError) as raised:
            current_tenant(request)
        assert bytes(raised.value.response.body) == DENIED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_the_guard_middleware() -> None:
    world = make_world()

    async with LifespanManager(world.us) as manager, client_for(manager.app) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("a", True),
        ("acme", True),
        ("a-1", True),
        ("a" * 63, True),
        ("a" * 64, False),
        ("-", False),
        ("a-", False),
        ("1a", False),
        ("Acme", False),
        ("a_b", False),
        ("a/b", False),
        ("a%2Fb", False),
        ("", False),
        ("a\n", False),
    ],
)
def test_resource_id_grammar_is_the_aip_122_lowercase_label(value: str, valid: bool) -> None:
    assert (RESOURCE_ID.fullmatch(value) is not None) is valid


@pytest.mark.unit
def test_lab_key_is_long_enough_for_hs256() -> None:
    assert len(LAB_HS256_KEY) >= 32


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("enforcement", ["middleware", "dependency"])
async def test_guard_matches_the_route_path_when_the_app_is_mounted_under_a_root_path(
    enforcement: Enforcement,
) -> None:
    world = make_world(enforcement)
    transport = httpx.ASGITransport(app=world.us, root_path="/api")

    async with httpx.AsyncClient(transport=transport, base_url="https://us.api.lab.test") as client:
        own = await client.get("/api" + ACME_README, headers=bearer(token()))
        unlisted = await client.get(
            "/api/v1/orgs/acme/projects/ml/documents/weights", headers=bearer(token())
        )
        foreign = await client.get("/api" + GLOBEX_README, headers=bearer(token()))
        public = await client.get("/api/healthz")

    assert own.status_code == 200
    assert (unlisted.status_code, unlisted.content) == (403, DENIED)
    assert (foreign.status_code, foreign.content) == (403, DENIED)
    assert public.status_code == 200
    assert world.recorder.handler_calls == ["get_document"]
    assert world.recorder.store_reads == [("acme", "web", "readme")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wildcard_project_is_denied_on_every_route_except_the_documented_list() -> None:
    world = make_world()
    spy = Spy(world.us)
    headers = bearer(token(projects=("web",)))

    async with client_for(spy) as client:
        written = await client.post(
            "/v1/orgs/acme/projects/-/documents",
            headers=headers,
            json={"document_id": "planted", "body": "x"},
        )
        fetched = await client.get("/v1/orgs/acme/projects/-/documents/readme", headers=headers)

    assert (written.status_code, written.content) == (403, DENIED)
    assert (fetched.status_code, fetched.content) == (403, DENIED)
    assert spy.body_bytes == 0
    assert ("acme", "-", "planted") not in world.store.rows
    assert world.recorder.handler_calls == []
    assert world.recorder.store_reads == []
