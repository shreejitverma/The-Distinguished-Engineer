"""Lab: prepared statement caches and JSONB codecs in asyncpg, psycopg 3, and SQLAlchemy."""

from __future__ import annotations

import json
import re
import struct
from typing import Any

import psycopg
import pytest
from psycopg import pq
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb, JsonbBinaryLoader
from psycopg.types.numeric import Int8
from sqlalchemy.ext.asyncio import create_async_engine

from api_labs.f13_database_access_and_transactions_for_apis.pg_driver_caches import (
    FLUSH,
    JSONB_OID,
    SYNC,
    Execution,
    FakeAsyncpg,
    asyncpg_cache_trace,
    asyncpg_statement_name,
    asyncpg_wire_constants,
    bind_message,
    psycopg_bind,
    psycopg_prepare_trace,
    sqlalchemy_asyncpg_adapter,
    sqlalchemy_asyncpg_jsonb_codec,
    sqlalchemy_prepare,
    sqlalchemy_psycopg_jsonb_load,
)

# --- wire layout --------------------------------------------------------------------


@pytest.mark.unit
def test_sync_and_flush_match_asyncpg_wire_constants() -> None:
    assert SYNC == b"S\x00\x00\x00\x04"
    assert asyncpg_wire_constants() == (SYNC, FLUSH)


@pytest.mark.unit
def test_bind_all_binary_shorthand_is_int32_0x00010001() -> None:
    msg = bind_message("__asyncpg_stmt_1__", [struct.pack("!i", 41)], all_binary=True)
    body = msg[5:]
    portal_end = body.index(b"\0")
    stmt_end = body.index(b"\0", portal_end + 1)
    after_names = body[stmt_end + 1 :]
    assert after_names[:4] == struct.pack("!i", 0x00010001)  # count 1, code 1
    assert after_names[4:6] == b"\x00\x01"  # one parameter value
    assert after_names[6:14] == b"\x00\x00\x00\x04\x00\x00\x00)"  # int4 41, big-endian
    assert after_names[-4:] == b"\x00\x01\x00\x01"  # one result code: binary
    assert struct.unpack("!i", msg[1:5])[0] == len(msg) - 1


@pytest.mark.unit
def test_bind_null_is_length_minus_one_with_no_bytes() -> None:
    msg = bind_message("", [None], all_binary=False)
    assert b"\xff\xff\xff\xff" in msg
    assert len(msg) == 1 + 4 + 2 + 4 + 2 + 4 + 4


# --- psycopg parameter typing -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "oid", "fmt"),
    [
        (41, 21, 1),  # int2: -2**15 <= v < 2**15
        (70_000, 23, 1),  # int4
        (2**40, 20, 1),  # int8
        (2**70, 1700, 1),  # numeric
        ("x", 0, 0),  # text format, OID 0: the server infers the type
        (1.5, 701, 1),  # float8
        (Int8(41), 20, 1),  # explicit wrapper pins the OID regardless of magnitude
    ],
)
def test_psycopg_types_a_python_int_by_its_magnitude(value: object, oid: int, fmt: int) -> None:
    bound = psycopg_bind("select %s", [value])
    assert bound.query == b"select $1"
    assert bound.oids == (oid,)
    assert bound.formats == (fmt,)


@pytest.mark.unit
def test_psycopg_refuses_a_bare_dict_and_wraps_jsonb_with_version_byte() -> None:
    with pytest.raises(psycopg.ProgrammingError, match="cannot adapt type 'dict'"):
        psycopg_bind("select %s", [{"a": 1}])
    auto = psycopg_bind("select %s", [Jsonb({"a": 1})])
    assert (auto.oids, auto.formats, auto.values) == ((JSONB_OID,), (0,), (b'{"a": 1}',))
    binary = psycopg_bind("select %b", [Jsonb({"a": 1})])
    assert (binary.formats, binary.values) == ((1,), (b'\x01{"a": 1}',))


