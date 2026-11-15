"""BOPLA / mass assignment (API3:2023): exploit the auto-binding API, then prove each control."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from api_labs.f15_api_authorization_and_owasp_security.bopla_mass_assignment import (
    PLACEHOLDER_HASH,
    UserRow,
    create_secure_app,
    create_vulnerable_app,
    make_engine,
)

ATTACK = {"display_name": "mallory", "role": "admin", "balance_cents": 99999900}
MALLORY = {"X-User-Id": "3"}
ROOT = {"X-User-Id": "1"}


def stored(engine: Engine, user_id: int) -> tuple[str, int, str, str | None]:
    with Session(engine) as session:
        row = session.get_one(UserRow, user_id)
        return row.role, row.balance_cents, row.display_name, row.bio


@pytest.fixture
def engine() -> Engine:
    return make_engine()


def asgi_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def vulnerable(engine: Engine) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(create_vulnerable_app(engine)) as client:
        yield client


@pytest_asyncio.fixture
async def secure(engine: Engine) -> AsyncIterator[httpx.AsyncClient]:
    async with asgi_client(create_secure_app(engine)) as client:
        yield client


# --- The exploit -------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vulnerable_patch_escalates_role_and_balance(
    vulnerable: httpx.AsyncClient, engine: Engine
) -> None:
    response = await vulnerable.patch("/users/me", json=ATTACK, headers=MALLORY)
    assert response.status_code == 200
    assert stored(engine, 3) == ("admin", 99999900, "mallory", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vulnerable_read_exposes_another_users_private_properties(
    vulnerable: httpx.AsyncClient,
) -> None:
    body = (await vulnerable.get("/users/2", headers=MALLORY)).json()
    assert body["email"] == "alice@example.test"
    assert body["balance_cents"] == 500
    assert body["password_hash"] == PLACEHOLDER_HASH


@pytest.mark.unit
@pytest.mark.asyncio
async def test_vulnerable_filter_on_hidden_property_enumerates_admins(
    vulnerable: httpx.AsyncClient,
) -> None:
    response = await vulnerable.get("/users", params={"role": "admin"}, headers=MALLORY)
    assert response.json() == [1]


# --- The fix: write side -----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_secure_patch_rejects_every_undeclared_key_and_writes_nothing(
    secure: httpx.AsyncClient, engine: Engine
) -> None:
    response = await secure.patch("/users/me", json=ATTACK, headers=MALLORY)
    assert response.status_code == 422
    errors = [(e["type"], e["loc"], e["input"]) for e in response.json()["detail"]]
    assert errors == [
        ("extra_forbidden", ["body", "role"], "admin"),
        ("extra_forbidden", ["body", "balance_cents"], 99999900),
    ]
    assert stored(engine, 3) == ("user", 0, "mallory", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_secure_patch_writes_only_the_keys_that_were_sent(
    secure: httpx.AsyncClient, engine: Engine
) -> None:
    await secure.patch("/users/me", json={"bio": "hi"}, headers=MALLORY)
    response = await secure.patch("/users/me", json={"bio": None}, headers=MALLORY)
    assert response.status_code == 200
    assert response.json() == {
        "id": 3,
        "display_name": "mallory",
        "bio": None,
        "email": "mallory@example.test",
        "role": "user",
        "balance_cents": 0,
    }
    assert stored(engine, 3) == ("user", 0, "mallory", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_secure_patch_rejects_explicit_null_for_non_nullable_property(
    secure: httpx.AsyncClient, engine: Engine
) -> None:
    response = await secure.patch("/users/me", json={"display_name": None}, headers=MALLORY)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "display_name"]
    assert stored(engine, 3)[2] == "mallory"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_duplicate_json_keys_resolve_last_wins_before_validation(
    secure: httpx.AsyncClient, engine: Engine
) -> None:
    raw = b'{"bio": "first", "bio": "second"}'
    headers = {**MALLORY, "Content-Type": "application/json"}
    response = await secure.patch("/users/me", content=raw, headers=headers)
    assert response.json()["bio"] == "second"
    assert stored(engine, 3)[3] == "second"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_role_property_is_writable_only_through_the_admin_schema(
    secure: httpx.AsyncClient, engine: Engine
) -> None:
    denied = await secure.patch("/admin/users/2", json={"role": "support"}, headers=MALLORY)
    assert denied.status_code == 403
    granted = await secure.patch("/admin/users/2", json={"role": "support"}, headers=ROOT)
    assert granted.status_code == 200
    money = await secure.patch("/admin/users/2", json={"balance_cents": 1}, headers=ROOT)
    assert money.status_code == 422
    assert stored(engine, 2) == ("support", 500, "alice", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unknown_query_parameter_is_rejected_not_used_as_a_filter(
    secure: httpx.AsyncClient,
) -> None:
    response = await secure.get("/users", params={"role": "admin"}, headers=MALLORY)
    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert (detail["type"], detail["loc"]) == ("extra_forbidden", ["query", "role"])
    assert (await secure.get("/users", headers=MALLORY)).json() == [1, 2, 3]


# --- The fix: read side ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_secure_read_of_another_user_returns_only_public_properties(
    secure: httpx.AsyncClient,
) -> None:
    body = (await secure.get("/users/2", headers=MALLORY)).json()
    assert body == {"id": 2, "display_name": "alice", "bio": None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forbid_on_response_model_turns_a_leaked_dict_key_into_an_error(
    secure: httpx.AsyncClient,
) -> None:
    with pytest.raises(ResponseValidationError) as caught:
        await secure.get("/debug/leaky-dict", headers=MALLORY)
    leaked = sorted(str(e["loc"][-1]) for e in caught.value.errors())
    assert leaked == ["balance_cents", "email", "password_hash", "role"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openapi_publishes_closed_write_schemas_and_open_default_schemas(
    secure: httpx.AsyncClient, vulnerable: httpx.AsyncClient
) -> None:
    schemas = (await secure.get("/openapi.json")).json()["components"]["schemas"]
    assert schemas["UserSelfUpdate"]["additionalProperties"] is False
    assert "role" not in schemas["UserSelfUpdate"]["properties"]
    assert "role" in schemas["AdminUserUpdate"]["properties"]
    body_schema = (await vulnerable.get("/openapi.json")).json()["paths"]["/users/me"]["patch"]
    content = body_schema["requestBody"]["content"]["application/json"]["schema"]
    assert content["additionalProperties"] is True


# --- Pydantic mechanics the fix depends on -----------------------------------------------


class Profile(BaseModel):
    nickname: str | None = None


class StrictOuter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile | None = None
    preferences: dict[str, Any] | None = None


class LooseUpdate(BaseModel):
    display_name: str | None = None


class Account(BaseModel):
    role: str = "user"
    display_name: str = "mallory"


@pytest.mark.unit
def test_default_extra_ignore_drops_attack_keys_silently() -> None:
    update = LooseUpdate.model_validate(ATTACK)
    assert update.model_dump(exclude_unset=True) == {"display_name": "mallory"}
    assert update.model_extra is None


@pytest.mark.unit
def test_extra_forbid_does_not_reach_nested_models_but_runtime_override_does() -> None:
    nested = StrictOuter.model_validate({"profile": {"nickname": "m", "role": "admin"}})
    assert nested.profile == Profile(nickname="m")
    with pytest.raises(ValidationError) as caught:
        StrictOuter.model_validate({"profile": {"role": "admin"}}, extra="forbid")
    assert caught.value.errors()[0]["loc"] == ("profile", "role")


@pytest.mark.unit
def test_open_dict_field_accepts_any_property_name() -> None:
    outer = StrictOuter.model_validate({"preferences": {"role": "admin"}})
    assert outer.preferences == {"role": "admin"}


@pytest.mark.unit
def test_field_name_is_an_extra_key_when_the_field_has_an_alias() -> None:
    class Aliased(BaseModel):
        model_config = ConfigDict(extra="forbid")

        display_name: str = Field(alias="displayName")

    with pytest.raises(ValidationError) as caught:
        Aliased.model_validate({"displayName": "a", "display_name": "b"})
    assert [(e["type"], e["loc"]) for e in caught.value.errors()] == [
        ("extra_forbidden", ("display_name",))
    ]


@pytest.mark.unit
def test_model_copy_update_and_model_construct_skip_validation() -> None:
    copied = Account().model_copy(update={"role": "admin", "is_staff": True})
    assert copied.role == "admin"
    assert copied.__dict__["is_staff"] is True
    assert Account.model_construct(role="admin").role == "admin"


@pytest.mark.unit
def test_extra_allow_stores_and_re_serializes_injected_keys() -> None:
    class Permissive(BaseModel):
        model_config = ConfigDict(extra="allow")

        display_name: str | None = None

    echoed = Permissive.model_validate(ATTACK)
    assert echoed.model_dump() == ATTACK


@pytest.mark.unit
def test_subclass_instance_serializes_as_declared_type_unless_polymorphic() -> None:
    class Public(BaseModel):
        id: int

    class Private(Public):
        password_hash: str

    class Envelope(BaseModel):
        user: Public

    envelope = Envelope(user=Private(id=1, password_hash=PLACEHOLDER_HASH))
    assert envelope.model_dump() == {"user": {"id": 1}}
    leaked = {"user": {"id": 1, "password_hash": PLACEHOLDER_HASH}}
    assert envelope.model_dump(polymorphic_serialization=True) == leaked
    assert envelope.model_dump(serialize_as_any=True) == leaked
