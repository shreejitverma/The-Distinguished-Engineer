"""Lab: pool sizing across replicas, and prepared statements under PgBouncer transaction mode."""

from __future__ import annotations

import importlib
import inspect
import re

import pytest
from psycopg._preparing import PrepareManager
from sqlalchemy import create_engine, event
from sqlalchemy.pool import QueuePool

from api_labs.f13_database_access_and_transactions_for_apis.pool_sizing import (
    AppTier,
    AsyncpgLikeConnection,
    Bouncer,
    BouncerTopology,
    DriverProcess,
    PgError,
    PoolMode,
    PsycopgLikeConnection,
    ServerLimits,
    budget,
    deadlock_free_pool_size,
    largest_safe_ceiling,
    littles_law_connections,
    nullpool_connects,
    per_process_ceiling,
    probe_queuepool,
    surge_pods,
)

USERS = "SELECT id, email FROM users WHERE id = $1"
ORDERS = "SELECT id, total FROM orders WHERE id = $1"

# ---------------------------------------------------------------------------
# Budget arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_surge_percentage_rounds_up() -> None:
    assert surge_pods(10, "25%") == 3  # 2.5 -> 3
    assert surge_pods(4, "25%") == 1
    assert surge_pods(1, "25%") == 1  # even one replica surges by a whole pod
    assert surge_pods(8, 0) == 0
    with pytest.raises(ValueError, match="percentage"):
        surge_pods(4, "25")


@pytest.mark.unit
def test_per_process_ceiling_is_pool_size_plus_overflow() -> None:
    assert per_process_ceiling(5, 10) == 15  # SQLAlchemy defaults
    assert per_process_ceiling(5, 0) == 5
    assert per_process_ceiling(0, 10) is None  # pool_size=0 means no limit
    assert per_process_ceiling(5, -1) is None


@pytest.mark.unit
def test_default_engine_on_a_small_fleet_overruns_default_max_connections() -> None:
    api = AppTier("api", replicas=4, workers_per_pod=4)  # pool_size=5, max_overflow=10
    assert api.processes_at_peak() == (4 + 1) * 4
    report = budget([api], ServerLimits())
    assert report.usable == 97  # 100 - superuser_reserved_connections 3
    assert report.demand == 300
    assert report.headroom == -203
    assert not report.fits


@pytest.mark.unit
def test_rollout_surge_is_what_breaks_a_budget_that_fits_at_steady_state() -> None:
    steady = AppTier("api", replicas=4, workers_per_pod=2, pool_size=5, max_overflow=5, max_surge=0)
    rolling = AppTier("api", replicas=4, workers_per_pod=2, pool_size=5, max_overflow=5)
    assert budget([steady], ServerLimits()).demand == 80
    assert budget([steady], ServerLimits()).fits
    assert budget([rolling], ServerLimits()).demand == 100
    assert not budget([rolling], ServerLimits()).fits


@pytest.mark.unit
def test_reserved_connections_shrink_the_app_budget() -> None:
    assert ServerLimits(100, 3, 0).usable_by_app_roles() == 97
    assert ServerLimits(100, 3, 10).usable_by_app_roles() == 87
    assert ServerLimits(500, 3, 5).usable_by_app_roles() == 492


@pytest.mark.unit
def test_largest_safe_ceiling_fits_and_one_more_does_not() -> None:
    limits = ServerLimits()
    tiers = [AppTier("api", replicas=4, workers_per_pod=4), AppTier("worker", 2, 1)]
    # api: 5 pods x 4 = 20 processes, worker: 3 pods x 1 = 3 processes
    other = 7  # migrations, cron, psql sessions
    ceiling = largest_safe_ceiling(tiers, limits, other)
    assert ceiling == (97 - 7) // 23 == 3

    def sized(c: int) -> list[AppTier]:
        return [
            AppTier("api", 4, 4, pool_size=c, max_overflow=0),
            AppTier("worker", 2, 1, pool_size=c, max_overflow=0),
        ]

    assert budget(sized(ceiling), limits, other).fits
    assert not budget(sized(ceiling + 1), limits, other).fits