@pytest.mark.unit
def test_psycopg_jsonb_binary_loader_rejects_unknown_version() -> None:
    loader = Transformer().get_loader(JSONB_OID, pq.Format.BINARY)
    assert isinstance(loader, JsonbBinaryLoader)
    assert loader.load(b'\x01{"a": 1}') == {"a": 1}
    with pytest.raises(psycopg.DataError, match="unknown jsonb binary format: 2"):
        loader.load(b'\x02{"a": 1}')


# --- psycopg PrepareManager ---------------------------------------------------------


@pytest.mark.unit
def test_psycopg_prepares_on_the_sixth_execution_with_default_threshold() -> None:
    trace = psycopg_prepare_trace([Execution("select %s::int4", [41])] * 7)
    assert [s.decision for s in trace.steps] == ["NO"] * 5 + ["SHOULD", "YES"]
    assert trace.steps[5].name == "_pg3_0"
    assert trace.prepared == {"_pg3_0": "select $1::int4"}


@pytest.mark.unit
def test_psycopg_threshold_zero_prepares_first_and_none_never() -> None:
    first = psycopg_prepare_trace([Execution("select 1")] * 2, threshold=0)
    assert [s.decision for s in first.steps] == ["SHOULD", "YES"]
    never = psycopg_prepare_trace([Execution("select 1")] * 10, threshold=None)
    assert {s.decision for s in never.steps} == {"NO"}
    assert never.prepared == {}


@pytest.mark.unit
def test_psycopg_cache_key_includes_parameter_oids() -> None:
    small, large = Execution("select %s", [41]), Execution("select %s", [70_000])
    trace = psycopg_prepare_trace([small, large] * 7, threshold=1)
    assert sorted(trace.prepared) == ["_pg3_0", "_pg3_1"]
    assert set(trace.prepared.values()) == {"select $1"}


@pytest.mark.unit
def test_psycopg_prepared_max_evicts_lru_with_deallocate() -> None:
    queries = [Execution(f"select {i}") for i in range(3)]
    trace = psycopg_prepare_trace(queries, threshold=0, prepared_max=2)
    assert [s.deallocate for s in trace.steps] == [(), (), ("_pg3_0",)]
    assert sorted(trace.prepared) == ["_pg3_1", "_pg3_2"]


@pytest.mark.unit
def test_psycopg_ddl_command_status_flushes_every_prepared_statement() -> None:
    run = [Execution("select 1")] * 2 + [Execution("drop table t", command_status=b"DROP TABLE")]
    trace = psycopg_prepare_trace(run, threshold=0)
    assert trace.steps[-1].deallocate == ("DEALLOCATE ALL",)
    assert trace.prepared == {}


