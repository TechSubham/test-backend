"""Session service — history, stored system prompt, per-session locks (ARCHITECTURE §6)."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import SessionNotFound
from app.db.database import Database
from app.db.repository import MessageRepository, SessionRepository, StoredMessage


@dataclass(frozen=True)
class SessionContext:
    exists: bool
    stored_system_prompt: str | None
    history: list[StoredMessage]  # bounded window for the LLM, oldest first


class SessionService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        # Weak values: a lock disappears once no request holds or waits on it.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @asynccontextmanager
    async def lock(self, session_id: str) -> AsyncIterator[None]:
        """Serialise turns within one session; different sessions run concurrently."""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        async with lock:
            yield

    async def load_context(self, session_id: str) -> SessionContext:
        async with self.database.session() as db:
            row = await SessionRepository(db).get(session_id)
            if row is None:
                return SessionContext(False, None, [])
            history = await MessageRepository(db).recent_for_session(
                session_id, self.settings.max_history_messages
            )
            return SessionContext(True, row.system_prompt, history)

    async def full_history(self, session_id: str) -> list[StoredMessage]:
        """All stored messages, oldest first. The system prompt is never a message."""
        async with self.database.session() as db:
            if not await SessionRepository(db).exists(session_id):
                raise SessionNotFound()
            return await MessageRepository(db).list_for_session(session_id)
