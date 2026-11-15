"""Bind parameters protect values, not identifiers: prove the hole and the allowlist fix."""

from __future__ import annotations

import sqlite3
import sys
from typing import Any

import httpx
import pytest
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy import Engine, column, func, literal_column, select, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects import mssql, mysql, postgresql
from sqlalchemy.dialects import sqlite as sqlite_dialect
from starlette.datastructures import QueryParams

from api_labs.f15_api_authorization_and_owasp_security.identifier_injection import (
    ADMIN_DIGEST,
    ResetRequest,
    SortError,
    create_app,
    extract_via_order_by,
    find_one,
    list_users_safe,
    list_users_vulnerable,
    make_engine,
    mongo_match,
    parse_sort,
    reject_operator_keys,
    users,
)

ADMIN_DIGEST_SQL = "SELECT secret_hash FROM users WHERE tenant_id = 1 AND name = 'admin'"
ISSUED_RESET = "tok-EXAMPLE-NOT-A-REAL-TOKEN-0001"


@pytest.fixture
def engine() -> Engine:
    return make_engine()


def accounts() -> list[dict[str, Any]]:
    return [
        {"email": "admin@example.test", "reset_token": ISSUED_RESET},
        {"email": "user@example.test"},
    ]


@pytest.mark.unit
def test_order_by_oracle_reads_another_tenants_secret_despite_bound_where(engine: Engine) -> None:
    def probe(sort: str) -> list[str]:
        return list_users_vulnerable(engine, 2, sort)

    assert probe("name") == ["bobXops", "bob_ops", "mallory"]  # tenant 2; BINARY: X < _
    recovered, calls = extract_via_order_by(probe, ADMIN_DIGEST_SQL, len(ADMIN_DIGEST))
    assert recovered == ADMIN_DIGEST
    assert 1 + 6 * 16 <= calls <= 1 + 7 * 16  # ceil(log2(95)) = 7 probes per character


@pytest.mark.unit
def test_stacked_statement_is_refused_but_the_oracle_never_needed_it(engine: Engine) -> None:
    with pytest.raises(sa_exc.ProgrammingError, match="one statement at a time"):
        list_users_vulnerable(engine, 2, "name; DROP TABLE users")
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(users)).scalar_one() == 4


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "secret_hash",
        "id",
        "CASE WHEN 1 THEN id END",
        "name; DROP TABLE users",
        "name,-name",
        "name,created_at,name,created_at",
        "",
    ],
)
def test_allowlist_rejects_everything_it_does_not_name(raw: str) -> None:
    with pytest.raises(SortError):
        parse_sort(raw)


@pytest.mark.unit
def test_allowlist_maps_keys_to_columns_and_appends_a_tiebreaker(engine: Engine) -> None:
    stmt = select(users.c.name).order_by(*parse_sort("-created_at,name"))
    assert str(stmt).endswith("ORDER BY users.created_at DESC, users.name ASC, users.id ASC")
    assert list_users_safe(engine, 2, "-created_at") == ["bobXops", "mallory", "bob_ops"]


@pytest.mark.unit
def test_bind_parameter_in_order_by_sorts_by_a_constant() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO t (name) VALUES (?)", [("c",), ("a",), ("b",)])
    by_name = conn.execute("SELECT name FROM t ORDER BY name").fetchall()
    by_param = conn.execute("SELECT name FROM t ORDER BY ?", ("name",)).fetchall()
    assert by_name == [("a",), ("b",), ("c",)]
    assert by_param == [("c",), ("a",), ("b",)]  # no error, and no sort either
    conn.close()


@pytest.mark.unit
def test_unknown_quoted_identifier_is_a_string_literal_until_dqs_is_off() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO t (name) VALUES (?)", [("c",), ("a",)])
    assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML) is True
    assert conn.execute('SELECT name FROM t ORDER BY "nmae"').fetchall() == [("c",), ("a",)]
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        conn.execute('SELECT name FROM t ORDER BY "nmae"')
    conn.close()


@pytest.mark.unit
def test_quoting_stops_syntax_injection_but_not_a_secret_column_choice(engine: Engine) -> None:
    payload = 'name" DESC, (SELECT 1) --'
    quoted = select(users.c.name).where(users.c.tenant_id == 2).order_by(column(payload))
    assert 'ORDER BY "name"" DESC, (SELECT 1) --"' in str(quoted)
    by_secret = select(users.c.name).where(users.c.tenant_id == 2).order_by("secret_hash")
    with engine.connect() as conn:
        # Unknown quoted name -> SQLite string literal -> constant sort, insertion order.
        assert list(conn.execute(quoted).scalars()) == ["mallory", "bob_ops", "bobXops"]
        # A real column name is not injection, yet it leaks the secret's order: 111 < 777 < eee.
        assert list(conn.execute(by_secret).scalars()) == ["bob_ops", "mallory", "bobXops"]


@pytest.mark.unit
def test_sqlalchemy_refuses_plain_string_sql_in_order_by_but_not_text() -> None:
    payload = "name; DROP TABLE users"
    with pytest.raises(sa_exc.CompileError, match="Can't resolve label reference"):
        str(select(users.c.name).order_by(payload))
    with pytest.raises(sa_exc.ArgumentError, match="should be explicitly declared as text"):
        select(users.c.name).where(payload)  # type: ignore[arg-type]
    assert str(select(users.c.name).order_by(text(payload))).endswith(payload)
    assert str(select(users.c.name).order_by(literal_column(payload))).endswith(payload)