@pytest.mark.unit
def test_littles_law_gives_busy_connections() -> None:
    assert littles_law_connections(2000, 0.005) == 10
    assert littles_law_connections(2000, 0.0051) == 11  # rounds up
    assert littles_law_connections(0, 1.0) == 0


@pytest.mark.unit
def test_hikari_deadlock_free_formula() -> None:
    assert deadlock_free_pool_size(3, 4) == 10  # HikariCP wiki example
    assert deadlock_free_pool_size(8, 3) == 17  # HikariCP wiki example
    assert deadlock_free_pool_size(40, 1) == 1  # one connection per thread never deadlocks


@pytest.mark.unit
def test_bouncer_server_side_connections() -> None:
    assert BouncerTopology(instances=2, pools=1).server_connections() == 40
    assert BouncerTopology(2, 1, reserve_pool_size=5).server_connections() == 50
    capped = BouncerTopology(2, 3, default_pool_size=20, max_db_connections=30)
    assert capped.server_connections() == 60  # 3 pools x 20 capped at 30 per instance


# ---------------------------------------------------------------------------
# Real SQLAlchemy pools
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_real_queuepool_holds_at_most_pool_size_plus_overflow() -> None:
    probe = probe_queuepool(pool_size=2, max_overflow=1, attempts=5, timeout=0.05)
    assert probe.max_checked_out == 3
    assert probe.dbapi_connects == 3
    assert probe.error is not None
    assert probe.error.startswith("QueuePool limit of size 2 overflow 1 reached")


@pytest.mark.unit
def test_real_queuepool_nested_checkout_deadlocks_below_hikari_bound() -> None:
    # One request holds a connection and its dependency asks for a second one.
    starved = probe_queuepool(pool_size=1, max_overflow=0, attempts=2, timeout=0.05)
    assert starved.max_checked_out == 1
    assert starved.error is not None
    assert starved.error.startswith("QueuePool limit of size 1 overflow 0 reached")
    enough = probe_queuepool(
        pool_size=deadlock_free_pool_size(1, 2), max_overflow=0, attempts=2, timeout=0.05
    )
    assert enough.max_checked_out == 2
    assert enough.error is None


@pytest.mark.unit
def test_queuepool_reuses_and_nullpool_reconnects() -> None:
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=5, max_overflow=10)
    connects = 0

    def on_connect(*_: object) -> None:
        nonlocal connects
        connects += 1

    event.listen(engine, "connect", on_connect)
    for _ in range(4):
        with engine.connect():
            pass
    engine.dispose()
    assert connects == 1
    assert nullpool_connects(4) == 4


@pytest.mark.unit
def test_pinned_library_defaults() -> None:
    qp = inspect.signature(QueuePool.__init__).parameters
    assert qp["pool_size"].default == 5
    assert qp["max_overflow"].default == 10
    assert qp["timeout"].default == 30.0
    connect = inspect.signature(importlib.import_module("asyncpg").connect).parameters
    assert connect["statement_cache_size"].default == 100
    assert connect["max_cached_statement_lifetime"].default == 300
    assert connect["max_cacheable_statement_size"].default == 15 * 1024
    assert PrepareManager.prepare_threshold == 5
    assert PrepareManager.prepared_max == 100
    adapter_cls = importlib.import_module(
        "sqlalchemy.dialects.postgresql.asyncpg"
    ).AsyncAdapt_asyncpg_connection
    adapter = inspect.signature(adapter_cls.__init__).parameters
    assert adapter["prepared_statement_cache_size"].default == 100
    # SQLAlchemy names nothing itself: asyncpg picks the name, named iff its cache is on.
    assert adapter_cls._default_name_func() is None


@pytest.mark.unit
def test_real_asyncpg_statement_names_come_from_one_process_global_counter() -> None:
    conn_cls = importlib.import_module("asyncpg.connection").Connection
    first = conn_cls._get_unique_id(object(), "stmt")
    second = conn_cls._get_unique_id(object(), "stmt")  # a different "connection"
    match_first = re.fullmatch(r"__asyncpg_stmt_([0-9a-f]+)__", first)
    match_second = re.fullmatch(r"__asyncpg_stmt_([0-9a-f]+)__", second)
    assert match_first is not None
    assert match_second is not None
    assert int(match_second.group(1), 16) == int(match_first.group(1), 16) + 1


