"""Async SQLite engine, PRAGMAs and schema initialisation."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db.models import Base

logger = logging.getLogger("app.db")

PRAGMAS_FILE = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)
PRAGMAS_MEMORY = ("PRAGMA foreign_keys=ON",)


class Database:
    def __init__(self, async_url: str, path: Path | None) -> None:
        self.path = path
        self.in_memory = path is None
        kwargs: dict = {}
        if self.in_memory:
            # One shared connection, otherwise every connection gets its own empty DB.
            kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        self.engine: AsyncEngine = create_async_engine(async_url, **kwargs)
        pragmas = PRAGMAS_MEMORY if self.in_memory else PRAGMAS_FILE

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _record) -> None:  # pragma: no cover - trivial
            cursor = dbapi_conn.cursor()
            for pragma in pragmas:
                cursor.execute(pragma)
            cursor.close()

        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init_schema(self) -> None:
        """Create the parent directory and all tables/indexes (idempotent)."""
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database ready at %s", "<memory>" if self.in_memory else str(self.path))

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 — reported via /health, never raised
            logger.warning("database ping failed: %s", type(exc).__name__)
            return False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