@pytest.mark.unit
def test_dialects_quote_identifiers_by_doubling_their_own_delimiter() -> None:
    name = 'a"b`c]d'
    quoted = {
        d.name: d.identifier_preparer.quote_identifier(name)
        for d in (
            postgresql.dialect(),  # type: ignore[no-untyped-call]
            sqlite_dialect.dialect(),
            mysql.dialect(),
            mssql.dialect(),  # type: ignore[no-untyped-call]
        )
    }
    assert quoted == {
        "postgresql": '"a""b`c]d"',
        "sqlite": '"a""b`c]d"',
        "mysql": '`a"b``c]d`',
        "mssql": '[a"b`c]]d]',
    }


@pytest.mark.unit
def test_psycopg_composes_identifiers_and_template_strings() -> None:
    hostile = 'x"; DROP TABLE users; --'
    composed = sql.SQL("SELECT id FROM users ORDER BY {} {}").format(
        sql.Identifier(hostile), sql.SQL("DESC")
    )
    assert (
        composed.as_string(None) == 'SELECT id FROM users ORDER BY "x""; DROP TABLE users; --" DESC'
    )
    if sys.version_info >= (3, 14):
        from string.templatelib import Interpolation, Template

        template = Template(
            "SELECT id FROM users WHERE id = ",
            Interpolation(5, "user_id", None, ""),
            " ORDER BY ",
            Interpolation(hostile, "sort", None, "i"),
        )
        assert sql.as_string(template) == (
            'SELECT id FROM users WHERE id = 5 ORDER BY "x""; DROP TABLE users; --"'
        )
    else:
        with pytest.raises(ImportError):
            __import__("string.templatelib")


@pytest.mark.unit
def test_like_wildcards_in_bound_values_match_until_escaped(engine: Engine) -> None:
    raw = select(users.c.name).where(users.c.name.contains("bob_ops")).order_by(users.c.id)
    escaped = select(users.c.name).where(users.c.name.contains("bob_ops", autoescape=True))
    with engine.connect() as conn:
        assert list(conn.execute(raw).scalars()) == ["bob_ops", "bobXops"]
        assert list(conn.execute(escaped).scalars()) == ["bob_ops"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_v0_leaks_a_bit_per_request_and_v1_refuses(engine: Engine) -> None:
    app = create_app(engine, accounts())
    transport = httpx.ASGITransport(app=app)
    first_char = f"substr(({ADMIN_DIGEST_SQL}), 1, 1)"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url = "/v0/tenants/2/users"
        ascending = (await client.get(url, params={"sort": "id"})).json()
        yes = f"CASE WHEN {first_char} = 'c' THEN id ELSE -id END"
        no = f"CASE WHEN {first_char} = 'd' THEN id ELSE -id END"
        assert (await client.get(url, params={"sort": yes})).json() == ascending
        assert (await client.get(url, params={"sort": no})).json() == ascending[::-1]

        url = "/v1/tenants/2/users"
        shape = await client.get(url, params={"sort": yes})
        assert shape.status_code == 422
        secret = await client.get(url, params={"sort": "secret_hash"})
        assert secret.status_code == 400
        assert secret.json() == {"detail": "unsupported sort field 'secret_hash'"}
        ok = await client.get(url, params={"sort": "-created_at"})
        assert ok.json() == ["bobXops", "mallory", "bob_ops"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_object_skips_the_token_check_unless_the_schema_is_typed(
    engine: Engine,
) -> None:
    app = create_app(engine, accounts())
    transport = httpx.ASGITransport(app=app)
    attack = {"email": "admin@example.test", "reset_token": {"$ne": None}}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        v0 = await client.post("/v0/password-reset", json=attack)
        assert v0.json() == {"reset": "admin@example.test"}
        v1 = await client.post("/v1/password-reset", json=attack)
        assert v1.status_code == 422
        assert v1.json()["detail"][0]["type"] == "string_type"
        good = {"email": "admin@example.test", "reset_token": ISSUED_RESET}
        assert (await client.post("/v1/password-reset", json=good)).json() == {
            "reset": "admin@example.test"
        }


@pytest.mark.unit
def test_ne_matches_documents_missing_the_field_and_guards_reject_operators() -> None:
    assert mongo_match({"email": "user@example.test"}, {"reset_token": {"$ne": "x"}})
    assert find_one(accounts(), {"reset_token": {"$ne": ISSUED_RESET}}) == "user@example.test"
    assert find_one(accounts(), {"email": {"$regex": "^adm"}}) == "admin@example.test"
    with pytest.raises(ValueError, match=r"operator or dotted key '\$ne'"):
        reject_operator_keys({"filter": [{"reset_token": {"$ne": None}}]})
    with pytest.raises(ValueError, match=r"dotted key 'profile\.role'"):
        reject_operator_keys({"profile.role": "admin"})
    reject_operator_keys({"email": "a@example.test", "tags": ["x", {"k": 1}]})
    with pytest.raises(ValidationError):
        ResetRequest.model_validate({"email": "a@example.test", "reset_token": 1234567890123456})


@pytest.mark.unit
def test_starlette_query_strings_never_build_nested_objects() -> None:
    params = QueryParams("email[$ne]=x&reset_token[$gt]=")
    assert dict(params) == {"email[$ne]": "x", "reset_token[$gt]": ""}