# ---------------------------------------------------------------------------
# PgBouncer transaction pooling model
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_transaction_mode_unlinks_server_at_idle_status() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    bouncer.connect("a")
    bouncer.begin("a")
    assert bouncer.server_of("a") == 1000  # status 'T': still linked
    bouncer.commit("a")
    assert bouncer.server_of("a") is None  # status 'I': released
    session = Bouncer(PoolMode.SESSION, pool_size=2)
    session.connect("a")
    session.begin("a")
    session.commit("a")
    assert session.server_of("a") == 1000


@pytest.mark.unit
def test_session_mode_keeps_named_statements_working() -> None:
    bouncer = Bouncer(PoolMode.SESSION, pool_size=2)
    a = AsyncpgLikeConnection(bouncer, "a", DriverProcess(), statement_cache_size=100)
    b = AsyncpgLikeConnection(bouncer, "b", DriverProcess(), statement_cache_size=100)
    for _ in range(3):
        assert a.transaction(USERS) == [USERS]
        assert b.transaction(ORDERS) == [ORDERS]


def _cross_link(bouncer: Bouncer, a: AsyncpgLikeConnection, b: AsyncpgLikeConnection) -> None:
    """A and B run overlapping transactions, so they hold different servers."""
    bouncer.begin("a")
    bouncer.begin("b")
    assert (bouncer.server_of("a"), bouncer.server_of("b")) == (1000, 1001)
    a.fetch(USERS)
    b.fetch(ORDERS)
    bouncer.commit("a")
    bouncer.commit("b")  # 1001 is now on top of the LIFO idle stack


@pytest.mark.unit
def test_cached_name_is_missing_on_the_next_server() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    process = DriverProcess()
    a = AsyncpgLikeConnection(bouncer, "a", process, statement_cache_size=100)
    b = AsyncpgLikeConnection(bouncer, "b", process, statement_cache_size=100)
    _cross_link(bouncer, a, b)
    with pytest.raises(PgError) as info:
        a.transaction(USERS)  # lands on 1001, which only has __asyncpg_stmt_2__
    assert info.value.sqlstate == "26000"
    assert info.value.message == 'prepared statement "__asyncpg_stmt_1__" does not exist'


@pytest.mark.unit
def test_two_processes_collide_on_the_same_name() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    a = AsyncpgLikeConnection(bouncer, "a", DriverProcess(), statement_cache_size=100)
    b = AsyncpgLikeConnection(bouncer, "b", DriverProcess(), statement_cache_size=100)
    assert a.transaction(USERS) == [USERS]  # __asyncpg_stmt_1__ on 1000, then released
    with pytest.raises(PgError) as info:
        b.transaction(ORDERS)  # worker 2 also starts at 1, and lands on 1000
    assert info.value.sqlstate == "42P05"
    assert info.value.message == 'prepared statement "__asyncpg_stmt_1__" already exists'


@pytest.mark.unit
def test_colliding_name_can_silently_run_another_clients_query() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    a = AsyncpgLikeConnection(bouncer, "a", DriverProcess(), statement_cache_size=100)
    b = AsyncpgLikeConnection(bouncer, "b", DriverProcess(), statement_cache_size=100)
    _cross_link(bouncer, a, b)  # both processes named their statement __asyncpg_stmt_1__
    assert a.transaction(USERS) == [ORDERS]  # wrong statement, no error from the model


@pytest.mark.unit
def test_statement_cache_size_zero_survives_multiplexing() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    a = AsyncpgLikeConnection(bouncer, "a", DriverProcess(), statement_cache_size=0)
    b = AsyncpgLikeConnection(bouncer, "b", DriverProcess(), statement_cache_size=0)
    _cross_link(bouncer, a, b)
    assert a.transaction(USERS) == [USERS]
    assert b.transaction(ORDERS) == [ORDERS]
    assert sum(be.parses for be in bouncer.backends) == 4  # one Parse per execution
    assert all(be.prepared == {} for be in bouncer.backends)


