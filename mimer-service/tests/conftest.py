"""Test fixtures.

Tests run against an in-memory SQLite database (via aiosqlite) so they need no
running Postgres. The same ORM models and seed builders are reused, giving real
integration coverage of the API + service + DB layers. The `get_session`
dependency is overridden to point at the test database.

* `client`  — an httpx AsyncClient bound to the app, sharing the test DB.
* `session` — a raw AsyncSession on the same DB (for service/worker tests).
"""

from __future__ import annotations

import os

# Keep the suite hermetic: neutralise ambient secrets/config that a developer's
# local ``.env`` would otherwise leak into tests (env vars override ``.env`` in
# pydantic-settings). Tests must not depend on whether an OpenFIGI key is set.
# Done before importing the app so the cached Settings never sees the real value.
os.environ["OPENFIGI_API_KEY"] = ""

from collections.abc import AsyncGenerator  # noqa: E402

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.seed.seed_data import (
    _build_data_sources,
    _build_funds,
    _build_fx_rates,
    _build_positions,
    _build_scheduled_jobs,
    _build_user_and_workspace,
    _build_workspace_children,
)
from app.services.source_budget import build_default_rows  # noqa: E402


@pytest_asyncio.fixture
async def session_local() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        session.add_all(_build_data_sources())
        funds = _build_funds()
        session.add_all(funds.values())
        user, workspace = _build_user_and_workspace()
        session.add_all([user, workspace])
        await session.flush()
        session.add_all(_build_workspace_children(user, workspace))
        session.add_all(_build_positions(funds, workspace.id))
        session.add_all(_build_fx_rates())
        session.add_all(_build_scheduled_jobs())
        session.add_all(build_default_rows())
        await session.commit()

    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(
    session_local: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session() -> AsyncGenerator:
        async with session_local() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def session(
    session_local: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with session_local() as session:
        yield session
