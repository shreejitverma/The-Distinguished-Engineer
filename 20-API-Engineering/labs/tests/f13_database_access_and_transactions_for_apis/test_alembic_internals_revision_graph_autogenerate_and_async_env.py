"""Alembic 1.20.1 revision DAG, merge, autogenerate blind spots, and async env.py."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.script.revision import (
    CycleDetected,
    LoopDetected,
    ResolutionError,
    Revision,
    RevisionMap,
)
from alembic.util import CommandError, rev_id
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table
from sqlalchemy.exc import OperationalError

from api_labs.f13_database_access_and_transactions_for_apis.alembic_internals import (
    RevSpec,
    autogenerate_diff,
    db_state,
    exec_ddl,
    graph_summary,
    upgrade_in_running_loop,
    write_project,
)

if TYPE_CHECKING:
    from pathlib import Path

    from alembic.config import Config

DIAMOND = (
    RevSpec("base1", None, ("CREATE TABLE t0 (id INTEGER)",), ("DROP TABLE t0",)),
    RevSpec("aaaa1", "base1", ("CREATE TABLE ta (id INTEGER)",), ("DROP TABLE ta",)),
    RevSpec("bbbb1", "base1", ("CREATE TABLE tb (id INTEGER)",), ("DROP TABLE tb",)),
)
FAILING_CHAIN = (
    RevSpec("r1", None, ("CREATE TABLE t1 (id INTEGER)",)),
    RevSpec("r2", "r1", ("CREATE TABLE t2 (id INTEGER)",)),
    RevSpec("r3", "r2", ("CREATE TABLE t3 (id INTEGER)", "SELECT * FROM no_such_table")),
)


def url_of(cfg: Config) -> str:
    url = cfg.get_main_option("sqlalchemy.url")
    assert url is not None
    return url


@pytest.mark.unit
def test_two_children_of_one_parent_make_two_heads(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND, tmp_path / "app.db")
    graph = graph_summary(cfg)
    assert graph.heads == ("aaaa1", "bbbb1")
    assert graph.bases == ("base1",)
    assert graph.upgrade_order[0] == "base1"


@pytest.mark.unit
def test_upgrade_head_refuses_divergent_heads(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND, tmp_path / "app.db")
    with pytest.raises(CommandError, match="Multiple head revisions are present"):
        command.upgrade(cfg, "head")
    assert asyncio.run(db_state(url_of(cfg))) == ((), ())


@pytest.mark.unit
def test_upgrade_heads_stores_one_version_row_per_head(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND, tmp_path / "app.db")
    command.upgrade(cfg, "heads")
    rows, tables = asyncio.run(db_state(url_of(cfg)))
    assert rows == ("aaaa1", "bbbb1")
    assert tables == ("alembic_version", "t0", "ta", "tb")


@pytest.mark.unit
def test_merge_revision_collapses_heads_and_rows(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND, tmp_path / "app.db")
    command.upgrade(cfg, "heads")
    merge = command.merge(cfg, "heads", message="merge", rev_id="mmmm1")
    assert merge is not None
    assert sorted(merge.down_revision or ()) == ["aaaa1", "bbbb1"]
    assert graph_summary(cfg).heads == ("mmmm1",)
    command.upgrade(cfg, "head")
    assert asyncio.run(db_state(url_of(cfg)))[0] == ("mmmm1",)


@pytest.mark.unit
def test_relative_downgrade_from_merge_point_is_ambiguous(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND, tmp_path / "app.db")
    command.merge(cfg, "heads", message="merge", rev_id="mmmm1")
    command.upgrade(cfg, "head")
    with pytest.raises(CommandError, match="Ambiguous walk"):
        command.downgrade(cfg, "-1")
    command.downgrade(cfg, "aaaa1")
    assert asyncio.run(db_state(url_of(cfg)))[0] == ("aaaa1", "bbbb1")


@pytest.mark.unit
def test_downgrade_base_round_trip_keeps_empty_version_table(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND, tmp_path / "app.db")
    command.upgrade(cfg, "heads")
    before = asyncio.run(db_state(url_of(cfg)))
    command.downgrade(cfg, "base")
    assert asyncio.run(db_state(url_of(cfg))) == ((), ("alembic_version",))
    command.upgrade(cfg, "heads")
    assert asyncio.run(db_state(url_of(cfg))) == before


@pytest.mark.unit
def test_depends_on_makes_get_heads_disagree_with_version_table(tmp_path: Path) -> None:
    specs = (
        RevSpec("xxxx1", None, ("CREATE TABLE tx (id INTEGER)",), branch_labels=("billing",)),
        RevSpec(
            "yyyy1",
            None,
            ("CREATE TABLE ty (id INTEGER)",),
            depends_on="xxxx1",
            branch_labels=("audit",),
        ),
    )
    cfg = write_project(tmp_path, specs, tmp_path / "app.db")
    graph = graph_summary(cfg)
    assert graph.heads == ("xxxx1", "yyyy1")
    assert graph.real_heads == ("yyyy1",)
    command.upgrade(cfg, "heads")
    assert asyncio.run(db_state(url_of(cfg)))[0] == graph.real_heads


@pytest.mark.unit
def test_cycles_are_rejected_when_the_map_is_built() -> None:
    with pytest.raises(LoopDetected, match=r"Self-loop is detected in revisions \(a\)"):
        Revision("a", "a")
    cyclic = RevisionMap(lambda: [Revision("x", None), Revision("a", "b"), Revision("b", "a")])
    with pytest.raises(CycleDetected, match=r"Cycle is detected in revisions \(a, b\)"):
        _ = cyclic.heads


@pytest.mark.unit
def test_partial_identifiers_match_by_unique_prefix_of_any_length() -> None:
    rmap = RevisionMap(lambda: [Revision("ae1027a6acf", None), Revision("ae10ffee0000", None)])
    with pytest.raises(ResolutionError, match="Multiple revisions start with 'ae1'"):
        rmap.get_revision("ae1")
    with pytest.raises(ResolutionError, match="please ensure at least four characters"):
        rmap.get_revision("zz")
    rev = rmap.get_revision("ae102")
    assert rev is not None
    assert rev.revision == "ae1027a6acf"
    lone = RevisionMap(lambda: [Revision("ae1027a6acf", None)]).get_revision("a")
    assert lone is not None
    assert lone.revision == "ae1027a6acf"


@pytest.mark.unit
def test_generated_revision_ids_are_12_hex_chars() -> None:
    ids = {rev_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids)


@pytest.mark.unit
def test_failed_migration_rolls_back_per_transaction_scope(tmp_path: Path) -> None:
    per_migration = write_project(tmp_path / "a", FAILING_CHAIN, tmp_path / "a.db")
    with pytest.raises(OperationalError, match="no such table"):
        command.upgrade(per_migration, "heads")
    assert asyncio.run(db_state(url_of(per_migration))) == (
        ("r2",),
        ("alembic_version", "t1", "t2"),
    )

    one_transaction = write_project(tmp_path / "b", FAILING_CHAIN, tmp_path / "b.db")
    one_transaction.attributes["transactional_ddl"] = True
    with pytest.raises(OperationalError, match="no such table"):
        command.upgrade(one_transaction, "heads")
    assert asyncio.run(db_state(url_of(one_transaction))) == ((), ())


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:coroutine 'run_async_migrations' was never awaited")
async def test_async_env_needs_run_sync_inside_a_running_loop(tmp_path: Path) -> None:
    cfg = write_project(tmp_path, DIAMOND[:2], tmp_path / "app.db")
    with pytest.raises(RuntimeError, match=r"asyncio.run\(\) cannot be called from a running"):
        command.upgrade(cfg, "heads")
    await upgrade_in_running_loop(cfg, "heads")
    assert await db_state(url_of(cfg)) == (("aaaa1",), ("alembic_version", "t0", "ta"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autogenerate_sees_a_rename_as_drop_plus_add(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"
    await exec_ddl(url, "CREATE TABLE users (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(50))")
    md = MetaData()
    Table("users", md, Column("id", Integer, primary_key=True), Column("full_name", String(50)))
    assert await autogenerate_diff(url, md) == ["add_column", "remove_column"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_type_change_is_on_by_default_server_default_is_opt_in(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"
    await exec_ddl(
        url,
        "CREATE TABLE jobs (id INTEGER NOT NULL PRIMARY KEY,"
        " code VARCHAR(10), status VARCHAR(10) DEFAULT 'new')",
    )
    md = MetaData()
    Table(
        "jobs",
        md,
        Column("id", Integer, primary_key=True),
        Column("code", String(20)),
        Column("status", String(10), server_default="queued"),
    )
    assert await autogenerate_diff(url, md) == ["modify_type"]
    assert await autogenerate_diff(url, md, compare_server_default=True) == [
        "modify_type",
        "modify_default",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_check_constraints_need_the_opt_in_plugin(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"
    await exec_ddl(url, "CREATE TABLE stock (id INTEGER NOT NULL PRIMARY KEY, qty INTEGER)")
    md = MetaData()
    Table(
        "stock",
        md,
        Column("id", Integer, primary_key=True),
        Column("qty", Integer),
        CheckConstraint("qty >= 0", name="ck_stock_qty"),
    )
    assert await autogenerate_diff(url, md) == []
    plugins = ["alembic.autogenerate.*", "alembic.ext.checkconstraint_byname"]
    assert await autogenerate_diff(url, md, plugins=plugins) == ["add_constraint"]