@pytest.mark.unit
def test_psycopg_fails_only_after_prepare_threshold() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    a = PsycopgLikeConnection(bouncer, "a")
    b = PsycopgLikeConnection(bouncer, "b")
    for _ in range(6):
        assert a.execute(USERS) == USERS  # 6th run prepares _pg3_0 on 1000
    assert bouncer.backends[0].prepared == {"_pg3_0": USERS}
    for _ in range(5):
        assert b.execute(ORDERS) == ORDERS  # unnamed while below the threshold
    with pytest.raises(PgError) as info:
        b.execute(ORDERS)  # every connection's first name is _pg3_0
    assert info.value.sqlstate == "42P05"
    assert info.value.message == 'prepared statement "_pg3_0" already exists'


@pytest.mark.unit
def test_psycopg_prepare_threshold_none_survives_multiplexing() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    a = PsycopgLikeConnection(bouncer, "a", prepare_threshold=None)
    b = PsycopgLikeConnection(bouncer, "b", prepare_threshold=None)
    for _ in range(10):
        assert a.execute(USERS) == USERS
        assert b.execute(ORDERS) == ORDERS
    assert all(be.prepared == {} for be in bouncer.backends)


@pytest.mark.unit
def test_max_prepared_statements_rewrites_names_and_prepares_on_demand() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2, max_prepared_statements=200)
    process = DriverProcess()
    a = AsyncpgLikeConnection(bouncer, "a", process, statement_cache_size=100)
    b = AsyncpgLikeConnection(bouncer, "b", process, statement_cache_size=100)
    _cross_link(bouncer, a, b)
    assert a.transaction(USERS) == [USERS]  # re-prepared on 1001 under PGBOUNCER_1
    assert set(bouncer.backends[1].prepared) == {"PGBOUNCER_1", "PGBOUNCER_2"}
    c = AsyncpgLikeConnection(bouncer, "c", DriverProcess(), statement_cache_size=100)
    assert c.transaction(USERS) == [USERS]  # a third client reuses PGBOUNCER_1
    assert sum(be.parses for be in bouncer.backends) == 3  # once per (server, query)


@pytest.mark.unit
def test_max_prepared_statements_also_fixes_cross_process_collisions() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2, max_prepared_statements=200)
    a = AsyncpgLikeConnection(bouncer, "a", DriverProcess(), statement_cache_size=100)
    b = AsyncpgLikeConnection(bouncer, "b", DriverProcess(), statement_cache_size=100)
    _cross_link(bouncer, a, b)
    assert a.transaction(USERS) == [USERS]
    assert b.transaction(ORDERS) == [ORDERS]
    assert PsycopgLikeConnection(bouncer, "p").execute(USERS) == USERS


@pytest.mark.unit
def test_small_max_prepared_statements_thrashes() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=1, max_prepared_statements=1)
    a = AsyncpgLikeConnection(bouncer, "a", DriverProcess(), statement_cache_size=100)
    for _ in range(5):
        assert a.transaction(USERS, ORDERS) == [USERS, ORDERS]
    assert bouncer.backends[0].parses == 10  # every execution re-Parses
    roomy = Bouncer(PoolMode.TRANSACTION, pool_size=1, max_prepared_statements=2)
    r = AsyncpgLikeConnection(roomy, "a", DriverProcess(), statement_cache_size=100)
    for _ in range(5):
        r.transaction(USERS, ORDERS)
    assert roomy.backends[0].parses == 2


@pytest.mark.unit
def test_set_outside_a_transaction_leaks_to_the_next_client() -> None:
    bouncer = Bouncer(PoolMode.TRANSACTION, pool_size=2)
    bouncer.connect("a")
    bouncer.connect("b")
    bouncer.set_guc("a", "statement_timeout", "1s")  # runs on 1000, which is then released
    assert bouncer.show_guc("b", "statement_timeout") == "1s"  # b inherits a's setting
    session = Bouncer(PoolMode.SESSION, pool_size=2)
    session.connect("a")
    session.connect("b")
    session.set_guc("a", "statement_timeout", "1s")
    assert session.show_guc("b", "statement_timeout") is None
