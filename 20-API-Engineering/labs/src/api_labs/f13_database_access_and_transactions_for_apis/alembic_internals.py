"""Alembic 1.20.1 lab: revision DAG, branch merge, autogenerate blind spots, async env.py.

Every helper drives the real Alembic machinery (``ScriptDirectory``, ``command``,
``MigrationContext``, ``compare_metadata``) against a throwaway project on disk and an
aiosqlite file database, so nothing touches the network and every run is deterministic.
The generated ``env.py`` is the stock ``alembic init -t async`` template plus the
cookbook's connection-sharing branch, so the same file serves the CLI path
(``asyncio.run``) and the in-loop programmatic path (``AsyncConnection.run_sync``).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import alembic
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection, MetaData

RevId = str | tuple[str, ...] | None

# Stock async template (alembic 1.20.1 templates/async/env.py) plus the cookbook branch
# "Programmatic API use (connection sharing) With Asyncio". The lab-only knobs are read
# from Config.attributes so a test can flip transactional_ddl without editing the file.
ENV_PY = """\
import asyncio

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

config = context.config
target_metadata = config.attributes.get("target_metadata")


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transactional_ddl=config.attributes.get("transactional_ddl"),
        transaction_per_migration=config.attributes.get(
            "transaction_per_migration", False
        ),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"autocommit": False},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online():
    connection = config.attributes.get("connection")
    if connection is None:
        asyncio.run(run_async_migrations())
    else:
        do_run_migrations(connection)


if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
"""


@dataclass(frozen=True)
class RevSpec:
    """One migration file: its identity, its parents, and the SQL it runs."""

    rev: str
    down: RevId
    upgrade_sql: tuple[str, ...] = ()
    downgrade_sql: tuple[str, ...] = ()
    depends_on: RevId = None
    branch_labels: RevId = None


@dataclass(frozen=True)
class GraphSummary:
    """What ``ScriptDirectory`` derives from the files alone, before any database."""

    heads: tuple[str, ...]
    real_heads: tuple[str, ...]
    bases: tuple[str, ...]
    upgrade_order: tuple[str, ...]


def render_revision(spec: RevSpec) -> str:
    """Render a migration module the way script.py.mako would, minus the docstring."""
    up = "\n".join(f"    op.execute({sql!r})" for sql in spec.upgrade_sql) or "    pass"
    down = "\n".join(f"    op.execute({sql!r})" for sql in spec.downgrade_sql) or "    pass"
    return (
        "from alembic import op\n\n"
        f"revision = {spec.rev!r}\n"
        f"down_revision = {spec.down!r}\n"
        f"branch_labels = {spec.branch_labels!r}\n"
        f"depends_on = {spec.depends_on!r}\n\n\n"
        f"def upgrade() -> None:\n{up}\n\n\n"
        f"def downgrade() -> None:\n{down}\n"
    )


def write_project(root: Path, specs: Sequence[RevSpec], db_path: Path) -> Config:
    """Create script_location, env.py, script.py.mako, and one file per revision."""
    script_dir = root / "migrations"
    versions = script_dir / "versions"
    versions.mkdir(parents=True)
    (script_dir / "env.py").write_text(ENV_PY)
    template = Path(alembic.__file__).parent / "templates" / "async" / "script.py.mako"
    shutil.copy(template, script_dir / "script.py.mako")
    for spec in specs:
        (versions / f"{spec.rev}_.py").write_text(render_revision(spec))
    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def graph_summary(cfg: Config) -> GraphSummary:
    """Heads, dependency-aware heads, bases, and the topological upgrade order."""
    script = ScriptDirectory.from_config(cfg)
    walked = [s.revision for s in script.walk_revisions("base", "heads")]
    return GraphSummary(
        heads=tuple(sorted(script.get_heads())),
        real_heads=tuple(sorted(script.get_heads(consider_depends_on=True))),
        bases=tuple(sorted(script.get_bases())),
        upgrade_order=tuple(reversed(walked)),
    )


def _heads_on(connection: Connection) -> tuple[str, ...]:
    return tuple(sorted(MigrationContext.configure(connection).get_current_heads()))


def _table_names(connection: Connection) -> tuple[str, ...]:
    return tuple(sorted(inspect(connection).get_table_names()))


async def db_state(url: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (rows of alembic_version, table names) read through an AsyncEngine."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            heads = await conn.run_sync(_heads_on)
            tables = await conn.run_sync(_table_names)
    finally:
        await engine.dispose()
    return heads, tables


def _upgrade_on(connection: Connection, cfg: Config, target: str) -> None:
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, target)


async def upgrade_in_running_loop(cfg: Config, target: str) -> None:
    """Cookbook path: share one AsyncConnection's sync facade with command.upgrade."""
    url = cfg.get_main_option("sqlalchemy.url")
    if url is None:
        raise ValueError("sqlalchemy.url is not set")
    engine = create_async_engine(url, connect_args={"autocommit": False})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_upgrade_on, cfg, target)
    finally:
        await engine.dispose()


def diff_kinds(diffs: Sequence[Any]) -> list[str]:
    """Flatten compare_metadata output to op names: tuples are one op, lists group modify_*."""
    kinds: list[str] = []
    for entry in diffs:
        if isinstance(entry, list):
            kinds.extend(str(item[0]) for item in entry)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        else:
            kinds.append(str(entry[0]))
    return kinds


async def autogenerate_diff(
    url: str,
    metadata: MetaData,
    *,
    compare_server_default: bool = False,
    plugins: Sequence[str] | None = None,
) -> list[str]:
    """Run the autogenerate comparison of metadata against the live schema."""
    opts: dict[str, Any] = {"compare_server_default": compare_server_default}
    if plugins is not None:
        opts["autogenerate_plugins"] = list(plugins)

    def run(connection: Connection) -> list[str]:
        ctx = MigrationContext.configure(connection, opts=opts)
        return diff_kinds(compare_metadata(ctx, metadata))

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(run)
    finally:
        await engine.dispose()


async def exec_ddl(url: str, *statements: str) -> None:
    """Create the "live" schema that autogenerate will reflect."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for sql in statements:
                await conn.exec_driver_sql(sql)
    finally:
        await engine.dispose()
