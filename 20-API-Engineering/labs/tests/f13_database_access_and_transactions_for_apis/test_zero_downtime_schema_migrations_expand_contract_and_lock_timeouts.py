"""Lab for Zero-Downtime-Schema-Migrations-Expand-Contract-and-Lock-Timeouts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic import op

from api_labs.f13_database_access_and_transactions_for_apis.zero_downtime_migrations import (
    CONFLICTS,
    LockMode,
    LockRequest,
    conflicts,
    lint_migration,
    lint_statements,
    parse_timeout_ms,
    render_offline_sql,
    simulate_lock_queue,
)

if TYPE_CHECKING:
    from collections.abc import Callable

M = LockMode

# --------------------------------------------------------------------------- lock queue


@pytest.mark.unit
def test_conflict_matrix_matches_postgres_table_13_2() -> None:
    # Symmetric, 8 modes, and the rows that matter for live APIs.
    for a in LockMode:
        for b in LockMode:
            assert conflicts(a, b) == conflicts(b, a)
    assert CONFLICTS[M.ACCESS_SHARE] == {M.ACCESS_EXCLUSIVE}
    assert not conflicts(M.ROW_EXCLUSIVE, M.ROW_EXCLUSIVE)  # writers share the table
    assert not conflicts(M.ROW_EXCLUSIVE, M.SHARE_UPDATE_EXCLUSIVE)  # VALIDATE, CIC
    assert conflicts(M.ROW_EXCLUSIVE, M.SHARE)  # plain CREATE INDEX blocks writes
    assert conflicts(M.SHARE_UPDATE_EXCLUSIVE, M.SHARE_UPDATE_EXCLUSIVE)
    assert not conflicts(M.SHARE, M.SHARE)
    pairs = sum(conflicts(a, b) for a in LockMode for b in LockMode)
    assert pairs == 38  # count of X marks in Table 13.2


def _gocardless(ddl_timeout_ms: int) -> list[LockRequest]:
    """A slow read, then DDL needing ACCESS EXCLUSIVE, then ordinary API reads."""
    return [
        LockRequest("report", M.ACCESS_SHARE, arrival_ms=0, hold_ms=10_000),
        LockRequest("ddl", M.ACCESS_EXCLUSIVE, 100, 5, lock_timeout_ms=ddl_timeout_ms),
        LockRequest("api_read_1", M.ACCESS_SHARE, 200, 1),
        LockRequest("api_write_1", M.ROW_EXCLUSIVE, 300, 1),
    ]


@pytest.mark.unit
def test_queued_access_exclusive_blocks_later_readers() -> None:
    out = simulate_lock_queue(_gocardless(ddl_timeout_ms=0))
    assert out["report"].granted_ms == 0
    assert out["ddl"].granted_ms == 10_000  # waits for the report to finish
    # The API read never conflicted with the report; it queued behind the DDL.
    assert out["api_read_1"].granted_ms == 10_005
    assert out["api_read_1"].waited_ms == 9_805
    assert out["api_write_1"].waited_ms == 9_705


@pytest.mark.unit
def test_without_wait_queue_rule_readers_jump_the_queue() -> None:
    out = simulate_lock_queue(_gocardless(ddl_timeout_ms=0), respect_wait_queue=False)
    assert out["api_read_1"].waited_ms == 0
    assert out["api_write_1"].waited_ms == 0
    assert out["ddl"].granted_ms == 10_000  # only because the late arrivals hold 1 ms


@pytest.mark.unit
def test_lock_timeout_bounds_the_stall() -> None:
    out = simulate_lock_queue(_gocardless(ddl_timeout_ms=2_000))
    assert out["ddl"].timed_out
    assert out["ddl"].granted_ms is None
    assert out["api_read_1"].granted_ms == 2_100  # woken when the DDL leaves the queue
    assert out["api_read_1"].waited_ms == 1_900
    assert out["api_write_1"].waited_ms == 1_800
    assert not out["api_read_1"].timed_out


@pytest.mark.unit
def test_share_update_exclusive_waiter_lets_dml_through() -> None:
    out = simulate_lock_queue(
        [
            LockRequest("autovacuum", M.SHARE_UPDATE_EXCLUSIVE, 0, 5_000),
            LockRequest("validate", M.SHARE_UPDATE_EXCLUSIVE, 10, 3_000),
            LockRequest("api_write", M.ROW_EXCLUSIVE, 20, 1),
            LockRequest("api_read", M.ACCESS_SHARE, 30, 1),
        ]
    )
    assert out["validate"].granted_ms == 5_000
    assert out["api_write"].waited_ms == 0
    assert out["api_read"].waited_ms == 0


@pytest.mark.unit
def test_plain_create_index_blocks_writers_not_readers() -> None:
    out = simulate_lock_queue(
        [
            LockRequest("create_index", M.SHARE, 0, 60_000),
            LockRequest("api_read", M.ACCESS_SHARE, 10, 1),
            LockRequest("api_write", M.ROW_EXCLUSIVE, 20, 1),
        ]
    )
    assert out["api_read"].waited_ms == 0
    assert out["api_write"].granted_ms == 60_000


# --------------------------------------------------------------------------- rendering


def _small() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.add_column("orders", sa.Column("status", sa.String(20), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index("ix_orders_status", "orders", ["status"], postgresql_concurrently=True)


@pytest.mark.unit
def test_render_offline_sql_matches_alembic_upgrade_sql() -> None:
    assert render_offline_sql(_small) == [
        "BEGIN;",
        "SET LOCAL lock_timeout = '2s';",
        "ALTER TABLE orders ADD COLUMN status VARCHAR(20);",
        "COMMIT;",
        "CREATE INDEX CONCURRENTLY ix_orders_status ON orders (status);",
        "BEGIN;",
        "COMMIT;",
    ]


# --------------------------------------------------------------------------- linter rules


def _timeout() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")


def _add_not_null() -> None:
    _timeout()
    op.add_column("orders", sa.Column("status", sa.String(20), nullable=False))


def _add_volatile_default() -> None:
    _timeout()
    op.add_column(
        "orders", sa.Column("ext_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"))
    )


def _set_not_null() -> None:
    _timeout()
    op.alter_column("orders", "status", nullable=False)


def _fk_validated() -> None:
    _timeout()
    op.create_foreign_key("fk_orders_user", "orders", "users", ["user_id"], ["id"])


def _check_validated() -> None:
    _timeout()
    op.create_check_constraint("ck_total", "orders", "total >= 0")


def _unique() -> None:
    _timeout()
    op.create_unique_constraint("uq_orders_ext_id", "orders", ["ext_id"])


def _plain_index() -> None:
    _timeout()
    op.create_index("ix_orders_status", "orders", ["status"])


def _concurrent_in_txn() -> None:
    op.create_index("ix_orders_status", "orders", ["status"], postgresql_concurrently=True)


def _type_change() -> None:
    _timeout()
    op.alter_column("orders", "total", type_=sa.BigInteger())


def _rename() -> None:
    _timeout()
    op.alter_column("orders", "total", new_column_name="amount")


def _backfill_under_lock() -> None:
    _timeout()
    op.add_column("orders", sa.Column("status", sa.String(20), nullable=True))
    op.execute("UPDATE orders SET status = 'new' WHERE status IS NULL")


def _no_timeout() -> None:
    op.add_column("orders", sa.Column("note", sa.Text(), nullable=True))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("upgrade", "expected"),
    [
        (_add_not_null, {"ZDM002"}),
        (_add_volatile_default, {"ZDM003"}),
        (_set_not_null, {"ZDM004"}),
        (_fk_validated, {"ZDM005"}),
        (_check_validated, {"ZDM005"}),
        (_unique, {"ZDM006"}),
        (_plain_index, {"ZDM007"}),
        (_concurrent_in_txn, {"ZDM008"}),
        (_type_change, {"ZDM009"}),
        (_rename, {"ZDM010"}),
        (_backfill_under_lock, {"ZDM012"}),
        (_no_timeout, {"ZDM001"}),
    ],
    ids=lambda v: getattr(v, "__name__", ""),
)
def test_each_unsafe_operation_trips_exactly_its_rule(
    upgrade: Callable[[], None], expected: set[str]
) -> None:
    assert {f.rule for f in lint_migration(upgrade)} == expected


def _expand() -> None:
    # Expand: nullable column, NOT VALID constraints, concurrent index, then validate
    # and promote to NOT NULL outside the DDL transaction.
    _timeout()
    op.add_column("orders", sa.Column("status", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_orders_status_nn", "orders", "status IS NOT NULL", postgresql_not_valid=True
    )
    op.create_foreign_key(
        "fk_orders_user", "orders", "users", ["user_id"], ["id"], postgresql_not_valid=True
    )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '2s'")
        op.create_index("ix_orders_status", "orders", ["status"], postgresql_concurrently=True)
        op.execute("ALTER TABLE orders VALIDATE CONSTRAINT ck_orders_status_nn")
        op.execute("ALTER TABLE orders VALIDATE CONSTRAINT fk_orders_user")
        op.alter_column("orders", "status", nullable=False)
        op.drop_constraint("ck_orders_status_nn", "orders", type_="check")


def _contract() -> None:
    _timeout()
    op.drop_column("orders", "legacy_status")


def _app(*columns: str) -> sa.MetaData:
    metadata = sa.MetaData()
    sa.Table("orders", metadata, *(sa.Column(c, sa.Integer) for c in columns))
    return metadata


@pytest.mark.unit
def test_safe_expand_migration_is_clean() -> None:
    assert lint_migration(_expand) == []


@pytest.mark.unit
def test_contract_only_after_app_stops_mapping_the_column() -> None:
    old_app = _app("id", "status", "legacy_status")
    new_app = _app("id", "status")
    findings = lint_migration(_contract, app_metadata=old_app)
    assert [(f.rule, f.statement) for f in findings] == [
        ("ZDM011", "ALTER TABLE orders DROP COLUMN legacy_status;")
    ]
    assert lint_migration(_contract, app_metadata=new_app) == []


@pytest.mark.unit
def test_set_local_outside_transaction_has_no_effect() -> None:
    def upgrade() -> None:
        with op.get_context().autocommit_block():
            op.execute("SET LOCAL lock_timeout = '2s'")
            op.add_column("orders", sa.Column("note", sa.Text(), nullable=True))

    findings = lint_migration(upgrade)
    assert [f.rule for f in findings] == ["ZDM001"]
    assert "SET LOCAL outside a transaction had no effect" in findings[0].message


@pytest.mark.unit
def test_set_local_expires_at_commit() -> None:
    statements = [
        "BEGIN;",
        "SET LOCAL lock_timeout = '2s';",
        "ALTER TABLE orders ADD COLUMN a TEXT;",
        "COMMIT;",
        "ALTER TABLE orders ADD COLUMN b TEXT;",
    ]
    assert [(f.rule, f.index) for f in lint_statements(statements)] == [("ZDM001", 4)]


@pytest.mark.unit
def test_zero_lock_timeout_disables_the_guard() -> None:
    statements = ["SET lock_timeout = 0;", "ALTER TABLE orders ADD COLUMN a TEXT;"]
    assert [f.rule for f in lint_statements(statements)] == ["ZDM001"]
    assert parse_timeout_ms("0") == 0
    assert parse_timeout_ms("100ms") == 100
    assert parse_timeout_ms("2s") == 2_000
    assert parse_timeout_ms("1min") == 60_000
    assert parse_timeout_ms("250") == 250  # unitless means milliseconds
    with pytest.raises(ValueError, match="unparseable"):
        parse_timeout_ms("soon")


@pytest.mark.unit
def test_not_valid_then_validate_in_one_transaction_holds_the_lock() -> None:
    statements = [
        "BEGIN;",
        "SET LOCAL lock_timeout = '2s';",
        "ALTER TABLE orders ADD CONSTRAINT ck CHECK (status IS NOT NULL) NOT VALID;",
        "ALTER TABLE orders VALIDATE CONSTRAINT ck;",
        "ALTER TABLE orders ALTER COLUMN status SET NOT NULL;",
        "COMMIT;",
    ]
    assert [(f.rule, f.index) for f in lint_statements(statements)] == [("ZDM012", 3)]


@pytest.mark.unit
def test_postgres_18_not_null_not_valid_also_proves_set_not_null() -> None:
    statements = [
        "SET lock_timeout = '2s';",
        "ALTER TABLE orders ADD CONSTRAINT nn_status NOT NULL status NOT VALID;",
        "ALTER TABLE orders VALIDATE CONSTRAINT nn_status;",
        "ALTER TABLE orders ALTER COLUMN status SET NOT NULL;",
    ]
    assert lint_statements(statements) == []
    assert [f.rule for f in lint_statements([statements[0], statements[3]])] == ["ZDM004"]


@pytest.mark.unit
def test_index_on_table_created_in_same_migration_is_exempt() -> None:
    def upgrade() -> None:
        op.create_table("refunds", sa.Column("id", sa.Integer, primary_key=True))
        op.create_index("ix_refunds_id", "refunds", ["id"])

    assert lint_migration(upgrade) == []


@pytest.mark.unit
def test_stable_now_default_is_metadata_only() -> None:
    def upgrade() -> None:
        _timeout()
        op.add_column(
            "orders",
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        )

    assert lint_migration(upgrade) == []
