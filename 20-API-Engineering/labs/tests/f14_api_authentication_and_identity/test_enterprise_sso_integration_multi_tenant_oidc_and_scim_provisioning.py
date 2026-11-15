"""Multi-tenant issuer validation and SCIM 2.0 deprovisioning, proven in-process."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import ec

from api_labs.f14_api_authentication_and_identity.enterprise_sso_scim import (
    ENTRA_V2_ISSUER_TEMPLATE,
    PATCH_OP_SCHEMA,
    Directory,
    IssuerRegistry,
    SigningKey,
    TenantConnection,
    TokenRejectedError,
    create_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

OKTA_ISSUER = "https://acme.okta.example/oauth2/default"
GLOBEX_TID = "6f1c9a52-0d0b-4a43-9c1e-5c1b0e7d2a11"
ATTACKER_TID = "0b5d2c7e-31aa-4f0e-8c47-7c2f3d9e4b60"
MSA_TID = "9188040d-6c67-4c5b-b112-36a304b66dad"
GLOBEX_ISSUER = ENTRA_V2_ISSUER_TEMPLATE.replace("{tenantid}", GLOBEX_TID)
SCIM_TOKENS = {"acme": "scim-acme-EXAMPLE-NOT-A-REAL-TOKEN", "globex": "scim-globex-EXAMPLE-TOKEN"}

_KEYS = {name: ec.generate_private_key(ec.SECP256R1()) for name in ("okta", "common", "msa")}


@dataclass
class World:
    client: httpx.AsyncClient
    directory: Directory
    registry: IssuerRegistry


def mint(key: str, claims: dict[str, Any], *, ttl: int = 600) -> str:
    now = int(time.time())
    body = {"iat": now, "exp": now + ttl, **claims}
    return jwt.encode(body, _KEYS[key], algorithm="ES256", headers={"kid": f"kid-{key}"})


def okta_token(sub: str, aud: str = "acme-client", **extra: Any) -> str:
    return mint("okta", {"iss": OKTA_ISSUER, "aud": aud, "sub": sub, **extra})


def entra_token(tid: str, oid: str, aud: str = "globex-client", **extra: Any) -> str:
    iss = ENTRA_V2_ISSUER_TEMPLATE.replace("{tenantid}", tid)
    return mint(
        "common",
        {"iss": iss, "aud": aud, "sub": f"pairwise-{oid}", "tid": tid, "oid": oid, **extra},
    )


def scim_headers(tenant: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {SCIM_TOKENS[tenant]}", **extra}


def deactivate_body(shape: str) -> dict[str, Any]:
    ops = {
        "okta": {"op": "replace", "value": {"active": False}},
        "entra-legacy": {"op": "Replace", "path": "active", "value": "False"},
        "entra-aadOptscim062020": {"op": "replace", "path": "active", "value": False},
    }
    return {"schemas": [PATCH_OP_SCHEMA], "Operations": [ops[shape]]}


def build_registry() -> IssuerRegistry:
    keys = [
        SigningKey("kid-okta", _KEYS["okta"].public_key(), OKTA_ISSUER),
        SigningKey("kid-common", _KEYS["common"].public_key(), ENTRA_V2_ISSUER_TEMPLATE),
        SigningKey(
            "kid-msa",
            _KEYS["msa"].public_key(),
            ENTRA_V2_ISSUER_TEMPLATE.replace("{tenantid}", MSA_TID),
        ),
    ]
    connections = [
        TenantConnection("acme", OKTA_ISSUER, "acme-client", "api://acme"),
        TenantConnection("globex", GLOBEX_ISSUER, "globex-client", "api://globex", "oid"),
    ]
    return IssuerRegistry(keys, connections)


@pytest_asyncio.fixture
async def world() -> AsyncIterator[World]:
    directory = Directory()
    registry = build_registry()
    hashes = {hashlib.sha256(t.encode()).hexdigest(): name for name, t in SCIM_TOKENS.items()}
    app = create_app(registry, directory, hashes)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield World(client, directory, registry)


async def provision(w: World, tenant: str, user_name: str, external_id: str) -> str:
    resp = await w.client.post(
        "/scim/v2/Users",
        json={"schemas": [], "userName": user_name, "externalId": external_id, "active": True},
        headers=scim_headers(tenant),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def login(w: World, tenant: str, id_token: str) -> httpx.Response:
    return await w.client.post(f"/t/{tenant}/sso/callback", json={"id_token": id_token})


# --------------------------------------------------------------------------- issuer validation


@pytest.mark.unit
def test_exact_issuer_accepted_and_bound_to_its_tenant() -> None:
    registry = build_registry()
    principal = registry.validate(okta_token("00u-alice"), tenant="acme", audience="acme-client")
    assert (principal.tenant, principal.issuer, principal.subject) == (
        "acme",
        OKTA_ISSUER,
        "00u-alice",
    )
    with pytest.raises(TokenRejectedError, match="InvalidIssuerError"):
        registry.validate(okta_token("00u-alice"), tenant="globex", audience="acme-client")


@pytest.mark.unit
def test_pyjwt_issuer_none_silently_skips_the_issuer_check() -> None:
    foreign = entra_token(ATTACKER_TID, "attacker-oid", aud="api://globex")
    key = _KEYS["common"].public_key()
    claims = jwt.decode(foreign, key, algorithms=["ES256"], audience="api://globex")
    assert claims["tid"] == ATTACKER_TID  # verify_iss is on by default, yet nothing was checked
    with pytest.raises(jwt.InvalidIssuerError):
        jwt.decode(
            foreign, key, algorithms=["ES256"], audience="api://globex", issuer=GLOBEX_ISSUER
        )


def _entra_variant(variant: str) -> str:
    base = {"aud": "globex-client", "sub": "s", "oid": "oid-x"}
    if variant == "foreign-tenant":  # BingBang: genuine Entra token from a tenant never onboarded
        return entra_token(ATTACKER_TID, "oid-x")
    if variant == "tid-not-guid":
        return mint("common", {**base, "iss": GLOBEX_ISSUER, "tid": "GLOBEX"})
    return mint("common", {**base, "iss": GLOBEX_ISSUER, "tid": ATTACKER_TID})  # tid-mismatch


@pytest.mark.unit
@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        ("foreign-tenant", "InvalidIssuerError"),
        ("tid-not-guid", "tid is not a GUID"),
        ("tid-mismatch", "iss does not embed tid"),
    ],
)
def test_entra_template_key_requires_onboarded_issuer_and_matching_tid(
    variant: str, reason: str
) -> None:
    registry = build_registry()
    good = entra_token(GLOBEX_TID, "oid-bob")
    assert registry.validate(good, tenant="globex", audience="globex-client").subject == "oid-bob"
    with pytest.raises(TokenRejectedError) as excinfo:
        registry.validate(_entra_variant(variant), tenant="globex", audience="globex-client")
    assert excinfo.value.reason == reason


@pytest.mark.unit
def test_tenant_pinned_key_cannot_sign_for_another_issuer() -> None:
    registry = build_registry()
    forged = mint(
        "msa",
        {"iss": GLOBEX_ISSUER, "aud": "globex-client", "sub": "s", "tid": GLOBEX_TID, "oid": "o"},
    )
    with pytest.raises(TokenRejectedError, match="key issuer mismatch"):
        registry.validate(forged, tenant="globex", audience="globex-client")


@pytest.mark.unit
def test_one_issuer_maps_to_exactly_one_tenant() -> None:
    dup = [
        TenantConnection("acme", OKTA_ISSUER, "a", "api://a"),
        TenantConnection("acme-eu", OKTA_ISSUER, "b", "api://b"),
    ]
    with pytest.raises(ValueError, match="registered twice"):
        IssuerRegistry([], dup)
    with pytest.raises(ValueError, match="concrete issuer"):
        IssuerRegistry([], [TenantConnection("x", ENTRA_V2_ISSUER_TEMPLATE, "c", "api://c")])


# --------------------------------------------------------------------------- SSO join and nOAuth


@pytest.mark.unit
@pytest.mark.asyncio
async def test_login_joins_on_immutable_subject_never_on_email(world: World) -> None:
    await provision(world, "globex", "carol@globex.example", "oid-carol")
    victim_email = {"email": "carol@globex.example", "preferred_username": "carol@globex.example"}
    attacker = entra_token(GLOBEX_TID, "oid-mallory", **victim_email)
    assert (await login(world, "globex", attacker)).json() == {"error": "not_provisioned"}
    carol = entra_token(GLOBEX_TID, "oid-carol", email="someone-else@example.com")
    assert (await login(world, "globex", carol)).status_code == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_from_one_tenant_is_rejected_on_another(world: World) -> None:
    await provision(world, "acme", "alice@acme.example", "00u-alice")
    assert (await login(world, "acme", okta_token("00u-alice"))).status_code == 200
    assert (await world.client.get("/t/acme/me")).status_code == 200
    assert (await world.client.get("/t/globex/me")).status_code == 401


# --------------------------------------------------------------------------- SCIM deprovisioning


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["okta", "entra-legacy", "entra-aadOptscim062020"])
async def test_every_idp_deactivation_shape_revokes_sessions_before_responding(
    world: World, shape: str
) -> None:
    user_id = await provision(world, "acme", "alice@acme.example", "00u-alice")
    assert (await login(world, "acme", okta_token("00u-alice"))).status_code == 200
    assert (await world.client.get("/t/acme/me")).status_code == 200

    resp = await world.client.patch(
        f"/scim/v2/Users/{user_id}", json=deactivate_body(shape), headers=scim_headers("acme")
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/scim+json"
    assert resp.json()["active"] is False
    assert world.directory.revocations == [("acme", user_id, "scim active=false", 1)]
    assert (await world.client.get("/t/acme/me")).status_code == 401
    assert (await login(world, "acme", okta_token("00u-alice"))).json() == {"error": "deactivated"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unexpired_idp_access_token_is_refused_after_deprovisioning(world: World) -> None:
    user_id = await provision(world, "globex", "bob@globex.example", "oid-bob")
    bearer = {"Authorization": f"Bearer {entra_token(GLOBEX_TID, 'oid-bob', aud='api://globex')}"}
    assert (await world.client.get("/api/whoami", headers=bearer)).json()["tenant"] == "globex"
    resp = await world.client.patch(
        f"/scim/v2/Users/{user_id}",
        json=deactivate_body("entra-legacy"),
        headers=scim_headers("globex"),
    )
    assert resp.status_code == 200
    assert (await world.client.get("/api/whoami", headers=bearer)).status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_patch_is_atomic_and_rejects_non_boolean_active(world: World) -> None:
    user_id = await provision(world, "acme", "alice@acme.example", "00u-alice")
    await login(world, "acme", okta_token("00u-alice"))
    body = {
        "schemas": [PATCH_OP_SCHEMA],
        "Operations": [
            {"op": "replace", "path": "active", "value": False},
            {"op": "replace", "path": "nickName", "value": "Al"},
        ],
    }
    resp = await world.client.patch(
        f"/scim/v2/Users/{user_id}", json=body, headers=scim_headers("acme")
    )
    assert (resp.status_code, resp.json()["scimType"]) == (400, "invalidPath")
    assert world.directory.users[("acme", user_id)].active is True
    assert (await world.client.get("/t/acme/me")).status_code == 200

    for bad in (0, "no", None):
        ops = {
            "schemas": [PATCH_OP_SCHEMA],
            "Operations": [{"op": "replace", "path": "active", "value": bad}],
        }
        resp = await world.client.patch(
            f"/scim/v2/Users/{user_id}", json=ops, headers=scim_headers("acme")
        )
        assert resp.json() == {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": "400",
            "scimType": "invalidValue",
            "detail": f"active must be a boolean, got {bad!r}",
        }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_if_match_with_a_stale_etag_returns_412(world: World) -> None:
    user_id = await provision(world, "acme", "alice@acme.example", "00u-alice")
    first = await world.client.get(f"/scim/v2/Users/{user_id}", headers=scim_headers("acme"))
    stale = first.headers["etag"]
    rename = {
        "schemas": [PATCH_OP_SCHEMA],
        "Operations": [{"op": "replace", "path": "displayName", "value": "A"}],
    }
    ok = await world.client.patch(
        f"/scim/v2/Users/{user_id}",
        json=rename,
        headers=scim_headers("acme", **{"If-Match": stale}),
    )
    assert (ok.status_code, ok.headers["etag"]) == (200, 'W/"2"')
    again = await world.client.patch(
        f"/scim/v2/Users/{user_id}",
        json=deactivate_body("okta"),
        headers=scim_headers("acme", **{"If-Match": stale}),
    )
    assert again.status_code == 412
    assert world.directory.users[("acme", user_id)].active is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scim_credential_selects_the_tenant_and_hides_other_tenants(world: World) -> None:
    acme_user = await provision(world, "acme", "alice@acme.example", "00u-alice")
    await login(world, "acme", okta_token("00u-alice"))
    cross = await world.client.patch(
        f"/scim/v2/Users/{acme_user}", json=deactivate_body("okta"), headers=scim_headers("globex")
    )
    assert cross.status_code == 404
    assert world.directory.users[("acme", acme_user)].active is True
    assert (await world.client.get("/t/acme/me")).status_code == 200
    anon = await world.client.get(
        f"/scim/v2/Users/{acme_user}", headers={"Authorization": "Bearer wrong"}
    )
    assert (anon.status_code, anon.json()["status"]) == (401, "401")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_returns_204_then_404_and_revokes_sessions(world: World) -> None:
    user_id = await provision(world, "acme", "alice@acme.example", "00u-alice")
    await login(world, "acme", okta_token("00u-alice"))
    resp = await world.client.delete(f"/scim/v2/Users/{user_id}", headers=scim_headers("acme"))
    assert resp.status_code == 204
    gone = await world.client.get(f"/scim/v2/Users/{user_id}", headers=scim_headers("acme"))
    assert gone.status_code == 404
    assert (await world.client.get("/t/acme/me")).status_code == 401


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reactivation_does_not_resurrect_revoked_sessions(world: World) -> None:
    user_id = await provision(world, "acme", "alice@acme.example", "00u-alice")
    await login(world, "acme", okta_token("00u-alice"))
    url, hdrs = f"/scim/v2/Users/{user_id}", scim_headers("acme")
    await world.client.patch(url, json=deactivate_body("okta"), headers=hdrs)
    enable = {
        "schemas": [PATCH_OP_SCHEMA],
        "Operations": [{"op": "replace", "value": {"active": True}}],
    }
    assert (await world.client.patch(url, json=enable, headers=hdrs)).json()["active"] is True
    assert (await world.client.get("/t/acme/me")).status_code == 401
    assert (await login(world, "acme", okta_token("00u-alice"))).status_code == 200
    assert (await world.client.get("/t/acme/me")).status_code == 200


# --------------------------------------------------------------------------- groups and queries


@pytest.mark.unit
@pytest.mark.asyncio
async def test_group_removal_shapes_and_live_entitlement_check(world: World) -> None:
    hdrs = scim_headers("acme")
    alice = await provision(world, "acme", "alice@acme.example", "00u-alice")
    bob = await provision(world, "acme", "bob@acme.example", "00u-bob")
    carl = await provision(world, "acme", "carl@acme.example", "00u-carl")
    group = (
        await world.client.post("/scim/v2/Groups", json={"displayName": "Finance"}, headers=hdrs)
    ).json()["id"]

    def patch(*ops: dict[str, Any]) -> dict[str, Any]:
        return {"schemas": [PATCH_OP_SCHEMA], "Operations": list(ops)}

    members = [{"value": u} for u in (alice, bob, carl)]
    add = await world.client.patch(
        f"/scim/v2/Groups/{group}",
        json=patch({"op": "Add", "path": "members", "value": members}),
        headers=hdrs,
    )
    assert add.status_code == 204
    await login(world, "acme", okta_token("00u-alice"))
    assert (await world.client.get("/t/acme/reports")).status_code == 200

    legacy = {"op": "Remove", "path": "members", "value": [{"value": alice}]}  # pre-flag Entra
    await world.client.patch(f"/scim/v2/Groups/{group}", json=patch(legacy), headers=hdrs)
    assert world.directory.groups[("acme", group)].members == {bob, carl}
    assert (await world.client.get("/t/acme/me")).status_code == 200  # session survives
    assert (await world.client.get("/t/acme/reports")).status_code == 403  # entitlement does not

    filtered = {"op": "remove", "path": f'members[value eq "{bob}"]'}  # Okta and flagged Entra
    await world.client.patch(f"/scim/v2/Groups/{group}", json=patch(filtered), headers=hdrs)
    assert world.directory.groups[("acme", group)].members == {carl}

    await world.client.patch(
        f"/scim/v2/Groups/{group}", json=patch({"op": "remove", "path": "members"}), headers=hdrs
    )
    assert world.directory.groups[("acme", group)].members == set()

    foreign = await provision(world, "globex", "eve@globex.example", "oid-eve")
    inject = patch({"op": "add", "path": "members", "value": [{"value": foreign}]})
    resp = await world.client.patch(f"/scim/v2/Groups/{group}", json=inject, headers=hdrs)
    assert (resp.status_code, resp.json()["scimType"]) == (400, "invalidValue")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_filter_and_pagination_follow_rfc_7644(world: World) -> None:
    hdrs = scim_headers("acme")
    for i in range(3):
        await provision(world, "acme", f"User{i}@Acme.example", f"00u-{i}")
    await provision(world, "globex", "user0@acme.example", "oid-0")  # same name, other tenant

    found = await world.client.get(
        "/scim/v2/Users", params={"filter": 'userName eq "user0@acme.example"'}, headers=hdrs
    )
    assert (found.json()["totalResults"], found.json()["Resources"][0]["externalId"]) == (
        1,
        "00u-0",
    )
    empty = await world.client.get(
        "/scim/v2/Users", params={"filter": 'externalId eq "00U-0"'}, headers=hdrs
    )
    assert empty.json()["totalResults"] == 0  # externalId is caseExact

    page = await world.client.get(
        "/scim/v2/Users", params={"startIndex": "0", "count": "2"}, headers=hdrs
    )
    assert (
        page.json()["startIndex"],
        page.json()["itemsPerPage"],
        page.json()["totalResults"],
    ) == (1, 2, 3)
    counted = await world.client.get("/scim/v2/Users", params={"count": "-5"}, headers=hdrs)
    assert (counted.json()["itemsPerPage"], counted.json()["Resources"]) == (0, [])

    bad = await world.client.get(
        "/scim/v2/Users", params={"filter": 'userName sw "u"'}, headers=hdrs
    )
    assert (bad.status_code, bad.json()["scimType"]) == (400, "invalidFilter")
    dup = await world.client.post(
        "/scim/v2/Users", json={"userName": "USER1@acme.example"}, headers=hdrs
    )
    assert (dup.status_code, dup.json()["scimType"]) == (409, "uniqueness")
