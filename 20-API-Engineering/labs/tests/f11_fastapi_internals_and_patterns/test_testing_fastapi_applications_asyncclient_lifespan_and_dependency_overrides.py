"""Unit tests for FastAPI testing with AsyncClient, LifespanManager, and transactional rollback."""

from collections.abc import AsyncIterator, Callable
from typing import Annotated

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from api_labs.f11_fastapi_internals_and_patterns.fastapi_testing_harness import (
    AppLifecycleState,
    LedgerAccount,
    create_app,
    create_test_engine,
    create_transactional_session,
    get_current_user,
    get_db,
    init_db,
)


async def setup_test_engine() -> AsyncEngine:
    """Initialize an in-memory SQLite engine with autocommit=False and create tables."""
    engine = create_test_engine()
    await init_db(engine)
    return engine


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_manager_initializes_app_state_and_cleanly_shuts_down() -> None:
    engine = await setup_test_engine()
    try:
        tracker = AppLifecycleState()
        app = create_app(engine, lifecycle_state=tracker)

        assert not tracker.startup_called
        assert not tracker.shutdown_called

        async with LifespanManager(app) as manager:
            assert tracker.startup_called
            assert not tracker.shutdown_called
            assert tracker.active_connections == 1
            has_state = getattr(manager.app, "state", None) or getattr(app, "state", None)
            assert has_state is not None

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/health")
                assert response.status_code == status.HTTP_200_OK
                payload = response.json()
                assert payload["status"] == "healthy"
                assert payload["ready"] is True
                assert payload["startup_called"] is True

        assert tracker.shutdown_called
        assert tracker.active_connections == 0
        assert not getattr(app.state, "ready", True)
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asyncclient_without_lifespan_fails_to_initialize_state() -> None:
    engine = await setup_test_engine()
    try:
        tracker = AppLifecycleState()
        app = create_app(engine, lifecycle_state=tracker)

        # ASGITransport directly invokes http scope, bypassing lifespan startup events
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health")
            assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
            assert response.json() == {"detail": "Application lifespan uninitialized"}

        assert not tracker.startup_called
        assert not tracker.shutdown_called
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_override_replaces_auth_dependency() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Without override and without auth header, endpoint rejects with 401
            unauth_resp = await client.get("/me")
            assert unauth_resp.status_code == status.HTTP_401_UNAUTHORIZED

            # Apply override on app.dependency_overrides
            async def mock_admin_user() -> str:
                return "mocked_service_admin"

            app.dependency_overrides[get_current_user] = mock_admin_user
            try:
                auth_resp = await client.get("/me")
                assert auth_resp.status_code == status.HTTP_200_OK
                assert auth_resp.json() == {"user": "mocked_service_admin"}
            finally:
                app.dependency_overrides.pop(get_current_user, None)

            # After pop(), original dependency is restored
            restored_resp = await client.get("/me")
            assert restored_resp.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_factory_leaves_overrides_empty_and_production_get_db_commits() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)
        # The production app must ship with an empty override map
        assert app.dependency_overrides == {}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            create_resp = await client.post(
                "/accounts",
                params={"holder_name": "dave_prod", "balance": 10},
            )
            assert create_resp.status_code == status.HTTP_201_CREATED

        # Without the rollback fixture, the real get_db commit is durable
        async with engine.connect() as verify_conn:
            result = await verify_conn.execute(select(LedgerAccount.holder_name))
            assert result.scalars().all() == ["dave_prod"]
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parent_overrides_do_not_reach_mounted_sub_application() -> None:
    engine = await setup_test_engine()
    try:
        parent = create_app(engine)
        child = FastAPI()

        @child.get("/me")
        async def child_me(user: Annotated[str, Depends(get_current_user)]) -> dict[str, str]:
            return {"user": user}

        parent.mount("/sub", child)

        async def mock_user() -> str:
            return "mocked_user"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=parent),
            base_url="http://test",
        ) as client:
            parent.dependency_overrides[get_current_user] = mock_user
            try:
                parent_resp = await client.get("/me")
                assert parent_resp.json() == {"user": "mocked_user"}
                # The mounted FastAPI instance is its own overrides provider
                sub_resp = await client.get("/sub/me")
                assert sub_resp.status_code == status.HTTP_401_UNAUTHORIZED

                child.dependency_overrides[get_current_user] = mock_user
                sub_resp = await client.get("/sub/me")
                assert sub_resp.json() == {"user": "mocked_user"}
            finally:
                parent.dependency_overrides.pop(get_current_user, None)
                child.dependency_overrides.pop(get_current_user, None)
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_override_with_nested_dependencies() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)

        def sub_role_dependency(
            x_tenant: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
        ) -> str:
            if not x_tenant:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X-Tenant-Id header required",
                )
            return f"tenant_{x_tenant}"

        def parameterized_user_override(
            tenant: Annotated[str, Depends(sub_role_dependency)],
        ) -> str:
            return f"user_in_{tenant}"

        app.dependency_overrides[get_current_user] = parameterized_user_override
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                # Missing sub-dependency header fails with 400
                missing_header_resp = await client.get("/me")
                assert missing_header_resp.status_code == status.HTTP_400_BAD_REQUEST
                assert missing_header_resp.json() == {"detail": "X-Tenant-Id header required"}

                # Providing sub-dependency header solves the entire graph
                valid_resp = await client.get("/me", headers={"X-Tenant-Id": "engineering"})
                assert valid_resp.status_code == status.HTTP_200_OK
                assert valid_resp.json() == {"user": "user_in_tenant_engineering"}
        finally:
            app.dependency_overrides.pop(get_current_user, None)
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_override_cache_key_isolation() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)
        invocation_count = 0

        def counting_user_override() -> str:
            nonlocal invocation_count
            invocation_count += 1
            return f"cached_user_v{invocation_count}"

        app.dependency_overrides[get_current_user] = counting_user_override
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.get("/me-cached")
                assert resp.status_code == status.HTTP_200_OK
                body = resp.json()
                assert body["user1"] == "cached_user_v1"
                assert body["user2"] == "cached_user_v1"
                assert body["identical"] is True
                # Overridden dependency must be evaluated only once per request when use_cache=True
                assert invocation_count == 1
        finally:
            app.dependency_overrides.pop(get_current_user, None)
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transactional_rollback_isolates_db_mutations() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)

        # 1. Open a single test connection and begin an outer transaction
        async with engine.connect() as connection:
            outer_trans = await connection.begin()

            # 2. Bind the test session with join_transaction_mode='create_savepoint'
            test_session = create_transactional_session(connection)

            # 3. Override get_db to yield this transactional session
            async def override_db() -> AsyncIterator[AsyncSession]:
                yield test_session

            app.dependency_overrides[get_db] = override_db
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    # Create an account (route executes session.commit())
                    create_resp = await client.post(
                        "/accounts",
                        params={"holder_name": "alice_vault", "balance": 1000},
                    )
                    assert create_resp.status_code == status.HTTP_201_CREATED
                    account_id = create_resp.json()["id"]

                    # Verify within the transaction that the account exists
                    list_resp = await client.get("/accounts")
                    assert list_resp.status_code == status.HTTP_200_OK
                    accounts = list_resp.json()
                    assert len(accounts) == 1
                    assert accounts[0]["id"] == account_id
                    assert accounts[0]["holder_name"] == "alice_vault"
            finally:
                app.dependency_overrides.pop(get_db, None)
                await test_session.close()
                # 4. Roll back the outer transaction
                await outer_trans.rollback()

        # 5. Connect cleanly to the engine to verify table is completely empty
        async with engine.connect() as verify_conn:
            verify_session = AsyncSession(bind=verify_conn)
            result = await verify_session.execute(select(LedgerAccount))
            persisted = result.scalars().all()
            assert len(persisted) == 0
            await verify_session.close()
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sequential_tests_with_savepoint_rollback_have_zero_state_leakage() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)

        # Simulate 5 sequential test iterations with outer rollback
        for iteration in range(1, 6):
            async with engine.connect() as connection:
                outer_trans = await connection.begin()
                session = create_transactional_session(connection)

                # Use factory function so FastAPI does not treat parameter as a query parameter
                def make_db_override(
                    s: AsyncSession,
                ) -> Callable[[], AsyncIterator[AsyncSession]]:
                    async def _override() -> AsyncIterator[AsyncSession]:
                        yield s

                    return _override

                app.dependency_overrides[get_db] = make_db_override(session)
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://test",
                    ) as client:
                        # Initial state must always be 0 rows
                        initial_list = await client.get("/accounts")
                        assert initial_list.json() == []

                        # Mutate state
                        resp = await client.post(
                            "/accounts",
                            params={
                                "holder_name": f"user_iteration_{iteration}",
                                "balance": iteration * 100,
                            },
                        )
                        assert resp.status_code == status.HTTP_201_CREATED

                        # Inside the test, row exists
                        current_list = await client.get("/accounts")
                        assert len(current_list.json()) == 1
                finally:
                    app.dependency_overrides.pop(get_db, None)
                    await session.close()
                    await outer_trans.rollback()
    finally:
        await engine.dispose()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_commits_within_single_request_survive_until_outer_rollback() -> None:
    engine = await setup_test_engine()
    try:
        app = create_app(engine)

        async with engine.connect() as connection:
            outer_trans = await connection.begin()
            session = create_transactional_session(connection)

            async def override_db() -> AsyncIterator[AsyncSession]:
                yield session

            app.dependency_overrides[get_db] = override_db
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    # Create source account (commit 1)
                    res1 = await client.post(
                        "/accounts",
                        params={"holder_name": "bob_sender", "balance": 500},
                    )
                    bob_id = res1.json()["id"]

                    # Create destination account (commit 2)
                    res2 = await client.post(
                        "/accounts",
                        params={"holder_name": "carol_receiver", "balance": 100},
                    )
                    carol_id = res2.json()["id"]

                    # Transfer balance (commit 3)
                    xfer = await client.post(
                        "/transfer",
                        params={"source_id": bob_id, "target_id": carol_id, "amount": 250},
                    )
                    assert xfer.status_code == status.HTTP_200_OK
                    assert xfer.json() == {
                        "source_balance": 250,
                        "target_balance": 350,
                        "transferred": 250,
                    }
            finally:
                app.dependency_overrides.pop(get_db, None)
                await session.close()
                await outer_trans.rollback()

        # Outer rollback cleanly removed all 3 committed operations
        async with engine.connect() as verify_conn:
            verify_session = AsyncSession(bind=verify_conn)
            result = await verify_session.execute(select(LedgerAccount))
            assert len(result.scalars().all()) == 0
            await verify_session.close()
    finally:
        await engine.dispose()