# --- asyncpg _StatementCache --------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncpg_cache_is_lru_and_closes_the_evicted_statement() -> None:
    trace = await asyncpg_cache_trace(["a", "b", "a", "c", "b"], max_size=2)
    assert trace.events == ["prepare a", "prepare b", "hit a", "prepare c", "prepare b"]
    assert trace.removed == ["b", "a"]
    assert trace.cached == ["c", "b"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncpg_cache_size_zero_and_oversized_queries_are_anonymous() -> None:
    off = await asyncpg_cache_trace(["q", "q"], max_size=0)
    assert off.events == ["prepare q (anonymous)", "prepare q (anonymous)"]
    big = "select " + "1" * 20
    sized = await asyncpg_cache_trace([big, big], max_cacheable_statement_size=16)
    assert sized.events == [f"prepare {big} (anonymous)"] * 2
    assert sized.cached == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncpg_cache_entries_expire_after_max_lifetime() -> None:
    trace = await asyncpg_cache_trace(["q"], max_lifetime=0.01, settle=0.1)
    assert trace.removed == ["q"]
    assert trace.cached == []


@pytest.mark.unit
def test_asyncpg_statement_names_come_from_one_process_wide_counter() -> None:
    first, second = asyncpg_statement_name(), asyncpg_statement_name()
    pattern = re.compile(r"__asyncpg_stmt_([0-9a-f]+)__")
    m1, m2 = pattern.fullmatch(first), pattern.fullmatch(second)
    assert m1 is not None
    assert m2 is not None
    assert int(m2.group(1), 16) == int(m1.group(1), 16) + 1


# --- SQLAlchemy asyncpg dialect -----------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlalchemy_asyncpg_caches_prepared_statements_per_connection() -> None:
    fake = FakeAsyncpg()
    adapter = sqlalchemy_asyncpg_adapter(fake)
    for _ in range(3):
        await sqlalchemy_prepare(adapter, "SELECT 1")
    assert fake.prepares == [("SELECT 1", None)]
    off = FakeAsyncpg()
    adapter_off = sqlalchemy_asyncpg_adapter(off, prepared_statement_cache_size=0)
    for _ in range(3):
        await sqlalchemy_prepare(adapter_off, "SELECT 1")
    assert len(off.prepares) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlalchemy_lru_grows_to_one_and_a_half_capacity_before_pruning() -> None:
    fake = FakeAsyncpg()
    adapter = sqlalchemy_asyncpg_adapter(fake, prepared_statement_cache_size=2)
    for sql in ["q1", "q2", "q3", "q3", "q1"]:
        await sqlalchemy_prepare(adapter, sql)
    assert [q for q, _ in fake.prepares] == ["q1", "q2", "q3"]  # 3 entries held for cap 2
    await sqlalchemy_prepare(adapter, "q4")  # 4 > 2 + 2 * 0.5: keep the 2 most recently used
    await sqlalchemy_prepare(adapter, "q2")
    assert [q for q, _ in fake.prepares] == ["q1", "q2", "q3", "q4", "q2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlalchemy_name_func_and_schema_invalidation() -> None:
    names = iter(["__app_a__", "__app_b__"])
    fake = FakeAsyncpg()
    adapter = sqlalchemy_asyncpg_adapter(fake, prepared_statement_name_func=lambda: next(names))
    await sqlalchemy_prepare(adapter, "SELECT 1")
    await sqlalchemy_prepare(adapter, "SELECT 1", invalidate_timestamp=4_000_000_000.0)
    assert fake.prepares == [("SELECT 1", "__app_a__"), ("SELECT 1", "__app_b__")]
    assert fake.reloads == 1


@pytest.mark.unit
def test_sqlalchemy_asyncpg_url_cache_size_is_a_dbapi_argument() -> None:
    engine = create_async_engine("postgresql+asyncpg://u@h/db?prepared_statement_cache_size=0")
    _, kwargs = engine.dialect.create_connect_args(engine.url)
    assert kwargs["prepared_statement_cache_size"] == 0
    assert "statement_cache_size" not in kwargs  # asyncpg's own cache stays at 100


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sqlalchemy_asyncpg_registers_binary_jsonb_codec_with_custom_loads() -> None:
    def loads(text: str) -> dict[str, Any]:
        return {"custom": json.loads(text)}

    engine = create_async_engine("postgresql+asyncpg://u@h/db", json_deserializer=loads)
    codec = await sqlalchemy_asyncpg_jsonb_codec(engine.dialect)
    assert (codec["schema"], codec["format"]) == ("pg_catalog", "binary")
    assert codec["encoder"]('{"a": 1}') == b'\x01{"a": 1}'
    assert codec["decoder"](b'\x01{"a": 1}') == {"custom": {"a": 1}}


@pytest.mark.unit
def test_sqlalchemy_psycopg_scopes_custom_jsonb_loads_to_the_engine() -> None:
    def loads(data: str | bytes) -> dict[str, Any]:
        return {"custom": json.loads(data)}

    engine = create_async_engine("postgresql+psycopg://u@h/db", json_deserializer=loads)
    assert type(engine.dialect).__name__ == "PGDialectAsync_psycopg"
    payload = b'\x01{"a": 1}'
    assert sqlalchemy_psycopg_jsonb_load(engine.dialect, engine.url, payload) == {
        "custom": {"a": 1}
    }
    global_loader = Transformer().get_loader(JSONB_OID, pq.Format.BINARY)
    assert global_loader.load(payload) == {"a": 1}
